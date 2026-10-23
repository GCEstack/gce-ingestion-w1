"""Pass 3 -- quality / completeness verification.

Checks each normalized record back against its raw capture. This stage identifies
evidence-quality problems; it never repairs semantic content and never re-captures.

Quality verdicts:
  complete                 -- nothing found wrong
  complete_with_warnings   -- usable, with recorded evidence-quality caveats
  partial                  -- the provider walk itself was truncated
  invalid                  -- the normalized record does not match the raw capture
  needs_recapture          -- the artifact is missing, unreadable, or empty
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from ..realms.handles import StageHandles
from .manifest import build_manifest
from .normalize import KNOWN_ROLES, NORMALIZED_SCHEMA, normalized_dir

QUALITY_DIMENSION = "capture_quality"
QUALITY_VERSION = "quality-v1"

COMPLETE = "complete"
COMPLETE_WITH_WARNINGS = "complete_with_warnings"
PARTIAL = "partial"
INVALID = "invalid"
NEEDS_RECAPTURE = "needs_recapture"

VERDICTS = (COMPLETE, COMPLETE_WITH_WARNINGS, PARTIAL, INVALID, NEEDS_RECAPTURE)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_normalized(rec: Dict[str, Any], manifest_entry: Optional[Dict[str, Any]],
                     raw_doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare one normalized record to its raw capture. Pure."""
    findings: List[Dict[str, str]] = []
    cid = rec.get("conversation_id")

    if rec.get("schema") != NORMALIZED_SCHEMA:
        findings.append({"code": "schema_mismatch", "detail": str(rec.get("schema"))})
    if not cid:
        findings.append({"code": "missing_conversation_id", "detail": "no id"})
    if not rec.get("turns"):
        findings.append({"code": "zero_turns", "detail": "normalized record has no turns"})

    if manifest_entry is not None:
        if not manifest_entry.get("artifact_present"):
            findings.append({"code": "artifact_missing", "detail": "raw blob not on disk"})
        if manifest_entry.get("capture_state") == "partial":
            findings.append({"code": "provider_walk_truncated",
                             "detail": "capture completed as partial"})
        for w in manifest_entry.get("capture_warnings") or []:
            findings.append({"code": "capture_warning", "detail": w})
        raw_count = manifest_entry.get("message_count")
        if raw_count is not None and raw_count != rec.get("turn_count"):
            findings.append({"code": "turn_count_mismatch",
                             "detail": f"raw={raw_count} normalized={rec.get('turn_count')}"})

    if raw_doc is not None:
        raw_msgs = raw_doc.get("messages") or []
        if len(raw_msgs) != rec.get("turn_count"):
            findings.append({"code": "raw_turn_count_mismatch",
                             "detail": f"raw={len(raw_msgs)} normalized={rec.get('turn_count')}"})
        raw_ids = [m.get("id") for m in raw_msgs if isinstance(m, dict)]
        norm_ids = [t.get("message_id") for t in rec.get("turns") or []]
        if [i for i in raw_ids if i] != [i for i in norm_ids if i]:
            findings.append({"code": "message_id_sequence_mismatch",
                             "detail": "normalized ids differ from raw ids in order"})
        if raw_doc.get("capture_status") == "partial":
            findings.append({"code": "provider_walk_truncated", "detail": "raw status partial"})

    if not rec.get("ordering_is_monotonic"):
        findings.append({"code": "ordering_not_monotonic", "detail": "create_time out of order"})

    ids = [t.get("message_id") for t in rec.get("turns") or []]
    real = [i for i in ids if i]
    if len(real) != len(set(real)):
        findings.append({"code": "duplicate_message_ids", "detail": "repeated message id"})

    empties = sum(1 for t in rec.get("turns") or [] if not t.get("text"))
    if empties:
        # A ratio, not a bare count. Empty turns are normal for tool/system/reasoning
        # traffic, so a universal boolean carried no signal; the ratio does discriminate
        # (observed range across the corpus: 20% to 93%).
        ratio = empties / max(1, rec.get("turn_count") or 0)
        if ratio > 0.75:
            findings.append({"code": "mostly_empty_turns",
                             "detail": f"{empties}/{rec['turn_count']} ({ratio:.0%}) turns "
                                       f"carry no extractable text"})
        else:
            findings.append({"code": "empty_turns",
                             "detail": f"{empties}/{rec['turn_count']} ({ratio:.0%}) turns "
                                       f"carry no extractable text"})
    # A single turn holding nearly everything is the classic unsplit blob.
    if rec.get("turn_count"):
        biggest = max((t["text_chars"] for t in rec["turns"]), default=0)
        total = rec.get("total_text_chars") or 0
        if rec["turn_count"] > 5 and total and biggest / total > 0.95:
            findings.append({"code": "unsplit_blob",
                             "detail": f"one turn holds {biggest/total:.0%} of the text"})

    unknown_roles = sorted({t.get("role") for t in rec.get("turns") or []
                            if t.get("role") not in KNOWN_ROLES})
    if unknown_roles:
        findings.append({"code": "unknown_roles", "detail": ",".join(unknown_roles)})

    unextractable = rec.get("unextractable_part_kinds") or []
    if unextractable:
        findings.append({"code": "unextractable_parts", "detail": ",".join(unextractable)})

    # Encoding: the record is already decoded text, so a replacement char is the signal.
    blob = json.dumps(rec, ensure_ascii=False)
    if "\ufffd" in blob:
        findings.append({"code": "encoding_replacement_char", "detail": "U+FFFD present"})

    blocking = {"artifact_missing", "zero_turns", "schema_mismatch", "raw_turn_count_mismatch",
                "message_id_sequence_mismatch", "encoding_replacement_char",
                "missing_conversation_id", "duplicate_message_ids"}
    codes = {f["code"] for f in findings}
    if "artifact_missing" in codes:
        verdict = NEEDS_RECAPTURE
    elif codes & blocking:
        verdict = INVALID
    elif "provider_walk_truncated" in codes:
        verdict = PARTIAL
    elif findings:
        verdict = COMPLETE_WITH_WARNINGS
    else:
        verdict = COMPLETE

    return {
        "conversation_id": cid,
        "capture_quality": verdict,
        "findings": findings,
        "finding_codes": sorted(codes),
        "turn_count": rec.get("turn_count", 0),
        "empty_turns": empties,
    }


def run_quality(store_root: str) -> Dict[str, Any]:
    """Verify every normalized record present on disk. Writes DERIVED only."""
    manifest = build_manifest(store_root)
    by_id = {e["conversation_id"]: e for e in manifest["conversations"]}
    ndir = normalized_dir(store_root)
    results: List[Dict[str, Any]] = []

    if not os.path.isdir(ndir):
        return {"generation_id": "qual-none", "checked": 0, "results": [],
                "by_verdict": {}, "note": "no normalized records; run pass 2 first"}

    with StageHandles("classify", store_root) as handles:
        conn = handles.connection("DERIVED", write=True)
        for name in sorted(os.listdir(ndir)):
            if not name.endswith(".json"):
                continue
            cid = name[:-5]
            path = os.path.join(ndir, name)
            # classification_set is keyed (generation_id, dimension, value), so a
            # shared generation would collapse every conversation holding the same
            # verdict into a single row. One generation per conversation.
            generation_id = f"qual-{cid[:16]}"
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError) as exc:
                results.append({"conversation_id": cid, "capture_quality": INVALID,
                                "findings": [{"code": "unreadable_normalized", "detail": str(exc)}],
                                "finding_codes": ["unreadable_normalized"],
                                "turn_count": 0, "empty_turns": 0,
                                "generation_id": generation_id})
                continue
            raw_doc = None
            entry = by_id.get(cid)
            if entry and entry.get("artifact_present"):
                try:
                    with open(entry["artifact_path"], "r", encoding="utf-8") as fh:
                        raw_doc = json.load(fh)
                except (OSError, ValueError):
                    raw_doc = None
            res = check_normalized(rec, entry, raw_doc)
            res["generation_id"] = generation_id
            results.append(res)
            conn.execute(
                "INSERT OR IGNORE INTO classification_set "
                "(generation_id, dimension, value, basis, basis_spans, confidence, "
                " conflicts, review_state, review_lane, classifier_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (generation_id, QUALITY_DIMENSION, res["capture_quality"],
                 json.dumps({"conversation_id": cid}),
                 None, 1.0,
                 json.dumps(res["findings"]) if res["findings"] else None,
                 "PENDING" if res["capture_quality"] in (INVALID, NEEDS_RECAPTURE) else "CLEAR",
                 "luce" if res["capture_quality"] in (INVALID, NEEDS_RECAPTURE) else None,
                 QUALITY_VERSION, _now()),
            )
        conn.commit()

    by_verdict: Dict[str, int] = {}
    for r in results:
        by_verdict[r["capture_quality"]] = by_verdict.get(r["capture_quality"], 0) + 1
    return {"generation_id": "per-conversation", "checked": len(results), "results": results,
            "by_verdict": by_verdict}
