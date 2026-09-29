"""Stage 2 end to end: the real control plane and the real agent.

Done when: an admin can revoke a grant mid-run from the console and the run stops.

Starts the TypeScript control plane, signs in through device verification, pairs
this agent with a one-time code, grants files, starts a run from the web API,
waits until the run is stopped at an approval, revokes the grant as an admin, and
checks the run stops on the agent and is recorded as stopped by the control plane.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from conftest import base_config

from ondo_agent import log as L
from ondo_agent.connection import ControlPlaneAgent, pair
from ondo_agent.demo.policies import renewal_pack_policy
from ondo_agent.log import EventLog
from ondo_agent.models.gateway import scripted_client

ROOT = Path(__file__).resolve().parents[2]
TSX = ROOT / "node_modules" / ".bin" / "tsx"
PASSWORD = "quartermaster-demo"

pytestmark = pytest.mark.skipif(
    not TSX.exists() or shutil.which("node") is None, reason="control plane not installed (npm install)"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    env = {
        **os.environ,
        "ONDO_DEV": "1",
        "ONDO_SEED": "demo",
        "PORT": str(port),
        "ONDO_DB": str(tmp_path / "cp.sqlite"),
        "ONDO_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "ONDO_WEB_DIST": str(tmp_path / "no-web"),
    }
    proc = subprocess.Popen(
        [str(TSX), "src/main.ts"], cwd=ROOT / "server", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError(proc.stdout.read().decode() if proc.stdout else "server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


async def _sign_in(c: httpx.AsyncClient, email: str) -> None:
    r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.json()["stage"] == "needs_device"
    outbox = (await c.get("/api/dev/outbox")).json()
    code = re.search(r"code is (\d{6})", outbox[0]["body"]).group(1)
    assert (await c.post("/api/auth/verify", json={"code": code, "trust": True})).json()["stage"] == "verified"


async def _wait(pred, timeout=15.0, every=0.1):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = await pred()
        if v:
            return v
        await asyncio.sleep(every)
    raise AssertionError("timed out")


async def test_admin_revokes_a_grant_mid_run_and_the_run_stops(server, drive, tmp_path):
    async with httpx.AsyncClient(base_url=server) as mara, httpx.AsyncClient(base_url=server) as admin:
        await _sign_in(mara, "mara.okonjo@northwind-ops.com")

        # Pair with a one-time code, as the PairAgent screen does.
        code = (await mara.post("/api/pairing", json={})).json()["code"]
        creds = await pair(server, code, tmp_path / "agent.json")
        cfg = base_config(drive, tmp_path, grants={})
        agent = ControlPlaneAgent(
            cfg, creds, model_factory=lambda: scripted_client(renewal_pack_policy(drive), name="scripted")
        )
        conn = asyncio.create_task(agent.run_forever())
        try:
            await _wait(lambda: _connected(mara))
            # Nothing is granted by pairing; grant files for the client drive.
            r = await mara.put(
                f"/api/agents/{creds.agent_id}/grants/files", json={"granted": True, "scope": [str(drive)]}
            )
            assert r.status_code == 200 and r.json()["delivered"]
            await _wait(lambda: _async(agent.state.grants["files"].granted))

            run_id = (
                await mara.post("/api/runs", json={"request": "Build the Q3 renewal pack for Northwind."})
            ).json()["run_id"]
            # The run reads the drive and stops at the first write, waiting for a person.
            pending = await _wait(lambda: _pending(mara))
            assert pending[0]["run_id"] == run_id and "file_write" in pending[0]["effects"]

            await _sign_in(admin, "it.admin@northwind-ops.com")
            r = await admin.put(f"/api/agents/{creds.agent_id}/grants/files", json={"granted": False})
            assert r.status_code == 200

            detail = await _wait(lambda: _status(mara, run_id, "stopped"))
            assert "files grant was revoked by an administrator" in detail["run"]["reason"]
            types = [e["type"] for e in detail["events"]]
            assert types[0] == L.RUN_STARTED and types[-1] == L.RUN_STOPPED
            assert L.GRANT_CHANGED in types
            assert [a["status"] for a in detail["approvals"]] == ["expired"]
        finally:
            agent.stop()
            conn.cancel()

    # The agent's own log agrees, and nothing was written.
    log = EventLog.open(cfg.runs_dir / f"{run_id}.jsonl")
    stopped = log.of_type(L.RUN_STOPPED)[-1]
    assert stopped.source == "broker:admin:it.admin@northwind-ops.com"
    assert not (drive / "Q3_Renewal_Pack.xlsx").exists()
    assert not any(e.data.get("op") == "edited" for e in log.of_type(L.FILE_ACCESS))

    # And the control plane's audit log has the revocation, the stop, and who did each.
    async with httpx.AsyncClient(base_url=server, cookies=admin.cookies) as a2:
        audit = (await a2.get("/api/admin/audit", params={"limit": 1000})).json()
    actions = [(r["actor"], r["action"]) for r in audit]
    assert ("user:it.admin@northwind-ops.com", "grant.revoked") in actions
    assert any(a == "agent.run.stopped" for _, a in actions)
    assert any(a == "agent.file.read" for _, a in actions)


async def _async(v):
    return v


async def _connected(c: httpx.AsyncClient):
    s = (await c.get("/api/pairing/status")).json()
    return s.get("paired") and s["agent"]["connected"]


async def _pending(c: httpx.AsyncClient):
    return (await c.get("/api/approvals", params={"status": "pending"})).json()


async def _status(c: httpx.AsyncClient, run_id: str, status: str):
    d = (await c.get(f"/api/runs/{run_id}")).json()
    return d if d["run"]["status"] == status else None


async def test_desktop_run_through_the_control_plane(server, desktop, drive, tmp_path):
    """Stage 4 through the whole stack: capabilities reported, windows granted by
    name, the submit approved from the web, window actions in the audit log."""
    from ondo_agent.demo.policies import legacy_app_policy

    saved = tmp_path / "saved.json"
    app = desktop.launch("Legacy billing", out=saved)
    async with httpx.AsyncClient(base_url=server) as mara:
        await _sign_in(mara, "mara.okonjo@northwind-ops.com")
        code = (await mara.post("/api/pairing", json={})).json()["code"]
        creds = await pair(server, code, tmp_path / "agent.json")
        cfg = base_config(drive, tmp_path, grants={}, desktop={"enabled": True, "escape_twice": True})
        agent = ControlPlaneAgent(
            cfg, creds, model_factory=lambda: scripted_client(legacy_app_policy(drive), name="scripted")
        )
        conn = asyncio.create_task(agent.run_forever())
        try:
            await _wait(lambda: _connected(mara))
            caps = (await mara.get("/api/pairing/status")).json()["agent"]["capabilities"]
            assert caps["screen"] and caps["desktop"] and not caps["pixels"]
            # Policy-excluded windows cannot be granted, whoever asks.
            r = await mara.put(
                f"/api/agents/{creds.agent_id}/grants/screen", json={"granted": True, "scope": ["Password manager"]}
            )
            assert r.status_code == 403
            for kind, scope in (("files", [str(drive)]), ("screen", ["Legacy billing"]), ("input", [])):
                assert (
                    await mara.put(
                        f"/api/agents/{creds.agent_id}/grants/{kind}", json={"granted": True, "scope": scope}
                    )
                ).status_code == 200
            await _wait(lambda: _async(agent.state.grants["input"].granted))

            run_id = (
                await mara.post("/api/runs", json={"request": "Update Halleck in the legacy billing app."})
            ).json()["run_id"]
            pending = await _wait(lambda: _pending(mara))
            assert pending[0]["effects"] == ["submits_to_system_of_record"]
            assert pending[0]["values"][0]["before"] == "184500" and pending[0]["values"][0]["after"] == "193725"
            assert (await mara.post(f"/api/approvals/{pending[0]['id']}", json={"approved": True})).status_code == 200
            detail = await _wait(lambda: _status(mara, run_id, "finished"))
            assert detail["run"]["answer"] == "The billing app says: Saved 193725."
            async with httpx.AsyncClient(base_url=server) as admin:
                await _sign_in(admin, "it.admin@northwind-ops.com")
                audit = (await admin.get("/api/admin/audit", params={"limit": 1000})).json()
            acted = [r for r in audit if r["action"] == "agent.window.acted"]
            assert [r["detail"]["action"] for r in acted] == ["set_text", "click"]
        finally:
            agent.stop()
            conn.cancel()
            app.terminate()
    assert json.loads(saved.read_text())["annual_value"] == "193725"


async def _screen(c: httpx.AsyncClient, agent_id: str, pred):
    s = (await c.get(f"/api/agents/{agent_id}/screen")).json()["screen"]
    return s if s is not None and pred(s) else None


async def test_ask_about_this_screen_through_the_control_plane(server, desktop, drive, tmp_path):
    """Stage 5's screen watching through the whole stack: the agent says which
    shared window is in front, a run started "about this screen" gets what that
    window shows before its first turn, and Escape twice stops the watching."""
    from desktop_env import screen_available

    from ondo_agent.desktop.model import Window
    from ondo_agent.models.adapters.scripted import say

    if not screen_available():
        pytest.skip("needs tesseract and gi-cairo")
    app = desktop.launch_remote()
    seen: list[str] = []

    def policy(messages, tools):
        seen.extend(m.text for m in messages if m.role == "user")
        m = re.search(r"Status: ([^\n<]*)", "\n".join(seen))
        return say(f"The session says: {m.group(1).strip()}." if m else "I cannot see a status.")

    async with httpx.AsyncClient(base_url=server) as mara:
        await _sign_in(mara, "mara.okonjo@northwind-ops.com")
        code = (await mara.post("/api/pairing", json={})).json()["code"]
        creds = await pair(server, code, tmp_path / "agent.json")
        cfg = base_config(
            drive, tmp_path, grants={}, desktop={"enabled": True, "escape_twice": True}, screen={"enabled": True}
        )
        agent = ControlPlaneAgent(cfg, creds, model_factory=lambda: scripted_client(policy, supports_vision=False))
        conn = asyncio.create_task(agent.run_forever())
        try:
            await _wait(lambda: _connected(mara))
            caps = (await mara.get("/api/pairing/status")).json()["agent"]["capabilities"]
            assert caps["pixels"] is True
            r = await mara.put(
                f"/api/agents/{creds.agent_id}/grants/screen", json={"granted": True, "scope": ["Remote billing"]}
            )
            assert r.status_code == 200
            # Bring the remote session to the front, as the user would.
            from ondo_agent.desktop.atspi import AtspiBackend
            from ondo_agent.screen.x11 import X11Screen

            win: Window = next(w for w in AtspiBackend().windows() if w.title.startswith("Remote billing"))
            X11Screen().activate(win)
            front = await _wait(lambda: _screen(mara, creds.agent_id, lambda s: s["window"] == win.label))
            assert front["watching"] is True

            run_id = (
                await mara.post(
                    "/api/runs", json={"request": "What does the status say?", "context": {"window": win.label}}
                )
            ).json()["run_id"]
            detail = await _wait(lambda: _status(mara, run_id, "finished"))
            assert detail["run"]["answer"] == "The session says: Not saved."
            injected = [e for e in detail["events"] if e["source"] == "harness.screen_context"]
            assert [e["type"] for e in injected] == [L.WINDOW_ACCESS, L.CONTEXT_INJECTION]
            # The control plane got the screenshot's hash, never its pixels.
            [img] = injected[1]["data"]["images"]
            assert len(img["sha256"]) == 64 and "data_b64" not in json.dumps(detail)

            # Escape twice pauses watching; only the web resumes it.
            desktop.keys("Escape", "Escape")
            await _wait(lambda: _screen(mara, creds.agent_id, lambda s: not s["watching"]))
            assert (await mara.post(f"/api/agents/{creds.agent_id}/watch", json={"on": True})).status_code == 200
            await _wait(lambda: _screen(mara, creds.agent_id, lambda s: s["watching"] and s["window"] == win.label))
        finally:
            agent.stop()
            conn.cancel()
            app.terminate()


async def test_connector_consent_and_revocation_through_the_control_plane(server, drive, tmp_path):
    """Stage 6's consent through the whole stack: the first use of the ticketing
    connector is allowed from the web, recorded by the control plane and not asked
    again; revoking it from the web stops a run that is using it."""
    import sys

    from ondo_agent.demo.policies import ticket_reply_policy
    from ondo_agent.models.adapters.scripted import call, say

    store = tmp_path / "tickets.json"
    second = iter(
        [
            call(("ticketing_get_ticket", {"id": "NW-1043"})),
            call(("ticketing_add_comment", {"id": "NW-1043", "body": "Looking into it.", "public": True})),
            say("done"),
        ]
    )
    policies = iter([ticket_reply_policy(drive), lambda m, t: next(second)])
    cfg = base_config(
        drive,
        tmp_path,
        grants={},
        connectors={
            "ticketing": {"command": [sys.executable, "-m", "ondo_agent.demo.ticketing_server", "--store", str(store)]}
        },
    )
    async with httpx.AsyncClient(base_url=server) as mara:
        await _sign_in(mara, "mara.okonjo@northwind-ops.com")
        code = (await mara.post("/api/pairing", json={})).json()["code"]
        creds = await pair(server, code, tmp_path / "agent.json")
        agent = ControlPlaneAgent(cfg, creds, model_factory=lambda: scripted_client(next(policies)))
        conn = asyncio.create_task(agent.run_forever())
        try:
            await _wait(lambda: _connected(mara))
            me = (await mara.get("/api/me")).json()
            assert [(c["id"], c["allowed_by_policy"], c["consent"]) for c in me["agents"][0]["connectors"]] == [
                ("ticketing", True, None)
            ]
            await mara.put(f"/api/agents/{creds.agent_id}/grants/files", json={"granted": True, "scope": [str(drive)]})
            await _wait(lambda: _async(agent.state.grants["files"].granted))

            run_id = (await mara.post("/api/runs", json={"request": "Reply to Halleck's renewal ticket."})).json()[
                "run_id"
            ]
            kinds = []
            for _ in range(3):  # consent, then the reply, then the status change
                [p] = await _wait(lambda: _pending(mara))
                kinds.append((p["kind"], p["connector"], p["effects"]))
                assert (await mara.post(f"/api/approvals/{p['id']}", json={"approved": True})).status_code == 200
                await _wait(lambda pid=p["id"]: _gone(mara, pid))
            assert kinds == [
                ("consent", "ticketing", []),
                ("effect", None, ["sends_externally"]),
                ("effect", None, ["submits_to_system_of_record"]),
            ]
            await _wait(lambda: _status(mara, run_id, "finished"))
            consent = (await mara.get("/api/me")).json()["agents"][0]["connectors"][0]["consent"]
            assert consent["by"] == "mara.okonjo@northwind-ops.com"
            assert json.loads(store.read_text())["NW-1042"]["status"] == "pending"

            # The second run is not asked again; revoking while it waits stops it.
            run2 = (await mara.post("/api/runs", json={"request": "Update the Pemberton ticket."})).json()["run_id"]
            [p] = await _wait(lambda: _pending(mara))
            assert p["kind"] == "effect" and p["run_id"] == run2
            r = await mara.delete(f"/api/agents/{creds.agent_id}/connectors/ticketing")
            assert r.status_code == 200 and r.json()["delivered"]
            detail = await _wait(lambda: _status(mara, run2, "stopped"))
            assert detail["run"]["reason"] == "Consent for ticketing was revoked by the user."
            assert not [c for c in json.loads(store.read_text())["NW-1043"]["comments"]]
            async with httpx.AsyncClient(base_url=server) as admin:
                await _sign_in(admin, "it.admin@northwind-ops.com")
                actions = [r["action"] for r in (await admin.get("/api/admin/audit", params={"limit": 1000})).json()]
            for a in ("connector.consented", "connector.revoked", "agent.connector.read", "agent.connector.changed"):
                assert a in actions, a
        finally:
            agent.stop()
            conn.cancel()


async def _gone(c: httpx.AsyncClient, approval_id: str):
    return not any(p["id"] == approval_id for p in (await c.get("/api/approvals", params={"status": "pending"})).json())


async def _crash(agent: ControlPlaneAgent, conn: asyncio.Task) -> None:
    """End an agent the way a killed process does: runs cut off, no final events."""
    for t in list(agent._tasks):
        t.cancel()
    agent.stop()
    conn.cancel()
    await asyncio.gather(conn, *agent._tasks, return_exceptions=True)
    if agent.connectors is not None:
        await agent.connectors.aclose()


async def test_a_run_survives_the_agent_restarting(server, drive, tmp_path):
    """Stage 6's long-running tasks through the whole stack. The agent is killed
    while a reconciliation runs on the ledger's side and while another run waits
    for approval. A new agent process, with the same credentials and run folder,
    reconnects: the control plane has it resume both from their logs. The first
    waits for the same reconciliation and finishes; the second's old approval is
    expired and nothing it proposed happened."""
    from ondo_agent.models.adapters.scripted import call, say

    def server_cmd(c):
        return [
            "{python}",
            "-m",
            "ondo_agent.demo.workplace_servers",
            "--connector",
            c,
            "--store",
            str(tmp_path / f"{c}.json"),
        ]

    cfg = base_config(
        drive,
        tmp_path,
        grants={},
        policy={"allowed_connectors": ["ledger", "documents"]},
        connectors={
            "ledger": {"command": server_cmd("ledger"), "env": {"ONDO_LEDGER_SECONDS": "4"}, "poll_seconds": 0.3},
            "documents": {"command": server_cmd("documents")},
        },
    )

    def first_model(m, t):
        # Each run asks for one call and never gets past it: the agent is killed first.
        if any("Reconcile receivables" in x.text for x in m if x.role == "user"):
            return call(("ledger_start_reconciliation", {"period": "2026-09"}))
        return call(("documents_update_document", {"id": "doc-71", "content": "Emptied.\n"}))

    def second_model(m, t):
        last = [x for x in m if x.role == "tool"][-1]
        if "UNKNOWN CREDIT" in last.text:
            return say("Three items did not match.")
        return say("The change was never approved, so I left the tracker as it was.")

    async with httpx.AsyncClient(base_url=server) as mara:
        await _sign_in(mara, "mara.okonjo@northwind-ops.com")
        code = (await mara.post("/api/pairing", json={})).json()["code"]
        creds = await pair(server, code, tmp_path / "agent.json")
        agent = ControlPlaneAgent(cfg, creds, model_factory=lambda: scripted_client(first_model))
        conn = asyncio.create_task(agent.run_forever())
        await _wait(lambda: _connected(mara))
        recon = (await mara.post("/api/runs", json={"request": "Reconcile receivables for September."})).json()[
            "run_id"
        ]
        edit = (await mara.post("/api/runs", json={"request": "Clear the renewals tracker."})).json()["run_id"]
        # Each run is allowed its connector, then one waits on the ledger and one on a person.
        allowed: set[str] = set()
        deadline = time.monotonic() + 20
        while len(allowed) < 2:
            if time.monotonic() > deadline:
                runs = [(await mara.get(f"/api/runs/{r}")).json() for r in (recon, edit)]
                raise AssertionError(
                    [
                        (
                            d["run"]["status"],
                            d["run"]["reason"],
                            [(e["type"], str(e["data"])[:160]) for e in d["events"][-4:]],
                        )
                        for d in runs
                    ]
                )
            for p in await _wait(lambda: _pending(mara)):
                if p["kind"] == "consent" and p["id"] not in allowed:
                    await mara.post(f"/api/approvals/{p['id']}", json={"approved": True})
                    allowed.add(p["id"])
            await asyncio.sleep(0.1)

        async def effect_waiting():
            return [p for p in await _pending(mara) if p["kind"] == "effect"]

        [waiting] = await _wait(effect_waiting)
        assert waiting["run_id"] == edit

        async def operation_running():
            evs = (await mara.get(f"/api/runs/{recon}")).json()["events"]
            return any(e["type"] == "operation_started" for e in evs)

        await _wait(operation_running)
        await _crash(agent, conn)

        # A new process: same credentials, same run folder.
        agent2 = ControlPlaneAgent(cfg, creds, model_factory=lambda: scripted_client(second_model))
        conn2 = asyncio.create_task(agent2.run_forever())
        try:
            done = await _wait(lambda: _status(mara, recon, "finished"), timeout=30)
            assert done["run"]["answer"] == "Three items did not match."
            types = [e["type"] for e in done["events"]]
            assert types.count("run_started") == 1 and types.count("operation_started") == 1
            assert "run_restored" in types and types[-1] == "run_finished"
            second = await _wait(lambda: _status(mara, edit, "finished"), timeout=30)
            assert second["run"]["answer"] == "The change was never approved, so I left the tracker as it was."
            assert [a["status"] for a in second["approvals"] if a["kind"] == "effect"] == ["expired"]
            ops = [
                r for r in json.loads((tmp_path / "ledger.json").read_text()).values() if r.get("kind") == "operation"
            ]
            assert len(ops) == 1
            assert "Awaiting PO" in json.loads((tmp_path / "documents.json").read_text())["doc-71"]["content"]
            for run_id in (recon, edit):
                assert EventLog.open(tmp_path / "runs" / f"{run_id}.jsonl").verify_chain()
            async with httpx.AsyncClient(base_url=server) as admin:
                await _sign_in(admin, "it.admin@northwind-ops.com")
                actions = [r["action"] for r in (await admin.get("/api/admin/audit", params={"limit": 1000})).json()]
            for a in ("run.resumed", "agent.run.restored", "agent.operation.started", "agent.operation.finished"):
                assert a in actions, a
        finally:
            await _crash(agent2, conn2)


async def test_it_enrolls_an_agent_that_does_nothing_until_its_person_confirms(server, drive, tmp_path):
    """Deployment without a pairing code: the admin makes an enrollment token for
    device management; the agent enrolls itself for the signed-in person; it gets
    no tasks and no grants until that person confirms the computer is theirs."""
    from ondo_agent.connection import Credentials, enroll

    async with httpx.AsyncClient(base_url=server) as admin, httpx.AsyncClient(base_url=server) as mara:
        await _sign_in(admin, "it.admin@northwind-ops.com")
        await _sign_in(mara, "mara.okonjo@northwind-ops.com")
        tok = (await admin.post("/api/admin/enrollment-tokens", json={"label": "Intune"})).json()
        assert tok["token"].startswith("ondo_enr_")
        assert (await mara.post("/api/admin/enrollment-tokens", json={})).status_code == 403

        try:
            await enroll(server, "ondo_enr_wrong", "mara.okonjo@northwind-ops.com", tmp_path / "x.json")
            raise AssertionError("a wrong token enrolled")
        except RuntimeError as e:
            assert "not valid" in str(e)
        try:
            await enroll(server, tok["token"], "nobody@northwind-ops.com", tmp_path / "x.json")
            raise AssertionError("an unknown person was enrolled")
        except RuntimeError as e:
            assert "No active person" in str(e)

        creds = await enroll(server, tok["token"], "Mara.Okonjo@northwind-ops.com", tmp_path / "agent.json")
        assert Credentials.load(tmp_path / "agent.json").agent_id == creds.agent_id
        agent = ControlPlaneAgent(
            base_config(drive, tmp_path, grants={}), creds, model_factory=lambda: scripted_client(lambda m, t: None)
        )
        conn = asyncio.create_task(agent.run_forever())
        try:
            me = (await mara.get("/api/me")).json()
            [pending] = [a for a in me["agents"] if a["id"] == creds.agent_id]
            assert pending["confirmed"] == 0
            # Until confirmed: no tasks go to it, and nothing can be granted to it.
            r = await mara.post("/api/runs", json={"request": "Read the renewals workbook."})
            assert r.status_code == 409
            r = await mara.put(
                f"/api/agents/{creds.agent_id}/grants/files", json={"granted": True, "scope": [str(drive)]}
            )
            assert r.status_code == 409 and "Confirm" in r.json()["error"]

            assert (await mara.post(f"/api/agents/{creds.agent_id}/confirm", json={"mine": True})).json()["confirmed"]
            await _wait(lambda: _connected(mara))
            r = await mara.put(
                f"/api/agents/{creds.agent_id}/grants/files", json={"granted": True, "scope": [str(drive)]}
            )
            assert r.status_code == 200

            # A second computer enrolled in Mara's name that she does not recognise.
            other = await enroll(server, tok["token"], "mara.okonjo@northwind-ops.com", tmp_path / "other.json")
            assert (await mara.post(f"/api/agents/{other.agent_id}/confirm", json={"mine": False})).status_code == 200
            ids = [a["id"] for a in (await mara.get("/api/me")).json()["agents"]]
            assert other.agent_id not in ids and ids[0] == creds.agent_id

            # Revoked tokens enroll nothing more.
            assert (await admin.delete(f"/api/admin/enrollment-tokens/{tok['id']}")).status_code == 200
            try:
                await enroll(server, tok["token"], "mara.okonjo@northwind-ops.com", tmp_path / "y.json")
                raise AssertionError("a revoked token enrolled")
            except RuntimeError as e:
                assert "not valid" in str(e)
            [listed] = (await admin.get("/api/admin/enrollment-tokens")).json()
            assert listed["uses"] == 2 and listed["revoked_at"]
            actions = [r["action"] for r in (await admin.get("/api/admin/audit", params={"limit": 1000})).json()]
            for a in (
                "admin.enrollment_token_created",
                "agent.enrolled",
                "agent.confirmed",
                "agent.rejected",
                "admin.enrollment_token_revoked",
            ):
                assert a in actions, a
        finally:
            agent.stop()
            conn.cancel()
