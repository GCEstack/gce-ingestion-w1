"""RawObservation insert + blob put/get + predecessor chain (manifest M2)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .realms.handles import StageHandles
from .store import cas


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RawObservation:
    run_id: str
    provider: str
    provider_conversation_id: Optional[str] = None
    capture_path: str = "dom-snapshot"
    observation_id: Optional[str] = None
    raw_payload_ref: Optional[str] = None
    content_hash: Optional[str] = None
    content_hash_alg: str = "sha256"
    account_ref: Optional[str] = None
    scrape_timestamp: Optional[str] = None
    ordering_evidence: Optional[dict] = None
    provider_metadata: Optional[dict] = None
    scraper_version: Optional[str] = None
    adapter_version: Optional[str] = None
    source_pointer: Optional[str] = None
    completeness_status: str = "COMPLETE"
    partial_reason: Optional[str] = None
    predecessor_observation_id: Optional[str] = None
    handling_domain: str = "STANDARD"


def store_observation(handles: StageHandles, obs: RawObservation, payload: bytes) -> str:
    """Append a RawObservation row + content-addressed blob.

    Returns observation_id. Identical payloads share a blob; every scrape still
    gets a NEW observation_id (no in-place mutation, P3).
    """
    digest = cas.put_blob(handles.store_root, payload)
    obs.observation_id = obs.observation_id or uuid.uuid4().hex
    obs.raw_payload_ref = cas._path_for(handles.store_root, digest)
    obs.content_hash = digest
    obs.content_hash_alg = "sha256"
    obs.scrape_timestamp = obs.scrape_timestamp or _now()
    conn = handles.connection("EVIDENCE", write=True)
    conn.execute(
        "INSERT INTO raw_observation ("
        " observation_id, run_id, provider, account_ref, provider_conversation_id,"
        " scrape_timestamp, capture_path, raw_payload_ref, content_hash, content_hash_alg,"
        " ordering_evidence, provider_metadata, scraper_version, adapter_version, source_pointer,"
        " completeness_status, partial_reason, predecessor_observation_id, handling_domain"
        ") VALUES ("
        " ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            obs.observation_id, obs.run_id, obs.provider, obs.account_ref,
            obs.provider_conversation_id, obs.scrape_timestamp, obs.capture_path,
            obs.raw_payload_ref, obs.content_hash, obs.content_hash_alg,
            json.dumps(obs.ordering_evidence) if obs.ordering_evidence else None,
            json.dumps(obs.provider_metadata) if obs.provider_metadata else None,
            obs.scraper_version, obs.adapter_version, obs.source_pointer,
            obs.completeness_status, obs.partial_reason,
            obs.predecessor_observation_id, obs.handling_domain,
        ),
    )
    conn.commit()
    return obs.observation_id


def latest_predecessor(handles: StageHandles, provider: str,
                       provider_conversation_id: str) -> Optional[str]:
    """Return the most recent observation_id for the same conversation (predecessor chain)."""
    conn = handles.connection("EVIDENCE")
    row = conn.execute(
        "SELECT observation_id FROM raw_observation "
        "WHERE provider=? AND provider_conversation_id=? "
        "ORDER BY scrape_timestamp DESC LIMIT 1",
        (provider, provider_conversation_id),
    ).fetchone()
    return row["observation_id"] if row else None


def get_payload(handles: StageHandles, observation_id: str) -> bytes:
    conn = handles.connection("EVIDENCE")
    row = conn.execute(
        "SELECT content_hash FROM raw_observation WHERE observation_id=?",
        (observation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(observation_id)
    return cas.get_blob(handles.store_root, row["content_hash"])


def latest_observation(handles: StageHandles, provider: str,
                       provider_conversation_id: str) -> Optional[dict]:
    """Newest stored observation for a conversation, as a plain dict.

    ``provider_metadata`` is returned already decoded. This is the incremental-backup
    read: compare its ``update_time`` against the provider's current one to decide
    whether a recapture is warranted. Never mutates and never deletes.
    """
    conn = handles.connection("EVIDENCE")
    row = conn.execute(
        "SELECT observation_id, scrape_timestamp, content_hash, capture_path, "
        "       completeness_status, partial_reason, provider_metadata "
        "FROM raw_observation WHERE provider=? AND provider_conversation_id=? "
        "ORDER BY scrape_timestamp DESC, rowid DESC LIMIT 1",
        (provider, provider_conversation_id),
    ).fetchone()
    if row is None:
        return None
    meta = None
    if row["provider_metadata"]:
        try:
            meta = json.loads(row["provider_metadata"])
        except (TypeError, ValueError):
            meta = None
    return {
        "observation_id": row["observation_id"],
        "scrape_timestamp": row["scrape_timestamp"],
        "content_hash": row["content_hash"],
        "capture_path": row["capture_path"],
        "completeness_status": row["completeness_status"],
        "partial_reason": row["partial_reason"],
        "provider_metadata": meta or {},
    }
