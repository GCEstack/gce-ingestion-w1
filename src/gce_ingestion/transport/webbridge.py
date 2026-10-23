"""WebBridge transport client (design C.6).

Envelope is PINNED. Evidence: C6 CONFIRMED 2026-09-23 --
``06_HANDOFFS/Don/20260923-KEEL-C6-WEBBRIDGE-VERIFICATION.md`` (24/24 command calls ok,
no 502, extension attached) plus the capture artifact alongside it.

Pinned shape (candidate A)::

    POST http://127.0.0.1:<port>/command
    {"action": "<tool>", "args": {...}, "session": "<name>"}

Reply: ``{"ok": true, "data": {...}}``. For ``evaluate`` the JS result is read from
``data.value``; a JS throw arrives as ``data.exceptionDetails``.

Observed runtime constraint (Stage 1, 2026-09-26): a bare ``evaluate`` before any
``navigate`` in the same session returns HTTP 502 -- the session has no target tab yet.
Callers must ``navigate`` (or ``find_tab``) first. ``ensure_target`` does that.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config import Config
from .base import (
    DaemonUnavailable,
    EnvelopeResult,
    ProbeResult,
    TransportClient,
    TransportError,
)

DAEMON_URL = "http://{host}:{port}"
STATUS_PATH = "/status"
COMMAND_PATH = "/command"
CHATGPT_HOME_FALLBACK = "https://chatgpt.com/"

_DISCONNECT_MARKERS = ("extension disconnected", "extension not connected", "no extension")


def _is_disconnect_text(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _DISCONNECT_MARKERS)


def _is_disconnect(payload: dict) -> bool:
    """True when the daemon answered but the browser extension is gone.

    Observed live 2026-09-26 mid-run. It is a transport condition, not a capture
    verdict, so it is raised as a retryable TransportError rather than being allowed
    to surface as a permanent per-conversation failure.
    """
    err = payload.get("error")
    return _is_disconnect_text(json.dumps(err) if err else "")


class WebBridgeClient(TransportClient):
    """Single client. One instance. Envelope pinned by C6 (2026-09-23)."""

    def __init__(self, config: Optional[Config] = None, session: str = "gce-ingestion"):
        self.cfg = config or Config()
        self.session = session
        self._target_url: Optional[str] = None
        self._closed = False

    # -- low level ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{DAEMON_URL.format(host=self.cfg.webbridge_host, port=self.cfg.webbridge_port)}{path}"

    def _command(self, action: str, args: dict) -> dict:
        body = json.dumps({"action": action, "args": args, "session": self.session}).encode()
        req = urllib.request.Request(
            self._url(COMMAND_PATH), data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.evaluate_timeout_s) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code == 502:
                # Two distinct causes share a 502: no target tab bound to this session
                # yet, and the extension having dropped. The body's own message says
                # which; surface it rather than guessing.
                raise TransportError(
                    f"webbridge 502 on {action}: {detail}"
                ) from exc
            raise TransportError(f"webbridge HTTP {exc.code} on {action}: {detail}") from exc
        except OSError as exc:
            raise TransportError(f"webbridge transport failure on {action}: {exc}") from exc
        if not payload.get("ok") and _is_disconnect(payload):
            raise TransportError(f"webbridge extension disconnected on {action}: "
                                 f"{payload.get('error')}")
        return payload

    # -- reachability ------------------------------------------------------
    def daemon_status(self) -> dict:
        """Raw ``/status`` JSON. Raises TransportError when unreachable."""
        try:
            with urllib.request.urlopen(self._url(STATUS_PATH), timeout=self.cfg.probe_timeout_s) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 - status is best-effort by contract
            raise TransportError(f"webbridge /status unreachable: {exc}") from exc

    def status(self) -> str:
        try:
            return "UP" if self.daemon_status().get("running") else "DOWN"
        except TransportError:
            return "DOWN"

    def extension_attached(self) -> bool:
        try:
            return bool(self.daemon_status().get("extension_connected"))
        except TransportError:
            return False

    def start(self) -> None:
        if self.status() == "DOWN":
            raise DaemonUnavailable(
                f"WebBridge {self.cfg.webbridge_host}:{self.cfg.webbridge_port} unreachable"
            )

    # -- probe (fail-closed) ----------------------------------------------
    def probe(self) -> ProbeResult:
        """Confirm daemon + extension before any walk.

        Fail-closed: daemon down or extension unattached => structure_ok=False and the
        caller must STOP. Never silently continue.
        """
        try:
            st = self.daemon_status()
        except TransportError as exc:
            return ProbeResult(structure_ok=False, signal="DAEMON_DOWN", detail=str(exc))
        if not st.get("running"):
            return ProbeResult(structure_ok=False, signal="DAEMON_DOWN",
                               detail="daemon reports running=false")
        if not st.get("extension_connected"):
            return ProbeResult(
                structure_ok=False, signal="EXTENSION_NOT_ATTACHED",
                detail="daemon up but browser extension is not attached; open Chrome with the extension enabled",
                observed_candidates=[f"daemon={st.get('version')}"],
            )
        return ProbeResult(
            structure_ok=True, signal="OK",
            detail=(f"daemon={st.get('version')} "
                    f"extension={st.get('extension_version')} session={self.session}"),
        )

    # -- navigation --------------------------------------------------------
    def navigate(self, url: str, new_tab: bool = True, group_title: Optional[str] = None) -> dict:
        args: dict[str, Any] = {"url": url, "newTab": new_tab}
        if group_title:
            args["group_title"] = group_title
        resp = self._command("navigate", args)
        if not resp.get("ok"):
            raise TransportError(f"navigate failed: {resp}")
        self._target_url = url
        return resp.get("data") or {}

    def ensure_target(self, url: str, group_title: Optional[str] = None) -> dict:
        """Bind this session to a tab, navigating only if not already there.

        Required before the first ``evaluate`` of a session: a bare evaluate is a 502.
        """
        if self._target_url and self._target_url.split("#")[0] == url.split("#")[0]:
            return {"reused": True, "url": self._target_url}
        return self.navigate(url, new_tab=True, group_title=group_title)

    # -- evaluate ----------------------------------------------------------
    def evaluate(self, script: str, timeout_s: Optional[int] = None) -> EnvelopeResult:
        if self._closed:
            raise TransportError("webbridge session already closed")
        last: Optional[Exception] = None
        attempts = max(1, self.cfg.transport_retries)
        for attempt in range(attempts):
            try:
                resp = self._command("evaluate", {"code": script})
                break
            except TransportError as exc:
                last = exc
                # A dropped extension can re-attach on its own; wait, then re-navigate
                # so the session has a target again, and only then give up.
                if _is_disconnect_text(str(exc)):
                    if self.wait_for_extension(timeout_s=self.cfg.extension_wait_s):
                        self._target_url = None
                        try:
                            self._command("navigate", {"url": CHATGPT_HOME_FALLBACK,
                                                       "newTab": True})
                        except TransportError:
                            pass
                    if attempt + 1 >= attempts:
                        raise
                elif attempt + 1 >= attempts:
                    raise
                time.sleep(self.cfg.retry_backoff_s[min(attempt, len(self.cfg.retry_backoff_s) - 1)])
        if not resp.get("ok"):
            raise TransportError(f"evaluate not ok: {resp}")
        data = resp.get("data") or {}
        if data.get("exceptionDetails"):
            raise TransportError(f"JS exception: {data['exceptionDetails']}")
        if "value" in data:
            value = data["value"]
        else:
            value = (data.get("result") or {}).get("value")
        return EnvelopeResult(action="evaluate", payload=value, session=self.session,
                              structure_ok=True)

    def evaluate_value(self, script: str, timeout_s: Optional[int] = None) -> Any:
        return self.evaluate(script, timeout_s=timeout_s).payload

    def wait_for_extension(self, timeout_s: int = 60, poll_s: float = 1.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.extension_attached():
                return True
            time.sleep(poll_s)
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._command("close_session", {})
        except TransportError:
            pass
