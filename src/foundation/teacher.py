"""src/foundation/teacher.py - frontier-as-teacher: a strong model writes the GUIDANCE a small local model was missing.

Owner decision 2026-09-20: use a frontier model to close the gap, but as a teacher, not a crutch. The flow:

    a task the local model fails  ->  (capped, redacted) one question to Claude  ->  a short generalisable LESSON (pending)
    ->  the lesson is A/B-tested on the eval task that failed  ->  the owner approves it  ->  it is injected into later prompts.

So each frontier call buys permanent local capability instead of a one-off answer, and nothing the frontier says changes behaviour
until a test showed it helped AND the owner approved it (hash-pinned; a lesson edited afterwards is retired, like a forged skill).

Guard rails (all code, none of it the model's opinion):
  * OFF by default. Needs `teacher on <calls per day>`; the daily cap is enforced here. With ANTHROPIC_API_KEY set and funded it asks the frontier
    model; with no key, or the key's credit balance empty, or the key refused, it falls back to AURIX's own free local model (self-hosted, $0)
    instead of blocking - the guidance is weaker but the lesson pipeline (parse, A/B test, owner approval) is unchanged either way.
  * Only the task description, the local model's own attempt and a plain-words failure class are sent. NEVER the exam's hidden cases
    (a lesson must generalise, not memorise), never files, credentials, missions' private data. Everything is redacted first, and
    anything that looks like a credential FILE or a private key is refused outright rather than redacted.
  * Input is capped (never truncated silently: over the cap = refused); the reply is parsed as JSON, clamped, and stored as `pending`.
  * Every call is audited (`teacher_called`: model, sizes, redaction count; never the text) and announced to the owner.
"""
from __future__ import annotations

import functools
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_INPUT_CHARS = 8000
MAX_OUTPUT_TOKENS = 700
MAX_LESSON_LINE = 300
MAX_LESSON_LINES = 6
MAX_INJECTED_CHARS = 1500
MAX_ACTIVE_LESSONS = 40
CALL_TIMEOUT = 90

Post = Callable[[str, Dict[str, str], dict, float], Tuple[int, dict]]


# ---------------------------------------------------------------------------------------------------------------------------------
# config + budget
# ---------------------------------------------------------------------------------------------------------------------------------

def data_root() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data"


def _config_path() -> Path:
    return data_root() / "teacher.json"


def _usage_path() -> Path:
    return data_root() / "teacher_usage.json"


def lessons_dir() -> Path:
    return data_root() / "lessons"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def config() -> Dict[str, Any]:
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {"enabled": bool(raw.get("enabled", False)), "daily_calls": max(0, int(raw.get("daily_calls", 0) or 0)),
            "model": os.environ.get("AURIX_TEACHER_MODEL", DEFAULT_MODEL), "key_present": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())}


def set_config(enabled: bool, daily_calls: int = 0) -> str:
    _atomic(_config_path(), {"enabled": bool(enabled), "daily_calls": max(0, min(int(daily_calls), 50))})
    audit.append("teacher_set", enabled=bool(enabled), daily_calls=max(0, min(int(daily_calls), 50)))
    return status_text()


WORKSPACE_RX = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
WORKSPACE_HINT = ("Your Anthropic API key is not scoped to a workspace, so every request needs your workspace id. Send me <code>workspace wrkspc_...</code> "
                  "(Anthropic console -> Settings -> Workspaces -> copy the id; it is an id, not a secret), or create a key inside a workspace.")


def workspace_id() -> str:
    """The Anthropic workspace id some org keys require (header anthropic-workspace-id): env ANTHROPIC_WORKSPACE_ID, else what the owner sent with `workspace <id>`."""
    v = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if v:
        return v
    try:
        return str(json.loads((data_root() / "anthropic.json").read_text(encoding="utf-8")).get("workspace_id", "")).strip()
    except (OSError, ValueError):
        return ""


def set_workspace(value: str) -> str:
    value = (value or "").strip()
    if value.lower() in ("clear", "none", "off"):
        _atomic(data_root() / "anthropic.json", {})
        audit.append("anthropic_workspace_set", set=False)
        return "Cleared the workspace id."
    if not WORKSPACE_RX.match(value):
        return "That does not look like a workspace id (letters, digits, - and _; from the Anthropic console)."
    _atomic(data_root() / "anthropic.json", {"workspace_id": value})
    audit.append("anthropic_workspace_set", set=True)
    return "✅ Saved your workspace id. The teacher and the upgrade lane will use it on their next call."


def api_headers() -> Dict[str, str]:
    h = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": API_VERSION, "content-type": "application/json"}
    ws = workspace_id()
    if ws:
        h["anthropic-workspace-id"] = ws
    return h


def api_error(status: int, data: Any) -> str:
    msg = ((data or {}).get("error") or {}).get("message", "") if isinstance(data, dict) else ""
    if status == 400 and "workspace" in msg.lower():
        return "the API needs a workspace id. " + re.sub(r"<[^>]+>", "", WORKSPACE_HINT)
    if "credit balance" in msg.lower():
        return "the API credit balance is empty. Add credits under Plans & Billing in the Anthropic console (the API is billed separately from a Claude subscription)."
    if status in (401, 403):
        return f"the API refused the key (HTTP {status}): it may be revoked or from another workspace."
    return f"the API answered HTTP {status}" + (f": {msg[:160]}" if msg else "")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_today() -> Dict[str, int]:
    try:
        return dict(json.loads(_usage_path().read_text(encoding="utf-8")).get(_today(), {}))
    except (OSError, ValueError):
        return {}


def _record_usage(in_chars: int, out_chars: int) -> None:
    try:
        allu = json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        allu = {}
    day = allu.setdefault(_today(), {"calls": 0, "in_chars": 0, "out_chars": 0})
    day["calls"] += 1
    day["in_chars"] += in_chars
    day["out_chars"] += out_chars
    for k in sorted(allu)[:-30]:                                # keep a month
        allu.pop(k, None)
    _atomic(_usage_path(), allu)


def can_call() -> Tuple[bool, str]:
    from src.foundation import shards
    if shards.is_paused("teacher"):
        return False, "The teacher is paused (`resume teacher`)."
    c = config()
    if not c["enabled"] or c["daily_calls"] <= 0:
        return False, "The teacher is off. `teacher on <calls per day>` turns it on."
    used = usage_today().get("calls", 0)
    if used >= c["daily_calls"]:
        return False, f"Today's teacher budget is used up ({used}/{c['daily_calls']} calls). It resets tomorrow, or raise it with `teacher on <n>`."
    return True, ""


def status_text() -> str:
    c, u = config(), usage_today()
    state = "ON" if c["enabled"] and c["daily_calls"] else "OFF"
    lines = [f"🎓 <b>Teacher is {state}</b> · model <code>{html.escape(c['model'])}</code> · key {'present' if c['key_present'] else 'NOT set'}"
             f" · today {u.get('calls', 0)}/{c['daily_calls']} calls"]
    ls = all_lessons()
    if ls:
        counts: Dict[str, int] = {}
        for x in ls:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        lines.append("Lessons: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + " (<code>lessons</code>)")
    lines.append("<i>It only ever sees a task description, the local model's own attempt and a plain failure class - never hidden test "
                 "cases, files or credentials - after redaction. <code>teacher on 5</code> / <code>teacher off</code>.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------------------
# what may leave the machine
# ---------------------------------------------------------------------------------------------------------------------------------

_REDACTIONS = [
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "[telegram-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[api-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws-key]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[slack-token]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[google-key]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|credential)s?(\s*[:=]\s*)(\S+)"), r"\1\2[redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[hex]"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"), "[blob]"),
    (re.compile(r"/home/[A-Za-z0-9_.-]+"), "~"),
    (re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9_. -]+"), "~"),
]
_REFUSE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bvault\.env\b|(?<![\w])\.env\b|\bid_(?:rsa|ed25519|ecdsa)\b|\bauth\.json\b|\.ssh/", re.I)


def redact(text: str) -> Tuple[str, int]:
    n = 0
    for rx, sub in _REDACTIONS:
        text, k = rx.subn(sub, text)
        n += k
    return text, n


def refuse_reason(text: str) -> Optional[str]:
    m = _REFUSE.search(text or "")
    return f"it mentions a credential file or private key ('{m.group(0)}')" if m else None


TEACHER_SYSTEM = (
    "You are a senior engineer coaching a SMALL local language model (a 3-8B model) that just failed a task. You will be given the task, "
    "the model's own attempt and a plain-words description of how it failed. Write guidance that lets the small model do this KIND of "
    "task correctly next time. Rules: be general (no answers to this specific task, no hard-coded values from it); be concrete "
    "(checks to perform, mistakes to avoid, a tiny pattern if useful); short. Reply with ONLY a JSON object: "
    '{"title": "<=60 chars", "diagnosis": "<one sentence: what went wrong>", "guidance": ["<rule>", ... up to 6, each <=250 chars], '
    '"applies_when": ["<keywords such as python, validation, dates, parsing>", ...]}')


def build_request(kind: str, request_text: str, failure_class: str, attempt: str = "") -> Tuple[str, Optional[str], int]:
    """(user_text, refusal_reason, redaction_count). The user text is what would be sent; refusal_reason is set if it must not be."""
    parts = [f"Task kind: {kind}", f"The task (as given to the small model):\n{request_text.strip()}",
             f"How it failed: {failure_class.strip()}"]
    if attempt.strip():
        parts.append("The small model's attempt:\n" + attempt.strip())
    raw = "\n\n".join(parts)
    why = refuse_reason(raw)
    if why:
        return "", why, 0
    text, n = redact(raw)
    if len(text) > MAX_INPUT_CHARS:
        return "", f"the request is {len(text)} characters (limit {MAX_INPUT_CHARS}); not truncating code silently", n
    return text, None, n


def default_post(url: str, headers: Dict[str, str], body: dict, timeout: float) -> Tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {}


def _local_fallback(system: str, user_text: str, max_tokens: int = 1500, purpose: str = "general",
                    json_mode: Optional[bool] = None, data_class: str = "private") -> Tuple[Optional[str], str]:
    """The free, self-hosted models, used whenever the paid frontier path is unavailable. Since 2026-09-29 this goes through the
    Worker Registry & Router (workers.py): the right local worker for the PURPOSE (a coder model for code edits, with enforced JSON
    where the caller parses JSON), private data kept on local/LAN workers, every call audited. Only if the registry cannot be read at
    all (no database, e.g. a host script) does it fall back to the old single default model. Never raises."""
    reason = ""
    try:
        from src.foundation import workers
        text, info = workers.ask(purpose, system, user_text, data_class=data_class, max_tokens=max_tokens, json_mode=json_mode)
        if text and text.strip():
            return text.strip(), ""
        reason = info.get("reason") or "the routed worker gave an empty reply"
        if info.get("tried") or "no enabled worker" not in reason:
            return None, f"the local workers failed: {reason}"           # real workers were tried: say so, don't mask it
        if any(w.backend != "executor" for w in workers.registry()):         # model workers exist but none may take this
            return None, f"the local workers could not take this: {reason}"
    except Exception as e:                                                      # noqa: BLE001 - fall through to the legacy path
        reason = f"router error: {type(e).__name__}"
    try:
        import asyncio

        from src.foundation import llm_bridge
        text = asyncio.run(llm_bridge.default_llm(system, user_text, max_tokens=max_tokens))
    except Exception as e:                                                      # noqa: BLE001 - best-effort fallback, never fatal
        return None, f"the local model failed too: {type(e).__name__}" + (f" ({reason})" if reason else "")
    text = (text or "").strip()
    return (text, "") if text else (None, "the local model gave an empty reply")


def call_teacher(user_text: str, post: Post = default_post, local: Callable[[str, str], Tuple[Optional[str], str]] = None) -> Tuple[Optional[str], str]:
    """One capped call to the frontier model; if a key isn't configured, its credit balance is empty, it's refused, or the network
    is down, falls back to the free local model instead of failing outright (self-hosted: this never has to cost money). Never raises."""
    local = local or functools.partial(_local_fallback, purpose="review", json_mode=True, data_class="internal")   # lessons are JSON
    c = config()
    err = "no ANTHROPIC_API_KEY is set"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        body = {"model": c["model"], "max_tokens": MAX_OUTPUT_TOKENS, "system": TEACHER_SYSTEM,
                "messages": [{"role": "user", "content": user_text}]}
        try:
            status, data = post(API_URL, api_headers(), body, CALL_TIMEOUT)
        except Exception as e:
            err = f"network error: {type(e).__name__}"
        else:
            if status == 200:
                text = "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    return text, ""
                err = "empty reply"
            else:
                err = api_error(status, data)
    text, local_err = local(TEACHER_SYSTEM, user_text, MAX_OUTPUT_TOKENS)
    return (text, "") if text else (None, f"{err}; {local_err}")


# ---------------------------------------------------------------------------------------------------------------------------------
# lessons
# ---------------------------------------------------------------------------------------------------------------------------------

_INJECTION = re.compile(r"(?i)ignore (all |any )?(previous|prior|above)|disregard (the )?(system|previous)|you are now|reveal .*(prompt|key|secret)|"
                        r"\b(curl|wget|rm -rf|sudo)\b|https?://")


def parse_lesson(text: str) -> Optional[Dict[str, Any]]:
    from src.foundation.planner import extract_json
    obj = extract_json(text or "")
    if not isinstance(obj, dict):
        return None
    lines = [re.sub(r"\s+", " ", str(x)).strip()[:MAX_LESSON_LINE] for x in (obj.get("guidance") or []) if isinstance(x, (str, int, float))]
    lines = [x for x in lines if x and not _INJECTION.search(x)][:MAX_LESSON_LINES]
    if not lines:
        return None
    kw = [re.sub(r"[^a-z0-9_ -]", "", str(k).lower()).strip()[:30] for k in (obj.get("applies_when") or []) if isinstance(k, str)]
    return {"title": re.sub(r"\s+", " ", str(obj.get("title") or "lesson"))[:60], "diagnosis": re.sub(r"\s+", " ", str(obj.get("diagnosis") or ""))[:240],
            "guidance": lines, "applies_when": [k for k in kw if k][:8]}


def lesson_text(les: Dict[str, Any]) -> str:
    return "\n".join(f"- {g}" for g in les["guidance"])


def _digest(les: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"guidance": les["guidance"], "applies_when": les["applies_when"]}, sort_keys=True).encode()).hexdigest()


def _path(lid: str) -> Path:
    return lessons_dir() / f"{lid}.json"


def save_lesson(les: Dict[str, Any]) -> Dict[str, Any]:
    les["sha256"] = _digest(les)
    _atomic(_path(les["id"]), les)
    return les


def load_lesson(lid: str) -> Optional[Dict[str, Any]]:
    if not re.fullmatch(r"l-[0-9a-f]{6}", lid or ""):
        return None
    try:
        return json.loads(_path(lid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def all_lessons() -> List[Dict[str, Any]]:
    out = []
    try:
        for p in sorted(lessons_dir().glob("l-*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def integrity_ok(les: Dict[str, Any]) -> bool:
    return les.get("sha256") == _digest(les)


def new_lesson(parsed: Dict[str, Any], source: str, model: str, evidence: str = "") -> Dict[str, Any]:
    lid = "l-" + hashlib.sha256(f"{time.time()}{source}{parsed['title']}".encode()).hexdigest()[:6]
    return save_lesson({"id": lid, "status": "pending", "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": source, "model": model,
                        "evidence": evidence, **parsed})


def _set_status(lid: str, status: str, allowed_from: Tuple[str, ...], event: str) -> str:
    les = load_lesson(lid)
    if les is None:
        return f"No lesson {html.escape(lid)}."
    if les["status"] not in allowed_from:
        return f"Lesson {lid} is {les['status']}; cannot {event.split('_')[-1]} it."
    if status == "active":
        if not integrity_ok(les):
            audit.append("lesson_tamper", id=lid)
            les["status"] = "retired"
            _atomic(_path(lid), les)
            return f"⚠️ Lesson {lid} no longer matches what was proposed. I retired it."
        if sum(1 for x in all_lessons() if x["status"] == "active") >= MAX_ACTIVE_LESSONS:
            return f"There are already {MAX_ACTIVE_LESSONS} active lessons; retire one first."
        les["approved"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    les["status"] = status
    _atomic(_path(lid), les)
    audit.append(event, id=lid)
    return f"Lesson {lid} {status}."


def approve(lid: str) -> str:
    return _set_status(lid, "active", ("pending",), "lesson_approved")


def deny(lid: str) -> str:
    return _set_status(lid, "denied", ("pending",), "lesson_denied")


def retire(lid: str) -> str:
    return _set_status(lid, "retired", ("active", "pending"), "lesson_retired")


def render_list() -> str:
    ls = all_lessons()
    if not ls:
        return "No lessons yet. When something fails, <code>teach</code> asks the teacher for one (needs <code>teacher on</code>)."
    icon = {"active": "✅", "pending": "📝", "denied": "🚫", "retired": "⌛"}
    lines = [f"<b>Lessons</b> ({len(ls)})"]
    for x in ls[-12:]:
        lines.append(f"{icon.get(x['status'], '•')} <code>{x['id']}</code> {html.escape(x['title'])} · {x['status']}"
                     + (f" · {html.escape(x.get('evidence', ''))}" if x.get("evidence") else ""))
    lines.append("<i>show lesson &lt;id&gt; · approve|deny|retire lesson &lt;id&gt;</i>")
    return "\n".join(lines)


def render_show(lid: str) -> str:
    les = load_lesson(lid)
    if les is None:
        return f"No lesson {html.escape(lid)}."
    e = html.escape
    lines = [f"🎓 <b>{e(les['title'])}</b> <code>{les['id']}</code> · {les['status']} · from {e(str(les.get('source')))} · by {e(str(les.get('model')))}",
             f"<b>Diagnosis:</b> {e(les.get('diagnosis', ''))}", "<b>Guidance:</b>"] + [f"• {e(g)}" for g in les["guidance"]]
    if les.get("applies_when"):
        lines.append("<b>Applies when:</b> " + e(", ".join(les["applies_when"])))
    if les.get("evidence"):
        lines.append("<b>Evidence:</b> " + e(les["evidence"]))
    if les["status"] == "pending":
        lines.append(f"<code>approve lesson {les['id']}</code> or <code>deny lesson {les['id']}</code>")
    return "\n".join(lines)


def relevant_text(query: str, k: int = 3) -> str:
    """Prompt block of the ACTIVE, integrity-checked lessons whose keywords appear in `query` ('' if none). Advisory text only."""
    q = (query or "").lower()
    if not q:
        return ""
    scored = []
    for les in all_lessons():
        if les.get("status") != "active" or not integrity_ok(les):
            continue
        hits = sum(1 for kw in les.get("applies_when", []) if kw and kw in q)
        if hits:
            scored.append((hits, les))
    scored.sort(key=lambda t: -t[0])
    blocks, used = [], 0
    for _, les in scored[:k]:
        block = f"### Lesson: {les['title']}\n{lesson_text(les)}"
        if used + len(block) > MAX_INJECTED_CHARS:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------------------------------------------------------------
# the teaching call itself
# ---------------------------------------------------------------------------------------------------------------------------------

def teach(kind: str, source: str, request_text: str, failure_class: str, attempt: str = "", post: Post = default_post) -> Tuple[Optional[Dict[str, Any]], str]:
    """Ask the teacher about ONE failure. Returns (pending lesson or None, message). Enforces budget, redaction, refusal."""
    ok, why = can_call()
    if not ok:
        return None, why
    user, refusal, n_red = build_request(kind, request_text, failure_class, attempt)
    if refusal:
        audit.append("teacher_refused", source=source, reason=refusal[:120])
        return None, f"Not sent: {refusal}."
    reply, err = call_teacher(user, post)
    _record_usage(len(user), len(reply or ""))                   # a failed call still counts: the budget is about attempts
    audit.append("teacher_called", source=source, model=config()["model"], in_chars=len(user), out_chars=len(reply or ""),
                 redactions=n_red, ok=bool(reply))
    if reply is None:
        return None, f"The teacher call failed: {err}."
    parsed = parse_lesson(reply)
    if parsed is None:
        return None, "The teacher replied, but not with usable guidance (nothing stored)."
    les = new_lesson(parsed, source, config()["model"])
    audit.append("lesson_proposed", id=les["id"], source=source)
    return les, f"Lesson {les['id']} proposed ({n_red} redaction(s) applied before sending)."
