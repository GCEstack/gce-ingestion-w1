"""Acquisition run + checkpoint/resume tokens (manifest M7; CONTROL tables)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from .quarantine import _now


def start_run(conn: sqlite3.Connection, provider: str, note: Optional[str] = None) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO acquisition_run (run_id, provider, started_at, status, note) "
        "VALUES (?,?,?,?,?)",
        (run_id, provider, _now(), "RUNNING", note),
    )
    conn.commit()
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, status: str = "COMPLETE",
               note: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE acquisition_run SET finished_at=?, status=?, note=? WHERE run_id=?",
        (_now(), status, note, run_id),
    )
    conn.commit()


def save_checkpoint(conn: sqlite3.Connection, run_id: str, provider: str,
                    provider_conv_id: str) -> None:
    """Token = last provider_conv_id + run_id envelope (design C.7)."""
    token = json.dumps({"run_id": run_id, "last_provider_conv_id": provider_conv_id})
    conn.execute(
        "INSERT INTO checkpoint (run_id, provider, token, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(run_id, provider) DO UPDATE SET token=excluded.token, created_at=excluded.created_at",
        (run_id, provider, token, _now()),
    )
    conn.commit()


def load_checkpoint(conn: sqlite3.Connection, run_id: str, provider: str) -> Optional[str]:
    row = conn.execute(
        "SELECT token FROM checkpoint WHERE run_id=? AND provider=?",
        (run_id, provider),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["token"])
    return data.get("last_provider_conv_id")
