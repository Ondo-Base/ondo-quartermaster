"""A small "legacy" desktop billing app for the desktop tests and demo (GTK 3).

It stands in for the internal app with no API: a form with named fields, a
Submit button, and one unnamed icon button, which real apps always have and a
per-app profile has to name. On Submit it writes what it saved to a JSON file
so tests can check what actually landed.

    python -m ondo_agent.demo.legacy_app --title "Legacy billing" --out saved.json
    python -m ondo_agent.demo.legacy_app --title "Password manager"   # a decoy window
"""

from __future__ import annotations

import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Legacy billing")
    ap.add_argument("--out", default="")
    ap.add_argument("--account", default="Halleck Logistics")
    ap.add_argument("--value", default="184500")
    a = ap.parse_args()

    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    win = Gtk.Window(title=a.title)
    win.set_default_size(420, 220)
    grid = Gtk.Grid(row_spacing=8, column_spacing=8, margin=12)

    def field(row: int, label: str, value: str) -> Gtk.Entry:
        lbl = Gtk.Label(label=label, xalign=0)
        e = Gtk.Entry()
        e.set_text(value)
        e.get_accessible().set_name(label)
        grid.attach(lbl, 0, row, 1, 1)
        grid.attach(e, 1, row, 1, 1)
        return e

    account = field(0, "Account", a.account)
    value = field(1, "Annual value", a.value)
    status = Gtk.Label(label="Not saved", xalign=0)
    status.get_accessible().set_name("Status")

    refresh = Gtk.Button()  # icon-only and unnamed, as legacy apps have
    refresh.add(Gtk.Image.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON))
    refresh.connect("clicked", lambda *_: status.set_text("Refreshed"))
    submit = Gtk.Button(label="Submit")

    def on_submit(*_):
        saved = {"account": account.get_text(), "annual_value": value.get_text()}
        if a.out:
            with open(a.out, "w") as f:
                json.dump(saved, f)
        status.set_text(f"Saved {saved['annual_value']}")

    submit.connect("clicked", on_submit)
    grid.attach(refresh, 0, 2, 1, 1)
    grid.attach(submit, 1, 2, 1, 1)
    grid.attach(status, 0, 3, 2, 1)
    win.add(grid)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
