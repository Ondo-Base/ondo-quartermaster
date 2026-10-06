"""The harness's own message and response types.

These are the only shapes the loop knows. Every provider's wire format is
generated from them at the adapter, and every provider's response is normalised
back into them. A provider's SDK types never cross into the harness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["end", "tool_use", "max_tokens", "refusal", "error"]


@dataclass
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ImagePart:
    """A base64 image. Only sent when the profile says the model supports vision."""

    media_type: str
    data_b64: str
    width: int = 0
    height: int = 0
    type: Literal["image"] = "image"


Part = TextPart | ImagePart


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    parts: list[Part] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    # For role == "tool": which call this answers.
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @classmethod
    def system(cls, text: str) -> Message:
        return cls("system", [TextPart(text)])

    @classmethod
    def user(cls, text: str) -> Message:
        return cls("user", [TextPart(text)])


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    # Reasoning text, when a provider returns it separately. Logged, never re-sent.
    reasoning: str = ""
    raw_stop_reason: str = ""


class ModelError(Exception):
    """A provider call failed in a way the gateway may retry or fail over."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ContextLengthError(ModelError):
    """The request was too long. The reactive context layer handles this one."""
