"""src/foundation/freelance.py - real, finished work for the "freelance small automation jobs" money idea: paste a client's brief
(from Upwork, Fiverr, an email - whatever), AURIX drafts an actual working script for it, using the same frontier-or-free-local
pipeline as the upgrade lane. You review the code, decide whether it is good enough, and deliver it yourself.

What AURIX does not do: create a profile on any platform, bid on or accept a job, submit anything, or promise a price. It also does
not know what a given platform's AI-disclosure rules are - check the one you are using (research on this varies by platform: some
require disclosure, some only if the client asks, some are silent). No pay estimate is auto-logged here either, unlike transcription:
job scope varies too much to guess a number honestly. `earned <amount> freelance_scripts` logs it once you actually get paid.
"""
from __future__ import annotations

import ast
import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit, teacher

e = html.escape
MAX_BRIEF_CHARS = 4000
MAX_OUTPUT_TOKENS = 4000
JOB_RX = re.compile(r"^f-[0-9a-f]{6}$")
KEEP_JOBS = 200
LEADS_URL = "https://remoteok.com/api?tags=python"                    # public, no auth, no account: reading what is already posted
MAX_LEAD_ATTEMPTS = 3
KEEP_SEEN_LEADS = 500
# RemoteOK is mostly full-time salaried roles, not contract gigs: skip titles that are obviously not a quick freelance script job
_NOT_A_SMALL_JOB_RX = re.compile(r"\b(senior|sr\.?|staff|principal|lead|architect|manager|director|head\s+of|vp|chief|founder|"
                                  r"full[\s-]?time)\b", re.I)

DRAFTER_SYSTEM = (
    "You are a freelance developer completing a small paid job. You are given the client's brief. Write a COMPLETE, WORKING "
    "solution: a single script or small tool that does exactly what was asked. Prefer the Python standard library; if the brief "
    "clearly needs something else, say so in the explanation rather than silently assuming a library is installed. Keep it as "
    "short as correctness allows, with brief comments only where the logic is not obvious. Reply with ONLY a JSON object: "
    '{"title": "<=70 chars", "explanation": "<=400 chars: what it does and how to run it>", "filename": "<a sensible file name, '
    'e.g. rename_by_date.py>", "code": "<the complete file content>"}.'
)


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "freelance"


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


def jobs() -> List[dict]:
    return _read(_data() / "jobs.json", [])


def _save(items: List[dict]) -> None:
    _atomic(_data() / "jobs.json", items[-KEEP_JOBS:])


def get(job_id: str) -> Optional[dict]:
    return next((j for j in jobs() if j["id"] == job_id), None)


def pending() -> List[dict]:
    return [j for j in jobs() if j["status"] == "review"]


# ---------------------------------------------------------------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------------------------------------------------------------

def _call_drafter(brief: str, post: teacher.Post = teacher.default_post,
                   local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> Tuple[Optional[str], str]:
    """Same shape as teacher.call_teacher / upgrades._call_engineer: try the paid frontier model, fall back to the free local one."""
    local = local or teacher._local_fallback
    err = "no ANTHROPIC_API_KEY is set"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        body = {"model": teacher.config()["model"], "max_tokens": MAX_OUTPUT_TOKENS, "system": DRAFTER_SYSTEM,
                "messages": [{"role": "user", "content": brief}]}
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
    text, local_err = local(DRAFTER_SYSTEM, brief, MAX_OUTPUT_TOKENS)
    return (text, "") if text else (None, f"{err}; {local_err}")


def request(brief: str, post: teacher.Post = teacher.default_post,
            local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> str:
    """One drafting attempt. Returns the owner-facing message: the drafted job (with a preview of the code) or why it failed."""
    brief = " ".join((brief or "").split())
    if len(brief) < 8:
        return "Describe the job in a sentence or two, e.g. <code>freelance: write a script that renames files by their EXIF date</code>."
    why = teacher.refuse_reason(brief)
    if why:
        return f"I will not draft that: {why}."
    if len(brief) > MAX_BRIEF_CHARS:
        return f"That brief is {len(brief)} characters (limit {MAX_BRIEF_CHARS}); trim it rather than have me guess what to drop."
    redacted, n_red = teacher.redact(brief)
    text, err = _call_drafter(redacted, post, local)
    if text is None:
        audit.append("freelance_draft_failed", why=err[:120])
        return f"Could not draft that: {err}."
    from src.foundation.planner import extract_json
    obj = extract_json(text)
    if not isinstance(obj, dict) or not all(isinstance(obj.get(k), str) and obj.get(k) for k in ("title", "explanation", "filename", "code")):
        audit.append("freelance_draft_failed", why="the reply was not the JSON I asked for")
        return "The draft came back malformed; try rephrasing the brief."
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", obj["filename"])[:60] or "job.py"
    syntax_note = ""
    if filename.endswith(".py"):
        try:
            ast.parse(obj["code"])
        except SyntaxError as ex:
            syntax_note = f" ⚠️ It does not parse ({ex.msg} at line {ex.lineno}) - read it carefully before delivering."
    job = {"id": "f-" + secrets.token_hex(3), "brief": redacted[:1000], "title": obj["title"][:80], "explanation": obj["explanation"][:400],
           "filename": filename, "code": obj["code"][:20000], "status": "review", "created": time.time(), "redactions": n_red}
    items = jobs()
    items.append(job)
    _save(items)
    audit.append("freelance_drafted", id=job["id"], title=job["title"][:80], chars=len(job["code"]))
    return render(job) + syntax_note


def render(job: dict) -> str:
    code = job["code"]
    body = code[:3200] + ("\n... (truncated)" if len(code) > 3200 else "")
    return (f"🧰 <b>{e(job['title'])}</b> <code>{job['id']}</code> · <code>{e(job['filename'])}</code>\n{e(job['explanation'])}\n"
            f"<pre>{e(body)}</pre>\n<code>yes {job['id']}</code> keeps it (you deliver it yourself) · <code>no {job['id']}</code> discards it")


def approve(job_id: str) -> str:
    job = get(job_id)
    if job is None or job["status"] != "review":
        return "No draft waiting with that id."
    items = jobs()
    for j in items:
        if j["id"] == job_id:
            j["status"] = "kept"
    _save(items)
    audit.append("freelance_kept", id=job_id)
    return f"✅ Kept <code>{job_id}</code>. Delivering it (and any disclosure the platform asks for) is yours."


def decline(job_id: str) -> str:
    job = get(job_id)
    if job is None or job["status"] != "review":
        return "No draft waiting with that id."
    items = jobs()
    for j in items:
        if j["id"] == job_id:
            j["status"] = "discarded"
    _save(items)
    audit.append("freelance_discarded", id=job_id)
    return f"🗑️ Discarded <code>{job_id}</code>."


# ---------------------------------------------------------------------------------------------------------------------------------
# finding a real lead (no login, no bidding, no messaging - reading a public listing feed, same as a person browsing it)
# ---------------------------------------------------------------------------------------------------------------------------------

def _seen_leads() -> List[str]:
    return _read(_data() / "seen_leads.json", [])


def _mark_seen(job_id: str) -> None:
    seen = _seen_leads()
    seen.append(job_id)
    _atomic(_data() / "seen_leads.json", seen[-KEEP_SEEN_LEADS:])


def _fetch_leads(fetch: Optional[Callable[[], bytes]] = None) -> Tuple[List[dict], str]:
    if fetch is None:
        def fetch():
            req = urllib.request.Request(LEADS_URL, headers={"User-Agent": "AURIX/1.0 (freelance lead search; public listing read)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
    try:
        data = json.loads(fetch().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError) as ex:
        return [], f"could not reach it: {type(ex).__name__}"
    except ValueError:
        return [], "the feed did not return valid JSON"
    if not isinstance(data, list):
        return [], "unexpected response shape"
    return [d for d in data if isinstance(d, dict) and d.get("id") and d.get("position")], ""


def find_lead(post: teacher.Post = teacher.default_post, local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None,
              fetch: Optional[Callable[[], bytes]] = None) -> str:
    """Checks a real, public, no-login job feed for a small python/scripting listing you have not seen yet, and drafts a solution
    for it exactly as if you had pasted the brief yourself. AURIX never creates an account, bids, or messages anyone - the only
    thing left for you is a yes/no on the draft, plus the listing's own link if you want to actually apply."""
    leads, err = _fetch_leads(fetch)
    if err:
        return f"Could not check for real leads right now: {err}."
    seen = set(_seen_leads())
    tried = 0
    for d in leads:
        job_id = str(d["id"])
        if job_id in seen:
            continue
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(d.get("description") or ""))).strip()
        title = str(d.get("position") or "").strip()
        if len(desc) < 40 or _NOT_A_SMALL_JOB_RX.search(title):
            continue
        _mark_seen(job_id)
        tried += 1
        brief = f"{title} at {str(d.get('company') or 'a company')}. {desc}"[:MAX_BRIEF_CHARS]
        msg = request(brief, post, local)
        if msg.startswith("🧰"):
            url = str(d.get("url") or "")
            lead_line = f"🔎 Found a real, public listing: <b>{e(title)}</b>" + (f" at {e(str(d['company']))}" if d.get("company") else "")
            return lead_line + (f"\n{e(url)}" if url else "") + "\n\n" + msg
        if tried >= MAX_LEAD_ATTEMPTS:
            break
    return ("Checked a real remote-jobs feed - nothing new and small enough to draft right now. "
            "Try again later, or send me a brief yourself: <code>freelance: &lt;job brief&gt;</code>.")


def status_text() -> str:
    items = jobs()
    if not items:
        return "🧰 No freelance drafts yet. <code>freelance: &lt;job brief&gt;</code> drafts one."
    icon = {"review": "🕐", "kept": "✅", "discarded": "🗑️"}
    lines = ["🧰 <b>Freelance drafts</b>"]
    for j in items[-10:][::-1]:
        lines.append(f"{icon.get(j['status'], '•')} <code>{j['id']}</code> {e(j['title'])} - {j['status']}")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    items = jobs()
    return {"jobs": [{"id": j["id"], "title": j["title"], "filename": j["filename"], "status": j["status"], "created": j["created"]}
                     for j in items[-15:][::-1]]}
