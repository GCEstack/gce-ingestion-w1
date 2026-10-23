"""ChatGPT browser backup CLI.

Read-oriented. Enumerates Don's ChatGPT conversations from the already-authenticated
browser session and stores each one as durable, content-addressed evidence.

    python -m gce_ingestion.cli.chatgpt_backup --store-root <dir> [--max 3] [--force]

Non-destructive by construction: the only browser actions are navigation and same-origin
GETs. No send, no edit, no archive, no settings change, no cookie export.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..adapters.chatgpt import OK, ChatGPTAdapter
from ..config import Config, force_utf8
from ..control.run import finish_run, start_run
from ..evidence import latest_observation
from ..stages.acquire import ingest_observations
from ..transport.webbridge import WebBridgeClient


def _discovery_path(store_root: str) -> Path:
    return Path(store_root) / "CONTROL" / "discovery_chatgpt.json"


def load_discovery(store_root: str) -> Optional[Dict[str, Any]]:
    """Last COMPLETE enumeration. Discovery is rate-limited (~1 success/60s on the
    live account, 2026-09-26), so a capture run reuses it instead of re-hitting the
    list endpoint and throttling itself."""
    p = _discovery_path(store_root)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if doc.get("status") == OK else None


def save_discovery(store_root: str, doc: Dict[str, Any]) -> Path:
    p = _discovery_path(store_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    tmp.replace(p)
    return p


def _needs_capture(prev: Optional[dict], item: Dict[str, Any], force: bool) -> bool:
    """Incremental decision. Identity is the conversation id; the provider's
    ``update_time`` is the change signal. ``force`` overrides."""
    if force or prev is None:
        return True
    prev_update = (prev.get("provider_metadata") or {}).get("update_time")
    now_update = item.get("update_time")
    if not now_update or not prev_update:
        return True
    return str(now_update) != str(prev_update)


def plan(store_root: str, items: List[Dict[str, Any]], force: bool) -> List[Dict[str, Any]]:
    """Decide the capture set from stored state, without touching the browser."""
    from ..realms.handles import StageHandles

    todo: List[Dict[str, Any]] = []
    with StageHandles("acquire", store_root) as handles:
        for it in items:
            prev = latest_observation(handles, "chatgpt", it["id"])
            if _needs_capture(prev, it, force):
                todo.append({**it, "reason": "force" if force and prev else
                             ("new" if prev is None else "changed")})
    return todo


def summarize(store_root: str) -> Dict[str, Any]:
    """Read-only picture of what is already backed up."""
    from ..realms.handles import StageHandles

    out: Dict[str, Any] = {"conversations": 0, "messages": 0, "by_status": {},
                           "segments": {}, "projects": {}, "warnings": []}
    with StageHandles("acquire", store_root) as handles:
        conn = handles.connection("EVIDENCE")
        rows = conn.execute(
            "SELECT provider_conversation_id, provider_metadata, completeness_status "
            "FROM raw_observation WHERE provider='chatgpt' ORDER BY scrape_timestamp, rowid"
        ).fetchall()
        seen = set()
        for row in rows:
            if row["provider_conversation_id"] in seen:
                continue
            seen.add(row["provider_conversation_id"])
            out["conversations"] += 1
            out["by_status"][row["completeness_status"]] = \
                out["by_status"].get(row["completeness_status"], 0) + 1
            try:
                meta = json.loads(row["provider_metadata"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            out["messages"] += int(meta.get("message_count") or 0)
            for seg in meta.get("segments") or []:
                out["segments"][seg] = out["segments"].get(seg, 0) + 1
            pid = meta.get("project_id")
            if pid:
                out["projects"][pid] = out["projects"].get(pid, 0) + 1
            for w in meta.get("warnings") or []:
                out["warnings"].append({"conversation_id": meta.get("conversation_id"), "warning": w})
        qconn = handles.connection("CONTROL")
        qrows = qconn.execute(
            "SELECT failure_code, reason, resumable FROM quarantine_record "
            "WHERE stage='acquire' ORDER BY created_at"
        ).fetchall()
        out["quarantined"] = [{"failure_code": r["failure_code"], "reason": r["reason"],
                               "resumable": bool(r["resumable"])} for r in qrows]
    disc = load_discovery(store_root)
    if disc:
        out["discovery"] = {"captured_at": disc.get("captured_at"),
                            "status": disc.get("status"),
                            "conversations": len(disc.get("items") or []),
                            "segments": disc.get("totals")}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    force_utf8()
    p = argparse.ArgumentParser(description="Back up ChatGPT conversations from the authenticated browser session")
    p.add_argument("--store-root", required=True, help="five-realm store root (EVIDENCE/DERIVED/CANON/VIEW/CONTROL)")
    p.add_argument("--session", default="gce-chatgpt-backup", help="WebBridge session name")
    p.add_argument("--max", type=int, default=0, help="capture at most N conversations (0 = all planned)")
    p.add_argument("--force", action="store_true", help="recapture even when update_time is unchanged")
    p.add_argument("--dry-run", action="store_true", help="enumerate + plan only; no browser capture")
    p.add_argument("--summarize", action="store_true", help="print stored state and exit")
    p.add_argument("--keep-tabs", action="store_true", help="do not close the WebBridge tab group")
    p.add_argument("--discover-only", action="store_true",
                   help="refresh the persisted enumeration and exit (no capture)")
    p.add_argument("--force-enumeration", action="store_true",
                   help="ignore the persisted enumeration and re-query the list endpoint")
    p.add_argument("--allow-partial-enumeration", action="store_true",
                   help="proceed even though one or more enumeration segments did not complete")
    args = p.parse_args(argv)

    store_root = os.path.abspath(args.store_root)
    os.makedirs(store_root, exist_ok=True)

    if args.summarize:
        print(json.dumps(summarize(store_root), indent=2, ensure_ascii=False))
        return 0

    transport = WebBridgeClient(Config(), session=args.session)
    adapter: Optional[ChatGPTAdapter] = None

    def ensure_adapter() -> Optional[ChatGPTAdapter]:
        """Create + fail-closed-probe the adapter, once, only when a browser is needed."""
        nonlocal adapter
        if adapter is not None:
            return adapter
        probe = transport.probe()
        if not probe.structure_ok:
            print(f"[stop] transport probe failed: {probe.signal} {probe.detail}", file=sys.stderr)
            return None
        adapter = ChatGPTAdapter(transport)
        aprobe = adapter.probe()
        if not aprobe.structure_ok:
            print(f"[stop] chatgpt probe failed (fail-closed): {aprobe.detail}", file=sys.stderr)
            return None
        return adapter

    cached = None if args.force_enumeration else load_discovery(store_root)
    if cached:
        print(f"[enum] reusing persisted enumeration: {len(cached['items'])} conversations "
              f"(captured {cached.get('captured_at')}); pass --force-enumeration to refresh",
              flush=True)
        items = cached["items"]
        enum_status = cached.get("status", OK)
        enum_errors = cached.get("errors") or []
        enum_totals = cached.get("totals") or {}
        if args.discover_only:
            return 0
    else:
        if ensure_adapter() is None:
            return 2 if transport.status() == "DOWN" else 3
        enum = adapter.enumerate("browser-session")
        items = enum.items
        enum_status = adapter.enumeration_status
        enum_errors = adapter.enumeration_errors
        enum_totals = adapter.enumeration_totals
        print(f"[enum] status={enum_status} {len(items)} conversations discovered "
              f"({len({i['id'] for i in items})} unique ids after dedupe)", flush=True)
        print(f"[enum] segments={json.dumps(enum_totals)}", flush=True)
        if enum_errors:
            print(f"[enum] ERRORS={json.dumps(enum_errors)}", file=sys.stderr, flush=True)
        if enum_status == OK:
            save_discovery(store_root, {
                "status": OK, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "totals": enum_totals, "errors": [], "items": items,
            })
            print(f"[enum] persisted to {_discovery_path(store_root)}", flush=True)
        if args.discover_only:
            transport.close()
            return 0 if enum_status == OK else 4
        if enum_status != OK and not args.allow_partial_enumeration:
            print(f"[stop] enumeration is {enum_status}; a capture against an incomplete "
                  f"conversation set is not trustworthy. Re-run, or pass "
                  f"--allow-partial-enumeration to proceed anyway.", file=sys.stderr)
            transport.close()
            return 4

    todo = plan(store_root, items, args.force)
    if args.max:
        todo = todo[: args.max]
    print(f"[plan] {len(todo)} to capture: "
          + json.dumps({r: sum(1 for t in todo if t['reason'] == r) for r in ('new', 'changed', 'force')}), flush=True)
    if args.dry_run:
        for t in todo:
            print(f"  - {t['id']}  {t['title']!r}  update={t.get('update_time')}  ({t['reason']})")
        if adapter is not None:
            transport.close()
        return 0
    if not todo:
        print("[done] nothing to capture; use --force to recapture")
        if adapter is not None:
            transport.close()
        return 0

    if ensure_adapter() is None:
        if adapter is not None:
            transport.close()
        return 2 if transport.status() == "DOWN" else 3
    # Seed the adapter from the enumeration (live or persisted) so a changed/forced
    # recapture still navigates with the right project-scoped URL. No re-enumeration.
    adapter.discovered = {it["id"]: it for it in items if it.get("id")}
    try:
        stored = ingest_observations(
            store_root=store_root, provider="chatgpt",
            conversation_ids=[t["id"] for t in todo], capture=adapter,
            adapter_version=adapter.version, scraper_version=adapter.version,
        )
    finally:
        if not args.keep_tabs:
            transport.close()

    summary = summarize(store_root)
    summary["enumeration"] = {"status": enum_status, "segments": enum_totals,
                              "errors": enum_errors, "discovered": len(items),
                              "source": "persisted" if cached else "live"}
    print(f"[run] stored {stored}/{len(todo)} new observations")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if enum_status == OK else 4


if __name__ == "__main__":
    sys.exit(main())
