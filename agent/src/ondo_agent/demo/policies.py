"""Scripted models for the demo tasks and the end-to-end tests.

These are deterministic stand-ins for an orchestrator. They act only on what the
tools return (nothing about the files is hard-coded), so a test that passes with
them proves the harness, the tools, the permissions and the gates work — and the
same tests run unchanged against a real provider when one is configured.

``renewal_pack_policy`` is text-only on purpose: it never asks for an image.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models.adapters.scripted import call, say
from ..models.types import Message, ModelResponse

_FEE = re.compile(r"annual fee for the current term is ([\d,]+) GBP")
_UPLIFT = re.compile(r"increases by ([\d.]+)% to ([\d,]+) GBP")
_NOUPLIFT = re.compile(r"No uplift applies")
_PARTY = re.compile(r"MASTER SERVICES AGREEMENT - ([A-Z0-9 &]+)")


def _tool_results(messages: list[Message]) -> list[tuple[str, str]]:
    return [(m.tool_name or "", m.text) for m in messages if m.role == "tool"]


def _turns(messages: list[Message]) -> int:
    return sum(1 for m in messages if m.role == "assistant")


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def parse_contracts(results: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, text in results:
        if name != "read_file":
            continue
        fm = re.search(r"File: (\S+)", text)
        party = _PARTY.search(text)
        fee = _FEE.search(text)
        if not (fm and party and fee):
            continue
        up = _UPLIFT.search(text)
        out[party.group(1).strip().title()] = {
            "file": fm.group(1),
            "current": _num(fee.group(1)),
            "uplift": float(up.group(1)) if up else 0.0,
            "new": _num(up.group(2)) if up else _num(fee.group(1)),
            "clause": "7.2",
        }
    return out


def parse_workbook(results: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for name, text in results:
        if name != "read_file" or 'Sheet "Renewals"' not in text:
            continue
        for line in text.splitlines():
            m = re.match(r"A(\d+): ([^|]+?) \| B\1: (\S+) .*?D\1: ([\d.]+) \| E\1: ([\d.]+)", line)
            if m:
                rows[m.group(2).strip().title()] = {
                    "row": int(m.group(1)),
                    "file": m.group(3),
                    "current": int(float(m.group(4))),
                    "pct": float(m.group(5)),
                }
    return rows


def renewal_pack_policy(drive: Path, *, threshold_pct: float = 5.0):
    """ "Build the Q3 renewal pack for Northwind from the contracts in the client folder,
    and flag anything that uplifts above five per cent." Files only."""
    contracts_dir = drive / "Contracts"
    workbook = drive / "Q3_Renewals.xlsx"
    pack = drive / "Q3_Renewal_Pack.xlsx"

    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        if t == 0:
            # Independent reads go in one turn.
            return call(
                ("list_folder", {"path": str(drive)}),
                ("list_folder", {"path": str(contracts_dir)}),
                text="I'll look through the client folder first.",
            )
        if t == 1:
            listing = next((r for n, r in results if n == "list_folder" and (".pdf" in r or ".docx" in r)), "")
            files = re.findall(r"^(\S+\.(?:pdf|docx))\s", listing, re.MULTILINE)
            calls = [("read_file", {"path": str(contracts_dir / f)}) for f in files]
            calls.append(("read_file", {"path": str(workbook)}))
            return call(*calls)
        contracts = parse_contracts(results)
        book = parse_workbook(results)
        if t == 2:
            edits, disagreements = [], []
            for name, row in book.items():
                c = contracts.get(name)
                if c and abs(c["uplift"] - row["pct"]) > 1e-9:
                    edits.append({"sheet": "Renewals", "cell": f"E{row['row']}", "value": c["uplift"]})
                    disagreements.append(name)
            rows: list[list[Any]] = [
                ["Account", "Contract", "Current annual value", "Uplift %", "New annual value", "Change", "Flag"]
            ]
            for name in sorted(contracts):
                c = contracts[name]
                flag = []
                if c["uplift"] > threshold_pct:
                    flag.append(f"above {threshold_pct:g}%")
                if name in disagreements:
                    flag.append(
                        f"workbook said {book[name]['pct']:g}%; contract clause {c['clause']} says {c['uplift']:g}%"
                    )
                rows.append(
                    [name, c["file"], c["current"], c["uplift"], c["new"], c["new"] - c["current"], "; ".join(flag)]
                )
            calls = []
            if edits:
                calls.append(
                    (
                        "edit_workbook",
                        {
                            "path": str(workbook),
                            "edits": edits,
                            "reason": "Match the signed contracts: " + ", ".join(disagreements) + ".",
                        },
                    )
                )
            calls.append(
                ("create_workbook", {"path": str(pack), "sheets": [{"name": "Q3 renewal pack", "rows": rows}]})
            )
            return call(*calls)
        # Report.
        changed = {n: c for n, c in contracts.items() if c["new"] != c["current"]}
        total = sum(c["new"] - c["current"] for c in changed.values())
        above = [n for n, c in contracts.items() if c["uplift"] > threshold_pct]
        disagreements = [n for n, r in book.items() if n in contracts and abs(contracts[n]["uplift"] - r["pct"]) > 1e-9]
        not_saved = [text for n, text in results if text.startswith("Not written")]
        lines = [
            f"I read {len(contracts)} contracts and the Q3 workbook.",
            f"{len(changed)} of {len(contracts)} accounts change at renewal, adding {total:,} to total annual value.",
        ]
        for n in disagreements:
            lines.append(
                f"{n}: the workbook had {book[n]['pct']:g}%; the signed contract (clause 7.2) says "
                f"{contracts[n]['uplift']:g}%."
            )
        lines.append(f"Above {threshold_pct:g}%: " + (", ".join(above) if above else "none."))
        if not_saved:
            lines.append("Some changes were not approved, so nothing was saved for them.")
        else:
            lines.append(f"The pack is saved as {pack.name}.")
        return say("\n".join(lines))

    return policy


_FIELD = re.compile(r'textbox "Annual value — ([^"]+)" \[ref=([A-Za-z0-9_-]+)\](?:: "?(\d+)"?)?')
_BUTTON = re.compile(r'button "Submit" \[ref=([A-Za-z0-9_-]+)\]')


def renewal_submit_policy(drive: Path, portal_url: str):
    """ "Key the Q3 renewal changes from the signed contracts into the billing portal."
    Document store (granted folder) and a web portal, driven through the
    accessibility tree only. Text-only: no images are requested or read."""
    contracts_dir = drive / "Contracts"

    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        last = results[-1][1] if results else ""
        if t == 0:
            return call(("list_folder", {"path": str(contracts_dir)}))
        if t == 1:
            files = re.findall(r"^(\S+\.(?:pdf|docx))\s", last, re.MULTILINE)
            return call(*[("read_file", {"path": str(contracts_dir / f)}) for f in files])
        contracts = parse_contracts(results)
        if t == 2:
            return call(("browser_navigate", {"url": portal_url.rstrip("/") + "/renewals"}))
        if t == 3:
            fields = []
            for name, ref, current in _FIELD.findall(last):
                c = contracts.get(name.title())
                if c and current and c["new"] != int(current):
                    fields.append(
                        {"ref": ref, "name": f"Annual value — {name}", "type": "textbox", "value": str(c["new"])}
                    )
            if not fields:
                return say("The portal already matches the signed contracts. Nothing to key.")
            return call(("browser_fill_form", {"fields": fields}))
        if t == 4:
            m = _BUTTON.search(last)
            if not m:
                return say("I could not find the Submit button on the renewal batch page.")
            return call(("browser_click", {"ref": m.group(1), "element": "Submit button"}))
        saved = re.search(r"Saved (\d+) change", last)
        if saved:
            return say(f"The billing portal saved {saved.group(1)} change(s) from the signed contracts.")
        if "not approved" in last:
            return say("The submission was not approved, so nothing was saved in the billing portal.")
        return say("I could not confirm the submission. Please check the renewal batch page.")

    return policy


def demo_router(cfg, profile):
    """The ``scripted`` demo profile: routes a request to one of the demo policies.

    For running the product end to end with no model provider configured. It is
    labelled as scripted in every log event, so nothing it does is mistaken for
    a model's work.
    """
    drive = cfg.resolve(profile.extra.get("drive", "demo/Northwind client drive"))
    portal = profile.extra.get("portal_url", "http://127.0.0.1:8765")
    chosen: dict[str, Any] = {}

    def policy(messages: list[Message], tools) -> ModelResponse:
        if "p" not in chosen:
            request = next(
                (m.text for m in messages if m.role == "user" and not m.text.startswith("<environment>")), ""
            )
            r = request.lower()
            if any(w in r for w in ("billing app", "desktop app", "legacy")):
                chosen["p"] = legacy_app_policy(drive, profile.extra.get("legacy_window", "Legacy billing"))
            elif any(w in r for w in ("portal", "billing system", "key the", "submit")):
                chosen["p"] = renewal_submit_policy(drive, portal)
            elif "row 14" in r or "why" in r:
                chosen["p"] = _spreadsheet_question(drive)
            else:
                chosen["p"] = renewal_pack_policy(drive)
        return chosen["p"](messages, tools)

    return policy


def _spreadsheet_question(drive: Path):
    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        if t == 0:
            return call(
                ("read_file", {"path": str(drive / "Q3_Renewals.xlsx")}),
                ("read_file", {"path": str(drive / "Contracts" / "Halleck_MSA_2026.pdf")}),
            )
        book = parse_workbook(results)
        contracts = parse_contracts(results)
        row = next(((n, r) for n, r in book.items() if r["row"] == 14), None)
        if not row:
            return say("I could not find row 14 in the workbook.")
        name, r = row
        c = contracts.get(name)
        if c and abs(c["uplift"] - r["pct"]) > 1e-9:
            return say(
                f"Row 14 is {name}. The workbook has a {r['pct']:g}% uplift; the signed contract "
                f"({c['file']}, clause 7.2) says {c['uplift']:g}%. The workbook figure looks like it was "
                f"carried over from last year's pack.\n\nI can correct row 14 and re-run the pack totals. Shall I?"
            )
        return say(f"Row 14 is {name}, and it matches the contract.")

    return policy


def legacy_app_policy(drive: Path, window: str = "Legacy billing", *, between=None):
    """ "Update Halleck Logistics' annual value in the legacy billing app from the signed
    contract." Reads the contract, then drives the app by naming controls in words.
    It never inspects the window to pick a target: the decision layer does that.
    ``between`` runs after typing and before submitting (the tests move the window)."""
    contract = drive / "Contracts" / "Halleck_MSA_2026.pdf"

    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        last = results[-1][1] if results else ""
        if t == 0:
            return call(("read_file", {"path": str(contract)}))
        c = next(iter(parse_contracts(results).values()), None)
        if t == 1:
            if not c:
                return say("I could not read the new annual value from the contract.")
            return call(
                (
                    "desktop_act",
                    {"window": window, "target": "Annual value field", "action": "set_text", "text": str(c["new"])},
                )
            )
        if t == 2:
            if between:
                between()
            return call(("desktop_act", {"window": window, "target": "the Submit button", "action": "click"}))
        if t == 3:
            if "not approved" in last:
                return say("The change was not approved, so nothing was submitted in the billing app.")
            return call(("desktop_inspect", {"window": window}))
        m = re.search(r'label "Status" \[ref=\w+\]: "([^"]*)"', last)
        return say(f"The billing app says: {m.group(1)}." if m else "I could not read the app's status.")

    return policy


def remote_app_policy(drive: Path, window: str = "Remote billing", *, between=None):
    """ "Update Halleck Logistics' annual value in the remote billing session from the
    signed contract." Text-only: it never sees a pixel. It walks the ladder (the
    accessibility tree first), then operates the window by naming targets in words
    and reads the outcome from the OCR text that comes back with each screenshot.
    ``between`` runs after typing and before submitting (the tests move the window)."""
    contract = drive / "Contracts" / "Halleck_MSA_2026.pdf"

    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        last = results[-1][1] if results else ""
        if t == 0:
            return call(("read_file", {"path": str(contract)}))
        c = next(iter(parse_contracts(results).values()), None)
        if t == 1:
            if not c:
                return say("I could not read the new annual value from the contract.")
            return call(("desktop_inspect", {"window": window}))
        if t == 2:
            if "screen_act" not in last:
                return say("The window has controls I can use directly; this policy only covers remote sessions.")
            return call(
                (
                    "screen_act",
                    {
                        "window": window,
                        "actions": [
                            {"action": "type", "target": "Annual value field", "text": str(c["new"]), "replace": True}
                        ],
                    },
                )
            )
        if t == 3:
            if "FAILED" in last:
                return say("Typing the new value failed, so I stopped before submitting.")
            if between:
                between()
            return call(
                ("screen_act", {"window": window, "actions": [{"action": "click", "target": "the Submit button"}]})
            )
        if "not approved" in last:
            return say("The change was not approved, so nothing was submitted in the billing session.")
        m = re.search(r"Status: ([^\n<]*)", last)
        return say(f"The billing session says: {m.group(1).strip()}." if m else "I could not read the status.")

    return policy


def ticket_reply_policy(drive: Path, customer: str = "Halleck"):
    """ "Reply to Halleck's renewal ticket with the new annual value from the signed
    contract, and mark it as waiting on the customer." Finds the ticket, reads it and
    the contract together, replies publicly with the figure and its source, then
    sets the status. Acts only on what the tools return."""
    contract = drive / "Contracts" / "Halleck_MSA_2026.pdf"

    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        last = results[-1][1] if results else ""
        if t == 0:
            return call(("ticketing_search_tickets", {"query": f"{customer} renewal", "status": "open"}))
        if t == 1:
            m = re.search(r'"id": "(NW-\d+)"', last)
            if not m:
                return say(f"I could not find an open renewal ticket for {customer}." + _why(last))
            return call(("ticketing_get_ticket", {"id": m.group(1)}), ("read_file", {"path": str(contract)}))
        ticket = re.search(r'"id": "(NW-\d+)"', "\n".join(x for _, x in results if "NW-" in x))
        c = next(iter(parse_contracts(results).values()), None)
        if t == 2:
            if not (ticket and c):
                return say("I could not read the ticket and the contract together.")
            body = (
                f"Hello, the annual value for the new term is {c['new']:,} GBP, as set out in clause "
                f"{c['clause']} of the signed agreement ({c['file']}). Kind regards, Northwind Operations"
            )
            return call(("ticketing_add_comment", {"id": ticket.group(1), "body": body, "public": True}))
        if t == 3:
            if "not approved" in last:
                return say("The reply was not approved, so nothing was sent to the customer.")
            return call(("ticketing_update_ticket", {"id": ticket.group(1), "status": "pending"}))
        if "not approved" in last:
            return say(f"I replied on {ticket.group(1)}, but the status change was not approved.")
        return say(f"I replied on {ticket.group(1)} with {c['new']:,} GBP and set it to waiting on the customer.")

    return policy


def _why(text: str) -> str:
    m = re.search(r"Permission denied: ([^.]*)", text)
    return f" ({m.group(1)})" if m else ""


def mail_and_calendar_policy(drive: Path):
    """ "Answer Priya's email with the renewal value from the signed contract, and
    offer her a 30-minute call next week." Finds the email, reads it and the
    contract, drafts and sends the reply, finds free time and invites her."""
    contract = drive / "Contracts" / "Halleck_MSA_2026.pdf"

    def policy(messages: list[Message], tools) -> ModelResponse:
        t = _turns(messages)
        results = _tool_results(messages)
        last = results[-1][1] if results else ""
        joined = "\n".join(x for _, x in results)
        if t == 0:
            return call(("mail_search_mail", {"query": "renewal"}))
        if t == 1:
            m = re.search(r'"id": "(msg-\d+)"', last)
            if not m:
                return say("I could not find Priya's email." + _why(last))
            return call(("mail_read_message", {"id": m.group(1)}), ("read_file", {"path": str(contract)}))
        c = next(iter(parse_contracts(results).values()), None)
        msg = re.search(r'"id": "(msg-\d+)"[^}]*"from": "([^"]+)"', joined)
        if t == 2:
            if not (c and msg):
                return say("I could not read the email and the contract together.")
            body = (
                f"Hi Priya, the annual value for the renewed term is {c['new']:,} GBP (clause {c['clause']} of "
                f"the signed agreement). Happy to talk it through; I'll send a time. Best, Mara"
            )
            return call(
                (
                    "mail_create_draft",
                    {
                        "to": msg.group(2),
                        "subject": "Re: Renewal value for the new term",
                        "body": body,
                        "reply_to": msg.group(1),
                    },
                )
            )
        if t == 3:
            d = re.search(r'"id": "(draft-\d+)"', last)
            return call(("mail_send_draft", {"id": d.group(1)})) if d else say("The draft was not saved.")
        if t == 4:
            if "not approved" in last:
                return say("The reply was not approved, so nothing was sent.")
            return call(
                (
                    "calendar_find_free_time",
                    {"duration_minutes": 30, "start": "2026-10-06T00:00", "end": "2026-10-10T23:59"},
                )
            )
        if t == 5:
            slot = re.search(r'"start": "([^"]+)", "end": "([^"]+)"', last)
            if not slot:
                return say("I sent the reply but found no free half hour next week.")
            return call(
                (
                    "calendar_create_event",
                    {
                        "title": "Halleck renewal call",
                        "start": slot.group(1),
                        "end": slot.group(2),
                        "attendees": msg.group(2) if msg else "",
                    },
                )
            )
        if "not approved" in last:
            return say("I sent the reply, but the invitation was not approved.")
        ev = re.search(r'"start": "([^"]+)"', last)
        return say(
            f"I replied to Priya with {c['new']:,} GBP and invited her to a call at {ev.group(1) if ev else '?'}."
        )

    return policy
