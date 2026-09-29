"""``ondo-agent``: run, replay, fork, inspect, pair and connect."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from . import log as L
from .log import EventLog, list_runs, read_events, verify_chain

DEFAULT_CONFIG = "ondo.yaml"
DEFAULT_CREDS = Path.home() / ".ondo" / "agent.json"


def _cfg(path: str):
    from .runtime import Config

    return Config.load(path)


async def _run(a) -> int:
    from .runtime import assemble, make_model

    cfg = _cfg(a.config)
    model = make_model(cfg, a.model) if a.model else None
    asm = await assemble(cfg, model=model)
    try:
        res = await asm.harness.run(a.request)
    finally:
        await asm.aclose()
    print(f"\n[{res.status}] run {res.run_id} · {res.steps} model turns · {res.usage}")
    if res.answer:
        print(res.answer)
    if res.reason:
        print("reason:", res.reason)
    print(f"log: {asm.log.path}")
    return 0 if res.status == "finished" else 1


async def _replay(a) -> int:
    from .approvals import AutoApprovals
    from .models.adapters.scripted import ReplayModel
    from .models.gateway import ModelClient
    from .models.profile import ModelProfile
    from .runtime import assemble

    cfg = _cfg(a.config)
    events = list(read_events(Path(a.log)))
    original = next((e.data.get("answer", "") for e in events if e.type == L.RUN_FINISHED), None)
    approvals = {e.data["approval_id"]: e.data["approved"] for e in events if e.type == L.APPROVAL_RESOLVED}
    model = ModelClient(ModelProfile("replay", "scripted", "replay"), ReplayModel(events))
    # Approvals replay as recorded, in order.
    decisions = list(approvals.values())
    asm = await assemble(
        cfg,
        model=model,
        approvals=AutoApprovals(lambda r: decisions.pop(0) if decisions else False, by="replay"),
        log=EventLog.create(cfg.runs_dir / "replays"),
    )
    try:
        res = await asm.harness.run(events[0].data["request"])
    finally:
        await asm.aclose()
    print(f"replayed {a.log} -> {asm.log.path}")
    print("same answer as recorded" if res.answer == original else f"answer differs:\n{res.answer}")
    return 0


async def _fork(a) -> int:
    from .runtime import assemble, make_model

    cfg = _cfg(a.config)
    src = EventLog.open(Path(a.log))
    child = src.fork(cfg.runs_dir, a.at)
    asm = await assemble(cfg, model=make_model(cfg, a.model), log=child)
    try:
        res = await asm.harness.run()
    finally:
        await asm.aclose()
    print(f"forked {src.run_id}@{a.at} -> {child.run_id} on {a.model or 'default model'}: [{res.status}]")
    print(res.answer or res.reason)
    return 0


def _trajectory(a) -> int:
    events = list(read_events(Path(a.log)))
    print(f"run {events[0].run_id} · {len(events)} events · chain {'ok' if verify_chain(events) else 'BROKEN'}")
    for e in events:
        mark = ">" if e.enters_context else " "
        when = time.strftime("%H:%M:%S", time.localtime(e.ts))
        summary = e.data.get("text") or e.data.get("title") or e.data.get("name") or e.data.get("reason") or ""
        if e.type == L.GATE:
            summary = f"{e.data['description']} -> {'GATED ' + ','.join(e.data['effects']) if e.data['required'] else 'no gate'}"
        if e.type == L.SCREENING:
            summary = f"{e.data['origin']} p={e.data['probability']} {'FLAGGED' if e.data['flagged'] else 'clear'}"
        summary = str(summary).replace("\n", " ")[:110]
        print(f"{mark} {e.seq:4d} {when} {e.type:20s} {e.source:32s} {summary}")
    print("\n> = entered the model's context; source = the component that put it there")
    return 0


def _search(a) -> int:
    cfg = _cfg(a.config)
    for p in list_runs(cfg.runs_dir):
        log = EventLog.open(p)
        for e in log.search(a.text):
            print(f"{log.run_id} {e.seq:4d} {e.type:18s} {json.dumps(e.data, ensure_ascii=False)[:120]}")
    return 0


def _tools_doc(a) -> int:
    from .browser.tools import browser_tools
    from .connectors.catalog import CATALOG, PLAIN
    from .desktop.tools import desktop_tools
    from .screen.tools import screen_tools
    from .tools.files import file_tools
    from .tools.spec import to_markdown

    out = [to_markdown(file_tools() + browser_tools() + desktop_tools() + screen_tools()).rstrip(), ""]
    out += [
        "# Connector tools",
        "",
        "Offered as `<connector>_<tool>`, with the parameters the connector's MCP server declares. Each "
        "connector is allowed by the person on first use, and only if the organisation's policy lists it.",
        "",
    ]
    for c in CATALOG.values():
        out += [
            f"## {c.name} (`{c.id}`)",
            "",
            c.description,
            "",
            "| Tool | Effect | What it does |",
            "| --- | --- | --- |",
        ]
        out += [f"| `{c.id}_{t.name}` | {PLAIN[t.effect]} | {t.description} |" for t in c.tools.values()]
        out.append("")
    print("\n".join(out).rstrip())
    return 0


def _demo_data(a) -> int:
    from .demo import northwind

    d = northwind.build(Path(a.dir))
    print(f"wrote {d}")
    return 0


def _legacy_app(a) -> int:
    import subprocess

    args = [sys.executable, "-m", "ondo_agent.demo.legacy_app", "--title", a.title]
    if a.out:
        args += ["--out", a.out]
    return subprocess.call(args)


def _remote_app(a) -> int:
    import subprocess

    args = [sys.executable, "-m", "ondo_agent.demo.remote_app"]
    if a.out:
        args += ["--out", a.out]
    return subprocess.call(args)


def _portal(a) -> int:
    from .demo.portal import Portal

    with Portal(port=a.port) as p:
        print(f"billing portal on {p.url}  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


def _platform_check(a) -> int:
    from .platform_check import main as check

    return check(a)


async def _pair(a) -> int:
    from .connection import pair

    creds = await pair(a.server, a.code, Path(a.credentials))
    print(f"paired as agent {creds.agent_id}; credentials in {a.credentials}")
    return 0


async def _enroll(a) -> int:
    from .connection import enroll, signed_in_user

    cfg = _cfg(a.config)
    agent_cfg = cfg.section("agent")
    server = a.server or agent_cfg.get("control_plane")
    tok = a.token or agent_cfg.get("enrollment_token")
    if not (server and tok):
        print("no control plane or enrollment token: set them in device management, or pass --server and --token")
        return 2
    creds = await enroll(server, tok, a.user or signed_in_user(), Path(a.credentials))
    print(f"enrolled as agent {creds.agent_id}; confirm this computer in Ondo on the web to start")
    return 0


async def _connect(a) -> int:
    from .connection import ControlPlaneAgent, Credentials, enroll, signed_in_user

    cfg = _cfg(a.config)
    creds_path = Path(a.credentials)
    agent_cfg = cfg.section("agent")
    if not creds_path.exists() and agent_cfg.get("control_plane") and agent_cfg.get("enrollment_token"):
        # Deployed by device management: set itself up for whoever is signed in.
        await enroll(agent_cfg["control_plane"], agent_cfg["enrollment_token"], signed_in_user(), creds_path)
    agent = ControlPlaneAgent(cfg, Credentials.load(creds_path))
    print(f"connecting to {agent.creds.server} as {agent.creds.agent_id}")
    await agent.run_forever()
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="ondo-agent", description="Ondo Quartermaster desktop agent")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run one request in the terminal, approvals asked on stdin")
    p.add_argument("request")
    p.add_argument("--model", help="profile name; overrides models.orchestrator")
    p = sub.add_parser("replay", help="re-run a recorded run against its recorded model responses")
    p.add_argument("log")
    p = sub.add_parser("fork", help="fork a run at a sequence number and finish it on another model")
    p.add_argument("log")
    p.add_argument("--at", type=int, required=True)
    p.add_argument("--model")
    p = sub.add_parser("trajectory", help="print a run's events with the source of every context injection")
    p.add_argument("log")
    p = sub.add_parser("search", help="search every recorded run")
    p.add_argument("text")
    sub.add_parser("tools-doc", help="print the tool documentation generated from the schema")
    p = sub.add_parser("demo-data", help="write the Northwind sample drive")
    p.add_argument("dir")
    p = sub.add_parser("portal", help="serve the sample billing portal")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("legacy-app", help="open the sample legacy billing app (GTK) for the desktop rung")
    p.add_argument("--title", default="Legacy billing")
    p.add_argument("--out", default="")
    p = sub.add_parser("remote-app", help="open the sample remote-session window (pixels only) for the screen rung")
    p.add_argument("--out", default="")
    p = sub.add_parser("pair", help="pair this device with the control plane using the code from the web UI")
    p.add_argument("--server", required=True)
    p.add_argument("--code", required=True)
    p.add_argument("--credentials", default=str(DEFAULT_CREDS))
    p = sub.add_parser("platform-check", help="check this computer's desktop and screen stack against one window")
    p.add_argument("--window", required=True, help="part of the title or app name of a window that is open")
    p.add_argument("--type", default="", help="text to put in its first text field, then read back")
    p.add_argument("--out", default="", help="folder to save the screenshot in")
    p.add_argument("--backend", default="auto", help="auto, uia or ax (desktop); the screen backend follows")
    p.add_argument("--no-screen", action="store_true", help="skip the screenshot and OCR")
    p = sub.add_parser("enroll", help="set this computer up with the organisation's enrollment token")
    p.add_argument("--server", default="", help="default: the ControlPlaneUrl device management set")
    p.add_argument("--token", default="", help="default: the EnrollmentToken device management set")
    p.add_argument("--user", default="", help="the signed-in person's work address (default: detected)")
    p.add_argument("--credentials", default=str(DEFAULT_CREDS))
    p = sub.add_parser("connect", help="hold the connection to the control plane and run tasks from it")
    p.add_argument("--credentials", default=str(DEFAULT_CREDS))
    sub.add_parser(
        "calibrate", help="measure the decision model and write gate thresholds (see --help)", add_help=False
    )

    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["calibrate"]:
        from .decision.calibrate import main as cal

        cal(argv[1:])
        return
    a = ap.parse_args(argv)
    handlers = {"run": _run, "replay": _replay, "fork": _fork, "pair": _pair, "enroll": _enroll, "connect": _connect}
    sync = {
        "platform-check": _platform_check,
        "trajectory": _trajectory,
        "search": _search,
        "tools-doc": _tools_doc,
        "demo-data": _demo_data,
        "portal": _portal,
        "legacy-app": _legacy_app,
        "remote-app": _remote_app,
    }
    if a.cmd in handlers:
        sys.exit(asyncio.run(handlers[a.cmd](a)))
    sys.exit(sync[a.cmd](a))


if __name__ == "__main__":
    main()
