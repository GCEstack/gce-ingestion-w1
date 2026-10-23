"""ChatGPT adapter Ã¢â‚¬â€ first-party JSON capture over the WebBridge substrate.

Capture surface (Stage 1 recon, 2026-09-26 -- see
``docs/2026-09-26-CHATGPT-BACKUP-STAGE1-RECON.md``):
the ChatGPT web app fetches its own conversation list and conversation bodies from
same-origin ``/backend-api/...``. The page holds the bearer token and account id in
``#client-bootstrap``. This adapter reads them *inside one evaluate* and uses them on
that same evaluate, so the credential never reaches the driver process, never lands on
disk, and is never logged.

Deliberately NOT used:
* rendered-DOM scraping -- loses tool/citation payloads, has no reliable role signal,
  depends on hashed utility classes, and needs the virtualised history scrolled;
* the ``network`` daemon action -- response bodies are released on stop, so it cannot
  re-read history without a reload per conversation;
* the Chrome ``Cookies`` SQLite DB -- outside the auth boundary.

Doctrines enforced here:
* capture != interpretation -- the stored payload is the provider JSON, byte-preserved;
* no bare ``null`` -- every miss is an explicit :class:`CaptureState`;
* titles are never identity -- dedupe and resume key on conversation id.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from ..failures import FailureCode
from ..transport.base import ProbeResult, TransportClient, TransportError
from .base import AdapterInterface, CaptureResult, EnumerateResult

# -- capture states (explicit; a miss is never a bare null) -------------------
OK = "ok"
NOT_CHATGPT = "not_chatgpt"
LOGGED_OUT = "logged_out"
WRONG_ROUTE = "wrong_route"
CONVERSATION_NOT_FOUND = "conversation_not_found"
NOT_READY = "not_ready"
EMPTY = "empty"
PARTIAL = "partial"
RATE_LIMITED = "rate_limited"
UNSUPPORTED = "unsupported"
ERROR = "error"

CAPTURE_STATES = (
    OK, NOT_CHATGPT, LOGGED_OUT, WRONG_ROUTE, CONVERSATION_NOT_FOUND,
    NOT_READY, EMPTY, PARTIAL, RATE_LIMITED, UNSUPPORTED, ERROR,
)

CHATGPT_HOSTS = ("chatgpt.com", "chat.openai.com")
CHATGPT_HOME = "https://chatgpt.com/"

# The EVIDENCE schema constrains capture_path to a closed vocabulary
# ('api','native-export','dom-snapshot','hybrid'). This capture is obtained from the
# provider's first-party HTTP API, so 'api' is the correct existing value. The precise
# surface is recorded in provider_metadata['capture_surface'] rather than by widening the
# schema, which has no migration path. Widening it is a design decision for Don/Chiave,
# not a project-level build choice.
CAPTURE_PATH = "api"
CAPTURE_SURFACE = "first-party-json"

LIST_PATH = "/backend-api/conversations"
DETAIL_PATH = "/backend-api/conversations/"
LIST_LIMIT_MAX = 100          # verified 2026-09-26: limit=200 -> 422
DETAIL_NUM_TURNS_MAX = 100    # verified 2026-09-26: num_turns=200 -> 422 "le": 100
PAGE_GUARD = 200              # hard bound on the backward pagination walk
# Measured 2026-09-26 against the live account (gce-dbg probes):
#   * /backend-api/conversations (LIST) -> HTTP 429 under burst. Sustained rate is
#     ~one SUCCESSFUL request per ~60s (observed gaps 60.6s / 64.1s / 59.0s; recovery
#     from a 429 alone took ~22s). Paced accordingly, and a 429 that survives the
#     backoff is reported as `rate_limited` -- never as an empty account.
#   * The LIST `total` field is NOT a stable completeness oracle: offset=0 reported
#     total=101 while offset=100 reported total=201 in the same run. Termination
#     therefore keys on a short page, and total disagreement is recorded as a warning.
#   * /backend-api/conversations/<id> (DETAIL) -> no 429 observed across 5 back-to-back
#     captures; a small polite gap is still applied.
#   * /backend-api/pins -> separate limiter; unaffected by list throttling.
LIST_PAGE_GAP_S = 62.0
LIST_BACKOFF_S = (62.0, 95.0)
LIST_ATTEMPTS = 3
DETAIL_PAGE_GAP_MS = 400
HTTP_ATTEMPTS = 2             # in-page retry, kept small so an evaluate stays short
HTTP_BACKOFF_MS = 3000

# Enumeration segments. Exhaustive: these three partition the account.
SEGMENTS = (
    # (segment name, extra query params)
    ("active", "exclude_conversation_origin=tpp&is_archived=false"),
    ("archived", "exclude_conversation_origin=tpp&is_archived=true"),
    ("tpp", "conversation_origin=tpp&is_archived=false"),
)

CONV_ROUTE_RE = re.compile(r"^/(?:g/g-p-[^/]+/)?c/([^/?#]+)")


# -- pure helpers (unit-tested without a browser) ---------------------------
def classify_route(url: str) -> Dict[str, Any]:
    """Classify a page URL. Returns an explicit state, never a bare None.

    ChatGPT has two conversation route shapes:
      ``/c/<id>``                 (no project)
      ``/g/g-p-<projectId>/c/<id>`` (project-scoped)
    """
    if not url:
        return {"status": WRONG_ROUTE, "reason": "no_url", "url": url}
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in CHATGPT_HOSTS):
        return {"status": NOT_CHATGPT, "reason": "host_not_chatgpt", "host": host, "url": url}
    if parts.path in ("/", ""):
        return {"status": WRONG_ROUTE, "reason": "home_route", "pathname": parts.path}
    m = CONV_ROUTE_RE.match(parts.path)
    if not m:
        return {"status": WRONG_ROUTE, "reason": "not_conversation_route", "pathname": parts.path}
    proj = None
    gp = re.match(r"^/g/(g-p-[^/]+)/c/", parts.path)
    if gp:
        proj = gp.group(1)
    return {"status": OK, "conversation_id": m.group(1), "project_id": proj, "url": url}


def conversation_url(conv_id: str, project_id: Optional[str] = None) -> str:
    """Canonical navigation URL for a conversation id."""
    if project_id:
        return f"https://chatgpt.com/g/{project_id}/c/{conv_id}"
    return f"https://chatgpt.com/c/{conv_id}"


def epoch_to_iso(value: Any) -> Optional[str]:
    """Provider epoch seconds (float) -> ISO-8601 UTC. None stays None."""
    if value is None:
        return None
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


# -- in-page scripts ---------------------------------------------------------
# PURE_JS is the shared preamble: locate the page's own bootstrap state, build the
# auth headers, and classify. It never returns the token.
PRELUDE_JS = r"""
  function __boot() {
    var el = document.getElementById('client-bootstrap');
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  function __headers() {
    var b = __boot();
    if (!b || !b.session || !b.session.accessToken) return null;
    var h = { 'authorization': 'Bearer ' + b.session.accessToken, 'oai-language': 'en-US' };
    var acct = b.session.account || {};
    if (acct.id) h['chatgpt-account-id'] = acct.id;
    return h;
  }
  function __authStatus() {
    var b = __boot();
    return b ? (b.authStatus || 'unknown') : 'no_bootstrap';
  }
  function __host() { return location.hostname; }
  function __sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  // Bounded retry on 429/5xx. Never retries 4xx that will not change (401/403/404).
  // Returns {ok, status, json, error}. The token never leaves this function's scope.
  async function __getJson(url, H, attempts, backoffMs) {
    var last = null;
    for (var i = 0; i < attempts; i++) {
      var r = await fetch(url, { headers: H, credentials: 'include' });
      if (r.ok) {
        try { return { ok: true, status: r.status, json: await r.json() }; }
        catch (e) { return { ok: false, status: r.status, error: 'bad_json' }; }
      }
      last = { ok: false, status: r.status, error: 'http_' + r.status };
      if (r.status !== 429 && r.status < 500) return last;
      if (i + 1 < attempts) await __sleep(backoffMs * (i + 1));
    }
    return last || { ok: false, status: 0, error: 'no_attempt' };
  }
"""

# PURE_START / PURE_END bracket the JS that the node test harness executes.
AUTH_JS = r"""
(() => {
  // PURE_START
  __PRELUDE__
  // PURE_END
  var host = location.hostname;
  if (!/^(chatgpt\.com|chat\.openai\.com)$/.test(host)) {
    return { status: 'not_chatgpt', host: host, href: location.href };
  }
  var auth = __authStatus();
  if (auth !== 'logged_in') return { status: 'logged_out', authStatus: auth, href: location.href };
  if (!__headers()) return { status: 'not_ready', reason: 'no_session_credentials', href: location.href };
  return { status: 'ok', authStatus: auth, href: location.href };
})();
"""

LIST_PAGE_JS = r"""
(() => {
  // PURE_START
  __PRELUDE__
  // PURE_END
  // One HTTP request per evaluate. Pagination and 429 backoff live in Python so no
  // single evaluate can exceed the daemon's evaluate window.
  return (async () => {
    var host = location.hostname;
    if (!/^(chatgpt\.com|chat\.openai\.com)$/.test(host)) {
      return { status: 'not_chatgpt', host: host, href: location.href };
    }
    var auth = __authStatus();
    if (auth !== 'logged_in') return { status: 'logged_out', authStatus: auth, href: location.href };
    var H = __headers();
    if (!H) return { status: 'not_ready', reason: 'no_session_credentials', href: location.href };
    var res = await __getJson(__URL__, H, __ATTEMPTS__, __BACKOFF__);
    if (!res.ok) {
      return { status: res.status === 429 ? 'rate_limited' : 'error',
               status_detail: res.status, error: res.error, href: location.href };
    }
    return { status: 'ok', total: res.json.total, limit: res.json.limit,
             offset: res.json.offset, items: (res.json.items || []), href: location.href };
  })();
})();
"""

PINS_JS = r"""
(() => {
  // PURE_START
  __PRELUDE__
  // PURE_END
  return (async () => {
    var host = location.hostname;
    if (!/^(chatgpt\.com|chat\.openai\.com)$/.test(host)) {
      return { status: 'not_chatgpt', host: host, href: location.href };
    }
    var auth = __authStatus();
    if (auth !== 'logged_in') return { status: 'logged_out', authStatus: auth, href: location.href };
    var H = __headers();
    if (!H) return { status: 'not_ready', reason: 'no_session_credentials', href: location.href };
    var res = await __getJson('/backend-api/pins?item_type=conversation', H, __ATTEMPTS__, __BACKOFF__);
    if (!res.ok) {
      return { status: res.status === 429 ? 'rate_limited' : 'error',
               status_detail: res.status, error: res.error, items: [], href: location.href };
    }
    var out = [];
    for (var i = 0; i < res.json.length; i++) {
      var it = (res.json[i] || {}).item || {};
      if (it.id) out.push(it);
    }
    return { status: 'ok', items: out, href: location.href };
  })();
})();
"""

# Waits until the page's OWN conversation list has rendered, so we do not race the
# boot traffic (racing it is what provokes 429). Bounded, never infinite.
LIST_READY_JS = r"""
(() => {
  var n = document.querySelectorAll('nav a[href*="/c/"], aside a[href*="/c/"]').length;
  return { count: n, ready: n > 0, readyState: document.readyState, href: location.href };
})()
"""

CAPTURE_PAGE_JS = r"""
(() => {
  // PURE_START
  __PRELUDE__
  // PURE_END
  // One HTTP request per evaluate. The backward pagination walk and its retries live in
  // Python, for the same reason as the list endpoint: a long walk inside one evaluate
  // overruns the daemon's evaluate window (observed: a 30s timeout on a large
  // conversation, 2026-09-26) and leaves nowhere to retry a 429.
  var out = { warnings: [] };
  var href = location.href;
  out.href = href;
  var host = location.hostname;
  out.host = host;
  if (!/^(chatgpt\.com|chat\.openai\.com)$/.test(host)) { out.status = 'not_chatgpt'; return out; }
  var m = /^\/(?:g\/(g-p-[^/]+)\/)?c\/([^/?#]+)/.exec(location.pathname);
  if (!m) { out.status = 'wrong_route'; out.pathname = location.pathname; return out; }
  out.project_id = m[1] || null;
  out.conversation_id = m[2];

  var auth = __authStatus();
  out.authStatus = auth;
  if (auth !== 'logged_in') { out.status = 'logged_out'; return out; }
  var H = __headers();
  if (!H) { out.status = 'not_ready'; out.reason = 'no_session_credentials'; return out; }

  var url = __DETAIL_PATH__ + encodeURIComponent(out.conversation_id) + __QUERY__;
  return (async () => {
    var res = await __getJson(url, H, __ATTEMPTS__, __BACKOFF__);
    if (!res.ok) {
      if (res.status === 404) { out.status = 'conversation_not_found'; return out; }
      out.status = res.status === 429 ? 'rate_limited' : 'error';
      out.http_status = res.status;
      out.error_detail = res.error;
      return out;
    }
    var j = res.json;
    var pi = j.page_info || {};
    out.status = 'ok';
    out.conversation = j;
    out.messages = j.messages || [];
    out.page_info = {
      has_previous_page: !!pi.has_previous_page,
      has_next_page: !!pi.has_next_page,
      start_cursor: pi.start_cursor || null,
      end_cursor: pi.end_cursor || null
    };
    return out;
  })();
})();
"""
def _render(template: str, **kw: str) -> str:
    for key, val in kw.items():
        template = template.replace("__%s__" % key.upper(), val)
    return template


def _prelude() -> str:
    return PRELUDE_JS


# -- metadata derivation (pure) ---------------------------------------------
_NON_TEXT_PARTS = {"image_asset_pointer", "execution_output", "tether_browsing_display",
                   "audio_transcription", "asset_pointer"}


def derive_metadata(result: Dict[str, Any], discovery: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Indexable per-conversation metadata for one capture.

    Pure: takes the JS result, returns a JSON-serialisable dict. No interpretation of
    message *content* -- only counts, types, and provenance.
    """
    convo = result.get("conversation") or {}
    stats = result.get("stats") or {}
    warnings = list(result.get("warnings") or [])
    for t in _NON_TEXT_PARTS:
        if (stats.get("part_types") or {}).get(t):
            warnings.append(f"non_text_part:{t}")
    meta: Dict[str, Any] = {
        "capture_status": result.get("status"),
        "capture_surface": CAPTURE_SURFACE,
        "conversation_id": result.get("conversation_id") or convo.get("conversation_id"),
        "title": convo.get("title") or "Untitled",
        "canonical_url": result.get("href"),
        "project_id": result.get("project_id") or convo.get("gizmo_id"),
        "create_time": epoch_to_iso(convo.get("create_time")),
        "update_time": epoch_to_iso(convo.get("update_time")),
        "default_model_slug": convo.get("default_model_slug"),
        "is_archived": convo.get("is_archived"),
        "is_temporary_chat": convo.get("is_temporary_chat"),
        "message_count": stats.get("message_count", 0),
        "role_counts": stats.get("role_counts", {}),
        "content_types": stats.get("content_types", []),
        "part_types": stats.get("part_types", {}),
        "model_slugs": stats.get("model_slugs", []),
        "pages_walked": stats.get("pages_walked", 0),
        "context_truncation_continuation": bool(convo.get("context_truncation_continuation")),
        "warnings": sorted(set(warnings)),
    }
    if discovery:
        meta["discovered_title"] = discovery.get("title")
        meta["discovered_project_id"] = discovery.get("project_id")
        meta["discovered_update_time"] = discovery.get("update_time")
        meta["segments"] = discovery.get("segments")
        meta["pinned_time"] = discovery.get("pinned_time")
        # The navigated URL is the authority for project scope, but enumeration also
        # knows it; fall back so a project chat is never recorded as project-less.
        if not meta.get("project_id") and discovery.get("project_id"):
            meta["project_id"] = discovery["project_id"]
    return meta


def classify_capture(raw: Any) -> Dict[str, Any]:
    """Turn whatever the page returned into an explicit capture state.

    A bare ``None``/non-dict becomes ``not_ready`` with a reason -- never a silent miss.
    """
    if raw is None:
        return {"status": NOT_READY, "reason": "evaluate_returned_null"}
    if not isinstance(raw, dict):
        return {"status": UNSUPPORTED, "reason": "unexpected_payload_type",
                "payload_type": type(raw).__name__}
    status = raw.get("status")
    if status in CAPTURE_STATES:
        return raw
    return {"status": UNSUPPORTED, "reason": "unknown_status", "raw_status": status}


def _project_of(item: Dict[str, Any]) -> Optional[str]:
    return item.get("gizmo_id") or item.get("conversation_template_id") or None


def merge_enumeration(segment_results: List[Dict[str, Any]],
                      pin_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge per-segment list pages into one conversation set.

    Pure, so it is unit-tested without a browser. Identity is the provider ``id``;
    titles are carried but never used to merge -- duplicate titles are expected and
    harmless. A segment that did not complete is reported, never hidden.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    totals: Dict[str, Any] = {}
    errors: List[Dict[str, Any]] = []
    complete = 0

    for res in segment_results:
        name = res.get("segment") or "?"
        st = res.get("status")
        totals[name] = {"status": st, "total": res.get("total"),
                        "collected": res.get("collected", len(res.get("items") or []))}
        if st == OK:
            complete += 1
        else:
            errors.append({"segment": name, "status": st,
                           "detail": res.get("status_detail") or res.get("error"),
                           "errors": res.get("errors") or []})
        for it in res.get("items") or []:
            cid = it.get("id")
            if not cid:
                continue
            if cid in merged:
                if name not in merged[cid]["segments"]:
                    merged[cid]["segments"].append(name)
                continue
            merged[cid] = {
                "id": cid,
                "title": it.get("title") or "Untitled",
                "update_time": it.get("update_time"),
                "create_time": it.get("create_time"),
                "project_id": _project_of(it),
                "is_archived": bool(it.get("is_archived")),
                "is_starred": it.get("is_starred") is True,
                "pinned_time": it.get("pinned_time"),
                "memory_scope": it.get("memory_scope"),
                "segments": [name],
            }
            order.append(cid)

    pins_status = None
    if pin_result is not None:
        pins_status = pin_result.get("status")
        if pins_status == OK:
            for it in pin_result.get("items") or []:
                cid = it.get("id")
                if not cid:
                    continue
                if cid in merged:
                    merged[cid]["is_starred"] = True
                    if "pinned" not in merged[cid]["segments"]:
                        merged[cid]["segments"].append("pinned")
                    continue
                merged[cid] = {
                    "id": cid,
                    "title": it.get("title") or "Untitled",
                    "update_time": it.get("update_time"),
                    "create_time": it.get("create_time"),
                    "project_id": _project_of(it),
                    "is_archived": bool(it.get("is_archived")),
                    "is_starred": True,
                    "pinned_time": it.get("pinned_time"),
                    "memory_scope": it.get("memory_scope"),
                    "segments": ["pinned"],
                }
                order.append(cid)
        else:
            errors.append({"segment": "pins", "status": pins_status,
                           "detail": pin_result.get("error")})

    if complete == 0 and errors:
        rate_limited = all(e.get("status") == RATE_LIMITED for e in errors)
        status = RATE_LIMITED if rate_limited else ERROR
    elif complete < len(segment_results):
        status = PARTIAL
    else:
        status = OK

    return {
        "status": status,
        "items": [merged[i] for i in order],
        "conversation_ids": order,
        "totals": totals,
        "pins_status": pins_status,
        "errors": errors,
        "segments_complete": complete,
        "segments_total": len(segment_results),
    }


# -- adapter ----------------------------------------------------------------
@dataclass
class ChatGPTAdapter(AdapterInterface):
    """Implements the Wave-1 adapter contract against the live ChatGPT web app."""

    transport: TransportClient
    version: str = "w1-chatgpt-0.1.0"
    session_group: str = "ChatGPT backup"
    num_turns: int = DETAIL_NUM_TURNS_MAX
    page_guard: int = PAGE_GUARD
    list_gap_s: float = LIST_PAGE_GAP_S
    list_backoff_s: tuple = LIST_BACKOFF_S
    list_attempts: int = LIST_ATTEMPTS
    page_gap_s: float = DETAIL_PAGE_GAP_MS / 1000.0
    backoff_base_s: float = HTTP_BACKOFF_MS / 1000.0
    discovered: Dict[str, dict] = field(default_factory=dict)
    _last_token: Optional[str] = None
    # Enumeration completeness, carried from the last enumerate() call.
    enumeration_status: str = "unknown"
    enumeration_errors: List[dict] = field(default_factory=list)
    enumeration_totals: Dict[str, Any] = field(default_factory=dict)

    provider = "chatgpt"

    # -- probe: fail-closed -------------------------------------------------
    def probe(self) -> ProbeResult:
        """Transport up + extension attached + page on ChatGPT + ``authStatus`` logged_in.

        Cheap by design: it does NOT page the conversation list, so a probe can never
        trip rate limiting. Fail-closed (F2): any of those missing => structure_ok=False
        and the caller must STOP rather than walk a logged-out or foreign page.
        """
        tprobe = self.transport.probe()
        if not tprobe.structure_ok:
            return ProbeResult(structure_ok=False, signal="F2_STOP",
                               detail=f"transport: {tprobe.signal} {tprobe.detail or ''}".strip())
        try:
            self.transport.ensure_target(CHATGPT_HOME, group_title=self.session_group)
            raw = self.transport.evaluate_value(self._auth_script())
        except TransportError as exc:
            return ProbeResult(structure_ok=False, signal="F2_STOP",
                               detail=f"evaluate failed: {exc}")
        state = classify_capture(raw)
        if state.get("status") != OK:
            return ProbeResult(structure_ok=False, signal="F2_STOP",
                               detail=f"chatgpt page state: {state.get('status')} "
                                      f"({state.get('reason') or state.get('authStatus') or ''})")
        return ProbeResult(structure_ok=True, signal="OK",
                           detail=f"chatgpt adapter {self.version} confirmed; authStatus=logged_in")

    # -- enumerate ----------------------------------------------------------
    def enumerate(self, account_ref: str, options: Optional[dict] = None) -> EnumerateResult:
        """Bounded, one-request-per-evaluate pagination with Python-side 429 backoff.

        Split deliberately: pagination and backoff in the page would exceed the daemon's
        evaluate window, and keeping the merge in Python makes the dedupe-by-id rule
        unit-testable without a browser.
        """
        options = options or {}
        probe = self.probe()
        if not probe.structure_ok:
            raise RuntimeError(f"enumerate blocked: probe() did not confirm structure (F2): {probe.detail}")

        # Do not race the page's own boot traffic -- that is what provokes 429.
        self._await_list_ready()

        seg_results: List[Dict[str, Any]] = []
        for segment in SEGMENTS:
            seg_results.append(self._walk_segment(segment))

        pin_result = classify_capture(self.transport.evaluate_value(self._pins_script()))
        merged = merge_enumeration(seg_results, pin_result)

        status = merged["status"]
        if status not in (OK, PARTIAL):
            raise RuntimeError(
                f"enumerate failed: {status} errors={json.dumps(merged['errors'])[:500]}"
            )
        # `partial` is carried, never hidden: the run summary must show it.
        self.enumeration_status = status
        self.enumeration_errors = merged["errors"]
        self.enumeration_totals = merged["totals"]
        items = merged["items"]
        self.discovered = {it["id"]: it for it in items}
        ids = list(merged["conversation_ids"])
        if options.get("limit"):
            ids = ids[: int(options["limit"])]
        return EnumerateResult(provider=self.provider, conversation_ids=ids,
                               structure_ok=True, items=[self.discovered[i] for i in ids])

    # -- enumeration internals ---------------------------------------------
    def _await_list_ready(self, timeout_s: int = 20, poll_s: float = 1.0) -> bool:
        """Bounded wait for the page's own sidebar list to render."""
        deadline = time.time() + timeout_s
        while True:
            try:
                st = self.transport.evaluate_value(LIST_READY_JS)
            except TransportError:
                st = None
            if isinstance(st, dict) and st.get("ready"):
                return True
            if time.time() >= deadline:
                return bool(isinstance(st, dict) and st.get("ready"))
            time.sleep(poll_s)

    def _walk_segment(self, segment) -> Dict[str, Any]:
        """Offset-paginate one enumeration segment. Returns a segment result dict.

        Termination keys on a SHORT PAGE, not on the provider's ``total``: ``total`` was
        observed disagreeing between pages of the same walk. Disagreement is kept as
        evidence rather than trusted as an oracle.

        Pages are an outer loop and retries an inner loop, so a multi-page segment does
        not spend its retry budget on its own earlier pages.
        """
        name, extra = segment[0], segment[1]
        items: List[Dict[str, Any]] = []
        seen_ids = set()
        offset = 0
        totals_seen: List[Any] = []
        errors: List[Dict[str, Any]] = []
        pages = 0
        while True:
            page_ok = False
            page_len = 0
            for attempt in range(self.list_attempts):
                url = (f"{LIST_PATH}?{extra}&expand=false&hide_snorlax=false"
                       f"&is_starred=false&limit={LIST_LIMIT_MAX}&order=updated&offset={offset}")
                raw = classify_capture(self.transport.evaluate_value(self._page_script(url)))
                st = raw.get("status")
                if st == OK:
                    page = [i for i in (raw.get("items") or []) if i and i.get("id")]
                    pages += 1
                    page_len = len(page)
                    if raw.get("total") is not None:
                        totals_seen.append(raw.get("total"))
                    for i in page:
                        if i["id"] in seen_ids:
                            continue
                        seen_ids.add(i["id"])
                        items.append(i)
                    offset += page_len
                    page_ok = True
                    break
                if st in (LOGGED_OUT, NOT_CHATGPT, NOT_READY):
                    return {"status": st, "segment": name,
                            "total": totals_seen[-1] if totals_seen else None,
                            "collected": offset, "items": items, "pages": pages,
                            "errors": [{"reason": raw.get("reason") or raw.get("authStatus")}]}
                errors.append({"offset": offset, "status": raw.get("status_detail"),
                               "error": raw.get("error")})
                if attempt + 1 < self.list_attempts:
                    idx = min(attempt, len(self.list_backoff_s) - 1)
                    time.sleep(self.list_backoff_s[idx])
            if not page_ok:
                break
            if page_len < LIST_LIMIT_MAX:
                return self._segment_done(name, totals_seen, offset, items, pages, errors)
            # LIST is throttled to ~1 success/60s; pace rather than collect 429s.
            time.sleep(self.list_gap_s)
        return {"status": RATE_LIMITED if errors and all(
                    e.get("status") == 429 for e in errors) else ERROR,
                "segment": name, "total": totals_seen[-1] if totals_seen else None,
                "collected": offset, "items": items, "pages": pages, "errors": errors}

    @staticmethod
    def _segment_done(name, totals_seen, offset, items, pages, errors) -> Dict[str, Any]:
        distinct = sorted({t for t in totals_seen if t is not None})
        if len(distinct) > 1:
            # Not fatal -- the walk already terminated on a short page -- but recorded,
            # because it means the account was changing under the enumeration.
            errors = list(errors) + [{"warning": "total_disagreement_across_pages",
                                      "totals": distinct, "collected": offset}]
        return {"status": OK, "segment": name,
                "total": distinct[-1] if distinct else None,
                "totals_seen": distinct, "collected": offset,
                "items": items, "pages": pages, "errors": errors}

    # -- capture internals ---------------------------------------------------
    def _capture_page_script(self, query: str) -> str:
        return _render(CAPTURE_PAGE_JS, prelude=_prelude(), query=json.dumps(query),
                       detail_path=json.dumps(DETAIL_PATH),
                       attempts=str(HTTP_ATTEMPTS), backoff=str(HTTP_BACKOFF_MS))

    def _walk_conversation(self, conv_id: str) -> Dict[str, Any]:
        """Backward-paginate one conversation, one HTTP request per evaluate.

        Returns the merged result dict that :meth:`traverse_and_capture` classifies.
        Terminal states are explicit; a stalled cursor is ``partial``, never silent.
        """
        by_id: Dict[str, Any] = {}
        pages: List[Dict[str, Any]] = []
        warnings: List[str] = []
        cursor: Optional[str] = None
        meta: Optional[Dict[str, Any]] = None
        href = project_id = None
        truncated = False
        guard = 0
        last_error: Optional[Dict[str, Any]] = None

        while guard < self.page_guard:
            guard += 1
            query = (f"?num_turns={self.num_turns}"
                     + (f"&before={cursor}" if cursor else ""))
            raw = classify_capture(self.transport.evaluate_value(
                self._capture_page_script(query)))
            st = raw.get("status")
            if st == OK:
                href = raw.get("href", href)
                project_id = raw.get("project_id", project_id)
                meta = raw.get("conversation") or meta
                msgs = raw.get("messages") or []
                for m in msgs:
                    if m and m.get("id"):
                        by_id[m["id"]] = m
                pi = raw.get("page_info") or {}
                pages.append({"got": len(msgs),
                              "has_previous_page": bool(pi.get("has_previous_page")),
                              "start_cursor": pi.get("start_cursor")})
                if not pi.get("has_previous_page"):
                    break
                nxt = pi.get("start_cursor")
                if not nxt or nxt == cursor:
                    truncated = True
                    warnings.append("cursor_not_advancing")
                    break
                cursor = nxt
                time.sleep(self.page_gap_s)
                continue

            if st in (NOT_CHATGPT, WRONG_ROUTE, LOGGED_OUT, NOT_READY,
                       CONVERSATION_NOT_FOUND):
                # Terminal page state: waiting cannot change it.
                raw.setdefault("messages", [])
                raw["stats"] = dict(self._stats(list(by_id.values())),
                                    pages_walked=len(pages))
                raw["pages"] = pages
                raw["warnings"] = warnings + list(raw.get("warnings") or [])
                return raw
            # rate_limited / error: bounded retry on the same cursor.
            last_error = {"status": st, "http_status": raw.get("status_detail"),
                          "error": raw.get("error_detail") or raw.get("error")}
            if guard >= self.page_guard:
                break
            time.sleep(self.backoff_base_s * min(guard, 3))
        else:
            truncated = True
            warnings.append("page_guard_reached")

        if not meta and not by_id and last_error is not None:
            out = {"status": last_error["status"], "warnings": warnings,
                   "href": href, "conversation_id": conv_id}
            if last_error.get("http_status"):
                out["http_status"] = last_error["http_status"]
            out["stats"] = dict(self._stats([]), pages_walked=len(pages))
            out["pages"] = pages
            return out

        messages = sorted(by_id.values(),
                          key=lambda m: (m.get("create_time") or 0))
        status = EMPTY if not messages else (PARTIAL if truncated else OK)
        # The id the PAGE reported, kept separate from the id we asked for, so
        # traverse_and_capture can still detect a mismatch.
        page_conv_id = (meta or {}).get("conversation_id")
        return {
            "status": status,
            "href": href,
            "conversation_id": conv_id,
            "page_conversation_id": page_conv_id,
            "project_id": project_id,
            "conversation": meta,
            "messages": messages,
            "stats": dict(self._stats(messages), pages_walked=len(pages)),
            "pages": pages,
            "warnings": warnings,
        }

    @staticmethod
    def _stats(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Counts and type inventories. Pure; no message content is interpreted."""
        roles: Dict[str, int] = {}
        part_types: Dict[str, int] = {}
        content_types: Dict[str, bool] = {}
        models: Dict[str, bool] = {}
        for msg in messages:
            role = (msg.get("author") or {}).get("role") or "unknown"
            roles[role] = roles.get(role, 0) + 1
            slug = (msg.get("metadata") or {}).get("model_slug")
            if slug:
                models[slug] = True
            content = msg.get("content") or {}
            if content.get("content_type"):
                content_types[content["content_type"]] = True
            for part in content.get("parts") or []:
                t = part.get("content_type") or "object" if isinstance(part, dict) else type(part).__name__
                part_types[t] = part_types.get(t, 0) + 1
        return {
            "message_count": len(messages),
            "role_counts": roles,
            "part_types": part_types,
            "content_types": sorted(content_types),
            "model_slugs": sorted(models),
        }

    # -- traverse + capture -------------------------------------------------
    def traverse_and_capture(self, provider_conv_id: str,
                             options: Optional[dict] = None) -> CaptureResult:
        options = options or {}
        disc = self.discovered.get(provider_conv_id)
        proj = (disc or {}).get("project_id")
        url = conversation_url(provider_conv_id, proj)
        try:
            self.transport.ensure_target(url, group_title=self.session_group)
            state = self._walk_conversation(provider_conv_id)
        except TransportError as exc:
            return CaptureResult(provider=self.provider,
                                 provider_conversation_id=provider_conv_id,
                                 payload=b"", capture_path=CAPTURE_PATH,
                                 structure_ok=False, incomplete=True,
                                 failure_code=FailureCode.F2.value,
                                 provider_metadata={"capture_status": ERROR,
                                                    "capture_surface": CAPTURE_SURFACE,
                                                    "capture_reason": str(exc),
                                                    "canonical_url": url})

        status = state.get("status")
        # Guard against a page that is on a *different* conversation than requested.
        got_id = state.get("page_conversation_id")
        if status in (OK, PARTIAL) and got_id and got_id != provider_conv_id:
            status, state = WRONG_ROUTE, {"status": WRONG_ROUTE,
                                          "reason": "page_conversation_mismatch",
                                          "expected": provider_conv_id, "actual": got_id}

        if status != OK:
            failure = {
                EMPTY: FailureCode.F8.value,
                PARTIAL: FailureCode.F3.value,
                RATE_LIMITED: FailureCode.F3.value,
                CONVERSATION_NOT_FOUND: FailureCode.F8.value,
                NOT_READY: FailureCode.F2.value,
                LOGGED_OUT: FailureCode.F2.value,
                NOT_CHATGPT: FailureCode.F2.value,
                WRONG_ROUTE: FailureCode.F2.value,
                UNSUPPORTED: FailureCode.F13.value,
                ERROR: FailureCode.F2.value,
            }.get(status, FailureCode.F6.value)
            meta = {"capture_status": status, "capture_surface": CAPTURE_SURFACE,
                    "capture_reason": state.get("reason") or state.get("error_detail"),
                    "canonical_url": url, "warnings": state.get("warnings") or []}
            if state.get("http_status"):
                meta["http_status"] = state["http_status"]
            return CaptureResult(provider=self.provider,
                                 provider_conversation_id=provider_conv_id,
                                 payload=b"", capture_path=CAPTURE_PATH,
                                 structure_ok=status == OK, incomplete=status != OK,
                                 failure_code=failure, provider_metadata=meta)

        payload = self.serialize(state)
        meta = derive_metadata(state, disc)
        return CaptureResult(
            provider=self.provider, provider_conversation_id=provider_conv_id,
            payload=payload, capture_path=CAPTURE_PATH,
            title_bytes=(meta.get("title") or "").encode("utf-8"),
            structure_ok=True, incomplete=status == PARTIAL,
            failure_code=None, provider_metadata=meta,
        )

    # -- payload / resume ---------------------------------------------------
    @staticmethod
    def serialize(state: Dict[str, Any]) -> bytes:
        """Byte-preserving serialisation of the provider payload.

        The provider's own conversation object is stored verbatim under
        ``provider_payload``; the walk bookkeeping sits beside it. Nothing in the
        conversation is normalised, reordered, or dropped.
        """
        doc = {
            "schema": "gce.chatgpt.capture.v1",
            "capture_status": state.get("status"),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "canonical_url": state.get("href"),
            "conversation_id": state.get("conversation_id"),
            "project_id": state.get("project_id"),
            "stats": state.get("stats"),
            "pages": state.get("pages"),
            "warnings": state.get("warnings") or [],
            "provider_payload": state.get("conversation"),
            "messages": state.get("messages"),
        }
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def checkpoint(self) -> str:
        return self._last_token or ""

    def resume(self, token: str) -> None:
        self._last_token = token or None

    # -- script rendering ---------------------------------------------------
    def _auth_script(self) -> str:
        return _render(AUTH_JS, prelude=_prelude())

    def _page_script(self, url: str) -> str:
        return _render(LIST_PAGE_JS, prelude=_prelude(), url=json.dumps(url),
                       attempts=str(1), backoff=str(HTTP_BACKOFF_MS))

    def _pins_script(self) -> str:
        return _render(PINS_JS, prelude=_prelude(),
                       attempts=str(HTTP_ATTEMPTS), backoff=str(HTTP_BACKOFF_MS))



