# Stage 1 Recon — ChatGPT browser backup (Nico, 2026-09-26)

**Mode:** Recon only. Reads of files, processes, prefs, live DOM/network. No writes to
chatgpt.com, no message sends, no conversation mutations, no credential extraction.
**Authority:** Don task brief (GCE Stack). Seat: Nico (Implementation Design / Build).
**Evidence labels:** [V] verified this run · [RH] reported-historical · [SS] stale · [UNK] unknown.

---

## 0. Headline finding

The current system **has no ChatGPT capture path at all**, and the WebBridge transport
**is** reusable unchanged. The correct build is a new provider adapter on the existing
Wave-1 spine — not a third copy of the Kimi scraper, and not a redesign.

---

## A. Existing reusable substrate

| Asset | Path | Class | Note |
|---|---|---|---|
| Daemon binary + logs | `~\.kimi-webbridge\bin\kimi-webbridge.exe` | **ACTIVE / REUSABLE** | v2.0.22. `start`/`status`/`stop`/`restart`/`logs`/`upgrade`. Binds `127.0.0.1:10086`. |
| Wire envelope | `POST /command` `{action, args, session}` | **ACTIVE / REUSABLE** | C6 CONFIRMED 2026-09-23 (`06_HANDOFFS\Don\20260923-KEEL-C6-WEBBRIDGE-VERIFICATION.md`). Read result from `data.value`. |
| Chrome extension | `fldmhceldgbpfpkbgopacenieobmligc` | **ACTIVE / REUSABLE** | v2.0.22 in the **Default** Chrome profile. [V] Attached this run. |
| Transport client slot | `gce-ingestion-w1\src\gce_ingestion\transport\webbridge.py` | **ACTIVE but HELD** | `navigate`/`evaluate` still `raise DaemonUnavailable`; envelope deliberately unpinned. Now un-pinnable — C6 is empirical. |
| Adapter slot | `...\adapters\chatgpt.py` | **ACTIVE, EMPTY** | 10-line `NetNewProviderStub`. The declared slot for exactly this work. |
| Adapter contract | `...\adapters\base.py` | **REUSABLE** | `probe/enumerate/traverse_and_capture/checkpoint/resume` — maps 1:1 onto the required capability list. |
| Failure vocabulary | `...\failures.py` | **REUSABLE** | F1–F13 with `retryable` / `stops_run` / `stops_adapter`. |
| Store / state | `...\evidence.py`, `store\cas.py`, `stages\acquire.py` | **REUSABLE** | CAS blob + `raw_observation` + `predecessor_observation_id` chain + per-conversation checkpoint. Resume-after-interrupt and preserve-prior-artifacts already exist. |
| Retry-with-explicit-state pattern | `kimi-chat-scraper\src\webbridge_extractor.py:284` | **REUSABLE** | `capture_chat_with_retry` never promotes a miss. |
| PURE_START/PURE_END node test harness | `kimi-chat-scraper\tests\test_webbridge_extractor.py:28` | **REUSABLE** | Pure JS executed under node; skipped if node absent. |

### Classification

**ACTIVE:** daemon + extension + `gce-ingestion-w1` code + its 35 tests · `kimi-chat-scraper`
tree · both suites green at Stage 1 entry (20 + 35).

**REUSABLE UNCHANGED:** daemon binary, wire envelope, extension, five-realm store, CAS,
checkpoint/resume, `AdapterInterface`, `FailureCode`, PURE-block test convention.

**PROVIDER-SPECIFIC (must NOT generalise):**
- `KIMI_URL`, `kimi://conversation/<id>` URL scheme, `a[href^="/chat/"]`, `/chat/<uuid>` route.
- `ASSISTANT_TURN_DELIM = /\s*(?:Edit\s+)?Copy\s+Share\s*/gi` — the K3/legacy "Copy Share"
  button split. ChatGPT has no such delimiter; role parity is **not** reliable there.
- `extractMainText()` sidebar/input stripping, `getChatTitle()`'s `- Kimi` suffix strip.
- `consolidate.py`: `{role, content, timestamp}` messages only; folder `"{date} - {title}"`
  (title-keyed, not ID-keyed); drops empty-content messages; hardcodes `🤖 Kimi`.

**STALE:** `gce-ingestion-w1\transport\webbridge.py` held state (superseded by C6);
`kimi-chat-scraper\STATUS.md` counts (93 vs actual 106); `kimi-chat-archive.old-bloated-*`;
`bin\kimi-webbridge.version` (`3.2.6` = desktop-app version, not daemon version — the
operations doc states this); the `Coda` browser profile as a WebBridge target (no extension).

**UNKNOWN:** whether archived-conversation content and alternate/regenerated branches are
reachable through the detail endpoint for *all* accounts; `context_truncation_continuation`
semantics when non-null; `include_has_versions` effect (no-op observed on the one
conversation that had versions).

---

## B. ChatGPT-specific surface — materially different from Kimi

| Dimension | Kimi | ChatGPT (observed) |
|---|---|---|
| Chat route | `/chat/<uuid>` | `/c/<uuid>` **and** `/g/g-p-<projectId>[-slug]/c/<uuid>` (project-scoped) |
| Turn boundary | "Copy Share" action-button text split, role by parity | authoritative `author.role` per message — **no delimiter needed** |
| Message store | flattened text | `messages[]` with per-message `id`, `author.role`, `create_time`, `content.content_type`, `content.parts[]`, `metadata.model_slug`, `status`, `weight` |
| Observed roles | user/assistant only | `user`, `assistant`, **`tool`** (tool results/citations), **`system`** |
| Observed content types | text | `text`, `multimodal_text`, `code`, `thoughts`, `reasoning_recap`, `tether_browsing_display`, `execution_output`, `model_editable_context` |
| Observed part types | — | plain string, `audio_transcription` (voice mode), `image_asset_pointer` |
| Whole-conversation fetch | n/a | **paged**: `num_turns` ≤ 100, backward walk via `&before=<page_info.start_cursor>` |
| Projects | `create_project_chats.py` | first-class: `gizmo_id` / `conversation_template_id` on every item |
| Auth probe | "not logged in" ⇒ 0 chats | `#client-bootstrap` JSON → `authStatus` (`logged_in`) |

ChatGPT has **no equivalent of the "Copy Share" delimiter**, which is precisely the Kimi
fragility the C6 report flagged. Structured capture removes that class of defect entirely.

---

## C. Capture strategy

**Chosen: browser-accessible first-party JSON, fetched same-origin from inside the page.**

The ChatGPT web app calls its own backend for both the conversation list and each
conversation. The page already holds the bearer token and the account id in
`#client-bootstrap`. The adapter reads them **inside one `evaluate`**, uses them on that
same `evaluate`, and returns only conversation data.

* Fidelity — highest available: per-message ids, roles, timestamps, per-model slugs,
  structured parts, tool results/citations, reasoning, code, images, voice transcripts.
* Repeatability — no DOM/CSS dependence; `data-*` class hashes are unstable and
  `article` selectors do not even exist on the conversation page (0 matches observed).
* Recoverability — the raw payload is the artifact; nothing is normalised away.
* Stability — the JSON contract is versioned by the API, not by A/B-tested markup.

**Explicitly rejected:**
- **Rendered DOM scraping** — loses tool/citation content, has no reliable role signal,
  needs the virtualised history scrolled, and depends on hashed Tailwind classes.
- **`network` action capture** — usable for recon, wrong for the product: bodies are
  released on `stop`, and it cannot re-fetch history without a page reload per item.
- **Reading the Chrome `Cookies` SQLite DB** — out of the auth boundary. Not attempted;
  the browser's own session is used instead.

**Auth boundary:** token + account id are read and used inside the page, never returned to
the driver process, never written to disk, never logged. No cookies exported, no passwords
stored, no MFA or access control touched. This is the sanctioned pattern in the WebBridge
skill's `site-exploration.md` ("read it in the same `evaluate` and send it there… it never
needs to leave the page").

### Observed silent-empty trap (must be classified, never treated as "no conversations")

A bare same-origin `fetch('/backend-api/conversations?offset=0&limit=3')` returns
**HTTP 200 `{"items":[],"total":0}`** — an empty list, not an error — because the list call
is authorised by `authorization: Bearer …` + `chatgpt-account-id`, not by cookies alone.
Only the page's own frontend sends those. Captured-vs-replayed comparison proved it:
network-captured response = 20 items / total 21; naive replay = 0 items / total 0;
authenticated replay = 20 items / total 21, identical ids/titles/timestamps.

---

## D. Artifact contract (one captured conversation)

Primary artifact = **the raw first-party conversation JSON, byte-preserved**, stored
content-addressed under `EVIDENCE/blobs/sha256/…`. Interpretation is a separate,
optional derivative — the raw payload is never replaced.

One observation row carries the indexable metadata:

| Field | Source |
|---|---|
| `conversation_id` | `/backend-api/conversations/<id>` → `conversation_id` |
| `title` | `title` |
| `canonical_url` | observed `location.href` (project slug resolved by the app) |
| `create_time` / `update_time` | provider epoch floats → ISO-8601 UTC |
| `default_model_slug` | conversation level |
| per-message `model_slug` | `messages[].metadata.model_slug` |
| `gizmo_id` (project) | conversation + list item |
| turn count / role counts | derived at walk time |
| `pages_walked`, `has_previous_page` exhausted | completeness evidence |
| `capture_path` | `first-party-json` |
| `completeness_status` | `COMPLETE` / `PARTIAL` |
| extraction warnings | content classes present but not text (e.g. image bytes) |

Message-level fidelity is total because the raw JSON *is* the record: roles, order, ids,
timestamps, code, links, citations, attachments, model metadata all survive.

---

## E. State strategy

Existing spine supplies it; no new registry.

- **What exists** — `raw_observation` rows keyed by `content_hash`, chained by
  `predecessor_observation_id`, one per capture. Re-running appends; nothing is overwritten.
- **What is captured** — `last_capture_status`, `update_time`, `message_count`,
  `content_hash` per conversation.
- **What changed** — compare provider `update_time` against the newest prior observation's
  stored `update_time`. Equal ⇒ skip (unless `--force`).
- **What needs retry** — a non-`ok` capture writes a quarantine row, resumable per F-code.
- **Resume** — `checkpoint` row holds `{run_id, last_provider_conv_id}`;
  `ingest_observations` skips forward to it. Enumeration is by stable conversation ID;
  **titles are never identity**.

---

## F. Smallest implementation plan

| # | File | Why |
|---|---|---|
| 1 | `src/gce_ingestion/transport/webbridge.py` | Un-hold: implement the C6-pinned envelope (`status`/`navigate`/`evaluate`/`close`). Justified by C6 CONFIRMED, not invented. |
| 2 | `src/gce_ingestion/adapters/chatgpt.py` | Replace the stub with the real adapter: auth probe, exhaustive enumeration, paged capture, explicit capture states. |
| 3 | `src/gce_ingestion/cli/chatgpt_backup.py` | Entry point: enumerate → filter → ingest via existing `ingest_observations`; run summary. |
| 4 | `tests/test_chatgpt_adapter.py` | Targeted tests for route classification, ID extraction, capture states, pagination, incremental state, retry, dedupe, serialisation. |
| 5 | `tests/test_adapters.py` | One justified edit: the existing test asserts ChatGPT **is** a net-new stub. That assertion is the thing being changed; Claude's isolation assertion is kept. |

Deliberately **not** touched: the Kimi extractor, the daemon, the extension, the Kimi
archive, and every other Wave-1 module.

---

## Live evidence captured this run

- Daemon `v2.0.22`, extension `2.0.22` attached, `/status` 200.
- `#client-bootstrap` `authStatus = "logged_in"`; account `e450738b-…`.
- Account: Peter Wilson, `user-eHOYYnVcbYMaV9ImrTI6zxEq` (email **not** stored in artifacts).
- Enumeration totals: **101 active**, **9 archived**, **3 `conversation_origin=tpp`**
  → 113 conversations. `limit` max 100, `offset` paginates.
- Pinned: 5 conversations via `/backend-api/pins?item_type=conversation`.
- `num_turns` cap = 100 (422 `le: 100` above it). `before`/`after` are the only real
  cursor params — `cursor`, `end_cursor`, `start_cursor`, `page`, `offset`, `from` are
  silently ignored and return the default page.
- Backward walk proof on `6ab70dbe-…` (GCE_Taxonomy_V3): 3 pages → **590 distinct
  messages** (311 assistant / 148 tool / 117 user / 14 system), `has_previous_page:false`
  at the end. A single `num_turns=100` page returned 223 — i.e. paging is mandatory.
- Roles/content types/part types/model slugs as tabulated in §B.
