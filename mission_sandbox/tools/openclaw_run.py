#!/usr/bin/env python3
"""openclaw_run.py - run ONE task through OpenClaw's workboard, inside the sandbox.

    python3 openclaw_run.py --task-file /app/data/workspace/m-xxxxxx/.openclaw/task-s2.md [--timeout-seconds 3300]
    python3 openclaw_run.py --selftest

Talks to the model ONLY through the sandbox's LLM proxy (LLM_BASE_URL, LLM_MODEL from the
environment - same env vars openhands_run.py uses), using OpenClaw's NATIVE Ollama API rather
than the OpenAI-compatible one: OpenClaw's own docs warn that the `/v1` path breaks tool calling,
so this strips a trailing `/v1` from LLM_BASE_URL before writing OpenClaw's config. The Gateway
runs headless with zero messaging channels (`--allow-unconfigured --ambient-channels`) and is
started fresh for each call - there is no long-lived daemon here, matching OpenHands' one-shot
Conversation-per-call shape rather than OpenClaw's normal always-on personal-assistant mode.

The last line printed is always:

    OPENCLAW_RESULT:{"status": "...", "summary": "...", "card_id": "..."}

status: done | blocked | timeout | gateway_unreachable | not_installed | error

OpenClaw is an OPTIONAL layer of the sandbox image (see the Dockerfile's OpenClaw block). Every
OpenClaw CLI call below is defensive and failures are reported as a structured result instead of
a traceback, same discipline as openhands_run.py. `--selftest` shows what this image actually has
without touching the network.

HONEST STATUS (2026-09-30, live-tested against a real rebuilt sandbox, matching and exceeding the
rigor that found OpenHands' two real bugs): four real, undocumented integration bugs were found
and fixed by actually running this against a live Gateway - `--bind loopback` (a container auto-
binds 0.0.0.0 and then refuses to start without an auth token), the `workboard` CLI being a plugin
disabled by default, `workboard show`'s response nesting the card under a `"card"` key while
`create`'s does not, and a freshly created card sitting at status "todo" (dispatch silently skips
anything not "ready"). A FIFTH issue remains OPEN and unresolved: `workboard dispatch` refuses
the card with "target tool policy blocks required tool workboard_heartbeat" regardless of the
`agents.defaults.sandbox.workspaceAccess` value tried (`none` and `rw` both fail identically) -
this is an internal OpenClaw tool-policy interaction with no documented fix found after a real
search. Rather than guess further at an internals-level config with no source to verify against,
this is left OPEN - exactly the same call as OpenHands' still-unpatched `cache_creation_tokens`
bug: a real, reported gap, not a hidden one. `executor="openclaw"` will emit `status: "error"`
with this exact message until someone (a future session, or OpenClaw's own maintainers) finds the
real fix; the mission-runner fallback to the built-in agent means this blocks nothing in the
meantime.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

RESULT_MARK = "OPENCLAW_RESULT:"
GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
GATEWAY_READY_TIMEOUT = 60
POLL_INTERVAL_SECONDS = 5
TERMINAL_STATUSES = {"done", "blocked", "review"}


def emit(status, summary="", card_id=None):
    print(RESULT_MARK + json.dumps({"status": status, "summary": " ".join(str(summary).split())[:1200],
                                    "card_id": card_id}), flush=True)
    return 0 if status == "done" else 3


def openclaw_available():
    return shutil.which("openclaw") is not None


def native_ollama_base_url(llm_base_url):
    """OpenClaw wants the native Ollama API - no `/v1` - or tool-calling breaks per OpenClaw's own docs."""
    return (llm_base_url or "").rstrip("/").removesuffix("/v1")


def write_config(config_path, base_url, model):
    """The Ollama provider entry OpenClaw's docs recipe for a custom base URL, pointed at the
    sandbox's own LLM proxy (already allowlists the exact native-API paths this needs:
    POST /api/chat, GET /api/tags - see mission_sandbox/llm_proxy.py)."""
    config = {
        "gateway": {"mode": "local"},
        "models": {"providers": {"ollama": {
            "baseUrl": base_url, "apiKey": "ollama-local", "api": "ollama", "timeoutSeconds": 300,
            "models": [{"id": model, "name": model, "input": ["text"], "contextTokens": 32768,
                       "params": {"num_ctx": 32768}}],
        }}},
        # OpenClaw's own security docs recommend this exact shape for the agent's INTERNAL tool
        # sandboxing (separate from the outer container, which is AURIX's own real isolation
        # boundary). Also found live 2026-09-30 to be load-bearing for a different reason: the
        # workboard refuses to dispatch a card to an agent that isn't marked sandboxed at all
        # ("target agent is not sandboxed for this restricted Workboard card"), so this is required
        # for workboard cards to run, not just defense in depth.
        "agents": {"defaults": {"model": {"primary": f"ollama/{model}"},
                                "sandbox": {"mode": "non-main", "scope": "session", "workspaceAccess": "none"}}},
        # The `workboard` CLI surface is a bundled plugin, disabled by default (found live
        # 2026-09-30: `openclaw workboard create` refused with "disabled by default. Run `openclaw
        # plugins enable workboard`"). Writing it straight into config has the same effect as that
        # command (confirmed by inspecting the config it writes) without a second CLI round-trip.
        "plugins": {"entries": {"workboard": {"enabled": True}}},
    }
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)


def run_cli(*args, home, timeout=30):
    """openclaw <args> --json, parsed. Never raises - a CLI/parse failure is just an empty dict."""
    try:
        p = subprocess.run(["openclaw", *args, "--json"], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "HOME": home})
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}


def start_gateway(home):
    """Headless, zero messaging channels (--allow-unconfigured --ambient-channels per OpenClaw's
    own CLI docs). Backgrounded: this script owns its lifetime and never blocks waiting for it to
    exit, since the Gateway itself does not exit until the process is killed. `HOME` is overridden
    to the sandbox's writable home (found live 2026-09-25: the real $HOME, /home/sbx, is read-only
    in this image, and OpenClaw reads its config from $HOME/.openclaw - so every openclaw subprocess
    needs the same override write_config() used, not just this one). `--bind loopback` is required
    too (found live 2026-09-30): OpenClaw detects it is running in a container and defaults to
    binding 0.0.0.0 for port-forwarding convenience, then REFUSES to start that way without an auth
    token configured - a real security default, correctly triggered, that this single-container/
    single-caller setup does not actually need. Binding loopback explicitly (this script is the
    only caller, over 127.0.0.1) sidesteps the auth requirement instead of provisioning a token for
    an endpoint nothing outside this process ever needs to reach."""
    return subprocess.Popen(
        ["openclaw", "gateway", "--allow-unconfigured", "--ambient-channels", "--bind", "loopback",
         "--port", str(GATEWAY_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "HOME": home})


def gateway_ready(deadline):
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1)
    return False


def run(task_file, timeout_seconds):
    if not openclaw_available():
        return emit("not_installed", "the `openclaw` binary is not on PATH in this sandbox image")
    base_url = native_ollama_base_url(os.environ.get("LLM_BASE_URL", ""))
    if not base_url:
        return emit("gateway_unreachable", "LLM_BASE_URL is not set")
    model = os.environ.get("LLM_MODEL", "qwen2.5:7b")
    # $HOME (/home/sbx) is read-only in this image - found live 2026-09-25. SANDBOX_HOME (/tmp/home,
    # the Dockerfile's own writable-home env var) is where OpenClaw's config AND every openclaw
    # subprocess's own $HOME must point instead, or it tries to write its config next to a read-only
    # home and OpenClaw itself would look for that config in the wrong place too.
    home = os.environ.get("SANDBOX_HOME", "/tmp/home")
    write_config(os.path.join(home, ".openclaw", "openclaw.json"), base_url, model)

    gw = start_gateway(home)
    try:
        if not gateway_ready(time.time() + GATEWAY_READY_TIMEOUT):
            return emit("gateway_unreachable", "the OpenClaw Gateway did not answer /health in time")
        task = open(task_file, encoding="utf-8").read()
        created = run_cli("workboard", "create", task, "--priority", "high", home=home)
        card = created.get("card", created)
        card_id = card.get("id")
        if not card_id:
            return emit("error", f"workboard create returned no card id: {created}")
        # A freshly created card starts at "todo", not "ready" (found live 2026-09-30: dispatch
        # silently skipped it - count=0 - until moved). The real status progression is triage ->
        # backlog -> todo -> scheduled -> ready -> running -> review -> blocked -> done.
        run_cli("workboard", "move", card_id, "--status", "ready", home=home)
        run_cli("workboard", "dispatch", "--max-starts", "1", home=home)

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            shown = run_cli("workboard", "show", card_id, home=home)
            card = shown.get("card", shown)                      # `show` nests the card, `create` does not
            status = str(card.get("status", "")).lower()
            if status in TERMINAL_STATUSES:
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            return emit("timeout", f"card {card_id} never reached a terminal status", card_id)

        status = str(card.get("status", "")).lower()
        summary = card.get("proof") or card.get("summary") or card.get("result") or f"card ended as {status}"
        return emit("done" if status == "done" else "blocked", summary, card_id)
    except Exception as e:                                    # CLI/protocol drift or a model failure: report, never crash
        return emit("error", f"{type(e).__name__}: {e}")
    finally:
        gw.terminate()


def selftest():
    assert native_ollama_base_url("http://llm-gateway:11434/v1") == "http://llm-gateway:11434"
    assert native_ollama_base_url("http://llm-gateway:11434") == "http://llm-gateway:11434"
    if not openclaw_available():
        print("openclaw binary NOT available in this image")
        print("(it is an optional layer: mission_sandbox/Dockerfile's OpenClaw block; rebuild the sandbox image)")
        return 4
    base = native_ollama_base_url(os.environ.get("LLM_BASE_URL", ""))
    print(f"openclaw binary present. LLM_BASE_URL(native)={base or '(unset)'}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-file")
    ap.add_argument("--timeout-seconds", type=int, default=3300)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.task_file:
        ap.error("--task-file is required")
    return run(a.task_file, a.timeout_seconds)


if __name__ == "__main__":
    sys.exit(main())
