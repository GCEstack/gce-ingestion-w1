"""M7 + M8 + end-to-end acquire: checkpoint/resume + UTF-8 title round-trip."""

import json

from gce_ingestion.control.run import (
    load_checkpoint,
    save_checkpoint,
    start_run,
)
from gce_ingestion.evidence import get_payload
from gce_ingestion.g1_autoflow import view_text
from gce_ingestion.realms.handles import StageHandles
from gce_ingestion.stages.acquire import ingest_observations
from gce_ingestion.transport.fake import FakeTransport
from gce_ingestion.adapters.kimi import KimiAdapter


def test_checkpoint_resume_token_roundtrip(store_root):
    with StageHandles("acquire", store_root) as handles:
        conn = handles.connection("CONTROL", write=True)
        run_id = start_run(conn, provider="kimi")
        save_checkpoint(conn, run_id, "kimi", "conv-007")
        assert load_checkpoint(conn, run_id, "kimi") == "conv-007"


def test_acquire_end_to_end_resumes_from_checkpoint(store_root, synthetic_kimi_fixture):
    with open(synthetic_kimi_fixture, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    transport = FakeTransport(conversations=data["conversations"])
    adapter = KimiAdapter(transport)

    # First pass ingests all.
    count1 = ingest_observations(store_root, "kimi",
                                 list(data["conversations"].keys()), adapter)
    assert count1 == len(data["conversations"])

    # Record a checkpoint at the last conversation then resume: nothing new.
    with StageHandles("acquire", store_root) as handles:
        control = handles.connection("CONTROL", write=True)
        run_id = start_run(control, provider="kimi")
        last = list(data["conversations"].keys())[-1]
        save_checkpoint(control, run_id, "kimi", last)
    count2 = ingest_observations(store_root, "kimi",
                                 list(data["conversations"].keys()), adapter, run_id=run_id)
    # resume skips already-checkpointed conversations
    assert count2 == 0


def test_utf8_title_round_trip_via_blob_and_view(store_root):
    title = "Conversazione di prova — UTF-8 ✓ 你好"
    with StageHandles("acquire", store_root) as handles:
        from gce_ingestion.evidence import RawObservation, store_observation
        oid = store_observation(handles, RawObservation(
            run_id="r1", provider="kimi", provider_conversation_id="c-utf8",
            capture_path="dom-snapshot"), title.encode("utf-8"))
        payload = get_payload(handles, oid)
        assert payload.decode("utf-8") == title  # bytes survive blob -> observation
        assert view_text("STANDARD", payload.decode("utf-8")) == title
