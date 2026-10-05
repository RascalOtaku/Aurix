"""Offline decision-triage prototype for Aurix.

Sits in front of expensive model calls: simple yes/no, choice, and score
decisions go to a tiny local Ollama model first. The heavy model is only
invoked when the local answer is uncertain or malformed; designated
high-stakes cases escalate to a human.

Prototype only: this does not rewire Aurix's production routing
(src/model_router.py, src/llm_core.py). Zero new dependencies (stdlib
urllib only). Everything runs against the local Ollama instance.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

OLLAMA_URL = "http://100.114.213.55:11434"
TRIAGE_MODEL = "llama3.2:1b"      # fast tier; falls back if not installed
FALLBACK_MODEL = "llama3.2:3b"    # still small, already on the box
REQUEST_TIMEOUT_S = 90
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 20

KINDS = ("yes_no", "choice", "score")
ROUTES = ("local", "heavy", "human")

# Designated escalation cases: high-stakes actions always route to a human,
# regardless of what the local model says. Documented, deterministic, safe.
ESCALATE_KEYWORDS = (
    "send email", "send the email", "publish", "post to", "pay ", "payment",
    "delete", "legal", "contract", "password",
)

_PROMPTS = {
    "yes_no": (
        "You are a triage classifier. Answer the question with exactly one "
        "word: YES or NO. No explanation, no punctuation.\n\nQuestion: {q}\nAnswer:"
    ),
    "choice": (
        "You are a triage router. Decide who should handle this task. Answer "
        "with exactly one word: HEAVY, HUMAN, or NEITHER. No explanation.\n"
        "- HEAVY: needs the big slow model (complex reasoning, code, research)\n"
        "- HUMAN: needs a person (approvals, money, publishing, irreversible actions)\n"
        "- NEITHER: the small local model can handle it directly\n\n"
        "Task: {q}\nAnswer:"
    ),
    "score": (
        "You are a triage scorer. Rate the urgency from 1 (lowest) to 5 "
        "(highest). Answer with exactly one digit. No explanation.\n\n"
        "Item: {q}\nAnswer:"
    ),
}

_CHOICE_DECISIONS = ("HEAVY", "HUMAN", "NEITHER")


_model_lock = threading.Lock()
_resolved_model: str | None = None


def _available_models() -> list:
    req = urllib.request.Request(OLLAMA_URL + "/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    return [m.get("name", "") for m in data.get("models", [])]


def resolve_model() -> str:
    """Pick the tiny triage model, falling back if it is not installed."""
    global _resolved_model
    with _model_lock:
        if _resolved_model:
            return _resolved_model
        names = _available_models()
        for candidate in (TRIAGE_MODEL, FALLBACK_MODEL):
            if any(candidate == n or n.startswith(candidate + ":") for n in names):
                _resolved_model = candidate
                return candidate
        # Ollama not reachable or no known model: still try the preferred name
        # and let the call path report the failure honestly.
        _resolved_model = TRIAGE_MODEL
        return _resolved_model


def _ollama_generate(model: str, prompt: str) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 12},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL + "/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data.get("response", "")


def _call_with_retry(model: str, prompt: str) -> tuple[str, int]:
    """Call Ollama with backoff. Returns (raw_text, attempts_used)."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _ollama_generate(model, prompt), attempt
        except Exception as exc:  # timeout, connection reset, 5xx, ...
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S)
    raise RuntimeError(
        "triage: Ollama unreachable after %d attempts: %s" % (MAX_ATTEMPTS, last_exc)
    )


def _parse_yes_no(raw: str) -> tuple[str | None, float]:
    token = raw.strip().upper()
    if token in ("YES", "NO"):
        return token, 1.0
    token = token.strip(".,!?\"' ")
    if token in ("YES", "NO"):
        return token, 0.85
    m = re.search(r"\b(YES|NO)\b", token)
    if m:
        return m.group(1), 0.6
    return None, 0.0


def _parse_choice(raw: str) -> tuple[str | None, float]:
    token = raw.strip().upper()
    if token in _CHOICE_DECISIONS:
        return token, 1.0
    token = token.strip(".,!?\"' ")
    if token in _CHOICE_DECISIONS:
        return token, 0.85
    m = re.search(r"\b(HEAVY|HUMAN|NEITHER)\b", token)
    if m:
        return m.group(1), 0.6
    return None, 0.0


def _parse_score(raw: str) -> tuple[str | None, float]:
    token = raw.strip()
    if re.fullmatch(r"[1-5]", token):
        return token, 1.0
    m = re.search(r"[1-5]", token)
    if m and len(token) <= 12:
        return m.group(0), 0.6
    return None, 0.0


_PARSERS = {
    "yes_no": _parse_yes_no,
    "choice": _parse_choice,
    "score": _parse_score,
}


def _needs_human_guard(question: str) -> bool:
    q = " " + question.lower() + " "
    return any(kw in q for kw in ESCALATE_KEYWORDS)


def triage(question: str, kind: str) -> dict:
    """Route one decision through the tiny local model.

    Returns {"decision", "confidence", "route", "latency_s", "model",
    "attempts"}. route is "local" (tiny model answered), "heavy" (uncertain
    or malformed local answer: escalate to the big model), or "human"
    (designated escalation case).
    """
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % (KINDS,))
    started = time.monotonic()
    model = resolve_model()

    # Deterministic guard first: high-stakes actions never ride on the tiny model.
    if _needs_human_guard(question):
        return {
            "decision": "HUMAN",
            "confidence": 1.0,
            "route": "human",
            "latency_s": round(time.monotonic() - started, 3),
            "model": model,
            "attempts": 0,
            "guard": True,
        }

    prompt = _PROMPTS[kind].format(q=question)
    raw, attempts = _call_with_retry(model, prompt)
    decision, confidence = _PARSERS[kind](raw)
    latency = round(time.monotonic() - started, 3)

    if decision is None:
        route = "heavy"
        decision = "UNCERTAIN"
    elif kind == "choice" and decision == "HUMAN":
        route = "human"
    elif kind == "choice" and decision == "HEAVY":
        route = "heavy"
    else:
        route = "local"

    return {
        "decision": decision,
        "confidence": confidence,
        "route": route,
        "latency_s": latency,
        "model": model,
        "attempts": attempts,
        "guard": False,
        "raw": raw.strip()[:60],
    }
