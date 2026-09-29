"""A deterministic baseline decision model.

Not a learned model and not a substitute for one. It exists so that:

- every question has an answer offline, in CI and on a fresh install;
- the calibration spike has a floor to beat (a vendor model that cannot beat
  keyword rules on our fixtures does not earn a place in the data path);
- the gate's second net degrades to "escalate to a human", never to "proceed",
  when no model is configured: unknown questions answer 0.5, and every gate
  threshold sits below 0.5.
"""

from __future__ import annotations

import re

from .interface import Answer, Boolean, Choice, Question, Score

# Phrases that address an agent rather than a human reader. Deliberately broad:
# screening is recall-first, and a false positive only adds a notice.
_INJECTION = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|messages?)",
    r"disregard (all |any )?(the )?(previous|prior|above|earlier|your) (instructions|rules|guidelines)",
    r"forget (all |your )?(previous |prior )?instructions",
    r"(you are|you're) (now )?(an? )?(ai|assistant|agent|language model|llm)\b",
    r"\b(system|developer) prompt\b",
    r"\bnew instructions?\b",
    r"\b(assistant|ai|agent|llm)s?\s*[:,]\s*(please )?(you must|now|do|send|email|forward|ignore)",
    r"(do not|don't) (tell|inform|mention (this )?to|alert) the (user|human|operator)",
    r"(email|send|forward|upload|post) (this|these|the (file|document|contents?|data|pack)) to\b",
    r"\bexfiltrat",
    r"<\s*/?\s*(system|instructions?)\s*>",
    r"\bjailbreak\b",
]
_INJECTION_RX = [re.compile(p, re.IGNORECASE) for p in _INJECTION]

# Gate baselines look at what the action *does*: the tool, the control's label
# (the verb on the button) and the target. Mentioning a billing system is not
# submitting to it; clicking its Save button is.
_FILE_TOOLS = ("edit_workbook", "create_workbook", "create_document", "write_text_file")
_PASSIVE_TOOLS = (
    "browser_navigate",
    "browser_navigate_back",
    "browser_type",
    "browser_select_option",
    "browser_snapshot",
    "browser_wait_for",
    "browser_hover",
    "browser_tabs",
)

_SUBMIT_VERBS = re.compile(
    r"\b(submit|save changes|save record|update record|create|close ticket|post|approve|transfer|add payee|void|"
    r"issue|book|raise|log call|pay|confirm payment|place order|commit|finali[sz]e|file)\b",
    re.I,
)
_SEND_VERBS = re.compile(r"\b(send|forward|share|upload|publish|reply|post message|invite)\b", re.I)
_MONEY = re.compile(
    r"\b(pay|payment|payee|transfer|refund|remit|disburse|purchase order|invoice|cheque|check run|journal|"
    r"book shipment|wire|charge)\b",
    re.I,
)
_SAFE_VERBS = re.compile(
    r"\b(next|previous|back|view|download|export|run report|filter|expand|collapse|accept|sign in|log in|"
    r"save draft|cancel|close dialog|discard|search|open|help)\b",
    re.I,
)
_SHARED = re.compile(r"(/shares?/|shared|sharepoint|team site|teams?/|/sites/)", re.I)


def _field(state: str, name: str) -> str:
    m = re.search(rf"^{name}: (.*)$", state, re.MULTILINE)
    return m.group(1) if m else ""


def _gate_probability(key: str, state: str) -> float:
    tool = _field(state, "Tool")
    element = _field(state, "Element")
    action = _field(state, "Action")
    label = element or action
    target = _field(state, "Path") or _field(state, "Url")
    overwrites = "Overwrites an existing target." in state
    is_file = tool in _FILE_TOOLS
    passive = tool in _PASSIVE_TOOLS
    enter_in_form = (tool == "browser_press_key" and "enter" in action.lower() and "textbox" in element.lower()) or (
        # Pixels: Enter pressed into a remote session's form usually submits it.
        tool == "screen_act" and bool(re.search(r"\b(enter|return)\b", element, re.I))
    )

    if key == "gate.submits_to_system_of_record":
        if is_file or passive:
            return 0.05
        if enter_in_form:
            return 0.65
        if _SUBMIT_VERBS.search(label) or re.search(r"submit", action, re.I):
            return 0.9 if not _SAFE_VERBS.search(action.split("dialog")[-1]) or "submit" in action.lower() else 0.35
        return 0.1 if not _SAFE_VERBS.search(label) else 0.04
    if key == "gate.sends_externally":
        if is_file or passive:
            return 0.04
        if _SEND_VERBS.search(label) or _SEND_VERBS.search(action.split(" on ")[0]):
            return 0.9 if "draft" not in label.lower() else 0.1
        return 0.05
    if key == "gate.overwrites_shared_file":
        if not is_file:
            return 0.03
        shared = bool(_SHARED.search(target) or _SHARED.search(action))
        replaces = overwrites or bool(re.search(r"\b(replace|overwrite|change|edit)\b", action, re.I))
        creates = bool(re.search(r"\bnew file\b|\bcreate\b", action, re.I)) and not overwrites
        if shared and replaces and not creates:
            return 0.9
        return 0.3 if shared else 0.05
    if key == "gate.moves_money":
        if passive:
            return 0.04
        money = bool(_MONEY.search(label) or _MONEY.search(action))
        if money and not _SAFE_VERBS.search(label):
            return 0.88
        if re.search(r"\b\d[\d,]*\.\d{2}\b|\b(GBP|USD|EUR)\b", action):
            return 0.6
        return 0.06
    return 0.5


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


class RulesDecisionModel:
    name = "rules-baseline"

    async def ask(self, state: str, questions: list[Question]) -> list[Answer]:
        return [self._one(state, q) for q in questions]

    def _one(self, state: str, q: Question) -> Answer:
        if isinstance(q, Boolean):
            p = self._boolean(state, q)
            return Answer(q.id, p >= 0.5, p, {"true": p, "false": 1 - p})
        if isinstance(q, Choice):
            return self._choice(state, q)
        if isinstance(q, Score):
            return Answer(q.id, 0.5, 0.5, {})
        raise TypeError(q)

    def _boolean(self, state: str, q: Boolean) -> float:
        if q.key == "screen.injection":
            hits = sum(1 for rx in _INJECTION_RX if rx.search(state))
            return {0: 0.03, 1: 0.8}.get(hits, 0.97)
        if q.key.startswith("gate.") and q.key != "gate.state_changed":
            return _gate_probability(q.key, state)
        # A question the baseline cannot answer is maximally uncertain, which
        # every gate treats as "escalate to a human".
        return 0.5

    def _choice(self, state: str, q: Choice) -> Answer:
        want = _tokens((q.criteria or "") + " " + q.prompt)
        scores = []
        for o in q.options:
            ot = _tokens(o)
            overlap = len(want & ot) / (len(ot) or 1)
            scores.append(overlap + 1e-6)
        total = sum(scores)
        dist = {o: s / total for o, s in zip(q.options, scores, strict=True)}
        best = max(q.options, key=lambda o: dist[o])
        return Answer(q.id, best, dist[best], dist)
