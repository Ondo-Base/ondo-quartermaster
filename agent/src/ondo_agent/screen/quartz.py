"""macOS screen backend. UNTESTED: written to the same interface as the X11
backend, never executed. Run ``tests/test_screen.py`` on macOS before relying on
it.

Window bounds and pointer events are in points; a Retina screenshot is in
pixels, twice as many. The session works that out from the capture itself
(capture width over window width), so nothing here assumes a scale. Screen
Recording and Accessibility permissions must be granted to the agent; neither
can be granted for the user.
"""

from __future__ import annotations

from ..desktop.model import StaleElement, Window
from .geometry import Rect


class QuartzScreen:
    name = "quartz"

    def __init__(self) -> None:
        import Quartz  # noqa: F401  (fail early with a clear ImportError)

    def _info(self, window: Window) -> dict:
        import Quartz

        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
        )
        for w in wins or []:
            if w.get("kCGWindowName") == window.title and (not window.pid or w.get("kCGWindowOwnerPID") == window.pid):
                return w
        raise StaleElement(f"window {window.label} is not on screen")

    def locate(self, window: Window) -> Rect:
        b = self._info(window)["kCGWindowBounds"]
        return Rect(int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"]))

    def capture(self, rect: Rect):
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=(rect.x, rect.y, rect.x + rect.w, rect.y + rect.h))

    def activate(self, window: Window) -> None:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

        pid = self._info(window)["kCGWindowOwnerPID"]
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)

    def _front(self, x: float | None = None, y: float | None = None) -> dict | None:
        import Quartz

        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
        )
        for w in wins or []:  # front to back
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            if x is None:
                return w
            b = w["kCGWindowBounds"]
            if b["X"] <= x < b["X"] + b["Width"] and b["Y"] <= y < b["Y"] + b["Height"]:
                return w
        return None

    def is_active(self, window: Window) -> bool:
        f = self._front()
        return f is not None and f.get("kCGWindowNumber") == self._info(window).get("kCGWindowNumber")

    def owns_point(self, window: Window, x: int, y: int) -> bool:
        f = self._front(x, y)
        return f is not None and f.get("kCGWindowNumber") == self._info(window).get("kCGWindowNumber")
