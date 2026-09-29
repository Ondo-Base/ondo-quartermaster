"""The agent loop. Hand-rolled, async, and small on purpose.

Each turn: check the kill switch and the budget, rebuild context from the log,
call the model, log the response, run the tool calls (independent ones in
parallel), fence and screen what comes back, log it. Stop when the model ends
its turn without calling a tool, when the budget runs out, or when the broker
says stop.

Everything the loop knows is in the log. There is no other state to lose, which
is what makes resume, fork and replay free.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from ..blobs import BlobStore
from ..log import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    CONTEXT_COLLAPSED,
    CONTEXT_INJECTION,
    MODEL_REQUEST,
    MODEL_RESPONSE,
    OPERATION_FINISHED,
    OPERATION_STARTED,
    PERMISSION_DENIED,
    RUN_FINISHED,
    RUN_RESTORED,
    RUN_STARTED,
    RUN_STOPPED,
    SCREENING,
    STEP,
    SYSTEM_PROMPT,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    EventLog,
)
from ..models.gateway import ModelClient
from ..models.types import ContextLengthError, ModelError, ToolCall
from ..permissions import PermissionBroker, PermissionDenied, RunStopped
from ..screening import Screener, fence
from ..tools.spec import Image, ToolContext, ToolResult, ToolSpec
from . import context as ctx
from .prompts import environment_block, system_prompt


@dataclass
class Budget:
    max_steps: int = 40
    max_tokens: int = 1_000_000


@dataclass
class RunResult:
    run_id: str
    status: str  # "finished" | "stopped" | "error"
    answer: str = ""
    reason: str = ""
    steps: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class Harness:
    def __init__(
        self,
        *,
        model: ModelClient,
        tools: list[ToolSpec],
        log: EventLog,
        broker: PermissionBroker,
        gates,
        approvals,
        screener: Screener | None,
        budget: Budget | None = None,
        config: dict[str, Any] | None = None,
        user: str = "local-user",
        services: dict[str, Any] | None = None,
    ):
        self.model = model
        self.tools = {t.name: t for t in tools}
        self.tool_list = tools
        self.log = log
        self.broker = broker
        self.gates = gates
        self.approvals = approvals
        self.screener = screener
        self.budget = budget or Budget()
        self.config = config or {}
        self.user = user
        # Long-lived things tools may use (the browser session). Not state: the log is.
        self.services = services or {}
        p = model.profile
        self.policy = ctx.ContextPolicy(
            context_window=p.context_window,
            vision=p.supports_vision,
            max_images=p.max_images_per_request or 20,
        )
        # Screenshots, by hash, beside the log.
        self.blobs = BlobStore.beside(log.path)
        self._collapsed_logged: set[int] = set()
        self.tool_ctx = ToolContext(
            run=self, broker=broker, log=log, gates=gates, approvals=approvals, screener=screener, config=self.config
        )
        broker.on_change(self._on_grant_change)

    # -- state derived from the log ---------------------------------------------

    @property
    def tainted(self) -> bool:
        return any(e.type == SCREENING and e.data.get("flagged") for e in self.log)

    def _usage(self) -> dict[str, int]:
        tot = {"input_tokens": 0, "output_tokens": 0}
        for e in self.log.of_type(MODEL_RESPONSE):
            u = e.data.get("usage", {})
            tot["input_tokens"] += u.get("input_tokens", 0)
            tot["output_tokens"] += u.get("output_tokens", 0)
        return tot

    def _on_grant_change(self, kind: str, data: dict[str, Any]) -> None:
        from ..log import GRANT_CHANGED, RUN_PAUSED, RUN_RESUMED

        source = f"broker:{data.get('by', 'user')}"
        if kind == "paused":
            self.log.append(RUN_PAUSED, source, data)
        elif kind == "resumed":
            self.log.append(RUN_RESUMED, source, data)
        elif kind != "stopped":  # a stop is logged once, as run_stopped
            self.log.append(GRANT_CHANGED, source, {"change": kind, **data})

    # -- entry points -----------------------------------------------------------

    def start(self, request: str) -> None:
        """Write the opening events. Separate from ``run`` so a fork can skip it."""
        p = self.model.profile
        self.log.append(
            RUN_STARTED,
            "harness",
            {
                "request": request,
                "user": self.user,
                "model": p.model,
                "profile": p.name,
                "tools": list(self.tools),
                "grants": self.broker.snapshot(),
            },
        )
        self.log.append(SYSTEM_PROMPT, "harness.prompt.base", {"text": system_prompt()})
        if p.prompt_overlay:
            self.log.append(SYSTEM_PROMPT, f"profile:{p.name}", {"text": p.prompt_overlay})
        self.log.append(
            CONTEXT_INJECTION,
            "harness.environment",
            {
                "text": environment_block(
                    run_id=self.log.run_id, user=self.user, grants=self.broker.snapshot(), tools=list(self.tools)
                ),
            },
        )
        self.log.append(USER_MESSAGE, f"user:{self.user}", {"text": request})

    async def run(self, request: str | None = None) -> RunResult:
        if request is not None and not self.log.of_type(RUN_STARTED):
            self.start(request)
        # A restored run keeps counting against the same budget.
        steps = len(self.log.of_type(MODEL_RESPONSE))
        try:
            while True:
                await self.broker.wait_resumed()
                self.broker.ensure_running()
                usage = self._usage()
                if steps >= self.budget.max_steps:
                    return self._stop(f"step budget of {self.budget.max_steps} reached", "harness.budget", steps)
                if usage["input_tokens"] + usage["output_tokens"] >= self.budget.max_tokens:
                    return self._stop(f"token budget of {self.budget.max_tokens:,} reached", "harness.budget", steps)

                response = await self._call_model()
                steps += 1
                if not response.tool_calls:
                    self.log.append(RUN_FINISHED, "harness", {"answer": response.text, "steps": steps})
                    return RunResult(self.log.run_id, "finished", response.text, steps=steps, usage=self._usage())
                await self._run_tools(response.tool_calls)
        except RunStopped as s:
            return self._stop(s.reason, f"broker:{s.by or 'user'}", steps)
        except ModelError as e:
            self.log.append(RUN_STOPPED, "harness.model", {"reason": f"model error: {e}", "status": "error"})
            return RunResult(self.log.run_id, "error", reason=str(e), steps=steps, usage=self._usage())

    async def resume(self, reason: str = "the desktop agent restarted") -> RunResult:
        """Carry on a run a previous agent process was running, from its log.

        The calls that process was serving when it ended get a result that says
        what can honestly be said: a long operation is waited for again by its
        handle; a call that finished is reported as done; a read that did not
        is safe to repeat; a change that was still waiting for approval did not
        happen; anything else may or may not have happened, and the model is
        told to check before trying again."""
        if self.log.of_type(RUN_FINISHED, RUN_STOPPED):
            raise ValueError(f"run {self.log.run_id} has already ended")
        responses = self.log.of_type(MODEL_RESPONSE)
        pending: list[dict[str, Any]] = []
        if responses:
            last = responses[-1]
            answered = {e.data["call_id"] for e in self.log.of_type(TOOL_RESULT) if e.seq > last.seq}
            pending = [c for c in last.data.get("tool_calls", []) if c["id"] not in answered]
        self.log.append(RUN_RESTORED, "harness", {"reason": reason, "interrupted": [c["id"] for c in pending]})
        try:
            for c in pending:
                call = ToolCall(c["id"], c["name"], c.get("arguments", {}))
                self._log_result(call, await self._restore_call(call))
        except RunStopped as s:
            return self._stop(s.reason, f"broker:{s.by or 'user'}", len(responses))
        return await self.run()

    async def _restore_call(self, c: ToolCall) -> ToolResult:
        spec = self.tools.get(c.name)
        mine = [e for e in self.log if e.data.get("call_id") == c.id]
        started = [e for e in mine if e.type == OPERATION_STARTED]
        finished = {e.data.get("handle") for e in mine if e.type == OPERATION_FINISHED}
        open_ops = [e for e in started if e.data.get("handle") not in finished]
        svc = self.services.get("connectors")
        if open_ops and svc is not None and spec is not None:
            op = open_ops[-1].data
            return await self._run_tool(c, resume=lambda ctx: svc.resume_operation(op, ctx))
        steps = [e for e in mine if e.type == STEP and e.data.get("status") in ("done", "error")]
        changes = spec is not None and spec.max_effect != "read"
        if steps:
            d = steps[-1].data
            what = d.get("title", c.name)
            if d.get("status") == "error":
                return ToolResult(f"This failed before the agent restarted: {what}.", is_error=True)
            return ToolResult(
                f"This finished before the agent restarted, but its full result was lost: {what}."
                + (" It was done: do not do it again." if changes else " Run it again if you need the details.")
            )
        if not changes:
            return ToolResult(
                "Interrupted by an agent restart before it finished. Nothing was changed; run it again if you "
                "still need it.",
                is_error=True,
            )
        # A yes to using the connector says nothing about whether the change was approved.
        requested = {e.data.get("id") for e in mine if e.type == APPROVAL_REQUESTED and e.data.get("kind") != "consent"}
        resolved = {
            e.data.get("approval_id"): e.data.get("approved")
            for e in self.log.of_type(APPROVAL_RESOLVED)
            if e.data.get("approval_id") in requested
        }
        if requested and not any(resolved.values()):
            return ToolResult(
                "Interrupted by an agent restart while waiting for approval. Nothing was changed; ask again if it "
                "is still needed.",
                is_error=True,
            )
        return ToolResult(
            "Interrupted by an agent restart after it started. It may or may not have happened: check the current "
            "state before doing it again.",
            is_error=True,
        )

    def _stop(self, reason: str, source: str, steps: int) -> RunResult:
        self.log.append(RUN_STOPPED, source, {"reason": reason, "status": "stopped", "steps": steps})
        return RunResult(self.log.run_id, "stopped", reason=reason, steps=steps, usage=self._usage())

    # -- model --------------------------------------------------------------------

    async def _call_model(self):
        tools = self.tool_list
        for attempt in (0, 1):
            messages, collapsed = ctx.build(self.log.events, self.policy, aggressive=attempt == 1, blobs=self.blobs)
            new = [s for s in collapsed if s not in self._collapsed_logged]
            if new:
                self._collapsed_logged.update(new)
                self.log.append(CONTEXT_COLLAPSED, "harness.context", {"seqs": new, "aggressive": attempt == 1})
            self.log.append(
                MODEL_REQUEST,
                f"model:{self.model.profile.name}",
                {
                    "profile": self.model.profile.name,
                    "model": self.model.profile.model,
                    "messages": len(messages),
                    "estimated_tokens": ctx.estimate_tokens(messages),
                },
            )
            # Race the call against the kill switch: a stop must not wait for a
            # slow provider.
            call = asyncio.ensure_future(self.model.complete(messages, tools))
            stop = asyncio.ensure_future(self.broker.wait_stopped())
            done, _ = await asyncio.wait({call, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                call.cancel()
                raise stop.result()
            stop.cancel()
            try:
                r = call.result()
            except ContextLengthError:
                if attempt == 0:
                    continue
                raise
            served = self.model.last_profile
            self.log.append(
                MODEL_RESPONSE,
                f"model:{served.name}",
                {
                    "text": r.text,
                    "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in r.tool_calls],
                    "stop_reason": r.stop_reason,
                    "raw_stop_reason": r.raw_stop_reason,
                    "usage": {
                        "input_tokens": r.usage.input_tokens,
                        "output_tokens": r.usage.output_tokens,
                        "cached_input_tokens": r.usage.cached_input_tokens,
                    },
                    "model": r.model or served.model,
                    "profile": served.name,
                    **({"reasoning": r.reasoning} if r.reasoning else {}),
                },
            )
            return r
        raise AssertionError("unreachable")

    # -- tools --------------------------------------------------------------------

    async def _run_tools(self, calls: list[ToolCall]) -> None:
        parallel = [c for c in calls if c.name in self.tools and self.tools[c.name].parallel_safe]
        serial = [c for c in calls if c not in parallel]
        results: dict[str, ToolResult] = {}

        async def one(c: ToolCall) -> None:
            results[c.id] = await self._run_tool(c)

        if parallel:
            await asyncio.gather(*(one(c) for c in parallel))
        for c in serial:
            await one(c)
        # Results go into the log in the order the model asked for them.
        for c in calls:
            self._log_result(c, results[c.id])
        self.broker.ensure_running()

    async def _run_tool(self, c: ToolCall, resume=None) -> ToolResult:
        """Serve one call. ``resume`` serves it instead of the tool's handler, for a
        call a previous agent process started (its tool_call is already logged)."""
        spec = self.tools.get(c.name)
        if resume is None:
            self.log.append(
                TOOL_CALL,
                f"model:{self.model.last_profile.name}",
                {
                    "call_id": c.id,
                    "name": c.name,
                    "arguments": c.arguments,
                },
            )
        if spec is None:
            return ToolResult(f"Unknown tool {c.name!r}. Available: {', '.join(self.tools)}.", is_error=True)
        if "__unparsed_arguments__" in c.arguments:
            return ToolResult(
                "The tool arguments were not valid JSON. Send them again as a JSON object.", is_error=True
            )
        self.log.append(
            STEP,
            "harness.steps",
            {"call_id": c.id, "title": spec.step_title(c.arguments), "tool": c.name, "status": "running"},
        )
        try:
            await self.broker.wait_resumed()
            self.broker.ensure(spec.grant)
            assert spec.handler is not None
            call_ctx = self.tool_ctx.for_call(c.id)
            work = resume(call_ctx) if resume is not None else spec.handler(c.arguments, call_ctx)
            task = asyncio.ensure_future(work)
            stop = asyncio.ensure_future(self.broker.wait_stopped())
            done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                task.cancel()
                raise stop.result()
            stop.cancel()
            result = task.result()
        except RunStopped:
            raise
        except PermissionDenied as p:
            self.log.append(
                PERMISSION_DENIED,
                "broker",
                {
                    "call_id": c.id,
                    "tool": c.name,
                    "kind": p.kind,
                    "reason": p.reason,
                    "target": p.target,
                },
            )
            result = ToolResult(
                f"Permission denied: {p}. This is final for this run.", is_error=True, detail={"permission": p.reason}
            )
        except Exception as e:  # a tool bug becomes a readable error, not a dead run
            result = ToolResult(f"{type(e).__name__}: {e}", is_error=True)

        if result.untrusted_origin and not result.is_error:
            screen = None
            if self.screener is not None:
                screen = await self.screener.screen(result.content, result.untrusted_origin, self.log)
            result = ToolResult(
                fence(result.content, result.untrusted_origin, screen),
                result.is_error,
                {
                    **result.detail,
                    "screening": None
                    if screen is None
                    else {"flagged": screen.flagged, "probability": screen.probability},
                },
                result.untrusted_origin,
                result.images,
            )
        self.log.append(
            STEP,
            "harness.steps",
            {
                "call_id": c.id,
                "title": spec.step_title(c.arguments),
                "tool": c.name,
                "status": "error" if result.is_error else "done",
                "detail": _step_detail(result),
            },
        )
        return result

    def store_images(self, images: list[Image]) -> list[dict[str, Any]]:
        """Write screenshots to the blob store; return the references the log keeps."""
        return [
            {
                "sha256": self.blobs.put(im.data),
                "media_type": im.media_type,
                "width": im.width,
                "height": im.height,
                "label": im.label,
            }
            for im in images
        ]

    def inject(self, text: str, source: str, images: list[Image] | None = None) -> None:
        """Put something into the model's context that no tool call asked for (what
        is on screen when the user asks about it). The source says who put it there."""
        data: dict[str, Any] = {"text": text}
        if images:
            data["images"] = self.store_images(images)
        self.log.append(CONTEXT_INJECTION, source, data)

    def _log_result(self, c: ToolCall, r: ToolResult) -> None:
        detail = dict(r.detail)
        if r.images:
            detail["images"] = self.store_images(r.images)
        self.log.append(
            TOOL_RESULT,
            f"tool:{c.name}",
            {
                "call_id": c.id,
                "name": c.name,
                "content": r.content,
                "is_error": r.is_error,
                "origin": r.untrusted_origin,
                "detail": _jsonable(detail),
            },
        )


def _step_detail(r: ToolResult) -> dict[str, Any]:
    d = {
        k: v
        for k, v in r.detail.items()
        if k in ("summary", "path", "url", "files", "values", "screening", "approval", "permission", "effects")
    }
    if r.is_error:
        d["error"] = r.content[:300]
    return _jsonable(d)


def _jsonable(d: Any) -> Any:
    return json.loads(json.dumps(d, default=str))
