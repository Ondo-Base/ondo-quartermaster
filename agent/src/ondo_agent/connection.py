"""The agent's connection to the control plane. The agent dials out.

No inbound ports on user machines, no firewall exceptions, no VPN: one
authenticated websocket, held open, reconnecting with backoff.

Agent -> server: ``hello``, ``event`` (every log event, streamed as it is
written; the server stores them idempotently by run and sequence), ``run_status``.

Server -> agent: ``start_run``, ``approval``, ``grants`` (a grant changed; applied
to every running run immediately, which is how an admin revocation stops a run),
``policy``, ``stop_run``.

The server is the system of record for identity, policy and the audit log. It
owns nothing that runs on the desktop: every enforcement decision still happens
here, in the broker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import websockets

from .approvals import QueueApprovals
from .log import RUN_STOPPED, EventLog, unfinished_runs
from .models.gateway import ModelClient
from .permissions import Consent, Grant, PermissionBroker, Policy
from .runtime import Config, assemble, make_broker

log = logging.getLogger("ondo.agent")


@dataclass
class Credentials:
    server: str
    agent_id: str
    token: str

    @classmethod
    def load(cls, path: Path) -> Credentials:
        d = json.loads(Path(path).read_text())
        return cls(d["server"], d["agent_id"], d["token"])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2))
        path.chmod(0o600)


def device_info() -> dict[str, str]:
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
    }


async def pair(server: str, code: str, creds_path: Path) -> Credentials:
    """Exchange a one-time pairing code (shown in the web UI) for an agent token."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(server.rstrip("/") + "/api/agent/pair", json={"code": code, "device": device_info()})
        r.raise_for_status()
        d = r.json()
    creds = Credentials(server.rstrip("/"), d["agent_id"], d["token"])
    creds.save(creds_path)
    return creds


def signed_in_user() -> str:
    """The address of the person signed in to this computer, for enrollment.

    ``ONDO_USER_EMAIL`` wins (set by a deployment script that knows better). On
    Windows it is the user principal name of an Entra ID or domain account, which
    is what SCIM provisions as the person's address. Elsewhere there is no
    reliable source, so it must be given."""
    import os
    import subprocess
    import sys

    if os.environ.get("ONDO_USER_EMAIL"):
        return os.environ["ONDO_USER_EMAIL"].strip()
    if sys.platform == "win32":  # pragma: no cover - UNTESTED: needs a domain-joined or Entra-joined PC
        try:
            out = subprocess.run(["whoami", "/upn"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if "@" in out:
            return out
    raise RuntimeError("cannot tell who is signed in: set ONDO_USER_EMAIL to their work address")


async def enroll(server: str, enrollment_token: str, user: str, creds_path: Path) -> Credentials:
    """Set this computer up for ``user`` with the organisation's enrollment token
    (from device management). The agent it gets does nothing until the person
    confirms, in the web, that this computer is theirs."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            server.rstrip("/") + "/api/agent/enroll",
            json={"token": enrollment_token, "user": user, "device": device_info()},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"enrollment refused: {r.json().get('error', r.text)}")
        d = r.json()
    creds = Credentials(server.rstrip("/"), d["agent_id"], d["token"])
    creds.save(creds_path)
    return creds


@dataclass
class DeviceState:
    """Grants and policy as the control plane last sent them."""

    grants: dict[str, Grant] = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)
    # Connectors the person has allowed, as the control plane records them.
    consents: dict[str, Consent] = field(default_factory=dict)

    def broker(self) -> PermissionBroker:
        p = self.policy
        return PermissionBroker(
            {k: Grant(g.kind, g.granted, list(g.scope)) for k, g in self.grants.items()},
            Policy(
                list(p.excluded_paths),
                list(p.excluded_windows),
                list(p.disabled_grants),
                p.writes_require_approval,
                list(p.allowed_connectors),
            ),
            {k: Consent(c.connector, c.by, c.at) for k, c in self.consents.items()},
        )


class ControlPlaneAgent:
    def __init__(self, cfg: Config, creds: Credentials, *, model_factory: Callable[[], ModelClient] | None = None):
        self.cfg = cfg
        self.creds = creds
        self.model_factory = model_factory
        self.state = DeviceState()
        self.approvals = QueueApprovals()
        self.runs: dict[str, PermissionBroker] = {}
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()
        # One browser per agent, so a persistent profile keeps the user signed in
        # to their portals between runs. Its origin allowlist follows org policy.
        self.browser = None
        if cfg.section("browser").get("enabled"):
            from .browser.session import BrowserSession

            self.browser = BrowserSession.from_config(cfg.section("browser"), cfg)
        # One set of connectors per agent, shared by its runs, like the browser.
        self.connectors = None
        if cfg.section("connectors"):
            from .connectors.service import ConnectorService

            self.connectors = ConnectorService.from_config(cfg.section("connectors"))
        # Screen watching: which shared window is in front (see ``watch.py``).
        self.watcher = None
        self._watch_task: asyncio.Task | None = None
        # Local config is the starting point until the control plane sends grants.
        b = make_broker(cfg)
        self.state.grants = b.grants
        self.state.policy = b.policy
        self.state.consents = dict(b.consents)
        # Consents the control plane has confirmed, and ones given in a run that it
        # has not echoed yet. Only a confirmed consent can be revoked by omission.
        self._confirmed: set[str] = set()
        self._pending_consents: set[str] = set()

    # -- outbound ---------------------------------------------------------------

    def send(self, msg: dict[str, Any]) -> None:
        self.outbox.put_nowait(msg)

    def _hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "agent_id": self.creds.agent_id,
            "device": device_info(),
            "grants": {k: {"granted": g.granted, "scope": g.scope} for k, g in self.state.grants.items()},
            "capabilities": self._capabilities(),
            # Runs this process is serving, and logs a previous process left without
            # an end. The control plane answers with resume_run or abandon_run.
            "running": sorted(self.runs),
            "unfinished": sorted(r for r in unfinished_runs(self.cfg.runs_dir) if r not in self.runs),
        }

    def _capabilities(self) -> dict[str, Any]:
        desktop = bool(self.cfg.section("desktop").get("enabled"))
        browser = bool(self.cfg.section("browser").get("enabled"))
        return {
            "files": True,
            "browser": browser,
            "desktop": desktop,
            "desktop_backend": self.cfg.section("desktop").get("backend", "auto") if desktop else None,
            # What the screen and input grants actually unlock on this agent.
            "screen": desktop,
            "input": desktop or browser,
            # The connectors configured here, and what each can do. Consent is the person's.
            "connectors": self.connectors.summaries() if self.connectors else [],
            # Pixels: the screen rung (screenshots, pointer and keyboard), where enabled.
            "pixels": desktop and bool(self.cfg.section("screen").get("enabled")),
            # Which settings device management set here (names only; never the token).
            "managed": sorted(k for k in self.cfg.section("managed") if k != "EnrollmentToken"),
        }

    # -- inbound ----------------------------------------------------------------

    async def handle(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t in ("start_run", "resume_run"):
            if msg.get("run_id") in self.runs:
                return  # already being served by this process
            task = asyncio.create_task(self._run(msg, resume=t == "resume_run"))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif t == "abandon_run":
            self._abandon(str(msg.get("run_id", "")), str(msg.get("reason", "")))
        elif t == "approval":
            self.approvals.resolve(
                msg["approval_id"], bool(msg["approved"]), msg.get("by", "unknown"), msg.get("note", "")
            )
        elif t == "grants":
            self._apply_grants(msg)
        elif t == "policy":
            policy = msg.get("policy") or {}
            # The organisation's policy, then this device's management on top: it only narrows.
            from .managed import narrow_policy

            policy = narrow_policy(policy, self.cfg.section("managed"))
            self.state.policy = Policy.from_dict(policy)
            allowed = self.state.policy.allowed_connectors
            for broker in self.runs.values():
                broker.policy.allowed_connectors = list(allowed)
                for cid in [c for c in broker.consents if c not in allowed]:
                    broker.revoke_connector(cid, by="admin", reason="not allowed by policy")
                    broker.stop(f"Your administrator no longer allows {cid}.", by="admin")
            if self.browser is not None and policy.get("allowed_origins"):
                self.browser.origins.allowed = list(policy["allowed_origins"])
        elif t in ("stop_run", "pause_run", "resume_run"):
            b = self.runs.get(msg.get("run_id", ""))
            if b and t == "stop_run":
                b.stop(msg.get("reason", "stopped from the web"), by=msg.get("by", "user"))
            elif b and t == "pause_run":
                b.pause(by=msg.get("by", "user"))
            elif b:
                b.resume(by=msg.get("by", "user"))
        elif t == "watch" and self.watcher is not None:
            if msg.get("on"):
                self.watcher.resume()
            else:
                self.watcher.pause("paused from the web")
        elif t == "ping":
            self.send({"type": "pong"})

    def _apply_grants(self, msg: dict[str, Any]) -> None:
        by = msg.get("by", "control-plane")
        if isinstance(msg.get("connectors"), dict):
            self._apply_consents(msg["connectors"], by, msg.get("reason", "revoked"))
        for kind, g in (msg.get("grants") or {}).items():
            if kind not in ("files", "screen", "input"):
                continue
            granted, scope = bool(g.get("granted")), list(g.get("scope", []))
            self.state.grants[kind] = Grant(kind, granted, scope)  # type: ignore[arg-type]
            for broker in self.runs.values():
                current = broker.grants[kind]
                if current.granted and not granted:
                    reason = msg.get("reason", "revoked")
                    broker.revoke(kind, by=by, reason=reason)  # type: ignore[arg-type]
                    # The run was planned with this grant; it stops rather than
                    # carrying on with less than it was started with.
                    broker.stop(f"The {kind} grant was {reason}.", by=by)
                elif granted and (not current.granted or current.scope != scope):
                    try:
                        broker.grant(kind, scope, by=by)  # type: ignore[arg-type]
                    except Exception as e:  # policy refuses: stays revoked
                        log.warning("grant refused by policy: %s", e)

    def _apply_consents(self, given: dict[str, Any], by: str, reason: str) -> None:
        # A consent given in a run moments ago may not be recorded yet when some
        # other grant change is pushed: absent-but-pending is not a revocation.
        self._pending_consents -= set(given)
        revoked = {c for c in self._confirmed if c not in given}
        self._confirmed = set(given)
        kept = {k: c for k, c in self.state.consents.items() if k in self._pending_consents}
        self.state.consents = {
            **kept,
            **{k: Consent(k, str(v.get("by", "")), float(v.get("at", 0)) / 1000) for k, v in given.items()},
        }
        for broker in self.runs.values():
            for cid in [c for c in broker.consents if c in revoked]:
                broker.revoke_connector(cid, by=by, reason=reason)
                # The run was planned with this connector: it stops rather than carry on without it.
                broker.stop(f"Consent for {cid} was {reason}.", by=by)
            for cid, c in self.state.consents.items():
                if cid not in broker.consents and broker.connector_allowed(cid):
                    broker.consents[cid] = Consent(cid, c.by, c.at)

    def _abandon(self, run_id: str, reason: str) -> None:
        """Close the log of a run that was stopped while this agent was offline."""
        path = unfinished_runs(self.cfg.runs_dir).get(run_id)
        if path is None or run_id in self.runs:
            return
        lg = EventLog.open(path)
        lg.subscribe(lambda e: self.send({"type": "event", "event": json.loads(e.to_json())}))
        lg.append(RUN_STOPPED, "control-plane", {"reason": reason, "status": "stopped"})

    async def _run(self, msg: dict[str, Any], resume: bool = False) -> None:
        run_id = msg.get("run_id")
        path = unfinished_runs(self.cfg.runs_dir).get(str(run_id)) if resume else None
        if resume and path is None:
            self.send({"type": "run_status", "run_id": run_id, "status": "error", "reason": "No record of this task."})
            return
        broker = self.state.broker()

        def consent_changed(kind: str, data: dict[str, Any]) -> None:
            # A yes given in this run holds for the next one before the control plane echoes it.
            cid = str(data.get("kind", "")).removeprefix("connector:")
            if kind == "granted" and str(data.get("kind", "")).startswith("connector:") and cid in broker.consents:
                self.state.consents[cid] = broker.consents[cid]
                self._pending_consents.add(cid)

        broker.on_change(consent_changed)
        log_ = EventLog.open(path) if path is not None else EventLog.create(self.cfg.runs_dir, run_id)
        self.runs[log_.run_id] = broker
        # A resumed run sends its whole log again: events the old process had not
        # delivered arrive now, and the control plane ignores the ones it has.
        for e in log_.events:
            self.send({"type": "event", "event": json.loads(e.to_json())})
        log_.subscribe(lambda e: self.send({"type": "event", "event": json.loads(e.to_json())}))
        self.send({"type": "run_status", "run_id": log_.run_id, "status": "running"})
        model = self.model_factory() if self.model_factory else None
        a = await assemble(
            self.cfg,
            model=model,
            approvals=self.approvals,
            log=log_,
            broker=broker,
            user=msg.get("user", "user"),
            services={k: v for k, v in (("browser", self.browser), ("connectors", self.connectors)) if v is not None},
        )
        try:
            if path is not None:
                res = await a.harness.resume()
            else:
                a.harness.start(msg["request"])
                if (msg.get("context") or {}).get("window"):
                    from .watch import inject_screen_context

                    await inject_screen_context(a.harness, str(msg["context"]["window"]))
                res = await a.harness.run()
            self.send(
                {
                    "type": "run_status",
                    "run_id": log_.run_id,
                    "status": res.status,
                    "answer": res.answer,
                    "reason": res.reason,
                }
            )
        except Exception as e:
            log.exception("run failed")
            self.send({"type": "run_status", "run_id": log_.run_id, "status": "error", "reason": str(e)})
        finally:
            await a.aclose()  # the shared browser is a service, not the run's to close
            self.runs.pop(log_.run_id, None)

    # -- connection loop ----------------------------------------------------------

    def _start_watcher(self) -> None:
        d = self.cfg.section("desktop")
        if self._watch_task is not None or not d.get("enabled") or self.cfg.section("screen").get("watch") is False:
            return
        from .desktop.session import DesktopSession
        from .watch import ScreenWatcher

        try:
            desktop = DesktopSession.from_config(d)
        except Exception as e:  # no accessibility stack here: nothing to watch with
            log.warning("screen watching unavailable: %s", e)
            return
        self.watcher = ScreenWatcher(
            desktop, self.state.broker, self.send, escape_twice=bool(d.get("escape_twice", True))
        )
        self._watch_task = asyncio.create_task(self.watcher.run())

    async def run_forever(self) -> None:
        self._start_watcher()
        url = self.creds.server.replace("http://", "ws://").replace("https://", "wss://") + "/agent/ws"
        backoff = 1.0
        while not self._stopping:
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"authorization": f"Bearer {self.creds.token}"},
                    ping_interval=20,
                    max_size=16 * 2**20,
                ) as ws:
                    backoff = 1.0
                    await ws.send(json.dumps(self._hello()))
                    if self.watcher is not None:
                        self.watcher.resend()
                    sender = asyncio.create_task(self._sender(ws))
                    try:
                        async for raw in ws:
                            await self.handle(json.loads(raw))
                    finally:
                        sender.cancel()
            except (OSError, websockets.WebSocketException) as e:
                log.warning("control plane connection lost: %s; retrying in %.0fs", e, backoff)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _sender(self, ws) -> None:
        while True:
            msg = await self.outbox.get()
            try:
                await ws.send(json.dumps(msg, default=str))
            except Exception:
                # Put it back for the next connection; the server dedupes events.
                self.outbox.put_nowait(msg)
                raise

    def stop(self) -> None:
        self._stopping = True
        if self._watch_task is not None:
            self._watch_task.cancel()
        if self.browser is not None:
            asyncio.ensure_future(self.browser.aclose())
        if self.connectors is not None:
            asyncio.ensure_future(self.connectors.aclose())
        for b in self.runs.values():
            b.stop("agent shutting down", by="agent")
