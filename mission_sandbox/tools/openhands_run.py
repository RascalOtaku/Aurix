#!/usr/bin/env python3
"""openhands_run.py - run ONE coding task with the OpenHands SDK, inside the sandbox.

    python3 openhands_run.py --task-file /app/data/workspace/m-xxxxxx/.openhands/task-s2.md [--max-iters 40]
    python3 openhands_run.py --selftest

Talks to the model ONLY through the sandbox's LLM proxy (LLM_BASE_URL, LLM_MODEL from the
environment). The agent gets a terminal and a file editor, working directory = the mission
workspace. The last line printed is always:

    OPENHANDS_RESULT:{"status": "...", "summary": "...", "iterations": N}

status: finished | error | stuck | max_iterations | sdk_unavailable | llm_unreachable

The SDK is an OPTIONAL layer of the sandbox image (requirements-openhands.txt). Its API has
moved between releases, so every SDK touch below is defensive and failures are reported as a
structured result instead of a traceback. `--selftest` shows what this image actually has.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

RESULT_MARK = "OPENHANDS_RESULT:"


def emit(status, summary="", iterations=None):
    print(RESULT_MARK + json.dumps({"status": status, "summary": " ".join(str(summary).split())[:1200],
                                    "iterations": iterations}), flush=True)
    return 0 if status == "finished" else 3


def normalize_model(model):
    """LiteLLM routes on a provider prefix; the proxy speaks the OpenAI-compatible API."""
    model = (model or "qwen2.5:7b").strip()
    return model if "/" in model else "openai/" + model


def map_status(raw):
    """SDK execution-status text (enum repr, str, or None) -> our status vocabulary."""
    s = str(raw or "").lower()
    if "finish" in s or s.endswith("idle") or "complete" in s:
        return "finished"
    if "stuck" in s:
        return "stuck"
    if "error" in s or "fail" in s:
        return "error"
    if "max" in s and "iter" in s:
        return "max_iterations"
    return "error"


def llm_reachable(base_url, timeout=8):
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_sdk():
    """(LLM, Agent, Conversation, Tool, TerminalTool, FileEditorTool) or raise ImportError."""
    from openhands.sdk import LLM, Agent, Conversation, Tool
    try:
        from openhands.tools.terminal import TerminalTool
    except ImportError:
        from openhands.tools.execute_bash import BashTool as TerminalTool
    from openhands.tools.file_editor import FileEditorTool
    return LLM, Agent, Conversation, Tool, TerminalTool, FileEditorTool


def last_agent_text(conversation):
    """Best-effort: the agent's final message text, across SDK versions."""
    try:
        events = list(getattr(getattr(conversation, "state", None), "events", []) or [])
    except Exception:
        events = []
    for ev in reversed(events):
        try:
            if getattr(ev, "source", "") == "agent":
                msg = getattr(ev, "llm_message", None)
                content = getattr(msg, "content", None) or []
                text = " ".join(getattr(c, "text", "") for c in content if getattr(c, "text", ""))
                if text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


def run(task_file, max_iters):
    try:
        LLM, Agent, Conversation, Tool, TerminalTool, FileEditorTool = load_sdk()
    except ImportError as e:
        return emit("sdk_unavailable", f"{e}")
    base_url = os.environ.get("LLM_BASE_URL", "")
    if not base_url or not llm_reachable(base_url):
        return emit("llm_unreachable", f"cannot reach {base_url or '(LLM_BASE_URL unset)'}")
    task = open(task_file, encoding="utf-8").read()
    workspace = os.environ.get("MISSION_WORKSPACE") or os.getcwd()
    try:
        from pydantic import SecretStr
        # reasoning_effort defaults to "high": LiteLLM then asks the backend for extended-thinking output,
        # which a small local model served over Ollama's OpenAI-compat endpoint rejects outright (found live
        # 2026-09-24: "qwen2.5:7b does not support thinking", every run failing before it could do anything).
        # "none" is a real, provider-neutral value the SDK accepts (see LLM.model_fields["reasoning_effort"]).
        llm = LLM(model=normalize_model(os.environ.get("LLM_MODEL")), base_url=base_url,
                  api_key=SecretStr(os.environ.get("LLM_API_KEY", "ollama")), reasoning_effort="none")
        agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)])
        try:
            conversation = Conversation(agent=agent, workspace=workspace, max_iteration_per_run=max_iters)
        except TypeError:                                   # older/newer signature without the cap
            conversation = Conversation(agent=agent, workspace=workspace)
        conversation.send_message(task)
        conversation.run()
    except Exception as e:                                  # SDK/API drift or a model failure: report, never crash
        return emit("error", f"{type(e).__name__}: {e}")
    state = getattr(conversation, "state", None)
    status = map_status(getattr(state, "execution_status", None))
    iterations = getattr(state, "iteration", None)
    return emit(status, last_agent_text(conversation) or f"ended with status {status}", iterations)


def selftest():
    assert normalize_model("qwen2.5:7b") == "openai/qwen2.5:7b"
    assert normalize_model("anthropic/claude-x") == "anthropic/claude-x"
    assert map_status("ConversationExecutionStatus.FINISHED") == "finished"
    assert map_status("stuck") == "stuck" and map_status(None) == "error"
    try:
        load_sdk()
    except ImportError as e:
        print(f"openhands SDK NOT available in this image: {e}")
        print("(it is an optional layer: mission_sandbox/requirements-openhands.txt; rebuild the sandbox image)")
        return 4
    base = os.environ.get("LLM_BASE_URL", "")
    print(f"openhands SDK importable. LLM_BASE_URL={base or '(unset)'} reachable={llm_reachable(base) if base else False}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-file")
    ap.add_argument("--max-iters", type=int, default=40)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.task_file:
        ap.error("--task-file is required")
    return run(a.task_file, a.max_iters)


if __name__ == "__main__":
    sys.exit(main())
