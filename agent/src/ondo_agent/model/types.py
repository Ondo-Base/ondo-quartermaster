"""Provider-neutral message types. Adapters translate to and from these.

Nothing in this module names a vendor. If a field exists only because one
provider needs it, it belongs in the adapter or in the capability profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(slots=True)
class TextBlock:
    text: str
    #: Untrusted text (file contents, screen text, MCP server text) is fenced as
    #: data by the context builder before it ever reaches an adapter.
    untrusted: bool = False
    kind: Literal["text"] = "text"


@dataclass(slots=True)
class ImageBlock:
    #: base64, no data: prefix
    data: str
    media_type: str = "image/png"
    width: int | None = None
    height: int | None = None
    #: native-resolution size the coordinates must be scaled back to
    native_width: int | None = None
    native_height: int | None = None
    kind: Literal["image"] = "image"


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    kind: Literal["tool_call"] = "tool_call"


@dataclass(slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    untrusted: bool = True
    images: list[ImageBlock] = field(default_factory=list)
    kind: Literal["tool_result"] = "tool_result"


Block = TextBlock | ImageBlock | ToolCall | ToolResultBlock


@dataclass(slots=True)
class Message:
    role: Role
    blocks: list[Block] = field(default_factory=list)

    @classmethod
    def text(cls, role: Role, text: str, *, untrusted: bool = False) -> Message:
        return cls(role=role, blocks=[TextBlock(text=text, untrusted=untrusted)])

    @property
    def flat_text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))


class StopReason(StrEnum):
    END = "end"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    SAFETY = "safety"
    ERROR = "error"


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None


@dataclass(slots=True)
class ModelResponse:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.END
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    raw: dict[str, Any] | None = None

    def as_message(self) -> Message:
        blocks: list[Block] = []
        if self.text:
            blocks.append(TextBlock(text=self.text))
        blocks.extend(self.tool_calls)
        return Message(role=Role.ASSISTANT, blocks=blocks)
