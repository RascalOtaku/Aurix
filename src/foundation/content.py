"""src/foundation/content.py - real, finished work for the "niche channel or site" money idea: give AURIX a topic (ghost towns,
homelab builds, whatever the owner's channel is about) and it drafts a real piece - title, outline, full body - using the same
frontier-or-free-local pipeline as freelance.py and the upgrade lane. The owner reviews, keeps or discards, then records, edits,
publishes and follows the platform's disclosure rules themselves; AURIX never posts anything anywhere.

The one risk unique to this workstream (unlike code, which either runs or does not): prose can be confidently WRONG about facts -
a ghost town's real history, a date, a technical spec. The drafting prompt requires the model to mark anything it is not certain
of with [VERIFY] rather than inventing specifics with false confidence, and the render always keeps that marker visible.
"""
from __future__ import annotations

import functools
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit, teacher

e = html.escape
MAX_TOPIC_CHARS = 1000
MAX_OUTPUT_TOKENS = 4000
KEEP_PIECES = 200

DRAFTER_SYSTEM = (
    "You are a writer drafting one piece for the owner's own niche channel or site. You are given a topic. Write an engaging, "
    "well-organized piece a real reader would finish. Be factually careful: if a specific claim (a date, a name, a number, a "
    "technical spec) is not something you are confident of, write it as \"[VERIFY: <what you would check>]\" instead of "
    "inventing a plausible-sounding specific - a wrong fact published under the owner's name is worse than an honest gap. "
    'Reply with ONLY a JSON object: {"title": "<=100 chars", "outline": ["<=12 words each", ...], "body": "<the full piece, '
    'plain text with blank lines between paragraphs>", "platform_note": "<=200 chars: anything platform-specific worth knowing '
    '(e.g. disclosure rules, formatting) - or empty if none>"}.'
)


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "content"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def pieces() -> List[dict]:
    return _read(_data() / "pieces.json", [])


def _save(items: List[dict]) -> None:
    _atomic(_data() / "pieces.json", items[-KEEP_PIECES:])


def get(piece_id: str) -> Optional[dict]:
    return next((p for p in pieces() if p["id"] == piece_id), None)


def pending() -> List[dict]:
    return [p for p in pieces() if p["status"] == "review"]


def _call_drafter(topic: str, post: teacher.Post = teacher.default_post,
                   local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> Tuple[Optional[str], str]:
    """Same paid-then-free-local shape as teacher.call_teacher / upgrades._call_engineer / freelance._call_drafter."""
    local = local or functools.partial(teacher._local_fallback, purpose="write", json_mode=True, data_class="internal")   # routed; replies are JSON
    err = "no ANTHROPIC_API_KEY is set"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        body = {"model": teacher.config()["model"], "max_tokens": MAX_OUTPUT_TOKENS, "system": DRAFTER_SYSTEM,
                "messages": [{"role": "user", "content": topic}]}
        try:
            status, data = post(teacher.API_URL, teacher.api_headers(), body, 120)
        except Exception as ex:                                                # noqa: BLE001
            err = f"network error: {type(ex).__name__}"
        else:
            if status == 200:
                text = "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    return text, ""
                err = "empty reply"
            else:
                err = teacher.api_error(status, data)
    text, local_err = local(DRAFTER_SYSTEM, topic, MAX_OUTPUT_TOKENS)
    return (text, "") if text else (None, f"{err}; {local_err}")


def request(topic: str, post: teacher.Post = teacher.default_post,
            local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> str:
    """One drafting attempt. Returns the owner-facing message: the drafted piece (with a preview) or why it failed."""
    topic = " ".join((topic or "").split())
    if len(topic) < 8:
        return "Give me a topic in a sentence, e.g. <code>content: a piece on setting up Home Assistant on a Pi for beginners</code>."
    why = teacher.refuse_reason(topic)
    if why:
        return f"I will not draft that: {why}."
    if len(topic) > MAX_TOPIC_CHARS:
        return f"That topic is {len(topic)} characters (limit {MAX_TOPIC_CHARS}); trim it rather than have me guess what to drop."
    redacted, n_red = teacher.redact(topic)
    text, err = _call_drafter(redacted, post, local)
    if text is None:
        audit.append("content_draft_failed", why=err[:120])
        return f"Could not draft that: {err}."
    from src.foundation.planner import extract_json
    obj = extract_json(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("title"), str) or not obj.get("title") \
            or not isinstance(obj.get("body"), str) or not obj.get("body") or not isinstance(obj.get("outline"), list):
        audit.append("content_draft_failed", why="the reply was not the JSON I asked for")
        return "The draft came back malformed; try rephrasing the topic."
    body = obj["body"][:20000]
    piece = {"id": "c-" + secrets.token_hex(3), "topic": redacted[:500], "title": obj["title"][:100],
              "outline": [str(x)[:80] for x in obj["outline"]][:20], "body": body,
              "platform_note": str(obj.get("platform_note") or "")[:200], "words": len(body.split()),
              "verify_flags": len(re.findall(r"\[VERIFY", body, re.I)), "status": "review", "created": time.time(), "redactions": n_red}
    items = pieces()
    items.append(piece)
    _save(items)
    audit.append("content_drafted", id=piece["id"], title=piece["title"][:80], words=piece["words"], verify_flags=piece["verify_flags"])
    return render(piece)


def render(piece: dict) -> str:
    body = piece["body"]
    preview = body[:2800] + ("\n... (truncated)" if len(body) > 2800 else "")
    lines = [f"📝 <b>{e(piece['title'])}</b> <code>{piece['id']}</code> · {piece['words']} words"]
    if piece["outline"]:
        lines.append("<b>Outline:</b> " + " · ".join(e(o) for o in piece["outline"]))
    if piece["verify_flags"]:
        lines.append(f"⚠️ {piece['verify_flags']} claim(s) marked <code>[VERIFY]</code> - check those before publishing.")
    lines.append(f"<pre>{e(preview)}</pre>")
    if piece.get("platform_note"):
        lines.append(f"<i>{e(piece['platform_note'])}</i>")
    lines.append(f"<code>yes {piece['id']}</code> keeps it (recording, editing and publishing stay yours) · <code>no {piece['id']}</code> discards it")
    return "\n".join(lines)


def approve(piece_id: str) -> str:
    piece = get(piece_id)
    if piece is None or piece["status"] != "review":
        return "No draft waiting with that id."
    items = pieces()
    for p in items:
        if p["id"] == piece_id:
            p["status"] = "kept"
    _save(items)
    audit.append("content_kept", id=piece_id)
    return f"✅ Kept <code>{piece_id}</code>. Recording/editing/publishing (and any disclosure rules) are yours."


def decline(piece_id: str) -> str:
    piece = get(piece_id)
    if piece is None or piece["status"] != "review":
        return "No draft waiting with that id."
    items = pieces()
    for p in items:
        if p["id"] == piece_id:
            p["status"] = "discarded"
    _save(items)
    audit.append("content_discarded", id=piece_id)
    return f"🗑️ Discarded <code>{piece_id}</code>."


def status_text() -> str:
    items = pieces()
    if not items:
        return "📝 No content drafts yet. <code>content: &lt;topic&gt;</code> drafts one."
    icon = {"review": "🕐", "kept": "✅", "discarded": "🗑️"}
    lines = ["📝 <b>Content drafts</b>"]
    for p in items[-10:][::-1]:
        lines.append(f"{icon.get(p['status'], '•')} <code>{p['id']}</code> {e(p['title'])} - {p['status']}")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    items = pieces()
    return {"pieces": [{"id": p["id"], "title": p["title"], "words": p["words"], "verify_flags": p["verify_flags"],
                        "status": p["status"], "created": p["created"]} for p in items[-15:][::-1]]}
