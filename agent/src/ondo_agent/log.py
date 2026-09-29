"""The append-only session log: the single source of truth for a run.

Everything the model saw, everything it did, and every decision the harness made
about it is one event in one stream. Nothing else is state. The model context is
rebuilt from the log on every turn (see ``harness.context``), the web UI's
trajectory view reads the same events, the audit export is the same events, and
replay, fork and search all work on the stored file.

Every event carries a ``source``: the component that produced it. When an event
puts something into the model's context (a system prompt, a tool result, a
screening notice) the source says which component put it there. That is the
trajectory view the security review will ask for.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("ondo.agent")

# Event types. Keep this list closed: the UI and the audit export switch on it.
RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"
RUN_STOPPED = "run_stopped"
RUN_PAUSED = "run_paused"
RUN_RESUMED = "run_resumed"
SYSTEM_PROMPT = "system_prompt"
USER_MESSAGE = "user_message"
CONTEXT_INJECTION = "context_injection"
MODEL_REQUEST = "model_request"
MODEL_RESPONSE = "model_response"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
CONTEXT_COLLAPSED = "context_collapsed"
FILE_ACCESS = "file_access"
WINDOW_ACCESS = "window_access"
CONNECTOR_ACCESS = "connector_access"
DIFF_PROPOSED = "diff_proposed"
DECISION = "decision"
SCREENING = "screening"
GATE = "gate"
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_RESOLVED = "approval_resolved"
PERMISSION_DENIED = "permission_denied"
GRANT_CHANGED = "grant_changed"
STEP = "step"

# Events whose content enters the model's context. The trajectory view marks
# these, and ``harness.context`` rebuilds the conversation from exactly these.
CONTEXT_EVENTS = frozenset({SYSTEM_PROMPT, USER_MESSAGE, CONTEXT_INJECTION, MODEL_RESPONSE, TOOL_RESULT})


@dataclass
class Event:
    run_id: str
    seq: int
    type: str
    source: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    # Hash chain: each event commits to the previous one, so an exported log is
    # tamper-evident without trusting the store it came from.
    prev_hash: str = ""
    hash: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        return cls(**d)

    @property
    def enters_context(self) -> bool:
        return self.type in CONTEXT_EVENTS


def _hash_event(e: Event) -> str:
    body = json.dumps(
        {
            "run_id": e.run_id,
            "seq": e.seq,
            "type": e.type,
            "source": e.source,
            "data": e.data,
            "ts": e.ts,
            "id": e.id,
            "prev_hash": e.prev_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


Subscriber = Callable[[Event], Any]


class EventLog:
    """One run's append-only event stream, persisted as JSON lines.

    Appends are synchronous and flushed before ``append`` returns; an event the
    caller has seen is an event on disk. Subscribers are notified after the
    write, so a crash between the two loses a notification, never a record.
    """

    def __init__(self, path: Path, run_id: str, events: list[Event] | None = None):
        self.path = Path(path)
        self.run_id = run_id
        self._events: list[Event] = list(events or [])
        self._subscribers: list[Subscriber] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- construction -------------------------------------------------------

    @classmethod
    def create(cls, root: Path, run_id: str | None = None) -> EventLog:
        run_id = run_id or new_run_id()
        path = Path(root) / f"{run_id}.jsonl"
        if path.exists():
            raise FileExistsError(f"run {run_id} already has a log at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return cls(path, run_id)

    @classmethod
    def open(cls, path: Path) -> EventLog:
        path = Path(path)
        events = list(read_events(path))
        run_id = events[0].run_id if events else path.stem
        return cls(path, run_id, events)

    # -- writing ------------------------------------------------------------

    def append(self, type: str, source: str, data: dict[str, Any] | None = None) -> Event:
        prev = self._events[-1].hash if self._events else ""
        e = Event(
            run_id=self.run_id,
            seq=len(self._events),
            type=type,
            source=source,
            data=data or {},
            prev_hash=prev,
        )
        e.hash = _hash_event(e)
        line = e.to_json()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._events.append(e)
        for sub in list(self._subscribers):
            try:
                res = sub(e)
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception:  # a broken subscriber must never break the log
                _log.warning("event log subscriber failed", exc_info=True)
        return e

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)

    # -- reading ------------------------------------------------------------

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def of_type(self, *types: str) -> list[Event]:
        return [e for e in self._events if e.type in types]

    def search(self, text: str) -> list[Event]:
        """Plain substring search over every event. Deterministic; no index."""
        needle = text.lower()
        return [e for e in self._events if needle in json.dumps(e.data, ensure_ascii=False).lower()]

    def verify_chain(self) -> bool:
        return verify_chain(self._events)

    # -- fork ---------------------------------------------------------------

    def fork(self, root: Path, upto_seq: int, new_run_id: str | None = None) -> EventLog:
        """Copy events [0, upto_seq] into a new run.

        The fork keeps every context event verbatim, so a replay of the fork with
        one thing changed (a tool, a model, a profile) sees exactly the history
        the original saw up to that point.
        """
        child = EventLog.create(root, new_run_id)
        for e in self._events[: upto_seq + 1]:
            data = dict(e.data)
            if e.type == RUN_STARTED:
                data = {**data, "forked_from": {"run_id": self.run_id, "seq": upto_seq}}
            child.append(e.type, e.source, data)
        # Screenshots the copied events refer to come along, so the fork's context
        # is the same pixels, not a stub.
        from .blobs import BlobStore

        BlobStore.beside(self.path).copy_to(BlobStore.beside(child.path))
        return child


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def read_events(path: Path) -> Iterable[Event]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield Event.from_dict(json.loads(line))


def verify_chain(events: list[Event]) -> bool:
    prev = ""
    for e in events:
        if e.prev_hash != prev or _hash_event(e) != e.hash:
            return False
        prev = e.hash
    return True


def list_runs(root: Path) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
