"""Capability profiles: what actually differs between models.

The harness reads a profile and adapts. Adding a provider is a new profile and
an adapter case, never a change to the loop. No `if provider ==` in the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class PrefixCache(StrEnum):
    NONE = "none"
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"


class ReasoningControl(StrEnum):
    NONE = "none"
    EFFORT = "effort"
    BUDGET = "budget"


@dataclass(slots=True, frozen=True)
class ModelProfile:
    id: str
    provider: str
    #: the model name as the transport expects it
    wire_name: str
    adapter: str = "openai_compatible"

    supports_vision: bool = False
    prefix_cache: PrefixCache = PrefixCache.NONE
    max_images_per_request: int = 0
    max_image_long_edge_px: int = 1568
    #: We use our own computer tool schema regardless (§6); this only records
    #: whether a native one exists to map onto.
    native_computer_tool: bool = False
    reasoning_control: ReasoningControl = ReasoningControl.NONE
    parallel_tool_calls: bool = True
    context_window: int = 128_000
    max_output_tokens: int = 8_192
    #: A small per-model prompt overlay, versioned with the profile (§10).
    prompt_overlay: str = ""
    cost_per_mtok_in: float | None = None
    cost_per_mtok_out: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_overrides(self, **kw: Any) -> ModelProfile:
        return replace(self, **kw)


#: Two providers wired from day one, plus the scripted provider that runs in CI
#: and in the eval suite. Agnosticism added later is never real.
PROFILES: dict[str, ModelProfile] = {
    "scripted": ModelProfile(
        id="scripted",
        provider="scripted",
        wire_name="scripted",
        adapter="scripted",
        supports_vision=True,
        max_images_per_request=3,
        context_window=1_000_000,
        parallel_tool_calls=True,
    ),
    # Provider A: OpenAI-compatible transport. This one profile also covers a
    # self-hosted LiteLLM proxy and OpenRouter, which is the point of §4.
    "provider-a-large": ModelProfile(
        id="provider-a-large",
        provider="openai_compatible",
        wire_name="gpt-4.1",
        adapter="openai_compatible",
        supports_vision=True,
        prefix_cache=PrefixCache.AUTOMATIC,
        max_images_per_request=20,
        max_image_long_edge_px=1536,
        reasoning_control=ReasoningControl.EFFORT,
        context_window=1_000_000,
        max_output_tokens=32_768,
        cost_per_mtok_in=2.0,
        cost_per_mtok_out=8.0,
    ),
    "provider-a-small": ModelProfile(
        id="provider-a-small",
        provider="openai_compatible",
        wire_name="gpt-4.1-mini",
        adapter="openai_compatible",
        supports_vision=True,
        prefix_cache=PrefixCache.AUTOMATIC,
        max_images_per_request=20,
        context_window=1_000_000,
        max_output_tokens=16_384,
        cost_per_mtok_in=0.4,
        cost_per_mtok_out=1.6,
    ),
    # Provider B: Anthropic-style messages transport.
    "provider-b-large": ModelProfile(
        id="provider-b-large",
        provider="anthropic_messages",
        wire_name="claude-opus-5",
        adapter="anthropic_messages",
        supports_vision=True,
        prefix_cache=PrefixCache.EXPLICIT,
        max_images_per_request=20,
        max_image_long_edge_px=1568,
        native_computer_tool=True,
        reasoning_control=ReasoningControl.BUDGET,
        context_window=200_000,
        max_output_tokens=32_000,
        cost_per_mtok_in=5.0,
        cost_per_mtok_out=25.0,
    ),
    "provider-b-small": ModelProfile(
        id="provider-b-small",
        provider="anthropic_messages",
        wire_name="claude-haiku-4-5-20251001",
        adapter="anthropic_messages",
        supports_vision=True,
        prefix_cache=PrefixCache.EXPLICIT,
        max_images_per_request=20,
        reasoning_control=ReasoningControl.BUDGET,
        context_window=200_000,
        max_output_tokens=8_192,
        cost_per_mtok_in=1.0,
        cost_per_mtok_out=5.0,
    ),
}


class Job(StrEnum):
    """Name the job, not the model (§4)."""

    ORCHESTRATOR = "orchestrator"
    EXTRACTOR = "extractor"
    GROUNDING = "grounding"


def resolve(job: Job, config: dict[str, str] | None = None) -> ModelProfile:
    """Which profile runs a job. Config decides; the loop never does."""
    config = config or {}
    profile_id = config.get(str(job)) or _DEFAULT_ASSIGNMENT[job]
    if profile_id not in PROFILES:
        raise KeyError(f"unknown model profile {profile_id!r}; known: {sorted(PROFILES)}")
    return PROFILES[profile_id]


_DEFAULT_ASSIGNMENT: dict[Job, str] = {
    Job.ORCHESTRATOR: "scripted",
    Job.EXTRACTOR: "scripted",
    Job.GROUNDING: "scripted",
}
