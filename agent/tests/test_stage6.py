"""Stage 6, first part: connectors, consented to one at a time.

The connector is a real MCP server over stdio (``demo/ticketing_server.py``), the
same way a first-party connector is reached in production. What is shown here:

- the first use of a connector asks the person to allow it, once; the answer is
  remembered across runs until revoked, and "no" is final for the run;
- a connector the administrator does not allow is never started or offered;
- every change stops for approval with the exact values, before and after, and
  a public reply is gated as sending externally;
- ticket text is untrusted: fenced and screened.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import base_config

from ondo_agent import log as L
from ondo_agent.approvals import AutoApprovals
from ondo_agent.demo.policies import ticket_reply_policy
from ondo_agent.gates import GateKeeper, GateRule, ProposedAction
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.gateway import scripted_client
from ondo_agent.runtime import assemble

THRESHOLDS = str(Path(__file__).resolve().parents[1] / "config" / "thresholds.json")
REQUEST = "Reply to Halleck's renewal ticket with the new annual value from the signed contract, and mark it as waiting on the customer."


def ticket_config(drive, tmp_path, *, allowed=("ticketing",), rules=None):
    store = tmp_path / "tickets.json"
    cfg = base_config(
        drive,
        tmp_path,
        policy={"excluded_paths": ["**/HR/**"], "allowed_connectors": list(allowed)},
        connectors={
            "ticketing": {"command": ["{python}", "-m", "ondo_agent.demo.ticketing_server", "--store", str(store)]}
        },
        gates={"thresholds_file": THRESHOLDS, "rules": rules or []},
    )
    return cfg, store


def tickets(store: Path) -> dict:
    return json.loads(store.read_text())


async def run(cfg, policy, approvals, request=REQUEST):
    a = await assemble(cfg, model=scripted_client(policy), approvals=approvals)
    try:
        res = await a.harness.run(request)
    finally:
        await a.aclose()
    return a, res


async def test_consent_once_then_every_change_gated_with_exact_values(drive, tmp_path):
    cfg, store = ticket_config(drive, tmp_path)
    approvals = AutoApprovals(True, by="mara.okonjo")
    a, res = await run(cfg, ticket_reply_policy(drive), approvals)

    assert res.status == "finished", [e.data for e in a.log.of_type(L.TOOL_RESULT)]
    assert res.answer == "I replied on NW-1042 with 193,725 GBP and set it to waiting on the customer."
    t = tickets(store)["NW-1042"]
    assert t["status"] == "pending"
    [reply] = t["comments"]
    assert reply["public"] and "193,725 GBP" in reply["body"] and "clause 7.2" in reply["body"]

    consent, comment, update = approvals.seen
    # Asked once, at the first use, saying what the connector can do.
    assert consent.kind == "consent" and consent.connector == "ticketing" and consent.effects == []
    assert consent.title == "Let Ondo use Ticketing?"
    assert ("Add comment", "Sends out · asks first") in [(v.label, v.after) for v in consent.values]
    # A public reply is sending externally; the approver sees who gets it and the exact text.
    assert comment.effects == ["sends_externally"]
    assert [(v.label, v.after) for v in comment.values][0] == ("Visible to", "Customer (priya.shah@halleck.example)")
    assert comment.values[1].after == reply["body"]
    # An update shows the record before and after.
    assert update.effects == ["submits_to_system_of_record"]
    assert [(v.label, v.before, v.after) for v in update.values] == [("Status", "open", "pending")]
    gates = [e.data for e in a.log.of_type(L.GATE) if e.data["required"]]
    assert [g["by_effect"][g["effects"][0]]["source"] for g in gates] == [
        "declared:ticketing.add_comment",
        "declared:ticketing.update_ticket",
    ]
    # Reads and changes are in the log, and ticket text came back fenced as untrusted.
    ops = [(e.data["tool"], e.data["op"]) for e in a.log.of_type(L.CONNECTOR_ACCESS)]
    assert ops == [
        ("search_tickets", "read"),
        ("get_ticket", "read"),
        ("add_comment", "changed"),
        ("update_ticket", "changed"),
    ]
    results = [e.data for e in a.log.of_type(L.TOOL_RESULT) if e.data["name"].startswith("ticketing_")]
    assert all(r["content"].startswith('<untrusted_data origin="connector:ticketing"') for r in results)
    consented = [e.data for e in a.log.of_type(L.GRANT_CHANGED) if e.data["kind"] == "connector:ticketing"]
    assert consented == [{"change": "granted", "kind": "connector:ticketing", "scope": [], "by": "mara.okonjo"}]

    # The yes is remembered: the next run is not asked again.
    assert json.loads(cfg.consents_path.read_text())["ticketing"]["by"] == "mara.okonjo"
    again = AutoApprovals(True, by="mara.okonjo")
    script = iter([call(("ticketing_get_ticket", {"id": "NW-1043"})), say("read it")])
    a2, res2 = await run(cfg, lambda m, t: next(script), again, "What is NW-1043 about?")
    assert res2.status == "finished" and again.seen == []
    assert "account is locked" in a2.log.of_type(L.TOOL_RESULT)[0].data["content"]


async def test_no_to_consent_is_final_for_the_run_and_changes_nothing(drive, tmp_path):
    cfg, store = ticket_config(drive, tmp_path)
    before = tickets(store) if store.exists() else None
    approvals = AutoApprovals(lambda r: r.kind != "consent", by="mara.okonjo")
    script = iter(
        [
            call(("ticketing_search_tickets", {"query": "halleck"})),
            call(("ticketing_add_comment", {"id": "NW-1042", "body": "hello", "public": True})),
            say("stopped"),
        ]
    )
    a, res = await run(cfg, lambda m, t: next(script), approvals)
    assert res.status == "finished"
    assert [r.kind for r in approvals.seen] == ["consent"]  # asked once, not again for the second call
    denied = [e.data["reason"] for e in a.log.of_type(L.PERMISSION_DENIED)]
    assert denied == ["consent_refused", "consent_refused"]
    assert not a.log.of_type(L.CONNECTOR_ACCESS)
    assert not cfg.consents_path.exists()
    if before is not None:
        assert tickets(store) == before
    assert all(not t["comments"] or t["id"] == "NW-1038" for t in tickets(store).values())


async def test_a_connector_policy_does_not_allow_is_never_started_or_offered(drive, tmp_path):
    cfg, store = ticket_config(drive, tmp_path, allowed=())
    approvals = AutoApprovals(True)
    script = iter([call(("ticketing_search_tickets", {"query": "halleck"})), say("done")])
    a, res = await run(cfg, lambda m, t: next(script), approvals)
    offered = a.log.of_type(L.RUN_STARTED)[0].data["tools"]
    assert not any(t.startswith("ticketing_") for t in offered)
    assert "Unknown tool" in a.log.of_type(L.TOOL_RESULT)[0].data["content"]
    assert approvals.seen == [] and not store.exists()  # the server never ran


async def test_ticket_text_is_untrusted_and_a_flagged_ticket_taints_the_run(drive, tmp_path):
    cfg, store = ticket_config(drive, tmp_path)
    approvals = AutoApprovals(True, by="mara.okonjo")
    script = iter(
        [
            call(("ticketing_get_ticket", {"id": "NW-1044"})),
            call(("ticketing_update_ticket", {"id": "NW-1044", "assignee": "mara.okonjo"})),
            say("done"),
        ]
    )
    a, res = await run(cfg, lambda m, t: next(script), approvals, "Pick up the Fairhaven invoice ticket.")
    got = a.log.of_type(L.TOOL_RESULT)[0].data
    assert got["content"].startswith('<untrusted_data origin="connector:ticketing" screening="flagged')
    assert a.harness.tainted
    update = approvals.seen[-1]
    gate = [e.data for e in a.log.of_type(L.GATE)][-1]
    assert gate["tainted"] and update.kind == "effect"
    # The injected instruction did nothing: every ticket is still open.
    assert all(t["status"] in ("open", "closed") for t in tickets(store).values())
    assert [t for t in tickets(store).values() if t["status"] == "closed"] == [tickets(store)["NW-1038"]]


# -- no subprocess needed ------------------------------------------------------------------


async def test_declared_effects_gate_unless_an_allow_rule_says_otherwise():
    note = ProposedAction(
        "ticketing.add_comment",
        {"public": False},
        "Added a note to ticket NW-1",
        max_effect="send_external",
        app="Ticketing",
        element="add comment",
        declared_effects=["submits_to_system_of_record"],
    )
    d = await GateKeeper([], None, {}).evaluate(note)
    assert d.effects[0] == "submits_to_system_of_record"
    assert d.by_effect["submits_to_system_of_record"].source == "declared:ticketing.add_comment"
    # An operator may decide internal notes are fine; that is configuration, not the model.
    allow = GateRule("submits_to_system_of_record", "allow", "notes-ok", tool="ticketing.add_comment")
    d = await GateKeeper([allow], None, {}).evaluate(note)
    assert "submits_to_system_of_record" not in d.effects


def test_any_mcp_server_can_be_declared_in_configuration():
    import pytest

    from ondo_agent.connectors.catalog import from_config

    d = from_config(
        "billing",
        {"name": "Billing", "tools": {"list_invoices": {"effect": "read"}, "approve": {"effect": "move_money"}}},
    )
    assert {t.name: t.gate_effects({}) for t in d.tools.values()} == {"list_invoices": [], "approve": ["moves_money"]}
    assert from_config("desk", {"catalog": "ticketing"}).tools.keys() >= {"search_tickets", "update_ticket"}
    with pytest.raises(ValueError, match="unknown effect"):
        from_config("x", {"tools": {"t": {"effect": "delete_everything"}}})


def test_a_consent_not_yet_recorded_is_not_revoked_by_an_unrelated_push(tmp_path):
    """The control plane records a consent when the agent's approval event reaches
    it. A grants push that crosses it on the wire must not read as a revocation."""
    from ondo_agent.connection import ControlPlaneAgent, Credentials
    from ondo_agent.permissions import Consent

    cfg = base_config(Path(tmp_path), tmp_path, policy={"allowed_connectors": ["ticketing"]})
    agent = ControlPlaneAgent(cfg, Credentials("http://127.0.0.1:9", "agt_x", "t"))
    broker = agent.state.broker()
    agent.runs["r1"] = broker
    agent._pending_consents.add("ticketing")
    broker.consents["ticketing"] = agent.state.consents["ticketing"] = Consent("ticketing", "mara", 1.0)
    agent._apply_grants({"type": "grants", "by": "mara", "grants": {}, "connectors": {}})
    assert broker.stopped is None and "ticketing" in broker.consents
    # Once confirmed, leaving it out is a revocation, and the run stops.
    agent._apply_grants({"type": "grants", "grants": {}, "connectors": {"ticketing": {"by": "mara", "at": 1000}}})
    agent._apply_grants(
        {"type": "grants", "by": "mara", "reason": "revoked by the user", "grants": {}, "connectors": {}}
    )
    assert broker.stopped is not None and broker.stopped.reason == "Consent for ticketing was revoked by the user."
