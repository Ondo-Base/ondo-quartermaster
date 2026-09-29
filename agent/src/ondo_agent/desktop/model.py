"""The platform-neutral view of a desktop: windows and named elements.

Rung two of the ladder. Every backend (AT-SPI on Linux, UI Automation on
Windows, AXUIElement on macOS) reduces its OS tree to these types, and the tools
only ever see these types.

Elements are addressed by *locator* — window, role, name and which occurrence of
that role and name — never by coordinates or by an OS handle. A locator is
resolved against a fresh read of the tree every time it is used. That is what
survives a moved window, a rescaled display, and the stale references macOS 26
is known for: there is nothing cached to go stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Window:
    id: str  # backend-specific, only valid for this read
    title: str
    app: str
    pid: int = 0

    @property
    def label(self) -> str:
        return f"{self.app} — {self.title}" if self.app and self.app not in self.title else self.title


@dataclass
class Element:
    role: str
    name: str
    value: str = ""
    states: frozenset[str] = frozenset()
    actions: tuple[str, ...] = ()
    depth: int = 0
    # Occurrence of this (role, name) in the window, in tree order. With the
    # window, this is the locator.
    occurrence: int = 0
    # The backend's live object, valid only until the next read. Never logged.
    handle: Any = field(default=None, repr=False, compare=False)

    @property
    def described(self) -> str:
        return f'{self.role} "{self.name}"' if self.name else self.role

    @property
    def locator(self) -> Locator:
        return Locator(self.role, self.name, self.occurrence)

    @property
    def actionable(self) -> bool:
        return bool(self.actions) or "editable" in self.states


@dataclass(frozen=True)
class Locator:
    role: str
    name: str
    occurrence: int = 0

    def key(self) -> str:
        return f"{self.role}|{self.name}|{self.occurrence}"


# Roles that carry a value a person would want to see before something lands.
FIELD_ROLES = {
    "text",
    "entry",
    "edit",
    "password text",
    "spin button",
    "combo box",
    "check box",
    "radio button",
    "slider",
    "textfield",
    "text field",
    "document text",
}
# Roles whose activation commits something: gate candidates.
COMMIT_ROLES = {"push button", "button", "menu item", "link", "toggle button"}


class DesktopBackend(Protocol):
    name: str

    def windows(self) -> list[Window]: ...
    def elements(self, window: Window, max_nodes: int = 600) -> list[Element]: ...
    def click(self, element: Element) -> None: ...
    def set_text(self, element: Element, text: str) -> None: ...
    def focus(self, element: Element) -> None: ...
    def active(self) -> Window | None: ...


class StaleElement(Exception):
    """The OS says the element is gone. The caller re-reads the tree and retries once."""


def number_occurrences(elements: list[Element]) -> list[Element]:
    seen: dict[tuple[str, str], int] = {}
    for e in elements:
        k = (e.role, e.name)
        e.occurrence = seen.get(k, 0)
        seen[k] = e.occurrence + 1
    return elements


def apply_profile(elements: list[Element], aliases: dict[str, str]) -> list[Element]:
    """Per-app profiles name what the app leaves unnamed: {"push button#2": "Submit"}.

    Real enterprise apps have unnamed buttons and duplicate labels. A small profile
    per app is the actual work of this rung; this is where it plugs in.
    """
    if not aliases:
        return elements
    counts: dict[str, int] = {}
    for e in elements:
        if e.name:
            continue
        counts[e.role] = counts.get(e.role, 0) + 1
        alias = aliases.get(f"{e.role}#{counts[e.role]}")
        if alias:
            e.name = alias
    return number_occurrences(elements)
