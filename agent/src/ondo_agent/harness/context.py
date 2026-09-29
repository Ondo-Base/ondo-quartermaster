"""Context management: rebuild the model's context from the log, every turn.

The log is the source of truth, so the context is a pure function of it plus a
collapse policy. Layers, in the order the plan asks for them:

- **Collapse** — verbose tool output from older turns (spreadsheet dumps, page
  snapshots) is replaced by a one-line stub that says what it was and how to get
  it again. The most recent ``keep_recent_turns`` stay whole.
- **Proactive** — when the estimated size passes ``proactive_ratio`` of the
  window, collapse harder: only the latest turn keeps full tool output.
- **Reactive** — on a context-length error the loop calls ``build`` again with
  ``aggressive=True`` and retries once.
- **Screenshots** — a rolling buffer: only the ``keep_images`` most recent go to
  the model, as images, and only when its profile has vision. Older ones become a
  one-line stub. The cut moves in batches of ``image_batch``, so the prefix a
  provider caches does not change on every screenshot. Never more than
  ``max_images`` per request.

The same code runs for every provider, so context behaves the same on every model.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from ..log import (
    CONTEXT_INJECTION,
    MODEL_RESPONSE,
    SYSTEM_PROMPT,
    TOOL_RESULT,
    USER_MESSAGE,
    Event,
)
from ..models.types import ImagePart, Message, Part, TextPart, ToolCall


@dataclass
class ContextPolicy:
    context_window: int = 128_000
    keep_recent_turns: int = 2
    collapse_over_chars: int = 2_000
    proactive_ratio: float = 0.6
    vision: bool = False
    keep_images: int = 3
    image_batch: int = 3
    max_images: int = 20


def kept_images(total: int, policy: ContextPolicy) -> int:
    """How many of the most recent screenshots go to the model as images."""
    if total <= policy.keep_images:
        n = total
    else:
        n = policy.keep_images + (total - policy.keep_images) % policy.image_batch
    return min(n, policy.max_images)


def _image_refs(e: Event) -> list[dict]:
    if e.type == TOOL_RESULT:
        return list((e.data.get("detail") or {}).get("images") or [])
    if e.type == CONTEXT_INJECTION:
        return list(e.data.get("images") or [])
    return []


def estimate_tokens(messages: list[Message]) -> int:
    """Characters over four. Good enough to decide when to collapse; the provider's
    own count comes back in usage and is logged."""
    n = 0
    for m in messages:
        n += len(m.text) + 16
        # A provider bills an image by its area: roughly one token per 750 pixels.
        n += sum(4 * max(1, p.width * p.height // 750) for p in m.parts if isinstance(p, ImagePart))
        for c in m.tool_calls:
            n += len(c.name) + len(json.dumps(c.arguments)) + 16
    return n // 4


def _stub(e: Event) -> str:
    d = e.data
    n = len(d.get("content", ""))
    origin = d.get("origin") or d.get("name", "tool")
    return f"[collapsed: {n:,} characters of {d.get('name', 'tool')} output from {origin}. Call the tool again if you need it.]"


def build(
    events: list[Event], policy: ContextPolicy, *, aggressive: bool = False, blobs=None
) -> tuple[list[Message], list[int]]:
    """Return (messages, seqs of collapsed tool results). ``blobs`` is where the
    screenshots live; without it (or without vision) they are stubs."""
    refs = [(e.seq, i) for e in events for i, _ in enumerate(_image_refs(e))]
    keep = kept_images(len(refs), policy) if policy.vision and blobs is not None else 0
    if aggressive:
        keep = min(keep, 1)
    send = set(refs[len(refs) - keep :]) if keep else set()

    def images_for(e: Event) -> tuple[list[Part], str]:
        parts: list[Part] = []
        notes: list[str] = []
        for i, r in enumerate(_image_refs(e)):
            label = r.get("label") or "screenshot"
            data = blobs.get(r["sha256"]) if (e.seq, i) in send else None
            if data is None:
                why = "pruned from context" if policy.vision else "not shown: this model cannot see images"
                notes.append(f"[{label}: {why}. Take a new screenshot if you need it.]")
                continue
            notes.append(f"[{label}, {r.get('width')}x{r.get('height')} pixels, attached]")
            parts.append(
                ImagePart(
                    r.get("media_type", "image/png"),
                    base64.b64encode(data).decode(),
                    r.get("width", 0),
                    r.get("height", 0),
                )
            )
        return parts, "\n".join(notes)

    # Which model turn each tool result belongs to.
    turn_of: dict[int, int] = {}
    turn = 0
    for e in events:
        if e.type == MODEL_RESPONSE:
            turn += 1
        elif e.type == TOOL_RESULT:
            turn_of[e.seq] = turn
    last_turn = turn

    def assemble(keep_turns: int) -> tuple[list[Message], list[int]]:
        system: list[str] = []
        msgs: list[Message] = []
        collapsed: list[int] = []
        pending: dict[str, str] = {}  # tool calls with no result yet: id -> name

        def flush() -> None:
            # A call the run never executed (it stopped, or a fork cut it off)
            # still needs an answer, or no provider will accept the history.
            for cid, name in pending.items():
                msgs.append(
                    Message(
                        "tool",
                        [TextPart("Not executed: the run stopped before this call ran.")],
                        tool_call_id=cid,
                        tool_name=name,
                        is_error=True,
                    )
                )
            pending.clear()

        for e in events:
            d = e.data
            if e.type in (CONTEXT_INJECTION, USER_MESSAGE, MODEL_RESPONSE):
                flush()
            if e.type == SYSTEM_PROMPT:
                system.append(d["text"])
            elif e.type == CONTEXT_INJECTION:
                imgs, note = images_for(e)
                text = d["text"] + (f"\n{note}" if note else "")
                # Text before the image: the model knows what it is looking for.
                msgs.append(Message("user", [TextPart(text), *imgs]))
            elif e.type == USER_MESSAGE:
                msgs.append(Message.user(d["text"]))
            elif e.type == MODEL_RESPONSE:
                msgs.append(
                    Message(
                        "assistant",
                        [TextPart(d.get("text", ""))] if d.get("text") else [],
                        [ToolCall(c["id"], c["name"], c["arguments"]) for c in d.get("tool_calls", [])],
                    )
                )
                pending.update({c["id"]: c["name"] for c in d.get("tool_calls", [])})
            elif e.type == TOOL_RESULT:
                pending.pop(d["call_id"], None)
                content = d.get("content", "")
                old = last_turn - turn_of.get(e.seq, last_turn) >= keep_turns
                if old and len(content) > policy.collapse_over_chars:
                    content = _stub(e)
                    collapsed.append(e.seq)
                imgs, note = images_for(e)
                if note:
                    content = f"{content}\n{note}"
                msgs.append(
                    Message(
                        "tool",
                        [TextPart(content), *imgs],
                        tool_call_id=d["call_id"],
                        tool_name=d.get("name"),
                        is_error=bool(d.get("is_error")),
                    )
                )
        flush()
        out = [Message.system("\n\n".join(system))] if system else []
        return out + msgs, collapsed

    keep = 1 if aggressive else policy.keep_recent_turns
    msgs, collapsed = assemble(keep)
    if not aggressive and estimate_tokens(msgs) > policy.context_window * policy.proactive_ratio:
        msgs, collapsed = assemble(1)
    if aggressive and estimate_tokens(msgs) > policy.context_window * policy.proactive_ratio:
        msgs, collapsed = assemble(0)
    return msgs, collapsed
