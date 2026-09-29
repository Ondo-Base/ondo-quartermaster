"""One screen session per run: screenshots, their frames, grounding, and input.

Every screenshot gets a short id (s1, s2 …) and a ``Frame``. Coordinates the
model sends are pixels in a screenshot it was shown, so they are mapped through
that screenshot's frame, and the window is located again at the moment of
acting. Nothing about where a window *was* is trusted.

Before any input: the window is brought to the front, and the backend is asked
whether it really has the keyboard (for keys) or really is the window under the
point (for the pointer). If not, nothing is sent.
"""

from __future__ import annotations

import asyncio
import fnmatch
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ..desktop.model import Window
from ..desktop.session import DesktopSession
from ..tools.spec import Image
from . import ocr
from .backend import ScreenBackend
from .geometry import Frame, Rect, fit
from .grounding import Grounder, Located

T = TypeVar("T")

# Windows known to be remote sessions: pixels are the only rung they have, so
# the ladder need not be walked for them first. Matched against app and title.
REMOTE_SESSIONS = [
    "*citrix*",
    "*wfica*",
    "*remote desktop*",
    "*mstsc*",
    "*vnc*",
    "*remmina*",
    "*freerdp*",
    "*horizon client*",
    "*anydesk*",
    "*teamviewer*",
]


class ScreenError(Exception):
    """A readable reason nothing happened. The message is for the model."""


@dataclass
class Shot:
    frame: Frame
    image: Image


@dataclass
class ScreenSession:
    backend: ScreenBackend
    desktop: DesktopSession
    grounder: Grounder | None = None
    input: Any = None  # PynputInput, created on first use (it needs a display)
    max_long_edge: int = 1280
    max_pixels: int = 0
    pixel_only: list[str] = field(default_factory=lambda: list(REMOTE_SESSIONS))
    tesseract: str | None = "tesseract"
    min_grounding_confidence: float = 0.5
    frames: dict[str, Frame] = field(default_factory=dict)
    latest: dict[str, str] = field(default_factory=dict)  # window label -> shot id
    # What was typed into each window since its last commit: the approval's values.
    typed: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # The input box the pointer last put focus in, per window (window-relative).
    last_field: dict[str, tuple[str, tuple[float, float, float, float]]] = field(default_factory=dict)
    _n: int = 0

    @classmethod
    def from_config(cls, cfg: dict[str, Any], desktop: DesktopSession) -> ScreenSession:
        from .backend import make_screen_backend
        from .grounding import make_grounder

        o = cfg.get("ocr") or {}
        tess = o.get("tesseract", "tesseract")
        return cls(
            backend=make_screen_backend(cfg.get("backend", "auto")),
            desktop=desktop,
            grounder=make_grounder(cfg.get("grounding") or {"provider": "ocr", "tesseract": tess}),
            max_long_edge=int(cfg.get("max_long_edge", 1280)),
            max_pixels=int(cfg.get("max_pixels", 0)),
            pixel_only=list(cfg.get("pixel_only_windows", REMOTE_SESSIONS)),
            tesseract=tess if o.get("enabled", True) and ocr.available(tess) else None,
            min_grounding_confidence=float(cfg.get("min_grounding_confidence", 0.5)),
        )

    async def _run(self, fn: Callable[..., T], *a) -> T:
        return await asyncio.to_thread(fn, *a)

    def _input(self):
        if self.input is None:
            from .backend import PynputInput

            self.input = PynputInput()
        return self.input

    async def aclose(self) -> None:
        if self.input is not None and hasattr(self.input, "close"):
            self.input.close()
        if hasattr(self.backend, "close"):
            self.backend.close()
        if hasattr(self.grounder, "aclose"):
            await self.grounder.aclose()

    # -- the ladder -------------------------------------------------------------------

    def is_remote_session(self, w: Window) -> bool:
        names = [w.app.lower(), w.title.lower(), w.label.lower()]
        return any(fnmatch.fnmatch(n, p.lower()) for p in self.pixel_only for n in names)

    # -- seeing -----------------------------------------------------------------------

    async def locate(self, w: Window) -> Rect:
        return await self._run(self.backend.locate, w)

    async def capture(self, w: Window) -> tuple[Rect, Any]:
        """Bring the window forward and capture exactly its area. Refuses if another
        window covers part of it: the capture would show that window, and it may be
        one the user never shared."""
        await self._run(self.backend.activate, w)
        await asyncio.sleep(0.15)
        rect = await self.locate(w)
        inset = [(0.5, 0.5), (0.05, 0.05), (0.95, 0.05), (0.05, 0.95), (0.95, 0.95)]
        for fx, fy in inset:
            x, y = round(rect.x + rect.w * fx), round(rect.y + rect.h * fy)
            if not await self._run(self.backend.owns_point, w, x, y):
                raise ScreenError(f"Another window covers part of {w.label}, so no screenshot was taken.")
        img = await self._run(self.backend.capture, rect)
        return rect, img

    def _next(self) -> str:
        self._n += 1
        return f"s{self._n}"

    def _png(self, img: Any) -> bytes:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def fit_edge(self, profile_edge: int | None) -> int:
        return min(self.max_long_edge, profile_edge or self.max_long_edge)

    async def screenshot(self, w: Window, *, max_edge: int | None = None) -> Shot:
        rect, img = await self.capture(w)
        size = fit(img.width, img.height, self.fit_edge(max_edge), self.max_pixels)
        sent = img if size == img.size else img.resize(size)
        sid = self._next()
        frame = Frame.for_capture(sid, w.label, rect.size, img.size, size)
        self.frames[sid] = frame
        self.latest[w.label] = sid
        return Shot(frame, Image(self._png(sent), "image/png", size[0], size[1], f"{sid} · {w.label}"))

    async def zoom(self, w: Window, shot: str | None, box: list[float], *, max_edge: int | None = None) -> Shot:
        base = self.frame_for(w, shot)
        x0, y0, x1, y1 = box
        if not (x1 > x0 and y1 > y0 and base.contains(x0, y0) and base.contains(x1 - 1, y1 - 1)):
            raise ScreenError(f"The zoom region must lie inside {base.shot} ({base.width}x{base.height}).")
        rect, img = await self.capture(w)
        if not base.same_size(rect):
            raise ScreenError(f"{w.label} changed size since {base.shot}. Take a new screenshot.")
        ds = img.width / rect.w  # capture pixels per input unit (2 on a Retina Mac)
        a, b, c, d = base.region(x0, y0, x1, y1)
        crop = img.crop((round(a * ds), round(b * ds), round(c * ds), round(d * ds)))
        edge = self.fit_edge(max_edge)
        s = min(4.0, edge / max(crop.size))  # zooming in is the point: upscale, to a limit
        size = (max(1, round(crop.width * s)), max(1, round(crop.height * s)))
        sent = crop.resize(size)
        sid = self._next()
        frame = base.zoom(sid, x0, y0, x1, y1, size)
        self.frames[sid] = frame
        self.latest[w.label] = sid
        return Shot(frame, Image(self._png(sent), "image/png", size[0], size[1], f"{sid} · zoom of {base.shot}"))

    def frame_for(self, w: Window, shot: str | None) -> Frame:
        sid = shot or self.latest.get(w.label)
        if not sid:
            raise ScreenError(f"There is no screenshot of {w.label} yet. Take one first, or name the target.")
        f = self.frames.get(sid)
        if f is None or f.window != w.label:
            raise ScreenError(f"{sid} is not a screenshot of {w.label}.")
        return f

    async def read_text(self, w: Window, rect_img: tuple[Rect, Any] | None = None) -> str | None:
        if not self.tesseract:
            return None
        _, img = rect_img or await self.capture(w)
        return await asyncio.to_thread(ocr.read_text, img, binary=self.tesseract)

    # -- pointing ---------------------------------------------------------------------

    async def ground(self, w: Window, target: str) -> tuple[Rect, float, float, Located]:
        """Find ``target`` on a fresh full-resolution capture: (window rect, x, y in
        window-relative input units, what the grounder said)."""
        if self.grounder is None:
            raise ScreenError(
                "No grounding model is configured, so targets cannot be found by description. "
                "Give coordinates in a screenshot instead."
            )
        from .grounding import GroundingError

        rect, img = await self.capture(w)
        try:
            hit = await self.grounder.locate(img, target)
        except GroundingError as e:
            raise ScreenError(str(e)) from None
        if hit is None:
            raise ScreenError(f'I could not find "{target}" in {w.label}. Take a screenshot and check, or zoom in.')
        if hit.confidence < self.min_grounding_confidence:
            raise ScreenError(f'I am not sure where "{target}" is (confidence {hit.confidence:.2f}). Give the point.')
        k = rect.w / img.width  # capture pixels back to input units
        if hit.box and "field" in hit.how:
            bx0, by0, bx1, by1 = hit.box
            self.last_field[w.label] = (target, (bx0 * k, by0 * k, bx1 * k, by1 * k))
        return rect, hit.x * k, hit.y * k, hit

    async def point(self, w: Window, args: dict[str, Any], prefix: str = "") -> tuple[int, int, str]:
        """Where on screen an action lands: from coordinates in a screenshot, or by
        grounding the target. Returns (x, y, how it was found)."""
        x, y = args.get(prefix + "x"), args.get(prefix + "y")
        target = args.get(prefix + "target")
        if x is not None and y is not None:
            f = self.frame_for(w, args.get("shot"))
            if not f.contains(float(x), float(y)):
                raise ScreenError(f"({x}, {y}) is outside {f.shot}, which is {f.width}x{f.height}.")
            rect = await self.locate(w)
            if not f.same_size(rect):
                raise ScreenError(f"{w.label} changed size since {f.shot}. Take a new screenshot.")
            sx, sy = f.to_screen(rect, float(x), float(y))
            self.last_field.pop(w.label, None)
            return sx, sy, f"({x}, {y}) in {f.shot}"
        if target:
            rect, wx, wy, hit = await self.ground(w, str(target))
            if "field" not in hit.how:
                self.last_field.pop(w.label, None)
            return round(rect.x + wx), round(rect.y + wy), f"{hit.how}, {hit.confidence:.2f}"
        raise ScreenError("Give x and y (pixels in a screenshot) or a target to find.")

    # -- acting -----------------------------------------------------------------------

    async def ensure_under(self, w: Window, x: int, y: int) -> None:
        await self._run(self.backend.activate, w)
        await asyncio.sleep(0.1)
        rect = await self.locate(w)
        if not rect.contains(x, y):
            raise ScreenError(f"That point is outside {w.label}. Nothing was clicked.")
        if not await self._run(self.backend.owns_point, w, x, y):
            raise ScreenError(f"Another window is on top of {w.label} at that point. Nothing was clicked.")

    async def ensure_focused(self, w: Window) -> None:
        await self._run(self.backend.activate, w)
        await asyncio.sleep(0.1)
        if not await self._run(self.backend.is_active, w):
            raise ScreenError(f"{w.label} does not have the keyboard, so nothing was typed.")

    async def send(self, fn: str, *a, **kw) -> None:
        await asyncio.to_thread(getattr(self._input(), fn), *a, **kw)

    async def field_text(self, w: Window) -> str | None:
        """What the box the pointer last focused now shows, by OCR. Informational:
        OCR can misread, so this never fails a step on its own."""
        if not self.tesseract or w.label not in self.last_field:
            return None
        _, (a, b, c, d) = self.last_field[w.label]
        rect, img = await self.capture(w)
        k = img.width / rect.w
        crop = img.crop((round(a * k) + 2, round(b * k) + 2, round(c * k) - 2, round(d * k) - 2))
        return (await asyncio.to_thread(ocr.read_text, crop, binary=self.tesseract)).strip()
