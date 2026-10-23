"""KimiAdapter — first provider (design C.6). DOM-snapshot capture path.

Native-first ordering: api > native-export > dom-snapshot, but Wave-1 ships
dom-snapshot only when probe passes. Raw payload = faithful capture (no
extraction-time normalization). probe() is fail-closed.
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote

from ..failures import FailureCode
from ..transport.base import ProbeResult, TransportClient
from .base import (
    AdapterInterface,
    CaptureResult,
    EnumerateResult,
    has_known_structure_token,
)

_DOM_SNAPSHOT = "dom-snapshot"
# Adapter-versioned marker: NOT a sole splitter — just one of several structure
# tokens the probe asserts.
_STRUCTURE_SCRIPT = "return { structure_token: document.querySelector('[data-conversation]') ? 'found' : '', conversation_marker: document.querySelectorAll('[data-conversation]').length }"


class KimiAdapter(AdapterInterface):
    provider = "kimi"

    def __init__(self, transport: TransportClient, version: str = "w1-0.1.0"):
        self._transport = transport
        self._version = version
        self._last_token: Optional[str] = None

    # -- probe: fail-closed ------------------------------------------------
    def probe(self) -> ProbeResult:
        """Confirm a known structure token before any walk.

        Fail-closed (F2): empty DOM, daemon down, or no structure token => STOP,
        never silently walk an empty/unknown archive.
        """
        if self._transport.status() == "DOWN":
            return ProbeResult(structure_ok=False, signal="F2_STOP",
                               detail="transport down")
        try:
            res = self._transport.evaluate(_STRUCTURE_SCRIPT)
        except Exception as exc:  # noqa: BLE001  (fail-closed on any transport error)
            return ProbeResult(structure_ok=False, signal="F2_STOP",
                               detail=f"probe transport error: {exc}")
        payload = res.payload
        if not has_known_structure_token(payload):
            return ProbeResult(structure_ok=False, signal="F2_STOP",
                               detail="no known structure token; empty/unknown DOM (fail-closed)")
        return ProbeResult(structure_ok=True, signal="OK",
                           detail=f"adapter {self._version} structure confirmed")

    # -- enumerate ---------------------------------------------------------
    def enumerate(self, account_ref: str, options: Optional[dict] = None) -> EnumerateResult:
        probe = self.probe()
        if not probe.structure_ok:
            raise RuntimeError("enumerate blocked: probe() did not confirm structure (F2)")
        # Deterministic: FakeTransport script returns a bounded conversation list.
        res = self._transport.evaluate(
            "return Array.from(document.querySelectorAll('[data-conversation]')).map(e=>e.getAttribute('data-conversation'))"
        )
        ids = list(res.payload.get("conversation_ids", []))
        return EnumerateResult(provider=self.provider, conversation_ids=ids,
                               structure_ok=True)

    # -- traverse + capture ------------------------------------------------
    def traverse_and_capture(self, provider_conv_id: str,
                             options: Optional[dict] = None) -> CaptureResult:
        probe = self.probe()
        if not probe.structure_ok:
            return CaptureResult(
                provider=self.provider, provider_conversation_id=provider_conv_id,
                payload=b"", structure_ok=False, incomplete=True,
                failure_code=FailureCode.F2.value,
            )
        self._transport.navigate(self._url(provider_conv_id))
        res = self._transport.evaluate(_STRUCTURE_SCRIPT)
        payload = res.payload
        # Message split must find structure; on miss -> F6 quarantine (not silent).
        if not has_known_structure_token(payload):
            return CaptureResult(
                provider=self.provider, provider_conversation_id=provider_conv_id,
                payload=b"", structure_ok=False, incomplete=True,
                failure_code=FailureCode.F6.value,
            )
        html = res.payload.get("html", b"").encode("utf-8", "surrogatepass")
        title = payload.get("title_bytes", b"").encode("utf-8", "surrogatepass")
        self._last_token = provider_conv_id
        return CaptureResult(
            provider=self.provider, provider_conversation_id=provider_conv_id,
            payload=html, capture_path=_DOM_SNAPSHOT,
            title_bytes=title or None, structure_ok=True,
        )

    def checkpoint(self) -> str:
        return self._last_token or ""

    def resume(self, token: str) -> None:
        self._last_token = token

    @staticmethod
    def _url(provider_conv_id: str) -> str:
        return "kimi://conversation/" + quote(provider_conv_id, safe="")
