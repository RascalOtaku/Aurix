#!/usr/bin/env python3
"""aurix_absorb_agent.py - the host-side half of "send AURIX a repo link" (stdlib only; run from cron every minute on the host).

  requests/<id>.json  ->  clone (shallow, hooks/filters/symlinks off)  ->  READ the files as text  ->  facts/<id>.json
  approved/<id>.json  ->  verify the HMAC signature + expiry  ->  absorb: move the snapshot into library/<owner>__<repo>/<commit7>/ (no .git)
                                                              or  discard: delete the working copy          ->  results/<id>.json

It NEVER executes, imports, installs or builds anything from a repo. It only reads bytes and matches text patterns. The brain half
(src/foundation/repos.py) judges the facts and asks the owner; this half does only what a signed approval says.
Layout under $AURIX_ABSORB_DIR (default ~/ai-brain-sync/sandbox/absorb, which Syncthing already ignores): requests/ facts/ approved/ results/ work/ library/

  python3 aurix_absorb_agent.py cycle      # what cron runs
  python3 aurix_absorb_agent.py status
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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(os.environ.get("AURIX_ABSORB_DIR") or (Path.home() / "ai-brain-sync" / "sandbox" / "absorb"))
ENV_FILE = Path(os.environ.get("AURIX_ENV_FILE") or (Path.home() / "ai-brain-sync" / "odysseus" / ".env"))
CLONE_TIMEOUT = 240
MAX_MB = 400
MAX_FILES = 6000
MAX_READ_BYTES = 1_500_000
WORK_DAYS = 7
ID_RX = re.compile(r"r-[0-9a-f]{6}")
OWNER_RX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RX = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
TEXT_EXT = {".py", ".sh", ".js", ".ts", ".mjs", ".cjs", ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".md", ".txt", ".rst", ".go", ".rs", ".rb", ".php", ".lua",
            ".java", ".c", ".h", ".cpp", ".cs", ".bat", ".ps1", ".mk", ""}
CODE_EXT = TEXT_EXT - {".md", ".txt", ".rst", ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml", ""}
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".apk", ".jar", ".dmg", ".pyd", ".o", ".a", ".class", ".whl"}

SUSPICIOUS = [
    (re.compile(r"\bos\.system\s*\("), "shell execution via os.system"),
    (re.compile(r"subprocess\.(?:run|call|Popen|check_output)\s*\([^)]*shell\s*=\s*True"), "shell=True subprocess call"),
    (re.compile(r"\beval\s*\("), "eval() use"),
    (re.compile(r"\bexec\s*\("), "exec() use"),
    (re.compile(r"\b(?:requests|urllib\.request|httpx)\b.{0,40}\bhttps?://(?!localhost|127\.)", re.S), "outbound web request"),
    (re.compile(r"\bsocket\.socket\s*\("), "raw socket usage"),
    (re.compile(r"(?i)\bapi[_-]?key\b\s*=\s*['\"][A-Za-z0-9_\-]{12,}"), "hardcoded-looking API key"),
    (re.compile(r"crontab|/etc/cron"), "cron/scheduled task changes"),
    (re.compile(r"chmod\s+\+?[0-7]{3,4}|chmod\s+[ugoa]*\+x"), "permission/executable bit changes"),
]
PIPE_SHELL = re.compile(r"(?:curl|wget)\s[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")
OBFUSCATED = re.compile(r"(?:exec|eval)\s*\(\s*(?:base64|codecs|zlib|marshal|bytes\.fromhex|__import__\(['\"]base64|atob\s*\()")
LONG_BLOB = re.compile(r"[A-Za-z0-9+/=]{600,}")
KEY_FILES = re.compile(r"\.ssh/|id_rsa|id_ed25519|authorized_keys|\.aws/credentials|\.netrc|\.gnupg|/etc/shadow|keychain")
NETWORK = re.compile(r"https?://|requests\.|urllib|socket\.|fetch\(|axios|curl |wget ")


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


# ---------------------------------------------------------------------------------------------------------------------------------
# clone + read
# ---------------------------------------------------------------------------------------------------------------------------------

def git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(ROOT / "home")})
    return env


def clone(owner: str, repo: str, dest: Path) -> str:
    """Shallow clone of ONE github.com repo. No hooks, no filters (LFS), no symlinks, no submodules, no credentials. Returns "" or an error."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    (ROOT / "home").mkdir(parents=True, exist_ok=True)
    cmd = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "core.symlinks=false", "-c", "core.hooksPath=/dev/null",
           "-c", "core.fsmonitor=false", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
           "clone", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules", f"https://github.com/{owner}/{repo}.git", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT, env=git_env())
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return f"the download took longer than {CLONE_TIMEOUT}s and was stopped"
    except OSError as ex:
        return f"git could not start: {ex}"
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        msg = (r.stderr or r.stdout or "clone failed").strip().splitlines()
        return ("repository not found or not public" if any("not found" in x.lower() or "could not read" in x.lower() for x in msg) else (msg[-1] if msg else "clone failed"))[:200]
    return ""


def walk(dest: Path):
    """(relative path, absolute path) of regular files only, skipping .git and never following symlinks."""
    n = 0
    for dp, dns, fns in os.walk(dest, followlinks=False):
        dns[:] = [d for d in dns if d != ".git" and not os.path.islink(os.path.join(dp, d))]
        for fn in fns:
            p = Path(dp) / fn
            if p.is_symlink() or not p.is_file():
                continue
            n += 1
            if n > MAX_FILES:
                return
            yield p.relative_to(dest).as_posix(), p


def parse_deps(dest: Path) -> List[str]:
    deps: List[str] = []
    req = dest / "requirements.txt"
    if req.is_file():
        for ln in req.read_text(errors="ignore").splitlines()[:200]:
            ln = ln.strip()
            if ln and not ln.startswith(("#", "-")):
                deps.append(re.split(r"[<>=!~;\[ ]", ln)[0])
    pj = dest / "package.json"
    if pj.is_file():
        try:
            d = json.loads(pj.read_text(errors="ignore"))
            deps += list((d.get("dependencies") or {}))[:60]
        except ValueError:
            pass
    gm = dest / "go.mod"
    if gm.is_file():
        deps += [ln.split()[0] for ln in gm.read_text(errors="ignore").splitlines() if ln.startswith("\t") and ln.split()][:60]
    return [d for d in dict.fromkeys(deps) if d][:80]


def install_hooks(dest: Path) -> List[str]:
    hooks: List[str] = []
    pj = dest / "package.json"
    if pj.is_file():
        try:
            for k in ("preinstall", "install", "postinstall", "prepare"):
                if k in (json.loads(pj.read_text(errors="ignore")).get("scripts") or {}):
                    hooks.append(f"package.json {k} script")
        except ValueError:
            pass
    if (dest / "setup.py").is_file():
        hooks.append("setup.py (runs on pip install)")
    for name in ("install.sh", "Makefile", "Dockerfile"):
        if (dest / name).is_file():
            hooks.append(name)
    return hooks


def license_of(dest: Path) -> str:
    for p in sorted(dest.glob("*")):
        if p.is_file() and re.match(r"(?i)^(licen[sc]e|copying|unlicense)", p.name):
            t = p.read_text(errors="ignore")[:4000].lower()
            for needle, name in (("mit license", "MIT"), ("apache license", "Apache-2.0"), ("gnu affero", "AGPL"), ("gnu general public", "GPL"), ("gnu lesser", "LGPL"),
                                 ("bsd", "BSD"), ("mozilla public", "MPL-2.0"), ("unlicense", "Unlicense"), ("creative commons", "Creative Commons")):
                if needle in t:
                    return name
            return p.name
    return ""


def facts_for(rid: str, owner: str, repo: str, dest: Path) -> Dict[str, Any]:
    exts: Dict[str, int] = {}
    total = 0
    n = 0
    sus: List[Dict[str, Any]] = []
    hard: Dict[str, List[str]] = {"pipe_to_shell": [], "obfuscated_exec": [], "key_files_and_network": []}
    binaries: List[Dict[str, Any]] = []
    tests = 0
    for rel, p in walk(dest):
        n += 1
        ext = p.suffix.lower()
        exts[ext] = exts.get(ext, 0) + 1
        try:
            size = p.stat().st_size
        except OSError:
            continue
        total += size
        if ext in BINARY_EXT:
            binaries.append({"name": rel, "kb": round(size / 1024)})
            continue
        if re.search(r"(^|/)(tests?|__tests__)/|test_[^/]*\.py$|_test\.(py|go)$|\.test\.[jt]s$", rel):
            tests += 1
        if ext not in TEXT_EXT or size > MAX_READ_BYTES:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        is_doc = ext in (".md", ".txt", ".rst")
        if not is_doc:
            if PIPE_SHELL.search(text) and len(hard["pipe_to_shell"]) < 5:
                hard["pipe_to_shell"].append(rel)
            if (OBFUSCATED.search(text) or (LONG_BLOB.search(text) and re.search(r"\b(?:exec|eval)\s*\(", text))) and len(hard["obfuscated_exec"]) < 5:
                hard["obfuscated_exec"].append(rel)
            if ext in CODE_EXT and KEY_FILES.search(text) and NETWORK.search(text) and len(hard["key_files_and_network"]) < 5:
                hard["key_files_and_network"].append(rel)
        if ext in CODE_EXT and len(sus) < 40:
            for rx, label in SUSPICIOUS:
                m = rx.search(text)
                if m:
                    sus.append({"file": rel[:120], "line": text[:m.start()].count("\n") + 1, "what": label})
    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        if (dest / name).is_file():
            readme = (dest / name).read_text(errors="ignore")[:3000]
            break
    try:
        r = subprocess.run(["git", "-C", str(dest), "log", "-1", "--format=%H|%cI"], capture_output=True, text=True, timeout=20, env=git_env())
        commit, _, cdate = (r.stdout.strip() or "|").partition("|")
    except (OSError, subprocess.TimeoutExpired):
        commit, cdate = "", ""
    return {"schema": 1, "id": rid, "owner": owner, "repo": repo, "commit": commit, "commit_date": cdate, "analyzed": now_iso(), "files": n, "size_mb": round(total / 1e6, 1),
            "extensions": dict(sorted(exts.items(), key=lambda t: -t[1])[:12]), "top_level": sorted(x.name for x in dest.iterdir() if x.name != ".git")[:30],
            "license": license_of(dest), "dependencies": parse_deps(dest), "install_hooks": install_hooks(dest), "tests": tests, "binaries": binaries[:12],
            "suspicious": sus, "hard_flags": {k: v for k, v in hard.items() if v}, "readme": readme, "error": ""}


# ---------------------------------------------------------------------------------------------------------------------------------
# cycle
# ---------------------------------------------------------------------------------------------------------------------------------

def process_requests() -> List[str]:
    done = []
    for f in sorted((ROOT / "requests").glob("*.json")) if (ROOT / "requests").exists() else []:
        try:
            req = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rid, owner, repo = str(req.get("id", "")), str(req.get("owner", "")), str(req.get("repo", ""))
        if not ID_RX.fullmatch(rid) or (ROOT / "facts" / f"{rid}.json").exists():
            continue
        if not (OWNER_RX.match(owner) and REPO_RX.match(repo)) or ".." in repo:
            atomic_write(ROOT / "facts" / f"{rid}.json", {"id": rid, "error": "that is not a plain github.com/owner/repo link"})
            continue
        dest = ROOT / "work" / rid
        err = clone(owner, repo, dest)
        if not err:
            size = sum(p.stat().st_size for _, p in walk(dest))
            if size > MAX_MB * 1e6:
                shutil.rmtree(dest, ignore_errors=True)
                err = f"too big ({round(size / 1e6)} MB, my limit is {MAX_MB} MB)"
        atomic_write(ROOT / "facts" / f"{rid}.json", {"id": rid, "error": err} if err else facts_for(rid, owner, repo, dest))
        try:
            f.unlink()
        except OSError:
            pass
        done.append(rid)
    return done


def process_approved(key: Optional[bytes]) -> List[Dict[str, Any]]:
    results = []
    adir = ROOT / "approved"
    for f in sorted(adir.glob("*.json")) if adir.exists() else []:
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rid = str(item.get("id", ""))
        if not ID_RX.fullmatch(rid) or (ROOT / "results" / f"{rid}.json").exists():
            continue

        def finish(status: str, detail: Any) -> None:
            res = {"id": rid, "kind": item.get("kind"), "status": status, "detail": detail, "at": now_iso()}
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
            finish("refused", "bad signature: not applied")
            continue
        try:
            if datetime.fromisoformat(item["expires"]) < datetime.now().astimezone():
                finish("refused", "approval expired (24 h); send the link again")
                continue
        except (KeyError, ValueError):
            finish("refused", "missing or invalid expiry")
            continue
        owner, repo, dest = str(item.get("owner", "")), str(item.get("repo", "")), ROOT / "work" / rid
        try:
            if item.get("kind") == "discard":
                shutil.rmtree(dest, ignore_errors=True)
                finish("applied", "working copy deleted")
            elif item.get("kind") == "absorb":
                if not (OWNER_RX.match(owner) and REPO_RX.match(repo)) or ".." in repo or not dest.is_dir():
                    raise ValueError("no working copy to store (it may have expired); send the link again")
                commit = str(item.get("commit", ""))[:7] or "unknown"
                lib = ROOT / "library" / f"{owner}__{repo}" / commit
                if lib.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                    finish("applied", f"already in the library at library/{owner}__{repo}/{commit}")
                    continue
                shutil.rmtree(dest / ".git", ignore_errors=True)
                lib.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(lib))
                reg_path = ROOT / "library" / "registry.json"
                try:
                    reg = json.loads(reg_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    reg = []
                reg.append({"id": rid, "repo": f"{owner}/{repo}", "commit": item.get("commit", ""), "path": str(lib.relative_to(ROOT)), "stored": now_iso(),
                            "note": "UNTRUSTED third-party code: stored inert, never run or imported by AURIX"})
                atomic_write(reg_path, reg)
                finish("applied", f"stored at library/{owner}__{repo}/{commit} (inert, no .git)")
            else:
                finish("refused", f"unknown action {item.get('kind')!r}")
        except Exception as ex:                                                  # noqa: BLE001 - report every failure to the owner
            finish("failed", f"{type(ex).__name__}: {ex}")
    return results


def clean_old() -> None:
    wd = ROOT / "work"
    for d in wd.iterdir() if wd.exists() else []:
        try:
            if time.time() - d.stat().st_mtime > WORK_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _paused(shard_id: str) -> bool:
    """The owner's pause switch (shards.py writes this file; stdlib copy of the check so cron jobs honour it)."""
    try:
        d = Path(os.environ.get("AURIX_SHARDS_DIR") or (Path.home() / "ai-brain-sync" / "sandbox" / "shards"))
        return shard_id in json.loads((d / "paused.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def cycle() -> None:
    if _paused("absorb"):
        return
    done = process_requests()
    results = process_approved(load_key())
    clean_old()
    if done or results:                                                          # cron appends stdout to logs/absorb.log; quiet when idle
        print(now_iso(), "absorb: facts for", done or "-", "| actions:", [(r["id"], r["kind"], r["status"]) for r in results] or "-", flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "cycle":
        cycle()
    elif cmd == "status":
        print("root:", ROOT, "| key:", "present" if load_key() else "MISSING", "| requests waiting:", len(list((ROOT / "requests").glob("*.json"))) if (ROOT / "requests").exists() else 0)
    else:
        print("usage: aurix_absorb_agent.py [cycle|status]")
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
