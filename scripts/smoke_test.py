#!/usr/bin/env python3
"""
Aurix fresh-install smoke test.

Runs on Linux (e.g. the OptiPlex 7070) and Windows (e.g. Steammachine)
with only the Python standard library. No third-party packages needed.

What it does:
  1. Preflight: Python version, `docker` and `docker compose` availability.
  2. `docker compose up -d --build` (skip the build with --no-build).
  3. Waits for the app's /api/health to report healthy.
  4. Runs endpoint checks: /api/health, /api/version,
     /api/health/aggregate (per-service table), the UI index page
     (verifies Aurix branding), and /login.
  5. Checks `docker compose ps` for the expected services.
  6. Probes bundled integrations directly (ChromaDB, SearXNG) — informational.
  7. Prints a PASS/FAIL/WARN/SKIP report and exits 0 (all green) or 1.

Usage:
  Linux:    python3 scripts/smoke_test.py [--no-build] [--teardown]
  Windows:  python scripts\\smoke_test.py [--no-build] [--teardown]

Options:
  --no-build       Skip `docker compose build` (faster re-runs).
  --teardown       Run `docker compose down` after the checks.
  --port PORT      App port (default: $APP_PORT or 7000).
  --wait SECONDS   How long to wait for healthy (default: 300).
  --compose FILE   Compose file (default: docker-compose.yml in repo root).
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = int(os.environ.get("APP_PORT", "7000"))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Reporter:
    def __init__(self):
        self.results = []  # (name, status, detail)

    def add(self, name, status, detail=""):
        self.results.append((name, status, detail))
        tag = "[%s]" % status
        print("%-6s %-28s %s" % (tag, name, detail), flush=True)

    def summary(self):
        counts = {}
        for _, s, _ in self.results:
            counts[s] = counts.get(s, 0) + 1
        print("\n==== summary ====")
        for s in (PASS, FAIL, WARN, SKIP):
            if s in counts:
                print("  %s: %d" % (s, counts[s]))
        return 0 if counts.get(FAIL, 0) == 0 else 1


def run(cmd, cwd=None, timeout=1200):
    """Run a command (arg list, no shell) and return (rc, stdout+stderr tail)."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
        return p.returncode, p.stdout[-4000:]
    except FileNotFoundError:
        return 127, "command not found: %s" % cmd[0]
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, "timed out after %ss\n%s" % (timeout, out[-4000:])


def http_get(url, timeout=10):
    """GET a URL. Returns (status_code, body_text) or (None, error)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return None, "connection failed: %s" % e


def main():
    ap = argparse.ArgumentParser(description="Aurix fresh-install smoke test")
    ap.add_argument("--no-build", action="store_true", help="skip docker compose build")
    ap.add_argument("--teardown", action="store_true", help="docker compose down afterwards")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--wait", type=int, default=300, dest="wait_secs")
    ap.add_argument("--compose", default="docker-compose.yml")
    args = ap.parse_args()

    rep = Reporter()
    compose_file = REPO_ROOT / args.compose
    base = "http://127.0.0.1:%d" % args.port

    print("Aurix smoke test — repo: %s" % REPO_ROOT, flush=True)
    print("App base URL: %s\n" % base, flush=True)

    # ---- preflight ----
    if sys.version_info < (3, 8):
        rep.add("preflight:python", FAIL, "need >= 3.8, have %s" % sys.version.split()[0])
        return rep.summary()
    rep.add("preflight:python", PASS, sys.version.split()[0])

    rc, out = run(["docker", "--version"], timeout=30)
    if rc != 0:
        rep.add("preflight:docker", FAIL, out.strip().splitlines()[-1] if out else "not found")
        print("\nDocker is required. Install Docker Desktop (Windows) or docker.io (Linux).")
        return rep.summary()
    rep.add("preflight:docker", PASS, out.strip().splitlines()[0][:60])

    rc, out = run(["docker", "compose", "version"], timeout=30)
    if rc != 0:
        rep.add("preflight:compose", FAIL, "docker compose v2 not available")
        return rep.summary()
    rep.add("preflight:compose", PASS, out.strip().splitlines()[0][:60])

    if not compose_file.exists():
        rep.add("preflight:compose-file", FAIL, "missing %s" % compose_file)
        return rep.summary()
    rep.add("preflight:compose-file", PASS, str(compose_file.name))

    # ---- required secrets: generate missing ones into .env ----
    # docker-compose.yml uses ${NEO4J_PASSWORD:?} (hard fail) for the neo4j
    # service, and the setup docs don't call this out. Generate it so a
    # fresh checkout can boot without manual .env surgery.
    env_file = REPO_ROOT / ".env"
    try:
        env_text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    except OSError as e:
        rep.add("preflight:env", FAIL, "cannot read .env: %s" % e)
        return rep.summary()
    if "NEO4J_PASSWORD" not in env_text:
        generated = secrets.token_hex(24)
        try:
            with open(env_file, "a", encoding="utf-8") as f:
                if env_text and not env_text.endswith("\n"):
                    f.write("\n")
                f.write("# generated by scripts/smoke_test.py (fresh-install requirement)\n")
                f.write("NEO4J_PASSWORD=%s\n" % generated)
            rep.add("preflight:env", WARN, "NEO4J_PASSWORD was missing — generated one into .env")
        except OSError as e:
            rep.add("preflight:env", FAIL, "cannot write .env: %s" % e)
            return rep.summary()
    else:
        rep.add("preflight:env", PASS, ".env has NEO4J_PASSWORD")

    # ---- port conflicts: fail fast with a clear message ----
    # A common failure is another stack already holding a port (e.g. ntfy's
    # 8091 on a dev PC). Detect before `compose up` so the error is obvious.
    busy = []
    for p in (args.port, 8100, 8180, 8091):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            busy.append(p)
        finally:
            s.close()
    if busy:
        rep.add("preflight:ports", FAIL,
                "port(s) already in use: %s — stop the other stack or free them" %
                ", ".join(map(str, busy)))
        return rep.summary()
    rep.add("preflight:ports", PASS, "ports %s free" % "/".join(map(str, (args.port, 8100, 8180, 8091))))

    # ---- bring up ----
    up_cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
    if not args.no_build:
        up_cmd.append("--build")
    print("\nBringing up: %s" % " ".join(up_cmd), flush=True)
    rc, out = run(up_cmd, cwd=str(REPO_ROOT), timeout=1800)
    if rc != 0:
        rep.add("compose:up", FAIL, out.strip().splitlines()[-1] if out else "failed")
        print(out)
        return rep.summary()
    rep.add("compose:up", PASS, "containers started")

    # ---- wait for healthy ----
    print("\nWaiting for %s/api/health ..." % base, flush=True)
    healthy = False
    deadline = time.time() + args.wait_secs
    while time.time() < deadline:
        code, body = http_get(base + "/api/health", timeout=5)
        if code == 200:
            try:
                if json.loads(body).get("status") == "healthy":
                    healthy = True
                    break
            except Exception:
                pass
        time.sleep(5)
    if not healthy:
        rep.add("app:healthy", FAIL, "not healthy after %ds — check `docker compose logs odysseus`" % args.wait_secs)
        return rep.summary()
    rep.add("app:healthy", PASS, "/api/health reports healthy")

    # ---- endpoint checks ----
    code, body = http_get(base + "/api/version", timeout=10)
    if code == 200:
        try:
            ver = json.loads(body).get("version", "?")
            rep.add("api:version", PASS, "version=%s" % ver)
        except Exception:
            rep.add("api:version", FAIL, "unparseable body")
    else:
        rep.add("api:version", FAIL, "HTTP %s" % code)

    code, body = http_get(base + "/api/health/aggregate", timeout=30)
    if code == 200:
        try:
            data = json.loads(body)
            services = data.get("services", {})
            bad = ["%s=%s" % (k, v.get("status")) for k, v in services.items()
                   if v.get("status") != "ok"]
            for k, v in services.items():
                print("    service %-12s %-8s %6sms %s" % (
                    k, v.get("status"), v.get("latency_ms"), v.get("error") or ""))
            if bad:
                rep.add("api:aggregate", WARN, "degraded: %s" % ", ".join(bad))
            else:
                rep.add("api:aggregate", PASS, "all %d services ok" % len(services))
        except Exception as e:
            rep.add("api:aggregate", FAIL, "unparseable: %s" % e)
    else:
        rep.add("api:aggregate", FAIL, "HTTP %s" % code)

    code, body = http_get(base + "/", timeout=10)
    if code == 200 and "Aurix" in body:
        rep.add("ui:index", PASS, "200 + Aurix branding present")
    elif code == 200:
        rep.add("ui:index", WARN, "200 but no Aurix branding found")
    else:
        rep.add("ui:index", FAIL, "HTTP %s" % code)

    code, _ = http_get(base + "/login", timeout=10)
    rep.add("ui:login", PASS if code == 200 else FAIL, "HTTP %s" % code)

    # ---- compose ps ----
    rc, out = run(["docker", "compose", "-f", str(compose_file), "ps", "--format", "json"],
                  cwd=str(REPO_ROOT), timeout=30)
    if rc == 0 and out.strip():
        try:
            lines = [json.loads(l) for l in out.strip().splitlines() if l.strip().startswith("{")]
            if not lines and out.strip().startswith("["):
                lines = json.loads(out)
            names = [c.get("Service") or c.get("Name") for c in lines]
            states = [(c.get("Service"), c.get("State")) for c in lines]
            expected = ["odysseus", "chromadb", "searxng", "ntfy"]
            missing = [s for s in expected if s not in names]
            not_running = [s for s, st in states if st and st.lower() not in ("running", "healthy")]
            detail = "%d containers" % len(lines)
            if missing:
                rep.add("compose:services", WARN, "missing from ps: %s" % ",".join(missing))
            elif not_running:
                rep.add("compose:services", WARN, "not running: %s" % ",".join(not_running))
            else:
                rep.add("compose:services", PASS, detail)
        except Exception as e:
            rep.add("compose:services", WARN, "could not parse ps output: %s" % e)
    else:
        rep.add("compose:services", SKIP, "ps failed")

    # ---- container detail: surface crashed/restarting containers ----
    # e.g. a sandbox stuck in a restart loop (exit 1) is invisible to the
    # endpoint checks above; report exit codes + log tails so it's obvious.
    rc, out = run(["docker", "compose", "-f", str(compose_file), "ps", "-a", "--format", "json"],
                  cwd=str(REPO_ROOT), timeout=30)
    if rc == 0 and out.strip():
        try:
            lines = [json.loads(l) for l in out.strip().splitlines() if l.strip().startswith("{")]
            if not lines and out.strip().startswith("["):
                lines = json.loads(out)
            bad = [c for c in lines
                   if (c.get("State") or "").lower() not in ("running", "healthy", "")]
            for c in bad:
                svc = c.get("Service") or c.get("Name")
                lrc, logs = run(["docker", "compose", "-f", str(compose_file), "logs",
                                 "--tail", "15", svc], cwd=str(REPO_ROOT), timeout=30)
                tail = (logs.strip().splitlines()[-5:] if logs.strip() else ["<no logs>"])
                rep.add("container:%s" % svc, WARN,
                        "state=%s exit=%s | %s" % (
                            c.get("State"), c.get("ExitCode"), " / ".join(tail)[-220:]))
            if not bad:
                rep.add("compose:containers", PASS, "all containers running/healthy")
        except Exception as e:
            rep.add("compose:containers", SKIP, "could not inspect: %s" % e)

    # ---- direct integration probes (informational) ----
    code, _ = http_get("http://127.0.0.1:8100/api/v1/heartbeat", timeout=5)
    rep.add("probe:chromadb", PASS if code == 200 else SKIP,
            "heartbeat %s" % ("ok" if code == 200 else "unreachable (not exposed?)"))

    code, _ = http_get("http://127.0.0.1:8180/", timeout=8)
    rep.add("probe:searxng", PASS if code == 200 else SKIP,
            "HTTP %s" % (code if code else "unreachable (not exposed?)"))

    # ---- teardown (optional) ----
    if args.teardown:
        print("\nTearing down...", flush=True)
        rc, out = run(["docker", "compose", "-f", str(compose_file), "down"],
                      cwd=str(REPO_ROOT), timeout=300)
        rep.add("compose:down", PASS if rc == 0 else WARN, "")
    else:
        print("\nLeaving containers running. Tear down with:")
        print("  docker compose -f %s down" % compose_file)

    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())
