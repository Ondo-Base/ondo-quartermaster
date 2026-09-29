"""A stand-in for a Citrix or remote-desktop window, for the Stage 5 tests and demo.

The whole form is pixels on one canvas: no buttons, fields or labels exist as
widgets, so the accessibility tree shows one unnamed drawing and nothing to act
on. That is exactly what a remote session looks like to the local machine, and
why the ladder bottoms out in pixels for it.

The app does its own hit-testing and keyboard handling, the way a remote session
forwards input: click a box to focus it, type, Ctrl+A to select all, Backspace,
Tab, and Submit (or Enter) saves. On save it writes what landed to a JSON file so
tests can check the real end state.

    python -m ondo_agent.demo.remote_app --out saved.json
"""

from __future__ import annotations

import argparse
import json

TITLE = "Remote billing — Citrix Workspace"

# Logical layout (GTK scales it on HiDPI).
W, H = 640, 360
FIELDS = [("Account", 72), ("Annual value", 122)]
BOX_X, BOX_W, BOX_H = 190, 300, 34
SUBMIT = (190, 180, 110, 36)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default=TITLE)
    ap.add_argument("--out", default="")
    ap.add_argument("--account", default="Halleck Logistics")
    ap.add_argument("--value", default="184500")
    a = ap.parse_args()

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    GLib.set_prgname("wfica")  # what a Citrix session is called on the machine
    values = [a.account, a.value]
    state = {"focus": -1, "select_all": False, "status": "Not saved"}

    win = Gtk.Window(title=a.title)
    win.set_default_size(W, H)
    area = Gtk.DrawingArea()
    area.set_can_focus(True)
    area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.KEY_PRESS_MASK)

    def text(cr, x, y, s, size=17, rgb=(0.1, 0.12, 0.15), bold=False):
        import cairo

        cr.select_font_face(
            "DejaVu Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL
        )
        cr.set_font_size(size)
        cr.set_source_rgb(*rgb)
        cr.move_to(x, y)
        cr.show_text(s)

    def draw(_w, cr):
        cr.set_source_rgb(0.89, 0.9, 0.92)  # the form: light grey, never white
        cr.paint()
        cr.set_source_rgb(0.17, 0.2, 0.25)
        cr.rectangle(0, 0, W * 4, 40)
        cr.fill()
        text(cr, 16, 26, "HALLECK BILLING - remote session", 15, (0.95, 0.96, 0.97), bold=True)
        for i, (label, y) in enumerate(FIELDS):
            text(cr, 32, y + 23, label)
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(BOX_X, y, BOX_W, BOX_H)
            cr.fill()
            focused = state["focus"] == i
            cr.set_source_rgb(*((0.18, 0.45, 0.82) if focused else (0.5, 0.54, 0.6)))
            cr.set_line_width(2 if focused else 1)
            cr.rectangle(BOX_X + 0.5, y + 0.5, BOX_W - 1, BOX_H - 1)
            cr.stroke()
            text(cr, BOX_X + 10, y + 23, values[i])
        x, y, w, h = SUBMIT
        cr.set_source_rgb(0.8, 0.83, 0.87)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgb(0.4, 0.44, 0.5)
        cr.set_line_width(1)
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.stroke()
        text(cr, x + 26, y + 24, "Submit", bold=True)
        text(cr, 32, 262, f"Status: {state['status']}")
        return False

    def save():
        saved = {"account": values[0], "annual_value": values[1]}
        if a.out:
            with open(a.out, "w") as f:
                json.dump(saved, f)
        state["status"] = f"Saved {values[1]}"

    def on_click(_w, ev):
        area.grab_focus()
        x, y = ev.x, ev.y
        state["focus"] = -1
        for i, (_label, fy) in enumerate(FIELDS):
            if BOX_X <= x < BOX_X + BOX_W and fy <= y < fy + BOX_H:
                state["focus"] = i
        sx, sy, sw, sh = SUBMIT
        if sx <= x < sx + sw and sy <= y < sy + sh:
            save()
        state["select_all"] = False
        area.queue_draw()
        return True

    def on_key(_w, ev):
        name = Gdk.keyval_name(ev.keyval) or ""
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        f = state["focus"]
        if name in ("Return", "KP_Enter"):
            save()
        elif name in ("Tab", "ISO_Left_Tab"):
            state["focus"] = (f + 1) % len(FIELDS)
        elif f >= 0 and ctrl and name.lower() == "a":
            state["select_all"] = True
        elif f >= 0 and name == "BackSpace":
            values[f] = "" if state["select_all"] else values[f][:-1]
            state["select_all"] = False
        elif f >= 0 and ev.string and ev.string.isprintable() and not ctrl:
            values[f] = ev.string if state["select_all"] else values[f] + ev.string
            state["select_all"] = False
        area.queue_draw()
        return True

    area.connect("draw", draw)
    area.connect("button-press-event", on_click)
    area.connect("key-press-event", on_key)
    win.add(area)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    area.grab_focus()
    Gtk.main()


if __name__ == "__main__":
    main()
