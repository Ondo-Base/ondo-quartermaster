"""What the sample MCP servers share: the SDK's server class, its tool error, and a
JSON file the tests can read to check what actually landed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def server_class():
    """``(Server, ToolError)`` for the installed MCP SDK."""
    try:
        from mcp.server.mcpserver import MCPServer as Server
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # the MCP SDK before 2.0
        from mcp.server.fastmcp import FastMCP as Server
        from mcp.server.fastmcp.exceptions import ToolError
    return Server, ToolError


class JsonStore:
    """Records by id, in one JSON file, seeded on first use."""

    def __init__(self, path: Path, seed: list[dict[str, Any]]):
        self.path = Path(path)
        if not self.path.exists():
            self._save({r["id"]: r for r in seed})

    def _load(self) -> dict[str, dict]:
        return json.loads(self.path.read_text())

    def _save(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def get(self, rid: str) -> dict:
        r = self._load().get(rid)
        if r is None:
            raise ValueError(f"no record {rid}")
        return r

    def put(self, r: dict) -> dict:
        data = self._load()
        data[r["id"]] = r
        self._save(data)
        return r

    def all(self) -> list[dict]:
        return list(self._load().values())
