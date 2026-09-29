"""Stage 6: settings from the organisation's device management.

What Intune or Group Policy writes under HKLM\\SOFTWARE\\Policies\\Ondo\\Quartermaster
(or a macOS configuration profile) is read here through an injected reader, so
the rules are tested on any platform: managed settings name the control plane
and carry the enrollment token, and otherwise only ever narrow.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ondo_agent import managed
from ondo_agent.connection import ControlPlaneAgent, Credentials
from ondo_agent.runtime import Config, make_broker

# What winreg returns for the ADMX template's values: REG_SZ, REG_MULTI_SZ, REG_DWORD.
REGISTRY = {
    "ControlPlaneUrl": "https://ondo.northwind-ops.com",
    "EnrollmentToken": "ondo_enr_example",
    "AllowedConnectors": ["ticketing", "mail"],
    "DisabledGrants": "input",
    "ExcludedPaths": "**/Finance/Payroll/**;**/*.kdbx",
    "DisableScreenWatching": 1,
    "SomethingElse": "ignored",
}


def test_values_are_read_as_the_template_writes_them():
    m = managed.load(lambda: REGISTRY)
    assert m == {
        "ControlPlaneUrl": "https://ondo.northwind-ops.com",
        "EnrollmentToken": "ondo_enr_example",
        "AllowedConnectors": ["ticketing", "mail"],
        "DisabledGrants": ["input"],
        "ExcludedPaths": ["**/Finance/Payroll/**", "**/*.kdbx"],
        "DisableScreenWatching": True,
    }
    assert managed.load(lambda: {}) == {}

    def broken():
        raise PermissionError("access denied")

    assert managed.load(broken) == {}  # unreadable: behave as unmanaged, do not fail to start


def test_managed_settings_only_narrow():
    local = {
        "excluded_paths": ["**/HR/**"],
        "disabled_grants": [],
        "allowed_connectors": ["ticketing", "calendar", "documents"],
    }
    m = managed.normalise(REGISTRY | {"DisabledConnectors": ["documents"]})
    p = managed.narrow_policy(local, m)
    assert p["excluded_paths"] == ["**/HR/**", "**/Finance/Payroll/**", "**/*.kdbx"]
    assert p["disabled_grants"] == ["input"]
    # "mail" is allowed by device management but not by the local policy: still not allowed.
    assert p["allowed_connectors"] == ["ticketing"]
    # An empty AllowedConnectors allows nothing; absent, it leaves the policy alone.
    assert managed.narrow_policy(local, {"AllowedConnectors": []})["allowed_connectors"] == []
    assert managed.narrow_policy(local, {})["allowed_connectors"] == local["allowed_connectors"]


def test_the_agent_config_and_the_control_plane_policy_both_obey_it(tmp_path, monkeypatch):
    raw = {
        "agent": {"runs_dir": str(tmp_path / "runs")},
        "policy": {"allowed_connectors": ["ticketing", "calendar"], "excluded_paths": []},
        "connectors": {"ticketing": {"command": ["x"]}, "calendar": {"command": ["y"]}},
        "screen": {"enabled": True, "watch": True},
    }
    path = tmp_path / "ondo.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(
        managed, "load", lambda reader=None: managed.normalise(REGISTRY | {"DisabledConnectors": ["calendar"]})
    )
    cfg = Config.load(path)
    assert cfg.section("agent")["control_plane"] == "https://ondo.northwind-ops.com"
    assert cfg.section("agent")["enrollment_token"] == "ondo_enr_example"
    assert list(cfg.section("connectors")) == ["ticketing"]
    assert cfg.section("screen")["watch"] is False
    b = make_broker(cfg)
    assert b.policy.allowed_connectors == ["ticketing"] and "input" in b.policy.disabled_grants

    # The control plane's organisation policy is narrowed the same way when it arrives.
    agent = ControlPlaneAgent(cfg, Credentials("http://127.0.0.1:9", "agt_x", "t"))
    caps = agent._capabilities()
    assert "EnrollmentToken" not in caps["managed"] and "AllowedConnectors" in caps["managed"]

    async def deliver():
        await agent.handle(
            {
                "type": "policy",
                "policy": {"allowed_connectors": ["ticketing", "mail", "calendar"], "excluded_paths": []},
            }
        )

    import asyncio

    asyncio.run(deliver())
    # Allowed by the organisation and by device management; calendar is disabled on this device.
    assert agent.state.policy.allowed_connectors == ["ticketing", "mail"]
    assert "**/*.kdbx" in agent.state.policy.excluded_paths


def test_unmanaged_is_the_default_in_tests(monkeypatch):
    # conftest sets ONDO_IGNORE_MANAGED, so a developer's own machine policy never leaks in.
    assert managed.load() == {}
    assert Path(managed.MACOS_PLIST).name == "com.ondo.quartermaster.plist"


DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "windows"


def test_the_policy_template_writes_exactly_what_the_agent_reads():
    import xml.etree.ElementTree as ET

    ns = {"p": "http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions"}
    root = ET.parse(DEPLOY / "policy" / "OndoQuartermaster.admx").getroot()
    names = set()
    for pol in root.iterfind(".//p:policy", ns):
        assert pol.get("key") == managed.POLICY_KEY.replace("SOFTWARE", "Software")
        assert pol.get("class") == "Machine"
        names |= {e.get("valueName") for e in pol.iterfind(".//p:text", ns)}
        if pol.get("valueName"):
            names.add(pol.get("valueName"))
    assert names == set(managed.SETTINGS)
    # Every string and presentation the template refers to exists in the English resources.
    adml = ET.parse(DEPLOY / "policy" / "en-US" / "OndoQuartermaster.adml").getroot()
    strings = {s.get("id") for s in adml.iterfind(".//p:string", ns)}
    presentations = {s.get("id") for s in adml.iterfind(".//p:presentation", ns)}
    text = (DEPLOY / "policy" / "OndoQuartermaster.admx").read_text()
    import re

    assert set(re.findall(r"\$\(string\.(\w+)\)", text)) <= strings
    assert set(re.findall(r"\$\(presentation\.(\w+)\)", text)) <= presentations


def test_the_deployed_configuration_loads_and_grants_nothing(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    shutil.copy(DEPLOY / "ondo.yaml", tmp_path / "ondo.yaml")
    shutil.copy(Path(__file__).resolve().parents[1] / "config" / "profiles.yaml", tmp_path / "profiles.yaml")
    cfg = Config.load(tmp_path / "ondo.yaml")
    assert cfg.runs_dir == tmp_path / "AppData" / "Local" / "Ondo" / "runs"
    assert "gateway" in cfg.profiles
    b = make_broker(cfg)
    assert not any(g.granted for g in b.grants.values()) and b.policy.allowed_connectors == []
