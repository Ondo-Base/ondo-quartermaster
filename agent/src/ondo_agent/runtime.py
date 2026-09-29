"""Assemble a harness from configuration.

Everything that must hold (grants, exclusions, gate rules, thresholds, budgets)
comes from here, never from prompt text. The same config drives the CLI, the
control-plane connection and the tests.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .approvals import ApprovalProvider, TerminalApprovals
from .decision.logged import LoggedDecisionModel, make_decision_model
from .gates import GateKeeper, load_rules
from .harness.loop import Budget, Harness
from .log import EventLog
from .models.gateway import ModelClient
from .models.profile import ModelProfile, load_profiles
from .permissions import ConsentStore, Grant, PermissionBroker, Policy
from .screening import Screener
from .tools.files import file_tools
from .tools.spec import ToolSpec

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


@dataclass
class Config:
    raw: dict[str, Any]
    base_dir: Path
    profiles: dict[str, ModelProfile] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path)
        raw = _expand(yaml.safe_load(path.read_text()) or {})
        cfg = cls(raw, path.parent.resolve())
        prof = raw.get("models", {}).get("profiles_file")
        if prof:
            cfg.profiles = load_profiles(cfg.resolve(prof))
        for name, body in (raw.get("models", {}).get("profiles") or {}).items():
            cfg.profiles[name] = ModelProfile.from_dict({"name": name, **body})
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_dir: Path | None = None) -> Config:
        cfg = cls(_expand(raw), (base_dir or Path.cwd()).resolve())
        for name, body in (raw.get("models", {}).get("profiles") or {}).items():
            cfg.profiles[name] = ModelProfile.from_dict({"name": name, **body})
        return cfg

    def resolve(self, p: str) -> Path:
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else (self.base_dir / q).resolve()

    @property
    def runs_dir(self) -> Path:
        return self.resolve(self.raw.get("agent", {}).get("runs_dir", ".ondo/runs"))

    @property
    def consents_path(self) -> Path:
        return self.resolve(self.raw.get("agent", {}).get("consents_file", ".ondo/consents.json"))

    @property
    def labels_path(self) -> Path:
        return self.resolve(self.raw.get("agent", {}).get("decision_labels", ".ondo/decisions.jsonl"))

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}


def make_broker(cfg: Config) -> PermissionBroker:
    g = cfg.section("grants")
    files = g.get("files") or {}
    screen = g.get("screen") or {}
    inp = g.get("input") or {}
    grants = {
        "files": Grant("files", bool(files.get("granted")), [str(cfg.resolve(f)) for f in files.get("folders", [])]),
        "screen": Grant("screen", bool(screen.get("granted")), list(screen.get("windows", []))),
        "input": Grant("input", bool(inp.get("granted"))),
    }
    policy = Policy.from_dict(cfg.section("policy"))
    store = ConsentStore(cfg.consents_path)
    broker = PermissionBroker(grants, policy, store.load())
    store.follow(broker)
    return broker


def make_model(cfg: Config, name: str | None = None) -> ModelClient:
    m = cfg.section("models")
    name = name or m.get("orchestrator")
    if not name or name not in cfg.profiles:
        raise ValueError(f"model profile {name!r} not configured (have: {', '.join(cfg.profiles) or 'none'})")
    profile = cfg.profiles[name]
    if profile.adapter == "scripted":
        from .demo.policies import demo_router
        from .models.adapters.scripted import ScriptedModel

        return ModelClient(profile, ScriptedModel(demo_router(cfg, profile), name=profile.name))
    fallbacks = [ModelClient(cfg.profiles[f]) for f in m.get("fallbacks", []) if f != name]
    return ModelClient(profile, fallbacks=fallbacks)


def make_decision(cfg: Config) -> LoggedDecisionModel:
    return LoggedDecisionModel(make_decision_model(cfg.section("decision")), cfg.labels_path)


def make_gates(cfg: Config, decision: LoggedDecisionModel) -> GateKeeper:
    g = cfg.section("gates")
    thresholds: dict[str, float] = {}
    if g.get("thresholds_file"):
        tf = cfg.resolve(g["thresholds_file"])
        if tf.exists():
            thresholds = GateKeeper.load_thresholds(tf)
    thresholds.update(g.get("thresholds") or {})
    return GateKeeper(load_rules(g.get("rules")), decision, thresholds)


def make_tools(cfg: Config) -> list[ToolSpec]:
    tools = file_tools()
    b = cfg.section("browser")
    if b.get("enabled"):
        from .browser.tools import browser_tools

        tools += browser_tools()
    if cfg.section("desktop").get("enabled"):
        from .desktop.tools import desktop_tools

        tools += desktop_tools()
        if cfg.section("screen").get("enabled"):
            from .screen.tools import screen_tools

            tools += screen_tools()
    return tools


def _escape_twice(broker: PermissionBroker):
    """Start the Escape-twice listener for this run. If it cannot start, input
    control is not safe to hand over, so the input grant is revoked instead."""
    import logging

    from .desktop import hotkey

    def trigger() -> None:
        broker.release_input()
        broker.stop("Escape pressed twice", by="user")

    try:
        return hotkey.register(trigger)
    except Exception as e:
        logging.getLogger("ondo.agent").warning("Escape-twice unavailable (%s); input control is off", e)
        broker.revoke("input", by="agent", reason="escape_twice_unavailable")
        return lambda: None


@dataclass
class Assembled:
    harness: Harness
    broker: PermissionBroker
    log: EventLog
    cleanup: list[Any]

    async def aclose(self) -> None:
        for c in self.cleanup:
            try:
                r = c()
                if hasattr(r, "__await__"):
                    await r
            except Exception:  # one failing cleanup must not skip the rest
                logging.getLogger("ondo.agent").warning("cleanup failed", exc_info=True)


async def assemble(
    cfg: Config,
    *,
    model: ModelClient | None = None,
    approvals: ApprovalProvider | None = None,
    log: EventLog | None = None,
    broker: PermissionBroker | None = None,
    user: str = "local-user",
    services: dict[str, Any] | None = None,
) -> Assembled:
    broker = broker or make_broker(cfg)
    decision = make_decision(cfg)
    screening_cfg = cfg.section("screening")
    screener = (
        Screener(decision, threshold=float(screening_cfg.get("threshold", 0.5)))
        if screening_cfg.get("enabled", True)
        else None
    )
    gates = make_gates(cfg, decision)
    model = model or make_model(cfg)
    log = log if log is not None else EventLog.create(cfg.runs_dir)
    b = cfg.section("budget")
    tools = make_tools(cfg)
    cleanup: list[Any] = [model.aclose]
    services: dict[str, Any] = dict(services or {})
    if cfg.section("browser").get("enabled") and "browser" not in services:
        from .browser.session import BrowserSession

        session = BrowserSession.from_config(cfg.section("browser"), cfg)
        services["browser"] = session
        cleanup.append(session.aclose)
    d = cfg.section("desktop")
    if d.get("enabled"):
        if "desktop" not in services:
            from .desktop.session import DesktopSession

            services["desktop"] = DesktopSession.from_config(d)
        if cfg.section("screen").get("enabled") and "screen" not in services:
            from .screen.session import ScreenSession

            services["screen"] = ScreenSession.from_config(cfg.section("screen"), services["desktop"])
            cleanup.append(services["screen"].aclose)
        if d.get("escape_twice", True):
            cleanup.append(_escape_twice(broker))
    if cfg.section("connectors"):
        svc = services.get("connectors")
        if svc is None:
            from .connectors.service import ConnectorService

            svc = ConnectorService.from_config(cfg.section("connectors"))
            services["connectors"] = svc
            cleanup.append(svc.aclose)
        # Only what policy allows is started, and only what is running is offered.
        await svc.start(only=[c for c in svc.decls if broker.connector_allowed(c)])
        tools = tools + svc.tool_specs(broker)
    harness = Harness(
        model=model,
        tools=tools,
        log=log,
        broker=broker,
        gates=gates,
        approvals=approvals or TerminalApprovals(user),
        screener=screener,
        budget=Budget(int(b.get("max_steps", 40)), int(b.get("max_tokens", 1_000_000))),
        config=cfg.raw,
        user=user,
        services=services,
    )
    return Assembled(harness, broker, log, cleanup)
