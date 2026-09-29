"""The one path every effectful tool goes through: gate, then (maybe) approval.

Tools describe what they are about to do as a ``ProposedAction`` with the exact
values. The gate decides on effect; if a person must decide, the request shows
them those values, and the answer is logged with who gave it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..approvals import ApprovalRequest, ApprovalValue
from ..gates import GateDecision, ProposedAction
from ..log import APPROVAL_REQUESTED, APPROVAL_RESOLVED
from ..tools.spec import ToolContext


@dataclass
class EffectOutcome:
    allowed: bool
    asked: bool
    decision: GateDecision
    by: str = ""
    note: str = ""
    approval_id: str | None = None


async def gate_and_approve(
    ctx: ToolContext,
    action: ProposedAction,
    *,
    title: str,
    summary: str,
    values: list[ApprovalValue] | None = None,
    diff: str | None = None,
    always_ask: bool = False,
    extra_effects: list[str] | None = None,
) -> EffectOutcome:
    decision = await ctx.gates.evaluate(action, log=ctx.log, tainted=ctx.run.tainted)
    if not (always_ask or decision.required):
        return EffectOutcome(True, False, decision)
    effects = list(decision.effects) + [e for e in (extra_effects or []) if e not in decision.effects]
    req = ApprovalRequest(
        run_id=ctx.log.run_id,
        title=title,
        summary=summary,
        effects=effects,
        values=values or [],
        diff=diff,
        tool=action.tool,
        arguments=action.arguments,
        call_id=getattr(ctx, "call_id", ""),
    )
    ctx.log.append(APPROVAL_REQUESTED, "gates", req.to_dict())
    res = await ctx.approvals.request(req)
    ctx.log.append(
        APPROVAL_RESOLVED,
        f"user:{res.by}",
        {
            "approval_id": req.id,
            "approved": res.approved,
            "by": res.by,
            "note": res.note,
        },
    )
    return EffectOutcome(res.approved, True, decision, res.by, res.note, req.id)
