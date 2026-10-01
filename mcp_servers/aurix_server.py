#!/usr/bin/env python3
"""aurix_server.py — MCP server exposing AURIX brain + all agents to Odysseus"""
import json, urllib.request, urllib.error, os, subprocess, sys
from pathlib import Path

AURIX_URL = os.environ.get("AURIX_URL", "http://localhost:7777")
BRAIN = Path(os.environ.get("AURIX_BRAIN", "/home/rascal/ai/brain"))
VENV = BRAIN / "venv" / "bin" / "python3"

def _call(path, method="GET", body=None):
    url = f"{AURIX_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type":"application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": str(e), "code": e.code}
    except Exception as e:
        return {"error": str(e)}

def _run(script, *args, timeout=600):
    cmd = [str(VENV), str(BRAIN / "tasks" / script)] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=str(BRAIN))
        return {"output": r.stdout[-3000:], "error": r.stderr[-500:] if r.returncode else ""}
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}

TOOLS = [
    # ── Core AURIX ────────────────────────────────────────────────────────────
    {
        "name": "aurix_status",
        "description": "Get real-time AURIX system status: memory count, model, wiki pages, ollama state, all service health",
        "inputSchema": {"type":"object","properties":{},"required":[]}
    },
    {
        "name": "aurix_chat",
        "description": "Send a message to the AURIX brain and get a response with full memory context",
        "inputSchema": {"type":"object","properties":{
            "message":{"type":"string","description":"Message to send to AURIX brain"}
        },"required":["message"]}
    },
    {
        "name": "aurix_remember",
        "description": "Save a fact or note to AURIX memory",
        "inputSchema": {"type":"object","properties":{
            "text":{"type":"string","description":"Text to remember"},
            "tier":{"type":"string","description":"Memory tier: hot, auto, longterm","default":"auto"}
        },"required":["text"]}
    },
    {
        "name": "aurix_search_memory",
        "description": "Search AURIX memory and wiki for relevant information",
        "inputSchema": {"type":"object","properties":{
            "query":{"type":"string","description":"Search query"}
        },"required":["query"]}
    },
    {
        "name": "aurix_shell",
        "description": "Run a shell command on the AURIX Precision Tower 3431",
        "inputSchema": {"type":"object","properties":{
            "command":{"type":"string","description":"Shell command to run"}
        },"required":["command"]}
    },
    # ── Heal loop ─────────────────────────────────────────────────────────────
    {
        "name": "aurix_heal",
        "description": "Run the AURIX self-healing loop. Checks all services, DNS, ChromaDB, browser agent, disk mounts and auto-fixes issues. Use --dry-run to audit without fixing.",
        "inputSchema": {"type":"object","properties":{
            "dry_run":{"type":"boolean","description":"If true, audit only — do not fix","default":False}
        },"required":[]}
    },
    # ── Deep research ─────────────────────────────────────────────────────────
    {
        "name": "aurix_research",
        "description": "Run deep web research on any topic using the IterResearch pipeline (Plan→Search→Extract→Synthesize). Saves results to wiki.",
        "inputSchema": {"type":"object","properties":{
            "question":{"type":"string","description":"Research question or topic"},
            "depth":{"type":"integer","description":"Research depth: 1=fast, 2=standard, 3=thorough","default":2},
            "save":{"type":"boolean","description":"Save result to wiki","default":True}
        },"required":["question"]}
    },
    # ── Reynolds Gang ─────────────────────────────────────────────────────────
    {
        "name": "aurix_reynolds",
        "description": "Query or update the Reynolds Gang treasure research knowledge base. Ask about burial locations, historical figures, events, or run a new research pass.",
        "inputSchema": {"type":"object","properties":{
            "action":{"type":"string","description":"status, query, or run","enum":["status","query","run"]},
            "question":{"type":"string","description":"Question to ask the knowledge base (for action=query)"}
        },"required":["action"]}
    },
    # ── Media agent ───────────────────────────────────────────────────────────
    {
        "name": "aurix_media",
        "description": "Download movies, TV shows, music, or anime via the AURIX media pipeline. Searches torrents then falls back to yt-dlp. Scans Jellyfin after download.",
        "inputSchema": {"type":"object","properties":{
            "request":{"type":"string","description":"What to download, e.g. 'download Blade Runner 1982' or 'download Led Zeppelin IV'"}
        },"required":["request"]}
    },
    # ── Trade agent ───────────────────────────────────────────────────────────
    {
        "name": "aurix_portfolio",
        "description": "Get current AURIX paper trading portfolio status, positions, P&L, and significant moves",
        "inputSchema": {"type":"object","properties":{},"required":[]}
    },
    # ── Browser agent ─────────────────────────────────────────────────────────
    {
        "name": "aurix_browse",
        "description": "Use the AURIX headless Chromium browser to navigate websites, extract information, fill forms, or find torrents. Slower but can access any webpage.",
        "inputSchema": {"type":"object","properties":{
            "task":{"type":"string","description":"Browser task description, e.g. 'go to example.com and return the page title'"},
            "steps":{"type":"integer","description":"Max browser steps","default":10}
        },"required":["task"]}
    },
]

def handle_tool(name, args):
    if name == "aurix_status":
        result = _call("/status")
        # Enrich with heal_loop quick check
        try:
            import subprocess as sp
            r = sp.run([str(VENV), str(BRAIN/"tasks"/"heal_loop.py"), "--dry-run"],
                      capture_output=True, text=True, timeout=60, cwd=str(BRAIN))
            result["heal_summary"] = r.stdout[-1000:]
        except: pass
        return result

    elif name == "aurix_chat":
        return _call("/chat", "POST", {"message": args["message"]})

    elif name == "aurix_remember":
        msg = f"remember: {args['text']}"
        return _call("/chat", "POST", {"message": msg})

    elif name == "aurix_search_memory":
        return _call("/tool", "POST", {"tool":"search_memory","args":{"query":args["query"]}})

    elif name == "aurix_shell":
        return _call("/tool", "POST", {"tool":"shell","args":{"cmd":args["command"]}})

    elif name == "aurix_heal":
        dry = args.get("dry_run", False)
        extra = ["--dry-run"] if dry else []
        return _run("heal_loop.py", *extra, timeout=300)

    elif name == "aurix_research":
        q = args["question"]
        depth = str(args.get("depth", 2))
        extra = ["--save"] if args.get("save", True) else []
        return _run("deep_research_agent.py", q, "--depth", depth, *extra, timeout=600)

    elif name == "aurix_reynolds":
        action = args.get("action", "status")
        if action == "status":
            return _run("reynolds_agent.py", "--status")
        elif action == "query":
            q = args.get("question", "what are the burial clues")
            return _run("reynolds_agent.py", "--query", q)
        elif action == "run":
            return _run("reynolds_agent.py", timeout=1800)

    elif name == "aurix_media":
        req = args["request"]
        return _run("media_agent.py", req, timeout=120)

    elif name == "aurix_portfolio":
        return _call("/tool", "POST", {"tool":"portfolio","args":{}})

    elif name == "aurix_browse":
        task = args["task"]
        steps = str(args.get("steps", 10))
        return _run("browser_agent.py", "--task", task, "--steps", steps, timeout=300)

    return {"error": f"Unknown tool: {name}"}

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: req = json.loads(line)
        except json.JSONDecodeError: continue
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            resp = {"jsonrpc":"2.0","id":req_id,"result":{
                "protocolVersion":"2024-11-05",
                "capabilities":{"tools":{}},
                "serverInfo":{"name":"aurix","version":"2.0"}
            }}
        elif method == "tools/list":
            resp = {"jsonrpc":"2.0","id":req_id,"result":{"tools":TOOLS}}
        elif method == "tools/call":
            tool_name = req["params"]["name"]
            tool_args = req["params"].get("arguments", {})
            result = handle_tool(tool_name, tool_args)
            resp = {"jsonrpc":"2.0","id":req_id,"result":{
                "content":[{"type":"text","text":json.dumps(result, indent=2)}]
            }}
        elif method == "notifications/initialized":
            continue
        else:
            resp = {"jsonrpc":"2.0","id":req_id,"error":{"code":-32601,"message":"Method not found"}}
        print(json.dumps(resp), flush=True)

if __name__ == "__main__":
    main()
