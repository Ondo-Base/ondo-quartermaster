"""Coordinates, both ways.

Three spaces, and every conversion is explicit:

- **input units** — what the OS takes for pointer events. Physical pixels on X11
  and on a DPI-aware Windows process; points on macOS.
- **capture pixels** — what a screenshot holds. Equal to input units except on
  HiDPI macOS, where a Retina capture is 2x (``device_scale``).
- **image pixels** — what the model or the grounder is shown: the capture
  downscaled to fit the profile's long edge (around 1280), or a zoomed crop.

A ``Frame`` maps one image's pixels to *window-relative* input units. The window's
screen position is looked up again at the moment of acting, so a window that
moved after the screenshot is still hit where the model meant. A window that
changed size is not: the screenshot no longer describes it, and the tool says so.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    @property
    def size(self) -> tuple[int, int]:
        return (self.w, self.h)


def fit(w: int, h: int, max_edge: int, max_pixels: int = 0) -> tuple[int, int]:
    """The largest size within ``max_edge`` (and ``max_pixels``, if set) with the
    same aspect ratio. Never upscales. Send images at this size so no pipeline
    downscales them again and moves every coordinate."""
    s = min(1.0, max_edge / max(w, h))
    if max_pixels and w * h * s * s > max_pixels:
        s = (max_pixels / (w * h)) ** 0.5
    return max(1, round(w * s)), max(1, round(h * s))


@dataclass(frozen=True)
class Frame:
    shot: str  # "s3": how the model refers to the image
    window: str  # window label
    window_size: tuple[int, int]  # the window's size, in input units, when captured
    width: int  # image size, pixels
    height: int
    # Image pixel (0, 0) in window-relative input units, and input units per image pixel.
    ox: float
    oy: float
    sx: float
    sy: float

    @classmethod
    def for_capture(
        cls, shot: str, window: str, window_size: tuple[int, int], capture: tuple[int, int], image: tuple[int, int]
    ) -> Frame:
        """A whole-window screenshot: ``capture`` pixels scaled to ``image`` pixels."""
        ww, wh = window_size
        return cls(shot, window, window_size, image[0], image[1], 0.0, 0.0, ww / image[0], wh / image[1])

    def contains(self, mx: float, my: float) -> bool:
        return 0 <= mx < self.width and 0 <= my < self.height

    def to_window(self, mx: float, my: float) -> tuple[float, float]:
        return self.ox + mx * self.sx, self.oy + my * self.sy

    def to_screen(self, window_rect: Rect, mx: float, my: float) -> tuple[int, int]:
        x, y = self.to_window(mx, my)
        return round(window_rect.x + x), round(window_rect.y + y)

    def region(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        """An image-pixel region as window-relative input units (left, top, right, bottom)."""
        a, b = self.to_window(x0, y0)
        c, d = self.to_window(x1, y1)
        return a, b, c, d

    def zoom(self, shot: str, x0: float, y0: float, x1: float, y1: float, image: tuple[int, int]) -> Frame:
        """The frame of a crop of this image's (x0, y0)-(x1, y1), shown at ``image`` size."""
        ox, oy = self.to_window(x0, y0)
        return Frame(
            shot,
            self.window,
            self.window_size,
            image[0],
            image[1],
            ox,
            oy,
            (x1 - x0) * self.sx / image[0],
            (y1 - y0) * self.sy / image[1],
        )

    def same_size(self, rect: Rect, tolerance: int = 2) -> bool:
        return abs(rect.w - self.window_size[0]) <= tolerance and abs(rect.h - self.window_size[1]) <= tolerance
