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
from .log import EventLog
from .models.gateway import ModelClient
from .permissions import Grant, PermissionBroker, Policy
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


@dataclass
class DeviceState:
    """Grants and policy as the control plane last sent them."""

    grants: dict[str, Grant] = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)

    def broker(self) -> PermissionBroker:
        return PermissionBroker(
            {k: Grant(g.kind, g.granted, list(g.scope)) for k, g in self.grants.items()}, Policy(**self.policy.__dict__)
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
        # Local config is the starting point until the control plane sends grants.
        b = make_broker(cfg)
        self.state.grants = b.grants
        self.state.policy = b.policy

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
            # Pixels: the screen rung (screenshots, pointer and keyboard), where enabled.
            "pixels": desktop and bool(self.cfg.section("screen").get("enabled")),
        }

    # -- inbound ----------------------------------------------------------------

    async def handle(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "start_run":
            task = asyncio.create_task(self._run(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif t == "approval":
            self.approvals.resolve(
                msg["approval_id"], bool(msg["approved"]), msg.get("by", "unknown"), msg.get("note", "")
            )
        elif t == "grants":
            self._apply_grants(msg)
        elif t == "policy":
            policy = msg.get("policy") or {}
            self.state.policy = Policy.from_dict(policy)
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
        elif t == "ping":
            self.send({"type": "pong"})

    def _apply_grants(self, msg: dict[str, Any]) -> None:
        by = msg.get("by", "control-plane")
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

    async def _run(self, msg: dict[str, Any]) -> None:
        run_id = msg.get("run_id")
        broker = self.state.broker()
        log_ = EventLog.create(self.cfg.runs_dir, run_id)
        self.runs[log_.run_id] = broker
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
            services={"browser": self.browser} if self.browser is not None else None,
        )
        try:
            res = await a.harness.run(msg["request"])
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

    async def run_forever(self) -> None:
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
        if self.browser is not None:
            asyncio.ensure_future(self.browser.aclose())
        for b in self.runs.values():
            b.stop("agent shutting down", by="agent")
