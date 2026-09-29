"""One MCP connection per connector, owned by one task.

The same shape as the browser session: the MCP client's streams live in anyio
task groups that must be entered and exited in one task, so a single owner task
holds the session and serves calls from a queue.

Two transports: a local server started as a subprocess (stdio), or a remote one
over streamable HTTP (``url``), with an optional bearer token from the
environment.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("ondo.agent")


@dataclass
class McpTool:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class McpResult:
    text: str
    is_error: bool


@dataclass
class McpConnection:
    name: str
    command: list[str] = field(default_factory=list)
    url: str = ""
    token_env: str = ""
    env: dict[str, str] = field(default_factory=dict)
    tools: dict[str, McpTool] = field(default_factory=dict)
    _task: asyncio.Task | None = None
    _queue: asyncio.Queue | None = None
    _ready: asyncio.Event | None = None
    _error: BaseException | None = None

    def _transport(self):
        if self.url:
            from mcp.client.streamable_http import streamable_http_client

            headers = {}
            if self.token_env and os.environ.get(self.token_env):
                headers["authorization"] = f"Bearer {os.environ[self.token_env]}"
            if headers:
                import httpx

                return streamable_http_client(self.url, http_client=httpx.AsyncClient(headers=headers))
            return streamable_http_client(self.url)
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {**os.environ, **self.env} if self.env else None
        return stdio_client(StdioServerParameters(command=self.command[0], args=self.command[1:], env=env))

    async def _main(self) -> None:
        from mcp import ClientSession

        assert self._queue is not None and self._ready is not None
        try:
            async with self._transport() as streams:
                r, w = streams[0], streams[1]
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    for t in (await s.list_tools()).tools:
                        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
                        self.tools[t.name] = McpTool(t.name, t.description or "", dict(schema))
                    self._ready.set()
                    while True:
                        item = await self._queue.get()
                        if item is None:
                            break
                        name, args, fut = item
                        try:
                            res = await s.call_tool(name, args)
                            if not fut.done():
                                fut.set_result(res)
                        except Exception as e:  # surface to the caller, keep serving
                            if not fut.done():
                                fut.set_exception(e)
        except BaseException as e:
            self._error = e
            self._ready.set()
            while self._queue is not None and not self._queue.empty():
                item = self._queue.get_nowait()
                if item and not item[2].done():
                    item[2].set_exception(RuntimeError(f"{self.name} connection ended: {e}"))
            if isinstance(e, asyncio.CancelledError):
                raise

    async def start(self, timeout: float = 60) -> None:
        if self._task is not None and self._error is None:
            return
        self._error = None  # a failed start is tried again by the next run
        self.tools = {}
        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._main())
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        if self._error is not None:
            raise RuntimeError(f"could not start the {self.name} connector: {self._error}")

    async def call(self, name: str, args: dict[str, Any], timeout: float = 60) -> McpResult:
        if self._error is not None or self._queue is None:
            raise RuntimeError(f"the {self.name} connector is not running: {self._error}")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((name, args, fut))
        res = await asyncio.wait_for(fut, timeout=timeout)
        texts = [c.text for c in (res.content or []) if getattr(c, "type", "") == "text"]
        err = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
        return McpResult("\n".join(texts), err)

    async def aclose(self) -> None:
        if self._task is None:
            return
        if self._queue is not None:
            await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=15)
        except (TimeoutError, Exception):
            self._task.cancel()
        self._task = None
