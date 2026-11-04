#!/usr/bin/env python3
"""aurix_ops_agent.py - the host-side half of AURIX's housekeeping lane (src/foundation/ops.py). Stdlib only.

Run by aurix_upgrade_agent.py's `cycle` (its existing cron, every minute, under flock), or by hand: `python3 scripts/aurix_ops_agent.py cycle`.

    <upgrades>/ops/approved/<id>.json  ->  verify the HMAC signature (same key as the upgrade lane) and expiry  ->  run ONE whitelisted
    operation  ->  <upgrades>/ops/results/<id>.json for the app to report

    update   bash scripts/aurix_git_sync.sh sync  then  bash scripts/aurix_deploy.sh   (both carry their own safety rails: the sync stops on a
             conflict or a secret-looking file and never force-pushes; the deploy rolls back an unhealthy build by itself)
    scan     read-only: fetch, then list remote branches that are merged into main or whose tip matches the private patterns
    prune    delete exactly the branches the signed approval names, each re-checked as still a candidate; never main, never forced
    secret   gh secret set AURIX_PRIVATE_PATTERNS from .git/info/aurix-private-patterns (the patterns never leave this machine except to GitHub)

This agent decides nothing about WHAT to do: it only does what a signed approval names, and refuses anything else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(os.environ.get("AURIX_UPGRADES_DIR") or (Path.home() / "ai-brain-sync" / "sandbox" / "upgrades")) / "ops"
SOURCE = Path(os.environ.get("AURIX_SOURCE_DIR") or (Path.home() / "ai-brain-sync" / "odysseus"))
ENV_FILE = Path(os.environ.get("AURIX_ENV_FILE") or (SOURCE / ".env"))
REPO = os.environ.get("AURIX_GITHUB_REPO", "RascalOtaku/Aurix")
OPS = ("update", "scan", "prune", "secret")
ID_RX = re.compile(r"^o-[0-9a-f]{6}$")
BRANCH_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,120}$")
NEVER_DELETE = ("main", "HEAD", "master")
TIMEOUT = 1500


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def canonical(payload: dict) -> bytes:
    return json.dumps({k: v for k, v in payload.items() if k != "sig"}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def sign(key: bytes, payload: dict) -> str:
    return hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()


def load_key() -> Optional[bytes]:
    raw = os.environ.get("AURIX_GAMING_HMAC_KEY", "").strip()
    if not raw:
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("AURIX_GAMING_HMAC_KEY="):
                    raw = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    try:
        return bytes.fromhex(raw) if raw else None
    except ValueError:
        return None


def run(cmd: List[str], timeout: int = 300, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(SOURCE), capture_output=True, text=True, timeout=timeout, input=stdin)


def _tail(r: subprocess.CompletedProcess, n: int = 4) -> str:
    return "\n".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-n:])[-500:]


def remote() -> str:
    """The git remote that points at GitHub's repo (the sync script adds one by URL); falls back to origin."""
    r = run(["git", "remote", "-v"])
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and REPO.lower() in parts[1].lower() and parts[2:] == ["(push)"]:
            return parts[0]
    return "origin"


def patterns_file() -> Path:
    gd = run(["git", "rev-parse", "--git-dir"]).stdout.strip() or ".git"
    p = Path(gd)
    return (p if p.is_absolute() else SOURCE / p) / "info" / "aurix-private-patterns"


def candidates() -> List[Dict[str, str]]:
    rem = remote()
    run(["git", "fetch", "--prune", "-q", rem], timeout=180)
    refs = [x.strip() for x in run(["git", "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{rem}/"]).stdout.splitlines()]
    pats = patterns_file()
    out = []
    for ref in refs:
        name = ref[len(rem) + 1:] if ref.startswith(rem + "/") else ref
        if not BRANCH_RX.match(name) or name in NEVER_DELETE or name.startswith("dependabot/"):
            continue
        why = []
        if run(["git", "merge-base", "--is-ancestor", ref, f"{rem}/main"]).returncode == 0:
            why.append("already merged into main")
        if pats.is_file() and run(["git", "grep", "-q", "-i", "-I", "-E", "-f", str(pats), ref, "--", "."]).returncode == 0:
            why.append("its files contain your private patterns")
        if why:
            out.append({"name": name, "why": " and ".join(why)})
    return out


def op_update() -> Dict[str, Any]:
    r = run(["bash", "scripts/aurix_git_sync.sh", "sync"], timeout=600)
    if r.returncode != 0:
        return {"ok": False, "detail": "sync stopped (nothing deployed): " + _tail(r)}
    d = run(["bash", "scripts/aurix_deploy.sh"], timeout=TIMEOUT)
    return {"ok": d.returncode == 0, "detail": ("synced; " if d.returncode == 0 else "synced, but the deploy rolled back: ") + _tail(d, 3)}


def op_scan() -> Dict[str, Any]:
    c = candidates()
    return {"ok": True, "candidates": c, "detail": f"{len(c)} candidate(s)"}


def op_prune(names: List[str]) -> Dict[str, Any]:
    current = {c["name"] for c in candidates()}
    rem, done, skipped = remote(), [], []
    for n in names:
        if not isinstance(n, str) or not BRANCH_RX.match(n) or n in NEVER_DELETE or n not in current:
            skipped.append(str(n)[:60])
            continue
        r = run(["git", "push", rem, "--delete", n], timeout=120)
        (done if r.returncode == 0 else skipped).append(n)
    return {"ok": not skipped, "detail": f"deleted {', '.join(done) or 'none'}" + (f"; skipped {', '.join(skipped)}" if skipped else "")}


def op_secret() -> Dict[str, Any]:
    pats = patterns_file()
    if not pats.is_file():
        return {"ok": False, "detail": "no .git/info/aurix-private-patterns on this machine"}
    if not shutil.which("gh"):
        return {"ok": False, "detail": "the GitHub CLI is not installed here: sudo apt install gh, then gh auth login (once)"}
    if run(["gh", "auth", "status"], timeout=30).returncode != 0:
        return {"ok": False, "detail": "the GitHub CLI is not logged in: run gh auth login once on the server"}
    r = run(["gh", "secret", "set", "AURIX_PRIVATE_PATTERNS", "--repo", REPO], timeout=60, stdin=pats.read_text(encoding="utf-8"))
    n = len([x for x in pats.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")])
    return {"ok": r.returncode == 0, "detail": f"{n} patterns set" if r.returncode == 0 else _tail(r)}


def process(key: Optional[bytes]) -> List[Dict[str, Any]]:
    out = []
    ap = ROOT / "approved"
    for f in sorted(ap.glob("*.json")) if ap.is_dir() else []:
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            item = {}
        oid = str(item.get("id", ""))
        res: Dict[str, Any]
        if not ID_RX.match(oid) or f.stem != oid:
            res = {"ok": False, "detail": "malformed request"}
        elif key is None:
            res = {"ok": False, "detail": "AURIX_GAMING_HMAC_KEY is missing on the host"}
        elif not hmac.compare_digest(str(item.get("sig", "")), sign(key, item)):
            res = {"ok": False, "detail": "bad signature: refused"}
        elif str(item.get("expires", "")) < now_iso():
            res = {"ok": False, "detail": "expired"}
        elif item.get("op") not in OPS:
            res = {"ok": False, "detail": "operation not allowed"}
        else:
            try:
                op = item["op"]
                res = (op_update() if op == "update" else op_scan() if op == "scan" else
                       op_prune(list(b.get("name") if isinstance(b, dict) else b for b in item.get("branches") or [])) if op == "prune"
                       else op_secret())
            except subprocess.TimeoutExpired:
                res = {"ok": False, "detail": "timed out"}
            except Exception as ex:                                    # noqa: BLE001 - always report to the owner
                res = {"ok": False, "detail": f"{type(ex).__name__}: {ex}"[:300]}
        res.update({"id": oid or f.stem, "op": item.get("op"), "finished": now_iso()})
        atomic_write(ROOT / "results" / f"{f.stem}.json", res)
        f.unlink(missing_ok=True)
        out.append(res)
    return out


def cycle() -> None:
    for r in process(load_key()):
        print(now_iso(), "ops:", r["id"], r.get("op"), "ok" if r.get("ok") else "FAILED", "-", str(r.get("detail"))[:300], flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "cycle":
        cycle()
    elif cmd == "status":
        print("ops root:", ROOT, "| source:", SOURCE, "| key:", "present" if load_key() else "MISSING")
    else:
        print("usage: aurix_ops_agent.py [cycle|status]")
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
