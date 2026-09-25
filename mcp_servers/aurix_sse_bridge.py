#!/usr/bin/env python3
"""aurix_sse_bridge.py — AURIX MCP SSE server (mcp 1.27.x)"""
import json, os, urllib.request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

AURIX_URL = os.environ.get("AURIX_URL", "http://localhost:7777")

def _call(path, method="GET", body=None):
    url = f"{AURIX_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

server = Server("aurix-brain")

@server.list_tools()
async def list_tools():
    return [
        Tool(name="aurix_status",
             description="Real-time AURIX system status: memory count, model, wiki pages, ollama",
             inputSchema={"type":"object","properties":{},"required":[]}),
        Tool(name="aurix_chat",
             description="Send message to AURIX brain with full memory context",
             inputSchema={"type":"object","properties":{"message":{"type":"string"}},"required":["message"]}),
        Tool(name="aurix_remember",
             description="Save a fact to AURIX memory",
             inputSchema={"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}),
        Tool(name="aurix_search",
             description="Search AURIX memory and wiki knowledge base",
             inputSchema={"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}),
        Tool(name="aurix_shell",
             description="Run shell command on the GPU workstation",
             inputSchema={"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "aurix_status":
        result = _call("/status")
    elif name == "aurix_chat":
        result = _call("/chat", "POST", {"message": arguments.get("message","")})
    elif name == "aurix_remember":
        result = _call("/chat", "POST", {"message": f"remember: {arguments.get('text','')}"})
    elif name == "aurix_search":
        result = _call("/tool", "POST", {"tool":"search_memory","args":{"query":arguments.get("query","")}})
    elif name == "aurix_shell":
        result = _call("/tool", "POST", {"tool":"shell","args":{"cmd":arguments.get("command","")}})
    else:
        result = {"error": f"Unknown: {name}"}
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

sse = SseServerTransport("/messages/")

async def handle_sse(request: Request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0], streams[1],
            server.create_initialization_options()
        )
    return Response()

# Mount handle_post_message as raw ASGI app — it returns None (writes directly)
app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages", app=sse.handle_post_message),
])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7778, log_level="warning")
