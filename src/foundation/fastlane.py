"""src/foundation/fastlane.py - which missions may start without the owner's approval tap.

Owner decision 2026-09-20: "read only and data gathering can skip approval". The decision is CODE, never the model's opinion, and it
fails closed: any doubt -> the mission is proposed as usual and waits for `approve mission <id>`.

A mission qualifies only if EVERY one of these holds:
  * its domain packs are on FAST_PACKS (no legal gates: not trading, bug bounty, social, real estate, medical data, self-improve...;
    a pack's own sandbox notes are not a gate)
  * it needs nothing from the owner (no credentials/authorizations/services, nothing to install, no unknown capabilities)
  * every capability it uses is either a read-only tool (read_file, web_search) or a sandbox tool (bash, python, write_file, openhands)
    and, for the sandbox tools, the mission really is sandboxed (isolated container: no secrets, no network, its own workspace)
  * neither the goal nor any step mentions an outward or destructive action (send, post, buy, delete, deploy, log in, ...)
  * its risk ceiling is not above MEDIUM
Everything is still audited (`mission_approved by=policy:fast_lane`, `fast_lane_started`), the tool gate still applies inside the
mission, `stop` still kills it, and `fast lane off` turns the whole thing off.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Tuple

from src.foundation import audit
from src.foundation import capabilities as cap

FAST_PACKS = frozenset({"software_dev"})
SANDBOX_TOOLS = frozenset({"bash", "python", "write_file", "openhands"})
READONLY_TOOLS = frozenset({"read_file", "web_search"})
BLOCKING_KINDS = frozenset({"credential", "authorization", "service"})
MAX_RISK = 2                                       # RiskTier.MEDIUM.value

# Words that mean the goal reaches outside the sandbox or destroys something. Deliberately broad: a false positive only costs one tap.
_VERBS = ("send|e-?mail|post|publish|tweet|upload|push|deploy|purchase|buy|pay|wire|transfer|donate|subscribe|register|delete|erase|"
          "wipe|uninstall|reboot|shutdown|invest|withdraw|deposit|trade")
_OUTWARD = re.compile(
    r"\b(?:(?:" + _VERBS + r")(?:s|es|ed|d|ing)?|sent|bought|paid|"
    r"message (?:me|him|her|them|someone)|text me|place an order|sign[- ]?up|log[- ]?in|login|"
    r"sudo|passwords?|passwd|credentials?|api[- ]?keys?|tokens?|secrets?|ssh|cron|crontab|systemctl|docker)\b", re.I)


def state_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "fastlane.json"


def enabled() -> bool:
    """On by default (the owner asked for it). `fast lane off` writes the flag file; AURIX_FASTLANE=0 also forces it off."""
    if os.environ.get("AURIX_FASTLANE", "1").strip() == "0":
        return False
    from src.foundation import shards
    if shards.is_paused("fastlane"):
        return False
    try:
        return bool(json.loads(state_path().read_text(encoding="utf-8")).get("enabled", True))
    except (OSError, ValueError):
        return True


def set_enabled(flag: bool) -> str:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enabled": bool(flag)}), encoding="utf-8")
    os.replace(tmp, p)
    audit.append("fast_lane_set", enabled=bool(flag))
    return status_text()


def status_text() -> str:
    on = enabled()
    return ("⚡ <b>Fast lane is " + ("ON" if on else "OFF") + "</b>\n"
            + ("Missions that are sandbox-only or read-only, need nothing from you and mention no outward action start immediately "
               "(audited as <code>policy:fast_lane</code>). Everything else still asks. <code>fast lane off</code> to turn it off."
               if on else "Every mission waits for <code>approve mission &lt;id&gt;</code>. <code>fast lane on</code> to enable it."))


def check(m) -> Tuple[bool, List[str]]:
    """(eligible, reasons). When eligible the reasons say why it is safe; when not, they say what needs the owner's tap."""
    blockers: List[str] = []
    req = m.requirements or {}
    if not m.steps:
        blockers.append("no plan yet")
    if getattr(m.risk_ceiling, "value", 99) > MAX_RISK:
        blockers.append(f"risk ceiling {getattr(m.risk_ceiling, 'name', '?')}")
    for key, label in (("manual", "needs you to provide something"), ("owner_install", "needs you to install something"),
                       ("installable", "needs packages installed"), ("unknown", "has unknown capabilities")):
        if req.get(key):
            blockers.append(label)
    packs = set(req.get("packs") or [])
    if not packs <= FAST_PACKS:
        blockers.append("domain pack(s) " + ", ".join(sorted(packs - FAST_PACKS)) + " need your approval")

    used = {c for s in m.steps for c in s.capabilities} | set(req.get("capabilities") or [])
    needs_sandbox = False
    for name in sorted(used):
        c = cap.lookup(name)
        if c is None:
            blockers.append(f"unknown capability {name}")
        elif c.kind in BLOCKING_KINDS:
            blockers.append(f"{c.id} is a {c.kind}")
        elif c.id in SANDBOX_TOOLS:                     # by id: openhands is a python_pkg, bash/python/write_file are builtins
            needs_sandbox = True
        elif c.kind == "builtin" and c.id not in READONLY_TOOLS:
            blockers.append(f"tool {c.id} is not on the fast lane")
    if needs_sandbox and not m.sandboxed:
        blockers.append("its code would run outside the isolated sandbox")

    # Steps that come from a code-defined domain pack carry safety notes ("nothing is pushed or deployed"); only their titles are
    # scanned. Anything else (the goal, and every model-written step) is scanned in full.
    pack_titles = {s.title for p in cap.match_packs(m.objective or "") for s in p.steps}
    text = " ".join([m.objective or ""] + [s.title if s.title in pack_titles else f"{s.title} {s.description}" for s in m.steps])
    hit = _OUTWARD.search(text)
    if hit:
        blockers.append(f"mentions an outward or destructive action ('{hit.group(0).lower()}')")

    if blockers:
        return False, blockers
    return True, ["sandbox-only" if needs_sandbox else "read-only", "no credentials, packs or outward actions"]


def announce(m, reasons: List[str]) -> None:
    audit.append("fast_lane_started", mission=m.id, reasons=reasons[:4])
