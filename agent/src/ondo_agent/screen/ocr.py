"""Local OCR, through the tesseract binary. No network, no GPU.

Two uses: reading a window's text for a model that cannot see images, and the
OCR grounder (``grounding.py``). Tesseract reads small UI text badly at 1x, so
images are upscaled before reading and coordinates are scaled back.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    line: tuple[int, int, int]  # (block, paragraph, line)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def available(binary: str = "tesseract") -> bool:
    return shutil.which(binary) is not None


def auto_upscale(size: tuple[int, int]) -> float:
    """Tesseract reads UI text best at 25-35 pixels tall. A small window is
    usually a 1x capture with small text; a large one is usually HiDPI or a full
    screen. Scale towards a long edge of about 1400 pixels, between 1x and 3x."""
    return round(min(3.0, max(1.0, 1400 / max(size))), 2)


def _tesseract(img, psm: str, upscale: float, binary: str, timeout: float) -> list[tuple]:
    from PIL import Image

    if upscale != 1:
        img = img.resize((max(1, round(img.width * upscale)), max(1, round(img.height * upscale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = subprocess.run(
        [binary, "stdin", "stdout", "--psm", psm, "tsv"],
        input=buf.getvalue(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tesseract failed: {r.stderr.decode(errors='replace')[:300]}")
    out = []
    rows = csv.DictReader(io.StringIO(r.stdout.decode("utf-8", "replace")), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in rows:
        text = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1
        if text and conf >= 0:
            out.append((text, int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]), conf,
                        (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))))  # fmt: skip
    return out


def boxes(img) -> list[tuple[int, int, int, int]]:
    """Rectangles drawn with a border: buttons, input wells, panels. Tesseract's
    page segmentation tends to skip text inside them, so each is read on its own."""
    px = img.load()
    w, h = img.size
    runs: dict[int, list[tuple[int, int]]] = {}
    for y in range(h):
        x = 0
        while x < w:
            if px[x, y] < 200:
                s = x
                while x < w and px[x, y] < 200:
                    x += 1
                if x - s >= 30:
                    runs.setdefault(y, []).append((s, x - 1))
            x += 1

    def vertical(x: int, y0: int, y1: int) -> bool:
        return sum(1 for y in range(y0, y1 + 1) if px[x, y] < 200) >= 0.85 * (y1 - y0 + 1)

    def light_inside(x0: int, y0: int, x1: int, y1: int) -> bool:
        # A button or an input well: a border around a light inside. A solid dark
        # area (a title bar, the black beyond the screen edge) is not a box.
        xs = range(x0 + 3, x1 - 2, max(1, (x1 - x0) // 24))
        ys_ = range(y0 + 3, y1 - 2, max(1, (y1 - y0) // 8))
        vals = [px[x, y] for x in xs for y in ys_]
        return bool(vals) and sum(1 for v in vals if v >= 200) >= 0.6 * len(vals)

    found: list[tuple[int, int, int, int]] = []
    ys = sorted(runs)
    for i, yt in enumerate(ys):
        for x0, x1 in runs[yt]:
            for yb in ys[i + 1 :]:
                if yb - yt > 240:
                    break
                if yb - yt < 10:
                    continue
                if any(abs(a - x0) <= 3 and abs(b - x1) <= 3 for a, b in runs[yb]):
                    if vertical(x0, yt, yb) and vertical(x1, yt, yb) and light_inside(x0, yt, x1, yb):
                        found.append((x0, yt, x1, yb))
                    break
    # Thick borders produce the same box several times: keep the outermost.
    found.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    out: list[tuple[int, int, int, int]] = []
    for b in found:
        if not any(o[0] - 4 <= b[0] and o[1] - 4 <= b[1] and b[2] <= o[2] + 4 and b[3] <= o[3] + 4 and
                   (o[2] - o[0]) - (b[2] - b[0]) < 12 for o in out):  # fmt: skip
            out.append(b)
    return out[:40]


def words(image, *, binary: str = "tesseract", upscale: float | None = None, timeout: float = 30) -> list[Word]:
    """Every word tesseract finds, with its box in ``image`` pixels: one pass over
    the whole image, then one per bordered box for the text that pass skipped."""

    img = image.convert("L")
    if upscale is None:
        upscale = auto_upscale(img.size)
    out = [
        Word(t, round(x / upscale), round(y / upscale), max(1, round(ww / upscale)), max(1, round(hh / upscale)), c, ln)
        for t, x, y, ww, hh, c, ln in _tesseract(img, "11", upscale, binary, timeout)
    ]
    for n, (x0, y0, x1, y1) in enumerate(boxes(img)):
        inner = img.crop((x0 + 3, y0 + 3, x1 - 2, y1 - 2))
        if inner.width < 8 or inner.height < 8:
            continue
        up = min(4.0, max(0.5, 60 / inner.height))  # a single line, about 40 pixels tall
        for t, x, y, ww, hh, c, _ in _tesseract(inner, "7", up, binary, timeout):
            wd = Word(t, x0 + 3 + round(x / up), y0 + 3 + round(y / up), max(1, round(ww / up)), max(1, round(hh / up)),
                      c, (1000 + n, 0, 0))  # fmt: skip
            if not any(_overlaps(wd, o) for o in out):
                out.append(wd)
    return out


def _overlaps(a: Word, b: Word) -> bool:
    ix = max(0, min(a.right, b.right) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy > 0.3 * min(a.w * a.h, b.w * b.h)


def lines(ws: list[Word]) -> list[list[Word]]:
    """Words grouped into visual lines, top to bottom, left to right. Tesseract's
    own line numbers split a form row into pieces, so rows are rebuilt from where
    the words sit."""
    rows: list[list[Word]] = []
    for w in sorted(ws, key=lambda w: (w.cy, w.x)):
        for row in rows:
            ref = row[0]
            if abs(ref.cy - w.cy) <= max(ref.h, w.h) * 0.6:
                row.append(w)
                break
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w.x)
    rows.sort(key=lambda r: min(w.y for w in r))
    return rows


def read_text(image, *, binary: str = "tesseract") -> str:
    out = []
    for row in lines(words(image, binary=binary)):
        # Keep wide gaps visible, so "Annual value   184500" does not read as one phrase.
        parts = [row[0].text]
        for a, b in zip(row, row[1:], strict=False):
            parts.append(("   " if b.x - a.right > 2.5 * max(a.h, b.h) else " ") + b.text)
        out.append("".join(parts))
    return "\n".join(out)
