"""The append-only session log: the single source of truth for a run.

The log is JSONL on disk plus an in-process fan-out to subscribers (the control
plane streams from those). It supports the three things the plan asks for:
search, replay and fork.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from .events import Event, EventType

Subscriber = Callable[[Event], None]


class SessionLog:
    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path
        self._events: list[Event] = []
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")

    # -- writing ----------------------------------------------------------
    def append(
        self,
        type: EventType,
        source: str,
        payload: dict[str, Any] | None = None,
        *,
        parent_id: str | None = None,
        untrusted: bool = False,
    ) -> Event:
        with self._lock:
            event = Event(
                type=type,
                source=source,
                run_id=self.run_id,
                seq=len(self._events),
                payload=payload or {},
                parent_id=parent_id,
                untrusted=untrusted,
            )
            self._events.append(event)
            if self._fh is not None:
                self._fh.write(event.to_json() + "\n")
                self._fh.flush()
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub(event)
            except Exception:  # a broken UI subscriber must not stop a run
                pass
        return event

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # -- reading ----------------------------------------------------------
    def __iter__(self) -> Iterator[Event]:
        with self._lock:
            return iter(list(self._events))

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def search(
        self,
        *,
        types: Iterable[EventType] | None = None,
        source: str | None = None,
        text: str | None = None,
    ) -> list[Event]:
        wanted = set(types) if types else None
        needle = text.lower() if text else None
        out = []
        for e in self.events:
            if wanted and e.type not in wanted:
                continue
            if source and not e.source.startswith(source):
                continue
            if needle and needle not in e.to_json().lower():
                continue
            out.append(e)
        return out

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def queue(self, loop: asyncio.AbstractEventLoop | None = None) -> tuple["asyncio.Queue[Event]", Callable[[], None]]:
        """Subscribe and deliver onto an asyncio queue (for SSE bridging)."""
        loop = loop or asyncio.get_event_loop()
        q: asyncio.Queue[Event] = asyncio.Queue()
        unsub = self.subscribe(lambda e: loop.call_soon_threadsafe(q.put_nowait, e))
        return q, unsub

    # -- replay and fork --------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "SessionLog":
        events = [Event.from_json(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        run_id = events[0].run_id if events else path.stem
        log = cls(run_id=run_id, path=None)
        log._events = events
        return log

    def fork(self, at_seq: int, new_run_id: str, path: Path | None = None) -> "SessionLog":
        """A new log carrying history up to ``at_seq`` inclusive.

        This is how a run is re-run with one tool changed or one model swapped,
        without asking a customer to reproduce anything.
        """
        forked = SessionLog(run_id=new_run_id, path=path)
        for e in self.events:
            if e.seq > at_seq:
                break
            forked.append(e.type, e.source, dict(e.payload), parent_id=e.parent_id, untrusted=e.untrusted)
        return forked

    # -- views ------------------------------------------------------------
    def trajectory(self) -> list[dict[str, Any]]:
        """Flat rows for the TaskRun trajectory view.

        Every row names the component that put the content in context, which is
        the audit trail, not a debugging luxury.
        """
        rows = []
        for e in self.events:
            rows.append(
                {
                    "seq": e.seq,
                    "ts": e.ts,
                    "type": str(e.type),
                    "source": e.source,
                    "untrusted": e.untrusted,
                    "summary": _summarise(e),
                    "payload": e.payload,
                }
            )
        return rows


def _summarise(e: Event) -> str:
    p = e.payload
    match e.type:
        case EventType.USER_MESSAGE | EventType.MODEL_MESSAGE:
            return _clip(p.get("text", ""))
        case EventType.TOOL_CALL:
            return f"{p.get('name')}({_clip(str(p.get('arguments', {})), 80)})"
        case EventType.TOOL_RESULT:
            return ("error: " if p.get("is_error") else "") + _clip(str(p.get("content", "")), 120)
        case EventType.DECISION:
            return f"{p.get('question')} -> {p.get('value')} (p={p.get('probability')})"
        case EventType.GATE_RAISED:
            return f"{p.get('effect')}: {p.get('reason', '')}"
        case EventType.MODEL_REQUEST:
            return f"{p.get('model')} · {p.get('message_count')} msgs · {p.get('input_tokens_est')} tok est"
        case EventType.CONTEXT_INJECTION:
            return f"{p.get('label', '')} ({p.get('tokens_est', 0)} tok est)"
        case _:
            return _clip(str(p), 120) if p else ""


def _clip(s: str, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"
