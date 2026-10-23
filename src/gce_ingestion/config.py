"""Configuration / parameter matrix (Calibro design F). Copy, do not invent."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


def force_utf8() -> None:
    """Set PYTHONUTF8 behaviour + reconfigure stdout/stderr to utf-8.

    Every entrypoint must call this first (Windows charmap guard).
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def store_root_required() -> str:
    """Required env GCE_INGESTION_STORE_ROOT; raise if absent."""
    root = os.environ.get("GCE_INGESTION_STORE_ROOT")
    if not root:
        raise RuntimeError("GCE_INGESTION_STORE_ROOT is required")
    return root


@dataclass(frozen=True)
class Config:
    webbridge_host: str = os.environ.get("WEBBRIDGE_HOST", "127.0.0.1")
    webbridge_port: int = int(os.environ.get("WEBBRIDGE_PORT", "10086"))
    chrome_path: str = os.environ.get(
        "CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    probe_timeout_s: int = 15
    evaluate_timeout_s: int = 30
    wait_for_load_s: int = 20
    walk_delay_s: float = 0.5
    transport_retries: int = 3
    retry_backoff_s: tuple = (1, 2, 4)
    # A dropped browser extension can re-attach on its own; wait for it, bounded.
    extension_wait_s: int = 30
    hash_alg: str = "sha256"
    sqlite_journal: str = "WAL"
    # Retries apply ONLY to transport/network. Never retry F1/F2/F7/F9/F13.
    retryable_failures: tuple = ("TRANSPORT",)
