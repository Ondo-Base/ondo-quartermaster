"""Long-running tasks, and runs that survive the agent restarting.

A long operation on a connector's side (a reconciliation) is started once and
waited for by its handle, with progress shown as it goes. A run survives the
agent process ending: a new process opens the same log and carries on, waits for
the same operation rather than starting another, and says honestly what became
of the calls the old process was serving.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import base_config

from ondo_agent import log as L
from ondo_agent.approvals import AutoApprovals, QueueApprovals, Resolution
from ondo_agent.log import EventLog
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.gateway import scripted_client
from ondo_agent.runtime import assemble

THRESHOLDS = str(Path(__file__).resolve().parents[1] / "config" / "thresholds.json")


def config(drive, tmp_path, seconds: float = 1.5):
    def server(c, **extra):
        return {
            "command": [
                "{python}",
                "-m",
                "ondo_agent.demo.workplace_servers",
                "--connector",
                c,
                "--store",
                str(tmp_path / f"{c}.json"),
            ],
            **extra,
        }

    return base_config(
        drive,
        tmp_path,
        policy={"allowed_connectors": ["ledger", "calendar", "documents"]},
        connectors={
            "ledger": server(
                "ledger", env={"ONDO_LEDGER_SECONDS": str(seconds)}, poll_seconds=0.2, max_poll_seconds=0.4
            ),
            "calendar": server("calendar"),
            "documents": server("documents"),
        },
        gates={"thresholds_file": THRESHOLDS, "rules": []},
    )


def answer_when(pred, text):
    """A model that asks for nothing more once a tool result matches ``pred``."""

    def policy(messages, tools):
        results = [m for m in messages if m.role == "tool"]
        if results and pred(results[-1].text):
            return say(text)
        return say("still waiting")

    return policy


async def until(pred, timeout=20.0):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def crash_when(a, pred):
    """Run until ``pred(log)`` holds, then end the process's work abruptly."""
    task = asyncio.create_task(a.harness.run("Reconcile receivables for September."))
    try:
        await until(lambda: pred(a.log) or task.done())
        assert not task.done(), [e.type for e in a.log]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        await a.aclose()


async def test_a_long_operation_is_waited_for_with_progress(drive, tmp_path):
    cfg = config(drive, tmp_path)
    script = iter([call(("ledger_start_reconciliation", {"period": "2026-09"})), say("Three items did not match.")])
    a = await assemble(cfg, model=scripted_client(lambda m, t: next(script)), approvals=AutoApprovals(True))
    try:
        res = await a.harness.run("Reconcile receivables for September.")
    finally:
        await a.aclose()
    assert res.status == "finished"
    [started] = a.log.of_type(L.OPERATION_STARTED)
    [ended] = a.log.of_type(L.OPERATION_FINISHED)
    assert started.data["handle"] == "op-1" and ended.data["status"] == "succeeded"
    progress = [e.data["title"] for e in a.log.of_type(L.STEP) if "% done" in e.data["title"]]
    assert progress and progress[0].startswith("Reconciling receivables for 2026-09: ")
    result = a.log.of_type(L.TOOL_RESULT)[0].data
    assert result["content"].startswith('<untrusted_data origin="connector:ledger"')
    assert "UNKNOWN CREDIT" in result["content"]


async def test_after_a_restart_the_same_operation_is_waited_for_not_started_again(drive, tmp_path):
    cfg = config(drive, tmp_path, seconds=3)
    script = iter([call(("ledger_start_reconciliation", {"period": "2026-09"}))])
    first = await assemble(cfg, model=scripted_client(lambda m, t: next(script)), approvals=AutoApprovals(True))
    path = first.log.path
    await crash_when(first, lambda log: log.of_type(L.OPERATION_STARTED))

    log = EventLog.open(path)
    assert log.verify_chain() and not log.of_type(L.OPERATION_FINISHED)
    second = await assemble(
        cfg,
        model=scripted_client(answer_when(lambda c: "UNKNOWN CREDIT" in c, "Three items did not match.")),
        approvals=AutoApprovals(True),
        log=log,
    )
    try:
        res = await second.harness.resume()
    finally:
        await second.aclose()
    assert res.status == "finished" and res.answer == "Three items did not match."
    assert [e.data["interrupted"] for e in log.of_type(L.RUN_RESTORED)] == [
        [log.of_type(L.TOOL_CALL)[0].data["call_id"]]
    ]
    assert [e.data["handle"] for e in log.of_type(L.OPERATION_FINISHED)] == ["op-1"]
    ops = [r for r in json.loads((tmp_path / "ledger.json").read_text()).values() if r.get("kind") == "operation"]
    assert len(ops) == 1  # waited for, not started twice
    assert log.verify_chain() and len(log.of_type(L.RUN_STARTED)) == 1


async def test_after_a_restart_each_interrupted_call_is_reported_honestly(drive, tmp_path):
    cfg = config(drive, tmp_path)
    new = "# Renewals tracker\n\nEmptied.\n"
    script = iter(
        [
            call(
                ("calendar_create_event", {"title": "Focus", "start": "2026-10-07T10:00", "end": "2026-10-07T11:00"}),
                ("documents_update_document", {"id": "doc-71", "content": new}),
            )
        ]
    )

    class ConsentOnly(QueueApprovals):
        """Allows each connector at once; nobody answers the change itself."""

        async def request(self, req):
            if req.kind == "consent":
                return Resolution(True, "mara.okonjo")
            return await super().request(req)

    approvals = ConsentOnly()
    first = await assemble(cfg, model=scripted_client(lambda m, t: next(script)), approvals=approvals)
    path = first.log.path
    await crash_when(
        first, lambda log: [e for e in log.of_type(L.APPROVAL_REQUESTED) if e.data.get("kind") == "effect"]
    )

    log = EventLog.open(path)
    seen = []

    def policy(messages, tools):
        seen.extend(m.text for m in messages if m.role == "tool")
        return say("I checked what happened.")

    second = await assemble(cfg, model=scripted_client(policy), approvals=AutoApprovals(True), log=log)
    try:
        res = await second.harness.resume()
    finally:
        await second.aclose()
    assert res.status == "finished"
    event, update = [e.data["content"] for e in log.of_type(L.TOOL_RESULT)]
    assert event.startswith("This finished before the agent restarted") and "do not do it again" in event
    assert update.startswith("Interrupted by an agent restart while waiting for approval. Nothing was changed")
    doc = json.loads((tmp_path / "documents.json").read_text())["doc-71"]
    assert "Awaiting PO" in doc["content"]  # the unapproved change did not land
    assert [e["title"] for e in json.loads((tmp_path / "calendar.json").read_text()).values()].count("Focus") == 1


async def test_a_stop_while_waiting_ends_the_run_and_leaves_the_handle_open(drive, tmp_path):
    cfg = config(drive, tmp_path, seconds=30)
    script = iter([call(("ledger_start_reconciliation", {"period": "2026-09"}))])
    a = await assemble(cfg, model=scripted_client(lambda m, t: next(script)), approvals=AutoApprovals(True))
    try:
        task = asyncio.create_task(a.harness.run("Reconcile receivables for September."))
        await until(lambda: a.log.of_type(L.OPERATION_STARTED))
        a.broker.stop("Stopped by the user")
        res = await asyncio.wait_for(task, 5)
    finally:
        await a.aclose()
    assert res.status == "stopped"
    assert not a.log.of_type(L.OPERATION_FINISHED)


async def test_the_demo_profile_runs_the_connector_tasks_the_guides_name(drive, tmp_path):
    """With no model provider, the scripted ``demo`` profile must route each connector
    request in the README and docs/testing-on-windows-and-mac.md to its script."""
    from ondo_agent.models.profile import load_profiles
    from ondo_agent.runtime import make_model

    cfg = config(drive, tmp_path)

    def server(c):
        return {
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

    cfg.raw["connectors"].update(
        {
            "mail": server("mail"),
            "ticketing": {
                "command": ["{python}", "-m", "ondo_agent.demo.ticketing_server", "--store", str(tmp_path / "t.json")]
            },
        }
    )
    cfg.raw["policy"]["allowed_connectors"] += ["mail", "ticketing"]
    cfg.raw["models"] = {"orchestrator": "demo"}
    cfg.profiles = load_profiles(Path(__file__).resolve().parents[1] / "config" / "profiles.yaml")
    cfg.profiles["demo"].extra["drive"] = str(drive)
    cases = [
        ("Reconcile receivables for September.", "3 items did not match for 2026-09"),
        (
            "Reply to Halleck's renewal ticket with the new annual value from the signed contract, and mark it as waiting on the customer.",
            "I replied on NW-1042",
        ),
        (
            "Answer Priya's email with the renewal value from the signed contract, and offer her a 30-minute call next week.",
            "I sent the reply, but the invitation was not approved.",
        ),
    ]
    for request, expected in cases:
        a = await assemble(
            cfg,
            model=make_model(cfg),
            approvals=AutoApprovals(lambda r: r.kind == "consent" or "mail" in r.tool or "ticketing" in r.tool),
        )
        try:
            res = await a.harness.run(request)
        finally:
            await a.aclose()
        assert res.status == "finished" and res.answer.startswith(expected), (request, res.answer)
