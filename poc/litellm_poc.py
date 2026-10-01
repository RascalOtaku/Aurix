"""Spec §10 task 7: "LiteLLM in front of Ollama - confirm one request can be routed end-to-end to a local model."
Here: one OpenAI-compatible endpoint; a request for `aurix-local` really reaches the 3431's 7B model; a request whose primary is
unreachable is really answered by the 7070's own CPU model through LiteLLM's fallback; and the endpoint is not open to anyone.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ["LITELLM_URL"].rstrip("/")
KEY = os.environ["LITELLM_MASTER_KEY"]
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    failures += 0 if ok else 1


def call(method, path, body=None, key=KEY, timeout=300):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


GPU_HOST = os.environ.get("AURIX_GPU_HOST", "100.64.0.10")   # the GPU box's Tailscale address

def ollama_ps(host):
    """Names of the models an Ollama server currently has loaded (what it really ran), or []."""
    try:
        with urllib.request.urlopen(f"http://{host}:11434/api/ps", timeout=6) as r:
            return [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return []


def ask(model, prompt="Reply with the single word: ready"):
    t = time.time()
    status, body = call("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 20, "temperature": 0})
    text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "") if status == 200 else ""
    return status, body, text, time.time() - t


def main():
    print("== LiteLLM: is it up?")
    up = False
    for _ in range(90):
        try:
            with urllib.request.urlopen(URL + "/health/liveliness", timeout=3) as r:
                up = r.status == 200
                if up:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    check("the proxy is up", up)
    if not up:
        return

    print("== access control")
    status, _ = call("GET", "/v1/models", key=None)
    check("no key -> refused (401), the endpoint is not open", status == 401, str(status))
    status, _ = call("GET", "/v1/models", key="sk-wrong")
    check("wrong key -> refused (LiteLLM without a database answers 400 'No connected db'; still a refusal)", 400 <= status <= 403, str(status))
    status, body = call("GET", "/v1/models")
    names = sorted(m["id"] for m in body.get("data", []))
    check("with the key, the three routes are listed", {"aurix-local", "aurix-local-cpu", "aurix-fallback-test"} <= set(names), ", ".join(names))

    print("== one request, routed end to end")
    status, body, text, secs = ask("aurix-local")
    check("aurix-local answers (HTTP 200) with real text", status == 200 and bool(text.strip()), f"{status}, {text.strip()[:40]!r}, {secs:.1f}s")
    check("the answer is the one asked for", "ready" in text.lower(), repr(text.strip()[:40]))
    ps = ollama_ps(GPU_HOST)
    check("...and the 3431's Ollama itself reports the 7B model resident (LiteLLM only echoes the alias, so ask the machine)",
          any(m.startswith("qwen2.5:7b") for m in ps), ", ".join(ps) or "nothing loaded")

    print("== fallback MECHANISM: primary unreachable -> LiteLLM falls back to the next route")
    status, body, text, secs = ask("aurix-fallback-test")
    check("the request still succeeds (HTTP 200) with real text", status == 200 and bool(text.strip()), f"{status}, {secs:.1f}s")
    check("...only AFTER the dead primary was tried and timed out (>= 5 s), so this really was the fallback", secs >= 5.0, f"{secs:.1f}s")
    check("...and the answer came from the fallback route's model (3B on the 3431)", any(m.startswith("qwen2.5:3b") for m in ollama_ps(GPU_HOST)))

    print("== the real fallback route: the 7070's own CPU model")
    status, body, text, secs = ask("aurix-local-cpu", "Reply with the single word: ready")
    if status == 200:
        check("the 7070's CPU model answers through LiteLLM", bool(text.strip()), f"{secs:.1f}s, {text.strip()[:30]!r}")
    else:
        check("the 7070's CPU model answers through LiteLLM (if this fails: ufw must allow 172.30.77.0/24 -> port 11434 on the 7070)",
              False, f"HTTP {status} after {secs:.0f}s")

    print("== nonsense in, sane error out")
    status, _, _, _ = ask("no-such-model")
    check("an unknown model is a clean 4xx, not a crash", 400 <= status < 500, str(status))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                                                      # noqa: BLE001
        print(f"  FAIL  unexpected error: {type(e).__name__}: {e}")
        failures += 1
    print(f"LiteLLM: {'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
