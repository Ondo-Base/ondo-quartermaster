"""X11 screen backend, through python-xlib. The one CI runs, on Xvfb.

Works with and without a window manager: a client window is found by its title
(and process id, when the window says), and the stacking and hit-testing
questions are asked about its top-level ancestor, which is what a window manager
reparents into a frame.
"""

from __future__ import annotations

import logging
import threading

from ..desktop.model import StaleElement, Window
from .geometry import Rect

_log = logging.getLogger("ondo.agent")


class X11Screen:
    name = "x11"

    def __init__(self) -> None:
        from Xlib import X, display

        self._X = X
        self.d = display.Display()
        self.root = self.d.screen().root
        self._lock = threading.Lock()
        self._net_name = self.d.intern_atom("_NET_WM_NAME")
        self._net_pid = self.d.intern_atom("_NET_WM_PID")
        self._utf8 = self.d.intern_atom("UTF8_STRING")

    def close(self) -> None:
        try:
            self.d.close()
        except Exception:
            _log.debug("X11 display already closed", exc_info=True)

    # -- finding windows --------------------------------------------------------------

    def _title(self, w) -> str:
        try:
            p = w.get_full_property(self._net_name, self._utf8)
            if p and p.value:
                v = p.value
                return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            name = w.get_wm_name()
            return name.decode("utf-8", "replace") if isinstance(name, bytes) else (name or "")
        except Exception:  # the window went away mid-walk
            _log.debug("X11 window vanished while reading its title", exc_info=True)
            return ""

    def _pid(self, w) -> int:
        try:
            p = w.get_full_property(self._net_pid, self._X.AnyPropertyType)
            return int(p.value[0]) if p and len(p.value) else 0
        except Exception:
            _log.debug("X11 window vanished while reading its pid", exc_info=True)
            return 0

    def _find(self, window: Window):
        """(client, top-level) for a window, or raise StaleElement."""
        hits = []
        for top in self.root.query_tree().children:
            stack = [top]
            depth = 0
            while stack and depth < 4:
                nxt = []
                for w in stack:
                    try:
                        attrs = w.get_attributes()
                    except Exception:
                        _log.debug("X11 window vanished while walking the tree", exc_info=True)
                        continue
                    if attrs.map_state == self._X.IsViewable and self._title(w) == window.title:
                        hits.append((w, top))
                    try:
                        nxt.extend(w.query_tree().children)
                    except Exception:
                        _log.debug("X11 window vanished while walking the tree", exc_info=True)
                stack = nxt
                depth += 1
        if not hits:
            raise StaleElement(f"window {window.label} is not on screen")
        if window.pid:
            same = [h for h in hits if self._pid(h[0]) == window.pid]
            hits = same or hits
        return hits[0]

    # -- the backend ------------------------------------------------------------------

    def locate(self, window: Window) -> Rect:
        with self._lock:
            client, _ = self._find(window)
            g = client.get_geometry()
            t = self.root.translate_coords(client, 0, 0)
            return Rect(int(t.x), int(t.y), int(g.width), int(g.height))

    def capture(self, rect: Rect):
        from PIL import Image

        with self._lock:
            sw, sh = self.root.get_geometry().width, self.root.get_geometry().height
            x0, y0 = max(0, rect.x), max(0, rect.y)
            x1, y1 = min(sw, rect.x + rect.w), min(sh, rect.y + rect.h)
            if x1 <= x0 or y1 <= y0:
                raise StaleElement("the window is off screen")
            raw = self.root.get_image(x0, y0, x1 - x0, y1 - y0, self._X.ZPixmap, 0xFFFFFFFF)
        img = Image.frombytes("RGB", (x1 - x0, y1 - y0), raw.data, "raw", "BGRX")
        if (x0, y0, x1, y1) != (rect.x, rect.y, rect.x + rect.w, rect.y + rect.h):
            # Partly off screen: keep the window's own coordinate system, black where unseen.
            full = Image.new("RGB", (rect.w, rect.h))
            full.paste(img, (x0 - rect.x, y0 - rect.y))
            img = full
        return img

    def activate(self, window: Window) -> None:
        X = self._X
        with self._lock:
            client, top = self._find(window)
            top.configure(stack_mode=X.Above)
            self.d.set_input_focus(client, X.RevertToParent, X.CurrentTime)
            self.d.sync()

    def _top_of(self, w):
        """The root child containing ``w``."""
        root_id = self.root.id
        cur = w
        for _ in range(16):
            try:
                parent = cur.query_tree().parent
            except Exception:
                return None
            if parent is None or parent.id == root_id:
                return cur
            cur = parent
        return None

    def is_active(self, window: Window) -> bool:
        with self._lock:
            client, top = self._find(window)
            focus = self.d.get_input_focus().focus
            if isinstance(focus, int) or focus is None:
                return False
            f = self._top_of(focus)
            return f is not None and f.id == top.id

    def owns_point(self, window: Window, x: int, y: int) -> bool:
        with self._lock:
            _, top = self._find(window)
            under = self.root.translate_coords(self.root, x, y).child
            return bool(under) and under.id == top.id
