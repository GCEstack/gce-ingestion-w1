"""FakeTransport — deterministic transport for unit tests (design C.6 / G fallback).

WebBridge daemon is offline this wave; all live acquire is skipped and unit tests
run against FakeTransport.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

from .base import EnvelopeResult, ProbeResult, TransportClient


class FakeTransport(TransportClient):
    """Scripted transport over a synthetic conversation registry.

    ``conversations`` maps provider_conv_id -> {"html": str, "title": str}.
    """

    def __init__(self, conversations: Optional[Dict[str, dict]] = None,
                 conversations_path: Optional[str] = None):
        self._conversations: Dict[str, dict] = {}
        if conversations:
            self._conversations.update(conversations)
        if conversations_path and os.path.exists(conversations_path):
            with open(conversations_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._conversations.update(data.get("conversations", data))
        self._current: Optional[str] = None
        self._closed = False

    # -- TransportClient ---------------------------------------------------
    def status(self) -> str:
        return "UP"

    def start(self) -> None:
        return None

    def navigate(self, url: str) -> None:
        path = urlparse(url).path  # "kimi://conversation/<id>" -> /conversation/<id>
        self._current = unquote(path.rsplit("/", 1)[-1])

    def evaluate(self, script: str, timeout_s: Optional[int] = None) -> EnvelopeResult:
        payload: dict = {}
        if "conversation_ids" in script:
            payload["conversation_ids"] = list(self._conversations.keys())
            payload["structure_token"] = "found" if self._conversations else ""
        elif "data-conversation" in script and "structure_token" not in script:
            # conversation-list enumeration over [data-conversation] nodes
            payload["conversation_ids"] = list(self._conversations.keys())
            payload["structure_token"] = "found" if self._conversations else ""
        elif "structure_token" in script:
            cur = self._conversations.get(self._current) if self._current else None
            if self._current is None:
                # index/probe context: structure present iff any conversations exist
                payload["structure_token"] = "found" if self._conversations else ""
                payload["conversation_marker"] = len(self._conversations)
            else:
                payload["structure_token"] = "found" if cur else ""
                payload["conversation_marker"] = 1 if cur else 0
                if cur:
                    payload["html"] = cur.get("html", "")
                    payload["title_bytes"] = cur.get("title", "")
        return EnvelopeResult(action="evaluate", payload=payload,
                              session=None, structure_ok=True)

    def close(self) -> None:
        self._closed = True
