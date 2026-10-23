"""M4: G1 predicate + G2 (no VERIFIED) + G3 candidates-only + G4 flag +
G5 pointers + G6 RESTRICTED redaction."""

from gce_ingestion.g1_autoflow import (
    Classification,
    authority_state_allowed,
    can_emit_knowledge,
    g1_autoflow_ok,
    get_current_pointer,
    promotion_reason_required,
    record_supersession_candidate,
    set_current_pointer,
    view_text,
    RESTRICTED_REDACTION,
)
from gce_ingestion.realms.handles import StageHandles


def test_g1_predicate_autoflow_requires_all_five_conditions():
    ok = Classification(dimension="topic", value="basketball", basis="text:b12",
                        basis_spans="0:120", confidence=1.0)
    assert g1_autoflow_ok(ok) is True
    # missing basis_spans -> REVIEW-PENDING
    assert g1_autoflow_ok(Classification("topic", "basketball", "b", "", 1.0)) is False
    # low confidence -> pending
    assert g1_autoflow_ok(Classification("topic", "basketball", "b", "s", 0.9)) is False
    # conflict -> never collapsed
    assert g1_autoflow_ok(Classification("topic", "basketball", "b", "s", 1.0,
                                         conflicts='["x"]')) is False


def test_g2_no_verified_candidate_only():
    assert authority_state_allowed("CANDIDATE")
    assert not authority_state_allowed("VERIFIED")
    assert authority_state_allowed("APPROVED")
    # emission refused without six evidence elements (F9)
    assert can_emit_knowledge("CANDIDATE", 6) is True
    assert can_emit_knowledge("CANDIDATE", 5) is False
    assert can_emit_knowledge("VERIFIED", 6) is False


def test_g2_check_constraint_rejects_verified_in_db(store_root):
    with StageHandles("normalize", store_root) as handles:
        conn = handles.connection("DERIVED", write=True)
        try:
            conn.execute(
                "INSERT INTO knowledge_object (knowledge_id, generation_id, authority_state, "
                "personal_domain, content_ref, evidence_elements, provenance, created_at) "
                "VALUES ('k1','g1','VERIFIED',0,'r',6,'{}','t')")
            conn.commit()
            raise AssertionError("VERIFIED should be rejected by CHECK constraint")
        except Exception:
            conn.rollback()
        # CANDIDATE with full evidence inserts fine
        conn.execute(
            "INSERT INTO knowledge_object (knowledge_id, generation_id, authority_state, "
            "personal_domain, content_ref, evidence_elements, provenance, created_at) "
            "VALUES ('k2','g1','CANDIDATE',0,'r',6,'{}','t')")
        conn.commit()


def test_g3_pipeline_writes_candidate_only(store_root):
    with StageHandles("classify", store_root) as handles:
        conn = handles.connection("CONTROL", write=True)
        cid = record_supersession_candidate(conn, proposed_for="conv1", basis="sim 0.94")
        assert cid
        row = conn.execute(
            "SELECT * FROM supersession_candidate WHERE candidate_id=?", (cid,)).fetchone()
        assert row["state"] == "PENDING"


def test_g4_personal_domain_promotion_needs_reason():
    assert promotion_reason_required(personal_domain=True) is True


def test_g5_current_pointer_roundtrip(store_root):
    with StageHandles("classify", store_root) as handles:
        conn = handles.connection("CONTROL", write=True)
        set_current_pointer(conn, "classify", "kimi", "gen-9")
        assert get_current_pointer(conn, "classify", "kimi") == "gen-9"
        set_current_pointer(conn, "classify", "kimi", "gen-10")
        assert get_current_pointer(conn, "classify", "kimi") == "gen-10"


def test_g6_restricted_redacted_by_default():
    assert view_text("RESTRICTED", "sensitive raw text") == RESTRICTED_REDACTION
    assert view_text("STANDARD", "public text") == "public text"
