"""M3: F1-F13 enum + QuarantineRecord + ResolutionEvent."""

from gce_ingestion.control.quarantine import record_quarantine, resolve_quarantine
from gce_ingestion.failures import FailureCode
from gce_ingestion.realms.handles import StageHandles


def test_failure_enum_all_codes():
    assert {c.value for c in FailureCode} == {f"F{i}" for i in range(1, 14)}


def test_retryable_and_stop_semantics():
    # never retry F1/F2/F7/F9/F13
    for f in (FailureCode.F1, FailureCode.F2, FailureCode.F7,
              FailureCode.F9, FailureCode.F13):
        assert not f.retryable
    # F8 ABSENT continue, F3 resumable retryable
    assert FailureCode.F8.retryable
    assert FailureCode.F3.retryable
    assert FailureCode.F1.stops_run and FailureCode.F2.stops_adapter


def test_quarantine_record_and_resolution(store_root):
    with StageHandles("acquire", store_root) as handles:
        conn = handles.connection("CONTROL", write=True)
        q = record_quarantine(conn, FailureCode.F3, reason="partial resume",
                              stage="acquire")
        assert q.resumable is True
        row = conn.execute(
            "SELECT * FROM quarantine_record WHERE quarantine_id=?", (q.quarantine_id,)
        ).fetchone()
        assert row["failure_code"] == "F3"
        ev = resolve_quarantine(conn, q.quarantine_id, resolved_to="RETRY")
        assert ev.quarantine_id == q.quarantine_id
        resolved = conn.execute(
            "SELECT * FROM resolution_event WHERE event_id=?", (ev.event_id,)
        ).fetchone()
        assert resolved["resolved_to"] == "RETRY"


def test_nonresumable_stop_recorded(store_root):
    with StageHandles("acquire", store_root) as handles:
        conn = handles.connection("CONTROL", write=True)
        q = record_quarantine(conn, FailureCode.F2, reason="adapter stop", stage="acquire")
        assert q.resumable is False
