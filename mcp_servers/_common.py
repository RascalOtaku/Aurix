"""
_common.py

Shared constants and helpers for built-in MCP servers.
"""

MAX_OUTPUT_CHARS = 10_000
MAX_READ_CHARS = 20_000
SHELL_TIMEOUT = 60
PYTHON_TIMEOUT = 30
SEARCH_TIMEOUT = 30


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate text to *limit* characters with a suffix note."""
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    return text


def mcp_server(name: str):
    """An MCP Server that keeps the `@server.list_tools()` / `@server.call_tool()` decorator style on both SDKs.

    mcp 1.x: the SDK's own Server (unchanged behaviour). mcp 2.x removed those decorators in favour of
    `Server(name, on_list_tools=..., on_call_tool=...)` handlers that receive (ctx, params) and return result
    objects; the wrapper below registers the same functions that way, so the servers run on either version.
    """
    from mcp.server import Server

    srv = Server(name)
    if hasattr(srv, "list_tools") and hasattr(srv, "call_tool"):
        return srv
    return _V2Server(name)


class _V2Server:
    def __init__(self, name: str):
        self.name = name
        self._list = None
        self._call = None
        self._server = None

    def list_tools(self):
        def register(fn):
            self._list = fn
            return fn
        return register

    def call_tool(self):
        def register(fn):
            self._call = fn
            return fn
        return register

    def _build(self):
        if self._server is None:
            from mcp.server import Server
            import mcp.types as types

            async def on_list_tools(ctx, params):
                return types.ListToolsResult(tools=list(await self._list()) if self._list else [])

            async def on_call_tool(ctx, params):
                try:                            # 1.x turned handler exceptions into error results; keep that
                    content = await self._call(params.name, dict(params.arguments or {}))
                    return types.CallToolResult(content=list(content))
                except Exception as e:
                    return types.CallToolResult(content=[types.TextContent(type="text", text=f"Error: {e}")],
                                                is_error=True)

            self._server = Server(self.name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        return self._server

    def create_initialization_options(self, *args, **kwargs):
        return self._build().create_initialization_options(*args, **kwargs)

    async def run(self, *args, **kwargs):
        return await self._build().run(*args, **kwargs)
