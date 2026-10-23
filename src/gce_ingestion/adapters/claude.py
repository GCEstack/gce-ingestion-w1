"""Claude adapter stub (Wave-1)."""

from __future__ import annotations

from ..transport.base import TransportClient
from .netnew import NetNewProviderStub


class ClaudeAdapter(NetNewProviderStub):
    provider = "claude"
