"""One internal tool schema.

Tools are defined once, here, in our own format. Each provider's wire format is
generated at the adapter, so vendor churn touches one file. The same definitions
produce the docs and the eval fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Effect(StrEnum):
    """What a tool call does to the world.

    Gates are on effect, not on tool (§8). A tool declares the effects it *can*
    have; the executor declares what a specific call *does* have.
    """

    READ = "read"
    WRITE_LOCAL = "write_local"
    WRITE_SHARED = "write_shared"
    SUBMIT_TO_SYSTEM_OF_RECORD = "submit_to_system_of_record"
    SEND_EXTERNAL = "send_external"
    MOVES_MONEY = "moves_money"
    CONTROL_INPUT = "control_input"
    CAPTURE_SCREEN = "capture_screen"


class Grant(StrEnum):
    """The three independent grants. Never one "allow"."""

    FILES = "files"
    SCREEN = "screen"
    INPUT = "input"


class Rung(StrEnum):
    """Where a tool sits on the ladder. The harness climbs down in this order."""

    STRUCTURED = "structured"
    SEMANTIC = "semantic"
    PIXELS = "pixels"


JsonType = Literal["string", "integer", "number", "boolean", "object", "array"]


@dataclass(slots=True)
class Param:
    name: str
    type: JsonType
    description: str
    required: bool = True
    enum: list[str] | None = None
    items: JsonType | None = None
    default: Any = None

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            s["enum"] = self.enum
        if self.type == "array":
            s["items"] = {"type": self.items or "string"}
        if self.default is not None:
            s["default"] = self.default
        return s


@dataclass(slots=True)
class ToolSpec:
    name: str
    #: Written like documentation. In every harness studied, tool descriptions
    #: carry the operating rules — "prefer the accessibility tree; only
    #: screenshot when the tree is empty" does more work here than in the
    #: system prompt.
    description: str
    params: list[Param] = field(default_factory=list)
    effects: frozenset[Effect] = frozenset({Effect.READ})
    grants: frozenset[Grant] = frozenset()
    rung: Rung = Rung.STRUCTURED
    #: Independent reads may be issued in one turn.
    parallel_safe: bool = True
    #: Tool output that the collapse layer may summarise when long.
    collapsible: bool = True

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.params},
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    # -- wire formats -----------------------------------------------------
    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.json_schema(),
        }

    def to_markdown(self) -> str:
        lines = [f"### `{self.name}`", "", self.description.strip(), ""]
        lines.append(f"- Ladder rung: **{self.rung}**")
        if self.grants:
            lines.append("- Requires grant: " + ", ".join(sorted(self.grants)))
        lines.append("- Possible effects: " + ", ".join(sorted(self.effects)))
        if self.params:
            lines += ["", "| Parameter | Type | Required | Description |", "| --- | --- | --- | --- |"]
            for p in self.params:
                lines.append(f"| `{p.name}` | {p.type} | {'yes' if p.required else 'no'} | {p.description} |")
        return "\n".join(lines) + "\n"


def render_tool_docs(specs: list[ToolSpec]) -> str:
    """The tool reference, generated from the same definitions the model sees."""
    by_rung: dict[Rung, list[ToolSpec]] = {}
    for s in specs:
        by_rung.setdefault(s.rung, []).append(s)
    out = ["# Tool reference", "", "Generated from `ondo_agent.model.schema`. Do not edit by hand.", ""]
    for rung in (Rung.STRUCTURED, Rung.SEMANTIC, Rung.PIXELS):
        if rung not in by_rung:
            continue
        out += [f"## {rung.value.title()}", ""]
        out += [s.to_markdown() for s in sorted(by_rung[rung], key=lambda s: s.name)]
    return "\n".join(out)
