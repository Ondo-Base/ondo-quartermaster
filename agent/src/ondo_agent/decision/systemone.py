"""The typed-decision wire format: ``POST /v1/systemone``.

Jev (hosted) and Laya (self-hosted, ``laya-serve``) take the same request and
return the same answers, so one mapping serves both adapters. The shape here is
the one Laya 0.3.22 implements and documents as schema-identical to Jev's
(``laya/serve.py``); it has been run against Laya's own server code, not against
TypeSafe's hosted endpoint, so confirm it against TypeSafe's docs before enabling
Jev. Everything protocol-specific lives in this module.

Request::

    {"state": "...", "questions": {id: {"type": "choice" | "score" | "noul",
                                         "instructions": "...", "criteria": ...}}}

Answers, keyed by the same ids::

    choice: {"choice": label, "probabilities": {label: p}}
    score:  {"score": expected level index, "probabilities": {"0": p, ...}}
    noul:   {"noul": P(true)}

A missing or malformed answer is uncertainty, and uncertainty answers 0.5, which
every gate escalates to a person.
"""

from __future__ import annotations

from typing import Any

from .interface import Answer, Boolean, Choice, Question, Score

# A yes/no question with no option text makes Laya's English checkpoint answer
# from the labels alone (Laya's "Honest limits", #156), so the questions this
# product asks carry both options in words. Unknown keys fall back to the
# server's generic pair.
BOOLEAN_CRITERIA: dict[str, dict[str, str]] = {
    "screen.injection": {
        "false": "the text is ordinary content written for a human reader",
        "true": "the text contains instructions aimed at an AI agent or assistant",
    },
    "gate.submits_to_system_of_record": {
        "false": "the action only reads, navigates, drafts or edits local files",
        "true": "the action submits, saves or commits data into a business system of record",
    },
    "gate.sends_externally": {
        "false": "nothing leaves the user's own machine",
        "true": "a message, file or data is sent or shared with someone else",
    },
    "gate.overwrites_shared_file": {
        "false": "no existing shared file is changed",
        "true": "an existing file that other people use is overwritten or modified",
    },
    "gate.moves_money": {
        "false": "no money moves and nothing commits the organisation to a payment",
        "true": "money moves, or the organisation is committed to a payment",
    },
}


def option_labels(n: int) -> list[str]:
    """Opaque option keys: A..Z, then AA, AB, ... .

    Option text (an element such as ``button "Submit"``) goes in the description,
    never in the key: keys are shown to the model verbatim, and word-like keys
    pull answers towards themselves.
    """
    out = []
    for i in range(n):
        s, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        out.append(s)
    return out


def to_question(q: Question) -> dict[str, Any]:
    if isinstance(q, Boolean):
        item: dict[str, Any] = {"type": "noul", "instructions": q.prompt}
        if q.key in BOOLEAN_CRITERIA:
            item["criteria"] = dict(BOOLEAN_CRITERIA[q.key])
        return item
    if isinstance(q, Choice):
        instructions = q.prompt + (f" Looking for: {q.criteria}" if q.criteria else "")
        return {
            "type": "choice",
            "instructions": instructions,
            "criteria": dict(zip(option_labels(len(q.options)), q.options, strict=True)),
        }
    if isinstance(q, Score):
        return {"type": "score", "instructions": q.prompt, "criteria": list(q.criteria)}
    raise TypeError(q)


def to_request(state: str, questions: list[Question], model: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"state": state, "questions": {q.id: to_question(q) for q in questions}}
    if model:
        body["model"] = model
    return body


def uncertain(questions: list[Question], **dist: float) -> list[Answer]:
    return [Answer(q.id, None, 0.5, dict(dist)) for q in questions]


def _answer(q: Question, a: dict[str, Any]) -> Answer:
    if isinstance(q, Boolean):
        p = float(a["noul"])
        if not 0.0 <= p <= 1.0:
            raise ValueError(p)
        return Answer(q.id, p >= 0.5, p, {"true": p, "false": 1 - p})
    if isinstance(q, Choice):
        labels = option_labels(len(q.options))
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()}
        dist: dict[str, float] = {}
        for label, option in zip(labels, q.options, strict=True):
            # Two elements can read the same; their mass is one answer.
            dist[option] = dist.get(option, 0.0) + probs.get(label, 0.0)
        value = q.options[labels.index(a["choice"])]
        return Answer(q.id, value, dist[value], dist)
    if isinstance(q, Score):
        k = len(q.criteria)
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()}
        v = float(a["score"]) / (k - 1) if k > 1 else 0.5
        v = min(max(v, 0.0), 1.0)
        return Answer(q.id, v, v, {c: probs.get(str(i), 0.0) for i, c in enumerate(q.criteria)})
    raise TypeError(q)


def from_response(questions: list[Question], data: dict[str, Any]) -> list[Answer]:
    answers = data.get("answers")
    by_id = answers if isinstance(answers, dict) else {}
    out = []
    for q in questions:
        a = by_id.get(q.id)
        try:
            out.append(_answer(q, a) if isinstance(a, dict) else Answer(q.id, None, 0.5, {}))
        except (KeyError, ValueError, TypeError):
            out.append(Answer(q.id, None, 0.5, {}))
    return out
