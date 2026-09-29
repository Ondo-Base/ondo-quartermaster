"""Linux backend: AT-SPI through pyatspi.

The reference backend: it runs in CI against a real GTK application on a
virtual display, so the platform-neutral layer above it is tested for real.
pyatspi is synchronous; callers run these methods in a worker thread.
"""

from __future__ import annotations

import logging
import threading

from .model import Element, StaleElement, Window, number_occurrences

_log = logging.getLogger("ondo.agent")

_STATES = {
    "STATE_FOCUSED": "focused",
    "STATE_ENABLED": "enabled",
    "STATE_EDITABLE": "editable",
    "STATE_CHECKED": "checked",
    "STATE_SHOWING": "showing",
    "STATE_SELECTED": "selected",
    "STATE_ACTIVE": "active",
}
_CLICK_NAMES = ("click", "press", "activate", "toggle", "jump")


class AtspiBackend:
    name = "atspi"

    def __init__(self) -> None:
        import pyatspi  # noqa: F401  (fail early with a clear ImportError)

        self._lock = threading.Lock()

    def _desktop(self):
        import pyatspi

        return pyatspi.Registry.getDesktop(0)

    def windows(self) -> list[Window]:
        out: list[Window] = []
        with self._lock:
            for app in self._desktop():
                if app is None:
                    continue
                for i, w in enumerate(app):
                    if w is None:
                        continue
                    try:
                        role = w.getRoleName()
                    except Exception:
                        _log.debug("AT-SPI window vanished while listing", exc_info=True)
                        continue
                    if role in ("frame", "window", "dialog", "alert"):
                        out.append(Window(id=f"{_pid(app)}#{i}", title=w.name or "", app=app.name or "", pid=_pid(app)))
        return out

    def active(self) -> Window | None:
        """The window in front, if the toolkit says any window is active."""
        import pyatspi

        with self._lock:
            for app in self._desktop():
                if app is None:
                    continue
                for i, w in enumerate(app):
                    try:
                        if w is not None and w.getState().contains(pyatspi.STATE_ACTIVE):
                            return Window(id=f"{_pid(app)}#{i}", title=w.name or "", app=app.name or "", pid=_pid(app))
                    except Exception:
                        _log.debug("AT-SPI window vanished while checking focus", exc_info=True)
        return None

    def _window_node(self, window: Window):
        pid, _, idx = window.id.rpartition("#")
        for app in self._desktop():
            if app is not None and str(_pid(app)) == pid:
                for i, w in enumerate(app):
                    if w is not None and (str(i) == idx or w.name == window.title):
                        return w
        # The process went away and came back: resolve by title, the way a person would.
        for app in self._desktop():
            for w in app or []:
                if w is not None and w.name == window.title:
                    return w
        raise StaleElement(f"window {window.label} is gone")

    def elements(self, window: Window, max_nodes: int = 600) -> list[Element]:
        with self._lock:
            root = self._window_node(window)
            out: list[Element] = []

            def walk(node, depth: int) -> None:
                if node is None or len(out) >= max_nodes:
                    return
                try:
                    role = node.getRoleName()
                    name = node.name or ""
                    states = frozenset(
                        _STATES[k] for k in (_state_key(s) for s in node.getState().getStates()) if k in _STATES
                    )
                except Exception:
                    return
                actions: tuple[str, ...] = ()
                try:
                    act = node.queryAction()
                    actions = tuple(act.getName(i) for i in range(act.nActions))
                except NotImplementedError:
                    pass  # this element has no actions
                except Exception:
                    _log.debug("AT-SPI action query failed for %s", role, exc_info=True)
                value = _value(node, role)
                if role not in ("filler", "panel") or name:
                    out.append(Element(role, name, value, states, actions, depth, handle=node))
                for child in node:
                    walk(child, depth + 1)

            for child in root:
                walk(child, 0)
        return number_occurrences(out)

    def click(self, element: Element) -> None:
        with self._lock:
            try:
                act = element.handle.queryAction()
            except Exception as e:
                raise StaleElement(str(e)) from e
            names = [act.getName(i) for i in range(act.nActions)]
            for want in _CLICK_NAMES:
                if want in names:
                    act.doAction(names.index(want))
                    return
            if names:
                act.doAction(0)
                return
            raise ValueError(f"{element.described} cannot be clicked")

    def set_text(self, element: Element, text: str) -> None:
        with self._lock:
            try:
                et = element.handle.queryEditableText()
            except NotImplementedError as e:
                raise ValueError(f"{element.described} is not an editable field") from e
            except Exception as e:
                raise StaleElement(str(e)) from e
            if not et.setTextContents(text):
                raise ValueError(f"{element.described} refused the text")

    def focus(self, element: Element) -> None:
        with self._lock:
            try:
                element.handle.queryComponent().grabFocus()
            except Exception as e:
                raise StaleElement(str(e)) from e


def _state_key(s) -> str:
    # pyatspi names states STATE_X or ATSPI_STATE_X depending on version.
    return str(getattr(s, "value_name", s)).removeprefix("ATSPI_")


def _pid(app) -> int:
    try:
        return int(app.get_process_id())
    except Exception:
        return 0


def _value(node, role: str) -> str:
    try:
        return node.queryText().getText(0, -1) or ""
    except NotImplementedError:
        pass  # not a text element; try the value interface
    except Exception:
        _log.debug("AT-SPI text read failed for %s", role, exc_info=True)
    try:
        return str(node.queryValue().currentValue)
    except Exception:
        return ""
