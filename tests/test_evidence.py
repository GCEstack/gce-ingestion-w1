"""M2: RawObservation insert + blob put/get + predecessor chain + INSERT-only."""

import pytest

from gce_ingestion.evidence import (
    RawObservation,
    get_payload,
    latest_predecessor,
    store_observation,
)
from gce_ingestion.realms.handles import StageHandles
from gce_ingestion.store import cas


def test_insert_and_blob_roundtrip(store_root):
    payload = '<div data-conversation="c1">hello</div>'.encode()
    with StageHandles("acquire", store_root) as handles:
        oid = store_observation(
            handles,
            RawObservation(run_id="r1", provider="kimi",
                           provider_conversation_id="c1", capture_path="dom-snapshot"),
            payload,
        )
        assert oid
        digest = handles.connection("EVIDENCE").execute(
            "SELECT content_hash FROM raw_observation WHERE observation_id=?", (oid,)
        ).fetchone()["content_hash"]
        assert cas.blob_exists(store_root, digest)
        assert get_payload(handles, oid) == payload


def test_content_addressed_dedup_same_blob_new_observation_id(store_root):
    payload = b"identical payload"
    with StageHandles("acquire", store_root) as handles:
        a = store_observation(handles, RawObservation(
            run_id="r1", provider="kimi", provider_conversation_id="c1",
            capture_path="dom-snapshot"), payload)
        b = store_observation(handles, RawObservation(
            run_id="r1", provider="kimi", provider_conversation_id="c1",
            capture_path="dom-snapshot"), payload)
        assert a != b  # new observation every scrape (P3)
        conn = handles.connection("EVIDENCE")
        ha = conn.execute("SELECT content_hash FROM raw_observation WHERE observation_id=?",
                          (a,)).fetchone()
        hb = conn.execute("SELECT content_hash FROM raw_observation WHERE observation_id=?",
                          (b,)).fetchone()
        assert ha["content_hash"] == hb["content_hash"]  # shared blob


def test_predecessor_chain(store_root):
    with StageHandles("acquire", store_root) as handles:
        first = store_observation(handles, RawObservation(
            run_id="r1", provider="kimi", provider_conversation_id="c1",
            capture_path="dom-snapshot"), b"v1")
        assert latest_predecessor(handles, "kimi", "c1") == first
        second = store_observation(handles, RawObservation(
            run_id="r1", provider="kimi", provider_conversation_id="c1",
            capture_path="dom-snapshot", predecessor_observation_id=first), b"v2")
        row = handles.connection("EVIDENCE").execute(
            "SELECT predecessor_observation_id FROM raw_observation WHERE observation_id=?",
            (second,)).fetchone()
        assert row["predecessor_observation_id"] == first


def test_raw_observation_is_insert_only(store_root):
    with StageHandles("acquire", store_root) as handles:
        oid = store_observation(handles, RawObservation(
            run_id="r1", provider="kimi", provider_conversation_id="c1",
            capture_path="dom-snapshot"), b"x")
        conn = handles.connection("EVIDENCE", write=True)
        with pytest.raises(Exception):
            conn.execute("UPDATE raw_observation SET provider='other' WHERE observation_id=?",
                         (oid,))
        with pytest.raises(Exception):
            conn.execute("DELETE FROM raw_observation WHERE observation_id=?", (oid,))
