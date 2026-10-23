# ChatGPT Browser Backup — Verification Return (Nico, 2026-09-26)

**SEAT:** Nico — Implementation Design / Build
**AUTHORITY:** Don task brief (GCE Stack). Project boundary: GCE Stack, not Framework canon.
**CLASSIFICATION:** `CHATGPT BACKUP MVP PARTIAL`
**Store root:** `Projects\ACTIVE\gce-ingestion-w1\store\chatgpt-backup\`
**Transport:** WebBridge daemon v2.0.22 · extension v2.0.22 · 127.0.0.1:10086 (C6 CONFIRMED 2026-09-23)

---

## 1. Recon findings

- **There was no ChatGPT capture path at all.** `gce-ingestion-w1\adapters\chatgpt.py` was a
  10-line `NetNewProviderStub`. The Kimi scraper is Kimi-only and Kimi-specific down to its
  turn splitter (`/\s*(?:Edit\s+)?Copy\s+Share\s*/gi`).
- **The WebBridge transport was reusable unchanged** but the w1 client had deliberately
  *held* it (`navigate`/`evaluate` raised `DaemonUnavailable`, envelope "unpinned"). C6
  (2026-09-23) supplied the missing empirical pin, so un-holding it was justified, not invented.
- **ChatGPT has no delimiter-based turn boundary.** Roles are authoritative per message.
  The Kimi "Copy Share" fragility class does not exist here.
- **Routes are dual:** `/c/<id>` and `/g/g-p-<projectId>[-slug]/c/<id>`.
- **Highest-fidelity surface is first-party JSON**, not the DOM. Proven three ways:
  network-captured response (20 items), naive same-origin replay (0 items, HTTP 200 — a
  *silent empty*), authenticated in-page replay (20 items, identical ids).
- **A conversation does not fit in one request.** `num_turns` caps at 100 (422 above it);
  the full history is a backward `&before=<page_info.start_cursor>` walk.
- **Live account:** Peter Wilson, `user-eHOYYnVcbYMaV9ImrTI6zxEq` (email deliberately not
  stored in artifacts). **537 conversations across 20 projects.**

Full recon: `docs\2026-09-26-CHATGPT-BACKUP-STAGE1-RECON.md`

## 2. Architecture actually used

```
CLI (cli/chatgpt_backup.py)
  └─ ChatGPTAdapter (adapters/chatgpt.py)        probe / enumerate / traverse_and_capture
       └─ WebBridgeClient (transport/webbridge.py)  POST /command {action,args,session}
            └─ daemon 127.0.0.1:10086 -> Chrome extension -> page
                 └─ in-page fetch of ChatGPT's own /backend-api (bearer read in-page)
  └─ ingest_observations (stages/acquire.py)    loop + checkpoint + quarantine
       └─ store_observation (evidence.py)        row + CAS blob + predecessor chain
            └─ EVIDENCE/blobs/sha256/<2>/<64>
```

Two rules earned during the build and applied consistently:
1. **One HTTP request per `evaluate`.** Pagination and 429 backoff live in Python. A
   single all-in-one script overran the daemon's 30 s evaluate window (observed twice).
2. **Discovery is separate from capture.** The list endpoint is throttled to ~1 success
   per 60 s; a complete enumeration costs ~8.5 min. It is persisted, and a capture run
   reuses it instead of re-hitting the list and throttling itself.

## 3. Files changed / created

Created:
- `src\gce_ingestion\adapters\chatgpt.py` (the adapter; replaces the stub)
- `src\gce_ingestion\cli\chatgpt_backup.py` + `cli\__init__.py` (entry point)
- `tests\test_chatgpt_adapter.py` (69 tests)
- `tests\test_acquire_stage.py` (9 tests)
- `docs\2026-09-26-CHATGPT-BACKUP-STAGE1-RECON.md`
- `docs\2026-09-26-CHATGPT-BACKUP-VERIFICATION-RETURN.md` (this file)

Changed (minimal, additive, backward compatible):
- `transport\webbridge.py` — un-held; C6-pinned envelope; `ensure_target`; 502 and
  extension-disconnect classified; bounded reconnect.
- `adapters\base.py` — `CaptureResult.provider_metadata`, `EnumerateResult.items`.
- `stages\acquire.py` — metadata carried into the observation row; every skip records an
  explicit status+reason+url; a dying stage closes its run as `PARTIAL` instead of
  leaking a `RUNNING` row.
- `evidence.py` — `latest_observation()` for the incremental read.
- `config.py` — `extension_wait_s`.
- `tests\test_adapters.py` — the assertion that ChatGPT *is* a net-new stub was the thing
  being changed; Claude's A8 isolation assertion is retained unchanged.
- `pyproject.toml` — `gce-chatgpt-backup` console script.

**Not touched:** the Kimi extractor, the daemon, the extension, the Kimi archive, any
other Wave-1 module. `kimi-chat-scraper` suite still 20/20 green.

## 4. Capture source

**First-party JSON, fetched same-origin from inside the authenticated page.** The bearer
token and account id are read from `#client-bootstrap` and used in the same `evaluate`; they
never reach the driver process, never touch disk, never get logged. No cookie export, no
credential replay, no access-control bypass. `authStatus == "logged_in"` is the auth probe.

## 5. Artifact format

Primary artifact = the provider's own conversation object, byte-preserved, under
`provider_payload`, with the walked `messages` beside it. `capture_path = "api"` (the
schema's existing closed vocabulary); the precise surface rides in
`provider_metadata.capture_surface = "first-party-json"` rather than widening the CHECK.

```json
{
  "schema": "gce.chatgpt.capture.v1",
  "capture_status": "ok",
  "captured_at": "2026-09-26T08:15:31Z",
  "canonical_url": "https://chatgpt.com/g/g-p-.../c/6ab70dbe-...",
  "conversation_id": "6ab70dbe-...",
  "project_id": "g-p-6ab43fca...",
  "stats": { "message_count": 614, "role_counts": {"user":128,"assistant":324,"tool":148,"system":14},
             "part_types": {...}, "content_types": ["code","execution_output","model_editable_context",
             "multimodal_text","reasoning_recap","tether_browsing_display","text","thoughts"],
             "model_slugs": ["bidi","gpt-5-6-thinking"], "pages_walked": 3 },
  "pages": [ {"got": 223, "has_previous_page": true, "start_cursor": "..."} ],
  "warnings": ["non_text_part:audio_transcription", "non_text_part:image_asset_pointer"],
  "provider_payload": { ...the provider's conversation object, unfiltered... },
  "messages": [ {"id","author":{"role"},"create_time","content":{"content_type","parts"},
                 "metadata":{"model_slug"},"status","weight","recipient", ...} ]
}
```

Storage is content-addressed (`EVIDENCE/blobs/sha256/…`) plus a `raw_observation` row with
`content_hash`, `predecessor_observation_id`, and the metadata above. Re-running appends;
nothing is overwritten or deleted. **capture ≠ interpretation** is structural: the raw
object is the artifact, and no derived Markdown stands in for it.

Encoding verified: 3.4 M characters decoded as strict UTF-8, 6 975 non-ASCII,
**0 U+FFFD** — no mojibake stored.

## 6. State model

- **Identity** = provider conversation id. Titles are never identity (dedupe by id is
  unit-tested against a duplicate-title fixture).
- **Incremental** = compare provider `update_time` against the newest stored observation's.
  Equal ⇒ skip. `--force` overrides. Unknown ⇒ recapture.
- **Changed** = a new observation is appended, chained to its predecessor. Prior artifacts
  stay on disk (test asserts this).
- **Resume** = a `checkpoint` row per run; the loop skips forward to it.
- **Failures** = an explicit state plus an F-code, quarantined with
  `status=… reason=… url=…` — never a bare null.
- **Discovery** = persisted to `CONTROL\discovery_chatgpt.json`; only a `status: ok`
  enumeration is persisted, and a `partial` one is a hard stop unless
  `--allow-partial-enumeration`.

## 7. Three-chat proof

| # | Title | ID | Result | Turns | Pages | Warnings |
|---|-------|----|--------|-------|-------|----------|
| 1 | GCE_Taxonomy_V3 | `6ab70dbe-…0d7fed` | `ok` / COMPLETE | 614 | 3 | audio_transcription, image_asset_pointer |
| 2 | GCE_Kimi_Scraper | `6ab41b8a-…c3c84fe` | `ok` / COMPLETE | 54 | 1 | image_asset_pointer |
| 3 | VoiceBridge Cleanup Optimization | `6ab45301-…15e6efad8b78` | `ok` / COMPLETE | 47 | 1 | image_asset_pointer |

Materially different characteristics, as required: an ordinary text conversation, a code +
image conversation, and a long voice/tool/reasoning conversation. For all three: discovery
✓, navigation ✓ (project-scoped URL where applicable), full capture ✓, artifact on disk ✓,
`ids unique` ✓, ordering by `create_time` ✓, no truncation ✓, status `ok` ✓.

## 8. Larger batch result

**Reached 33 conversations, not 25** — the run was stopped by Don when ChatGPT rate-limited
the session.

- 33 distinct conversations · 40 capture events · **12 849 messages** · **85.1 MB** on disk
  (store total 91.5 MB) · **0 missing artifacts** · **33/33 COMPLETE**
- Turn counts range 10 → 1 723; largest single artifact 10.98 MB (`NCAA Methodology
  Walkthrough`, 511 turns, 2 pages).
- Roles captured: `user`, `assistant`, **`tool`** (results/citations), **`system`**.
- Content types captured: `text`, `multimodal_text`, `code`, `thoughts`, `reasoning_recap`,
  `tether_browsing_display`, `execution_output`, `model_editable_context`.
- Projects represented: 20 discovered, 8 in the captured set.
- Enumeration: 537 conversations, `status: ok` — 520 active + 9 archived + 3 `tpp` + 5 pinned.
- 2 quarantined skips, both with explicit reasons (see §9).
- Incremental proven: 7 conversations hold 2–3 captures each with an intact predecessor
  chain; a re-run planned `{"new": 1, "changed": 24}` and correctly re-captured only those.

**Not reached:** the full 537. That is the gap that keeps this `PARTIAL`, not a defect.

## 9. Known limitations (observed only)

1. **Rate limiting is the binding constraint.** `/backend-api/conversations` (list) allows
   ~1 success per 60 s; the detail endpoint showed no 429 across 33 captures. Enumeration
   costs ~8.5 min; a full 537-capture run needs pacing and will take hours. Not a defect,
   but it means full enumeration must be a deliberate, resumable, long-running job.
2. **The list endpoint's `total` is not a stable completeness oracle** — offset 0 reported
   101 while offset 100 reported 201 in one run. Termination therefore keys on a short
   page, and disagreement is recorded as a `total_disagreement_across_pages` warning.
3. **Image bytes are not captured** — only the `image_asset_pointer` reference. Flagged as
   `non_text_part:image_asset_pointer`, not silently dropped.
4. **Two skips, both transport, both fixed after the fact:**
   `6aa5a54d-…` evaluate timeout (the in-page walk overran the 30 s window — fixed by moving
   the walk to Python, one request per evaluate); `6ab4ae19-…` extension disconnected
   mid-run (now a bounded reconnect at the transport layer rather than a hard F2).
5. **One conversation is live during capture.** `GCE_Taxonomy_V3` had an active voice-mode
   turn; its tail is a `bidi`/`audio_transcription` exchange. A capture racing an in-flight
   turn is possible and is not separately flagged.
6. **Alternate/regenerated branches** — `include_has_versions` had no observable effect on
   any conversation captured; branch coverage is therefore unverified, not proven absent.
7. **`context_truncation_continuation`** was null everywhere observed; its semantics when
   non-null are untested.
8. **The `Coda` browser profile is not usable with this transport** — it has no WebBridge
   extension. The **Default** Chrome profile is required.

## 10. Next smallest action

**One action:** wait ~15 minutes for the throttle to clear, then run
`gce-chatgpt-backup --store-root <store> --max 25` to take the captured set from 33 → 58 and
confirm the two previously-skipped conversations now capture clean.

```
gce-chatgpt-backup --store-root "C:\Users\Dekan AI Brother\Projects\ACTIVE\gce-ingestion-w1\store\chatgpt-backup" --max 25
```

The persisted enumeration is reused, so this touches only the detail endpoint. Do **not**
pass `--force-enumeration` — that is what re-triggers the list throttle.

---

## Framework-level lesson (identified separately, per §20 — not promoted to canon)

A closed enum in a store schema is a contract, not a formality: the first build failed
`CHECK constraint failed` on `capture_path`. The correct project-level response was to use
the existing correct value (`api`) and record the precise surface in metadata — not to widen
the constraint. Whether `capture_path` should gain a `first-party-json` value is a design
decision for Don/Chiave, not a build decision. **Recommend routing to Chiave** if the
vocabulary is to be widened.

## Disclosure

- Two acquisition runs are recorded `STOPPED`, not left `RUNNING`: `4f92ecb6` (the
  `capture_path` CHECK failure, since fixed) and `22ea6421` (halted by Don on rate limiting).
  Neither stored partial evidence; no run remains `RUNNING`.
- Defects found and fixed during this build, all by test: the 502-before-navigate trap, the
  429-reported-as-ok silent empty, the shared-retry-budget bug in the page loop, the
  per-page evaluate timeout, the page-id mismatch guard defeated by the walk refactor, and
  the run-lifecycle leak.
- **This seat did not verify its own work.** The 115 passing tests and the live figures above
  are implementation evidence, not verification. Route to **Luce** for independent
  verification.

```
HANDOFF: C:\Users\Dekan AI Brother\GCE_ORG\02_COUNCILS\01_Executive_Council\Operations\06_HANDOFFS\Nico\20260926-NICO-CHATGPT-BACKUP-VERIFICATION-RETURN.md
NEXT ROUTE: Luce
```
