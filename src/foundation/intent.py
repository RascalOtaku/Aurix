"""src/foundation/intent.py - a tiny local model that turns plain speech into one of a few safe commands, before the big model.

The idea of nazirlouis/ada_local's FunctionGemma router: "kill the kitchen lights" or "is the printer busy?" does not need a
9B agent loop; a sub-1B model on the GPU PC (or the 7070's CPU) can map it to `turn off kitchen lights` / `printer` in well
under a second, and everything else falls through to the agent exactly as before.

    AURIX_ROUTER_MODEL  an Ollama model, e.g. qwen2.5:0.5b, gemma3:270m or functiongemma (unset = router off)
    AURIX_ROUTER_URL    Ollama base URL; default http://<AURIX_GPU_HOST>:11434, else http://localhost:11434

Safety: the router's reply is only a CANDIDATE command string. It must parse through the same commands.parse() as typed text,
and only the read-only / low-risk kinds in ROUTABLE are accepted - it can never approve, print, buy, deploy or start a mission.
Anything else, or any doubt, returns None and the message goes to the agent unchanged.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

ROUTABLE = frozenset({"look", "home", "home_switch", "printer", "fab_status", "disk", "homelab", "status", "doctor", "level",
                      "games", "version", "unwrap", "earnings", "dashboard", "pihole", "pihole_pause", "pihole_resume", "tv",
                      "tv_pause", "tv_resume", "kuma", "gpu"})
TIMEOUT = 8
MIN_CONFIDENCE = 0.75
# High-stakes words (from the triage prototype's human-escalation guard): such a message is never shortcut by the tiny
# model, however confident it is - it goes to the agent, whose approval gates exist for exactly these.
HIGH_STAKES = re.compile(r"\b(pay|payment|paid|buy|purchase|order|refund|invoice|transfer|send\s+(?:an?\s+|the\s+)?e-?mail|"
                         r"e-?mail\s+(?:the|my|this|our)\s+|"
                         r"publish|post\s+(?:to|on|this\s+on)|tweet|delete|remove|wipe|erase|legal|lawyer|contract|sign|"
                         r"password|passcode|api\s+key|token|unlock|"
                         r"open\s+(?:the\s+|my\s+)?(?:front\s+|back\s+)?(?:doors?|garage))\b", re.I)
SYSTEM = """You map one chat message to at most one command from this list, or to nothing.
Commands:
- look                      (what is on the PC screen / in the game right now)
- look: <question>          (a question about the PC screen)
- lights                    (list lights, plugs, fans and their state)
- turn on <device name>     / turn off <device name> / toggle <device name>
- printer                   (3D printer status / progress)
- ads                       (Pi-hole ad-blocking stats)  / pause ads <minutes> / resume ads
- tv                        (what is playing on Jellyfin) / pause tv / resume tv
- kuma                      (Uptime Kuma: which services are up or down)
- gpu                       (is the GPU PC on, which Ollama models it has, how often it went off)
- parts                     (3D parts designed so far)
- disk                      (server disk space)
- homelab                   (are the home servers up)
- status                    (what Aurix is doing)
- version                   (which Aurix version is running)
- unwrap                    (what happened overnight)
- earnings                  (money earned so far)
Reply with ONLY JSON: {"command": "<one command exactly as listed, or empty>", "confidence": <0..1>}.
Use an empty command for anything else: questions, chat, requests to make, buy, print, approve or change something."""

Post = Callable[[str, dict, int], dict]


def model() -> str:
    return os.environ.get("AURIX_ROUTER_MODEL", "").strip()


def enabled() -> bool:
    return bool(model())


def base_url() -> str:
    url = os.environ.get("AURIX_ROUTER_URL", "").strip()
    if url:
        return url.rstrip("/")
    host = os.environ.get("AURIX_GPU_HOST", "").strip()
    return f"http://{host}:11434" if host else "http://localhost:11434"


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def route(text: str, post: Optional[Post] = None) -> Optional[Tuple[str, str]]:
    """(kind, arg) for a safe command the message clearly means, else None. Never raises."""
    if not enabled():
        return None
    msg = " ".join((text or "").split())
    if not msg or len(msg) > 300:                              # long messages are real requests for the agent
        return None
    if HIGH_STAKES.search(msg):                                # money, publishing, deleting, secrets, doors: the agent decides
        return None
    post = post or _post
    body = {"model": model(), "stream": False, "format": "json", "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": msg}]}
    t0 = time.time()
    try:
        data = post(base_url() + "/api/chat", body, TIMEOUT)
        obj = json.loads(str(((data or {}).get("message") or {}).get("content") or "{}"))
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    cmd = str(obj.get("command") or "").strip()
    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if not cmd or conf < MIN_CONFIDENCE:
        return None
    from src.foundation import audit, commands
    parsed = commands.parse(cmd)                               # the same gate as typed text...
    if not parsed or parsed[0] not in ROUTABLE:                # ...and only the safe kinds
        return None
    audit.append("intent_routed", kind=parsed[0], ms=round((time.time() - t0) * 1000))
    return parsed
