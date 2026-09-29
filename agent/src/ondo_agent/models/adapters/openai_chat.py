"""OpenAI-compatible chat completions wire format.

This is the format a self-hosted LiteLLM gateway, OpenRouter, vLLM and most
self-hosted servers speak, so one adapter reaches most providers. Plain HTTP; no
provider SDK.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ...tools.spec import ToolSpec, to_openai_tools
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

_STOP = {"stop": "end", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


def _image_url(p: ImagePart) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{p.media_type};base64,{p.data_b64}"}}


def build_request(profile: ModelProfile, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
    wire: list[dict[str, Any]] = []
    # This format's tool messages carry text only. Screenshots a tool returned go
    # in one user message straight after the run of tool results, each labelled
    # with the call it answers, text first.
    held: list[dict[str, Any]] = []

    def release() -> None:
        if held:
            wire.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Screenshots from the tool results above:"}, *held],
                }
            )
            held.clear()

    for m in messages:
        if m.role == "tool":
            wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
            images = [p for p in m.parts if isinstance(p, ImagePart)]
            if images and profile.supports_vision:
                held.append({"type": "text", "text": f"From {m.tool_name} ({m.tool_call_id}):"})
                held.extend(_image_url(p) for p in images)
            continue
        release()
        if m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in m.tool_calls
                ]
            wire.append(item)
            continue
        images = [p for p in m.parts if isinstance(p, ImagePart)]
        if images and profile.supports_vision:
            content: list[dict[str, Any]] = []
            # Text before image: the model knows what it is looking for.
            for p in m.parts:
                if isinstance(p, TextPart):
                    content.append({"type": "text", "text": p.text})
            for p in images:
                content.append(_image_url(p))
            wire.append({"role": m.role, "content": content})
        else:
            wire.append({"role": m.role, "content": m.text})
    release()

    body: dict[str, Any] = {
        "model": profile.model,
        "messages": wire,
        "max_tokens": profile.max_output_tokens,
    }
    if tools:
        body["tools"] = to_openai_tools(tools)
        body["parallel_tool_calls"] = profile.parallel_tool_calls
    if profile.temperature is not None:
        body["temperature"] = profile.temperature
    if profile.reasoning_control == "effort" and profile.extra.get("reasoning_effort"):
        body["reasoning_effort"] = profile.extra["reasoning_effort"]
    return body


def parse_response(data: dict[str, Any]) -> ModelResponse:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls: list[ToolCall] = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            # Some models emit invalid JSON. Pass it through so the tool layer can
            # return a readable error the model can correct, rather than crash.
            args = {"__unparsed_arguments__": raw}
        calls.append(ToolCall(id=c.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args))
    raw_stop = choice.get("finish_reason") or ""
    stop = _STOP.get(raw_stop, "end")
    if calls:
        stop = "tool_use"
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    return ModelResponse(
        text=msg.get("content") or "",
        tool_calls=calls,
        stop_reason=stop,  # type: ignore[arg-type]
        usage=Usage(u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0, cached),
        model=data.get("model", ""),
        reasoning=msg.get("reasoning_content") or msg.get("reasoning") or "",
        raw_stop_reason=raw_stop,
    )


class OpenAIChatAdapter:
    def __init__(self, profile: ModelProfile, api_key: str | None, client: httpx.AsyncClient | None = None):
        self.profile = profile
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        body = build_request(self.profile, messages, tools)
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        url = self.profile.base_url.rstrip("/") + "/chat/completions"
        try:
            r = await self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ModelError(f"transport error: {e}", retryable=True) from e
        if r.status_code >= 400:
            text = r.text[:2000]
            if r.status_code == 400 and ("context_length" in text or "maximum context" in text):
                raise ContextLengthError(text, status=400)
            raise ModelError(
                f"{r.status_code}: {text}",
                retryable=r.status_code in (408, 409, 429) or r.status_code >= 500,
                status=r.status_code,
            )
        return parse_response(r.json())

    async def aclose(self) -> None:
        await self._client.aclose()
