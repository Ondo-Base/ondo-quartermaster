"""Anthropic-style messages transport.

Kept deliberately parallel to the OpenAI-compatible adapter: same contract, same
error classification, same coordinate-free view of the world. What differs —
explicit prefix-cache breakpoints, a thinking budget rather than an effort level,
system as a top-level field — is exactly the list the capability profile carries.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..types import (
    ImageBlock,
    Message,
    ModelResponse,
    Role,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    Usage,
)
from .base import Adapter, ContextLengthExceeded, ModelError, Request

_BUDGET = {"low": 2_000, "medium": 8_000, "high": 24_000}


class AnthropicMessagesAdapter(Adapter):
    name = "anthropic_messages"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        version: str = "2023-06-01",
        timeout: float = 180.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("ONDO_ANTHROPIC_BASE_URL") or "https://api.anthropic.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("ONDO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        self.version = version
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, request: Request) -> ModelResponse:
        if not self.configured:
            raise ModelError("no API key for the messages adapter", fatal=True)
        try:
            r = await self._client.post(
                f"{self.base_url}/messages",
                json=self.build_body(request),
                headers={"x-api-key": self.api_key or "", "anthropic-version": self.version},
            )
        except httpx.HTTPError as e:
            raise ModelError(f"transport error: {e}") from e
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        return self.parse(r.json(), request)

    # -- wire format ------------------------------------------------------
    def build_body(self, request: Request) -> dict[str, Any]:
        p = request.profile
        system: Any = request.system
        if p.prefix_cache == "explicit" and request.system:
            # One breakpoint after the frozen system prompt. Volatile content
            # (timestamps, run ids, the current screen) is always later in the
            # message list, so the prefix stays stable.
            system = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
        body: dict[str, Any] = {
            "model": p.wire_name,
            "system": system,
            "messages": [_encode_message(m, p.supports_vision) for m in request.messages],
            "max_tokens": request.max_output_tokens or p.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            tools = [t.to_anthropic() for t in request.tools]
            if p.prefix_cache == "explicit" and tools:
                tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            body["tools"] = tools
        if request.reasoning and p.reasoning_control == "budget":
            budget = _BUDGET.get(request.reasoning, 8_000)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Thinking needs headroom above the budget, and providers reject the
            # combination rather than clamping it.
            body["max_tokens"] = max(body["max_tokens"], budget + 4_096)
            body["temperature"] = 1.0
        if request.stop:
            body["stop_sequences"] = request.stop
        body.update(request.extra)
        return body

    def parse(self, data: dict[str, Any], request: Request) -> ModelResponse:
        text_parts, reasoning_parts, calls = [], [], []
        for block in data.get("content") or []:
            match block.get("type"):
                case "text":
                    text_parts.append(block.get("text", ""))
                case "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                case "tool_use":
                    calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            arguments=block.get("input") or {},
                        )
                    )
        usage = data.get("usage") or {}
        return ModelResponse(
            text="\n".join(p for p in text_parts if p),
            reasoning="\n".join(reasoning_parts),
            tool_calls=calls,
            stop_reason=_stop(data.get("stop_reason"), bool(calls)),
            usage=Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            ),
            model=data.get("model") or request.profile.wire_name,
            provider=self.name,
            raw=data,
        )


def _encode_message(m: Message, vision: bool) -> dict[str, Any]:
    # This transport has no tool role: results are user-turn blocks.
    role = "user" if m.role in (Role.USER, Role.TOOL) else "assistant"
    content: list[dict[str, Any]] = []
    for b in m.blocks:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ImageBlock) and vision:
            content.append({"type": "image", "source": {"type": "base64", "media_type": b.media_type, "data": b.data}})
        elif isinstance(b, ToolCall):
            content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.arguments})
        elif isinstance(b, ToolResultBlock):
            inner: list[dict[str, Any]] = [{"type": "text", "text": b.content}]
            if vision:
                inner += [
                    {"type": "image", "source": {"type": "base64", "media_type": i.media_type, "data": i.data}}
                    for i in b.images
                ]
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "is_error": b.is_error,
                    "content": inner,
                }
            )
    return {"role": role, "content": content or [{"type": "text", "text": ""}]}


def _stop(reason: str | None, has_calls: bool) -> StopReason:
    match reason:
        case "tool_use":
            return StopReason.TOOL_USE
        case "max_tokens":
            return StopReason.MAX_TOKENS
        case "refusal":
            return StopReason.REFUSAL
        case _:
            return StopReason.TOOL_USE if has_calls else StopReason.END


def _classify(status: int, text: str) -> ModelError:
    low = text.lower()
    if "prompt is too long" in low or ("context" in low and "long" in low):
        return ContextLengthExceeded(text[:500])
    fatal = status in (400, 401, 403, 404) and "overloaded" not in low
    return ModelError(f"HTTP {status}: {text[:500]}", fatal=fatal, status=status)
