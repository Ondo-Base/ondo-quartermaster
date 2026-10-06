"""OpenAI-compatible chat completions.

One adapter, three deployments: a vendor's own endpoint, a self-hosted LiteLLM
proxy, or OpenRouter. Which one is a base URL, not a code path — that is the
whole reason the gateway sits below the adapter.
"""

from __future__ import annotations

import json
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

_EFFORT = {"low": "low", "medium": "medium", "high": "high"}


class OpenAICompatibleAdapter(Adapter):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("ONDO_OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("ONDO_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
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
            raise ModelError("no API key for the OpenAI-compatible adapter", fatal=True)
        body = self.build_body(request)
        try:
            r = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as e:  # transport: retryable
            raise ModelError(f"transport error: {e}") from e
        if r.status_code >= 400:
            raise _classify(r.status_code, r.text)
        return self.parse(r.json(), request)

    # -- wire format ------------------------------------------------------
    def build_body(self, request: Request) -> dict[str, Any]:
        p = request.profile
        msgs: list[dict[str, Any]] = []
        # Stable content first so a prefix cache has something to hold onto,
        # automatic or explicit (§4).
        if request.system:
            msgs.append({"role": "system", "content": request.system})
        for m in request.messages:
            msgs.extend(_encode_message(m, p.supports_vision))
        body: dict[str, Any] = {
            "model": p.wire_name,
            "messages": msgs,
            "max_tokens": request.max_output_tokens or p.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            body["tools"] = [t.to_openai() for t in request.tools]
            body["tool_choice"] = "auto"
            if p.parallel_tool_calls:
                body["parallel_tool_calls"] = True
        if request.reasoning and p.reasoning_control == "effort":
            body["reasoning_effort"] = _EFFORT.get(request.reasoning, "medium")
        if request.stop:
            body["stop"] = request.stop
        body.update(request.extra)
        return body

    def parse(self, data: dict[str, Any], request: Request) -> ModelResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append(
                ToolCall(
                    id=tc.get("id") or "",
                    name=fn.get("name") or "",
                    # Providers ship arguments as a JSON string, sometimes
                    # malformed. Patching that is adapter work, not loop work.
                    arguments=_loads(fn.get("arguments")),
                )
            )
        usage = data.get("usage") or {}
        return ModelResponse(
            text=msg.get("content") or "",
            reasoning=msg.get("reasoning_content") or "",
            tool_calls=calls,
            stop_reason=_stop(choice.get("finish_reason"), bool(calls), msg),
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            ),
            model=data.get("model") or request.profile.wire_name,
            provider=self.name,
            raw=data,
        )


def _encode_message(m: Message, vision: bool) -> list[dict[str, Any]]:
    if m.role is Role.TOOL:
        out = []
        for b in m.blocks:
            if isinstance(b, ToolResultBlock):
                out.append({"role": "tool", "tool_call_id": b.tool_call_id, "content": b.content})
                if b.images and vision:
                    out.append({"role": "user", "content": [_image(i) for i in b.images]})
        return out

    if m.role is Role.ASSISTANT:
        d: dict[str, Any] = {"role": "assistant", "content": m.flat_text or None}
        calls = [b for b in m.blocks if isinstance(b, ToolCall)]
        if calls:
            d["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in calls
            ]
        return [d]

    content: list[dict[str, Any]] = []
    for b in m.blocks:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ImageBlock) and vision:
            content.append(_image(b))
    if len(content) == 1 and content[0]["type"] == "text":
        return [{"role": str(m.role), "content": content[0]["text"]}]
    return [{"role": str(m.role), "content": content}]


def _image(b: ImageBlock) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{b.media_type};base64,{b.data}"}}


def _loads(s: Any) -> dict[str, Any]:
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {"__unparsed__": s}


def _stop(reason: str | None, has_calls: bool, msg: dict[str, Any]) -> StopReason:
    if msg.get("refusal"):
        return StopReason.REFUSAL
    match reason:
        case "tool_calls" | "function_call":
            return StopReason.TOOL_USE
        case "length":
            return StopReason.MAX_TOKENS
        case "content_filter":
            return StopReason.SAFETY
        case _:
            return StopReason.TOOL_USE if has_calls else StopReason.END


def _classify(status: int, text: str) -> ModelError:
    low = text.lower()
    if "context" in low and ("length" in low or "window" in low or "too long" in low):
        return ContextLengthExceeded(text[:500])
    fatal = status in (400, 401, 403, 404) and "rate" not in low
    return ModelError(f"HTTP {status}: {text[:500]}", fatal=fatal, status=status)
