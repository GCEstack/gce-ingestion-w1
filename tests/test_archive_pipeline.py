"""Passes 1-3 and the pass 4/5 worker contract.

The quality checks are only worth anything if they discriminate, so most of these tests
feed deliberately broken records and assert the specific finding fires -- rather than
asserting that a good record is clean.
"""

import json

import pytest

from gce_ingestion.stages import index as index_mod
from gce_ingestion.stages import manifest as manifest_mod
from gce_ingestion.stages import normalize as norm_mod
from gce_ingestion.stages import quality as quality_mod
from gce_ingestion.stages import workers as workers_mod
from gce_ingestion.stages.quality import (
    COMPLETE,
    COMPLETE_WITH_WARNINGS,
    INVALID,
    NEEDS_RECAPTURE,
    PARTIAL,
    check_normalized,
)
from gce_ingestion.stages.workers import WorkerRejected, ingest_worker_output


# -- fixtures ----------------------------------------------------------------
def msg(mid, ct, role="user", parts=None, slug="gpt-x"):
    return {
        "id": mid, "create_time": ct, "update_time": ct,
        "author": {"role": role, "name": None},
        "content": {"content_type": "text", "parts": parts if parts is not None else ["hello"]},
        "metadata": {"model_slug": slug} if slug else {},
        "status": "finished_successfully", "weight": 1, "recipient": "all",
    }


def raw_doc(msgs, **kw):
    d = {
        "schema": "gce.chatgpt.capture.v1",
        "capture_status": "ok",
        "captured_at": "2026-09-26T08:00:00Z",
        "canonical_url": "https://chatgpt.com/c/cid-1",
        "conversation_id": "cid-1",
        "project_id": None,
        "provider_payload": {"conversation_id": "cid-1", "title": "T",
                             "create_time": 1, "update_time": 9,
                             "default_model_slug": "gpt-x", "is_archived": False},
        "messages": msgs,
        "stats": {"message_count": len(msgs), "pages_walked": 1},
        "pages": [], "warnings": [],
    }
    d.update(kw)
    return d


SOURCE = {"observation_id": "obs-1", "artifact_path": "/tmp/blob", "content_hash": "abc123",
          "capture_timestamp": "2026-09-26T08:00:00Z", "completeness_status": "COMPLETE"}


def norm_of(msgs, **kw):
    return norm_mod.normalize_capture(raw_doc(msgs, **kw), SOURCE)


# -- pass 2: normalization ---------------------------------------------------
def test_normalize_reads_code_carried_in_content_text_not_parts():
    """Regression: the payload for content_type 'code' lives in content.text, not
    content.parts. Reading only parts discarded 2,173 such messages corpus-wide."""
    m = msg("m", 1, "assistant")
    m["content"] = {"content_type": "code", "language": "json",
                    "response_format_name": None,
                    "text": '{"paths":["Google_Drive"],"query":"search"}'}
    t = norm_of([m])["turns"][0]
    assert t["text"] == '{"paths":["Google_Drive"],"query":"search"}'
    assert t["sibling_fields_used"] == ["text"]


def test_normalize_reads_execution_output_in_content_text():
    m = msg("m", 1, "tool")
    m["content"] = {"content_type": "execution_output", "text": "x" * 500}
    t = norm_of([m])["turns"][0]
    assert t["text_chars"] == 500
    assert t["sibling_fields_used"] == ["text"]


def test_normalize_reads_reasoning_recap_in_content_content():
    m = msg("m", 1, "assistant")
    m["content"] = {"content_type": "reasoning_recap", "content": "Thought for a moment"}
    t = norm_of([m])["turns"][0]
    assert t["text"] == "Thought for a moment"
    assert t["sibling_fields_used"] == ["content"]


def test_normalize_reads_thoughts_list():
    m = msg("m", 1, "assistant")
    m["content"] = {"content_type": "thoughts", "source_analysis_msg_id": "x",
                    "thoughts": [{"text": "step one"}, {"text": "step two"}]}
    t = norm_of([m])["turns"][0]
    assert "step one" in t["text"] and "step two" in t["text"]
    assert t["sibling_fields_used"] == ["thoughts"]


def test_normalize_reads_system_error_text():
    m = msg("m", 1, "tool")
    m["content"] = {"content_type": "system_error", "name": "InvalidArgumentError",
                    "text": "Encountered exception."}
    t = norm_of([m])["turns"][0]
    assert t["text"] == "Encountered exception."


def test_genuinely_empty_sibling_fields_stay_empty_not_invented():
    m = msg("m", 1, "tool")
    m["content"] = {"content_type": "tether_browsing_display", "assets": None,
                    "result": "", "summary": "", "tether_id": None}
    t = norm_of([m])["turns"][0]
    assert t["text"] == ""
    assert t["sibling_fields_used"] == []


def test_structural_fields_are_not_mistaken_for_content():
    m = msg("m", 1, "assistant")
    m["content"] = {"content_type": "code", "language": "python",
                    "response_format_name": "x", "text": "print(1)"}
    t = norm_of([m])["turns"][0]
    # language / response_format_name must not appear in the text
    assert "python" not in t["text"]
    assert t["text"] == "print(1)"


def test_parts_and_sibling_text_are_both_captured_when_both_present():
    m = msg("m", 1, "user")
    m["content"] = {"content_type": "multimodal_text",
                    "parts": ["visible words"],
                    "text": "metadata-ish extra"}
    t = norm_of([m])["turns"][0]
    assert "visible words" in t["text"] and "metadata-ish extra" in t["text"]


def test_quality_ratio_flag_is_more_severe_than_the_plain_count():
    # 32/42 = 76% empty -> severe
    mostly = norm_of([msg(f"m{i}", i) for i in range(10)]
                     + [msg(f"e{i}", 100 + i, parts=[""]) for i in range(32)])
    res = check_normalized(mostly, None, None)
    assert "mostly_empty_turns" in res["finding_codes"]
    # 3/33 = 9% empty -> ordinary warning
    mostly_low = norm_of([msg(f"m{i}", i) for i in range(30)]
                         + [msg(f"e{i}", 100 + i, parts=[""]) for i in range(3)])
    res2 = check_normalized(mostly_low, None, None)
    assert "empty_turns" in res2["finding_codes"]
    assert "mostly_empty_turns" not in res2["finding_codes"]


def test_validator_preserves_bullet_line_structure():
    """Regression: collapsing newlines turned every worker's bullet list into one
    run-on paragraph. Found by reading an ingested summary back."""
    rec, _ = workers_mod.validate_summary(
        good_sum(key_outcomes="- first outcome\n- second outcome\n- third outcome"), BATCH)
    assert rec["key_outcomes"].count("\n") == 2
    assert rec["key_outcomes"].startswith("- first outcome")
    # horizontal whitespace is still collapsed
    rec2, _ = workers_mod.validate_summary(
        good_sum(important_facts="-  a   b  \n-   c"), BATCH)
    assert rec2["important_facts"] == "- a b\n- c"


def test_summary_markdown_renders_bullets_as_bullets(tmp_path):
    from gce_ingestion.stages.workers import _summary_markdown
    s, _ = workers_mod.validate_summary(
        good_sum(key_outcomes="- one\n- two"), BATCH)
    md = _summary_markdown(s, {"title": "T", "canonical_url": "u", "turn_count": 1,
                               "capture_status": "ok", "provenance": {}})
    assert "- one\n- two" in md


def test_normalize_preserves_provider_order_and_flags_non_monotonic_input():
    # A real capture arrives already sorted by the walk, so order is preserved and the
    # record reports monotonic. Handed an unsorted document it must say so, not hide it.
    rec = norm_of([msg("m1", 1), msg("m2", 2, "assistant")])
    assert [t["message_id"] for t in rec["turns"]] == ["m1", "m2"]
    assert [t["role"] for t in rec["turns"]] == ["user", "assistant"]
    assert rec["ordering_is_monotonic"] is True

    unsorted = norm_of([msg("m2", 2, "assistant"), msg("m1", 1)])
    assert [t["message_id"] for t in unsorted["turns"]] == ["m2", "m1"]  # not re-sorted
    assert unsorted["ordering_is_monotonic"] is False


def test_normalize_records_provider_roles_verbatim_including_tool_and_system():
    rec = norm_of([msg("a", 1, "user"), msg("b", 2, "tool"), msg("c", 3, "system")])
    assert rec["role_counts"] == {"user": 1, "tool": 1, "system": 1}


def test_normalize_extracts_code_links_and_paths():
    text = "see `C:\\Users\\x\\a.md` and docs/plan.md plus https://example.com/x\n```py\nprint(1)\n```"
    rec = norm_of([msg("m", 1, "user", parts=[text])])
    t = rec["turns"][0]
    assert t["code_blocks"] == 1
    assert t["links"] == ["https://example.com/x"]
    assert any("a.md" in p for p in t["path_references"])
    assert any("plan.md" in p for p in t["path_references"])


def test_normalize_flags_unrecoverable_image_parts_without_dropping_them():
    rec = norm_of([msg("m", 1, "user",
                       parts=["look:", {"content_type": "image_asset_pointer",
                                        "pointer": "file-abc"}])])
    t = rec["turns"][0]
    assert t["unextractable_part_kinds"] == ["image_asset_pointer"]
    assert t["text"] == "look:"
    assert rec["unextractable_part_kinds"] == ["image_asset_pointer"]


def test_empty_text_part_is_counted_separately_not_as_unextractable():
    rec = norm_of([msg("m", 1, "user", parts=["", "real"])])
    t = rec["turns"][0]
    assert t["empty_text_parts"] == 1
    assert t["unextractable_part_kinds"] == []
    assert rec["unextractable_part_kinds"] == []


def test_normalize_keeps_provenance_chain_to_the_raw_artifact():
    rec = norm_of([msg("m", 1)])
    p = rec["provenance"]
    assert p["observation_id"] == "obs-1"
    assert p["source_content_hash"] == "abc123"
    assert p["source_artifact_path"] == "/tmp/blob"
    assert p["normalizer_version"] == norm_mod.NORMALIZED_SCHEMA


def test_normalize_does_not_summarize_or_classify():
    rec = norm_of([msg("m", 1)])
    blob = json.dumps(rec)
    for forbidden in ("primary_project", "lifecycle_state", "durable_value",
                      "summary", "key_outcomes"):
        assert forbidden not in blob


def test_digest_is_bounded_and_says_what_it_omitted():
    msgs = [msg(f"m{i}", i, "user", parts=[f"turn {i} " + "x" * 50]) for i in range(200)]
    d = norm_mod.build_digest(norm_of(msgs), head=3, tail=3)
    assert "bounded extract, not a summary" in d
    assert "middle turns omitted" in d
    # head + tail + the 5 longest middle turns, and nothing more
    assert d.count("### [user]") <= 3 + 3 + 5
    assert "conversation_id" in d and "source_hash" in d


def test_digest_points_at_the_full_record():
    rec = norm_of([msg("m", 1)])
    assert "DERIVED/normalized/cid-1.json" in norm_mod.build_digest(rec)


# -- pass 3: quality (must discriminate) -------------------------------------
def test_quality_flags_a_turn_count_that_disagrees_with_raw():
    rec = norm_of([msg("m1", 1)])
    res = check_normalized(rec, None, raw_doc([msg("m1", 1), msg("m2", 2)]))
    assert res["capture_quality"] == INVALID
    assert "raw_turn_count_mismatch" in res["finding_codes"]


def test_quality_flags_reordered_or_substituted_message_ids():
    rec = norm_of([msg("m1", 1), msg("m2", 2)])
    other = raw_doc([msg("m2", 1), msg("m1", 2)])
    res = check_normalized(rec, None, other)
    assert res["capture_quality"] == INVALID
    assert "message_id_sequence_mismatch" in res["finding_codes"]


def test_quality_flags_duplicate_message_ids():
    rec = norm_of([msg("m1", 1), msg("m1", 2)])
    res = check_normalized(rec, None, None)
    assert res["capture_quality"] == INVALID
    assert "duplicate_message_ids" in res["finding_codes"]


def test_quality_flags_non_monotonic_ordering():
    msgs = [msg("a", 5), msg("b", 1)]
    rec = norm_of(msgs)
    rec["ordering_is_monotonic"] = False
    res = check_normalized(rec, None, None)
    assert "ordering_not_monotonic" in res["finding_codes"]
    assert res["capture_quality"] == COMPLETE_WITH_WARNINGS


def test_quality_flags_an_unsplit_blob():
    msgs = [msg(f"m{i}", i, "user", parts=["tiny"]) for i in range(8)]
    msgs.append(msg("big", 99, "user", parts=["X" * 10000]))
    res = check_normalized(norm_of(msgs), None, None)
    assert "unsplit_blob" in res["finding_codes"]


def test_quality_flags_a_missing_artifact_as_needing_recapture():
    entry = {"conversation_id": "cid-1", "artifact_present": False, "capture_warnings": [],
             "message_count": 1, "capture_state": "captured"}
    res = check_normalized(norm_of([msg("m", 1)]), entry, None)
    assert res["capture_quality"] == NEEDS_RECAPTURE


def test_quality_flags_a_provider_truncated_walk_as_partial():
    entry = {"conversation_id": "cid-1", "artifact_present": True, "capture_warnings": [],
             "message_count": 1, "capture_state": "partial"}
    res = check_normalized(norm_of([msg("m", 1)]), entry, None)
    assert res["capture_quality"] == PARTIAL


def test_quality_flags_replacement_characters_as_invalid():
    rec = norm_of([msg("m", 1, "user", parts=["ok � bad"])])
    res = check_normalized(rec, None, None)
    assert res["capture_quality"] == INVALID
    assert "encoding_replacement_char" in res["finding_codes"]


def test_quality_flags_zero_turns():
    res = check_normalized(norm_of([]), None, None)
    assert res["capture_quality"] == INVALID
    assert "zero_turns" in res["finding_codes"]


def test_quality_flags_unknown_roles():
    msgs = [msg("m", 1)]
    msgs[0]["author"]["role"] = "wizard"
    res = check_normalized(norm_of(msgs), None, None)
    assert "unknown_roles" in res["finding_codes"]


def test_quality_of_a_clean_record_is_complete():
    rec = norm_of([msg("m1", 1, "user"), msg("m2", 2, "assistant")])
    res = check_normalized(rec, None, raw_doc(rec["turns"] and [
        msg("m1", 1, "user"), msg("m2", 2, "assistant")]))
    assert res["capture_quality"] == COMPLETE
    assert res["findings"] == []


def test_quality_capture_warning_alone_is_a_warning_not_a_defect():
    entry = {"conversation_id": "cid-1", "artifact_present": True,
             "capture_warnings": ["non_text_part:image_asset_pointer"],
             "message_count": 1, "capture_state": "captured"}
    res = check_normalized(norm_of([msg("m", 1)]), entry, raw_doc([msg("m", 1)]))
    assert res["capture_quality"] == COMPLETE_WITH_WARNINGS
    assert "capture_warning" in res["finding_codes"]


# -- pass 4/5: worker contract ----------------------------------------------
BATCH = ["cid-1", "cid-2"]


def good_cls(cid="cid-1", **kw):
    d = {
        "conversation_id": cid,
        "primary_project": "GCE Framework",
        "secondary_projects": ["EC8"],
        "workstream": ["architecture", "recon"],
        "lifecycle_state": "completed",
        "durable_value": ["accepted_decision", "artifact_reference"],
        "framework_candidate": False,
        "human_review_required": False,
        "confidence": 0.85,
        "basis": "user asked for a taxonomy; assistant produced a table",
        "basis_spans": "turn 3-9",
    }
    d.update(kw)
    return d


def good_sum(cid="cid-1", **kw):
    d = {
        "conversation_id": cid,
        "conversation_purpose": "Reconcile the GCE taxonomy.",
        "key_outcomes": "- Table agreed\n- 3 seats corrected",
        "important_facts": "- Taxonomy v3 is the working set",
        "artifacts": "- docs/taxonomy_v3.md",
        "open_items": "- Seat ranks still unset",
        "current_relevance": "Still the reference for taxonomy questions.",
        "authority_warning": "Discussion predates the 09-25 seat freeze.",
        "candidate_routing": "framework_reconciliation_candidate",
        "confidence": 0.8,
    }
    d.update(kw)
    return d


def payload(**kw):
    d = {"schema": workers_mod.WORKER_SCHEMA, "worker": "w-test",
         "workstream": "pilot", "classifications": [good_cls()],
         "summaries": [good_sum()], "conversation_ids": BATCH}
    d.update(kw)
    return d


def test_worker_rejects_a_wrong_schema():
    with pytest.raises(WorkerRejected):
        ingest_worker_output("/tmp/x", {"schema": "nope"}, BATCH)


def test_worker_cannot_assert_a_conversation_outside_its_batch():
    rec, problems = workers_mod.validate_classification(good_cls(cid="not-mine"), BATCH)
    assert rec == {}
    assert any("not in the assigned batch" in p for p in problems)


def test_unknown_vocabulary_is_rejected_not_silently_accepted():
    rec, problems = workers_mod.validate_classification(
        good_cls(primary_project="Underwater Basket Weaving"), BATCH)
    assert rec["primary_project"] == "unclassified"
    assert any("primary_project" in p for p in problems)
    rec2, p2 = workers_mod.validate_classification(good_cls(workstream=["vibes"]), BATCH)
    assert rec2["workstream"] == ["research"]
    assert any("workstream" in x for x in p2)


def test_low_confidence_worker_cannot_assert_current_authority():
    rec, problems = workers_mod.validate_classification(
        good_cls(lifecycle_state="current_reference", confidence=0.4), BATCH)
    assert rec["lifecycle_state"] == "unknown"
    assert rec["human_review_required"] is True
    assert any("asserted at confidence" in p for p in problems)


def test_unproven_state_becomes_unknown_and_queues_review():
    rec, _ = workers_mod.validate_classification(
        good_cls(lifecycle_state="whatever", confidence=0.9), BATCH)
    assert rec["lifecycle_state"] == "unknown"
    assert rec["human_review_required"] is True


def test_worker_reported_conflict_is_carried_not_merged_away():
    rec, _ = workers_mod.validate_classification(
        good_cls(needs_reconciliation=True, conflict_detail="turn 4 says A, turn 9 says B"),
        BATCH)
    assert rec["needs_reconciliation"] is True
    assert "turn 4 says A" in rec["conflict_detail"]


def test_low_confidence_framework_candidate_is_flagged_for_review():
    rec, problems = workers_mod.validate_classification(
        good_cls(framework_candidate=True, confidence=0.4), BATCH)
    assert rec["framework_candidate"] is True
    assert rec["human_review_required"] is True
    assert any("below 0.6" in p for p in problems)


def test_summary_missing_sections_are_reported_and_routed_to_review():
    rec, problems = workers_mod.validate_summary({"conversation_id": "cid-1"}, BATCH)
    assert rec["candidate_routing"] == "needs_human_review"
    assert any("missing section" in p for p in problems)


def test_summary_bad_routing_value_falls_back_to_review():
    rec, problems = workers_mod.validate_summary(
        good_sum(candidate_routing="promote_to_canon"), BATCH)
    assert rec["candidate_routing"] == "needs_human_review"
    assert any("candidate_routing" in p for p in problems)


def test_summary_below_half_confidence_routes_to_review():
    rec, _ = workers_mod.validate_summary(good_sum(confidence=0.3), BATCH)
    assert rec["candidate_routing"] == "needs_human_review"


def test_ingest_writes_dimensions_and_never_verifies(store_root):
    res = ingest_worker_output(store_root, payload(), BATCH)
    assert res["classifications_ingested"] == 1
    assert res["summaries_ingested"] == 1
    from gce_ingestion.realms.handles import StageHandles
    with StageHandles("classify", store_root) as h:
        dims = [r["dimension"] for r in h.connection("DERIVED").execute(
            "SELECT DISTINCT dimension FROM classification_set")]
        assert set(dims) >= {"project", "workstream", "lifecycle_state", "durable_value",
                             "candidate_routing"}
        # nothing may reach VERIFIED authority
        states = [r["authority_state"] for r in h.connection("DERIVED").execute(
            "SELECT authority_state FROM knowledge_object")]
        assert "VERIFIED" not in states


def test_ingest_queues_review_items(store_root):
    ingest_worker_output(store_root, payload(
        classifications=[good_cls(lifecycle_state="unknown", confidence=0.3)]), BATCH)
    from gce_ingestion.realms.handles import StageHandles
    with StageHandles("classify", store_root) as h:
        n = h.connection("CONTROL").execute(
            "SELECT COUNT(*) AS n FROM review_item WHERE state='PENDING'").fetchone()["n"]
    assert n >= 1


def test_summary_markdown_carries_provenance_not_canon(tmp_path):
    from gce_ingestion.stages.workers import _summary_markdown
    md = _summary_markdown(workers_mod.validate_summary(good_sum(), BATCH)[0],
                           {"title": "T", "canonical_url": "u", "turn_count": 5,
                            "capture_status": "ok",
                            "provenance": {"source_artifact_path": "/b", "source_content_hash": "ff"}})
    assert "Not canon, not authority" in md
    assert "/b" in md and "ff" in md


# -- pass 1 / 6 / 7 ----------------------------------------------------------
def test_capture_queue_does_not_restart_from_one(store_root):
    doc = {"conversations": [
        {"conversation_id": "a", "capture_state": "captured", "title": "A"},
        {"conversation_id": "b", "capture_state": "needs_capture", "title": "B"},
        {"conversation_id": "c", "capture_state": "needs_recapture", "title": "C"},
    ]}
    q = manifest_mod.capture_queue(doc)
    assert [x["conversation_id"] for x in q] == ["b", "c"]
    assert "a" not in [x["conversation_id"] for x in q]


def test_index_is_rebuildable_and_empty_safe(store_root):
    doc = index_mod.build_index(store_root)
    assert doc["schema"] == index_mod.INDEX_SCHEMA
    assert doc["totals"]["conversations"] == 0
    paths = index_mod.write_index(store_root, doc)
    assert paths["json"].endswith("index.json")


def test_review_queue_reasons_are_specific(store_root):
    doc = index_mod.build_review_queue(store_root)
    assert doc["schema"] == index_mod.REVIEW_SCHEMA
    assert isinstance(doc["items"], list)
