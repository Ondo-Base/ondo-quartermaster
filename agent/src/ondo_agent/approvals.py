"""Approval requests and the providers that resolve them.

A gate stops the run and shows a person the exact values before they land. The
harness does not care who answers: a person in the web UI (through the control
plane), the terminal, or a test. It only cares that the answer is recorded with
who gave it.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class ApprovalValue:
    label: str
    after: str
    before: str | None = None
    flagged: bool = False


@dataclass
class ApprovalRequest:
    run_id: str
    title: str
    summary: str
    effects: list[str]
    values: list[ApprovalValue] = field(default_factory=list)
    diff: str | None = None
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    # "effect": a gate stopped an action. "consent": a connector is being used for
    # the first time and the person decides whether Ondo may use it at all.
    kind: str = "effect"
    connector: str | None = None
    id: str = field(default_factory=lambda: "apr_" + uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Resolution:
    approved: bool
    by: str
    note: str = ""


class ApprovalProvider(Protocol):
    async def request(self, req: ApprovalRequest) -> Resolution: ...


class QueueApprovals:
    """Pending approvals held as futures, resolved from elsewhere (UI, socket, test)."""

    def __init__(self) -> None:
        self.pending: dict[str, tuple[ApprovalRequest, asyncio.Future[Resolution]]] = {}
        self._listeners: list[Callable[[ApprovalRequest], Any]] = []

    def on_request(self, fn: Callable[[ApprovalRequest], Any]) -> None:
        self._listeners.append(fn)

    async def request(self, req: ApprovalRequest) -> Resolution:
        fut: asyncio.Future[Resolution] = asyncio.get_running_loop().create_future()
        self.pending[req.id] = (req, fut)
        for fn in self._listeners:
            r = fn(req)
            if asyncio.iscoroutine(r):
                await r
        try:
            return await fut
        finally:
            self.pending.pop(req.id, None)

    def resolve(self, approval_id: str, approved: bool, by: str, note: str = "") -> bool:
        item = self.pending.get(approval_id)
        if not item or item[1].done():
            return False
        item[1].set_result(Resolution(approved, by, note))
        return True


class AutoApprovals:
    """For tests and fixtures only. Decides by a fixed answer or a function."""

    def __init__(self, decide: bool | Callable[[ApprovalRequest], bool] = True, by: str = "auto"):
        self.decide = decide
        self.by = by
        self.seen: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> Resolution:
        self.seen.append(req)
        ok = self.decide(req) if callable(self.decide) else self.decide
        return Resolution(bool(ok), self.by)


class TerminalApprovals:
    """Asks on stdin. Used by ``ondo-agent run`` when no control plane is attached."""

    def __init__(self, user: str = "local-user"):
        self.user = user

    async def request(self, req: ApprovalRequest) -> Resolution:
        lines = [f"\n== Approval required: {req.title}", req.summary]
        for v in req.values:
            arrow = f"{v.before} -> {v.after}" if v.before is not None else v.after
            lines.append(f"   {v.label}: {arrow}{'  [FLAGGED]' if v.flagged else ''}")
        if req.diff:
            lines.append(req.diff)
        lines.append("Approve? [y/N] ")
        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()
        answer = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        return Resolution(answer.strip().lower() in ("y", "yes"), self.user)
