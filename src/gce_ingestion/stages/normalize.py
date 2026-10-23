"""Pass 2 -- normalization to a canonical conversation schema.

Structural only. This stage does not summarise, does not classify, and does not
interpret. It reshapes one raw capture document into a stable record that later passes
and sub-agents can rely on, while keeping an unbroken provenance chain back to the
exact bytes that were captured.

capture != interpretation: the raw CAS blob is never modified. The normalized record is
a *second* artifact that always names its source by content hash and observation id.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..evidence import get_payload
from ..realms.handles import StageHandles
from ..store import cas

NORMALIZED_SCHEMA = "gce.chatgpt.normalized.v1"

# Roles as the provider reports them, kept verbatim. Not collapsed.
KNOWN_ROLES = ("user", "assistant", "tool", "system")

_CODE_FENCE = re.compile(r"```")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_WIN_PATH = re.compile(r"(?<![\w/])[A-Za-z]:\\[^\s<>\"'`|]+")
# Relative paths, either separator. The lookbehind deliberately allows a preceding "/"
# so forward-slash paths like docs/plan.md are found, not just bare filenames.
_REL_PATH = re.compile(
    r"(?<![\w])[\w.\-]+(?:[\\/][\w.\-]+)*\.(?:md|py|ps1|json|txt|js|ts|yaml|yml|sql|csv|log|docx|xlsx|pdf)\b"
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalized_dir(store_root: str) -> str:
    return os.path.join(store_root, "DERIVED", "normalized")


def digest_dir(store_root: str) -> str:
    return os.path.join(store_root, "DERIVED", "digests")


def _part_text(part: Any) -> str:
    """Flatten one content part to text without discarding its type."""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    for key in ("text", "transcription", "output", "code", "content"):
        v = part.get(key)
        if isinstance(v, str):
            return v
    return ""


def _part_kind(part: Any) -> str:
    if isinstance(part, str):
        return "text"
    if isinstance(part, dict):
        return part.get("content_type") or "object"
    return type(part).__name__


# Text lives in two different places depending on content_type. Verified against the
# live corpus 2026-09-26 by scanning all 12,849 captured messages:
#   parts-carrying : text, multimodal_text            -> content.parts[]
#   sibling-carrying: code, execution_output, system_error -> content.text
#                     reasoning_recap                 -> content.content
#                     thoughts                        -> content.thoughts[]
# Reading only content.parts silently discarded 2,173 `code` messages (tool call
# payloads and execution outputs, one 13.6 kB) and 1,535 reasoning recaps.
_SIBLING_TEXT_FIELDS = ("text", "content", "summary", "result", "model_set_context",
                        "repo_summary", "structured_context", "transcription", "output")
_SIBLING_LIST_FIELDS = ("thoughts",)
# Fields that are structural rather than content.
_SIBLING_SKIP = ("content_type", "parts", "language", "response_format_name",
                 "source_analysis_msg_id", "tether_id", "name")


def _sibling_text(content: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, int]]:
    """Extract text from content's non-parts fields. Returns (text, fields, char_map)."""
    chunks: List[str] = []
    used: List[str] = []
    sizes: Dict[str, int] = {}
    for field in _SIBLING_TEXT_FIELDS:
        v = content.get(field)
        if isinstance(v, str) and v.strip():
            chunks.append(v)
            used.append(field)
            sizes[field] = len(v)
    for field in _SIBLING_LIST_FIELDS:
        v = content.get(field)
        if isinstance(v, list):
            n = 0
            for item in v:
                s = _part_text(item) if not isinstance(item, str) else item
                if s:
                    chunks.append(s)
                    n += len(s)
            if n:
                used.append(field)
                sizes[field] = n
    return "\n".join(chunks), used, sizes


def normalize_turn(msg: Dict[str, Any]) -> Dict[str, Any]:
    """One provider message -> one canonical turn. Nothing is dropped silently.

    A part whose *content* cannot be recovered is recorded by kind. An empty plain-text
    part is counted separately instead: there is no content to lose, and counting it as
    unextractable made the quality signal fire on every conversation.
    """
    content = msg.get("content") or {}
    parts = content.get("parts") or []
    texts: List[str] = []
    kinds: List[str] = []
    unextractable: List[str] = []
    empty_text_parts = 0
    for p in parts:
        kind = _part_kind(p)
        kinds.append(kind)
        text = _part_text(p)
        if text:
            texts.append(text)
        elif kind in ("text", "string"):
            empty_text_parts += 1
        else:
            unextractable.append(kind)
    sibling_text, sibling_fields, sibling_sizes = _sibling_text(content)
    if sibling_text:
        texts.append(sibling_text)
    text = "\n".join(t for t in texts if t)
    meta = msg.get("metadata") or {}
    author = msg.get("author") or {}
    return {
        "message_id": msg.get("id"),
        "role": author.get("role") or "unknown",
        "role_name": author.get("name"),
        "created_at": msg.get("create_time"),
        "updated_at": msg.get("update_time"),
        "status": msg.get("status"),
        "weight": msg.get("weight"),
        "recipient": msg.get("recipient"),
        "content_type": content.get("content_type"),
        "part_kinds": kinds,
        "sibling_fields_used": sibling_fields,
        "sibling_field_chars": sibling_sizes,
        "text": text,
        "text_chars": len(text),
        "code_blocks": text.count("```") // 2,
        "links": sorted(set(_URL.findall(text))),
        "citations": _citations(text, msg),
        "path_references": sorted(set(_WIN_PATH.findall(text) + _REL_PATH.findall(text))),
        "model_slug": meta.get("model_slug"),
        "has_voice_metadata": bool(
            meta.get("voice_mode_message") or meta.get("bidi_voice_mode_message")
        ),
        "empty_text_parts": empty_text_parts,
        "unextractable_part_kinds": sorted(set(unextractable)),
    }


def _citations(text: str, msg: Dict[str, Any]) -> List[str]:
    """Citations: explicit source markers in the text plus any tool-sourced urls."""
    out = set(_URL.findall(text))
    parts = (msg.get("content") or {}).get("parts") or []
    for p in parts:
        if isinstance(p, dict):
            for key in ("url", "source", "link"):
                v = p.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    out.add(v)
    return sorted(out)


def normalize_capture(doc: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """Raw capture document -> canonical normalized record. Pure; no I/O."""
    convo = doc.get("provider_payload") or {}
    raw_msgs = doc.get("messages") or []
    turns = [normalize_turn(m) for m in raw_msgs if isinstance(m, dict)]
    # Provider order is create_time order (the walk already sorted); keep it, but record
    # whether it is actually monotonic so quality can flag it.
    ordered = [t["created_at"] for t in turns if t.get("created_at") is not None]
    monotonic = all(ordered[i] <= ordered[i + 1] for i in range(len(ordered) - 1))

    return {
        "schema": NORMALIZED_SCHEMA,
        "conversation_id": doc.get("conversation_id") or convo.get("conversation_id"),
        "title": convo.get("title") or "Untitled",
        "canonical_url": doc.get("canonical_url"),
        "project_id": doc.get("project_id") or convo.get("gizmo_id"),
        "create_time": convo.get("create_time"),
        "update_time": convo.get("update_time"),
        "default_model_slug": convo.get("default_model_slug"),
        "is_archived": convo.get("is_archived"),
        "is_temporary_chat": convo.get("is_temporary_chat"),
        "context_truncation_continuation": convo.get("context_truncation_continuation"),
        "turn_count": len(turns),
        "role_counts": _tally(t["role"] for t in turns),
        "turns": turns,
        "ordering_is_monotonic": monotonic,
        "total_text_chars": sum(t["text_chars"] for t in turns),
        "code_block_count": sum(t["code_blocks"] for t in turns),
        "link_count": sum(len(t["links"]) for t in turns),
        "citation_count": sum(len(t["citations"]) for t in turns),
        "path_reference_count": sum(len(t["path_references"]) for t in turns),
        "model_slugs": sorted({t["model_slug"] for t in turns if t.get("model_slug")}),
        "unextractable_part_kinds": sorted(
            {k for t in turns for k in t["unextractable_part_kinds"]}),
        "capture": {
            "capture_status": doc.get("capture_status"),
            "captured_at": doc.get("captured_at"),
            "capture_path": doc.get("capture_path"),
            "pages_walked": (doc.get("stats") or {}).get("pages_walked"),
            "content_types": (doc.get("stats") or {}).get("content_types", []),
            "warnings": doc.get("warnings") or [],
        },
        "provenance": {
            "observation_id": source.get("observation_id"),
            "source_artifact_path": source.get("artifact_path"),
            "source_content_hash": source.get("content_hash"),
            "source_capture_timestamp": source.get("capture_timestamp"),
            "completeness_status": source.get("completeness_status"),
            "normalizer_version": NORMALIZED_SCHEMA,
        },
    }


def _tally(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _latest_sources(handles: StageHandles) -> Dict[str, Dict[str, Any]]:
    conn = handles.connection("EVIDENCE")
    rows = conn.execute(
        "SELECT observation_id, provider_conversation_id, raw_payload_ref, content_hash, "
        "       scrape_timestamp, completeness_status "
        "FROM raw_observation WHERE provider='chatgpt' "
        "ORDER BY scrape_timestamp, rowid"
    ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        out[r["provider_conversation_id"]] = {
            "observation_id": r["observation_id"],
            "artifact_path": r["raw_payload_ref"],
            "content_hash": r["content_hash"],
            "capture_timestamp": r["scrape_timestamp"],
            "completeness_status": r["completeness_status"],
        }
    return out


def normalize_store(store_root: str, only: Optional[List[str]] = None) -> Dict[str, Any]:
    """Normalize every (or the selected) captured conversation.

    Writes a normalized record per conversation to DERIVED/normalized/, a DERIVED
    normalized_view row per record (provenance non-null, per F9), and a bounded digest
    per conversation for sub-agent consumption. EVIDENCE is read-only here.
    """
    ndir = normalized_dir(store_root)
    ddir = digest_dir(store_root)
    os.makedirs(ndir, exist_ok=True)
    os.makedirs(ddir, exist_ok=True)
    written: List[str] = []
    skipped: List[Dict[str, Any]] = []
    generation_id = "norm-" + uuid.uuid4().hex[:12]

    with StageHandles("normalize", store_root) as handles:
        sources = _latest_sources(handles)
        targets = [c for c in (only or sorted(sources)) if c in sources]
        for cid in targets:
            src = sources[cid]
            try:
                payload = get_payload(handles, src["observation_id"])
                raw = json.loads(payload.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - a bad capture is reported, not hidden
                skipped.append({"conversation_id": cid, "reason": f"unreadable: {exc}"})
                continue
            rec = normalize_capture(raw, src)
            body = json.dumps(rec, ensure_ascii=False).encode("utf-8")
            digest = cas.put_blob(store_root, body)
            npath = os.path.join(ndir, f"{cid}.json")
            with open(npath, "wb") as fh:
                fh.write(body)
            write_digest(ddir, cid, rec)
            conn = handles.connection("DERIVED", write=True)
            conn.execute(
                "INSERT INTO normalized_view "
                "(observation_id, generation_id, normalized_text, provenance, created_at) "
                "VALUES (?,?,?,?,?)",
                (src["observation_id"], generation_id, npath,
                 json.dumps({"conversation_id": cid, "normalized_blob": digest,
                             "source_content_hash": src["content_hash"],
                             "source_artifact_path": src["artifact_path"],
                             "schema": NORMALIZED_SCHEMA}),
                 _now()),
            )
            conn.commit()
            written.append(cid)

    return {"generation_id": generation_id, "normalized": written,
            "skipped": skipped, "normalized_dir": ndir, "digest_dir": ddir}


# -- digest: a bounded, honest extract for sub-agents ------------------------
def build_digest(rec: Dict[str, Any], head: int = 12, tail: int = 12) -> str:
    """A bounded read of one conversation for a judging worker.

    Not a summary. It is the head, the tail, the shape, and the pointer to the full
    normalized record, so a worker can classify and summarise without ingesting a
    1,700-turn conversation whole. Middle turns are represented by counts and by the
    longest turns, never silently dropped without saying so.
    """
    turns = rec.get("turns") or []
    lines: List[str] = []
    a = lines.append
    a(f"# DIGEST (bounded extract, not a summary) -- {rec.get('title')}")
    a("")
    a(f"- conversation_id : {rec.get('conversation_id')}")
    a(f"- canonical_url   : {rec.get('canonical_url')}")
    a(f"- project_id      : {rec.get('project_id')}")
    a(f"- created/updated : {rec.get('create_time')} / {rec.get('update_time')}")
    a(f"- default model   : {rec.get('default_model_slug')}")
    a(f"- turns           : {rec.get('turn_count')}  roles={rec.get('role_counts')}")
    a(f"- text chars      : {rec.get('total_text_chars')}   code blocks={rec.get('code_block_count')}"
      f"   links={rec.get('link_count')}   citations={rec.get('citation_count')}")
    a(f"- models seen     : {rec.get('model_slugs')}")
    a(f"- content types   : {(rec.get('capture') or {}).get('content_types')}")
    a(f"- capture         : status={(rec.get('capture') or {}).get('capture_status')}"
      f"  warnings={(rec.get('capture') or {}).get('warnings')}")
    a(f"- provenance      : observation={rec['provenance']['observation_id']}"
      f"  source_hash={rec['provenance']['source_content_hash'][:16]}...")
    a(f"- FULL RECORD     : {normalized_dir_hint(rec)}")
    a("")
    a(f"## Head {min(head, len(turns))} turns")
    a("")
    for t in turns[:head]:
        lines.extend(_turn_block(t))
    if len(turns) > head + tail:
        a(f"## ... {len(turns) - head - tail} middle turns omitted from this digest ...")
        a("")
        a("Longest 5 middle turns by text size:")
        a("")
        middle = sorted(turns[head:len(turns) - tail], key=lambda t: -t["text_chars"])[:5]
        for t in middle:
            lines.extend(_turn_block(t, limit=1200))
    if tail:
        a(f"## Tail {min(tail, len(turns))} turns")
        a("")
        for t in turns[-tail:]:
            lines.extend(_turn_block(t))
    return "\n".join(lines)


def normalized_dir_hint(rec: Dict[str, Any]) -> str:
    return f"DERIVED/normalized/{rec.get('conversation_id')}.json"


def _turn_block(t: Dict[str, Any], limit: int = 2500) -> List[str]:
    body = t["text"] if t["text_chars"] <= limit else t["text"][:limit] + f"\n...[+{t['text_chars']-limit} chars]"
    head = (f"### [{t['role']}] {t.get('message_id') or '-'} "
            f"ct={t.get('created_at')} model={t.get('model_slug') or '-'} "
            f"parts={t.get('part_kinds')} status={t.get('status')}")
    out = [head, ""]
    if body.strip():
        out.append(body)
    else:
        out.append(f"_(no extractable text; part kinds={t.get('part_kinds')}, "
                   f"unextractable={t.get('unextractable_part_kinds')})_")
    if t.get("citations"):
        out.append(f"_citations: {t['citations'][:6]}_")
    if t.get("path_references"):
        out.append(f"_paths: {t['path_references'][:8]}_")
    out.append("")
    return out


def write_digest(ddir: str, cid: str, rec: Dict[str, Any]) -> str:
    p = os.path.join(ddir, f"{cid}.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(build_digest(rec))
    return p
