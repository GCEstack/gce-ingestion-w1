"""Archive processing pipeline: RAW -> NORMALIZE -> QUALITY -> CLASSIFY -> SUMMARY
-> INDEX -> REVIEW.

Passes 1-3, 6 and 7 are deterministic and run here. Passes 4 and 5 (project /
workstream / lifecycle / value classification, and summarization) are judgement work and
are delegated to bounded sub-agents; this module owns their contract, their batches, and
the merge of their output.

    python -m gce_ingestion.cli.archive_pipeline --store-root <dir> pass1
    python -m gce_ingestion.cli.archive_pipeline --store-root <dir> all
    python -m gce_ingestion.cli.archive_pipeline --store-root <dir> batches --size 6
    python -m gce_ingestion.cli.archive_pipeline --store-root <dir> ingest <worker.json>

The browser is never touched by this pipeline. It reads EVIDENCE only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from ..config import force_utf8
from ..stages import index as index_mod
from ..stages import manifest as manifest_mod
from ..stages import normalize as norm_mod
from ..stages import quality as quality_mod
from ..stages import workers as workers_mod
from ..stages.workers import WORKER_SCHEMA, WorkerRejected


def _out(doc: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))


def cmd_pass1(store_root: str, args) -> int:
    doc = manifest_mod.build_manifest(store_root)
    p = manifest_mod.write_manifest(store_root, doc)
    queue = manifest_mod.capture_queue(doc)
    qpath = os.path.join(store_root, "CONTROL", "capture_queue.json")
    with open(qpath, "w", encoding="utf-8") as fh:
        json.dump({"schema": "gce.chatgpt.capture_queue.v1",
                   "generated_at": doc["generated_at"],
                   "note": "remaining capture work; does not restart from #1",
                   "count": len(queue), "items": queue}, fh, ensure_ascii=False, indent=2)
    _out({"manifest": p, "capture_queue": qpath,
          "totals": doc["totals"], "by_capture_state": doc["by_capture_state"],
          "queue_size": len(queue)})
    return 0


def cmd_pass2(store_root: str, args) -> int:
    res = norm_mod.normalize_store(store_root, only=args.only)
    _out({"normalized": len(res["normalized"]), "skipped": res["skipped"],
          "normalized_dir": res["normalized_dir"], "digest_dir": res["digest_dir"],
          "generation_id": res["generation_id"],
          "sample": res["normalized"][:5]})
    return 0


def cmd_pass3(store_root: str, args) -> int:
    res = quality_mod.run_quality(store_root)
    flagged = [r for r in res["results"] if r["capture_quality"] != "complete"]
    _out({"checked": res["checked"], "by_verdict": res["by_verdict"],
          "flagged": [{"id": r["conversation_id"], "verdict": r["capture_quality"],
                       "codes": r["finding_codes"]} for r in flagged]})
    return 0


def cmd_batches(store_root: str, args) -> int:
    """Emit sub-agent work packages. Each is a self-contained brief + batch id list."""
    index = index_doc(store_root)
    records = [r for r in index["conversations"] if r["capture_state"] == "captured"]
    if args.ids:
        want = list(args.ids)
        by_id = {r["conversation_id"]: r for r in records}
        missing = [c for c in want if c not in by_id]
        if missing:
            _out({"error": "unknown or uncaptured conversation ids", "missing": missing})
            return 2
        todo = [by_id[c] for c in want]
    else:
        todo = [r for r in records if not r["summary_path"] or not r["primary_project"]]
        if args.limit:
            todo = todo[: args.limit]
    size = args.size
    prefix = args.prefix
    packages = []
    for i in range(0, len(todo), size):
        chunk = todo[i:i + size]
        cid_list = [r["conversation_id"] for r in chunk]
        pkg = {
            "schema": "gce.chatgpt.worker_batch.v1",
            "batch_id": f"{prefix}-{i // size + 1:02d}",
            "conversation_ids": cid_list,
            "conversations": [
                {"conversation_id": r["conversation_id"], "title": r["title"],
                 "turns": r["turn_count"], "date": r["date"],
                 "digest_path": os.path.join(norm_mod.digest_dir(store_root),
                                             f"{r['conversation_id']}.md"),
                 "normalized_path": r["normalized_path"],
                 "capture_quality": r["capture_quality"]}
                for r in chunk
            ],
        }
        bp = os.path.join(store_root, "DERIVED", "batches", f"{pkg['batch_id']}.json")
        os.makedirs(os.path.dirname(bp), exist_ok=True)
        with open(bp, "w", encoding="utf-8") as fh:
            json.dump(pkg, fh, ensure_ascii=False, indent=2)
        packages.append({"batch_id": pkg["batch_id"], "path": bp, "size": len(cid_list),
                         "titles": [r["title"] for r in chunk],
                         "conversation_ids": cid_list})
    _out({"batches": packages, "todo": len(todo), "captured": len(records)})
    return 0


def index_doc(store_root: str) -> Dict[str, Any]:
    return index_mod.build_index(store_root)


def cmd_index(store_root: str, args) -> int:
    doc = index_mod.build_index(store_root)
    paths = index_mod.write_index(store_root, doc)
    _out({"index": paths, "totals": doc["totals"], "distributions": doc["distributions"]})
    return 0


def cmd_review(store_root: str, args) -> int:
    doc = index_mod.build_review_queue(store_root)
    p = index_mod.write_review_queue(store_root, doc)
    _out({"review_queue": p, "counts": doc["counts"],
          "sample": [i["conversation_id"] for i in doc["items"][:15]]})
    return 0


def _load_worker_file(path: str) -> Tuple[Dict[str, Any], List[str]]:
    """Read a worker file. LLM-authored JSON often contains a raw control character
    inside a string, which strict JSON forbids. Tolerate it, but say so -- a lenient
    read is reported, never silent."""
    notes: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text), notes
    except json.JSONDecodeError as exc:
        try:
            doc = json.loads(text, strict=False)
        except json.JSONDecodeError:
            raise
        notes.append(f"strict JSON parse failed ({exc.msg} at line {exc.lineno}); "
                     f"re-read with strict=False to tolerate a raw control character")
        return doc, notes


def cmd_ingest(store_root: str, args) -> int:
    results = []
    for path in args.files:
        try:
            payload, notes = _load_worker_file(path)
        except (OSError, ValueError) as exc:
            results.append({"file": path, "error": f"unreadable: {exc}"})
            continue
        batch_ids = payload.get("conversation_ids") or []
        if not batch_ids:
            bp = payload.get("batch_path")
            if bp and os.path.exists(bp):
                with open(bp, "r", encoding="utf-8") as bf:
                    batch_ids = json.load(bf).get("conversation_ids") or []
        if not batch_ids:
            results.append({"file": path, "error": "no batch ids; nothing ingested",
                            "notes": notes})
            continue
        try:
            res = ingest_worker_output_safe(store_root, payload, batch_ids)
            res["file"] = path
            if notes:
                res["ingest_notes"] = notes
            results.append(res)
        except WorkerRejected as exc:
            results.append({"file": path, "rejected": str(exc), "notes": notes})
    _out({"ingested": results})
    return 0


def ingest_worker_output_safe(store_root: str, payload: Dict[str, Any],
                              batch_ids: List[str]) -> Dict[str, Any]:
    return workers_mod.ingest_worker_output(store_root, payload, batch_ids)


def cmd_all(store_root: str, args) -> int:
    for fn in (cmd_pass1, cmd_pass2, cmd_pass3, cmd_index, cmd_review):
        rc = fn(store_root, args)
        if rc:
            return rc
    return 0


def cmd_status(store_root: str, args) -> int:
    _out(index_doc(store_root)["totals"])
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    force_utf8()
    p = argparse.ArgumentParser(description="ChatGPT archive processing pipeline")
    p.add_argument("--store-root", required=True)
    p.add_argument("command",
                   choices=["pass1", "pass2", "pass3", "all", "batches", "index", "review",
                            "ingest", "status"])
    p.add_argument("--only", nargs="*", help="limit pass 2 to these conversation ids")
    p.add_argument("--size", type=int, default=6, help="conversations per worker batch")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--ids", nargs="*", help="explicit conversation ids for the batch")
    p.add_argument("--prefix", default="batch", help="batch id prefix")
    p.add_argument("files", nargs="*", help="worker output files (ingest)")
    args = p.parse_args(argv)

    store_root = os.path.abspath(args.store_root)
    os.makedirs(store_root, exist_ok=True)
    table = {
        "pass1": cmd_pass1, "pass2": cmd_pass2, "pass3": cmd_pass3, "all": cmd_all,
        "batches": cmd_batches, "index": cmd_index, "review": cmd_review,
        "ingest": cmd_ingest, "status": cmd_status,
    }
    return table[args.command](store_root, args)


if __name__ == "__main__":
    sys.exit(main())
