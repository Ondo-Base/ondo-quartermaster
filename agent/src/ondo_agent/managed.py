"""Settings an organisation's device management sets, which the agent obeys.

IT sets these through Intune (the ADMX template in ``deploy/windows``, ingested
as a custom policy or through the Settings Catalog), Group Policy, or on macOS a
configuration profile. They land where every managed application reads them:

- Windows: ``HKLM\\SOFTWARE\\Policies\\Ondo\\Quartermaster`` (machine) and the same
  path under HKCU (user); machine wins.
- macOS: ``/Library/Managed Preferences/com.ondo.quartermaster.plist``.

Managed settings only ever narrow. They can name the control plane and carry an
enrollment token; add exclusions and disabled grants; and limit connectors. They
cannot grant anything, allow a path policy excludes, or widen what the control
plane's organisation policy allows. The person using the computer cannot change
them: the keys are writable only by administrators.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

POLICY_KEY = r"SOFTWARE\Policies\Ondo\Quartermaster"
MACOS_PLIST = Path("/Library/Managed Preferences/com.ondo.quartermaster.plist")

# Value name -> (config meaning, kind). Lists are REG_MULTI_SZ, or one string with
# entries separated by semicolons (what a single ADMX text box produces).
SETTINGS: dict[str, str] = {
    "ControlPlaneUrl": "text",
    "EnrollmentToken": "text",
    "AllowedConnectors": "list",
    "DisabledConnectors": "list",
    "DisabledGrants": "list",
    "ExcludedPaths": "list",
    "ExcludedWindows": "list",
    "DisableScreenWatching": "flag",
    "DisablePixels": "flag",
}

Reader = Callable[[], dict[str, Any]]


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = v.replace("\n", ";").split(";")
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def normalise(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, kind in SETTINGS.items():
        if name not in values or values[name] is None:
            continue
        v = values[name]
        if kind == "text":
            out[name] = str(v).strip()
        elif kind == "list":
            out[name] = _as_list(v)
        else:
            out[name] = str(v).strip().lower() in ("1", "true", "yes") if not isinstance(v, (bool, int)) else bool(v)
    return out


def read_windows() -> dict[str, Any]:  # pragma: no cover - needs Windows; UNTESTED
    import winreg  # type: ignore[import-not-found]

    found: dict[str, Any] = {}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):  # machine last: it wins
        try:
            key = winreg.OpenKey(hive, POLICY_KEY)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                found[name] = value
                i += 1
    return found


def read_macos() -> dict[str, Any]:  # pragma: no cover - needs a managed Mac; UNTESTED
    import plistlib

    try:
        with open(MACOS_PLIST, "rb") as f:
            return dict(plistlib.load(f))
    except (OSError, ValueError):
        return {}


def load(reader: Reader | None = None) -> dict[str, Any]:
    """The managed settings on this device, normalised. Empty when unmanaged, or
    when ``ONDO_IGNORE_MANAGED`` is set (tests; never set by an installer)."""
    if reader is None:
        if os.environ.get("ONDO_IGNORE_MANAGED"):
            return {}
        reader = read_windows if sys.platform == "win32" else read_macos if sys.platform == "darwin" else dict
    try:
        return normalise(reader())
    except Exception:  # unreadable policy: behave as unmanaged rather than not start
        return {}


def narrow_policy(policy: dict[str, Any], managed: dict[str, Any]) -> dict[str, Any]:
    """Apply managed settings to a policy (the local one, or the control plane's).
    Every change is a narrowing."""
    p = dict(policy or {})
    for key, name in (
        ("excluded_paths", "ExcludedPaths"),
        ("excluded_windows", "ExcludedWindows"),
        ("disabled_grants", "DisabledGrants"),
    ):
        if managed.get(name):
            p[key] = list(dict.fromkeys([*p.get(key, []), *managed[name]]))
    allowed = list(p.get("allowed_connectors", []))
    if "AllowedConnectors" in managed:
        allowed = [c for c in allowed if c in managed["AllowedConnectors"]]
    if managed.get("DisabledConnectors"):
        allowed = [c for c in allowed if c not in managed["DisabledConnectors"]]
    p["allowed_connectors"] = allowed
    return p


def apply(raw: dict[str, Any], managed: dict[str, Any]) -> dict[str, Any]:
    """The agent's configuration with managed settings applied."""
    if not managed:
        return raw
    out = dict(raw)
    agent = dict(out.get("agent") or {})
    if managed.get("ControlPlaneUrl"):
        agent["control_plane"] = managed["ControlPlaneUrl"]
    if managed.get("EnrollmentToken"):
        agent["enrollment_token"] = managed["EnrollmentToken"]
    out["agent"] = agent
    out["policy"] = narrow_policy(out.get("policy") or {}, managed)
    for c in managed.get("DisabledConnectors", []):
        (out.get("connectors") or {}).pop(c, None)
    screen = dict(out.get("screen") or {})
    if managed.get("DisableScreenWatching"):
        screen["watch"] = False
    if managed.get("DisablePixels"):
        screen["enabled"] = False
    if screen:
        out["screen"] = screen
    out["managed"] = managed  # what was applied, for the hello and the log
    return out
