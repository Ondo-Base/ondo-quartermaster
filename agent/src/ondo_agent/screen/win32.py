"""Windows screen backend. UNTESTED: written to the same interface as the X11
backend, never executed. Run ``tests/test_stage5.py`` on Windows, against a
native test app, before relying on it.

The process is made per-monitor DPI aware, so window rectangles, screenshots
and SendInput all speak physical pixels and ``device_scale`` stays 1.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from ..desktop.model import StaleElement, Window
from .geometry import Rect


class Win32Screen:
    name = "win32"

    def __init__(self) -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        except Exception:  # noqa: S110  (already set, or an older Windows)
            pass
        self.u = ctypes.windll.user32

    def _hwnd(self, window: Window) -> int:
        h = self.u.FindWindowW(None, window.title)
        if not h:
            raise StaleElement(f"window {window.label} is not on screen")
        return h

    def locate(self, window: Window) -> Rect:
        h = self._hwnd(window)
        r = wintypes.RECT()
        pt = wintypes.POINT(0, 0)
        self.u.GetClientRect(h, ctypes.byref(r))
        self.u.ClientToScreen(h, ctypes.byref(pt))
        return Rect(pt.x, pt.y, r.right - r.left, r.bottom - r.top)

    def capture(self, rect: Rect):
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=(rect.x, rect.y, rect.x + rect.w, rect.y + rect.h), all_screens=True)

    def activate(self, window: Window) -> None:
        h = self._hwnd(window)
        self.u.ShowWindow(h, 9)  # SW_RESTORE
        self.u.SetForegroundWindow(h)

    def is_active(self, window: Window) -> bool:
        return self.u.GetForegroundWindow() == self._hwnd(window)

    def owns_point(self, window: Window, x: int, y: int) -> bool:
        under = self.u.WindowFromPoint(wintypes.POINT(x, y))
        return bool(under) and self.u.GetAncestor(under, 2) == self._hwnd(window)  # GA_ROOT
