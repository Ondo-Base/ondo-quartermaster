"""The browser: web portals through the accessibility tree.

Shown here: a task spanning the document store and a web portal completes with no
screenshots in the transcript, and passes with a text-only model, proving the rung.

These tests drive a real Chromium through Playwright MCP. They skip when either
is missing.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from conftest import base_config

from ondo_agent import log as L
from ondo_agent.approvals import AutoApprovals
from ondo_agent.browser.session import default_command
from ondo_agent.demo.northwind import ACCOUNTS, TOTAL_INCREASE
from ondo_agent.demo.policies import renewal_submit_policy
from ondo_agent.demo.portal import Portal
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.gateway import scripted_client
from ondo_agent.models.types import ImagePart
from ondo_agent.runtime import assemble

CHROMIUM = os.environ.get("ONDO_CHROMIUM", "/opt/pw-browsers/chromium")


def _available() -> bool:
    cmd = default_command()
    return (
        Path(CHROMIUM).exists()
        and (cmd[0] != "npx" or shutil.which("npx") is not None)
        and shutil.which("node") is not None
    )


pytestmark = pytest.mark.skipif(not _available(), reason="Playwright MCP or Chromium not available")


def browser_config(drive, tmp_path, portal: Portal, **over):
    return base_config(
        drive,
        tmp_path,
        grants={"files": {"granted": True, "folders": [str(drive)]}, "input": {"granted": True}},
        browser={
            "enabled": True,
            "allowed_origins": [portal.url],
            "executable_path": CHROMIUM,
            "no_sandbox": True,
            "headless": True,
            "output_dir": str(tmp_path / "pw-out"),
        },
        gates={
            "thresholds_file": str(Path(__file__).resolve().parents[1] / "config" / "thresholds.json"),
            "rules": [
                {
                    "effect": "submits_to_system_of_record",
                    "action": "require",
                    "name": "billing-portal-submit",
                    "url": portal.url + "/*",
                    "element": r'^button "(Submit|Save)',
                }
            ],
        },
        **over,
    )


REQUEST = "Key the Q3 renewal changes from the signed contracts into the billing portal. Stop before submitting."


async def test_document_store_to_web_portal_with_a_text_only_model(drive, tmp_path):
    approvals = AutoApprovals(True, by="mara.okonjo")
    with Portal() as portal:
        cfg = browser_config(drive, tmp_path, portal)
        # A text-only profile: no vision. The harness never sends it an image.
        model = scripted_client(renewal_submit_policy(drive, portal.url), name="text-only", supports_vision=False)
        a = await assemble(cfg, model=model, approvals=approvals)
        try:
            res = await a.harness.run(REQUEST)
        finally:
            await a.aclose()

        assert res.status == "finished", (res, [e.data for e in a.log.of_type(L.TOOL_RESULT)][-1])
        assert res.answer == "The billing portal saved 6 change(s) from the signed contracts."
        # The portal really changed, by exactly the contract figures.
        expected = {x.name: x.new_value for x in ACCOUNTS}
        assert portal.state.values == expected
        assert sum(portal.state.values.values()) - sum(x.current for x in ACCOUNTS) == TOTAL_INCREASE
        assert len(portal.state.submissions) == 1

    # One approval, gated by the deterministic rule, showing the exact before -> after values.
    [req] = approvals.seen
    assert req.effects == ["submits_to_system_of_record"]
    halleck = next(v for v in req.values if v.label == "Annual value — Halleck Logistics")
    assert (halleck.before, halleck.after) == ("184500", "193725")
    assert len([v for v in req.values if v.before is not None]) == 6
    gate = next(e for e in a.log.of_type(L.GATE) if e.data["required"])
    assert gate.data["by_effect"]["submits_to_system_of_record"]["source"] == "rule:billing-portal-submit"
    # Approval came before the click reached the portal.
    approved = a.log.of_type(L.APPROVAL_RESOLVED)[0]
    click_result = next(e for e in a.log.of_type(L.TOOL_RESULT) if e.data["name"] == "browser_click")
    assert approved.seq < click_result.seq

    # No screenshots anywhere in the transcript.
    names = {e.data["name"] for e in a.log.of_type(L.TOOL_CALL)}
    assert not any("screenshot" in n for n in names)
    assert "browser_take_screenshot" not in {t.name for t in a.harness.tool_list}
    for msgs, _ in model.adapter.requests:
        assert not any(isinstance(p, ImagePart) for m in msgs for p in m.parts)
    assert all(e.data.get("detail", {}).get("images_dropped", 0) == 0 for e in a.log.of_type(L.TOOL_RESULT))
    # Page content was fenced and screened like any file.
    web = [e for e in a.log.of_type(L.SCREENING) if e.data["origin"].startswith("web:")]
    assert web and not any(e.data["flagged"] for e in web)


async def test_refused_submission_leaves_the_portal_untouched(drive, tmp_path):
    with Portal() as portal:
        cfg = browser_config(drive, tmp_path, portal)
        a = await assemble(
            cfg,
            model=scripted_client(renewal_submit_policy(drive, portal.url)),
            approvals=AutoApprovals(False, by="mara.okonjo"),
        )
        try:
            res = await a.harness.run(REQUEST)
        finally:
            await a.aclose()
        assert "not approved" in res.answer
        assert portal.state.submissions == []
        assert portal.state.values == {x.name: x.current for x in ACCOUNTS}


async def test_origins_are_enforced_before_and_after_navigation(drive, tmp_path):
    with Portal() as other, Portal() as portal:
        portal.state.help_url = other.url + "/"  # a different origin, linked from the allowed one
        cfg = browser_config(drive, tmp_path, portal)

        def policy(messages, tools):
            turns = sum(1 for m in messages if m.role == "assistant")
            last = next((m.text for m in reversed(messages) if m.role == "tool"), "")
            if turns == 0:
                return call(("browser_navigate", {"url": other.url + "/renewals"}))
            if turns == 1:
                return call(("browser_navigate", {"url": portal.url + "/"}))
            if turns == 2:
                ref = re.search(r'link "Help centre" \[ref=([A-Za-z0-9_-]+)\]', last).group(1)
                return call(("browser_click", {"ref": ref, "element": "Help centre link"}))
            return say("done")

        a = await assemble(cfg, model=scripted_client(policy), approvals=AutoApprovals(True))
        try:
            await a.harness.run("look around")
        finally:
            await a.aclose()
    denied = a.log.of_type(L.PERMISSION_DENIED)
    assert [d.data["reason"] for d in denied] == ["origin_not_allowed", "origin_not_allowed"]
    assert denied[0].data["target"].startswith(other.url)  # refused before navigating
    # Left after a link took it there. Playwright's own --allowed-origins usually
    # blocks the request first (a chrome-error page); either way we do not stay.
    assert denied[1].data["target"].startswith((other.url, "chrome-error:"))
    results = a.log.of_type(L.TOOL_RESULT)
    assert results[0].data["is_error"] and results[2].data["is_error"]


async def test_revoking_input_mid_run_stops_the_browser(drive, tmp_path):
    with Portal() as portal:
        cfg = browser_config(drive, tmp_path, portal)
        holder = {}

        def policy(messages, tools):
            turns = sum(1 for m in messages if m.role == "assistant")
            if turns == 0:
                return call(("browser_navigate", {"url": portal.url + "/renewals"}))
            if turns == 1:
                holder["a"].broker.revoke("input", by="admin@northwind-ops.com", reason="revoked from console")
                return call(("browser_snapshot", {}))
            return say("stopped")

        a = await assemble(cfg, model=scripted_client(policy), approvals=AutoApprovals(True))
        holder["a"] = a
        try:
            await a.harness.run("look")
        finally:
            await a.aclose()
    last = a.log.of_type(L.TOOL_RESULT)[-1]
    assert last.data["is_error"] and "input grant is not active" in last.data["content"]
