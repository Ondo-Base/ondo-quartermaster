"""Windows backend: UI Automation through pywinauto.

UNTESTED: written against pywinauto's documented UIA API but never run, because
this repository's CI and development environment are Linux. Run
``tests/test_stage4.py`` on a Windows machine (with the GTK test app replaced by
a WinForms or Win32 equivalent) before relying on it.
"""

from __future__ import annotations

import logging
import threading

from .model import Element, StaleElement, Window, number_occurrences

_log = logging.getLogger("ondo.agent")

# UIA control types, mapped to the neutral role names the tools use.
_ROLE = {
    "Button": "push button",
    "Edit": "text",
    "Text": "label",
    "CheckBox": "check box",
    "RadioButton": "radio button",
    "ComboBox": "combo box",
    "MenuItem": "menu item",
    "Hyperlink": "link",
    "ListItem": "list item",
    "TabItem": "page tab",
    "Document": "document text",
    "Spinner": "spin button",
    "Slider": "slider",
    "DataItem": "table cell",
}


class UiaBackend:
    name = "uia"

    def __init__(self) -> None:
        from pywinauto import Desktop  # noqa: F401

        self._lock = threading.Lock()

    def _desktop(self):
        from pywinauto import Desktop

        return Desktop(backend="uia")

    def windows(self) -> list[Window]:
        out = []
        with self._lock:
            for w in self._desktop().windows():
                try:
                    info = w.element_info
                    out.append(
                        Window(id=str(info.handle), title=info.name or "", app=_proc_name(w), pid=w.process_id())
                    )
                except Exception:
                    _log.debug("UIA window vanished while listing", exc_info=True)
                    continue
        return out

    def active(self) -> Window | None:
        import ctypes

        h = ctypes.windll.user32.GetForegroundWindow()
        for w in self.windows():
            if w.id == str(h):
                return w
        return None

    def _window(self, window: Window):
        for w in self._desktop().windows():
            if str(w.element_info.handle) == window.id or w.element_info.name == window.title:
                return w
        raise StaleElement(f"window {window.label} is gone")

    def elements(self, window: Window, max_nodes: int = 600) -> list[Element]:
        with self._lock:
            root = self._window(window)
            out = []
            for i, c in enumerate(root.descendants()):
                if i >= max_nodes:
                    break
                info = c.element_info
                role = _ROLE.get(info.control_type, (info.control_type or "unknown").lower())
                states = set()
                if info.enabled:
                    states.add("enabled")
                if role in ("text", "document text") and not getattr(c, "is_read_only", lambda: True)():
                    states.add("editable")
                actions = (
                    ("click",) if role in ("push button", "menu item", "link", "check box", "radio button") else ()
                )
                value = ""
                try:
                    value = c.get_value() if hasattr(c, "get_value") else (c.window_text() or "")
                except Exception:
                    _log.debug("UIA value read failed for %s", role, exc_info=True)
                out.append(Element(role, info.name or "", str(value or ""), frozenset(states), actions, 0, handle=c))
        return number_occurrences(out)

    def click(self, element: Element) -> None:
        with self._lock:
            try:
                c = element.handle
                (c.invoke if hasattr(c, "invoke") else c.click_input)()
            except Exception as e:
                raise StaleElement(str(e)) from e

    def set_text(self, element: Element, text: str) -> None:
        with self._lock:
            try:
                element.handle.set_edit_text(text)
            except Exception as e:
                raise StaleElement(str(e)) from e

    def focus(self, element: Element) -> None:
        with self._lock:
            element.handle.set_focus()


def _proc_name(w) -> str:
    try:
        import psutil

        return psutil.Process(w.process_id()).name()
    except Exception:
        return ""
