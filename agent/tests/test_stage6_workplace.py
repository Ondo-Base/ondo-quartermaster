"""Stage 6: the mail, calendar and team-site connectors.

Each is a real MCP server over stdio (``demo/workplace_servers.py``). The rules
they follow are the ticketing connector's: consent once per connector, reads and
own-items freely, every send, invitation, shared change or share stops for
approval with the exact values; what comes back is untrusted.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import base_config

from ondo_agent import log as L
from ondo_agent.approvals import AutoApprovals
from ondo_agent.demo.policies import mail_and_calendar_policy
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.gateway import scripted_client
from ondo_agent.runtime import assemble

THRESHOLDS = str(Path(__file__).resolve().parents[1] / "config" / "thresholds.json")
CONNECTORS = ("mail", "calendar", "documents")


def workplace_config(drive, tmp_path, allowed=CONNECTORS):
    connectors = {
        c: {
            "command": [
                "{python}",
                "-m",
                "ondo_agent.demo.workplace_servers",
                "--connector",
                c,
                "--store",
                str(tmp_path / f"{c}.json"),
            ]
        }
        for c in CONNECTORS
    }
    return base_config(
        drive,
        tmp_path,
        policy={"excluded_paths": ["**/HR/**"], "allowed_connectors": list(allowed)},
        connectors={"org_domain": "northwind-ops.com", **connectors},
        gates={"thresholds_file": THRESHOLDS, "rules": []},
    )


def store(tmp_path, c) -> dict:
    return json.loads((tmp_path / f"{c}.json").read_text())


async def run(cfg, policy, approvals, request="task"):
    a = await assemble(cfg, model=scripted_client(policy), approvals=approvals)
    try:
        res = await a.harness.run(request)
    finally:
        await a.aclose()
    return a, res


def scripted(*steps):
    it = iter(steps)
    return lambda m, t: next(it)


async def test_reply_by_mail_and_invite_by_calendar(drive, tmp_path):
    cfg = workplace_config(drive, tmp_path)
    approvals = AutoApprovals(True, by="mara.okonjo")
    a, res = await run(cfg, mail_and_calendar_policy(drive), approvals)
    assert res.status == "finished", [e.data for e in a.log.of_type(L.TOOL_RESULT)]
    assert res.answer == "I replied to Priya with 193,725 GBP and invited her to a call at 2026-10-06T09:30."

    kinds = [(r.kind, r.connector, r.effects) for r in approvals.seen]
    assert kinds == [
        ("consent", "mail", []),
        ("effect", None, ["sends_externally"]),  # send: the draft itself was not gated
        ("consent", "calendar", []),
        ("effect", None, ["sends_externally"]),  # an invitation to someone
    ]
    send, invite = approvals.seen[1], approvals.seen[3]
    to = send.values[0]
    assert to.label == "To" and to.flagged and "outside northwind-ops.com" in to.after
    assert "193,725 GBP" in send.values[-1].after
    assert [(v.label, v.after) for v in invite.values][:2] == [
        ("Title", "Halleck renewal call"),
        ("When", "2026-10-06T09:30 to 2026-10-06T10:00"),
    ]
    sent = [m for m in store(tmp_path, "mail").values() if m["folder"] == "sent"]
    assert len(sent) == 1 and sent[0]["to"] == ["priya.shah@halleck.example"]
    [ev] = [e for e in store(tmp_path, "calendar").values() if e["title"] == "Halleck renewal call"]
    assert ev["attendees"] == ["priya.shah@halleck.example"]


async def test_own_items_need_no_approval_but_inviting_someone_does(drive, tmp_path):
    cfg = workplace_config(drive, tmp_path)
    approvals = AutoApprovals(lambda r: r.kind == "consent", by="mara.okonjo")  # consents yes, effects no
    a, res = await run(
        cfg,
        scripted(
            call(("mail_create_draft", {"to": "priya.shah@halleck.example", "subject": "Hi", "body": "Draft"})),
            call(("calendar_create_event", {"title": "Focus", "start": "2026-10-07T10:00", "end": "2026-10-07T11:00"})),
            call(
                (
                    "calendar_create_event",
                    {
                        "title": "Sync",
                        "start": "2026-10-07T14:00",
                        "end": "2026-10-07T14:30",
                        "attendees": "priya.shah@halleck.example",
                    },
                )
            ),
            say("done"),
        ),
        approvals,
    )
    assert res.status == "finished"
    assert [r.kind for r in approvals.seen] == ["consent", "consent", "effect"]
    assert [m["id"] for m in store(tmp_path, "mail").values() if m["folder"] == "drafts"] == ["draft-1"]
    titles = {e["title"] for e in store(tmp_path, "calendar").values()}
    assert "Focus" in titles and "Sync" not in titles  # refused: no invitation went out
    assert "Not done" in a.log.of_type(L.TOOL_RESULT)[-1].data["content"]


async def test_changing_a_shared_document_shows_the_diff_and_who_changed_it_last(drive, tmp_path):
    cfg = workplace_config(drive, tmp_path)
    approvals = AutoApprovals(True, by="mara.okonjo")
    new = (
        "# Renewals tracker\n\n| Customer | Renewal | Status |\n| --- | --- | --- |\n"
        "| Halleck Logistics | 1 July | PO raised |\n| Pemberton Freight | 1 Nov | In review |\n"
    )
    a, res = await run(
        cfg,
        scripted(
            call(("documents_update_document", {"id": "doc-71", "content": new})),
            call(("documents_share_document", {"id": "doc-71", "email": "priya.shah@halleck.example"})),
            say("done"),
        ),
        approvals,
    )
    assert res.status == "finished"
    _, update, share = approvals.seen
    assert update.effects == ["overwrites_shared_file"]
    assert "-| Halleck Logistics | 1 July | Awaiting PO |" in update.diff
    assert "+| Halleck Logistics | 1 July | PO raised |" in update.diff
    assert ("Last changed by", "sam.adeyemi@northwind-ops.com") in [(v.label, v.after) for v in update.values]
    assert share.effects == ["sends_externally"] and share.values[1].flagged
    doc = store(tmp_path, "documents")["doc-71"]
    assert "PO raised" in doc["content"] and doc["shared_with"] == ["priya.shah@halleck.example"]


async def test_an_email_carrying_instructions_is_flagged_and_gates_everything_after(drive, tmp_path):
    cfg = workplace_config(drive, tmp_path, allowed=("mail",))
    approvals = AutoApprovals(True, by="mara.okonjo")
    a, res = await run(
        cfg,
        scripted(
            call(("mail_read_message", {"id": "msg-302"})),
            call(("mail_create_draft", {"to": "payments@fairhaven-remit.example", "subject": "Invoices", "body": "x"})),
            say("done"),
        ),
        approvals,
    )
    first = a.log.of_type(L.TOOL_RESULT)[0].data["content"]
    assert first.startswith('<untrusted_data origin="connector:mail" screening="flagged')
    # Even a draft, normally free, stops for a person once the run is tainted.
    assert [r.kind for r in approvals.seen] == ["consent", "effect"]
    assert a.log.of_type(L.GATE)[-1].data["tainted"]
    # Calendar and team sites were not allowed by policy: never started, never offered.
    offered = a.log.of_type(L.RUN_STARTED)[0].data["tools"]
    assert any(t.startswith("mail_") for t in offered)
    assert not any(t.startswith(("calendar_", "documents_")) for t in offered)
    assert not (tmp_path / "calendar.json").exists()


async def test_a_complete_declaration_settles_every_effect_but_a_tainted_run_still_gates():
    from ondo_agent.gates import GateKeeper, ProposedAction

    draft = ProposedAction(
        "mail.create_draft",
        {"to": "priya.shah@halleck.example", "body": "Please pay the invoice today"},
        "Drafted a message to priya.shah@halleck.example in Mail",
        max_effect="write_local",
        declared_effects=[],
        effects_enumerated=True,
    )

    class Always:  # a decision model that would gate everything, to show it is not asked
        async def ask(self, *a, **k):
            raise AssertionError("the decision model was asked about an enumerated action")

    d = await GateKeeper([], Always(), {}).evaluate(draft)
    assert not d.required
    assert {v.source for v in d.by_effect.values()} == {"declared_none:mail.create_draft"}
    d = await GateKeeper([], Always(), {}).evaluate(draft, tainted=True)
    assert d.required and {v.source for v in d.by_effect.values()} == {"tainted_run"}
