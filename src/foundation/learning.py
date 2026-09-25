"""src/foundation/learning.py - "send AURIX something to read" (articles, write-ups, pasted text - the reading/absorption
counterpart to repos.py's "send AURIX a repo link"). Same shape: fetch it read-only, summarize it (frontier-or-local, same
fallback as everywhere else), show the owner a verdict, store it inert only on a yes. Nothing fetched is ever executed.

A video link (YouTube etc.) is tracked by title/URL only - actually transcribing video isn't built (that would piggyback on
transcription.py's local Whisper someday, but downloading video is a bigger, separate piece of infrastructure not built yet).

Safety, since this accepts ANY url (repos.py is scoped to github.com, this is not):
  * only http/https, and the resolved host must not be a private/loopback/link-local address (no reaching internal services)
  * size-capped read, only text/html or text/plain content-types are parsed as an article
  * the same credential-file/secret refusal and redaction as teacher.py before anything is sent to a model
"""
from __future__ import annotations

import html as html_lib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit, teacher

e = html_lib.escape
MAX_FETCH_BYTES = 3 * 1024 * 1024
MAX_TEXT_CHARS = 40000
MAX_OUTPUT_TOKENS = 1200
KEEP = 200
_URL_RX = re.compile(r"^https?://", re.I)
_VIDEO_RX = re.compile(r"(?:youtube\.com|youtu\.be|vimeo\.com)", re.I)
_TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RX = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)

SUMMARY_SYSTEM = (
    "You are helping the owner decide whether something is worth keeping in their reading library. You are given the title "
    "and extracted text of one article or piece of writing. Reply with ONLY a JSON object: "
    '{"summary": "<=400 chars: what it is about", "takeaway": "<=200 chars: the single most useful or interesting thing in '
    'it", "category": "one of: tech, story, other"}.'
)


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "learning"


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


def items() -> List[dict]:
    return _read(_data() / "items.json", [])


def _save(rows: List[dict]) -> None:
    _atomic(_data() / "items.json", rows[-KEEP:])


def get(item_id: str) -> Optional[dict]:
    return next((r for r in items() if r["id"] == item_id), None)


def pending() -> List[dict]:
    return [r for r in items() if r["status"] == "review"]


def library() -> List[dict]:
    return [r for r in items() if r["status"] == "kept"]


# ---------------------------------------------------------------------------------------------------------------------------------
# fetching (SSRF-guarded: this accepts any url, unlike repos.py which is scoped to github.com)
# ---------------------------------------------------------------------------------------------------------------------------------

def _is_safe_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for family, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def _strip_html(raw: str) -> Tuple[str, str]:
    """(title, plain_text). A crude but safe stripper: no script/style bodies, tags removed, entities unescaped."""
    tm = _TITLE_RX.search(raw)
    title = html_lib.unescape(re.sub(r"\s+", " ", tm.group(1))).strip()[:150] if tm else ""
    text = _TAG_RX.sub(" ", raw)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return title, text.strip()


def _fetch(url: str) -> Tuple[Optional[str], Optional[str], str]:
    """(title, text, error). error is "" on success."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, None, "only http/https links are supported"
    if not _is_safe_host(parsed.hostname):
        return None, None, "that host does not resolve to a public address"
    req = urllib.request.Request(url, headers={"User-Agent": "AURIX/1.0 (personal reading list; read-only)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ("text/html", "text/plain", ""):
                return None, None, f"that content type ({ctype or 'unknown'}) is not something I read as an article"
            data = r.read(MAX_FETCH_BYTES + 1)
    except urllib.error.HTTPError as ex:
        return None, None, f"the site answered HTTP {ex.code}"
    except (urllib.error.URLError, OSError) as ex:
        return None, None, f"could not fetch it: {type(ex).__name__}"
    if len(data) > MAX_FETCH_BYTES:
        return None, None, f"that page is over the {MAX_FETCH_BYTES // 1_000_000} MB limit"
    raw = data.decode("utf-8", errors="replace")
    title, text = _strip_html(raw) if "<html" in raw.lower() or "<body" in raw.lower() or "<title" in raw.lower() else ("", raw.strip())
    return (title or parsed.hostname), text, ""


# ---------------------------------------------------------------------------------------------------------------------------------
# summarizing (same paid-then-local-fallback shape as freelance.py / content.py)
# ---------------------------------------------------------------------------------------------------------------------------------

def _call_summarizer(text: str, post: teacher.Post = teacher.default_post,
                      local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> Tuple[Optional[str], str]:
    local = local or teacher._local_fallback
    err = "no ANTHROPIC_API_KEY is set"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        body = {"model": teacher.config()["model"], "max_tokens": MAX_OUTPUT_TOKENS, "system": SUMMARY_SYSTEM,
                "messages": [{"role": "user", "content": text}]}
        try:
            status, data = post(teacher.API_URL, teacher.api_headers(), body, 90)
        except Exception as ex:                                                # noqa: BLE001
            err = f"network error: {type(ex).__name__}"
        else:
            if status == 200:
                out = "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text").strip()
                if out:
                    return out, ""
                err = "empty reply"
            else:
                err = teacher.api_error(status, data)
    out, local_err = local(SUMMARY_SYSTEM, text, MAX_OUTPUT_TOKENS)
    return (out, "") if out else (None, f"{err}; {local_err}")


def request(source: str, tag: str = "", post: teacher.Post = teacher.default_post,
            local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> str:
    source = (source or "").strip()
    if len(source) < 4:
        return "Send a link, or paste the text itself: <code>learn: https://...</code> or <code>learn: &lt;paste text&gt;</code>."
    if _URL_RX.match(source):
        if _VIDEO_RX.search(source):
            row = {"id": "n-" + secrets.token_hex(3), "kind": "video", "source": source[:500], "title": source[:150],
                   "summary": "A video link - AURIX does not transcribe video yet, so this is tracked by title/URL only.",
                   "takeaway": "", "category": "video", "status": "review", "created": time.time()}
            items_ = items()
            items_.append(row)
            _save(items_)
            audit.append("learning_added", id=row["id"], kind="video")
            return render(row)
        title, text, err = _fetch(source)
        if err:
            audit.append("learning_fetch_failed", why=err[:120])
            return f"Could not fetch that: {err}."
    else:
        title, text = (source.splitlines() or [""])[0][:80], source
    why = teacher.refuse_reason(text)
    if why:
        return f"I will not absorb that: {why}."
    text = text[:MAX_TEXT_CHARS]
    redacted, n_red = teacher.redact(text)
    reply, err = _call_summarizer(redacted, post, local)
    category = tag.strip().lower() if tag.strip().lower() in ("tech", "story", "other") else None
    summary_obj: Dict[str, Any] = {}
    if reply is not None:
        from src.foundation.planner import extract_json
        summary_obj = extract_json(reply) or {}
    row = {"id": "n-" + secrets.token_hex(3), "kind": "article" if _URL_RX.match(source) else "text",
           "source": source[:500] if _URL_RX.match(source) else redacted[:300],
           "title": (title or "Untitled")[:150],
           "summary": str(summary_obj.get("summary") or (f"Could not summarize it: {err}" if reply is None else "")).strip()[:400] or redacted[:300],
           "takeaway": str(summary_obj.get("takeaway") or "").strip()[:200],
           "category": category or str(summary_obj.get("category") or "other").strip().lower(),
           "text": redacted, "status": "review", "created": time.time(), "redactions": n_red}
    items_ = items()
    items_.append(row)
    _save(items_)
    audit.append("learning_added", id=row["id"], kind=row["kind"], category=row["category"])
    return render(row)


def render(row: dict) -> str:
    icon = {"tech": "\U0001F4BB", "story": "\U0001F4D6", "video": "\U0001F3A5", "other": "\U0001F4C4"}.get(row["category"], "\U0001F4C4")
    lines = [f"{icon} <b>{e(row['title'])}</b> <code>{row['id']}</code> - {e(row['category'])}"]
    if row.get("source", "").startswith(("http://", "https://")):
        lines.append(e(row["source"]))
    lines.append(e(row["summary"]))
    if row.get("takeaway"):
        lines.append(f"<i>Best takeaway: {e(row['takeaway'])}</i>")
    lines.append(f"<code>yes {row['id']}</code> keeps it in your library · <code>no {row['id']}</code> discards it")
    return "\n".join(lines)


def approve(item_id: str) -> str:
    row = get(item_id)
    if row is None or row["status"] != "review":
        return "No item waiting with that id."
    rows = items()
    for r in rows:
        if r["id"] == item_id:
            r["status"] = "kept"
    _save(rows)
    audit.append("learning_kept", id=item_id)
    return f"✅ Kept <code>{item_id}</code> in your library."


def decline(item_id: str) -> str:
    row = get(item_id)
    if row is None or row["status"] != "review":
        return "No item waiting with that id."
    rows = items()
    for r in rows:
        if r["id"] == item_id:
            r["status"] = "discarded"
            r["text"] = ""                                                    # no reason to keep the content of something declined
    _save(rows)
    audit.append("learning_discarded", id=item_id)
    return f"\U0001F5D1️ Discarded <code>{item_id}</code>."


def status_text() -> str:
    lib = library()
    pend = pending()
    if not lib and not pend:
        return "\U0001F4DA No reading library yet. <code>learn: &lt;link or pasted text&gt;</code> to add something."
    lines = ["\U0001F4DA <b>Reading library</b>"]
    if pend:
        lines.append(f"{len(pend)} waiting for your yes/no.")
    for r in sorted(lib, key=lambda x: -x["created"])[:10]:
        lines.append(f"• {e(r['title'])} <code>{r['id']}</code> - {e(r['category'])}")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    rows = items()
    return {"pending": len(pending()), "kept": len(library()),
            "items": [{"id": r["id"], "title": r["title"], "category": r["category"], "status": r["status"], "created": r["created"]}
                      for r in sorted(rows, key=lambda x: -x["created"])[:15]]}
