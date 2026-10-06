"""Adapter contract.

An adapter normalizes only what genuinely differs: reasoning controls,
prefix-cache hints, streaming events, refusal and safety-stop shapes, image
limits, and tool-call quirks that need patching. It is a few hundred lines, and
it reads the capability profile rather than branching on a provider name.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ..profile import ModelProfile
from ..schema import ToolSpec
from ..types import Message, ModelResponse


class ModelError(RuntimeError):
    """Transport or provider error. Retryable unless ``fatal``."""

    def __init__(self, message: str, *, fatal: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.fatal = fatal
        self.status = status


class ContextLengthExceeded(ModelError):
    """The reactive layer of context management catches this (§3)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, fatal=False)


@dataclass(slots=True)
class Request:
    """What the harness asks for, in our units."""

    profile: ModelProfile
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_output_tokens: int | None = None
    temperature: float = 0.0
    #: "low" | "medium" | "high"; the adapter maps it to effort or to a budget.
    reasoning: str | None = None
    stop: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class Adapter(abc.ABC):
    name: str = "adapter"

    @abc.abstractmethod
    async def complete(self, request: Request) -> ModelResponse: ...

    async def aclose(self) -> None:
        return None
