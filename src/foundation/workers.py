"""src/foundation/workers.py - the Worker Registry & Router: every model and agent AURIX can use, as SUBORDINATE workers.

    AURIX authority (owner, approval gate, missions, capabilities)          <- decides WHAT may happen
    ------------------------------------------------------------------
    Worker Registry & Router (this module)                                 <- decides WHO does a piece of model work, and WHERE data may go
    ------------------------------------------------------------------
    individual workers: Ollama models on the 3431 GPU / 7070 CPU, the sandboxed OpenHands / OpenClaw executors, later cloud providers

A worker is never an authority. It gets text in and gives text out; anything consequential it proposes still goes through the approval
gate, missions and the owner. The router adds no permission to anything - it only narrows (locality, data class, purpose, budget).

WHO: the registry is built live from AURIX's own Model Endpoints table (Settings -> Model Endpoints; API keys stay encrypted there and are
never read here) plus the two sandbox executors. Nothing is hard-coded to a provider, so swapping or adding a worker needs no code change.

WHERE (data locality, fails closed):
    locality   local = this host (the 7070)   lan = private network or tailnet (e.g. the 3431 GPU)   cloud = anything else
    data class private (DEFAULT) and internal may go to local/lan workers only; public may also go to an enabled cloud worker.
    A cloud worker is OFF until the owner grants its own authorization: `authorize worker-<slug>` (capabilities.py authorizations).

HOW WELL (routing by purpose): code edits go to a code-tuned model with ENFORCED JSON (Ollama `format: json`; found 2026-09-29: 15 of
19 upgrade drafts died as "not the JSON I asked for" from a general model); planning/review/writing to the strongest general model;
the 7070's CPU models never get heavy work (a CPU fallback once sat 10 minutes on an agent prompt and produced nothing).

HONESTY: ask() returns (text, info). No text means it failed, and info["reason"] says why - never a placeholder answer. Every call is
audited (`worker_call`: worker, purpose, data class, ok, timing, sizes) - never the prompt or the reply.

Protected component (approval_gate): agent tools cannot edit this module (it decides where private data may go).
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.foundation import audit

DATA_CLASSES = ("private", "internal", "public")
LOCALITY_FOR = {"private": ("local", "lan"), "internal": ("local", "lan"), "public": ("local", "lan", "cloud")}
# internal data (AURIX's own code, repo plans, drafts) may ALSO go to authorised cloud workers once the owner grants this; private never.
CLOUD_INTERNAL_AUTH = "cloud-internal"
CLOUD_DAILY_CALLS = 150              # per cloud worker per day: stays inside free-tier limits; local workers are unmetered

# purpose -> what matters. `heavy` purposes never go to a CPU worker; `json` purposes ask for enforced JSON by default.
PURPOSES: Dict[str, Dict[str, object]] = {
    "code_edit": {"prefer": "coder", "heavy": True, "json": True},     # structured code edits (the upgrade lane)
    "code":      {"prefer": "coder", "heavy": True, "json": False},    # write/fix code (freelance drafts, skills)
    "plan":      {"prefer": "large", "heavy": True, "json": False},
    "review":    {"prefer": "large", "heavy": True, "json": False},    # red-team / critique / lessons
    "write":     {"prefer": "large", "heavy": True, "json": False},    # content drafts
    "summarize": {"prefer": "large", "heavy": False, "json": False},   # daily logs, reading notes
    "extract":   {"prefer": "mid", "heavy": False, "json": False},
    "classify":  {"prefer": "small", "heavy": False, "json": False},
    "general":   {"prefer": "large", "heavy": True, "json": False},
}
EXECUTORS = (("openhands", "OpenHands coding agent (sandboxed mission executor)"),
             ("openclaw", "OpenClaw agent runtime (sandboxed mission executor)"))
DEFAULT_TIMEOUT_S = 600


@dataclass
class Worker:
    id: str
    backend: str                  # ollama | openai_compat | executor
    title: str
    model: str = ""
    endpoint_id: str = ""
    base_url: str = ""
    locality: str = "cloud"       # fails closed: unknown host = cloud
    cpu_only: bool = False
    params_b: float = 0.0         # parameter count from the model name, e.g. qwen2.5:7b -> 7.0
    coder: bool = False
    embed_only: bool = False
    enabled: bool = True
    purposes: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def provenance(self) -> str:
        return f"worker:{self.id}"


# --------------------------------------------------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------------------------------------------------

def locality_of(base_url: str) -> str:
    host = (urlparse(base_url or "").hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal", "host-gateway"):
        return "local"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "cloud"                                                   # a hostname we cannot place is treated as cloud
    if ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10"):     # RFC1918 or the tailnet (CGNAT range Tailscale uses)
        return "lan"
    return "cloud"


def _params(model: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", (model or "").lower().split(":")[-1]) or re.search(r"(\d+(?:\.\d+)?)b", (model or "").lower())
    return float(m.group(1)) if m else 0.0


# Model ids that are not text-chat models (a provider's /models list mixes in image, speech, video, music, embeddings, robotics and
# agent products - found 2026-09-30: the Gemini endpoint listed 61 ids). They are never registered as text workers.
_NON_CHAT = re.compile(r"(embed|tts|image|imagen|veo|lyria|audio|[-_]live|robotics|\baqa\b|transcribe|computer-use|translate|"
                       r"nano-banana|dall-e|whisper|moderation|deep-research|antigravity|omni|customtools)", re.I)


def _cloud_rank(model: str, heavy: bool) -> tuple:
    """Order cloud models by name, since they publish no size: heavy work Pro > Flash > other > Lite; light work the reverse;
    stable before preview/experimental; `-latest` aliases and newer version numbers first."""
    n = (model or "").lower()
    cls = 3 if "lite" in n else 0 if "pro" in n else 1 if "flash" in n else 2
    if not heavy:
        cls = {3: 0, 1: 1, 0: 2, 2: 3}[cls]
    preview = 1 if ("preview" in n or "exp" in n) else 0
    m = re.search(r"(\d+(?:\.\d+)?)", n.split("/")[-1])
    ver = 99.0 if "latest" in n else float(m.group(1)) if m else 0.0
    return (cls, preview, -ver)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "endpoint"


def _load_endpoints() -> List[dict]:
    """The Model Endpoints table, WITHOUT secrets: the api_key column is never touched (reading it would decrypt it). Empty on failure."""
    try:
        from src.database import ModelEndpoint, SessionLocal
    except Exception:                                                    # noqa: BLE001 - no DB here (tests, host scripts)
        return []
    db = SessionLocal()
    try:
        out = []
        for ep in db.query(ModelEndpoint).all():
            out.append({"id": ep.id, "name": ep.name, "base_url": ep.base_url, "is_enabled": bool(ep.is_enabled),
                        "model_type": ep.model_type or "llm", "cached_models": ep.cached_models, "hidden_models": ep.hidden_models})
        return out
    except Exception:                                                    # noqa: BLE001
        return []
    finally:
        db.close()


def _jsonlist(v) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    try:
        x = json.loads(v or "[]")
        return [str(i) for i in x] if isinstance(x, list) else []
    except (TypeError, ValueError):
        return []


def _authorized(key: str, authorizations: Optional[Dict[str, dict]]) -> bool:
    from src.foundation import capabilities
    granted = capabilities.load_authorizations() if authorizations is None else authorizations
    return capabilities._authorized(key, granted, time.time()).present


def registry(endpoints: Optional[List[dict]] = None, authorizations: Optional[Dict[str, dict]] = None) -> List[Worker]:
    eps = _load_endpoints() if endpoints is None else endpoints
    out: List[Worker] = []
    for ep in eps:
        if (ep.get("model_type") or "llm") != "llm":
            continue
        base = ep.get("base_url") or ""
        loc = locality_of(base)
        port = urlparse(base).port
        host = (urlparse(base).hostname or "").lower()
        backend = ("ollama" if port == 11434 or "ollama" in (ep.get("name") or "").lower()
                   else "anthropic" if host.endswith("anthropic.com") else "openai_compat")
        cpu = loc == "local" and ("cpu" in (ep.get("name") or "").lower() or urlparse(base).hostname == "host.docker.internal")
        hidden = set(_jsonlist(ep.get("hidden_models")))
        cloud_ok = loc != "cloud" or _authorized(f"worker-{_slug(ep.get('name') or ep.get('id'))}", authorizations)
        for model in _jsonlist(ep.get("cached_models")):
            if model in hidden or _NON_CHAT.search(model):
                continue
            low = model.lower()
            w = Worker(id=f"{_slug(ep.get('name') or ep['id'])}/{model}", backend=backend, title=f"{model} @ {ep.get('name') or base}",
                       model=model, endpoint_id=ep.get("id", ""), base_url=base, locality=loc, cpu_only=cpu, params_b=_params(model),
                       coder="coder" in low or "code" in low.split(":")[0], embed_only="embed" in low,
                       enabled=bool(ep.get("is_enabled", True)) and cloud_ok)
            if not cloud_ok:
                w.note = f"cloud worker: off until `authorize worker-{_slug(ep.get('name') or ep.get('id'))}`"
            w.purposes = [p for p in PURPOSES if _suits(w, p)]
            out.append(w)
    for ex_id, title in EXECUTORS:
        out.append(Worker(id=ex_id, backend="executor", title=title, locality="local", enabled=True, purposes=[],
                          note="runs inside the mission sandbox; reached through missions (approval gate), not ask()"))
    return out


def _suits(w: Worker, purpose: str) -> bool:
    spec = PURPOSES[purpose]
    if w.backend == "executor" or w.embed_only:
        return False
    if spec["heavy"] and w.cpu_only:
        return False                                                     # never heavy work on the 7070 CPU
    if spec["heavy"] and 0 < w.params_b < 3:
        return False                                                     # 1-2B models cannot carry planning/code/review
    return True


# --------------------------------------------------------------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------------------------------------------------------------

def route(purpose: str, data_class: str = "private", workers: Optional[List[Worker]] = None,
          cloud_internal: Optional[bool] = None) -> List[Worker]:
    """Ordered candidates for a purpose. Unknown purpose or data class fails closed (treated as general / private). When cloud is
    allowed for this data, authorised cloud (frontier) workers come FIRST for heavy work and local workers stay as the fallback."""
    purpose = purpose if purpose in PURPOSES else "general"
    data_class = data_class if data_class in DATA_CLASSES else "private"
    allowed = LOCALITY_FOR[data_class]
    if data_class == "internal":
        if cloud_internal is None:
            try:
                cloud_internal = _authorized(CLOUD_INTERNAL_AUTH, None)
            except Exception:                                            # noqa: BLE001 - cannot tell: fail closed
                cloud_internal = False
        if cloud_internal:
            allowed = allowed + ("cloud",)
    ws = [w for w in (registry() if workers is None else workers)
          if w.enabled and w.backend != "executor" and w.locality in allowed and purpose in w.purposes]
    prefer = PURPOSES[purpose]["prefer"]

    def score(w: Worker) -> tuple:
        size = w.params_b or 5.0
        if w.locality == "cloud":
            return (0 if PURPOSES[purpose]["heavy"] else 1, 0, 0) + _cloud_rank(w.model, bool(PURPOSES[purpose]["heavy"]))
        if prefer == "coder":
            key = (0 if w.coder else 1, -size)
        elif prefer == "large":
            key = (1 if w.coder else 0, -size)                           # a general model writes/plans better than a coder one
        elif prefer == "mid":
            key = (abs(size - 4.0), 0)
        else:                                                            # small: fastest adequate model first
            key = (size, 0)
        frontier_first = 0 if (w.locality == "cloud" and PURPOSES[purpose]["heavy"]) else 1
        return (frontier_first, 1 if w.cpu_only else 0, 0 if w.locality == "lan" else 1) + key   # frontier, then GPU box, then CPU
    return sorted(ws, key=score)


Poster = Callable[[str, dict, float], Tuple[int, dict]]


def _ollama_post(url: str, body: dict, timeout: float) -> Tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {}


def _ollama_root(base_url: str) -> str:
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def _usage_path():
    from pathlib import Path
    import os
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "workers" / "usage.json"


def _calls_today(worker_id: str) -> int:
    try:
        d = json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int((d.get(time.strftime("%Y-%m-%d")) or {}).get(worker_id, 0))


def _count_call(worker_id: str) -> None:
    import os
    p = _usage_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    day = time.strftime("%Y-%m-%d")
    d = {day: d.get(day, {})}                                            # keep only today
    d[day][worker_id] = d[day].get(worker_id, 0) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, p)


# After a failure, skip that worker for a while instead of paying for the same failure on every call (2026-09-30: a live call tried
# six Gemini models - Pro rate-limited on the free tier, one 404, three overloaded - before gemini-3.6-flash answered).
COOLDOWN_S = {"404": 86400, "429": 3600, "5xx": 600, "error": 600}


def _cooldown_path():
    return _usage_path().with_name("cooldown.json")


def _cooled(worker_id: str, now: float) -> float:
    try:
        until = float(json.loads(_cooldown_path().read_text(encoding="utf-8")).get(worker_id, 0))
    except (OSError, ValueError, TypeError):
        return 0.0
    return until if until > now else 0.0


def _cool(worker_id: str, why: str, now: float) -> None:
    import os
    kind = "404" if why.startswith("HTTP 404") else "429" if why.startswith("HTTP 429") else \
        "5xx" if re.match(r"HTTP 5\d\d", why) else "error" if why and not why.startswith("HTTP 4") else ""
    if not kind:
        return                                                           # other 4xx (bad request etc.): not a reason to bench the worker
    p = _cooldown_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d = {k: v for k, v in d.items() if v > now}                         # drop expired entries
    d[worker_id] = now + COOLDOWN_S[kind]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, p)


def _resolve_cloud(w: Worker) -> Optional[Tuple[str, Dict[str, str]]]:
    """(chat_url, headers) through AURIX's own endpoint resolver: the key is decrypted only here, at call time, and never logged."""
    from src.endpoint_resolver import resolve_endpoint_by_id
    r = resolve_endpoint_by_id(w.endpoint_id, w.model)
    return (r[0], dict(r[2])) if r else None


def _openai_post(url: str, headers: Dict[str, str], body: dict, timeout: float) -> Tuple[int, dict]:
    h = dict(headers)
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {}


def _call_cloud(w: Worker, system: str, prompt: str, max_tokens: int, json_mode: bool, timeout: float,
                resolve, cloud_post) -> Tuple[str, str]:
    target = resolve(w)
    if not target:
        return "", "endpoint disabled or unresolvable"
    url, headers = target
    model = w.model[len("models/"):] if w.model.startswith("models/") else w.model   # Gemini lists ids as models/<name>
    body = {"model": model, "max_tokens": int(max_tokens),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    status, data = cloud_post(url, headers, body, timeout)
    if status == 400 and json_mode:                                      # some free endpoints reject response_format: ask once without it
        body.pop("response_format", None)
        status, data = cloud_post(url, headers, body, timeout)
    if status != 200:
        err = data.get("error") if isinstance(data, dict) else ""
        err = err.get("message", "") if isinstance(err, dict) else str(err or "")
        return "", f"HTTP {status}: {err[:120]}"
    try:
        return (data["choices"][0]["message"]["content"] or "").strip(), ""
    except (KeyError, IndexError, TypeError):
        return "", "reply had no message content"


def ask(purpose: str, system: str, prompt: str, *, data_class: str = "private", max_tokens: int = 1500,
        json_mode: Optional[bool] = None, timeout: float = DEFAULT_TIMEOUT_S, workers: Optional[List[Worker]] = None,
        post: Poster = _ollama_post, cloud_internal: Optional[bool] = None,
        resolve=_resolve_cloud, cloud_post=_openai_post) -> Tuple[Optional[str], dict]:
    """Do one piece of model work with the best allowed worker, falling through the route in order. Returns (text, info);
    text is None on failure and info["reason"] says why. Callable backends: Ollama (local/LAN) and OpenAI-compatible cloud
    endpoints (free tiers: Gemini, Groq, OpenRouter, GitHub Models...), the latter only when the data class and the owner allow."""
    purpose = purpose if purpose in PURPOSES else "general"
    json_mode = PURPOSES[purpose]["json"] if json_mode is None else json_mode
    cands = [w for w in route(purpose, data_class, workers, cloud_internal) if w.backend in ("ollama", "openai_compat")]
    if not cands:
        info = {"worker": None, "purpose": purpose, "data_class": data_class,
                "reason": f"no enabled worker may take '{purpose}' work for {data_class} data"}
        audit.append("worker_call", worker=None, purpose=purpose, data_class=data_class, ok=False, why=info["reason"])
        return None, info
    tried = []
    for w in cands:
        t0 = time.time()
        until = _cooled(w.id, t0)
        if until:
            tried.append(f"{w.id}: resting after a recent failure ({int(until - t0)}s left)")
            continue
        if w.backend == "openai_compat" and _calls_today(w.id) >= CLOUD_DAILY_CALLS:
            tried.append(f"{w.id}: daily budget of {CLOUD_DAILY_CALLS} calls used")
            continue
        try:
            if w.backend == "openai_compat":
                _count_call(w.id)
                text, why = _call_cloud(w, system, prompt, max_tokens, json_mode, timeout, resolve, cloud_post)
            else:
                body = {"model": w.model, "stream": False,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        "options": {"num_predict": int(max_tokens)}}
                if json_mode:
                    body["format"] = "json"                              # grammar-constrained: the reply is valid JSON by construction
                status, data = post(_ollama_root(w.base_url) + "/api/chat", body, timeout)
                text = ((data.get("message") or {}).get("content") or "").strip() if status == 200 else ""
                why = "" if text else (f"HTTP {status}: {str(data.get('error', ''))[:120]}" if status != 200 else "empty reply")
        except Exception as e:                                           # noqa: BLE001 - try the next worker
            text, why = "", f"{type(e).__name__}: {str(e)[:120]}"
        ms = int((time.time() - t0) * 1000)
        audit.append("worker_call", worker=w.id, locality=w.locality, purpose=purpose, data_class=data_class, json=bool(json_mode), ok=bool(text), ms=ms,
                     in_chars=len(system) + len(prompt), out_chars=len(text), why=why[:160])
        if text:
            return text, {"worker": w.id, "model": w.model, "provenance": w.provenance, "purpose": purpose, "json": bool(json_mode),
                          "ms": ms, "tried": tried}
        tried.append(f"{w.id}: {why}")
        _cool(w.id, why, time.time())
    return None, {"worker": None, "purpose": purpose, "data_class": data_class, "tried": tried,
                  "reason": "every allowed worker failed: " + "; ".join(tried)[:400]}


# --------------------------------------------------------------------------------------------------------------------------------
# owner view
# --------------------------------------------------------------------------------------------------------------------------------

def status_text(workers: Optional[List[Worker]] = None) -> str:
    import html
    e = html.escape
    ws = registry() if workers is None else workers
    models = [w for w in ws if w.backend != "executor"]
    lines = ["🧰 <b>Workers</b> · AURIX decides, workers do the work. Private data stays on local/LAN workers."]
    if not models:
        lines.append("❌ No model workers found (Settings → Model Endpoints is empty or unreadable).")
    for w in sorted(models, key=lambda w: (w.locality, w.cpu_only, -w.params_b)):
        state = "✅" if w.enabled else "⛔"
        tags = [w.locality.upper(), "CPU" if w.cpu_only else "", "coder" if w.coder else "", "embed" if w.embed_only else ""]
        lines.append(f"{state} <code>{e(w.model)}</code> · {e(' · '.join(t for t in tags if t))} · "
                     + (e(", ".join(w.purposes)) if w.purposes else "no routed purposes") + (f" · <i>{e(w.note)}</i>" if w.note else ""))
    lines.append("<b>Routing now</b>: " + " · ".join(f"{p} → {e(r[0].model) if r else '—'}"
                                                      for p, r in ((p, route(p, 'private', ws)) for p in ("code_edit", "plan", "summarize", "classify"))))
    for w in ws:
        if w.backend == "executor":
            lines.append(f"🤖 {e(w.title)} · <i>{e(w.note)}</i>")
    return "\n".join(lines)


def panel() -> Dict[str, object]:
    ws = registry()
    return {"workers": [asdict(w) | {"provenance": w.provenance} for w in ws],
            "routes": {p: [w.id for w in route(p, "private", ws)][:3] for p in PURPOSES}}
