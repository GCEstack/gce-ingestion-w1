"""M1: realm files + CAS + INSERT-only triggers + capability-handle matrix.

Every pipeline stage may write only its capability realm set; CANON write is
forbidden for all of acquire/normalize/classify/extract/materialize and raises
RealmViolation + writes a CONTROL AuditRecord.
"""

import sqlite3

import pytest

from gce_ingestion.realms import RealmViolation, STAGE_WRITE_REALMS
from gce_ingestion.realms.handles import StageHandles
from gce_ingestion.store.migrations import ensure_store


def test_store_creates_all_five_realm_files(store_root):
    paths = ensure_store(store_root)
    assert set(paths.keys()) == {"CONTROL", "EVIDENCE", "DERIVED", "VIEW", "CANON"}
    for realm in paths:
        conn = sqlite3.connect(paths[realm])
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        assert tables, f"{realm} has no tables"


@pytest.mark.parametrize("stage", sorted(STAGE_WRITE_REALMS.keys()))
def test_canon_write_forbidden_for_all_pipeline_stages(store_root, stage):
    with StageHandles(stage, store_root) as handles:
        # read of CANON is fine
        canon_ro = handles.canon()
        assert canon_ro is not None
        # CANON write raises RealmViolation
        with pytest.raises(RealmViolation):
            handles.connection("CANON", write=True)
    # an audit record for the denial was written to CONTROL
    with StageHandles("acquire", store_root) as chk:
        audit = chk.connection("CONTROL").execute(
            "SELECT actor, action, target_realm FROM audit_record WHERE target_realm='CANON'"
        ).fetchall()
        assert audit, "no RealmViolation audit record written to CONTROL"


def test_stage_write_matrix_respected(store_root):
    # acquire MAY open EVIDENCE + CONTROL for write without raising
    with StageHandles("acquire", store_root) as h:
        h.connection("EVIDENCE", write=True)
        h.connection("CONTROL", write=True)
    # acquire must NOT write DERIVED / VIEW / CANON
    for forbidden in ("DERIVED", "VIEW", "CANON"):
        with StageHandles("acquire", store_root) as h:
            with pytest.raises(RealmViolation):
                h.connection(forbidden, write=True)


def test_materialize_cannot_write_derived_or_evidence(store_root):
    with StageHandles("materialize", store_root) as h:
        with pytest.raises(RealmViolation):
            h.connection("DERIVED", write=True)
        with pytest.raises(RealmViolation):
            h.connection("EVIDENCE", write=True)
        # but may write VIEW
        h.connection("VIEW", write=True).execute(
            "INSERT INTO artifact (artifact_id, kind, created_at) VALUES ('a1','x','t')")
        h.connection("VIEW").commit()


def test_realm_violation_records_control_audit(store_root):
    with StageHandles("normalize", store_root) as h:
        with pytest.raises(RealmViolation):
            h.connection("CANON", write=True)
        rows = h.connection("CONTROL").execute(
            "SELECT * FROM audit_record WHERE action='DENIED_REALM_WRITE'").fetchall()
        assert rows
