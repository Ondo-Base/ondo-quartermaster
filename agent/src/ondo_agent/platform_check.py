"""``ondo-agent platform-check``: does this computer's desktop stack work?

Runs the pieces of the desktop and screen rungs that differ by platform (UI
Automation on Windows, AXUIElement and Quartz on macOS, AT-SPI and X11 on Linux)
against a window you name, and prints one line per check:

    ondo-agent platform-check --window Notepad --type "Ondo platform check"

It reads, and with ``--type`` writes, only the window you name, and takes one
screenshot of it (saved with ``--out``, never sent anywhere). Nothing goes
through a model, the control plane or the gates: this is the backend alone, to
find out whether it works before a task relies on it. See
docs/testing-on-windows-and-mac.md.
"""

from __future__ import annotations

import asyncio
import platform
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEXT_ROLES = ("text", "edit", "entry", "document", "text area", "textarea", "text field")


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, status: str, detail: str = "") -> Check:
        c = Check(name, status, detail)
        self.checks.append(c)
        print(f"{status:<4}  {name}{': ' + detail if detail else ''}", flush=True)
        return c

    @property
    def failed(self) -> bool:
        return any(c.status == "FAIL" for c in self.checks)


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}".strip()


async def run_checks(
    window: str, *, type_text: str = "", out: Path | None = None, backend: str = "auto", screen: bool = True
) -> Report:
    from . import managed
    from .desktop.session import DesktopSession

    r = Report()
    r.add(
        "platform",
        "PASS",
        f"{platform.system()} {platform.release()} ({platform.machine()}), Python {sys.version.split()[0]}",
    )

    # Device management: what Intune, Group Policy or a profile set here (names only).
    try:
        m = managed.load()
        r.add(
            "managed policy",
            "PASS",
            ", ".join(sorted(k for k in m if k != "EnrollmentToken")) or "none set on this computer",
        )
    except Exception as e:
        r.add("managed policy", "FAIL", _why(e))

    # 1. The accessibility backend, and the windows it sees.
    try:
        desktop = DesktopSession.from_config({"backend": backend})
        r.add("desktop backend", "PASS", type(desktop.backend).__name__)
    except Exception as e:
        r.add("desktop backend", "FAIL", _why(e))
        return r
    try:
        wins = await desktop.windows()
        shown = "; ".join(w.label for w in wins[:8]) + (" …" if len(wins) > 8 else "")
        r.add("list windows", "PASS" if wins else "FAIL", f"{len(wins)} found: {shown}" if wins else "none found")
    except Exception as e:
        r.add("list windows", "FAIL", _why(e))
        return r

    # 2. The window named, and its accessibility tree.
    try:
        target = await desktop.window(window, lambda _name: True)
        r.add("find window", "PASS", target.label)
    except Exception as e:
        r.add("find window", "FAIL", f"{_why(e)} (is it open? pass part of its title with --window)")
        return r
    try:
        elements = await desktop.read(target)
        named = [e for e in elements if e.name]
        sample = "; ".join(e.described for e in named[:6])
        r.add(
            "read accessibility tree",
            "PASS" if elements else "FAIL",
            f"{len(elements)} elements, {len(named)} named: {sample}" if elements else "the window exposed nothing",
        )
    except Exception as e:
        r.add("read accessibility tree", "FAIL", _why(e))
        elements = []

    # 3. Writing by element, not by coordinates, then reading it back.
    if type_text:
        field = next(
            (e for e in elements if any(t in e.role.lower() for t in TEXT_ROLES) and e.handle is not None), None
        )
        if field is None:
            r.add("set text by element", "FAIL", "no editable text element in this window")
        else:
            try:
                await asyncio.to_thread(desktop.backend.set_text, field, type_text)
                await asyncio.sleep(0.3)
                after = await desktop.read(target)
                ok = any(type_text in (e.value or "") or type_text in (e.name or "") for e in after)
                r.add(
                    "set text by element",
                    "PASS" if ok else "FAIL",
                    f"into {field.described}" + ("" if ok else "; the new text did not read back"),
                )
            except Exception as e:
                r.add("set text by element", "FAIL", _why(e))
    else:
        r.add("set text by element", "SKIP", "pass --type TEXT to try it (it replaces the field's text)")

    # 4. The screen rung: one screenshot of that window, and local OCR on it.
    if screen:
        try:
            from .screen import ocr
            from .screen.session import ScreenSession

            s = ScreenSession.from_config({"enabled": True, "backend": backend}, desktop)
            try:
                rect, img = await s.capture(target)
                r.add("screenshot", "PASS", f"{img.width}×{img.height} capture pixels for a {rect.w}×{rect.h} window")
                if out is not None:
                    out.mkdir(parents=True, exist_ok=True)
                    path = out / "platform-check.png"
                    img.convert("RGB").save(path)
                    r.add("screenshot saved", "PASS", str(path))
                if s.tesseract:
                    text = await asyncio.to_thread(ocr.read_text, img, binary=s.tesseract)
                    words = " ".join(text.split())[:120]
                    found = (type_text in text) if type_text else bool(words)
                    r.add("OCR", "PASS" if found else "FAIL", f"read: {words!r}" if words else "no text read")
                else:
                    r.add("OCR", "SKIP", "tesseract is not installed or not on PATH")
            finally:
                await s.aclose()
        except Exception as e:
            r.add("screenshot", "FAIL", _why(e))
    else:
        r.add("screenshot", "SKIP", "--no-screen")

    # 5. Escape twice: the key listener that gives the keyboard back.
    try:
        from .desktop.hotkey import EscapeTwice

        listener = await asyncio.to_thread(EscapeTwice(lambda: None).start)
        await asyncio.to_thread(listener.stop)
        r.add("escape-twice listener", "PASS", "started and stopped")
    except Exception as e:
        r.add("escape-twice listener", "FAIL", f"{_why(e)} (without it, input control is switched off)")
    return r


def main(a: Any) -> int:
    try:
        report = asyncio.run(
            run_checks(
                a.window,
                type_text=a.type or "",
                out=Path(a.out) if a.out else None,
                backend=a.backend,
                screen=not a.no_screen,
            )
        )
    except Exception:
        traceback.print_exc()
        return 2
    n = {s: sum(1 for c in report.checks if c.status == s) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped")
    return 1 if report.failed else 0
