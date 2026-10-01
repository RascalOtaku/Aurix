"""src/foundation/versions.py - AURIX's version history, and review tickets for BIG shifts.

Every successful deploy (scripts/aurix_deploy.sh, which the upgrade lane also uses) records a version: a per-file fingerprint of the
code that actually shipped, what changed since the previous version, and whether that was a BIG SHIFT. Code decides, not a model:

    big shift = any of: >= BIG_FILES files changed | a structurally protected component changed (identity.PROTECTED_COMPONENTS:
                approval gate, audit, auth, commands, ...) | a new Foundation module appeared
    version   = MAJOR.MINOR.PATCH, starting at 1.0.0: a big shift bumps MINOR, anything else bumps PATCH (MAJOR is the owner's call)

A big shift writes a review ticket (data/versions/reviews/v<version>.md): what changed and why it counts, for a Claude Code review
session to pick up (the owner runs Claude Code in ~/ai-brain-sync, or a future GitHub @claude integration reads the same ticket).
Nothing here edits code or calls a model. `version` on Telegram shows the history and open tickets; `reviewed v1.2.0` closes one.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape
BIG_FILES = 15
CODE_DIRS = ("src", "routes", "services", "core", "scripts", "mission_sandbox")
CODE_FILES = ("app.py", "Dockerfile", "docker-compose.yml", "requirements.txt")
VERSION_RX = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _root() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app"))


def _dir() -> Path:
    return _root() / "data" / "versions"


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def history() -> List[dict]:
    return _read(_dir() / "history.json", [])


def fingerprint(code_root: Optional[Path] = None) -> Dict[str, str]:
    """{relative path: sha256} for the shipped code. Data, logs, caches and secrets are never included."""
    root = Path(code_root) if code_root else _root()
    out: Dict[str, str] = {}
    paths = [root / f for f in CODE_FILES if (root / f).is_file()]
    for d in CODE_DIRS:
        base = root / d
        if base.is_dir():
            paths += [p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".log")
                      and not p.name.startswith(".env")]
    for p in sorted(paths):
        try:
            # Always forward slashes: str(Path) uses the native separator, which would make every
            # fingerprint (and the review tickets/tests that compare these paths) Windows-only when
            # taken on the dev PC instead of matching the real Linux paths used everywhere else.
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
    return out


def diff(old: Dict[str, str], new: Dict[str, str]) -> Dict[str, List[str]]:
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "modified": sorted(k for k in set(old) & set(new) if old[k] != new[k])}


def big_shift_reasons(d: Dict[str, List[str]]) -> List[str]:
    from src.foundation import identity
    changed = d["added"] + d["removed"] + d["modified"]
    reasons = []
    if len(changed) >= BIG_FILES:
        reasons.append(f"{len(changed)} files changed (threshold {BIG_FILES})")
    prot = sorted({c for f in changed if (c := identity.protected_component_for(f))})
    if prot:
        reasons.append("protected component(s) changed: " + ", ".join(prot))
    new_mods = [f for f in d["added"] if re.fullmatch(r"src/foundation/[^/]+\.py", f)]
    if new_mods:
        reasons.append("new Foundation module(s): " + ", ".join(new_mods))
    return reasons


def _bump(prev: Optional[str], big: bool) -> str:
    m = VERSION_RX.match(prev or "")
    if not m:
        return "1.0.0"
    major, minor, patch = map(int, m.groups())
    return f"{major}.{minor + 1}.0" if big else f"{major}.{minor}.{patch + 1}"


def record(source: str = "deploy", code_root: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """Record the code that is running now as a version (no-op if nothing changed since the last one)."""
    now = now or time.time()
    fp = fingerprint(code_root)
    hist = history()
    prev_fp = _read(_dir() / "manifests" / f"{hist[-1]['version']}.json", {}) if hist else {}
    d = diff(prev_fp, fp)
    changed = d["added"] + d["removed"] + d["modified"]
    if hist and not changed:
        return {"version": hist[-1]["version"], "unchanged": True}
    reasons = big_shift_reasons(d) if hist else ["first recorded version (baseline)"]
    big = bool(reasons)
    ver = _bump(hist[-1]["version"] if hist else None, big and bool(hist))
    code_hash = hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()
    entry = {"version": ver, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "source": source,
             "code_hash": code_hash, "files": len(fp), "changed": len(changed), "big_shift": big, "reasons": reasons,
             "added": d["added"][:200], "removed": d["removed"][:200], "modified": d["modified"][:400],
             "review": "open" if big and hist else "none"}
    _atomic(_dir() / "manifests" / f"{ver}.json", json.dumps(fp, sort_keys=True))
    hist.append(entry)
    _atomic(_dir() / "history.json", json.dumps(hist[-500:], indent=1))
    if entry["review"] == "open":
        _atomic(_dir() / "reviews" / f"v{ver}.md", ticket(entry))
    audit.append("version_recorded", version=ver, code_hash=code_hash, changed=len(changed), big_shift=big, source=source)
    return entry


def ticket(entry: dict) -> str:
    lines = [f"# AURIX v{entry['version']}: big-shift review", "", f"Recorded {entry['at']} by {entry['source']}. Status: **open**", "",
             "## Why this counts as a big shift", *[f"- {r}" for r in entry["reasons"]], "",
             "## What changed"]
    for k in ("added", "removed", "modified"):
        if entry[k]:
            lines += [f"**{k}** ({len(entry[k])}):", *[f"- `{p}`" for p in entry[k][:120]], ""]
    lines += ["## For the Claude Code review", "Run Claude Code in `~/ai-brain-sync` and ask it to review this ticket:",
              f"`review AURIX big shift v{entry['version']} (odysseus/data/versions/reviews/v{entry['version']}.md)`", "",
              "Check: tests pass; approval gate, audit, auth and protected paths are not weakened; no fake success; no secrets.",
              f"When done, the owner sends `reviewed v{entry['version']}` on Telegram."]
    return "\n".join(lines) + "\n"


def mark_reviewed(ver: str, by: str = "owner:telegram") -> str:
    ver = ver.lstrip("v")
    hist = history()
    for h in hist:
        if h["version"] == ver:
            if h.get("review") != "open":
                return f"v{e(ver)} has no open review ({e(h.get('review', 'none'))})."
            h["review"], h["reviewed_by"] = "done", by
            _atomic(_dir() / "history.json", json.dumps(hist, indent=1))
            audit.append("version_reviewed", version=ver, by=by)
            return f"✅ v{e(ver)} review closed."
    return f"No version v{e(ver)}."


def status_text(limit: int = 6) -> str:
    hist = history()
    if not hist:
        return "🏷️ No AURIX version recorded yet. The next successful deploy records the baseline (v1.0.0)."
    cur = hist[-1]
    lines = [f"🏷️ <b>AURIX v{e(cur['version'])}</b> · {e(cur['at'])} · {cur['files']} code files · <code>{cur['code_hash'][:12]}</code>"]
    for h in reversed(hist[-limit:]):
        flag = "🔶 big shift" if h["big_shift"] and h["version"] != "1.0.0" else "·"
        lines.append(f"{flag} v{e(h['version'])} ({e(h['at'][:10])}, {h['changed']} changed)" +
                     (f" — review <b>{e(h['review'])}</b>" if h.get("review") in ("open", "done") else ""))
    open_ = [h for h in hist if h.get("review") == "open"]
    if open_:
        lines.append("Open review tickets: " + ", ".join(f"<code>v{e(h['version'])}</code>" for h in open_) +
                     " · files in data/versions/reviews/ · close with <code>reviewed vX.Y.Z</code>")
    return "\n".join(lines)
