"""Screen watching: what is in front of the user, and asking about it.

When the screen grant is on, the agent tells the control plane which *shared*
window is in front, once a second and only when it changes. It sends the
window's name and nothing else. A window the grant does not cover, or one the
administrator excludes, is reported as nothing. The web UI uses this for the
context chip and for "Ask about this screen".

Escape twice stops watching as well as every run. Only the person at the device
resumes it, from the web UI.

A run started "about this screen" gets what is on that window put into its
context before the model's first turn: the accessibility tree where there is
one, and a screenshot (for a model that can see) or OCR text (for one that
cannot) where screen control is on. The grant is checked again here, on the
device. The content is fenced and screened like any other untrusted read.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .log import WINDOW_ACCESS
from .permissions import PermissionBroker

_log = logging.getLogger("ondo.agent")


class ScreenWatcher:
    def __init__(
        self,
        desktop,
        broker: Callable[[], PermissionBroker],
        send: Callable[[dict[str, Any]], None],
        *,
        interval: float = 1.0,
        escape_twice: bool = True,
    ):
        self.desktop = desktop
        self.broker = broker
        self.send = send
        self.interval = interval
        self.escape_twice = escape_twice
        self.watching = True
        self._last: tuple[str | None, bool] | None = None
        self._title: str | None = None
        self._unregister: Callable[[], None] | None = None

    def pause(self, reason: str) -> None:
        self.watching = False
        self._report(None, reason)

    def resume(self) -> None:
        self.watching = True
        self._last = None

    def resend(self) -> None:
        """After reconnecting: the control plane forgot, so say it again."""
        self._last = None

    def _report(self, window: str | None, reason: str = "", title: str | None = None) -> None:
        key = (window, self.watching)
        if key == self._last:
            return
        self._last = key
        msg = {"type": "screen_context", "window": window, "watching": self.watching, "reason": reason}
        if window and title:
            msg["title"] = title  # the window's own title, for display; ``window`` is what runs ask for
        self.send(msg)

    async def current(self):
        """The shared window in front, or None."""
        b = self.broker()
        if not b.grants["screen"].granted or not hasattr(self.desktop.backend, "active"):
            return None
        w = await asyncio.to_thread(self.desktop.backend.active)
        if w is None or b.window_excluded(w.label) or not b.window_allowed(w.label):
            return None
        return w

    async def run(self) -> None:
        if self.escape_twice:
            from .desktop import hotkey

            try:
                self._unregister = hotkey.register(lambda: self.pause("escape_twice"))
            except Exception as e:  # no way to stop it by keyboard, so it does not start
                _log.warning("Escape-twice unavailable (%s); screen watching is off", e)
                self.watching = False
        try:
            while True:
                if self.watching:
                    try:
                        w = await self.current()
                        self._report(w.label if w else None, title=w.title if w else None)
                    except Exception:
                        _log.debug("screen watch tick failed", exc_info=True)
                        self._report(None)
                else:
                    self._report(None, "paused")
                await asyncio.sleep(self.interval)
        finally:
            if self._unregister:
                self._unregister()


async def inject_screen_context(harness, window: str) -> None:
    """Put what is on ``window`` into the run's context, before the first turn."""
    from .desktop.session import render
    from .screening import fence

    broker = harness.broker
    desktop = harness.services.get("desktop")
    screen = harness.services.get("screen")
    source = "harness.screen_context"
    if desktop is None or not broker.grants["screen"].granted:
        harness.inject(f"The user asked about the window {window!r}, but screen access is off on this device.", source)
        return
    try:
        w = await desktop.window(window, broker.window_allowed)
    except LookupError:
        harness.inject(f"The user asked about {window!r}, but it is no longer open or no longer shared.", source)
        return
    els = await desktop.read(w)
    parts = [f"Window: {w.label}"]
    if els:
        parts.append("Accessibility tree:\n" + render(els, desktop.remember(w, els)))
    images = []
    vision = harness.model.profile.supports_vision
    if screen is not None:
        try:
            shot = await screen.screenshot(w, max_edge=harness.model.profile.max_image_long_edge_px or 1280)
            images.append(shot.image)
            parts.append(f"Screenshot {shot.frame.shot}: {shot.frame.width}x{shot.frame.height} pixels.")
            if not vision or not any(e.actionable for e in els):
                text = await screen.read_text(w)
                if text:
                    parts.append(f"Text on screen (OCR, may misread):\n{text}")
        except Exception as e:  # a screenshot that fails still leaves the tree
            parts.append(f"No screenshot: {e}")
    content = "\n\n".join(parts)
    origin = f"screen:{w.label}"
    verdict = await harness.screener.screen(content, origin, harness.log) if harness.screener else None
    harness.log.append(WINDOW_ACCESS, "harness.screen_context", {"window": w.label, "op": "context"})
    harness.inject(
        f"The user is looking at {w.label} and is asking about it. What it shows:\n" + fence(content, origin, verdict),
        source,
        images,
    )
