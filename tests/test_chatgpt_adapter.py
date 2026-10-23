"""ChatGPT adapter contract (Stage 1 recon 2026-09-26).

Covers, without a browser:
  * URL / route classification for both ChatGPT conversation route shapes
  * conversation-id extraction, and that titles are never identity
  * explicit capture-state classification (no bare ``null``)
  * pagination walk: short-page termination, per-request retry budget, 429 handling
  * enumeration merge: dedupe by id, partial/rate_limited propagation
  * metadata derivation + warnings for non-text content classes
  * serialisation: provider payload preserved, round-trips
  * incremental-state decisions (new / changed / force)
  * node harness over the PURE blocks of the embedded scripts

The adapter here runs against a scripted transport. Live browser verification is a
separate, recorded step -- these tests do not and cannot stand in for it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from gce_ingestion.adapters.chatgpt import (
    CAPTURE_STATES,
    CONVERSATION_NOT_FOUND,
    EMPTY,
    ERROR,
    LIST_LIMIT_MAX,
    LOGGED_OUT,
    NOT_CHATGPT,
    NOT_READY,
    OK,
    PARTIAL,
    RATE_LIMITED,
    SEGMENTS,
    UNSUPPORTED,
    WRONG_ROUTE,
    ChatGPTAdapter,
    _render,
    classify_capture,
    classify_route,
    conversation_url,
    derive_metadata,
    epoch_to_iso,
    merge_enumeration,
)
from gce_ingestion.failures import FailureCode
from gce_ingestion.transport.base import (
    EnvelopeResult,
    ProbeResult,
    TransportClient,
    TransportError,
)

# -- scripted transport ------------------------------------------------------
class ScriptedTransport(TransportClient):
    """Returns canned page results keyed by call order. No browser, no network."""

    def __init__(self, pages=None, pins=None, capture=None, auth=None, navigate_error=None,
                 capture_pages=None):
        self.pages = list(pages or [])
        self.pins = pins
        self.capture = capture
        self.capture_pages = list(capture_pages or [])
        self.auth = auth if auth is not None else {"status": OK, "authStatus": "logged_in"}
        self.navigate_error = navigate_error
        self.calls: list = []
        self.navigated: list = []
        self.closed = False

    def status(self):
        return "UP"

    def start(self):
        return None

    def probe(self):
        return ProbeResult(structure_ok=True, signal="OK", detail="scripted")

    def ensure_target(self, url, group_title=None):
        if self.navigate_error:
            raise TransportError(self.navigate_error)
        self.navigated.append(url)
        return {"reused": False, "url": url}

    def navigate(self, url, new_tab=True, group_title=None):
        return self.ensure_target(url, group_title)

    def evaluate_value(self, script, timeout_s=None):
        self.calls.append(script)
        if "pins?item_type=conversation" in script:
            return self.pins
        if "backend-api/conversations/" in script:
            # capture page fetch -- one HTTP request per evaluate
            return self.capture_pages.pop(0) if self.capture_pages else \
                {"status": ERROR, "error": "capture_pages_exhausted"}
        if "backend-api/conversations?" in script:
            return self.pages.pop(0) if self.pages else {"status": ERROR, "error": "exhausted"}
        if "querySelectorAll('nav a[href" in script:
            return {"count": 3, "ready": True}
        if "authStatus" in script and "conversations" not in script:
            return self.auth
        return self.capture

    def evaluate(self, script, timeout_s=None):
        return EnvelopeResult(action="evaluate", payload=self.evaluate_value(script),
                              session="scripted", structure_ok=True)

    def close(self):
        self.closed = True


def ok_page(items, total=None):
    return {"status": OK, "items": items, "total": total if total is not None else len(items),
            "limit": LIST_LIMIT_MAX, "offset": 0}


def item(cid, title="T", **kw):
    d = {"id": cid, "title": title, "update_time": "2026-09-26T00:00:00Z",
         "gizmo_id": None}
    d.update(kw)
    return d


# -- route classification ----------------------------------------------------
@pytest.mark.parametrize("url,expect", [
    ("https://chatgpt.com/c/abc-123", OK),
    ("https://chatgpt.com/g/g-p-6ab43/c/abc-123", OK),
    ("https://chatgpt.com/", WRONG_ROUTE),
    ("https://chatgpt.com/g/g-p-6ab43", WRONG_ROUTE),
    ("https://chatgpt.com/share/abc", WRONG_ROUTE),
    ("https://www.kimi.com/chat/abc", NOT_CHATGPT),
    ("https://example.com/c/abc", NOT_CHATGPT),
    ("", WRONG_ROUTE),
])
def test_route_classification_is_explicit(url, expect):
    assert classify_route(url)["status"] == expect


def test_route_extracts_conversation_id_and_project():
    r = classify_route("https://chatgpt.com/g/g-p-6ab43fca/c/6ab70dbe-1d18")
    assert r["status"] == OK
    assert r["conversation_id"] == "6ab70dbe-1d18"
    assert r["project_id"] == "g-p-6ab43fca"


def test_route_without_project_has_none_project():
    r = classify_route("https://chatgpt.com/c/6ab70dbe-1d18")
    assert r["status"] == OK and r["project_id"] is None


def test_conversation_url_roundtrips_through_classify_route():
    for proj in (None, "g-p-6ab43fca"):
        url = conversation_url("cid-9", proj)
        assert classify_route(url)["conversation_id"] == "cid-9"
        assert classify_route(url)["project_id"] == proj


# -- capture-state classification -------------------------------------------
def test_capture_never_collapses_to_a_bare_null():
    got = classify_capture(None)
    assert got["status"] == NOT_READY
    assert got["reason"] == "evaluate_returned_null"


def test_capture_rejects_unexpected_payload_shape_explicitly():
    got = classify_capture("a string")
    assert got["status"] == UNSUPPORTED
    assert got["payload_type"] == "str"


def test_capture_rejects_unknown_status_rather_than_promoting_it():
    got = classify_capture({"status": "looks_fine"})
    assert got["status"] == UNSUPPORTED
    assert got["raw_status"] == "looks_fine"


def test_every_documented_state_is_recognised():
    for st in CAPTURE_STATES:
        assert classify_capture({"status": st})["status"] == st


def test_rate_limited_and_partial_are_distinct_states():
    assert RATE_LIMITED in CAPTURE_STATES and PARTIAL in CAPTURE_STATES
    assert RATE_LIMITED != PARTIAL


# -- enumeration merge -------------------------------------------------------
def test_merge_dedupes_by_id_not_by_title():
    seg = [{"status": OK, "segment": "active", "total": 2, "collected": 2,
            "items": [item("id-1", "Same Title"), item("id-2", "Same Title")]}]
    pins = {"status": OK, "items": [{"id": "id-2", "title": "Same Title"}]}
    m = merge_enumeration(seg, pins)
    assert m["status"] == OK
    assert m["conversation_ids"] == ["id-1", "id-2"]
    assert m["items"][1]["is_starred"] is True
    assert m["items"][0]["segments"] == ["active"]


def test_merge_unions_segments_for_a_repeated_id():
    seg = [
        {"status": OK, "segment": "active", "total": 1, "collected": 1, "items": [item("x")]},
        {"status": OK, "segment": "tpp", "total": 1, "collected": 1, "items": [item("x")]},
    ]
    m = merge_enumeration(seg, {"status": OK, "items": []})
    assert m["conversation_ids"] == ["x"]
    assert m["items"][0]["segments"] == ["active", "tpp"]


def test_merge_reports_partial_when_one_segment_fails():
    seg = [
        {"status": OK, "segment": "active", "total": 1, "collected": 1, "items": [item("a")]},
        {"status": RATE_LIMITED, "segment": "archived", "total": None, "collected": 0,
         "items": [], "errors": [{"status": 429}]},
    ]
    m = merge_enumeration(seg, {"status": OK, "items": []})
    assert m["status"] == PARTIAL
    assert m["segments_complete"] == 1 and m["segments_total"] == 2
    assert m["errors"][0]["segment"] == "archived"


def test_merge_reports_rate_limited_when_all_segments_throttled():
    seg = [{"status": RATE_LIMITED, "segment": s[0], "total": None, "collected": 0,
            "items": [], "errors": [{"status": 429}]} for s in SEGMENTS]
    m = merge_enumeration(seg, {"status": OK, "items": []})
    assert m["status"] == RATE_LIMITED


def test_merge_never_reports_ok_when_nothing_was_collected():
    seg = [{"status": ERROR, "segment": s[0], "total": None, "collected": 0,
            "items": [], "errors": [{"status": 500}]} for s in SEGMENTS]
    m = merge_enumeration(seg, {"status": OK, "items": []})
    assert m["status"] == ERROR


# -- pagination walk ---------------------------------------------------------
def _adapter(pages=None, pins=None, capture=None, auth=None, capture_pages=None, **kw):
    t = ScriptedTransport(pages=pages, pins=pins, capture=capture, auth=auth,
                          capture_pages=capture_pages)
    kw.setdefault("list_gap_s", 0)
    kw.setdefault("list_backoff_s", (0,))
    kw.setdefault("page_gap_s", 0)
    kw.setdefault("backoff_base_s", 0)
    kw.setdefault("page_guard", 5)
    a = ChatGPTAdapter(t, **kw)
    return a, t


def _throttled():
    return {"status": RATE_LIMITED, "status_detail": 429, "error": "http_429"}


def test_walk_terminates_on_a_short_page_not_on_total():
    full = ok_page([item(f"id-{i}") for i in range(LIST_LIMIT_MAX)], total=99999)
    short = ok_page([item("last")], total=99999)
    a, t = _adapter([full, short], pins={"status": OK, "items": []})
    res = a._walk_segment(SEGMENTS[0])
    assert res["status"] == OK
    assert res["pages"] == 2
    assert len(res["items"]) == LIST_LIMIT_MAX + 1


def test_walk_retry_budget_is_per_request_not_per_segment():
    a, t = _adapter([_throttled(), _throttled(), ok_page([item("a")])],
                    pins={"status": OK, "items": []})
    res = a._walk_segment(SEGMENTS[0])
    assert res["status"] == OK
    assert res["collected"] == 1
    assert len(res["errors"]) == 2  # the throttles are kept as evidence


def test_walk_gives_up_as_rate_limited_after_exhausting_retries():
    a, t = _adapter([_throttled() for _ in range(a_default_attempts())],
                    pins={"status": OK, "items": []})
    res = a._walk_segment(SEGMENTS[0])
    assert res["status"] == RATE_LIMITED
    assert len(res["errors"]) == a.list_attempts


def a_default_attempts():
    from gce_ingestion.adapters.chatgpt import LIST_ATTEMPTS
    return LIST_ATTEMPTS


def test_walk_records_total_disagreement_as_evidence():
    a, t = _adapter([ok_page([item("a")], total=101), ok_page([item("b")], total=201)],
                    pins={"status": OK, "items": []})
    # one item per page -> first page is already "short", so force a full first page
    full = ok_page([item(f"i{i}") for i in range(LIST_LIMIT_MAX)], total=101)
    short = ok_page([item("z")], total=201)
    t.pages = [full, short]
    res = a._walk_segment(SEGMENTS[0])
    assert res["status"] == OK
    assert any(e.get("warning") == "total_disagreement_across_pages" for e in res["errors"])


def test_walk_drops_duplicate_ids_across_pages():
    full = ok_page([item(f"i{i}") for i in range(LIST_LIMIT_MAX)], total=200)
    short = ok_page([item("i0"), item("new-1")], total=200)
    a, t = _adapter([full, short], pins={"status": OK, "items": []})
    res = a._walk_segment(SEGMENTS[0])
    ids = [i["id"] for i in res["items"]]
    assert len(ids) == len(set(ids))
    assert "new-1" in ids


def test_walk_aborts_immediately_on_logged_out():
    a, t = _adapter([{"status": LOGGED_OUT, "authStatus": "logged_out"}])
    res = a._walk_segment(SEGMENTS[0])
    assert res["status"] == LOGGED_OUT
    assert len(t.pages) == 0  # no retry burned


# -- capture results ---------------------------------------------------------
def _page(msgs, has_previous=False, start_cursor=None, **conv):
    """One page as the in-page script returns it."""
    convo_meta = {"conversation_id": "cid-1", "title": "T",
                  "create_time": 1790381503.25, "update_time": 1790406714.33,
                  "default_model_slug": "gpt-5-6-thinking", "is_archived": False,
                  "context_truncation_continuation": None}
    convo_meta.update(conv)
    return {
        "status": OK,
        "href": "https://chatgpt.com/c/cid-1",
        "conversation_id": "cid-1",
        "project_id": None,
        "conversation": convo_meta,
        "messages": msgs,
        "page_info": {"has_previous_page": has_previous, "has_next_page": False,
                      "start_cursor": start_cursor, "end_cursor": None},
        "warnings": [],
    }


def _m(mid, ct, role="user", part="x", slug="gpt-5-6-thinking"):
    return {"id": mid, "create_time": ct, "author": {"role": role},
            "content": {"content_type": "text", "parts": [part]},
            "metadata": {"model_slug": slug}}


def _discovered(cid, title="T", project_id=None, **kw):
    """The shape merge_enumeration() actually produces -- the only thing that ever
    populates adapter.discovered."""
    d = {"id": cid, "title": title, "update_time": "2026-09-26T00:00:00Z",
         "create_time": "2026-09-20T00:00:00Z", "project_id": project_id,
         "is_archived": False, "is_starred": False, "pinned_time": None,
         "memory_scope": "global_enabled", "segments": ["active"]}
    d.update(kw)
    return d


def _cap(pages, **kw):
    return _adapter([], capture_pages=pages, **kw)


def test_capture_ok_produces_payload_and_metadata():
    a, t = _cap([_page([_m("m1", 1), _m("m2", 2, "assistant")])])
    a.discovered = {"cid-1": _discovered("cid-1", "T", project_id="g-p-1")}
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok and cap.failure_code is None
    # 'api' is the schema's existing vocabulary value; the precise surface rides in
    # provider_metadata so the closed CHECK constraint is not widened unilaterally.
    assert cap.capture_path == "api"
    assert cap.provider_metadata["capture_surface"] == "first-party-json"
    assert cap.provider_metadata["capture_status"] == OK
    assert cap.provider_metadata["message_count"] == 2
    assert cap.provider_metadata["project_id"] == "g-p-1"
    assert cap.provider_metadata["pages_walked"] == 1
    doc = json.loads(cap.payload.decode("utf-8"))
    assert doc["schema"] == "gce.chatgpt.capture.v1"
    assert len(doc["messages"]) == 2


def test_capture_walks_backward_dedupes_by_id_and_orders_by_create_time():
    a, t = _cap([
        _page([_m("m2", 3), _m("m1", 2)], has_previous=True, start_cursor="cur-1"),
        _page([_m("m1", 2), _m("m0", 1)]),
    ])
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok
    assert cap.provider_metadata["pages_walked"] == 2
    doc = json.loads(cap.payload.decode("utf-8"))
    assert [m["id"] for m in doc["messages"]] == ["m0", "m1", "m2"]
    assert cap.provider_metadata["message_count"] == 3


def test_capture_issues_one_request_per_page():
    a, t = _cap([
        _page([_m("m1", 1)], has_previous=True, start_cursor="cur-1"),
        _page([_m("m0", 0)]),
    ])
    a.traverse_and_capture("cid-1")
    detail_calls = [s for s in t.calls if "backend-api/conversations/" in s]
    assert len(detail_calls) == 2
    assert "num_turns=100" in detail_calls[0]
    assert "before=cur-1" in detail_calls[1]


def test_capture_is_partial_when_the_cursor_stalls():
    a, t = _cap([_page([_m("m1", 1)], has_previous=True, start_cursor="same"),
                 _page([_m("m0", 0)], has_previous=True, start_cursor="same")])
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok is False
    assert cap.provider_metadata["capture_status"] == PARTIAL
    assert "cursor_not_advancing" in cap.provider_metadata["warnings"]


def test_capture_is_partial_when_the_page_guard_is_hit():
    a, t = _cap([_page([_m("m1", 1)], has_previous=True, start_cursor="c0"),
                 _page([_m("m0", 0)], has_previous=True, start_cursor="c1")],
                page_guard=2)
    cap = a.traverse_and_capture("cid-1")
    assert cap.provider_metadata["capture_status"] == PARTIAL
    assert "page_guard_reached" in cap.provider_metadata["warnings"]


def test_capture_retries_a_throttled_page_on_the_same_cursor():
    a, t = _cap([{"status": RATE_LIMITED, "status_detail": 429, "error": "http_429"},
                 _page([_m("m1", 1)])])
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok
    assert len([s for s in t.calls if "backend-api/conversations/" in s]) == 2


def test_capture_reports_rate_limited_when_retries_run_out():
    a, t = _cap([_throttled()] * 3, page_guard=3)
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok is False
    assert cap.provider_metadata["capture_status"] == RATE_LIMITED
    assert cap.failure_code == FailureCode.F3.value


def test_capture_preserves_the_provider_payload_verbatim():
    """The stored provider object is the page's own, unfiltered: fields the adapter
    knows nothing about survive, and nothing is dropped."""
    convo = {"conversation_id": "cid-1", "title": "T", "safe_urls": ["https://example.com"],
             "moderation_results": [], "some_future_field": {"a": 1}}
    a, t = _cap([_page([_m("m1", 1)], **convo)])
    doc = json.loads(a.traverse_and_capture("cid-1").payload.decode("utf-8"))
    kept = doc["provider_payload"]
    for k, v in convo.items():
        assert kept[k] == v, k
    # the page's other conversation-level fields are preserved too
    assert kept["create_time"] == 1790381503.25
    assert kept["default_model_slug"] == "gpt-5-6-thinking"


def test_capture_uses_the_project_scoped_url_when_discovered():
    a, t = _cap([_page([_m("m1", 1)])])
    a.discovered = {"cid-1": _discovered("cid-1", project_id="g-p-abc")}
    a.traverse_and_capture("cid-1")
    assert t.navigated[-1] == "https://chatgpt.com/g/g-p-abc/c/cid-1"


def test_capture_uses_the_plain_route_when_the_conversation_has_no_project():
    a, t = _cap([_page([_m("m1", 1)])])
    a.discovered = {"cid-1": _discovered("cid-1")}
    a.traverse_and_capture("cid-1")
    assert t.navigated[-1] == "https://chatgpt.com/c/cid-1"


def test_capture_flags_non_text_content_classes_instead_of_dropping_them():
    img = {"id": "m1", "create_time": 1, "author": {"role": "user"},
           "content": {"content_type": "tether_browsing_display",
                       "parts": [{"content_type": "image_asset_pointer"}]},
           "metadata": {}}
    a, t = _cap([_page([img])])
    cap = a.traverse_and_capture("cid-1")
    assert "non_text_part:image_asset_pointer" in cap.provider_metadata["warnings"]
    # the raw part is still present in the stored payload
    doc = json.loads(cap.payload.decode("utf-8"))
    assert doc["messages"][0]["content"]["parts"][0]["content_type"] == "image_asset_pointer"


def test_capture_counts_roles_content_types_and_models():
    a, t = _cap([_page([
        _m("m1", 1, "user"),
        _m("m2", 2, "assistant", slug="gpt-x"),
        {"id": "m3", "create_time": 3, "author": {"role": "tool"},
         "content": {"content_type": "execution_output", "parts": ["out"]},
         "metadata": {}},
    ])])
    m = a.traverse_and_capture("cid-1").provider_metadata
    assert m["role_counts"] == {"user": 1, "assistant": 1, "tool": 1}
    assert m["content_types"] == ["execution_output", "text"]
    assert m["model_slugs"] == ["gpt-5-6-thinking", "gpt-x"]


def test_capture_rejects_a_page_showing_a_different_conversation():
    a, t = _cap([_page([_m("m1", 1)], conversation_id="other")])
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok is False
    assert cap.provider_metadata["capture_status"] == WRONG_ROUTE
    assert cap.provider_metadata["capture_reason"] == "page_conversation_mismatch"


@pytest.mark.parametrize("status,fcode", [
    (CONVERSATION_NOT_FOUND, FailureCode.F8.value),
    (RATE_LIMITED, FailureCode.F3.value),
    (NOT_READY, FailureCode.F2.value),
    (LOGGED_OUT, FailureCode.F2.value),
    (NOT_CHATGPT, FailureCode.F2.value),
    (WRONG_ROUTE, FailureCode.F2.value),
    (UNSUPPORTED, FailureCode.F13.value),
    (ERROR, FailureCode.F2.value),
])
def test_each_failure_state_maps_to_a_defined_failure_code(status, fcode):
    # retryable states are supplied once per allowed page attempt so the walk's
    # final verdict is the state under test, not an exhaustion artefact.
    guard = 5
    a, _ = _cap([{"status": status, "conversation_id": "cid-1"}] * guard, page_guard=guard)
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok is False
    assert cap.failure_code == fcode
    assert cap.provider_metadata["capture_status"] == status


def test_capture_empty_conversation_is_empty_not_ok():
    a, _ = _cap([_page([])])
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok is False
    assert cap.failure_code == FailureCode.F8.value
    assert cap.provider_metadata["capture_status"] == EMPTY


def test_transport_error_is_reported_as_error_not_swallowed():
    t = ScriptedTransport(navigate_error="502 no session target")
    a = ChatGPTAdapter(t)
    cap = a.traverse_and_capture("cid-1")
    assert cap.structure_ok is False
    assert cap.provider_metadata["capture_status"] == ERROR
    assert "502" in cap.provider_metadata["capture_reason"]


def test_probe_fails_closed_when_the_transport_is_down():
    class Down(ScriptedTransport):
        def probe(self):
            return ProbeResult(structure_ok=False, signal="DAEMON_DOWN", detail="down")

    a = ChatGPTAdapter(Down())
    p = a.probe()
    assert p.structure_ok is False and p.signal == "F2_STOP"


def test_probe_fails_closed_when_logged_out():
    a, _ = _adapter([], auth={"status": LOGGED_OUT, "authStatus": "logged_out"})
    p = a.probe()
    assert p.structure_ok is False
    assert "logged_out" in (p.detail or "")


def test_enumerate_raises_rather_than_returning_a_partial_silently():
    a, t = _adapter([_throttled() for _ in range(len(SEGMENTS) * a_default_attempts())],
                    pins={"status": OK, "items": []})
    with pytest.raises(RuntimeError, match="rate_limited"):
        a.enumerate("acc")


# -- metadata + serialisation ------------------------------------------------
def test_derive_metadata_converts_epoch_to_iso_and_keeps_none():
    assert epoch_to_iso(1790381503.25) == "2026-09-26T00:11:43Z"
    assert epoch_to_iso(None) is None
    assert epoch_to_iso("nonsense") is None
    assert epoch_to_iso(0) is None


def test_derive_metadata_is_json_serialisable():
    state = a_walked_state()
    json.dumps(derive_metadata(state, _discovered("cid-1")))


def test_serialize_roundtrips_and_carries_the_schema_tag():
    state = a_walked_state()
    doc = json.loads(ChatGPTAdapter.serialize(state).decode("utf-8"))
    assert doc["schema"] == "gce.chatgpt.capture.v1"
    assert doc["capture_status"] == OK
    assert doc["conversation_id"] == "cid-1"
    assert len(doc["messages"]) == 1
    assert doc["stats"]["message_count"] == 1
    # non-ASCII survives the round trip byte-exactly
    state["messages"][0]["content"]["parts"] = ["café — naïve"]
    again = json.loads(ChatGPTAdapter.serialize(state).decode("utf-8"))
    assert again["messages"][0]["content"]["parts"] == ["café — naïve"]


def a_walked_state():
    """The merged shape _walk_conversation() produces."""
    return {
        "status": OK, "href": "https://chatgpt.com/c/cid-1", "conversation_id": "cid-1",
        "project_id": None,
        "conversation": {"conversation_id": "cid-1", "title": "T",
                         "create_time": 1790381503.25, "update_time": 1790406714.33,
                         "default_model_slug": "gpt-5-6-thinking", "is_archived": False,
                         "context_truncation_continuation": None},
        "messages": [{"id": "m1", "create_time": 1, "author": {"role": "user"},
                      "content": {"content_type": "text", "parts": ["hi"]},
                      "metadata": {"model_slug": "gpt-5-6-thinking"}}],
        "stats": {"message_count": 1, "role_counts": {"user": 1},
                  "part_types": {"string": 1}, "content_types": ["text"],
                  "model_slugs": ["gpt-5-6-thinking"]},
        "pages": [{"got": 1, "has_previous_page": False, "start_cursor": None}],
        "warnings": [],
    }


def test_checkpoint_and_resume_roundtrip():
    a, _ = _adapter([])
    assert a.checkpoint() == ""
    a.resume("cid-7")
    assert a.checkpoint() == "cid-7"


# -- incremental state -------------------------------------------------------
def test_incremental_decisions():
    from gce_ingestion.cli.chatgpt_backup import _needs_capture

    now = {"id": "c", "update_time": "2026-09-26T00:00:00Z"}
    assert _needs_capture(None, now, False) is True            # never captured
    assert _needs_capture({"provider_metadata": {"update_time": "2026-09-25T00:00:00Z"}},
                          now, False) is True                    # provider moved
    assert _needs_capture({"provider_metadata": {"update_time": "2026-09-26T00:00:00Z"}},
                          now, False) is False                   # unchanged
    assert _needs_capture({"provider_metadata": {"update_time": "2026-09-26T00:00:00Z"}},
                          now, True) is True                     # forced
    assert _needs_capture({"provider_metadata": {}}, now, False) is True  # unknown -> recapture


# -- node harness over the embedded scripts ---------------------------------
# The scripts ship as strings, so they are executed for real under node against a stub
# DOM and a scripted fetch. This catches syntax errors and broken wiring that a
# Python-only test cannot see. Skipped when node is unavailable.
#
# argv: [prelude.js, script.js, fixtures.json]
# fixtures.json: {authStatus, hostname, href, pathname, pages: [{match, status, body}]}
HARNESS_JS = r"""
const fs = require('fs');
const prelude = fs.readFileSync(process.argv[2], 'utf8');
const target  = fs.readFileSync(process.argv[3], 'utf8');
const fx      = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

const BOOT = JSON.stringify({
  authStatus: fx.authStatus || 'logged_in',
  session: { accessToken: 'test-token', account: { id: 'acct-1' } }
});

globalThis.location = {
  hostname: fx.hostname || 'chatgpt.com',
  href: fx.href || 'https://chatgpt.com/c/cid-1',
  pathname: fx.pathname || '/c/cid-1'
};
globalThis.document = {
  readyState: 'complete',
  getElementById: (id) => (id === 'client-bootstrap' ? { textContent: BOOT } : null),
  querySelectorAll: () => [],
  querySelector: () => null
};
const fetchCalls = [];
globalThis.fetch = async (url) => {
  fetchCalls.push(url);
  const page = (fx.pages || []).find((p) => url.indexOf(p.match) >= 0);
  if (!page) {
    return { ok: false, status: 404, headers: { get: () => null }, json: async () => ({}) };
  }
  if (page.status && page.status >= 400) {
    return { ok: false, status: page.status, headers: { get: () => null },
             json: async () => (page.body || {}) };
  }
  return { ok: true, status: 200, headers: { get: () => null },
           json: async () => (page.body || {}) };
};

(0, eval)(prelude);
const result = (0, eval)(target);
Promise.resolve(result).then((r) => {
  process.stdout.write(JSON.stringify({ result: r, fetches: fetchCalls }));
}).catch((e) => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""


def _run_node(tmp_path, script_js, fixtures):
    from gce_ingestion.adapters.chatgpt import PRELUDE_JS

    if not shutil.which("node"):
        pytest.skip("node not available for embedded-JS tests")
    pre, tgt = tmp_path / "prelude.js", tmp_path / "script.js"
    fx, h = tmp_path / "fixtures.json", tmp_path / "harness.js"
    pre.write_text(PRELUDE_JS, encoding="utf-8")
    tgt.write_text(script_js, encoding="utf-8")
    fx.write_text(json.dumps(fixtures), encoding="utf-8")
    h.write_text(HARNESS_JS, encoding="utf-8")
    proc = subprocess.run(["node", str(h), str(pre), str(tgt), str(fx)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _auth_js():
    from gce_ingestion.adapters.chatgpt import AUTH_JS, PRELUDE_JS
    return _render(AUTH_JS, prelude=PRELUDE_JS)


def _page_js():
    from gce_ingestion.adapters.chatgpt import LIST_PAGE_JS, PRELUDE_JS
    return _render(LIST_PAGE_JS, prelude=PRELUDE_JS, url=json.dumps(URL_UNDER_TEST),
                   attempts="1", backoff="10")


def _pins_js():
    from gce_ingestion.adapters.chatgpt import PINS_JS, PRELUDE_JS
    return _render(PINS_JS, prelude=PRELUDE_JS, attempts="1", backoff="10")


def _capture_js(query="?num_turns=100"):
    from gce_ingestion.adapters.chatgpt import CAPTURE_PAGE_JS, PRELUDE_JS
    return _render(CAPTURE_PAGE_JS, prelude=PRELUDE_JS, query=json.dumps(query),
                   detail_path=json.dumps("/backend-api/conversations/"),
                   attempts="1", backoff="10")


URL_UNDER_TEST = "https://chatgpt.com/backend-api/conversations?offset=100&limit=100"


def test_auth_script_runs_and_reports_logged_in(tmp_path):
    out = _run_node(tmp_path, _auth_js(), {})
    assert out["result"]["status"] == OK
    assert out["result"]["authStatus"] == "logged_in"


def test_auth_script_reports_logged_out(tmp_path):
    out = _run_node(tmp_path, _auth_js(), {"authStatus": "logged_out"})
    assert out["result"]["status"] == LOGGED_OUT


def test_auth_script_reports_a_foreign_host_as_not_chatgpt(tmp_path):
    out = _run_node(tmp_path, _auth_js(),
                    {"hostname": "www.kimi.com", "href": "https://www.kimi.com/chat/x",
                     "pathname": "/chat/x"})
    assert out["result"]["status"] == NOT_CHATGPT


def test_list_page_script_fetches_exactly_the_requested_url(tmp_path):
    out = _run_node(tmp_path, _page_js(),
                    {"pages": [{"match": "backend-api/conversations?",
                                "body": {"total": 1, "limit": 100, "offset": 100,
                                         "items": [{"id": "c-1", "title": "One"}]}}]})
    assert out["result"]["status"] == OK
    assert out["result"]["total"] == 1
    assert out["fetches"] == [URL_UNDER_TEST]


def test_list_page_script_maps_429_to_rate_limited(tmp_path):
    out = _run_node(tmp_path, _page_js(),
                    {"pages": [{"match": "backend-api/conversations?", "status": 429}]})
    assert out["result"]["status"] == RATE_LIMITED
    assert out["result"]["status_detail"] == 429


def test_pins_script_extracts_items(tmp_path):
    out = _run_node(tmp_path, _pins_js(),
                    {"pages": [{"match": "/backend-api/pins",
                                "body": [{"item": {"id": "pin-1", "title": "Pinned"}},
                                         {"nope": 1}]}]})
    assert out["result"]["status"] == OK
    assert out["result"]["items"] == [{"id": "pin-1", "title": "Pinned"}]


def test_capture_page_script_returns_one_page_and_its_cursor(tmp_path):
    out = _run_node(tmp_path, _capture_js("?num_turns=100"), {
        "pages": [{"match": "num_turns=100",
                   "body": {"title": "T", "conversation_id": "cid-1",
                            "create_time": 1, "update_time": 2,
                            "default_model_slug": "gpt-x",
                            "messages": [{"id": "m2", "create_time": 2,
                                          "author": {"role": "assistant"},
                                          "content": {"content_type": "text", "parts": ["b"]},
                                          "metadata": {"model_slug": "gpt-x"}},
                                         {"id": "m1", "create_time": 1,
                                          "author": {"role": "user"},
                                          "content": {"content_type": "text", "parts": ["a"]},
                                          "metadata": {}}],
                            "page_info": {"has_previous_page": True,
                                          "start_cursor": "cur-1"}}}]})
    res = out["result"]
    assert res["status"] == OK
    assert res["conversation_id"] == "cid-1"
    assert len(res["messages"]) == 2
    assert res["page_info"] == {"has_previous_page": True, "has_next_page": False,
                                "start_cursor": "cur-1", "end_cursor": None}
    # exactly one request, for exactly the query asked for
    assert out["fetches"] == ["/backend-api/conversations/cid-1?num_turns=100"]


def test_capture_page_script_sends_the_before_cursor_when_asked(tmp_path):
    out = _run_node(tmp_path, _capture_js("?num_turns=100&before=cur-1"), {
        "pages": [{"match": "before=cur-1", "body": {"messages": [], "page_info": {}}}]})
    assert out["fetches"] == ["/backend-api/conversations/cid-1?num_turns=100&before=cur-1"]


def test_capture_page_script_maps_404_to_conversation_not_found(tmp_path):
    out = _run_node(tmp_path, _capture_js(),
                    {"pages": [{"match": "num_turns=100", "status": 404}]})
    assert out["result"]["status"] == CONVERSATION_NOT_FOUND


def test_capture_page_script_maps_429_to_rate_limited(tmp_path):
    out = _run_node(tmp_path, _capture_js(),
                    {"pages": [{"match": "num_turns=100", "status": 429}]})
    assert out["result"]["status"] == RATE_LIMITED
    assert out["result"]["http_status"] == 429


def test_capture_page_script_reports_wrong_route_on_a_non_conversation_page(tmp_path):
    out = _run_node(tmp_path, _capture_js(),
                    {"pathname": "/", "href": "https://chatgpt.com/", "pages": []})
    assert out["result"]["status"] == WRONG_ROUTE
    assert out["fetches"] == []


def test_capture_page_script_reports_logged_out_before_any_fetch(tmp_path):
    out = _run_node(tmp_path, _capture_js(), {"authStatus": "logged_out", "pages": []})
    assert out["result"]["status"] == LOGGED_OUT
    assert out["fetches"] == []          # no request is made on a logged-out page


def test_capture_page_script_reports_a_foreign_host(tmp_path):
    out = _run_node(tmp_path, _capture_js(),
                    {"hostname": "example.com", "href": "https://example.com/c/cid-1",
                     "pathname": "/c/cid-1", "pages": []})
    assert out["result"]["status"] == NOT_CHATGPT
