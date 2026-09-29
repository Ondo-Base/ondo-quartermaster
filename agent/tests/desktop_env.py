"""A real, throwaway desktop for the Stage 4 tests: Xvfb, a D-Bus session and the
AT-SPI registry, with GTK apps launched into it. Linux only."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def screen_available() -> bool:
    """The Stage 5 extras on top of the desktop: OCR and cairo for the canvas app."""
    if not available() or not shutil.which("tesseract"):
        return False
    try:
        import gi

        gi.require_foreign("cairo")
        import PIL  # noqa: F401
    except Exception:
        return False
    return True


def available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if not all(shutil.which(b) for b in ("Xvfb", "dbus-daemon", "xdotool")):
        return False
    if not Path("/usr/libexec/at-spi-bus-launcher").exists():
        return False
    try:
        import gi  # noqa: F401
        import pyatspi  # noqa: F401
    except Exception:
        return False
    return True


@contextmanager
def virtual_desktop():
    procs: list[subprocess.Popen] = []
    saved = {k: os.environ.get(k) for k in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "GTK_MODULES", "NO_AT_BRIDGE")}
    display = None
    for n in range(90, 120):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            display = f":{n}"
            break
    assert display, "no free X display"
    try:
        procs.append(
            subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x800x24", "+extension", "RECORD"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{display[1:]}").exists():
                break
            time.sleep(0.1)
        bus = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        procs.append(bus)
        address = bus.stdout.readline().strip()
        os.environ.update(
            {
                "DISPLAY": display,
                "DBUS_SESSION_BUS_ADDRESS": address,
                "GTK_MODULES": "gail:atk-bridge",
                "NO_AT_BRIDGE": "0",
            }
        )
        procs.append(
            subprocess.Popen(
                ["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        time.sleep(1.0)
        yield Desktop(procs)
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Desktop:
    def __init__(self, procs):
        self._procs = procs

    def launch(self, title: str, *, out: Path | None = None, scale: int = 1, value: str = "184500") -> subprocess.Popen:
        env = {**os.environ, "GDK_SCALE": str(scale)}
        args = [sys.executable, "-m", "ondo_agent.demo.legacy_app", "--title", title, "--value", value]
        if out:
            args += ["--out", str(out)]
        p = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        self.wait_window(title)
        return p

    def launch_remote(self, *, out: Path | None = None, scale: int = 1, title: str | None = None) -> subprocess.Popen:
        """The pixels-only stand-in for a Citrix window (``demo/remote_app.py``)."""
        from ondo_agent.demo.remote_app import TITLE

        env = {**os.environ, "GDK_SCALE": str(scale)}
        args = [sys.executable, "-m", "ondo_agent.demo.remote_app", "--title", title or TITLE]
        if out:
            args += ["--out", str(out)]
        p = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        self.wait_window((title or TITLE).split(" — ")[0])
        return p

    def wait_window(self, title: str, timeout: float = 15) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = subprocess.run(["xdotool", "search", "--name", title], capture_output=True, text=True)
            if r.stdout.strip():
                time.sleep(0.8)  # let the accessibility tree register
                return
            time.sleep(0.2)
        raise TimeoutError(f"window {title!r} did not appear")

    def move(self, title: str, x: int, y: int, w: int | None = None, h: int | None = None) -> None:
        wid = subprocess.run(["xdotool", "search", "--name", title], capture_output=True, text=True).stdout.split()[0]
        subprocess.run(["xdotool", "windowmove", wid, str(x), str(y)], check=True)
        if w and h:
            subprocess.run(["xdotool", "windowsize", wid, str(w), str(h)], check=True)
        time.sleep(0.3)

    def keys(self, *keys: str) -> None:
        subprocess.run(["xdotool", "key", "--delay", "80", *keys], check=True)
