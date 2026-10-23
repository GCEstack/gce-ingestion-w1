"""G1-G6 mechanical gates (manifest M4; design C.8 — no simplification).

G1 predicate + G2 (no VERIFIED) + G3 candidates-only + G4 personal-domain flag +
G5 current-pointers + G6 RESTRICTED redaction.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# G1 — auto-flow predicate. AUTO-FLOW only if all five Coda conditions hold.
# ---------------------------------------------------------------------------
@dataclass
class Classification:
    dimension: str
    value: str
    basis: str
    basis_spans: str
    confidence: float
    conflicts: Optional[str] = None
    review_state: str = "REVIEW-PENDING"
    review_lane: Optional[str] = None
    classifier_version: Optional[str] = None


def g1_autoflow_ok(clf: Classification) -> bool:
    """Encoded Coda auto-flow conditions as a predicate function.

    AUTO-FLOW requires basis + spans present, full confidence, no conflicts, and
    REVIEW-PENDING start. Anything less stays REVIEW-PENDING (never silently boosted).
    """
    if not clf.dimension or not clf.value:
        return False
    if not clf.basis or not clf.basis_spans:
        return False
    if clf.confidence is None or clf.confidence < 1.0:
        return False
    if clf.conflicts:          # CONFLICT never collapsed
        return False
    if clf.review_state not in ("REVIEW-PENDING",):
        return False
    return True


# ---------------------------------------------------------------------------
# G2 — knowledge authority_state: candidates only; VERIFIED impossible.
# ---------------------------------------------------------------------------
_AUTHORITY_CANDIDATES = {"CANDIDATE", "APPROVED", "REJECTED"}
# APPROVED/REJECTED-candidate insert requires six evidence elements.
REQUIRED_EVIDENCE_ELEMENTS = 6


def authority_state_allowed(state: str) -> bool:
    if state == "VERIFIED":
        return False
    return state in _AUTHORITY_CANDIDATES


def can_emit_knowledge(authority_state: str, evidence_elements: int) -> bool:
    """Emission refused (F9) unless provenance complete and G2 satisfied."""
    if not authority_state_allowed(authority_state):
        return False
    if evidence_elements < REQUIRED_EVIDENCE_ELEMENTS:
        return False
    return True


# ---------------------------------------------------------------------------
# G3 — pipeline writes supersession_candidate ONLY (never supersession_event).
# ---------------------------------------------------------------------------
def record_supersession_candidate(conn: sqlite3.Connection, proposed_for: str,
                                  basis: str) -> str:
    candidate_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO supersession_candidate (candidate_id, proposed_for, basis, state, created_at) "
        "VALUES (?,?,?,?,?)",
        (candidate_id, proposed_for, basis, "PENDING", _now()),
    )
    conn.commit()
    return candidate_id


# ---------------------------------------------------------------------------
# G4 — personal_domain flag + materialization gate (promotion needs a reason).
# ---------------------------------------------------------------------------
def promotion_reason_required(personal_domain: bool) -> bool:
    return personal_domain


# ---------------------------------------------------------------------------
# G5 — current-pointer bookkeeping; historical rows never deleted.
# ---------------------------------------------------------------------------
def set_current_pointer(conn: sqlite3.Connection, stage: str, scope: str,
                        generation_id: str) -> None:
    conn.execute(
        "INSERT INTO current_pointer (stage, scope, generation_id, updated_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(stage, scope) DO UPDATE SET "
        " generation_id=excluded.generation_id, updated_at=excluded.updated_at",
        (stage, scope, generation_id, _now()),
    )
    conn.commit()


def get_current_pointer(conn: sqlite3.Connection, stage: str, scope: str) -> Optional[str]:
    row = conn.execute(
        "SELECT generation_id FROM current_pointer WHERE stage=? AND scope=?",
        (stage, scope),
    ).fetchone()
    return row["generation_id"] if row else None


# ---------------------------------------------------------------------------
# G6 — RESTRICTED handling: derived stores refs, VIEW redacts by default.
# ---------------------------------------------------------------------------
RESTRICTED_REDACTION = "[REDACTED: RESTRICTED]"


def view_text(handling_domain: str, raw_text: str) -> str:
    """VIEW-facing text. RESTRICTED content is redacted by default (G6)."""
    if handling_domain == "RESTRICTED":
        return RESTRICTED_REDACTION
    return raw_text
