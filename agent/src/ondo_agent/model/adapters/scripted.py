"""A deterministic provider.

It is not a stub for want of a real one: the eval suite and CI need a model whose
output does not move, and a harness bug must be distinguishable from a model
having a different day. Scripts are written against the internal tool schema, so
this adapter exercises exactly the code path a real provider does.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..types import ModelResponse, StopReason, ToolCall, Usage
from .base import Adapter, Request

#: A turn is either a final message or a set of tool calls.
Turn = ModelResponse
#: A script may be a fixed list of turns, or a function of the request.
Script = Iterable[Turn] | Callable[[Request], Turn]


@dataclass(slots=True)
class ScriptedAdapter(Adapter):
    script: Script
    name: str = "scripted"
    _turns: list[Turn] | None = None
    _i: int = 0

    def __post_init__(self) -> None:
        if not callable(self.script):
            self._turns = list(self.script)

    async def complete(self, request: Request) -> ModelResponse:
        if callable(self.script):
            response = self.script(request)
        else:
            assert self._turns is not None
            if self._i >= len(self._turns):
                response = ModelResponse(text="", stop_reason=StopReason.END)
            else:
                response = self._turns[self._i]
                self._i += 1
        response.model = request.profile.wire_name
        response.provider = "scripted"
        if response.usage.input_tokens == 0:
            response.usage = Usage(
                input_tokens=_estimate(request),
                output_tokens=max(1, len(response.text) // 4),
            )
        return response


def say(text: str) -> ModelResponse:
    return ModelResponse(text=text, stop_reason=StopReason.END)


def call(name: str, /, **arguments: Any) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=uuid.uuid4().hex[:8], name=name, arguments=arguments)],
        stop_reason=StopReason.TOOL_USE,
    )


def call_many(*calls: tuple[str, dict[str, Any]]) -> ModelResponse:
    """Independent reads in one turn — the parallel-tool-call path."""
    return ModelResponse(
        tool_calls=[ToolCall(id=uuid.uuid4().hex[:8], name=n, arguments=a) for n, a in calls],
        stop_reason=StopReason.TOOL_USE,
    )


def _estimate(request: Request) -> int:
    n = len(request.system) // 4
    for m in request.messages:
        n += len(m.flat_text) // 4
    return n
