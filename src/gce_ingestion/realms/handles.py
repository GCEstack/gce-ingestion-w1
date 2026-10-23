"""Capability-scoped stage handles (design C.2 / manifest M1).

Pipeline stages get handles whose realm write-set is fixed by the capability
matrix. Attempting a write to a realm outside the stage's write-set raises
``RealmViolation`` and records a CONTROL AuditRecord. CANON is never writable
by any pipeline stage.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..store.migrations import connect_realm, ensure_store, realm_db_path
from ..realms import (
    ALL_REALMS,
    CANON_WRITE_STAGE,
    PIPELINE_STAGES,
    STAGE_WRITE_REALMS,
    RealmViolation,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(conn: sqlite3.Connection, actor: str, action: str, target_realm: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_record (audit_id, actor, action, target_realm, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, actor, action, target_realm, detail, _now()),
    )
    conn.commit()


class StageHandles:
    """Per-stage set of capability-scoped realm connections."""

    def __init__(self, stage: str, store_root: str):
        if stage not in PIPELINE_STAGES and stage != CANON_WRITE_STAGE:
            raise ValueError(f"unknown stage: {stage!r}")
        if stage == CANON_WRITE_STAGE:
            raise ValueError("coda_reconcile is a separate entrypoint; open via coda_reconcile.open_canon_write")
        self.stage = stage
        self.store_root = store_root
        ensure_store(store_root)
        self._write_realms = STAGE_WRITE_REALMS[stage]
        self._conns: dict[str, sqlite3.Connection] = {}
        for realm in sorted(ALL_REALMS):
            writable = realm in self._write_realms
            self._conns[realm] = connect_realm(store_root, realm, read_only=not writable)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            conn.close()
        self._conns.clear()

    def __enter__(self) -> "StageHandles":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- accessors ---------------------------------------------------------
    def connection(self, realm: str, write: bool = False) -> sqlite3.Connection:
        """Return a connection for ``realm``.

        read access is always allowed for all five realms; ``write=True`` raises
        ``RealmViolation`` (+ CONTROL AuditRecord) if the stage lacks capability.
        """
        realm = realm.upper()
        if realm not in ALL_REALMS:
            raise ValueError(f"unknown realm: {realm}")
        if write and realm not in self._write_realms:
            self._reject_write(realm)
            raise RealmViolation(f"{self.stage} cannot write {realm}")
        return self._conns[realm]

    def canon(self) -> sqlite3.Connection:
        """Read-only CANON handle. Writing CANON is impossible via any stage handle."""
        return self._conns["CANON"]

    def _reject_write(self, realm: str) -> None:
        # Record the violation in CONTROL (which all pipeline stages may write).
        try:
            _audit(
                self._conns["CONTROL"],
                actor=f"stage:{self.stage}",
                action="DENIED_REALM_WRITE",
                target_realm=realm,
                detail=f"{self.stage} attempted write to forbidden realm {realm}",
            )
        except sqlite3.Error:
            pass

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StageHandles stage={self.stage} write={sorted(self._write_realms)}>"
