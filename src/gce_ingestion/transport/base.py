"""Transport abstraction (design C.6). One TransportClient in production."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ProbeResult:
    structure_ok: bool
    signal: str = ""          # e.g. "NET_NEW_PROVIDER", "OK"
    detail: Optional[str] = None
    observed_candidates: list = field(default_factory=list)


@dataclass
class EnvelopeResult:
    action: str
    payload: Any
    session: Optional[str] = None
    structure_ok: bool = False


class TransportError(Exception):
    """Transport/network-level error. Retryable (config transport_retries)."""


class DaemonUnavailable(TransportError):
    pass


class TransportClient(abc.ABC):
    @abc.abstractmethod
    def status(self) -> str: ...
    @abc.abstractmethod
    def start(self) -> None: ...
    @abc.abstractmethod
    def navigate(self, url: str) -> None: ...
    @abc.abstractmethod
    def evaluate(self, script: str, timeout_s: Optional[int] = None) -> EnvelopeResult: ...
    @abc.abstractmethod
    def close(self) -> None: ...
