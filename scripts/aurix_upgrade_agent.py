#!/usr/bin/env python3
"""aurix_upgrade_agent.py - the host-side half of the upgrade lane (stdlib only; cron every minute on the host, under flock).

  approved/<id>.json (+ payload/<id>.json)  ->  verify the HMAC signature, expiry, every path, and that each file on disk is EXACTLY what the proposal
      was made against  ->  back up the originals  ->  write the new files  ->  py_compile them  ->  scripts/aurix_deploy.sh (build + health check;
      it rolls the running app back by itself if the new build is unhealthy)  ->  if the deploy did not succeed: put the source files back
  approved/undo-<id>.json  ->  verify  ->  restore the backed-up files (only if nobody changed them since)  ->  redeploy
  results/<id>.json         what happened, for the app to tell the owner

The app decides nothing here and this agent decides nothing about WHAT to change: it only does what a signed approval names, inside paths that are
re-checked below (a second, independent copy of the app's rules), and never touches protected components. Files land in the synced source folder,
so the change also appears on the owner's PC.

Layout under $AURIX_UPGRADES_DIR (default ~/ai-brain-sync/sandbox/upgrades): approved/ payload/ results/ backups/
Source root: $AURIX_SOURCE_DIR (default ~/ai-brain-sync/odysseus).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(os.environ.get("AURIX_UPGRADES_DIR") or (Path.home() / "ai-brain-sync" / "sandbox" / "upgrades"))
SOURCE = Path(os.environ.get("AURIX_SOURCE_DIR") or (Path.home() / "ai-brain-sync" / "odysseus"))
ENV_FILE = Path(os.environ.get("AURIX_ENV_FILE") or (SOURCE / ".env"))
DEPLOY_TIMEOUT = 1500
ID_RX = re.compile(r"u-[0-9a-f]{6}")
NEW_TEST_RX = re.compile(r"^tests/test_upgrade_[a-z0-9_]{3,40}\.py$")
NEW_MODULE_RX = re.compile(r"^src/foundation/[a-z][a-z0-9_]{2,40}\.py$")
ALLOW_PREFIXES = ("src/foundation/", "static/command.html")
# An independent copy of the hard "never" list (the app's identity.PROTECTED_COMPONENTS is the source of truth and is also consulted below).
NEVER = ("src/foundation/identity.py", "src/foundation/risk.py", "src/foundation/audit.py", "src/foundation/watchdog.py", "src/foundation/commands.py",
         "src/foundation/mission.py", "src/foundation/runner.py", "src/foundation/standing.py", "src/foundation/sandbox.py", "src/foundation/forge.py",
         "src/foundation/upgrades.py", "src/foundation/__init__.py")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def canonical(payload: dict) -> bytes:
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


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


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def path_problem(rel: str, exists: bool) -> Optional[str]:
    p = str(rel or "").replace("\\", "/")
    if not p or p.startswith("/") or ".." in p.split("/") or "\x00" in p:
        return f"bad path {rel!r}"
    if p in NEVER or p.endswith((".env", "auth.json")) or "/.ssh/" in p:
        return f"{p} is protected"
    if exists:
        if not p.startswith(ALLOW_PREFIXES):
            return f"{p} is outside the areas an upgrade may change"
    elif not (NEW_TEST_RX.match(p) or NEW_MODULE_RX.match(p)):
        return f"{p} is not a path an upgrade may create"
    try:                                                    # the app's own list, read from the CURRENT source (before any change is applied)
        sys.path.insert(0, str(SOURCE))
        from src.foundation import identity                  # type: ignore
        if identity.is_protected_path(p):
            return f"{p} is a protected component"
    except Exception:                                       # noqa: BLE001 - the hard-coded list above still stands
        pass
    finally:
        if str(SOURCE) in sys.path:
            sys.path.remove(str(SOURCE))
    return None


def deploy() -> Dict[str, Any]:
    """The safety-net deploy. Exit 0 = the new build is healthy; anything else = it already rolled the RUNNING app back."""
    try:
        r = subprocess.run(["bash", "scripts/aurix_deploy.sh"], cwd=str(SOURCE), capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "the deploy timed out"}
    tail = "\n".join((r.stdout or "").strip().splitlines()[-3:])
    return {"ok": r.returncode == 0, "out": tail[-400:]}


def _restore(backup_dir: Path, files: List[dict]) -> None:
    for f in files:
        rel = f["path"]
        b = backup_dir / rel
        target = SOURCE / rel
        if f.get("was_new") or not b.exists():
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(b, target)


def apply_upgrade(item: dict, payload: Dict[str, str]) -> Dict[str, Any]:
    uid = item["id"]
    files = item.get("files") or []
    if not files or len(files) > 6:
        raise ValueError("no files, or too many")
    plan = []
    for f in files:
        rel = str(f.get("path", ""))
        target = SOURCE / rel
        exists = target.is_file()
        bad = path_problem(rel, exists)
        if bad:
            raise ValueError(bad)
        new = payload.get(rel)
        if not isinstance(new, str) or sha(new) != f.get("sha_new"):
            raise ValueError(f"{rel}: the payload does not match what was approved")
        cur = target.read_text(encoding="utf-8") if exists else ""
        if (sha(cur) if exists else "") != f.get("sha_old", ""):
            raise ValueError(f"{rel} has changed since the proposal was made (someone edited it); ask for a fresh upgrade")
        plan.append({"path": rel, "was_new": not exists, "sha_new": f["sha_new"]})
    bdir = ROOT / "backups" / uid
    for p in plan:
        if not p["was_new"]:
            (bdir / p["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / p["path"], bdir / p["path"])
    atomic_write(bdir / "meta.json", {"files": plan, "at": now_iso()})
    try:
        for p in plan:
            t = SOURCE / p["path"]
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(payload[p["path"]], encoding="utf-8")
            if p["path"].endswith(".py"):
                with tempfile.TemporaryDirectory() as td:
                    py_compile.compile(str(t), cfile=os.path.join(td, "x.pyc"), doraise=True)
    except Exception:
        _restore(bdir, plan)
        raise
    d = deploy()
    if not d["ok"]:
        _restore(bdir, plan)
        return {"status": "rolled_back", "detail": "the new build did not come up healthy, so the old code was put back. " + d["out"]}
    return {"status": "applied", "detail": f"applied {len(plan)} file(s) and the new build is healthy. " + d["out"].splitlines()[0] if d["out"] else "applied"}


def undo_upgrade(item: dict) -> Dict[str, Any]:
    uid = item["id"]
    bdir = ROOT / "backups" / uid
    try:
        meta = json.loads((bdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("no backup for that upgrade on this machine")
    for f in meta["files"]:
        cur = (SOURCE / f["path"]).read_text(encoding="utf-8") if (SOURCE / f["path"]).is_file() else None
        if cur is None or sha(cur) != f["sha_new"]:
            raise ValueError(f"{f['path']} has changed since the upgrade was applied; not overwriting it. The original is in {bdir}")
    _restore(bdir, meta["files"])
    d = deploy()
    return {"status": "undone" if d["ok"] else "failed", "detail": "the old files are back" + (" and the app was rebuilt." if d["ok"] else "; the rebuild did not come up, see the deploy log. " + d["out"])}


def process_approved(key: Optional[bytes]) -> List[Dict[str, Any]]:
    results = []
    adir = ROOT / "approved"
    for f in sorted(adir.glob("*.json")) if adir.exists() else []:
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        uid = str(item.get("id", ""))
        is_undo = item.get("kind") == "undo"
        rid = ("undo-" + uid) if is_undo else uid
        if not ID_RX.fullmatch(uid) or (ROOT / "results" / f"{rid}.json").exists():
            continue

        def finish(status: str, detail: Any) -> None:
            res = {"id": uid, "kind": item.get("kind"), "status": status, "detail": detail, "at": now_iso()}
            atomic_write(ROOT / "results" / f"{rid}.json", res)
            results.append(res)
            try:
                (adir / "done").mkdir(exist_ok=True)
                shutil.move(str(f), str(adir / "done" / f.name))
            except OSError:
                pass

        if key is None:
            finish("refused", "no signing key on this machine")
            continue
        if not hmac.compare_digest(str(item.get("sig", "")), sign(key, item)):
            finish("refused", "bad signature: nothing was changed")
            continue
        try:
            if datetime.fromisoformat(item["expires"]) < datetime.now().astimezone():
                finish("refused", "approval expired (24 h); ask for a fresh upgrade")
                continue
        except (KeyError, ValueError):
            finish("refused", "missing or invalid expiry")
            continue
        try:
            if is_undo:
                r = undo_upgrade(item)
            elif item.get("kind") == "upgrade":
                payload = json.loads((ROOT / "payload" / f"{uid}.json").read_text(encoding="utf-8"))
                r = apply_upgrade(item, payload)
            else:
                finish("refused", f"unknown action {item.get('kind')!r}")
                continue
            finish(r["status"], r["detail"])
        except Exception as ex:                                                  # noqa: BLE001 - always report to the owner
            finish("failed", f"{type(ex).__name__}: {ex}")
    return results


def _paused(shard_id: str) -> bool:
    """The owner's pause switch (shards.py writes this file; stdlib copy of the check so cron jobs honour it)."""
    try:
        d = Path(os.environ.get("AURIX_SHARDS_DIR") or (Path.home() / "ai-brain-sync" / "sandbox" / "shards"))
        return shard_id in json.loads((d / "paused.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def cycle() -> None:
    if _paused("upgrade_applier"):
        return
    results = process_approved(load_key())
    for r in results:                                                            # cron appends stdout to logs/upgrade.log; quiet when idle
        print(now_iso(), "upgrade:", r["id"], r["kind"], r["status"], "-", str(r["detail"])[:300], flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "cycle":
        cycle()
    elif cmd == "status":
        print("root:", ROOT, "| source:", SOURCE, "| key:", "present" if load_key() else "MISSING",
              "| waiting:", len(list((ROOT / "approved").glob("*.json"))) if (ROOT / "approved").exists() else 0)
    else:
        print("usage: aurix_upgrade_agent.py [cycle|status]")
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
