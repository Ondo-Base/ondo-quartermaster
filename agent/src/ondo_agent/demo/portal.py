"""A small billing portal: the "system of record with no API" from the design.

Plain server-rendered HTML with real labels, so its accessibility tree is what a
well-behaved internal web app looks like. Used by the browser end-to-end test and
the local demo. In-memory; nothing persists.
"""

from __future__ import annotations

import html
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .northwind import ACCOUNTS


def slug(name: str) -> str:
    return name.lower().replace(" ", "-")


@dataclass
class PortalState:
    values: dict[str, int] = field(default_factory=lambda: {a.name: a.current for a in ACCOUNTS})
    submissions: list[dict[str, int]] = field(default_factory=list)
    help_url: str = "https://help.example.invalid/"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title></head>
<body><header><nav aria-label="Portal"><a href="/">Billing portal</a> · <a href="/renewals">Renewal batch</a> ·
<a href="{help}">Help centre</a></nav></header><main><h1>{title}</h1>{body}</main></body></html>"""


def _page(title: str, body: str, state: PortalState) -> bytes:
    return PAGE.format(title=html.escape(title), body=body, help=html.escape(state.help_url)).encode()


def make_handler(state: PortalState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("content-type", "text/html; charset=utf-8")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                body = '<p>Northwind billing. <a href="/renewals">Open the Q3 renewal batch</a>.</p>'
                return self._send(200, _page("Billing portal", body, state))
            if self.path.startswith("/renewals"):
                rows = "".join(
                    f'<tr><th scope="row">{html.escape(n)}</th><td>{v:,}</td>'
                    f'<td><input name="{slug(n)}" value="{v}" inputmode="numeric" '
                    f'aria-label="Annual value — {html.escape(n)}"></td></tr>'
                    for n, v in state.values.items()
                )
                saved = ""
                if "saved=" in self.path:
                    saved = f'<p role="status">Saved {self.path.split("saved=")[1]} change(s).</p>'
                body = (
                    f'{saved}<form method="post" action="/renewals"><table><thead><tr><th>Account</th>'
                    f"<th>Current annual value</th><th>New annual value</th></tr></thead><tbody>{rows}</tbody>"
                    f'</table><button type="submit">Submit</button></form>'
                )
                return self._send(200, _page("Q3 renewal batch", body, state))
            self._send(404, _page("Not found", "<p>No such page.</p>", state))

        def do_POST(self):
            n = int(self.headers.get("content-length", "0"))
            form = parse_qs(self.rfile.read(n).decode())
            changed: dict[str, int] = {}
            for name in list(state.values):
                raw = (form.get(slug(name)) or [""])[0].replace(",", "").strip()
                if raw.isdigit() and int(raw) != state.values[name]:
                    changed[name] = int(raw)
            state.values.update(changed)
            state.submissions.append(changed)
            self._send(303, b"", {"location": f"/renewals?saved={len(changed)}"})

    return Handler


class Portal:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.state = PortalState()
        self.server = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.url = f"http://{host}:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Portal:
        self._thread.start()
        return self

    def __exit__(self, *a) -> None:
        self.server.shutdown()
        self.server.server_close()
