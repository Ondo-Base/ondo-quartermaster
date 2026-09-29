"""What a platform must provide for pixel control, and the input side all share.

A backend finds a window on screen, captures it, brings it to the front, and
answers two safety questions before anything is sent: *is this window the one
with keyboard focus* and *is this window the one under that point*. Keystrokes
into the wrong window are the worst failure pixel control has, so neither
question is optional.

Pointer and keyboard events go through pynput on every platform (XTest on X11,
SendInput on Windows, Quartz events on macOS).
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Protocol

from ..desktop.model import Window
from .geometry import Rect

_log = logging.getLogger("ondo.agent")

# Escape is the user's: two presses stop every run and take the keyboard back
# (``desktop/hotkey.py``). The agent never sends it, so it can never trip it,
# and never block it.
RESERVED_KEYS = frozenset({"esc", "escape"})

_ALIASES = {
    "return": "enter",
    "enter": "enter",
    "tab": "tab",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "space": "space",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "page_up",
    "page_up": "page_up",
    "pgup": "page_up",
    "pagedown": "page_down",
    "page_down": "page_down",
    "pgdn": "page_down",
    "insert": "insert",
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "win": "cmd",
    "meta": "cmd",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}
MODIFIERS = frozenset({"ctrl", "shift", "alt", "cmd"})


def parse_combo(combo: str) -> list[str]:
    """ "ctrl+a" -> ["ctrl", "a"]; "Return" -> ["enter"]. Raises ValueError for
    Escape and for anything that is not a key."""
    parts = [p.strip() for p in combo.replace(" ", "").split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"{combo!r} is not a key combination")
    out = []
    for p in parts:
        low = p.lower()
        if low in RESERVED_KEYS:
            raise ValueError("Escape is reserved for the user: pressing it twice stops the run. Ondo never sends it.")
        if low in _ALIASES:
            out.append(_ALIASES[low])
        elif len(p) == 1 and p.isprintable():
            out.append(p)
        else:
            raise ValueError(f"unknown key {p!r} in {combo!r}")
    return out


class ScreenBackend(Protocol):
    name: str

    def locate(self, window: Window) -> Rect: ...
    def capture(self, rect: Rect) -> Any: ...  # a PIL image, in capture pixels
    def activate(self, window: Window) -> None: ...
    def is_active(self, window: Window) -> bool: ...
    def owns_point(self, window: Window, x: int, y: int) -> bool: ...


class PynputInput:
    """Pointer and keyboard. Imported lazily: pynput needs a display to import."""

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._kb = keyboard
        self._mouse_mod = mouse
        self.mouse = mouse.Controller()
        self.keys = keyboard.Controller()

    def _button(self, name: str):
        return {
            "left": self._mouse_mod.Button.left,
            "right": self._mouse_mod.Button.right,
            "middle": self._mouse_mod.Button.middle,
        }[name]

    def _key(self, name: str):
        return getattr(self._kb.Key, name) if len(name) > 1 else name

    def move(self, x: int, y: int) -> None:
        self.mouse.position = (x, y)
        time.sleep(0.05)

    def click(self, x: int, y: int, button: str = "left", count: int = 1, modifiers: list[str] | None = None) -> None:
        self.move(x, y)
        held = [self._key(m) for m in modifiers or []]
        for k in held:
            self.keys.press(k)
        try:
            self.mouse.click(self._button(button), count)
        finally:
            for k in reversed(held):
                self.keys.release(k)
        time.sleep(0.1)

    def drag(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.move(x0, y0)
        self.mouse.press(self._button("left"))
        try:
            steps = 8
            for i in range(1, steps + 1):
                self.move(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps))
        finally:
            self.mouse.release(self._button("left"))

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.move(x, y)
        self.mouse.scroll(dx, dy)

    def key(self, names: list[str], repeat: int = 1) -> None:
        keys = [self._key(n) for n in names]
        for _ in range(repeat):
            for k in keys:
                self.keys.press(k)
            for k in reversed(keys):
                self.keys.release(k)
            time.sleep(0.03)

    def hold(self, names: list[str], seconds: float) -> None:
        keys = [self._key(n) for n in names]
        for k in keys:
            self.keys.press(k)
        try:
            time.sleep(seconds)
        finally:
            for k in reversed(keys):
                self.keys.release(k)

    def type(self, text: str) -> None:
        if "\x1b" in text:
            raise ValueError("Escape is reserved for the user.")
        self.keys.type(text)

    def close(self) -> None:
        """Close the controllers' display connections now, rather than in a
        finaliser that may run after the display has gone."""

        class _Closed:
            def close(self) -> None:
                return None

        for c in (self.mouse, self.keys):
            d = getattr(c, "_display", None)
            if d is not None:
                try:
                    d.close()
                except Exception:  # already gone with the display
                    _log.debug("input display already closed", exc_info=True)
                c._display = _Closed()


def make_screen_backend(name: str = "auto") -> ScreenBackend:
    if name == "auto":
        name = {"win32": "win32", "darwin": "quartz"}.get(sys.platform, "x11")
    if name == "x11":
        from .x11 import X11Screen

        return X11Screen()
    if name == "win32":
        from .win32 import Win32Screen

        return Win32Screen()
    if name == "quartz":
        from .quartz import QuartzScreen

        return QuartzScreen()
    raise ValueError(f"unknown screen backend {name!r}")
