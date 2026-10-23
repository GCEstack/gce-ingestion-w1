"""Pass 4/5 -- worker contract, schema validation, and ingestion of sub-agent output.

A judging worker (a sub-agent) reads bounded digests and returns JSON in the schemas
below. This module is the gate: it validates shape, vocabulary, and provenance, rejects
anything unproven, and writes into DERIVED using the existing INSERT-only tables.

Deliberate constraints encoded here:
* a worker may only assert what its evidence supports -- unproven state is
  ``unknown`` and goes to the review queue, never to a confident bucket;
* ``framework_candidate`` is a *flag*, stored as a knowledge_object whose
  authority_state can never be VERIFIED (schema CHECK), so nothing becomes canon;
* conflicting values across workers are recorded as contradictions, not silently merged;
* a worker cannot invent a conversation that is not in its assigned batch.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..realms.handles import StageHandles
from .normalize import normalized_dir

WORKER_SCHEMA = "gce.chatgpt.worker_output.v1"
CLASSIFIER_VERSION = "worker-v2"
# The generation id carries the contract version. classification_set is INSERT-only, so a
# change to what a row means must produce a new generation lineage rather than an
# in-place correction; the index resolves each (conversation, dimension, value) to its
# newest generation.
GEN_VERSION = "v2"

# -- controlled vocabularies -------------------------------------------------
PRIMARY_PROJECTS = (
    "GCE Framework", "GCE Stack", "DevPad", "Cable Car", "VoiceBridge", "EC8",
    "Mastermind", "Employment", "Fantasy Sports", "NCAA", "Travel", "Finance",
    "Personal Admin", "Archive", "Other",
)
SPECIAL_PRIMARY = ("unclassified", "cross-project")

WORKSTREAMS = (
    "architecture", "implementation", "troubleshooting", "recon", "governance",
    "planning", "research", "operations", "deployment", "verification",
    "data_acquisition", "documentation", "brainstorming", "decision_making",
    "handoff", "historical_archive",
)

LIFECYCLE_STATES = (
    "active", "current_reference", "completed", "superseded", "stale",
    "abandoned", "historical", "unknown",
)
# A worker may never assert current authority from an old conversation alone.
AUTHORITY_ASSERTING_STATES = ("active", "current_reference")
NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

DURABLE_VALUES = (
    "accepted_decision", "architecture", "implementation_truth", "reusable_process",
    "artifact_reference", "unresolved_blocker", "historical_provenance",
    "low_value_chatter", "duplicate_discussion",
)
PROMOTABLE_VALUES = tuple(v for v in DURABLE_VALUES if v != "low_value_chatter")

SUMMARY_SECTIONS = (
    "conversation_purpose", "key_outcomes", "important_facts", "artifacts",
    "open_items", "current_relevance", "authority_warning", "candidate_routing",
)
ROUTING_OPTIONS = (
    "archive_only", "project_current_state_candidate", "project_handoff_candidate",
    "framework_reconciliation_candidate", "duplicate_or_low_value", "needs_human_review",
)

DIM_PROJECT = "project"
DIM_WORKSTREAM = "workstream"
DIM_LIFECYCLE = "lifecycle_state"
DIM_VALUE = "durable_value"
DIM_QUALITY = "capture_quality"
DIM_ROUTING = "candidate_routing"

ALLOWED_DIMS = (DIM_PROJECT, DIM_WORKSTREAM, DIM_LIFECYCLE, DIM_VALUE, DIM_QUALITY, DIM_ROUTING)

SUMMARY_DIR_NAME = "summaries"
REVIEW_LANE = "luce"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slug(v: str, maxlen: int = 24) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in (v or "x").lower())[:maxlen].strip("-") or "x"


def summaries_dir(store_root: str) -> str:
    return os.path.join(store_root, "DERIVED", SUMMARY_DIR_NAME)


class WorkerRejected(ValueError):
    """Worker output failed the contract. Never ingested; always reported."""


# -- validation --------------------------------------------------------------
def _as_list(v) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _clean_str(v, maxlen: int = 400) -> str:
    """Normalize whitespace WITHOUT destroying line structure.

    A summary's value is its bullet list. Collapsing newlines here turned every worker's
    '- item' lines into one run-on paragraph, which is a real content loss -- caught by
    reading an ingested summary back, not by a test.
    """
    lines = [" ".join(str(line).split()) for line in str(v or "").splitlines()]
    out = "\n".join(ln for ln in lines if ln)
    return out[:maxlen]


def validate_classification(rec: Any, batch_ids: Iterable[str]) -> Tuple[Dict[str, Any], List[str]]:
    """Validate one classification record. Returns (clean, problems)."""
    problems: List[str] = []
    if not isinstance(rec, dict):
        return {}, ["record is not an object"]
    cid = rec.get("conversation_id")
    if not cid:
        return {}, ["missing conversation_id"]
    if cid not in set(batch_ids):
        problems.append(f"conversation_id {cid} is not in the assigned batch")
        return {}, problems

    out: Dict[str, Any] = {"conversation_id": cid}

    primary = rec.get("primary_project")
    if primary in SPECIAL_PRIMARY or primary in PRIMARY_PROJECTS:
        out["primary_project"] = primary
    else:
        problems.append(f"primary_project {primary!r} not in vocabulary")
        out["primary_project"] = "unclassified"

    secondary = []
    for p in _as_list(rec.get("secondary_projects")):
        p = _clean_str(p, 80)
        if p and p not in secondary:
            secondary.append(p)
    out["secondary_projects"] = secondary[:8]

    ws = []
    for w in _as_list(rec.get("workstream")):
        w = _clean_str(w, 60)
        if w in WORKSTREAMS and w not in ws:
            ws.append(w)
        elif w not in WORKSTREAMS and w:
            problems.append(f"workstream {w!r} not in vocabulary")
    out["workstream"] = ws or ["research"]

    life = rec.get("lifecycle_state")
    conf = rec.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    if life not in LIFECYCLE_STATES:
        problems.append(f"lifecycle_state {life!r} not in vocabulary")
        life = "unknown"
    if life in AUTHORITY_ASSERTING_STATES and conf < 0.7:
        # Never let a low-confidence worker assert current authority.
        problems.append(f"lifecycle_state {life!r} asserted at confidence {conf}")
        life = "unknown"
    if life == "unknown":
        problems.append("lifecycle unknown -> reconciliation required")
    out["lifecycle_state"] = life
    out["confidence"] = round(max(0.0, min(1.0, conf)), 3)

    vals = []
    for v in _as_list(rec.get("durable_value")):
        v = _clean_str(v, 60)
        if v in DURABLE_VALUES and v not in vals:
            vals.append(v)
        elif v not in DURABLE_VALUES and v:
            problems.append(f"durable_value {v!r} not in vocabulary")
    out["durable_value"] = vals

    fc = rec.get("framework_candidate")
    out["framework_candidate"] = bool(fc) if isinstance(fc, bool) else False
    if out["framework_candidate"] and conf < 0.6:
        problems.append("framework_candidate asserted below 0.6 confidence -> queued for review")

    hr = rec.get("human_review_required")
    reasons = [ _clean_str(x, 160) for x in _as_list(rec.get("human_review_reasons")) if x ]
    out["human_review_required"] = bool(hr) or conf < 0.5 or life == "unknown" or bool(problems)
    out["human_review_reasons"] = reasons[:6]

    if rec.get("needs_reconciliation") or rec.get("conflict"):
        out["needs_reconciliation"] = True
        out["conflict_detail"] = _clean_str(
            rec.get("conflict_detail") or rec.get("conflict"), 400)
    else:
        out["needs_reconciliation"] = False

    out["evidence"] = {
        "basis": _clean_str(rec.get("basis"), 400),
        "basis_spans": _clean_str(rec.get("basis_spans"), 200),
    }
    out["warnings"] = problems[:8]
    return out, problems


def validate_summary(rec: Any, batch_ids: Iterable[str]) -> Tuple[Dict[str, Any], List[str]]:
    problems: List[str] = []
    if not isinstance(rec, dict):
        return {}, ["summary is not an object"]
    cid = rec.get("conversation_id")
    if not cid:
        return {}, ["missing conversation_id"]
    if cid not in set(batch_ids):
        return {}, [f"conversation_id {cid} is not in the assigned batch"]
    out: Dict[str, Any] = {"conversation_id": cid}
    for section in SUMMARY_SECTIONS:
        val = rec.get(section)
        if section == "candidate_routing":
            if val not in ROUTING_OPTIONS:
                problems.append(f"candidate_routing {val!r} not in vocabulary")
                out[section] = "needs_human_review"
            else:
                out[section] = val
            continue
        if val is None:
            problems.append(f"missing section {section}")
            out[section] = ""
        else:
            out[section] = _clean_str(val, 4000)
    conf = rec.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = round(max(0.0, min(1.0, conf)), 3)
    if conf < 0.5:
        out["candidate_routing"] = "needs_human_review"
        problems.append("summary confidence below 0.5 -> routed to review")
    out["warnings"] = problems[:8]
    return out, problems


def _summary_markdown(s: Dict[str, Any], rec_meta: Dict[str, Any]) -> str:
    def bullets(key: str) -> str:
        raw = s.get(key) or ""
        items = [ln.strip(" -*\t") for ln in raw.splitlines() if ln.strip(" -*\t")]
        if not items:
            return "_none stated_"
        return "\n".join(f"- {i}" for i in items)

    return "\n".join([
        f"# {rec_meta.get('title') or s['conversation_id']}",
        "",
        f"- conversation_id : {s['conversation_id']}",
        f"- canonical_url   : {rec_meta.get('canonical_url')}",
        f"- turns           : {rec_meta.get('turn_count')}  captured={rec_meta.get('capture_status')}",
        f"- summary confidence: {s.get('confidence')}",
        f"- candidate routing: {s.get('candidate_routing')}",
        "",
        "## Conversation purpose", s.get("conversation_purpose") or "_not stated_", "",
        "## Key outcomes", bullets("key_outcomes"), "",
        "## Important facts", bullets("important_facts"), "",
        "## Artifacts", bullets("artifacts"), "",
        "## Open items", bullets("open_items"), "",
        "## Current relevance", s.get("current_relevance") or "_not stated_", "",
        "## Authority warning", s.get("authority_warning") or "_none stated_", "",
        "---",
        "Derived summary. Not canon, not authority. Raw evidence: "
        f"`{rec_meta.get('provenance', {}).get('source_artifact_path')}` "
        f"(sha256 `{str(rec_meta.get('provenance', {}).get('source_content_hash'))[:16]}...`).",
        "",
    ])


# -- ingestion ---------------------------------------------------------------
def _insert_dimension(conn, generation_id: str, cid: str, dimension: str, value: str,
                      basis: Dict[str, Any], confidence: float, conflicts: Any,
                      review_state: str) -> None:
    # The conversation id MUST be inside basis: classification_set has no conversation
    # column, and the index resolves rows to conversations through this field. Omitting
    # it silently made every classification invisible downstream.
    basis_payload = {"conversation_id": cid, **(basis or {})}
    conn.execute(
        "INSERT OR IGNORE INTO classification_set "
        "(generation_id, dimension, value, basis, basis_spans, confidence, conflicts, "
        " review_state, review_lane, classifier_version, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (generation_id, dimension, value, json.dumps(basis_payload),
         (basis or {}).get("basis_spans"), confidence,
         json.dumps(conflicts) if conflicts else None,
         review_state, REVIEW_LANE if review_state != "CLEAR" else None,
         CLASSIFIER_VERSION, _now()),
    )


def _queue_review(handles: StageHandles, scope: str, scope_id: str, lane: str,
                  generation_id: str) -> None:
    conn = handles.connection("CONTROL", write=True)
    conn.execute(
        "INSERT INTO review_item (review_id, scope, scope_id, lane, state, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, scope, scope_id, lane, "PENDING", _now()),
    )
    conn.commit()


def ingest_worker_output(store_root: str, payload: Dict[str, Any],
                         batch_ids: List[str]) -> Dict[str, Any]:
    """Validate and ingest one worker's output. Rejects the batch atomically on
    contract failure so a partial/garbled worker cannot half-apply."""
    if not isinstance(payload, dict) or payload.get("schema") != WORKER_SCHEMA:
        raise WorkerRejected(f"expected schema {WORKER_SCHEMA!r}")
    worker = _clean_str(payload.get("worker"), 80) or "unnamed"
    workstream = _clean_str(payload.get("workstream"), 60)
    batch = set(batch_ids)

    raw_cls = payload.get("classifications") or []
    raw_sum = payload.get("summaries") or []
    if not isinstance(raw_cls, list) or not isinstance(raw_sum, list):
        raise WorkerRejected("classifications/summaries must be lists")

    clean_cls: List[Dict[str, Any]] = []
    clean_sum: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    all_problems: List[str] = []

    for rec in raw_cls:
        clean, problems = validate_classification(rec, batch)
        if not clean:
            rejected.append({"kind": "classification", "record": str(rec)[:200],
                             "problems": problems})
            all_problems.extend(problems)
            continue
        clean_cls.append(clean)
        all_problems.extend(clean.get("warnings") or [])

    for rec in raw_sum:
        clean, problems = validate_summary(rec, batch)
        if not clean:
            rejected.append({"kind": "summary", "record": str(rec)[:200], "problems": problems})
            all_problems.extend(problems)
            continue
        clean_sum.append(clean)
        all_problems.extend(clean.get("warnings") or [])

    sdir = summaries_dir(store_root)
    os.makedirs(sdir, exist_ok=True)
    ndir = normalized_dir(store_root)
    generation_id = f"cls-{workstream}-{uuid.uuid4().hex[:8]}"
    written_summaries: List[str] = []
    review_queued: List[str] = []

    with StageHandles("classify", store_root) as handles:
        conn = handles.connection("DERIVED", write=True)
        ctrl = handles.connection("CONTROL", write=True)
        for c in clean_cls:
            cid = c["conversation_id"]
            # Per-conversation generation: classification_set is keyed
            # (generation_id, dimension, value), so a shared generation would collapse
            # every conversation asserting the same value into one row.
            generation_id = f"cls-{GEN_VERSION}-{cid[:16]}-{_slug(workstream)}"
            review_state = "PENDING" if c["human_review_required"] else "CLEAR"
            _insert_dimension(conn, generation_id, cid, DIM_PROJECT,
                              c["primary_project"], c["evidence"], c["confidence"],
                              c.get("secondary_projects") or None, review_state)
            for w in c["workstream"]:
                _insert_dimension(conn, generation_id, cid, DIM_WORKSTREAM, w,
                                  c["evidence"], c["confidence"], None, review_state)
            _insert_dimension(conn, generation_id, cid, DIM_LIFECYCLE, c["lifecycle_state"],
                              c["evidence"], c["confidence"], None, review_state)
            for v in c["durable_value"]:
                _insert_dimension(conn, generation_id, cid, DIM_VALUE, v,
                                  c["evidence"], c["confidence"], None, review_state)
            if c["framework_candidate"]:
                # knowledge_object cannot be VERIFIED (schema CHECK). A candidate only.
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_object "
                    "(knowledge_id, generation_id, authority_state, personal_domain, "
                    " content_ref, evidence_elements, provenance, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"kc-{cid}-{uuid.uuid4().hex[:8]}", generation_id, "CANDIDATE", 0,
                     cid, 1, json.dumps({"basis": c["evidence"].get("basis"),
                                         "worker": worker}), _now()),
                )
                # supersession_candidate is a CONTROL table in this schema.
                ctrl.execute(
                    "INSERT INTO supersession_candidate (candidate_id, proposed_for, basis, "
                    "state, created_at) VALUES (?,?,?,?,?)",
                    (uuid.uuid4().hex, cid,
                     json.dumps({"kind": "framework_candidate", "worker": worker,
                                 "confidence": c["confidence"]}), "PENDING", _now()),
                )
            if c.get("needs_reconciliation"):
                # contradiction_object is a DERIVED table in this schema.
                conn.execute(
                    "INSERT INTO contradiction_object (contradiction_id, generation_id, "
                    "detail, created_at) VALUES (?,?,?,?)",
                    (uuid.uuid4().hex, generation_id,
                     json.dumps({"conversation_id": cid, "kind": "worker_reported_conflict",
                                 "detail": c.get("conflict_detail", "")}), _now()),
                )
            if review_state == "PENDING":
                _queue_review(handles, f"classification:{workstream}", cid, REVIEW_LANE,
                              generation_id)
                review_queued.append(cid)
        conn.commit()

        for s in clean_sum:
            cid = s["conversation_id"]
            generation_id = f"cls-{GEN_VERSION}-{cid[:16]}-{_slug(workstream)}"
            npath = os.path.join(ndir, f"{cid}.json")
            meta: Dict[str, Any] = {}
            if os.path.exists(npath):
                try:
                    with open(npath, "r", encoding="utf-8") as fh:
                        r = json.load(fh)
                    meta = {"title": r.get("title"), "canonical_url": r.get("canonical_url"),
                            "turn_count": r.get("turn_count"),
                            "capture_status": (r.get("capture") or {}).get("capture_status"),
                            "provenance": r.get("provenance") or {}}
                except (OSError, ValueError):
                    meta = {}
            spath = os.path.join(sdir, f"{cid}.md")
            with open(spath, "w", encoding="utf-8") as fh:
                fh.write(_summary_markdown(s, meta))
            written_summaries.append(spath)
            _insert_dimension(conn, generation_id, cid, DIM_ROUTING, s["candidate_routing"],
                              {"basis": "worker summary"}, s["confidence"], None,
                              "CLEAR" if s["candidate_routing"] != "needs_human_review" else "PENDING")
            if s["candidate_routing"] == "needs_human_review" or s["confidence"] < 0.5:
                _queue_review(handles, "summary", cid, REVIEW_LANE, generation_id)
                review_queued.append(cid)
        conn.commit()

    return {
        "schema": WORKER_SCHEMA,
        "worker": worker,
        "workstream": workstream,
        "generation_id": generation_id,
        "batch_size": len(batch_ids),
        "classifications_ingested": len(clean_cls),
        "summaries_ingested": len(clean_sum),
        "summaries_written": written_summaries,
        "rejected": rejected,
        "review_queued": sorted(set(review_queued)),
        "validation_problems": all_problems[:40],
    }

