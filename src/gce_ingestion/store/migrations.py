"""Store bootstrap: creates realm files, schema, INSERT-only triggers.

Realm files + CAS + INSERT-only triggers (manifest M1 / design C.1-C.4).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from ..realms import REALM_FILES, BLOB_ROOT_ALG

_IDENTITY_TYPES = ("STANDARD", "RESTRICTED")

DDL: dict[str, list[str]] = {}

DDL["EVIDENCE"] = [
    """
    CREATE TABLE IF NOT EXISTS raw_observation (
        observation_id              TEXT PRIMARY KEY,
        run_id                      TEXT NOT NULL,
        provider                    TEXT NOT NULL,
        account_ref                 TEXT,
        provider_conversation_id    TEXT,
        scrape_timestamp            TEXT NOT NULL,
        capture_path                TEXT NOT NULL
            CHECK (capture_path IN ('api','native-export','dom-snapshot','hybrid')),
        raw_payload_ref             TEXT NOT NULL,
        content_hash                TEXT NOT NULL,
        content_hash_alg            TEXT NOT NULL DEFAULT 'sha256',
        ordering_evidence           TEXT,          -- JSON
        provider_metadata           TEXT,          -- JSON
        scraper_version             TEXT,
        adapter_version             TEXT,
        source_pointer              TEXT,
        completeness_status         TEXT NOT NULL DEFAULT 'COMPLETE'
            CHECK (completeness_status IN ('COMPLETE','PARTIAL')),
        partial_reason              TEXT,
        predecessor_observation_id  TEXT,
        handling_domain             TEXT NOT NULL DEFAULT 'STANDARD'
            CHECK (handling_domain IN ('STANDARD','RESTRICTED'))
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS raw_observation_no_update
    BEFORE UPDATE ON raw_observation
    BEGIN
        SELECT RAISE(ABORT, 'raw_observation is INSERT-only (no UPDATE)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS raw_observation_no_delete
    BEFORE DELETE ON raw_observation
    BEGIN
        SELECT RAISE(ABORT, 'raw_observation is INSERT-only (no DELETE)');
    END
    """,
]

DDL["CONTROL"] = [
    """
    CREATE TABLE IF NOT EXISTS acquisition_run (
        run_id          TEXT PRIMARY KEY,
        provider        TEXT NOT NULL,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        status          TEXT NOT NULL DEFAULT 'RUNNING',
        note            TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoint (
        run_id         TEXT NOT NULL,
        provider       TEXT NOT NULL,
        token          TEXT NOT NULL,          -- last provider_conv_id + run_id envelope
        created_at     TEXT NOT NULL,
        PRIMARY KEY (run_id, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantine_record (
        quarantine_id   TEXT PRIMARY KEY,
        observation_id  TEXT,
        failure_code    TEXT NOT NULL,          -- F1..F13
        reason          TEXT,
        stage           TEXT,
        created_at      TEXT NOT NULL,
        resumable       INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS resolution_event (
        event_id        TEXT PRIMARY KEY,
        quarantine_id   TEXT,
        resolved_to     TEXT NOT NULL,
        basis           TEXT,
        resolved_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_item (
        review_id       TEXT PRIMARY KEY,
        scope           TEXT,
        scope_id        TEXT,
        lane            TEXT,
        state           TEXT NOT NULL DEFAULT 'PENDING',
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_record (
        audit_id        TEXT PRIMARY KEY,
        actor           TEXT NOT NULL,
        action          TEXT NOT NULL,
        target_realm    TEXT NOT NULL,
        detail          TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS current_pointer (
        stage           TEXT NOT NULL,
        scope           TEXT NOT NULL,
        generation_id   TEXT,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (stage, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generation (
        generation_id           TEXT PRIMARY KEY,
        stage                   TEXT NOT NULL,
        tool_version            TEXT,
        input_generation_ids    TEXT,          -- JSON list
        created_at              TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS merge_split_event (
        event_id    TEXT PRIMARY KEY,
        basis       TEXT NOT NULL,             -- TIER1 / TIER2 / ...
        detail      TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS supersession_candidate (
        candidate_id        TEXT PRIMARY KEY,
        proposed_for        TEXT,
        basis               TEXT,
        state               TEXT NOT NULL DEFAULT 'PENDING',
        created_at          TEXT NOT NULL
    )
    """,
]

DDL["DERIVED"] = [
    # Normalized / classified / knowledge generations. INSERT-only.
    """
    CREATE TABLE IF NOT EXISTS normalized_view (
        observation_id  TEXT NOT NULL,
        generation_id   TEXT NOT NULL,
        normalized_text TEXT,
        provenance      TEXT,           -- JSON; must be non-null to emit (F9)
        created_at      TEXT NOT NULL,
        PRIMARY KEY (observation_id, generation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classification_set (
        generation_id   TEXT NOT NULL,
        dimension       TEXT NOT NULL,
        value           TEXT NOT NULL,
        basis           TEXT,
        basis_spans     TEXT,
        confidence      REAL,
        conflicts       TEXT,
        review_state    TEXT NOT NULL DEFAULT 'REVIEW-PENDING',
        review_lane     TEXT,
        classifier_version TEXT,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (generation_id, dimension, value)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_object (
        knowledge_id    TEXT PRIMARY KEY,
        generation_id   TEXT NOT NULL,
        authority_state TEXT NOT NULL
            CHECK (authority_state NOT IN ('VERIFIED')),
        personal_domain INTEGER NOT NULL DEFAULT 0,
        content_ref     TEXT,
        evidence_elements INTEGER NOT NULL DEFAULT 0,
        provenance      TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generation_diff (
        diff_id         TEXT PRIMARY KEY,
        generation_id   TEXT,
        detail          TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contradiction_object (
        contradiction_id TEXT PRIMARY KEY,
        generation_id   TEXT,
        detail          TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    # INSERT-only triggers across derived tables.
    """
    CREATE TRIGGER IF NOT EXISTS derived_no_update
    BEFORE UPDATE ON normalized_view
    BEGIN SELECT RAISE(ABORT,'normalized_view INSERT-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS derived_no_delete
    BEFORE DELETE ON normalized_view
    BEGIN SELECT RAISE(ABORT,'normalized_view INSERT-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS class_no_update
    BEFORE UPDATE ON classification_set
    BEGIN SELECT RAISE(ABORT,'classification_set INSERT-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS class_no_delete
    BEFORE DELETE ON classification_set
    BEGIN SELECT RAISE(ABORT,'classification_set INSERT-only'); END
    """,
]

DDL["VIEW"] = [
    """
    CREATE TABLE IF NOT EXISTS artifact (
        artifact_id     TEXT PRIMARY KEY,
        kind            TEXT,
        title           TEXT,
        safe_folder_name TEXT,
        content_ref     TEXT,
        created_at      TEXT NOT NULL
    )
    """
]

DDL["CANON"] = [
    """
    CREATE TABLE IF NOT EXISTS canon_record (
        record_id        TEXT PRIMARY KEY,
        conversation_id  TEXT NOT NULL,
        authority_state  TEXT NOT NULL,
        content          TEXT,
        created_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS canon_history (
        history_id      TEXT PRIMARY KEY,
        record_id       TEXT,
        changed_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS supersession_event (
        event_id        TEXT PRIMARY KEY,
        prior_pointer   TEXT,
        new_pointer     TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS canon_current_pointer (
        conversation_id TEXT PRIMARY KEY,
        record_id       TEXT
    )
    """,
]


def realm_db_path(store_root: str, realm: str) -> str:
    rel_dir, filename = REALM_FILES[realm]
    return os.path.join(store_root, rel_dir, filename)


def connect_realm(store_root: str, realm: str, read_only: bool = False) -> sqlite3.Connection:
    """Open a realm sqlite connection (default read-write).

    read_only uses URI mode=ro so any accidental write fails at the engine.
    """
    path = realm_db_path(store_root, realm)
    if read_only:
        uri = f"file:{path.replace(os.sep, '/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA journal_mode={os.environ.get('GCE_SQLITE_JOURNAL','WAL')}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_store(store_root: str) -> dict[str, str]:
    """Create all realm dirs/files + schema. Returns realm->db path map."""
    os.makedirs(store_root, exist_ok=True)
    os.makedirs(os.path.join(store_root, "EVIDENCE", "blobs", BLOB_ROOT_ALG), exist_ok=True)
    paths: dict[str, str] = {}
    for realm in REALM_FILES:
        path = realm_db_path(store_root, realm)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = connect_realm(store_root, realm)
        try:
            for stmt in DDL[realm]:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        paths[realm] = path
    return paths
