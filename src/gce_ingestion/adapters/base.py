"""Adapter interface (design C.6 / manifest M5/M6)."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..transport.base import ProbeResult


class AdapterNotImplementedError(NotImplementedError):
    """Raised by net-new provider stubs for enumerate/capture (A8 isolation)."""


@dataclass
class EnumerateResult:
    provider: str
    conversation_ids: List[str]
    structure_ok: bool = True
    # Per-conversation discovery metadata (title, canonical URL, provider update_time,
    # project/gizmo id, segment). Identity is the id in ``conversation_ids``; titles are
    # never identity.
    items: List[dict] = field(default_factory=list)


@dataclass
class CaptureResult:
    provider: str
    provider_conversation_id: str
    payload: bytes
    capture_path: str = "dom-snapshot"
    title_bytes: Optional[bytes] = None
    structure_ok: bool = True
    incomplete: bool = False
    failure_code: Optional[str] = None
    # Indexable per-conversation metadata (title, canonical URL, provider timestamps,
    # counts, warnings). JSON-serialisable; carries no message content.
    provider_metadata: Optional[Dict[str, Any]] = None


class AdapterInterface(abc.ABC):
    provider: str = ""

    @abc.abstractmethod
    def probe(self) -> ProbeResult:
        """Fail-closed gate (F2). Must not report ok unless structure confirmed."""

    @abc.abstractmethod
    def enumerate(self, account_ref: str, options: Optional[dict] = None) -> EnumerateResult:
        ...

    @abc.abstractmethod
    def traverse_and_capture(self, provider_conv_id: str, options: Optional[dict] = None) -> CaptureResult:
        ...

    @abc.abstractmethod
    def checkpoint(self) -> str:
        """Return opaque resume token."""

    @abc.abstractmethod
    def resume(self, token: str) -> None:
        ...


def has_known_structure_token(payload: dict) -> bool:
    """True iff payload carries a known conversation-structure marker.

    Used by probe(). The "Edit Copy Share" delimiter is NEVER accepted as the sole
    structure splitter.
    """
    if not isinstance(payload, dict):
        return False
    for key in ("structure_token", "conversation_marker", "data_conversation"):
        if payload.get(key):
            return True
    # A bare sole delimiter is NOT structure evidence.
    if isinstance(payload.get("sole_delimiter_only"), bool) and payload["sole_delimiter_only"]:
        return False
    return False
