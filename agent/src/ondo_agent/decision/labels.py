"""Labelling logged decisions, and exporting them to fine-tune a local model.

Every decision the agent asks is appended to the decisions file
(``agent.decision_labels``, ``LoggedDecisionModel``) with the model's answer.
A person supplies the right answer here. Labels go to a sidecar file next to it
(``decisions.labels.jsonl``), append-only, latest wins, so labelling never
rewrites a file the agent is appending to.

``export`` writes what a fine-tune needs: each labelled decision as a
``/v1/systemone`` request plus the right answer per question, split into train
and held-out by run, so no run is on both sides. The calibration fixtures are
not in the decisions file, so they stay a clean measurement.

    ondo-agent decisions list
    ondo-agent decisions label <id> inj0=false
    ondo-agent decisions export --out finetune/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import systemone
from .interface import Boolean, Choice, Question, Score


def labels_file(decisions: Path) -> Path:
    return decisions.with_name(decisions.stem + ".labels.jsonl")


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut short by a crash is not a decision
    return out


def question_from_dict(d: dict[str, Any]) -> Question:
    kind = d.get("kind")
    if kind == "boolean":
        return Boolean(d["id"], d["prompt"], key=d.get("key", ""))
    if kind == "choice":
        return Choice(d["id"], d["prompt"], list(d["options"]), d.get("criteria"), key=d.get("key", ""))
    if kind == "score":
        return Score(d["id"], d["prompt"], list(d["criteria"]), key=d.get("key", ""))
    raise ValueError(f"unknown question kind {kind!r}")


def parse_value(q: Question, raw: str) -> Any:
    """A person's answer, checked against the question."""
    s = raw.strip()
    if isinstance(q, Boolean):
        if s.lower() in ("true", "yes", "y", "1"):
            return True
        if s.lower() in ("false", "no", "n", "0"):
            return False
        raise ValueError(f"{q.id} is a yes/no question; answer true or false")
    if isinstance(q, Choice):
        if s in q.options:
            return s
        if s.isdigit() and 1 <= int(s) <= len(q.options):
            return q.options[int(s) - 1]
        raise ValueError(f"{q.id}: answer one of the options, or its number 1..{len(q.options)}")
    v = float(s)
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"{q.id} is a score from 0 to 1")
    return v


def load(decisions: Path) -> list[dict[str, Any]]:
    """Decisions with their current labels merged in (``labels``: question id -> value)."""
    rows = _read(decisions)
    latest: dict[tuple[str, str], Any] = {}
    for lab in _read(labels_file(decisions)):
        latest[(lab["row"], lab["question"])] = lab["value"]
    for r in rows:
        r["labels"] = {q["id"]: latest[(r["id"], q["id"])] for q in r["questions"] if (r["id"], q["id"]) in latest}
    return rows


def label(decisions: Path, row_id: str, values: dict[str, str], *, by: str = "") -> dict[str, Any]:
    row = next((r for r in _read(decisions) if r.get("id") == row_id), None)
    if row is None:
        raise KeyError(f"no decision {row_id!r} in {decisions}")
    qs = {q["id"]: question_from_dict(q) for q in row["questions"]}
    parsed = {}
    for qid, raw in values.items():
        if qid not in qs:
            raise KeyError(f"decision {row_id} has no question {qid!r} (it has {', '.join(qs)})")
        parsed[qid] = parse_value(qs[qid], raw)
    with open(labels_file(decisions), "a", encoding="utf-8") as f:
        for qid, v in parsed.items():
            f.write(json.dumps({"row": row_id, "question": qid, "value": v, "by": by, "ts": time.time()}) + "\n")
    return parsed


def _target(q: Question, value: Any) -> Any:
    if isinstance(q, Boolean):
        return bool(value)
    if isinstance(q, Choice):
        return systemone.option_labels(len(q.options))[q.options.index(value)]
    return round(float(value) * (len(q.criteria) - 1))


def held_out(key: str, fraction: float) -> bool:
    h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return h < fraction


def export(decisions: Path, out: Path, *, fraction: float = 0.2) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    counts = {"train": 0, "heldout": 0, "unlabelled": 0}
    with (
        open(out / "train.jsonl", "w", encoding="utf-8") as tr,
        open(out / "heldout.jsonl", "w", encoding="utf-8") as ho,
    ):
        for r in load(decisions):
            if not r["labels"]:
                counts["unlabelled"] += 1
                continue
            qs = [question_from_dict(q) for q in r["questions"] if q["id"] in r["labels"]]
            ex = {
                "id": r["id"],
                "run_id": r.get("run_id"),
                "purpose": r.get("purpose", ""),
                **systemone.to_request(r["state"], qs),
                "answers": {q.id: _target(q, r["labels"][q.id]) for q in qs},
            }
            side = "heldout" if held_out(r.get("run_id") or r["id"], fraction) else "train"
            (ho if side == "heldout" else tr).write(json.dumps(ex, ensure_ascii=False) + "\n")
            counts[side] += 1
    return counts


def _list(decisions: Path, show_all: bool) -> int:
    for r in load(decisions):
        todo = [q for q in r["questions"] if q["id"] not in r["labels"]]
        if not todo and not show_all:
            continue
        answers = {a["question_id"]: a for a in r.get("answers", [])}
        print(f"{r['id']}  {r.get('purpose') or '-'}  {r.get('model', '')}")
        print("   " + r["state"][:300].replace("\n", "\n   "))
        for q in r["questions"]:
            a = answers.get(q["id"], {})
            have = r["labels"].get(q["id"], "(unlabelled)")
            print(f"   {q['id']}: {q['prompt']}")
            if q.get("options"):
                for i, o in enumerate(q["options"], 1):
                    print(f"      {i}. {o}")
            print(f"      model: {a.get('value')!r} p={a.get('probability')}   label: {have!r}")
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ondo-agent decisions", description="Label logged decisions and export them.")
    ap.add_argument("--file", default=".ondo/decisions.jsonl", help="the decisions file (agent.decision_labels)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="show decisions still to label")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("label", help="record the right answers: <id> question=value ...")
    p.add_argument("id")
    p.add_argument("answers", nargs="+")
    p.add_argument("--by", default="")
    p = sub.add_parser("export", help="write train.jsonl and heldout.jsonl, split by run")
    p.add_argument("--out", required=True)
    p.add_argument("--heldout", type=float, default=0.2)
    a = ap.parse_args(argv)
    path = Path(a.file)
    try:
        if a.cmd == "list":
            sys.exit(_list(path, a.all))
        if a.cmd == "label":
            values = dict(x.split("=", 1) for x in a.answers if "=" in x)
            if len(values) != len(a.answers):
                raise ValueError("answers are question=value")
            print(json.dumps(label(path, a.id, values, by=a.by)))
            return
        print(json.dumps(export(path, Path(a.out), fraction=a.heldout)))
    except (KeyError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
