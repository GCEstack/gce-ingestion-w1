"""coda_reconcile — the ONLY writer to CANON (design C.2, G3, manifest M10).

Isolated as its own module + entrypoint. It must NOT be importable from the
pipeline package graph (stages/realms/store/adapters/transport/control never
import this). Pipeline stages write supersession_candidate (CONTROL) only;
supersession_event / canon tables live here.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from .store.migrations import connect_realm, ensure_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_canon_write(store_root: str) -> sqlite3.Connection:
    """Open CANON read-write. Only reachable via this module/entrypoint."""
    ensure_store(store_root)
    return connect_realm(store_root, "CANON", read_only=False)


def write_canon_record(conn: sqlite3.Connection, conversation_id: str,
                       authority_state: str, content: str) -> str:
    if authority_state == "VERIFIED":
        raise ValueError("VERIFIED cannot be written by reconcile automation (G2)")
    record_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO canon_record (record_id, conversation_id, authority_state, content, created_at) "
        "VALUES (?,?,?,?,?)",
        (record_id, conversation_id, authority_state, content, _now()),
    )
    conn.commit()
    return record_id


def main(argv: Optional[list] = None) -> int:  # pragma: no cover
    import sys

    from .config import force_utf8, store_root_required

    force_utf8()
    store_root = store_root_required()
    conn = open_canon_write(store_root)
    print(f"coda_reconcile: CANON writable handle opened at {store_root}", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
