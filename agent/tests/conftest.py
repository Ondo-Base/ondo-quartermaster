from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from ondo_agent.approvals import AutoApprovals
from ondo_agent.demo import northwind
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.types import Message
from ondo_agent.runtime import Config

# A developer's or runner's own device-management policy must never leak into tests.
os.environ["ONDO_IGNORE_MANAGED"] = "1"


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    return northwind.build(tmp_path / "home")


def base_config(drive: Path, tmp_path: Path, **over: Any) -> Config:
    raw: dict[str, Any] = {
        "agent": {"runs_dir": str(tmp_path / "runs"), "decision_labels": str(tmp_path / "decisions.jsonl")},
        "grants": {"files": {"granted": True, "folders": [str(drive)]}},
        "policy": {
            "excluded_paths": ["**/HR/**", "**/*payroll*"],
            "excluded_windows": ["Personal mail", "HR portal", "Password manager"],
        },
        "decision": {"provider": "rules"},
        "gates": {"rules": []},
        "budget": {"max_steps": 20},
    }
    for k, v in over.items():
        raw[k] = v
    return Config.from_dict(raw, tmp_path)


def last_tool_texts(messages: list[Message]) -> list[str]:
    """Tool results since the last assistant turn."""
    out: list[str] = []
    for m in reversed(messages):
        if m.role == "tool":
            out.append(m.text)
        elif m.role == "assistant":
            break
    return list(reversed(out))


def spreadsheet_question_policy(drive: Path):
    """Behaves like a sensible model for "why did row 14 not match": read, then answer from what it read."""

    def policy(messages: list[Message], tools) -> Any:
        turns = sum(1 for m in messages if m.role == "assistant")
        if turns == 0:
            return call(("read_file", {"path": str(drive / "Q3_Renewals.xlsx")}))
        text = "\n".join(last_tool_texts(messages))
        m = re.search(r"A14: ([^|]+) \|.*?E14: ([0-9.]+)", text)
        if not m:
            return say("I could not find row 14.")
        return say(f"Row 14 is {m.group(1).strip()}. The workbook has a {m.group(2)}% uplift.")

    return policy


def pytest_collection_modifyitems(config, items):
    """In CI the desktop tests must run: a skip there would hide a broken stack."""
    import os

    if not os.environ.get("ONDO_REQUIRE_DESKTOP"):
        return
    from desktop_env import available, screen_available

    missing = [n for n, ok in (("desktop", available()), ("screen", screen_available())) if not ok]
    if missing:
        raise pytest.UsageError(f"ONDO_REQUIRE_DESKTOP is set but the {' and '.join(missing)} stack is missing")


@pytest.fixture(scope="session")
def desktop():
    """One virtual desktop per test session: pyatspi binds to the first session bus
    a process sees, so every desktop test must share it."""
    from desktop_env import available, virtual_desktop

    if not available():
        pytest.skip("needs Xvfb, D-Bus, AT-SPI and xdotool (Linux)")
    from ondo_agent.desktop import hotkey

    with virtual_desktop() as d:
        yield d
        hotkey.shutdown()


@pytest.fixture
def approve_all() -> AutoApprovals:
    return AutoApprovals(True, by="tester")
