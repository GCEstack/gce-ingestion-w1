"""Net-new provider stub base (A8 isolation)."""

from __future__ import annotations

from typing import Optional

from ..transport.base import ProbeResult, TransportClient
from .base import (
    AdapterInterface,
    AdapterNotImplementedError,
    CaptureResult,
    EnumerateResult,
)


class NetNewProviderStub(AdapterInterface):
    """Wave-1 stub for a not-yet-enabled provider.

    probe() -> structure_ok=False, NET_NEW_PROVIDER.
    enumerate/capture -> AdapterNotImplementedError.
    """

    provider = ""

    def __init__(self, transport: TransportClient):
        self._transport = transport

    def probe(self) -> ProbeResult:
        return ProbeResult(structure_ok=False, signal="NET_NEW_PROVIDER",
                           detail=f"{self.provider} acquisition not enabled in Wave-1")

    def enumerate(self, account_ref: str, options: Optional[dict] = None) -> EnumerateResult:
        raise AdapterNotImplementedError(f"{self.provider}.enumerate not implemented (Wave-1)")

    def traverse_and_capture(self, provider_conv_id: str,
                             options: Optional[dict] = None) -> CaptureResult:
        raise AdapterNotImplementedError(
            f"{self.provider}.traverse_and_capture not implemented (Wave-1)"
        )

    def checkpoint(self) -> str:
        return ""

    def resume(self, token: str) -> None:
        return None
