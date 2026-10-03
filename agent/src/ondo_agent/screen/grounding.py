"""Grounding: turning "the Submit button" into a point on a screenshot.

A specialist job, kept behind one small interface so it can be done by whatever
is best and allowed: a strong hosted model, or a small open-weight model inside
the customer's network so screenshots never leave the building. Swapping it is
configuration; the executor that clicks never changes.

- ``VisionGrounder`` — any OpenAI-compatible vision endpoint. Point it at a
  self-hosted vLLM serving UI-TARS or OS-Atlas (docs/design.md's recommendation), or at
  a hosted model. Understands the coordinate formats those models emit.
- ``OcrGrounder`` — tesseract, on the CPU. Finds labelled targets: buttons by
  their text, fields by the label beside them. The baseline that runs anywhere,
  CI included; it refuses what it cannot see rather than guessing.

Both return a point in the pixels of the image they were given, or None.
"""

from __future__ import annotations

import base64
import difflib
import io
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from . import ocr


@dataclass
class Located:
    x: float
    y: float
    confidence: float
    how: str  # which grounder, and what it matched
    box: tuple[float, float, float, float] | None = None


class GroundingError(Exception):
    """The grounder could not answer usefully (ambiguous, unreachable). The message
    is for the model."""


class Grounder(Protocol):
    name: str

    async def locate(self, image: Any, target: str) -> Located | None: ...


# -- OCR ----------------------------------------------------------------------------------

_FIELD = re.compile(r"\b(field|box|input|textbox|entry|text area)\b", re.I)
_FILLER = {
    "the", "a", "an", "button", "field", "box", "input", "text", "link", "tab", "menu", "item", "entry",
    "label", "labelled", "labeled", "called", "named", "textbox", "icon", "on", "in", "area", "control",
}  # fmt: skip


_QUOTED = re.compile(r"[\"“”']([^\"“”']+)[\"“”']")


def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", s.lower())


def phrase_of(target: str) -> list[str]:
    # Linear: the run inside the quotes can never contain a quote of any kind,
    # and the description is short by nature.
    quoted = _QUOTED.findall(target[:300])
    words = (quoted[0] if quoted else target[:300]).split()
    return [n for n in (_norm(w) for w in words) if n and (quoted or n not in _FILLER)]


class OcrGrounder:
    name = "ocr"

    def __init__(self, binary: str = "tesseract", min_confidence: float = 0.5):
        self.binary = binary
        self.min_confidence = min_confidence

    async def locate(self, image: Any, target: str) -> Located | None:
        import asyncio

        return await asyncio.to_thread(self._locate, image, target)

    def _locate(self, image: Any, target: str) -> Located | None:
        phrase = phrase_of(target)
        if not phrase:
            raise GroundingError(f'"{target}" does not name any text to look for.')
        # Tesseract drops text at some scales (button captions inside a border,
        # especially), so try the size it likes best first, then two others.
        matches: list[list[ocr.Word]] = []
        first = ocr.auto_upscale(image.size)
        for scale in dict.fromkeys((first, 1.0, 2.0)):
            matches = _find(ocr.words(image, binary=self.binary, upscale=scale), phrase)
            if matches:
                break
        if not matches:
            return None
        if len(matches) > 1:
            where = ", ".join(f"({round(m[0].x)},{round(m[0].y)})" for m in matches[:4])
            raise GroundingError(f'"{target}" appears {len(matches)} times on screen, at {where}. Say which one.')
        m = matches[0]
        x0, y0 = min(w.x for w in m), min(w.y for w in m)
        x1, y1 = max(w.right for w in m), max(w.y + w.h for w in m)
        conf = sum(w.conf for w in m) / len(m) / 100
        if _FIELD.search(target):
            box = field_beside(image, (x0, y0, x1, y1))
            if box is None:
                raise GroundingError(
                    f'I found the label "{" ".join(w.text for w in m)}" but no input box beside it. '
                    "Zoom in, or give the point."
                )
            bx0, by0, bx1, by1 = box
            return Located(
                (bx0 + bx1) / 2, (by0 + by1) / 2, conf, f"ocr: field beside “{' '.join(w.text for w in m)}”", box
            )
        return Located(
            (x0 + x1) / 2, (y0 + y1) / 2, conf, f"ocr: text “{' '.join(w.text for w in m)}”", (x0, y0, x1, y1)
        )


def _find(ws: list[ocr.Word], phrase: list[str]) -> list[list[ocr.Word]]:
    found = []
    for row in ocr.lines(ws):
        normed = [_norm(w.text) for w in row]
        for i in range(len(row) - len(phrase) + 1):
            score = [difflib.SequenceMatcher(None, normed[i + k], phrase[k]).ratio() for k in range(len(phrase))]
            if min(score) >= 0.8:
                found.append(row[i : i + len(phrase)])
    return found


def field_beside(image: Any, label: tuple[float, float, float, float]) -> tuple[int, int, int, int] | None:
    """The input box to the right of a label (or, failing that, just below it):
    a run of near-white pixels at least 40 wide, on a row through the label.
    Forms draw inputs as white wells; that is all this assumes."""
    img = image.convert("L")
    px = img.load()
    w, h = img.size
    x0, y0, x1, y1 = (round(v) for v in label)
    th = max(8, y1 - y0)

    def white_run(y: int, start: int, stop: int) -> tuple[int, int] | None:
        x = start
        while x < stop:
            if px[x, y] >= 245:
                s = x
                while x < stop and px[x, y] >= 245:
                    x += 1
                if x - s >= 40:
                    return s, x
            x += 1
        return None

    def border(x: int, top: int, bot: int) -> bool:
        col = [px[x, y] for y in range(top, bot + 1)]
        return sum(1 for v in col if v < 235) >= 0.8 * len(col)

    def widen(run: tuple[int, int], top: int, bot: int) -> tuple[int, int]:
        # Text already in the box splits the white run: grow it back to the
        # box's own left border, never past the label.
        s, e = run
        while s - 1 > x1 and not border(s - 1, top, bot):
            s -= 1
        return s, e

    def extent_down_up(xm: int, y: int) -> tuple[int, int]:
        top = y
        while top > 0 and px[xm, top - 1] >= 245:
            top -= 1
        bot = y
        while bot < h - 1 and px[xm, bot + 1] >= 245:
            bot += 1
        return top, bot

    cy = (y0 + y1) // 2
    if px[min(w - 1, x1 + 3), cy] < 245:  # the label sits on a non-white form
        run = white_run(cy, x1 + 2, min(w, x1 + 40 * th))
        if run:
            top, bot = extent_down_up((run[0] + run[1]) // 2, cy)
            run = widen(run, top, bot)
            return run[0], top, run[1], bot
        for y in range(y1 + 2, min(h, y1 + 3 * th)):
            run = white_run(y, max(0, x0 - th), min(w, x1 + 40 * th))
            if run:
                top, bot = extent_down_up((run[0] + run[1]) // 2, y)
                return run[0], top, run[1], bot
    return None


# -- a vision model -----------------------------------------------------------------------

DEFAULT_PROMPT = (
    "You are a GUI grounding model. In the screenshot, find the element described below and answer with only "
    "its centre point as (x,y).\nElement: {target}"
)

_BOX_TOKENS = re.compile(r"<\|box_start\|>\s*\(([\d.]+),\s*([\d.]+)\)\s*,\s*\(([\d.]+),\s*([\d.]+)\)\s*<\|box_end\|>")
_BOX = re.compile(r"\[\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]")
_POINT_TAG = re.compile(r"<point>\s*([\d.]+)[\s,]+([\d.]+)\s*</point>")
_POINT = re.compile(r"\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)")


def parse_point(text: str) -> tuple[float, float] | None:
    """The point a grounding model answered, in its own coordinate space.
    OS-Atlas and Qwen-VL box tokens, [x1,y1,x2,y2] boxes, <point>x y</point>, and
    UI-TARS's click(start_box='(x,y)') are all understood."""
    for pat in (_BOX_TOKENS, _BOX):
        m = pat.search(text)
        if m:
            a, b, c, d = (float(g) for g in m.groups())
            return (a + c) / 2, (b + d) / 2
    for pat in (_POINT_TAG, _POINT):
        m = pat.search(text)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None


class VisionGrounder:
    name = "vision"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str = "",
        coords: str = "relative_1000",
        max_long_edge: int = 1280,
        prompt: str = DEFAULT_PROMPT,
        timeout: float = 60,
        client: httpx.AsyncClient | None = None,
    ):
        if coords not in ("relative_1000", "relative_1", "absolute"):
            raise ValueError("coords must be relative_1000, relative_1 or absolute")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = os.environ.get(api_key_env) if api_key_env else None
        self.coords = coords
        self.max_long_edge = max_long_edge
        self.prompt = prompt
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def locate(self, image: Any, target: str) -> Located | None:
        from .geometry import fit

        w, h = fit(image.width, image.height, self.max_long_edge)
        sent = image if (w, h) == image.size else image.resize((w, h))
        buf = io.BytesIO()
        sent.convert("RGB").save(buf, "PNG")
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt.format(target=target)},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()},
                        },
                    ],
                }
            ],
        }
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = await self._client.post(self.base_url + "/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise GroundingError(f"the grounding model did not answer ({type(e).__name__}).") from e
        p = parse_point(text)
        if p is None:
            return None
        x, y = p
        if self.coords == "relative_1000":
            x, y = x * w / 1000, y * h / 1000
        elif self.coords == "relative_1":
            x, y = x * w, y * h
        if not (0 <= x <= w and 0 <= y <= h):
            return None
        # Back from the size it was shown at to the image we were given.
        return Located(x * image.width / w, y * image.height / h, 1.0, f"vision: {self.model}")

    async def aclose(self) -> None:
        await self._client.aclose()


def make_grounder(cfg: dict[str, Any] | None) -> Grounder | None:
    cfg = dict(cfg or {})
    provider = cfg.pop("provider", "ocr")
    if provider in ("none", "", None):
        return None
    if provider == "ocr":
        binary = cfg.get("tesseract", "tesseract")
        return OcrGrounder(binary, float(cfg.get("min_confidence", 0.5))) if ocr.available(binary) else None
    if provider == "vision":
        return VisionGrounder(
            cfg["base_url"],
            cfg["model"],
            api_key_env=cfg.get("api_key_env", ""),
            coords=cfg.get("coords", "relative_1000"),
            max_long_edge=int(cfg.get("max_long_edge", 1280)),
            prompt=cfg.get("prompt", DEFAULT_PROMPT),
        )
    raise ValueError(f"unknown grounding provider {provider!r}")
