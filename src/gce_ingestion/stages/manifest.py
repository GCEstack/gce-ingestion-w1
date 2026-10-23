"""Pass 1 -- capture manifest.

The authoritative inventory of what the browser pass actually captured, and what it did
not. Built from EVIDENCE (the raw observations) plus the persisted enumeration, so it
is derived from evidence rather than from anyone's memory of the run.

Writes a machine-readable manifest and a capture queue. Reads EVIDENCE and CONTROL;
writes CONTROL only. Never touches EVIDENCE.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..realms.handles import StageHandles

MANIFEST_SCHEMA = "gce.chatgpt.capture_manifest.v1"

# Queue states. Kept distinct so a rate-limited conversation is never mistaken for a
# processing failure.
NEEDS_CAPTURE = "needs_capture"
NEEDS_RECAPTURE = "needs_recapture"
PARTIAL = "partial"
RATE_LIMITED = "rate_limited"
CAPTURED = "captured"
FAILED = "failed"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def manifest_path(store_root: str) -> str:
    return os.path.join(store_root, "CONTROL", "capture_manifest.json")


def discovery_path(store_root: str) -> str:
    return os.path.join(store_root, "CONTROL", "discovery_chatgpt.json")


def load_discovery(store_root: str) -> Optional[Dict[str, Any]]:
    p = discovery_path(store_root)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _load_latest_observations(handles: StageHandles) -> Dict[str, Dict[str, Any]]:
    """Newest observation per conversation id, plus its full capture history count."""
    conn = handles.connection("EVIDENCE")
    rows = conn.execute(
        "SELECT observation_id, run_id, provider_conversation_id, scrape_timestamp, "
        "       capture_path, raw_payload_ref, content_hash, completeness_status, "
        "       partial_reason, provider_metadata "
        "FROM raw_observation WHERE provider='chatgpt' "
        "ORDER BY scrape_timestamp, rowid"
    ).fetchall()
    latest: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, int] = {}
    for r in rows:
        cid = r["provider_conversation_id"]
        counts[cid] = counts.get(cid, 0) + 1
        try:
            meta = json.loads(r["provider_metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        latest[cid] = {
            "observation_id": r["observation_id"],
            "run_id": r["run_id"],
            "conversation_id": cid,
            "capture_timestamp": r["scrape_timestamp"],
            "capture_path": r["capture_path"],
            "artifact_path": r["raw_payload_ref"],
            "content_hash": r["content_hash"],
            "completeness_status": r["completeness_status"],
            "partial_reason": r["partial_reason"],
            "artifact_present": os.path.exists(r["raw_payload_ref"]),
            "artifact_bytes": (os.path.getsize(r["raw_payload_ref"])
                               if os.path.exists(r["raw_payload_ref"]) else 0),
            "title": meta.get("title"),
            "canonical_url": meta.get("canonical_url"),
            "capture_status": meta.get("capture_status"),
            "capture_warnings": meta.get("warnings") or [],
            "message_count": meta.get("message_count", 0),
            "role_counts": meta.get("role_counts") or {},
            "project_id": meta.get("project_id"),
            "create_time": meta.get("create_time"),
            "update_time": meta.get("update_time"),
            "segments": meta.get("segments") or [],
        }
    for cid, obs in latest.items():
        obs["capture_count"] = counts[cid]
        obs["is_duplicate_capture"] = counts[cid] > 1
    return latest


def _load_quarantine(handles: StageHandles) -> Dict[str, Dict[str, Any]]:
    conn = handles.connection("CONTROL")
    out: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT failure_code, reason, resumable, created_at FROM quarantine_record "
        "WHERE stage='acquire' ORDER BY created_at"
    ):
        # reason format: "<provider> <conv_id> skipped: status=... reason=... url=..."
        parts = (r["reason"] or "").split()
        if len(parts) < 2:
            continue
        cid = parts[1]
        out[cid] = {
            "failure_code": r["failure_code"],
            "reason": r["reason"],
            "resumable": bool(r["resumable"]),
            "quarantined_at": r["created_at"],
        }
    return out


def _build_entry(cid: str, obs: Optional[Dict[str, Any]], disc: Optional[Dict[str, Any]],
                 quar: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "conversation_id": cid,
        "title": (obs or {}).get("title") or (disc or {}).get("title") or "Untitled",
        "canonical_url": (obs or {}).get("canonical_url"),
        "project_id": (obs or {}).get("project_id") or (disc or {}).get("project_id"),
        "discovered": disc is not None,
        "segments": (disc or {}).get("segments") or (obs or {}).get("segments") or [],
        "discovered_update_time": (disc or {}).get("update_time"),
        "captured": obs is not None,
    }
    if obs is None:
        if quar:
            entry["capture_state"] = RATE_LIMITED if "429" in (quar["reason"] or "") else FAILED
            entry["retry_status"] = "retryable" if quar["resumable"] else "blocked"
        else:
            entry["capture_state"] = NEEDS_CAPTURE
            entry["retry_status"] = "not_attempted"
        entry["capture_status"] = None
        entry["artifact_path"] = None
        entry["capture_warnings"] = []
        entry["capture_count"] = 0
        entry["message_count"] = 0
        if quar:
            entry["capture_error"] = quar["reason"]
            entry["failure_code"] = quar["failure_code"]
        return entry

    entry.update({
        "capture_status": obs.get("capture_status"),
        "completeness_status": obs.get("completeness_status"),
        "artifact_path": obs.get("artifact_path"),
        "artifact_present": obs.get("artifact_present"),
        "artifact_bytes": obs.get("artifact_bytes"),
        "content_hash": obs.get("content_hash"),
        "observation_id": obs.get("observation_id"),
        "capture_timestamp": obs.get("capture_timestamp"),
        "capture_warnings": obs.get("capture_warnings") or [],
        "message_count": obs.get("message_count", 0),
        "role_counts": obs.get("role_counts") or {},
        "create_time": obs.get("create_time"),
        "update_time": obs.get("update_time"),
        "capture_count": obs.get("capture_count", 1),
        "is_duplicate_capture": obs.get("is_duplicate_capture", False),
        "retry_status": "none",
    })
    if obs.get("completeness_status") == "COMPLETE" and obs.get("capture_status") == "ok":
        entry["capture_state"] = CAPTURED
    elif obs.get("capture_status") in ("partial", "rate_limited"):
        entry["capture_state"] = PARTIAL if obs.get("capture_status") == "partial" else RATE_LIMITED
        entry["retry_status"] = "retryable"
    else:
        entry["capture_state"] = NEEDS_RECAPTURE
        entry["retry_status"] = "retryable"
    if not obs.get("artifact_present"):
        entry["capture_state"] = NEEDS_RECAPTURE
        entry["retry_status"] = "retryable"
    if quar:
        entry["prior_quarantine"] = quar
    return entry


def build_manifest(store_root: str) -> Dict[str, Any]:
    """Read-only. Returns the manifest document; writes nothing."""
    with StageHandles("acquire", store_root) as handles:
        latest = _load_latest_observations(handles)
        quarantine = _load_quarantine(handles)
        ctrl = handles.connection("CONTROL")
        runs = [dict(r) for r in ctrl.execute(
            "SELECT run_id, status, started_at, finished_at, note FROM acquisition_run "
            "ORDER BY started_at")]
        checkpoints = [dict(r) for r in ctrl.execute(
            "SELECT provider, token, created_at FROM checkpoint ORDER BY created_at")]

    disc_doc = load_discovery(store_root) or {}
    discovered_items = {it["id"]: it for it in (disc_doc.get("items") or []) if it.get("id")}

    ids = sorted(set(discovered_items) | set(latest) | set(quarantine))
    entries = [_build_entry(cid, latest.get(cid), discovered_items.get(cid),
                            quarantine.get(cid)) for cid in ids]

    def count(pred) -> int:
        return sum(1 for e in entries if pred(e))

    by_state: Dict[str, int] = {}
    for e in entries:
        by_state[e["capture_state"]] = by_state.get(e["capture_state"], 0) + 1

    captured = [e for e in entries if e["captured"]]
    return {
        "schema": MANIFEST_SCHEMA,
        "generated_at": _now(),
        "store_root": store_root,
        "totals": {
            "discovered": len(discovered_items),
            "known_ids": len(ids),
            "captured": len(captured),
            "complete": count(lambda e: e.get("capture_state") == CAPTURED),
            "partial": count(lambda e: e.get("capture_state") == PARTIAL),
            "rate_limited": count(lambda e: e.get("capture_state") == RATE_LIMITED),
            "failed": count(lambda e: e.get("capture_state") == FAILED),
            "needs_capture": count(lambda e: e.get("capture_state") == NEEDS_CAPTURE),
            "needs_recapture": count(lambda e: e.get("capture_state") == NEEDS_RECAPTURE),
            "duplicate_captures": count(lambda e: e.get("is_duplicate_capture")),
            "messages": sum(e.get("message_count") or 0 for e in captured),
            "artifact_bytes": sum(e.get("artifact_bytes") or 0 for e in captured),
            "missing_artifacts": count(lambda e: e["captured"] and not e.get("artifact_present")),
        },
        "by_capture_state": by_state,
        "enumeration": {
            "status": disc_doc.get("status"),
            "captured_at": disc_doc.get("captured_at"),
            "segments": disc_doc.get("totals"),
        },
        "runs": runs,
        "checkpoints": checkpoints,
        "conversations": entries,
    }


def write_manifest(store_root: str, doc: Dict[str, Any]) -> str:
    p = manifest_path(store_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def capture_queue(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The remaining-capture queue, in a stable order. Never restarts from #1:
    previously captured ids are simply absent."""
    order = {NEEDS_CAPTURE: 0, RATE_LIMITED: 1, NEEDS_RECAPTURE: 2, PARTIAL: 3, FAILED: 4}
    q = [e for e in doc["conversations"] if e.get("capture_state") != CAPTURED]
    q.sort(key=lambda e: (order.get(e.get("capture_state"), 9), e.get("title") or ""))
    return q
