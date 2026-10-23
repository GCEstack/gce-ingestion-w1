"""Quarantine + resolution (manifest M3; design C.9 / C.4 CONTROL tables)."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..failures import FailureCode


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class QuarantineRecord:
    quarantine_id: str
    observation_id: Optional[str]
    failure_code: FailureCode
    reason: str
    stage: str
    created_at: str
    resumable: bool = True


@dataclass
class ResolutionEvent:
    event_id: str
    quarantine_id: str
    resolved_to: str
    basis: Optional[str]
    resolved_at: str = field(default_factory=_now)


def record_quarantine(
    conn: sqlite3.Connection,
    failure_code: FailureCode,
    reason: str,
    stage: str,
    observation_id: Optional[str] = None,
    resumable: Optional[bool] = None,
) -> QuarantineRecord:
    """Insert a quarantine_record (CONTROL). resumability follows F-code by default."""
    if resumable is None:
        # F3 (quarantine PARTIAL resumable) and general quarantine codes resumable;
        # F2 / F13 stop their scope. Mirror contract: resumable unless STOP.
        resumable = failure_code not in (
            FailureCode.F1, FailureCode.F2, FailureCode.F13,
        )
    rec = QuarantineRecord(
        quarantine_id=uuid.uuid4().hex,
        observation_id=observation_id,
        failure_code=failure_code,
        reason=reason,
        stage=stage,
        created_at=_now(),
        resumable=resumable,
    )
    conn.execute(
        "INSERT INTO quarantine_record "
        "(quarantine_id, observation_id, failure_code, reason, stage, created_at, resumable) "
        "VALUES (?,?,?,?,?,?,?)",
        (rec.quarantine_id, rec.observation_id, rec.failure_code.value,
         rec.reason, rec.stage, rec.created_at, int(rec.resumable)),
    )
    conn.commit()
    return rec


def resolve_quarantine(
    conn: sqlite3.Connection,
    quarantine_id: str,
    resolved_to: str,
    basis: Optional[str] = None,
) -> ResolutionEvent:
    ev = ResolutionEvent(
        event_id=uuid.uuid4().hex,
        quarantine_id=quarantine_id,
        resolved_to=resolved_to,
        basis=basis,
    )
    conn.execute(
        "INSERT INTO resolution_event (event_id, quarantine_id, resolved_to, basis, resolved_at) "
        "VALUES (?,?,?,?,?)",
        (ev.event_id, ev.quarantine_id, ev.resolved_to, ev.basis, ev.resolved_at),
    )
    conn.commit()
    return ev


def write_audit(conn: sqlite3.Connection, actor: str, action: str,
                target_realm: str, detail: Optional[str] = None) -> str:
    audit_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO audit_record (audit_id, actor, action, target_realm, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (audit_id, actor, action, target_realm, detail, _now()),
    )
    conn.commit()
    return audit_id
