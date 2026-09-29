"""Approval gates, decided on effect rather than on tool.

The gate is not "uses the keyboard". It is: submits to a system of record, sends
externally, overwrites a shared file, moves money.

Order of authority, which never changes:

1. **Deterministic rules decide first** — by tool, URL, path, app or element,
   from configuration. A ``require`` rule raises a gate. An ``allow`` rule marks
   an effect as enumerated-and-fine for that target.
2. **The decision model is a second net** for what nobody enumerated. It is asked
   only about effects no rule covered. Thresholds sit low, so uncertainty
   escalates to a person and never proceeds.
3. **It may never lower a gate a rule raised.** One direction only.
4. **A tainted run** (screening flagged something it read) gates every effect.

Every gate decision is logged with its probabilities, so thresholds are tuned
from evidence (``decision/calibrate.py``) rather than argued about.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .decision.interface import Boolean
from .decision.logged import LoggedDecisionModel
from .log import GATE, EventLog
from .permissions import glob_to_regex

EFFECTS = ("submits_to_system_of_record", "sends_externally", "overwrites_shared_file", "moves_money")

QUESTIONS = {
    "submits_to_system_of_record": "Will this action submit, save or commit data into a business system of record (billing, ERP, CRM, ledger, HR, ticketing)?",
    "sends_externally": "Will this action send a message, file or data to someone outside the user's own machine (email, chat, upload, share)?",
    "overwrites_shared_file": "Will this action overwrite or modify an existing file that other people use (a shared drive, team site or document store)?",
    "moves_money": "Could this action move money or commit the organisation to a payment (payment, transfer, refund, purchase order, invoice approval)?",
}

# Before calibration has run, every threshold is conservative.
DEFAULT_THRESHOLD = 0.2


@dataclass
class ProposedAction:
    tool: str
    arguments: dict[str, Any]
    description: str
    max_effect: str = "read"
    url: str | None = None
    path: str | None = None
    app: str | None = None
    element: str | None = None
    # True when the action writes over something that already exists.
    overwrites: bool = False
    # Effects the tool's own declaration says this action has (a connector's
    # create_ticket submits; a public reply sends externally). Configuration, so
    # they gate deterministically, like a require rule.
    declared_effects: list[str] = field(default_factory=list)

    def state(self) -> str:
        parts = [f"Action: {self.description}", f"Tool: {self.tool}"]
        for k in ("url", "path", "app", "element"):
            v = getattr(self, k)
            if v:
                parts.append(f"{k.capitalize()}: {v}")
        if self.overwrites:
            parts.append("Overwrites an existing target.")
        args = json.dumps(self.arguments, ensure_ascii=False)[:1500]
        parts.append(f"Arguments: {args}")
        return "\n".join(parts)


@dataclass
class GateRule:
    effect: str
    action: Literal["require", "allow"] = "require"
    name: str = ""
    tool: str | None = None
    url: str | None = None
    path: str | None = None
    app: str | None = None
    element: str | None = None  # regex, case-insensitive

    def matches(self, a: ProposedAction) -> bool:
        if self.tool and not fnmatch.fnmatchcase(a.tool, self.tool):
            return False
        if self.url and not (a.url and fnmatch.fnmatch(a.url, self.url)):
            return False
        if self.path and not (a.path and glob_to_regex(self.path).match(Path(a.path).as_posix())):
            return False
        if self.app and not (a.app and self.app.lower() in a.app.lower()):
            return False
        if self.element and not (a.element and re.search(self.element, a.element, re.IGNORECASE)):
            return False
        return True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GateRule:
        return cls(**d)


@dataclass
class EffectVerdict:
    gated: bool
    source: str
    probability: float | None = None
    threshold: float | None = None


@dataclass
class GateDecision:
    required: bool
    effects: list[str]
    by_effect: dict[str, EffectVerdict] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "effects": self.effects,
            "by_effect": {k: asdict(v) for k, v in self.by_effect.items()},
        }


class GateKeeper:
    def __init__(
        self,
        rules: list[GateRule] | None = None,
        decision_model: LoggedDecisionModel | None = None,
        thresholds: dict[str, float] | None = None,
    ):
        self.rules = rules or []
        self.model = decision_model
        self.thresholds = {e: DEFAULT_THRESHOLD for e in EFFECTS}
        self.thresholds.update(thresholds or {})

    @classmethod
    def load_thresholds(cls, path: Path) -> dict[str, float]:
        data = json.loads(Path(path).read_text())
        return {k: float(v) for k, v in data.get("thresholds", {}).items()}

    async def evaluate(
        self, action: ProposedAction, *, log: EventLog | None = None, tainted: bool = False
    ) -> GateDecision:
        verdicts: dict[str, EffectVerdict] = {}
        if action.max_effect == "read":
            d = GateDecision(False, [], {})
            return d

        # 1. Deterministic rules. ``require`` beats ``allow`` for the same effect.
        for effect in EFFECTS:
            req = [r for r in self.rules if r.effect == effect and r.action == "require" and r.matches(action)]
            if req:
                verdicts[effect] = EffectVerdict(True, f"rule:{req[0].name or 'unnamed'}")
                continue
            allow = [r for r in self.rules if r.effect == effect and r.action == "allow" and r.matches(action)]
            if allow:
                verdicts[effect] = EffectVerdict(False, f"allow:{allow[0].name or 'unnamed'}")
            elif effect in action.declared_effects:
                # Declared by the tool: gated unless an explicit allow rule says this
                # target is fine. An allow rule is the operator's decision to make.
                verdicts[effect] = EffectVerdict(True, f"declared:{action.tool}")

        # 2. The decision model, only for effects no rule enumerated.
        open_effects = [e for e in EFFECTS if e not in verdicts]
        if open_effects and self.model is not None:
            qs = [Boolean(id=e, prompt=QUESTIONS[e], key=f"gate.{e}") for e in open_effects]
            answers = await self.model.ask(action.state(), qs, log=log, purpose="gate")
            for a in answers:
                t = self.thresholds[a.question_id]
                verdicts[a.question_id] = EffectVerdict(a.probability >= t, "decision", round(a.probability, 4), t)
        for e in open_effects:
            # No model configured: nothing enumerated this effect, so a person decides.
            verdicts.setdefault(e, EffectVerdict(True, "no_decision_model"))

        # 3. A tainted run gates everything with an effect.
        if tainted:
            for e, v in verdicts.items():
                if not v.gated:
                    verdicts[e] = EffectVerdict(True, "tainted_run", v.probability, v.threshold)

        gated = [e for e in EFFECTS if verdicts[e].gated]
        decision = GateDecision(bool(gated), gated, verdicts)
        if log is not None:
            log.append(
                GATE,
                "gates",
                {
                    "tool": action.tool,
                    "description": action.description,
                    "url": action.url,
                    "path": action.path,
                    "element": action.element,
                    "tainted": tainted,
                    **decision.to_dict(),
                },
            )
        return decision


def load_rules(items: list[dict[str, Any]] | None) -> list[GateRule]:
    return [GateRule.from_dict(d) for d in (items or [])]
