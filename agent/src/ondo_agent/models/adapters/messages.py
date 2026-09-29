"""Messages API wire format.

The second wire format, wired from day one so the loop never grows assumptions
that only one format satisfies. Plain HTTP; no provider SDK. Explicit prefix
caching is expressed through the profile, never through the loop.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...tools.spec import ToolSpec, to_messages_tools
from ..profile import ModelProfile
from ..types import (
    ContextLengthError,
    ImagePart,
    Message,
    ModelError,
    ModelResponse,
    TextPart,
    ToolCall,
    Usage,
)

_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _content(m: Message, profile: ModelProfile) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in m.parts:
        if isinstance(p, TextPart) and p.text:
            out.append({"type": "text", "text": p.text})
    if profile.supports_vision:
        for p in m.parts:
            if isinstance(p, ImagePart):
                out.append(
                    {"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": p.data_b64}}
                )
    return out


def build_request(profile: ModelProfile, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
    system_text = "\n\n".join(m.text for m in messages if m.role == "system")
    wire: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        # The format wants alternating roles; merge consecutive same-role turns.
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"].extend(blocks)
        else:
            wire.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            # This format takes images inside the tool result, after its text.
            blocks = _content(m, profile)
            has_image = any(b["type"] == "image" for b in blocks)
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": blocks if has_image else m.text,
                        **({"is_error": True} if m.is_error else {}),
                    }
                ],
            )
        elif m.role == "assistant":
            blocks = _content(m, profile)
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            push("assistant", blocks or [{"type": "text", "text": ""}])
        else:
            push("user", _content(m, profile) or [{"type": "text", "text": ""}])

    body: dict[str, Any] = {
        "model": profile.model,
        "max_tokens": profile.max_output_tokens,
        "messages": wire,
    }
    if system_text:
        sys_block: dict[str, Any] = {"type": "text", "text": system_text}
        if profile.supports_prefix_cache == "explicit":
            sys_block["cache_control"] = {"type": "ephemeral"}
        body["system"] = [sys_block]
    if tools:
        wire_tools = to_messages_tools(tools)
        if profile.supports_prefix_cache == "explicit" and wire_tools:
            wire_tools[-1] = {**wire_tools[-1], "cache_control": {"type": "ephemeral"}}
        body["tools"] = wire_tools
    if profile.temperature is not None:
        body["temperature"] = profile.temperature
    if profile.reasoning_control == "budget" and profile.extra.get("reasoning_budget_tokens"):
        body["thinking"] = {"type": "enabled", "budget_tokens": int(profile.extra["reasoning_budget_tokens"])}
    return body


def parse_response(data: dict[str, Any]) -> ModelResponse:
    text, reasoning, calls = [], [], []
    for b in data.get("content") or []:
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "thinking":
            reasoning.append(b.get("thinking", ""))
        elif t == "tool_use":
            calls.append(ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {}))
    raw = data.get("stop_reason") or ""
    u = data.get("usage") or {}
    return ModelResponse(
        text="".join(text),
        tool_calls=calls,
        stop_reason=_STOP.get(raw, "end"),  # type: ignore[arg-type]
        usage=Usage(
            u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0, u.get("cache_read_input_tokens", 0) or 0
        ),
        model=data.get("model", ""),
        reasoning="".join(reasoning),
        raw_stop_reason=raw,
    )


class MessagesAdapter:
    def __init__(self, profile: ModelProfile, api_key: str | None, client: httpx.AsyncClient | None = None):
        self.profile = profile
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        body = build_request(self.profile, messages, tools)
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.profile.extra.get("api_version", "2023-06-01"),
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        url = self.profile.base_url.rstrip("/") + "/v1/messages"
        try:
            r = await self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ModelError(f"transport error: {e}", retryable=True) from e
        if r.status_code >= 400:
            text = r.text[:2000]
            if r.status_code in (400, 413) and ("too long" in text or "context" in text and "exceed" in text):
                raise ContextLengthError(text, status=r.status_code)
            raise ModelError(
                f"{r.status_code}: {text}",
                retryable=r.status_code in (408, 409, 429, 529) or r.status_code >= 500,
                status=r.status_code,
            )
        return parse_response(r.json())

    async def aclose(self) -> None:
        await self._client.aclose()
