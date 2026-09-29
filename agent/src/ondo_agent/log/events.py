"""Event vocabulary for the append-only session log.

Everything the model saw is one event stream. Nothing below is derived state:
the trajectory view, the audit export and replay all read these events.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    # run lifecycle
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    STEP_STARTED = "step_started"

    # context: every token in context arrives through one of these
    SYSTEM_PROMPT = "system_prompt"
    USER_MESSAGE = "user_message"
    CONTEXT_INJECTION = "context_injection"
    CONTEXT_COLLAPSED = "context_collapsed"
    CONTEXT_PRUNED = "context_pruned"

    # model layer
    MODEL_REQUEST = "model_request"
    MODEL_REASONING = "model_reasoning"
    MODEL_MESSAGE = "model_message"
    MODEL_REFUSAL = "model_refusal"

    # tools
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    # decision layer
    DECISION = "decision"

    # safety
    GATE_RAISED = "gate_raised"
    GATE_RESOLVED = "gate_resolved"
    POLICY_BLOCK = "policy_block"
    GRANT_CHANGED = "grant_changed"
    KILL_SWITCH = "kill_switch"

    # delegation
    SUBAGENT_SCHEDULED = "subagent_scheduled"
    SUBAGENT_FINISHED = "subagent_finished"

    ERROR = "error"


#: Events that carry text the model is allowed to treat as instructions.
TRUSTED_SOURCES = frozenset({"harness.system_prompt", "user", "control_plane.policy"})


@dataclass(slots=True)
class Event:
    """One immutable entry in the session log.

    ``source`` names the component that produced the event. The trajectory view
    shows it verbatim, which is what makes "why was this in context?" answerable
    — and it is the field the security review asks about.
    """

    type: EventType
    source: str
    run_id: str
    seq: int = -1
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    #: True when the payload contains file contents, screen text or MCP server
    #: text. Untrusted payloads are fenced as data by the context builder and
    #: never merged into instructions.
    untrusted: bool = False

    def to_json(self) -> str:
        d = asdict(self)
        d["type"] = str(self.type)
        return json.dumps(d, separators=(",", ":"), default=_fallback)

    @classmethod
    def from_json(cls, line: str) -> "Event":
        d = json.loads(line)
        d["type"] = EventType(d["type"])
        return cls(**d)


def _fallback(o: Any) -> Any:
    if isinstance(o, StrEnum):
        return str(o)
    if hasattr(o, "__dict__"):
        return vars(o)
    return repr(o)
