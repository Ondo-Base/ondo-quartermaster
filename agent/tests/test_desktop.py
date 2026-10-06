"""Desktop control through the accessibility tree.

Shown here: a legacy internal app is driven by element name, not coordinates,
survives the window being moved and the display rescaled, and picks its target
without an orchestrator turn.

Runs a real GTK app on a virtual X display and drives it through AT-SPI (the
Linux backend). The Windows and macOS backends share everything above the
backend but are not exercised here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import base_config
from desktop_env import available

from ondo_agent import log as L
from ondo_agent.approvals import AutoApprovals
from ondo_agent.demo.policies import legacy_app_policy
from ondo_agent.desktop.hotkey import EscapeTwice
from ondo_agent.desktop.model import Element, Locator, StaleElement, Window, apply_profile, number_occurrences
from ondo_agent.desktop.session import DesktopSession
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.gateway import scripted_client
from ondo_agent.runtime import assemble

needs_desktop = pytest.mark.skipif(not available(), reason="needs Xvfb, D-Bus, AT-SPI and xdotool (Linux)")
THRESHOLDS = str(Path(__file__).resolve().parents[1] / "config" / "thresholds.json")


def desktop_config(drive, tmp_path, *, screen=("Legacy billing",), **over):
    return base_config(
        drive,
        tmp_path,
        grants={
            "files": {"granted": True, "folders": [str(drive)]},
            "screen": {"granted": True, "windows": list(screen)},
            "input": {"granted": True},
        },
        policy={"excluded_paths": ["**/HR/**"], "excluded_windows": ["Password manager", "Personal mail", "HR portal"]},
        desktop={"enabled": True, "escape_twice": True, "profiles": {"Legacy billing": {"push button#1": "Refresh"}}},
        gates={
            "thresholds_file": THRESHOLDS,
            "rules": [
                {
                    "effect": "submits_to_system_of_record",
                    "action": "require",
                    "name": "legacy-billing-submit",
                    "app": "Legacy billing",
                    "element": r'^push button "Submit"',
                }
            ],
        },
        **over,
    )


@needs_desktop
@pytest.mark.parametrize("scale", [1, 2])
async def test_legacy_app_by_element_name_survives_move_and_rescale(desktop, drive, tmp_path, scale):
    saved = tmp_path / f"saved-{scale}.json"
    app = desktop.launch("Legacy billing", out=saved, scale=scale)
    try:
        moved = {}

        def move():  # between typing and submitting: move and resize the window
            desktop.move("Legacy billing", 600, 400, 700, 360)
            moved["at"] = time.time()

        approvals = AutoApprovals(True, by="mara.okonjo")
        model = scripted_client(legacy_app_policy(drive, between=move), name="text-only", supports_vision=False)
        a = await assemble(desktop_config(drive, tmp_path), model=model, approvals=approvals)
        try:
            res = await a.harness.run(
                "Update Halleck Logistics' annual value in the legacy billing app from the signed contract."
            )
        finally:
            await a.aclose()
    finally:
        app.terminate()

    assert res.status == "finished", (res, [e.data for e in a.log.of_type(L.TOOL_RESULT)])
    assert res.answer == "The billing app says: Saved 193725."
    assert json.loads(saved.read_text()) == {"account": "Halleck Logistics", "annual_value": "193725"}
    assert moved, "the window was moved mid-run"

    calls = [e.data for e in a.log.of_type(L.TOOL_CALL)]
    # Driven by element name: the model named targets in words, never coordinates or refs.
    acts = [c["arguments"] for c in calls if c["name"] == "desktop_act"]
    assert [x["target"] for x in acts] == ["Annual value field", "the Submit button"]
    assert not any(k in json.dumps(acts) for k in ('"x"', '"y"', "coordinate"))
    # ...and picked its targets without an orchestrator turn: no inspect before acting,
    # the decision layer chose, and each act took exactly one model turn.
    names = [c["name"] for c in calls]
    assert names.index("desktop_inspect") > names.index("desktop_act")
    picks = [e.data for e in a.log.of_type(L.DECISION) if e.data["purpose"] == "element_pick"]
    assert [p["answers"][0]["value"] for p in picks] == ['text "Annual value"', 'push button "Submit"']
    assert len(a.log.of_type(L.MODEL_RESPONSE)) == 5

    # The submit went through the gate, with the exact value that landed.
    [req] = approvals.seen
    assert req.effects == ["submits_to_system_of_record"]
    assert [(v.label, v.before, v.after) for v in req.values] == [('text "Annual value"', "184500", "193725")]
    gate = next(e for e in a.log.of_type(L.GATE) if e.data["required"])
    assert gate.data["by_effect"]["submits_to_system_of_record"]["source"] == "rule:legacy-billing-submit"


@needs_desktop
async def test_windows_follow_the_screen_grant_and_policy(desktop, drive, tmp_path):
    apps = [desktop.launch(t) for t in ("Legacy billing", "Password manager", "Notes")]
    try:
        script = iter(
            [
                call(("desktop_windows", {})),
                call(
                    ("desktop_act", {"window": "Password manager", "target": "Submit button", "action": "click"}),
                    ("desktop_inspect", {"window": "Notes"}),
                    ("desktop_inspect", {"window": "Legacy billing"}),
                ),
                say("done"),
            ]
        )
        a = await assemble(
            desktop_config(drive, tmp_path),
            model=scripted_client(lambda m, t: next(script)),
            approvals=AutoApprovals(True),
        )
        try:
            await a.harness.run("look")
        finally:
            await a.aclose()
    finally:
        for p in apps:
            p.terminate()

    results = [e.data for e in a.log.of_type(L.TOOL_RESULT)]
    listing = results[0]["content"]
    assert "Legacy billing" in listing and "Password manager" not in listing and "Notes" not in listing
    assert "2 other window(s) not shown" in listing
    # The exclusion is logged when it bites, without naming the window.
    assert any(e.data["op"] == "excluded" for e in a.log.of_type(L.WINDOW_ACCESS))
    denied = a.log.of_type(L.PERMISSION_DENIED)
    assert [d.data["reason"] for d in denied] == ["excluded_by_policy"]
    assert results[2]["is_error"] and "No granted window" in results[2]["content"]
    # A per-app profile names the unnamed icon button; the tree is fenced as untrusted.
    tree = results[3]["content"]
    assert 'push button "Refresh"' in tree and tree.startswith('<untrusted_data origin="window:')


@needs_desktop
async def test_vague_targets_are_refused_not_guessed(desktop, drive, tmp_path):
    app = desktop.launch("Legacy billing")
    try:
        script = iter(
            [call(("desktop_act", {"window": "Legacy billing", "target": "the thing", "action": "click"})), say("ok")]
        )
        a = await assemble(
            desktop_config(drive, tmp_path),
            model=scripted_client(lambda m, t: next(script)),
            approvals=AutoApprovals(True),
        )
        try:
            await a.harness.run("click something")
        finally:
            await a.aclose()
    finally:
        app.terminate()
    r = a.log.of_type(L.TOOL_RESULT)[0].data
    assert r["is_error"] and "could not tell which element" in r["content"]
    assert not a.log.of_type(L.APPROVAL_REQUESTED)


@needs_desktop
async def test_escape_twice_takes_the_keyboard_back(desktop, drive, tmp_path):
    saved = tmp_path / "saved.json"
    app = desktop.launch("Legacy billing", out=saved)
    try:

        def press():
            desktop.keys("Escape", "Escape")
            time.sleep(0.5)

        a = await assemble(
            desktop_config(drive, tmp_path),
            approvals=AutoApprovals(True),
            model=scripted_client(legacy_app_policy(drive, between=press)),
        )
        try:
            res = await a.harness.run("Update Halleck in the legacy billing app.")
        finally:
            await a.aclose()
    finally:
        app.terminate()
    assert res.status == "stopped" and res.reason == "Escape pressed twice"
    change = next(e for e in a.log.of_type(L.GRANT_CHANGED) if e.data["kind"] == "input")
    assert change.data["change"] == "revoked" and change.data["reason"] == "escape_twice"
    assert not saved.exists()  # nothing was submitted
    assert a.broker.grants["input"].granted is False


# -- no display needed ---------------------------------------------------------------------


def test_escape_twice_timing():
    hits = []
    w = EscapeTwice(lambda: hits.append(1), window_s=0.3)
    assert not w.press(True)
    assert w.press(True)  # second within the window
    assert not w.press(True)  # a third starts over
    w.press(False)  # any other key resets
    assert not w.press(True)
    time.sleep(0.35)
    assert not w.press(True)  # too slow
    assert hits == [1]


class FlakyBackend:
    """Raises StaleElement on the first action, as macOS does after a UI refresh."""

    name = "flaky"

    def __init__(self):
        self.reads = 0
        self.clicks = 0

    def windows(self):
        return [Window("1#0", "App", "app")]

    def elements(self, window, max_nodes=600):
        self.reads += 1
        return number_occurrences([Element("push button", "Save", actions=("click",), states=frozenset({"enabled"}))])

    def click(self, element):
        self.clicks += 1
        if self.clicks == 1:
            raise StaleElement("gone")

    set_text = focus = click


async def test_stale_elements_are_requeried_once():
    b = FlakyBackend()
    s = DesktopSession(b)
    w = (await s.windows())[0]
    await s.act(w, Locator("push button", "Save", 0), b.click)
    assert b.clicks == 2 and b.reads == 2


def test_profiles_name_unnamed_controls():
    els = number_occurrences([Element("push button", ""), Element("push button", "OK"), Element("push button", "")])
    apply_profile(els, {"push button#2": "Refresh"})
    assert [e.name for e in els] == ["", "OK", "Refresh"]
