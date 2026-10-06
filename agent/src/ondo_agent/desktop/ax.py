"""macOS backend: the Accessibility API (AXUIElement) through pyobjc.

UNTESTED: written against the documented AX API but never run, because this
repository's CI and development environment are Linux. Two known macOS realities
shape it:

- The Accessibility TCC permission cannot be granted programmatically. If it is
  missing, every call fails, and the error says to allow it in System Settings.
- Element references go stale (practitioners report caching failures on macOS
  26). Nothing is cached here beyond one read; a stale error surfaces as
  ``StaleElement`` and the tool layer re-reads the tree and retries once.
"""

from __future__ import annotations

import threading

from .model import Element, StaleElement, Window, number_occurrences

_ROLE = {
    "AXButton": "push button",
    "AXTextField": "text",
    "AXTextArea": "text",
    "AXStaticText": "label",
    "AXCheckBox": "check box",
    "AXRadioButton": "radio button",
    "AXPopUpButton": "combo box",
    "AXComboBox": "combo box",
    "AXMenuItem": "menu item",
    "AXLink": "link",
    "AXCell": "table cell",
    "AXSlider": "slider",
    "AXIncrementor": "spin button",
}


def _attr(el, name):
    from ApplicationServices import AXUIElementCopyAttributeValue, kAXErrorSuccess

    err, value = AXUIElementCopyAttributeValue(el, name, None)
    if err == -25202:  # kAXErrorInvalidUIElement
        raise StaleElement(f"{name}: element is no longer valid")
    return value if err == kAXErrorSuccess else None


class AxBackend:
    name = "ax"

    def __init__(self) -> None:
        from ApplicationServices import AXIsProcessTrusted

        if not AXIsProcessTrusted():
            raise PermissionError(
                "Allow Ondo in System Settings → Privacy & Security → Accessibility. "
                "macOS does not let Ondo approve this for you."
            )
        self._lock = threading.Lock()

    def _apps(self):
        from AppKit import NSWorkspace

        return [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.activationPolicy() == 0]

    def windows(self) -> list[Window]:
        from ApplicationServices import AXUIElementCreateApplication

        out = []
        with self._lock:
            for app in self._apps():
                pid = app.processIdentifier()
                ax = AXUIElementCreateApplication(pid)
                for i, w in enumerate(_attr(ax, "AXWindows") or []):
                    out.append(
                        Window(
                            id=f"{pid}#{i}",
                            title=str(_attr(w, "AXTitle") or ""),
                            app=str(app.localizedName() or ""),
                            pid=pid,
                        )
                    )
        return out

    def active(self) -> Window | None:
        from AppKit import NSWorkspace
        from ApplicationServices import AXUIElementCreateApplication

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = app.processIdentifier()
        w = _attr(AXUIElementCreateApplication(pid), "AXFocusedWindow")
        if w is None:
            return None
        title = str(_attr(w, "AXTitle") or "")
        return next((x for x in self.windows() if x.pid == pid and x.title == title), None)

    def _window(self, window: Window):
        from ApplicationServices import AXUIElementCreateApplication

        pid, _, idx = window.id.partition("#")
        wins = _attr(AXUIElementCreateApplication(int(pid)), "AXWindows") or []
        for i, w in enumerate(wins):
            if str(i) == idx or str(_attr(w, "AXTitle") or "") == window.title:
                return w
        raise StaleElement(f"window {window.label} is gone")

    def elements(self, window: Window, max_nodes: int = 600) -> list[Element]:
        with self._lock:
            out: list[Element] = []

            def walk(el, depth):
                if len(out) >= max_nodes:
                    return
                ax_role = str(_attr(el, "AXRole") or "")
                role = _ROLE.get(ax_role, ax_role.removeprefix("AX").lower())
                name = str(_attr(el, "AXTitle") or _attr(el, "AXDescription") or "")
                value = _attr(el, "AXValue")
                states = {"enabled"} if _attr(el, "AXEnabled") else set()
                if ax_role in ("AXTextField", "AXTextArea"):
                    states.add("editable")
                if _attr(el, "AXFocused"):
                    states.add("focused")
                actions = (
                    ("press",) if ax_role in ("AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXLink") else ()
                )
                if ax_role not in ("AXGroup", "AXScrollArea", "AXSplitGroup") or name:
                    out.append(
                        Element(
                            role,
                            name,
                            "" if value is None else str(value),
                            frozenset(states),
                            actions,
                            depth,
                            handle=el,
                        )
                    )
                for c in _attr(el, "AXChildren") or []:
                    walk(c, depth + 1)

            for c in _attr(self._window(window), "AXChildren") or []:
                walk(c, 0)
        return number_occurrences(out)

    def click(self, element: Element) -> None:
        from ApplicationServices import AXUIElementPerformAction, kAXErrorSuccess

        with self._lock:
            if AXUIElementPerformAction(element.handle, "AXPress") != kAXErrorSuccess:
                raise StaleElement(f"{element.described} did not accept a press")

    def set_text(self, element: Element, text: str) -> None:
        from ApplicationServices import AXUIElementSetAttributeValue, kAXErrorSuccess

        with self._lock:
            if AXUIElementSetAttributeValue(element.handle, "AXValue", text) != kAXErrorSuccess:
                raise StaleElement(f"{element.described} did not accept the text")

    def focus(self, element: Element) -> None:
        from ApplicationServices import AXUIElementSetAttributeValue

        with self._lock:
            AXUIElementSetAttributeValue(element.handle, "AXFocused", True)
