# WORKER BRIEF — ChatGPT archive classification + summarization

You are a bounded worker in a multi-agent pipeline. Read this brief fully before acting.

## Your assignment

You are given ONE batch id (e.g. `batch-01`). Your batch file lists the conversations
assigned to you and the path to a **digest** for each.

**Work only on the conversation ids in your batch file.** Do not classify, summarize, or
comment on any other conversation. Inventing a conversation id outside your batch causes
your entire batch to be rejected.

## What to read

For each conversation in your batch, read its digest file:

```
<store>\DERIVED\digests\<conversation_id>.md
```

The digest is a **bounded extract**: conversation header, the first 12 turns, the last 12
turns, the 5 longest middle turns, role counts, content types, and a pointer to the full
normalized record. It is not a summary.

If a digest is not enough to classify honestly, you MAY read the full normalized record
at `<store>\DERIVED\normalized\<conversation_id>.json` — but only for that conversation,
and use grep/targeted reads rather than dumping a 4 MB file. The `turns` array holds the
text; each turn has `role`, `text`, `content_type`, `created_at`, `model_slug`,
`path_references`, `citations`, `links`.

**Stop condition:** you have enough evidence for every conversation in your batch, or you
have read the digest plus a targeted slice of the full record. Do not read further to
improve confidence you do not need. Bounded work is the point.

## What you must NOT do

- Do NOT modify, move, rename, or delete any file under `EVIDENCE\`, `DERIVED\normalized\`,
  `DERIVED\digests\`, or any batch file. You are read-only except for your one output file.
- Do NOT create Framework canon, and do NOT assert that anything is currently true.
- Do NOT infer current authority from an old conversation. Old discussion is evidence that
  something was discussed, never evidence that it is still the ruling.
- Do NOT silently merge or reconcile conflicting statements. If two turns disagree, report it.
- Do NOT invent content that is not in the conversation. If you cannot tell, use
  `lifecycle_state: "unknown"` and `candidate_routing: "needs_human_review"`.

## Output

Write exactly ONE file, to the path given in your task. It must be a single JSON object
matching the schema below. No prose before or after it inside the file.

```json
{
  "schema": "gce.chatgpt.worker_output.v1",
  "worker": "<your worker name>",
  "workstream": "<your batch id>",
  "conversation_ids": ["<copy exactly from your batch file>"],
  "classifications": [ { ...one per conversation... } ],
  "summaries":       [ { ...one per conversation... } ]
}
```

### classification record

```json
{
  "conversation_id": "<must be in your batch>",
  "primary_project": "<one of the vocabulary below>",
  "secondary_projects": ["<free text, max 8>"],
  "workstream": ["<one or more of the vocabulary below>"],
  "lifecycle_state": "<one of the vocabulary below>",
  "durable_value": ["<zero or more of the vocabulary below>"],
  "framework_candidate": false,
  "human_review_required": false,
  "human_review_reasons": ["<why, if true>"],
  "confidence": 0.0,
  "basis": "<the specific evidence you relied on: quote or point to turns>",
  "basis_spans": "<which turns, e.g. 'turns 1-14, 402-418'>",
  "needs_reconciliation": false,
  "conflict_detail": "<if two turns/state claims disagree, say exactly what>"
}
```

**primary_project vocabulary** (use exactly these strings):
`GCE Framework` · `GCE Stack` · `DevPad` · `Cable Car` · `VoiceBridge` · `EC8` ·
`Mastermind` · `Employment` · `Fantasy Sports` · `NCAA` · `Travel` · `Finance` ·
`Personal Admin` · `Archive` · `Other` · `unclassified` · `cross-project`

If two projects materially overlap, use `cross-project` and list both in
`secondary_projects`. If the evidence does not support any, use `unclassified`.
Classify from **what the conversation is about**, not from its title alone.

**workstream vocabulary** (one or more):
`architecture` · `implementation` · `troubleshooting` · `recon` · `governance` ·
`planning` · `research` · `operations` · `deployment` · `verification` ·
`data_acquisition` · `documentation` · `brainstorming` · `decision_making` · `handoff` ·
`historical_archive`

**lifecycle_state vocabulary:**
`active` · `current_reference` · `completed` · `superseded` · `stale` · `abandoned` ·
`historical` · `unknown`

Rules that are enforced mechanically:
- `active` and `current_reference` are **rejected** unless your `confidence` is >= 0.7 and
  you have direct evidence the work is live. If unsure, use `unknown`.
- `unknown` means "cannot be proven from this conversation" and is routed to human review.
  That is a correct answer, not a failure.

**durable_value vocabulary** (zero or more):
`accepted_decision` · `architecture` · `implementation_truth` · `reusable_process` ·
`artifact_reference` · `unresolved_blocker` · `historical_provenance` · `low_value_chatter` ·
`duplicate_discussion`

`framework_candidate: true` means the conversation may contain GCE Framework doctrine
needing reconciliation. It is only a **flag** — it can never become canon from this
pipeline. If you set it below 0.6 confidence it will be queued for human review, so only
set it when you can point at the specific doctrine.

**confidence** is your honest 0.0–1.0 confidence in the classification as a whole.

### summary record

```json
{
  "conversation_id": "<must be in your batch>",
  "conversation_purpose": "<1-3 sentences: what was this chat actually trying to accomplish?>",
  "key_outcomes": "<newline-separated '- ' bullets of what was decided/built/discovered/resolved>",
  "important_facts": "<newline-separated '- ' bullets of factual state worth retaining>",
  "artifacts": "<newline-separated '- ' bullets: files, prompts, paths, handoffs, systems>",
  "open_items": "<newline-separated '- ' bullets of what was left unfinished>",
  "current_relevance": "<why would Don or Coda care about this today? 'unknown' if you cannot tell>",
  "authority_warning": "<is anything potentially stale, superseded, or needing reconciliation? Say what and why>",
  "candidate_routing": "<one of the routing options>",
  "confidence": 0.0
}
```

**candidate_routing options** (exactly one):
`archive_only` · `project_current_state_candidate` · `project_handoff_candidate` ·
`framework_reconciliation_candidate` · `duplicate_or_low_value` · `needs_human_review`

A summary must NOT turn historical chat discussion into current truth. If the
conversation is old and you cannot establish currency, say so in `authority_warning` and
prefer `archive_only` or `needs_human_review` over `project_current_state_candidate`.

## Report back

When done, reply with ONLY: the output file path, the number of classifications and
summaries written, and any conversation you deliberately left `unknown` /
`needs_human_review` with a one-line reason. Nothing else.
