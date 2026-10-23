"""Acquire stage (Wave-1). Uses acquire capability handles only.

Does the acquisition->store cycle: traverse each conversation, append a
RawObservation + CAS blob with a predecessor chain, and checkpoint after each
conversation. Never writes CANON/DERIVED/VIEW.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable, List, Optional

from ..control.run import finish_run, load_checkpoint, save_checkpoint, start_run
from ..evidence import RawObservation, latest_predecessor, store_observation
from ..failures import FailureCode
from ..realms.handles import StageHandles

log = logging.getLogger(__name__)


def ingest_observations(
    store_root: str,
    provider: str,
    conversation_ids: Iterable[str],
    capture: object,                       # object exposing traverse_and_capture
    adapter_version: str = "w1-0.1.0",
    scraper_version: str = "w1-0.1.0",
    run_id: Optional[str] = None,
) -> int:
    """Acquire+store observations for ``conversation_ids``. Returns count stored."""
    ids = list(conversation_ids)
    count = 0
    with StageHandles("acquire", store_root) as handles:
        control = handles.connection("CONTROL", write=True)
        run_id = run_id or start_run(control, provider=provider)
        state = {"count": 0}
        try:
            _acquire_loop(handles, control, ids, provider, run_id, capture,
                          adapter_version, scraper_version, state)
            count = state["count"]
            finish_run(control, run_id, status="COMPLETE", note=f"stored={count}")
        except BaseException as exc:
            # A stage that dies must not leave a RUNNING run behind: that is a silent
            # failure. Mark it PARTIAL with the real reason, then re-raise.
            count = state["count"]
            try:
                finish_run(
                    control, run_id, status="PARTIAL",
                    note=f"stored={count} aborted: {type(exc).__name__}: {exc}"[:500],
                )
            except sqlite3.Error:
                pass
            raise
    return count


def _acquire_loop(handles, control, ids, provider, run_id, capture,
                  adapter_version, scraper_version, state) -> None:
    from ..control.quarantine import record_quarantine

    resume_from = load_checkpoint(control, run_id, provider)
    resumed = resume_from in ids
    for conv_id in ids:
        if resumed:
            # skip up to and including the checkpointed conversation
            if conv_id != resume_from:
                continue
            resumed = False
            continue
        cap = capture.traverse_and_capture(conv_id)
        if not cap.structure_ok:
            meta = cap.provider_metadata or {}
            record_quarantine(
                control,
                FailureCode(cap.failure_code or FailureCode.F6),
                reason=(f"{provider} {conv_id} skipped: "
                        f"status={meta.get('capture_status', 'unknown')} "
                        f"reason={meta.get('capture_reason') or 'n/a'} "
                        f"url={meta.get('canonical_url') or 'n/a'}"),
                stage="acquire",
                resumable=cap.failure_code not in (FailureCode.F2.value,),
            )
            continue
        predecessor = latest_predecessor(handles, provider, conv_id)
        store_observation(
            handles,
            RawObservation(
                run_id=run_id, provider=provider,
                provider_conversation_id=conv_id,
                capture_path=cap.capture_path,
                provider_metadata=cap.provider_metadata,
                scraper_version=scraper_version,
                adapter_version=adapter_version,
                predecessor_observation_id=predecessor,
            ),
            payload=cap.payload,
        )
        save_checkpoint(control, run_id, provider, conv_id)
        state["count"] += 1
