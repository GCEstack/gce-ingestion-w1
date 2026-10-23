"""Passes 6 and 7 -- master index and human-review / reconciliation queue.

Both are read-only over the realm tables, so the index can be rebuilt at any time
without re-reading the browser and without touching evidence.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from ..realms.handles import StageHandles
from .normalize import normalized_dir
from .workers import summaries_dir

INDEX_SCHEMA = "gce.chatgpt.index.v1"
REVIEW_SCHEMA = "gce.chatgpt.review_queue.v1"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def index_path(store_root: str) -> str:
    return os.path.join(store_root, "VIEW", "index.json")


def index_markdown_path(store_root: str) -> str:
    return os.path.join(store_root, "VIEW", "index.md")


def review_path(store_root: str) -> str:
    return os.path.join(store_root, "VIEW", "review_queue.json")


def _dimensions(handles: StageHandles) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Latest classification per (conversation, dimension, value).

    Reads every generation, not a name prefix: quality uses ``qual-`` generations and
    workers use ``cls-``, and filtering on one prefix hid the other. Rows are keyed by
    conversation via the id embedded in ``basis``.
    """
    conn = handles.connection("DERIVED")
    best: Dict[tuple, Dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT generation_id, dimension, value, basis, basis_spans, confidence, "
        "       conflicts, review_state, created_at, rowid "
        "FROM classification_set ORDER BY rowid"
    ):
        cid = _conversation_of(r["basis"])
        if not cid:
            continue
        key = (cid, r["dimension"], r["value"])
        prior = best.get(key)
        if prior is None or (r["created_at"], r["rowid"]) >= (prior["created_at"],
                                                             prior["rowid"]):
            best[key] = {
                "value": r["value"], "confidence": r["confidence"],
                "review_state": r["review_state"], "generation_id": r["generation_id"],
                "created_at": r["created_at"], "conflicts": r["conflicts"],
                "rowid": r["rowid"],
            }
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for (cid, dim, _val), row in best.items():
        out.setdefault(cid, {}).setdefault(dim, []).append(row)
    return out


def _conversation_of(basis: Optional[str]) -> Optional[str]:
    if not basis:
        return None
    try:
        return (json.loads(basis) or {}).get("conversation_id")
    except (TypeError, ValueError):
        return None


def _keyword(rec: Dict[str, Any]) -> List[str]:
    """Explicit, evidence-derived keywords. Not a summary and not inferred authority."""
    kws = set()
    title = (rec.get("title") or "")
    for tok in title.replace("_", " ").replace("-", " ").split():
        t = tok.strip(".,:;()[]").lower()
        if len(t) > 3 and t not in ("chat", "the", "and", "for", "with", "from", "that", "this"):
            kws.add(t)
    for ref in (rec.get("top_paths") or [])[:6]:
        parts = str(ref).replace("\\", "/").split("/")
        for p in parts[-2:]:
            p = p.strip().lower()
            if len(p) > 3 and "." not in p:
                kws.add(p)
    return sorted(kws)[:20]


def build_index(store_root: str) -> Dict[str, Any]:
    """Materialize the account-level index from EVIDENCE + DERIVED."""
    from .manifest import build_manifest

    manifest = build_manifest(store_root)
    by_id = {e["conversation_id"]: e for e in manifest["conversations"]}
    ndir = normalized_dir(store_root)
    sdir = summaries_dir(store_root)

    with StageHandles("materialize", store_root) as handles:
        dims = _dimensions(handles)
        # Realm placement verified against the live schema: review_item and
        # supersession_candidate are CONTROL; contradiction_object is DERIVED.
        conn = handles.connection("CONTROL")
        review_rows = [dict(r) for r in conn.execute(
            "SELECT review_id, scope, scope_id, lane, state, created_at FROM review_item "
            "ORDER BY created_at")]
        framework_candidates = [dict(r) for r in conn.execute(
            "SELECT candidate_id, proposed_for, basis, state, created_at "
            "FROM supersession_candidate ORDER BY created_at")]
        pending_reviews = len([r for r in review_rows if r["state"] == "PENDING"])
        dconn = handles.connection("DERIVED")
        contradictions = [dict(r) for r in dconn.execute(
            "SELECT contradiction_id, generation_id, detail, created_at "
            "FROM contradiction_object ORDER BY created_at")]

    records: List[Dict[str, Any]] = []
    for cid, entry in sorted(by_id.items(), key=lambda kv: (kv[1].get("title") or "")):
        rec: Optional[Dict[str, Any]] = None
        npath = os.path.join(ndir, f"{cid}.json")
        if os.path.exists(npath):
            try:
                with open(npath, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                rec = None
        d = dims.get(cid, {})
        quality = next((x["value"] for x in d.get("capture_quality", [])), None)
        projects = [x["value"] for x in d.get("project", [])]
        workstreams = [x["value"] for x in d.get("workstream", [])]
        lifecycle = [x["value"] for x in d.get("lifecycle_state", [])]
        values = [x["value"] for x in d.get("durable_value", [])]
        routing = [x["value"] for x in d.get("candidate_routing", [])]
        summary_file = os.path.join(sdir, f"{cid}.md")
        conf = max([x["confidence"] or 0 for vs in d.values() for x in vs], default=None)

        records.append({
            "conversation_id": cid,
            "title": entry.get("title"),
            "canonical_url": entry.get("canonical_url"),
            "project_id": entry.get("project_id"),
            "date": (rec or {}).get("create_time") or entry.get("create_time"),
            "updated": (rec or {}).get("update_time") or entry.get("update_time"),
            "turn_count": (rec or {}).get("turn_count", entry.get("message_count", 0)),
            "capture_state": entry.get("capture_state"),
            "capture_status": entry.get("capture_status"),
            "capture_timestamp": entry.get("capture_timestamp"),
            "capture_warnings": entry.get("capture_warnings") or [],
            "capture_quality": quality or "unverified",
            "primary_project": projects[0] if projects else None,
            "secondary_projects": projects[1:],
            "workstream": workstreams,
            "lifecycle_state": lifecycle[0] if lifecycle else None,
            "durable_value": values,
            "candidate_routing": routing[0] if routing else None,
            "framework_candidate": any(v.get("proposed_for") == cid for v in framework_candidates),
            "classification_confidence": conf,
            "keywords": _keyword(rec) if rec else [],
            "summary_path": summary_file if os.path.exists(summary_file) else None,
            "normalized_path": npath if rec else None,
            "source_artifact_path": entry.get("artifact_path"),
            "source_content_hash": entry.get("content_hash"),
            "in_review_queue": any(r["scope_id"] == cid for r in review_rows),
            "needs_reconciliation": any(cid in (r.get("detail") or "") for r in contradictions),
        })

    def tally(key: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in records:
            v = r.get(key)
            if isinstance(v, list):
                for x in v:
                    out[x] = out.get(x, 0) + 1
            elif v:
                out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "schema": INDEX_SCHEMA,
        "generated_at": _now(),
        "store_root": store_root,
        "totals": {
            "conversations": len(records),
            "normalized": sum(1 for r in records if r["normalized_path"]),
            "summarized": sum(1 for r in records if r["summary_path"]),
            "classified": sum(1 for r in records if r["primary_project"]),
            "quality_verified": sum(1 for r in records if r["capture_quality"] != "unverified"),
            "in_review_queue": sum(1 for r in records if r["in_review_queue"]),
            "framework_candidates": sum(1 for r in records if r["framework_candidate"]),
            "captured_messages": manifest["totals"]["messages"],
        },
        "distributions": {
            "capture_state": tally("capture_state"),
            "capture_quality": tally("capture_quality"),
            "primary_project": tally("primary_project"),
            "workstream": tally("workstream"),
            "lifecycle_state": tally("lifecycle_state"),
            "durable_value": tally("durable_value"),
            "candidate_routing": tally("candidate_routing"),
            "project_id": tally("project_id"),
        },
        "framework_candidates": framework_candidates,
        "contradictions": contradictions,
        "pending_control_review_items": pending_reviews,
        "conversations": records,
    }


def _index_markdown(doc: Dict[str, Any]) -> str:
    L: List[str] = []
    a = L.append
    a("# ChatGPT archive index")
    a("")
    a(f"- generated_at : {doc['generated_at']}")
    t = doc["totals"]
    a(f"- conversations: {t['conversations']}  captured: {doc['distributions']['capture_state'].get('captured',0)}"
      f"  normalized: {t['normalized']}  classified: {t['classified']}"
      f"  summarized: {t['summarized']}  in review: {t['in_review_queue']}")
    a("")
    for name in ("capture_state", "capture_quality", "primary_project", "workstream",
                 "lifecycle_state", "candidate_routing"):
        dist = doc["distributions"][name]
        if not dist:
            continue
        a(f"## {name}")
        a("")
        a("| value | n |")
        a("|---|---|")
        for k, v in dist.items():
            a(f"| {k} | {v} |")
        a("")
    a("## conversations")
    a("")
    a("| title | id | project | workstream | life | quality | turns | routing |")
    a("|---|---|---|---|---|---|---|---|")
    for r in sorted(doc["conversations"], key=lambda x: (x.get("title") or "")):
        a("| {t} | `{c}` | {p} | {w} | {l} | {q} | {n} | {r} |".format(
            t=(r.get("title") or "")[:46], c=(r.get("conversation_id") or "")[:8],
            p=r.get("primary_project") or "-",
            w=",".join(r.get("workstream") or []) or "-",
            l=r.get("lifecycle_state") or "-", q=r.get("capture_quality"),
            n=r.get("turn_count"), r=r.get("candidate_routing") or "-"))
    a("")
    a("Derived index. Not canon. Every row links to its raw artifact by content hash.")
    a("")
    return "\n".join(L)


def write_index(store_root: str, doc: Dict[str, Any]) -> Dict[str, str]:
    p = index_path(store_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    mp = index_markdown_path(store_root)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(_index_markdown(doc))
    return {"json": p, "markdown": mp}


def build_review_queue(store_root: str) -> Dict[str, Any]:
    """Materialize the human-review / reconciliation queue.

    Scoped to CAPTURED conversations only. A conversation that was never captured is a
    capture-queue concern (CONTROL/capture_queue.json), not a human-judgement one; mixing
    them buried 502 real review items under capture backlog.
    """
    index = build_index(store_root)
    items: List[Dict[str, Any]] = []
    not_captured = 0
    for r in index["conversations"]:
        if r["capture_state"] != "captured":
            not_captured += 1
            continue
        reasons: List[str] = []
        if r["capture_quality"] in ("invalid", "needs_recapture", "unverified"):
            reasons.append(f"capture_quality={r['capture_quality']}")
        if r["lifecycle_state"] in (None, "unknown"):
            reasons.append("lifecycle_state unknown")
        if r["primary_project"] in (None, "unclassified", "cross-project"):
            reasons.append("project classification uncertain")
        if (r["classification_confidence"] or 0) < 0.5:
            reasons.append(f"classification confidence {r['classification_confidence']}")
        if r["needs_reconciliation"]:
            reasons.append("conflicting state reported by worker")
        if r["framework_candidate"]:
            reasons.append("flagged framework_candidate -- not promoted, needs human ruling")
        if r["candidate_routing"] == "needs_human_review":
            reasons.append("worker routed to human review")
        if r["capture_warnings"]:
            reasons.append("capture warnings: " + ",".join(r["capture_warnings"][:3]))
        if not r["summary_path"]:
            reasons.append("captured but not summarized")
        if reasons:
            items.append({
                "conversation_id": r["conversation_id"],
                "title": r["title"],
                "reasons": reasons,
                "capture_state": r["capture_state"],
                "capture_quality": r["capture_quality"],
                "lifecycle_state": r["lifecycle_state"],
                "primary_project": r["primary_project"],
                "source_artifact_path": r["source_artifact_path"],
                "normalized_path": r["normalized_path"],
                "summary_path": r["summary_path"],
            })
    return {
        "schema": REVIEW_SCHEMA,
        "generated_at": _now(),
        "scope": "captured conversations needing human judgement; uncaptured backlog is "
                 "tracked separately in CONTROL/capture_queue.json",
        "counts": {
            "total": len(items),
            "captured_conversations": index["totals"]["conversations"] - not_captured,
            "not_captured_excluded": not_captured,
            "by_reason": _by_reason(items),
            "control_review_items_pending":
                index.get("pending_control_review_items") or 0,
            "worker_reported_contradictions": len(index.get("contradictions") or []),
        },
        "items": items,
    }


def _by_reason(items: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        for r in it["reasons"]:
            key = r.split("=")[0].split(" ")[0]
            out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def write_review_queue(store_root: str, doc: Dict[str, Any]) -> str:
    p = review_path(store_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return p
