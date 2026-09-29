"""One internal tool schema, and the generators for every wire format.

Tools are defined once, here, in our own shape. Each provider's tool format is
generated from it at the adapter, so the churn in vendor tool-calling shapes
touches one file. The same definitions generate the tool documentation.

Descriptions carry the operating rules. In every harness worth studying the tool
descriptions do more work than the system prompt; write them like documentation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# The effect a tool *may* have. Gates are decided on effect, not on tool (see
# ``gates.py``), but the tool's declared ceiling is the first deterministic input.
Effect = Literal["read", "write_local", "write_shared", "submit", "send_external", "move_money"]


@dataclass
class Image:
    """A screenshot a tool returns. The bytes go to a blob store beside the run's
    log, never into the log itself (and never to the control plane): the log
    records the SHA-256, so the chain still commits to exactly what was seen."""

    data: bytes
    media_type: str = "image/png"
    width: int = 0
    height: int = 0
    # How the model and the UI refer to it: "s3 · Remote billing".
    label: str = ""


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # Structured detail for the log and the UI. Never sent to the model.
    detail: dict[str, Any] = field(default_factory=dict)
    # Where this content came from, when it is untrusted data (a file path, a URL).
    # Anything with an origin is fenced and screened before the model sees it.
    untrusted_origin: str | None = None
    # Screenshots. Sent to the model only when its profile supports vision, and
    # only the most recent few (see ``harness.context``).
    images: list[Image] = field(default_factory=list)


Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler | None = None
    # Which grant the tool needs before it can run at all.
    grant: Literal["files", "screen", "input", "none"] = "none"
    max_effect: Effect = "read"
    # Tools with side effects run one at a time even when the model batches them.
    parallel_safe: bool = True
    # One plain line for the run's step list ("Read Q3_Renewals.xlsx").
    title: Callable[[dict[str, Any]], str] | None = None

    def step_title(self, args: dict[str, Any]) -> str:
        if self.title:
            try:
                return self.title(args)
            except Exception:  # a bad title must never break a run; fall back to the name
                logging.getLogger("ondo.agent").debug("step title failed for %s", self.name, exc_info=True)
        return self.name.replace("_", " ").capitalize()

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolContext:
    """What a tool handler may reach: the broker, the log, the gate, the run.

    Handlers get this instead of globals so the same tool behaves identically in
    a live run, a replay and a test.
    """

    def __init__(self, *, run, broker, log, gates, approvals, screener, config):
        self.run = run
        self.broker = broker
        self.log = log
        self.gates = gates
        self.approvals = approvals
        self.screener = screener
        self.config = config


# -- wire-format generators --------------------------------------------------


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def to_messages_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]


def to_markdown(tools: list[ToolSpec]) -> str:
    out = ["# Ondo tools", "", "Generated from `ondo_agent.tools.spec`. Do not edit by hand.", ""]
    for t in tools:
        out += [f"## `{t.name}`", "", t.description, ""]
        out += [f"- Grant: `{t.grant}`", f"- Highest effect: `{t.max_effect}`", ""]
        props = t.parameters.get("properties", {})
        req = set(t.parameters.get("required", []))
        if props:
            out += ["| Parameter | Type | Required | Description |", "| --- | --- | --- | --- |"]
            for k, v in props.items():
                typ = v.get("type", "any")
                out.append(f"| `{k}` | {typ} | {'yes' if k in req else 'no'} | {v.get('description', '')} |")
            out.append("")
    return "\n".join(out)


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Small helper for writing JSON Schema objects inline."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
