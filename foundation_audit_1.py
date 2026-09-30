#!/usr/bin/env python3
"""foundation_audit_1.py — AURIX Foundation Audit 1

Read-only inventory of the current system, per the activation handoff (§10.1):
"run the existing read-only inventory script on whatever machine now holds
the AURIX repo; label every existing component Keep / Repair / Replace /
Retire before writing anything new."

This script performs NO writes, NO service restarts, NO file modifications.
It only reads, checks, and reports. Output is a structured report you (or a
future Claude session) can use to make the actual Keep/Repair/Replace/Retire
calls — this script surfaces facts, it doesn't make that judgment for you.

Run from anywhere; paths are resolved relative to this script's location
and the user's home directory.
"""
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
REPORT = {"generated_at": datetime.now().isoformat(), "sections": {}}


def run(cmd, timeout=10):
    """Run a shell command, return (stdout, returncode). Never raises."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return f"[error: {e}]", -1


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ── 1. What's actually running ──────────────────────────────────────────
def audit_processes():
    section("1. PROCESSES — AURIX/odysseus-related, currently running")
    out, _ = run("ps aux | grep -iE 'aurix|odysseus|supervisor.py|watch.py|telegram_listener|cortex_api' | grep -v grep")
    print(out or "(none found)")
    REPORT["sections"]["processes"] = out


def audit_services():
    section("2. SYSTEMD SERVICES — AURIX-related units and their state")
    out, _ = run("systemctl list-units --type=service --all 2>/dev/null | grep -iE 'aurix|odysseus'")
    print(out or "(none found)")
    REPORT["sections"]["systemd_services"] = out


def audit_docker():
    section("3. DOCKER CONTAINERS")
    out, rc = run("docker ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}' 2>/dev/null")
    if rc != 0:
        out = "(docker not available or permission denied — try with sudo)"
    print(out or "(no containers)")
    REPORT["sections"]["docker"] = out


def audit_ports():
    section("4. LISTENING PORTS (informational — cross-check against services above)")
    out, rc = run("ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null")
    print(out or "(could not enumerate — ss/netstat unavailable or needs sudo)")
    REPORT["sections"]["listening_ports"] = out


# ── 2. Known technical debt from the activation handoff (§4) ───────────
def audit_known_issues():
    section("5. KNOWN ISSUES FROM ACTIVATION HANDOFF §4")
    findings = {}

    # AUTH_ENABLED = False in odysseus/app.py
    app_py_candidates = list(HOME.glob("**/odysseus/app.py"))
    app_py_candidates = [p for p in app_py_candidates if "venv" not in str(p) and "site-packages" not in str(p)]
    if app_py_candidates:
        for p in app_py_candidates:
            try:
                text = p.read_text(errors="ignore")
                m = re.search(r"AUTH_ENABLED\s*=\s*(\w+)", text)
                status = m.group(1) if m else "not found in app.py (may be env-driven)"
                findings[f"AUTH_ENABLED in {p}"] = status
                print(f"  AUTH_ENABLED in {p}: {status}")
            except Exception as e:
                print(f"  Could not read {p}: {e}")
    else:
        print("  No odysseus/app.py found under home directory.")
        findings["AUTH_ENABLED"] = "app.py not found"

    # Also check .env files for AUTH_ENABLED, since app.py may read from env
    env_candidates = [p for p in HOME.glob("**/odysseus/.env") if "venv" not in str(p)]
    for p in env_candidates:
        try:
            text = p.read_text(errors="ignore")
            m = re.search(r"AUTH_ENABLED\s*=\s*(\w+)", text)
            if m:
                print(f"  AUTH_ENABLED in {p}: {m.group(1)}")
                findings[f"AUTH_ENABLED in {p}"] = m.group(1)
        except Exception:
            pass

    # SSH password auth
    sshd_config = Path("/etc/ssh/sshd_config")
    if sshd_config.exists():
        try:
            text = sshd_config.read_text(errors="ignore")
            m = re.search(r"^\s*PasswordAuthentication\s+(\w+)", text, re.MULTILINE)
            pw_auth = m.group(1) if m else "not explicitly set (check sshd_config.d/*.conf too — default is 'yes')"
            print(f"  SSH PasswordAuthentication: {pw_auth}")
            findings["ssh_password_auth"] = pw_auth
            # Also check drop-in config dir, since modern sshd often splits config there
            dropin_dir = Path("/etc/ssh/sshd_config.d")
            if dropin_dir.exists():
                for f in dropin_dir.glob("*.conf"):
                    dtext = f.read_text(errors="ignore")
                    dm = re.search(r"^\s*PasswordAuthentication\s+(\w+)", dtext, re.MULTILINE)
                    if dm:
                        print(f"  SSH PasswordAuthentication (in {f.name}): {dm.group(1)}")
                        findings[f"ssh_password_auth_{f.name}"] = dm.group(1)
        except PermissionError:
            print("  /etc/ssh/sshd_config not readable without sudo — run with sudo for this check")
            findings["ssh_password_auth"] = "permission denied"
    else:
        print("  /etc/ssh/sshd_config not found")

    # Curator import error — search for a curator module and try importing it
    curator_candidates = [p for p in HOME.glob("**/curator*.py") if "venv" not in str(p) and "site-packages" not in str(p)]
    if curator_candidates:
        for p in curator_candidates[:5]:
            print(f"  Found curator file: {p}")
            findings.setdefault("curator_files", []).append(str(p))
    else:
        print("  No curator*.py file found under home directory — cannot verify import error claim")
        findings["curator_files"] = "none found"

    REPORT["sections"]["known_issues"] = findings


# ── 3. Repo structure — top-level inventory of what exists ─────────────
def audit_repo_structure():
    section("6. REPO STRUCTURE — top-level odysseus components")
    odysseus_dirs = [p for p in HOME.glob("**/odysseus") if p.is_dir() and "venv" not in str(p) and ".git" not in str(p)]
    if not odysseus_dirs:
        print("  No odysseus directory found.")
        REPORT["sections"]["repo_structure"] = "not found"
        return

    for od in odysseus_dirs:
        print(f"\n  odysseus root: {od}")
        for sub in sorted(od.iterdir()):
            if sub.name.startswith(".") or sub.name in ("venv", "__pycache__", "node_modules"):
                continue
            if sub.is_dir():
                count = sum(1 for _ in sub.rglob("*.py"))
                print(f"    {sub.name}/  ({count} .py files)")
            elif sub.suffix == ".py":
                print(f"    {sub.name}")
    REPORT["sections"]["repo_structure"] = [str(d) for d in odysseus_dirs]


# ── 4. Dependency sanity — requirements vs installed ────────────────────
def audit_dependencies():
    section("7. DEPENDENCIES — requirements.txt vs installed (in current venv, if active)")
    in_venv = sys.prefix != sys.base_prefix
    print(f"  Running inside a venv: {in_venv}")
    if not in_venv:
        print("  Skipping install check — activate the project venv and re-run for this section.")
        REPORT["sections"]["dependencies"] = "skipped (no venv active)"
        return

    req_files = [p for p in HOME.glob("**/odysseus/requirements.txt") if "venv" not in str(p)]
    if not req_files:
        print("  No requirements.txt found.")
        REPORT["sections"]["dependencies"] = "requirements.txt not found"
        return

    req_path = req_files[0]
    out, rc = run(f"pip freeze 2>/dev/null")
    installed = {}
    for line in out.splitlines():
        if "==" in line:
            name, _, ver = line.partition("==")
            installed[name.lower()] = ver

    missing = []
    for line in req_path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pkg = re.split(r"[<>=\[]", line)[0].strip().lower()
        if pkg and pkg not in installed:
            missing.append(line)

    if missing:
        print(f"  {len(missing)} requirement(s) not found in current venv's installed packages:")
        for m in missing[:20]:
            print(f"    - {m}")
    else:
        print("  All requirements.txt entries appear to be installed.")
    REPORT["sections"]["dependencies"] = {"missing": missing}


# ── 5. Duplicate / orphaned paths ───────────────────────────────────────
def audit_orphans():
    section("8. ORPHANED / DUPLICATE PATHS — stale references worth checking by hand")
    checks = []
    # Old pre-rename home dir, if it somehow still exists or is referenced
    old_home = Path("/home/rascal")
    checks.append(("/home/rascal (pre-rename username)", old_home.exists()))
    # Sync conflict files anywhere in the tree
    out, _ = run(f"find {HOME} -iname '*sync-conflict*' 2>/dev/null | grep -v venv")
    checks.append(("sync-conflict files present", bool(out.strip())))
    if out.strip():
        for line in out.splitlines()[:10]:
            print(f"    conflict: {line}")

    for label, found in checks:
        marker = "FOUND" if found else "clear"
        print(f"  [{marker}] {label}")
    REPORT["sections"]["orphans"] = {label: found for label, found in checks}


# ── NEW: Aurix-specific checks ─────────────────────────────────────────
def audit_aurix():
    section("9. AURIX-SPECIFIC CHECKS — new framework components")
    findings = {}
    
    # Check for Aurix repo
    aurix_dirs = [p for p in HOME.glob("**/Aurix") if p.is_dir() and ".git" in str(p)]
    if aurix_dirs:
        aurix_root = aurix_dirs[0]
        print(f"  Aurix repo found at: {aurix_root}")
        findings["repo_root"] = str(aurix_root)
        
        # New modules we just added
        new_modules = [
            ("src/model_router.py", "Model routing by task type"),
            ("src/mcp_auto_discover.py", "MCP auto-discovery from environment"),
            ("AURIX_FIXES.md", "Documentation of improvements"),
        ]
        
        print("\n  New modules (status):")
        for path, desc in new_modules:
            full_path = aurix_root / path
            status = "✓ found" if full_path.exists() else "✗ missing"
            print(f"    [{status}] {path} — {desc}")
            findings[path] = status
        
        # Check for core modules
        core_modules = [
            "src/api_key_manager.py",
            "src/agent_loop.py",
            "src/mcp_manager.py",
            "src/llm_core.py",
            "src/endpoint_resolver.py",
            "src/model_discovery.py",
            "core/database.py",
            "routes/model_routes.py",
        ]
        
        print("\n  Core modules (status):")
        for mod in core_modules:
            full_path = aurix_root / mod
            status = "✓" if full_path.exists() else "✗"
            print(f"    [{status}] {mod}")
            findings[f"core_{mod}"] = "present" if full_path.exists() else "missing"
        
        # Check requirements
        req = aurix_root / "requirements.txt"
        if req.exists():
            text = req.read_text(errors="ignore")
            findings["requirements_lines"] = len(text.splitlines())
            print(f"\n  requirements.txt: {findings['requirements_lines']} lines")
        
        # Check for .env or config
        env_file = aurix_root / ".env"
        if env_file.exists():
            print(f"  .env file: present")
            findings["env_present"] = True
        else:
            print(f"  .env file: not found (check .env.example)")
            findings["env_present"] = False
    else:
        print("  No Aurix repo found in home directory tree")
        findings["repo_found"] = False
    
    REPORT["sections"]["aurix"] = findings


def main():
    print(f"AURIX Foundation Audit 1 — {REPORT['generated_at']}")
    print(f"Host: {socket.gethostname()}  |  User: {os.environ.get('USER', '?')}  |  Home: {HOME}")

    audit_processes()
    audit_services()
    audit_docker()
    audit_ports()
    audit_known_issues()
    audit_repo_structure()
    audit_dependencies()
    audit_orphans()
    audit_aurix()

    section("DONE")
    out_path = HOME / f"foundation_audit_1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        out_path.write_text(json.dumps(REPORT, indent=2, default=str))
        print(f"Full structured report written to: {out_path}")
    except Exception as e:
        print(f"Could not write JSON report: {e}")
    print("\nThis script made NO changes. Everything above is read-only inventory.")
    print("Next: go through each section and assign Keep / Repair / Replace / Retire by hand.")


if __name__ == "__main__":
    main()
