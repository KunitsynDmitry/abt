"""MCP client — persistent stdio connections to MCP servers.

Each McpConnection spawns a subprocess (the MCP server), keeps the session
alive in a background thread with its own asyncio event loop, and exposes
synchronous call_tool() for use from LangGraph nodes.
"""

import asyncio
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client

from ..models.source import SourceDefinition


class McpConnection:
    """Persistent connection to one MCP server via stdio.

    Spawns the server subprocess, initializes an MCP session, discovers
    tools, and keeps the connection alive in a daemon thread.
    """

    def __init__(self, source: SourceDefinition):
        self._source = source
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._tools: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._started = False

        self._transport_ctx = None
        self._session_ctx = None

    def _ensure_started(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"mcp-{self._source.name}",
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError(
                f"MCP server '{self._source.name}' did not start within 30s"
            )
        if self._error:
            raise self._error

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_and_serve())
        except Exception as e:
            self._error = e
            self._ready.set()

    async def _connect_and_serve(self):
        params = StdioServerParameters(
            command=self._source.config.get("command", ""),
            args=self._source.config.get("args", []),
            env=self._source.config.get("env"),
        )

        self._transport_ctx = stdio_client(params)
        read, write = await self._transport_ctx.__aenter__()

        self._session = ClientSession(read, write)
        await self._session.__aenter__()

        await self._session.initialize()
        result = await self._session.list_tools()
        self._tools = {t.name: t for t in result.tools}

        self._ready.set()

        # Block forever — keep the connection alive
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool synchronously. Blocks until result arrives."""
        self._ensure_started()

        async def _call():
            return await self._session.call_tool(name, arguments)

        future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        try:
            result = future.result(timeout=60)
        except Exception as e:
            raise RuntimeError(
                f"MCP tool '{name}' on '{self._source.name}' failed: {e}"
            ) from e

        if result.isError:
            error_text = _extract_text(result.content)
            raise RuntimeError(
                f"MCP tool '{name}' returned error: {error_text}"
            )

        if result.structuredContent is not None:
            return result.structuredContent

        return {"content": _extract_text(result.content)}

    @property
    def tools(self) -> dict[str, Any]:
        """Discovered MCP tools (name -> Tool metadata)."""
        self._ensure_started()
        return dict(self._tools)

    def close(self):
        """Shut down the MCP session and subprocess."""
        if self._loop is None:
            return

        async def _shutdown():
            if self._session is not None:
                try:
                    await self._session.__aexit__(None, None, None)
                except Exception:
                    pass
                self._session = None
            if self._transport_ctx is not None:
                try:
                    await self._transport_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                self._transport_ctx = None

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            future.result(timeout=5)
        except Exception:
            pass

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


class McpManager:
    """Pool of McpConnection instances — one per MCP source definition."""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}

    def get_connection(self, source: SourceDefinition) -> McpConnection:
        if source.name not in self._connections:
            conn = McpConnection(source)
            self._connections[source.name] = conn
        return self._connections[source.name]

    def close_all(self):
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


def _extract_text(content: list) -> str:
    """Extract text from MCP content blocks."""
    texts = []
    for block in content:
        if hasattr(block, "text"):
            texts.append(block.text)
    return "\n".join(texts) if texts else str(content)
