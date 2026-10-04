"""End-to-end MCP: Aurix's own McpManager starts a real built-in server over stdio, lists its tools, calls one.

Unit tests never start an MCP server, so a breaking SDK upgrade (mcp 2.x removed the Server.list_tools()
decorator every file in mcp_servers/ uses) passed CI while every built-in server would crash on start.
This is the test that catches that.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False


@unittest.skipUnless(HAVE_MCP, "mcp SDK not installed")
class BuiltinMcpServerTests(unittest.TestCase):
    def test_rag_server_starts_lists_tools_and_answers(self):
        from src.mcp_manager import McpManager

        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"
            wiki.mkdir()
            (wiki / "ops.md").write_text("# Ops\n## Deploy\nUse compose.\n")

            async def run():
                m = McpManager()
                m.set_disabled_map({})               # no tools disabled (the settings DB is not part of this test)
                env = {"PYTHONPATH": str(ROOT), "AURIX_PAGE_INDEX_DIRS": str(wiki)}
                ok = await m.connect_server("rag", "rag", "stdio", command=sys.executable,
                                            args=[str(ROOT / "mcp_servers" / "rag_server.py")], env=env)
                try:
                    tools = sorted(t["name"] for t in m.get_all_tools())
                    result = await m.call_tool("mcp__rag__wiki_outline", {"query": "deploy"}) if ok else {}
                finally:
                    await m.disconnect_all()
                return ok, tools, result

            ok, tools, result = asyncio.run(asyncio.wait_for(run(), timeout=60))
        self.assertTrue(ok, "the built-in rag MCP server did not start (MCP SDK incompatibility?)")
        self.assertEqual(tools, ["manage_rag", "wiki_outline", "wiki_read"])
        self.assertIn("[wiki/ops.md#2] Deploy", result.get("stdout", ""))

    def test_every_builtin_server_module_imports(self):
        """Import-time API use (decorators, transports) is exactly what an SDK major bump breaks."""
        import importlib.util
        scripts = [p for p in sorted((ROOT / "mcp_servers").glob("*_server.py")) if "from mcp" in p.read_text()]
        self.assertGreaterEqual(len(scripts), 4)
        for script in scripts:                   # (aurix_server.py speaks JSON-RPC itself, without the SDK)
            with self.subTest(server=script.name):
                spec = importlib.util.spec_from_file_location(f"_mcp_{script.stem}", script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(hasattr(module, "server"))

    def test_sse_bridge_serves_tools_over_sse(self):
        """mcp_servers/aurix_sse_bridge.py: the one server on the SSE transport, started for real and called."""
        import socket
        import subprocess
        import time
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(os.environ, AURIX_SSE_PORT=str(port), AURIX_URL="http://127.0.0.1:9")    # nothing listens on :9
        proc = subprocess.Popen([sys.executable, str(ROOT / "mcp_servers" / "aurix_sse_bridge.py")], env=env)

        async def run():
            for _ in range(100):                                     # wait for uvicorn to accept connections
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            async with sse_client(f"http://127.0.0.1:{port}/sse") as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = sorted(t.name for t in (await session.list_tools()).tools)
                    result = await session.call_tool("aurix_status", {})
                    return tools, result.content[0].text
        try:
            tools, text = asyncio.run(asyncio.wait_for(run(), timeout=60))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        self.assertEqual(tools, ["aurix_chat", "aurix_remember", "aurix_search", "aurix_shell", "aurix_status"])
        self.assertIn('"error"', text)                               # the AURIX API is down: reported, not a crash


if __name__ == "__main__":
    unittest.main()
