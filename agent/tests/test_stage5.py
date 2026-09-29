"""Stage 5: pixels, as the floor.

Done when: a Citrix or remote-desktop window can be operated; the harness picks
pixels only after trying the ladder above; and grounding can be switched to a
locally hosted model without touching the executor.

The "remote session" is ``demo/remote_app.py``: a GTK window whose whole form is
drawn on one canvas, so its accessibility tree has nothing to act on, exactly as
a Citrix window looks to the local machine. It runs on a virtual X display at 1x
and 2x, and is moved mid-task. Grounding is local OCR (tesseract) or a local
OpenAI-compatible vision endpoint standing in for a self-hosted UI-TARS.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import base_config
from desktop_env import screen_available

from ondo_agent import log as L
from ondo_agent.approvals import AutoApprovals
from ondo_agent.demo.policies import remote_app_policy
from ondo_agent.models.adapters.scripted import call, say
from ondo_agent.models.gateway import scripted_client
from ondo_agent.models.types import ImagePart
from ondo_agent.runtime import assemble

needs_screen = pytest.mark.skipif(
    not screen_available(), reason="needs the Stage 4 desktop plus tesseract and gi-cairo"
)
THRESHOLDS = str(Path(__file__).resolve().parents[1] / "config" / "thresholds.json")
REMOTE = "Remote billing"


def screen_config(drive, tmp_path, *, grounding=None, pixel_only=None, screen_windows=(REMOTE, "Legacy billing")):
    screen = {"enabled": True}
    if grounding is not None:
        screen["grounding"] = grounding
    if pixel_only is not None:
        screen["pixel_only_windows"] = pixel_only
    return base_config(
        drive,
        tmp_path,
        grants={
            "files": {"granted": True, "folders": [str(drive)]},
            "screen": {"granted": True, "windows": list(screen_windows)},
            "input": {"granted": True},
        },
        policy={"excluded_paths": ["**/HR/**"], "excluded_windows": ["Password manager", "Personal mail", "HR portal"]},
        desktop={"enabled": True, "escape_twice": True},
        screen=screen,
        gates={"thresholds_file": THRESHOLDS, "rules": []},
    )


def tool_results(a):
    return [e.data for e in a.log.of_type(L.TOOL_RESULT)]


@needs_screen
@pytest.mark.parametrize("scale", [1, 2])
async def test_remote_session_operated_by_pixels_survives_move_and_rescale(desktop, drive, tmp_path, scale):
    saved = tmp_path / f"saved-{scale}.json"
    app = desktop.launch_remote(out=saved, scale=scale)
    try:
        moved = {}

        def move():  # between typing and submitting: somewhere else, and a different size
            if scale == 1:
                desktop.move(REMOTE, 480, 300, 700, 420)
            else:
                desktop.move(REMOTE, 180, 150, 1000, 600)
            moved["at"] = time.time()

        approvals = AutoApprovals(True, by="mara.okonjo")
        model = scripted_client(remote_app_policy(drive, between=move), name="text-only", supports_vision=False)
        # pixel_only=[]: no shortcut for known remote sessions, so the ladder must be walked.
        a = await assemble(screen_config(drive, tmp_path, pixel_only=[]), model=model, approvals=approvals)
        try:
            res = await a.harness.run(
                "Update Halleck Logistics' annual value in the remote billing session from the signed contract."
            )
        finally:
            await a.aclose()
    finally:
        app.terminate()

    assert res.status == "finished", (res, tool_results(a))
    assert json.loads(saved.read_text()) == {"account": "Halleck Logistics", "annual_value": "193725"}
    assert res.answer == "The billing session says: Saved 193725."
    assert moved

    calls = [e.data for e in a.log.of_type(L.TOOL_CALL)]
    names = [c["name"] for c in calls]
    # The ladder: the accessibility tree was read first, and it had nothing to act on.
    assert names == ["read_file", "desktop_inspect", "screen_act", "screen_act"]
    read = next(e.data for e in a.log.of_type(L.WINDOW_ACCESS) if e.data["op"] == "read")
    assert read["actionable"] == 0
    assert "exposes nothing to act on" in tool_results(a)[1]["content"]
    # Targets were named in words; the grounder found them. No coordinates from the model.
    acts = [c["arguments"] for c in calls if c["name"] == "screen_act"]
    assert not any(k in json.dumps(acts) for k in ('"x"', '"y"'))
    grounded = [e.data for e in a.log.of_type(L.WINDOW_ACCESS) if e.data["op"] == "acted"]
    assert any("field beside" in (g.get("how") or "") for g in grounded)
    # The typed value was verified by reading the field back.
    assert "the field now reads '193725'" in tool_results(a)[2]["content"]

    # The submit went through the gate, with the exact value typed into the session.
    [req] = approvals.seen
    assert req.effects == ["submits_to_system_of_record"]
    assert [(v.label, v.after) for v in req.values] == [("Annual value field", "193725")]
    # Screenshots were kept beside the log, by hash, and never inlined into it.
    shots = [i for r in tool_results(a) for i in (r["detail"].get("images") or [])]
    assert shots and all(re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) for s in shots)
    assert "base64" not in a.log.path.read_text() and a.log.verify_chain()
    blobs = a.log.path.with_name(a.log.path.stem + ".blobs")
    assert all((blobs / s["sha256"]).exists() for s in shots)
    # A text-only model saw OCR text, never an image.
    last_request = model.adapter.requests[-1][0]
    assert not any(isinstance(p, ImagePart) for m in last_request for p in m.parts)


def _last_image(messages):
    from PIL import Image

    for m in reversed(messages):
        for p in reversed(m.parts):
            if isinstance(p, ImagePart):
                return Image.open(io.BytesIO(base64.b64decode(p.data_b64)))
    return None


def _find(img, target):
    """Stand-in for a vision model's eyes: find a target in the image it was sent."""
    from ondo_agent.screen.grounding import OcrGrounder

    hit = OcrGrounder()._locate(img, target)
    return round(hit.x), round(hit.y)


@needs_screen
async def test_vision_model_coordinates_are_scaled_back(desktop, drive, tmp_path):
    """A model that sees gives pixels in the downscaled screenshot it was shown;
    they land on the right control on a 2x display, after the window has moved."""
    saved = tmp_path / "saved.json"
    app = desktop.launch_remote(out=saved, scale=2)
    sizes = []
    try:

        def policy(messages, tools):
            t = sum(1 for m in messages if m.role == "assistant")
            if t == 0:  # a known remote session: no need to walk the tree first
                return call(("screen_view", {"window": REMOTE}))
            img = _last_image(messages)
            sizes.append(img.size)
            if t == 1:
                x, y = _find(img, "Annual value field")
                desktop.move(REMOTE, 0, 70)  # after the screenshot, before the click
                return call(
                    (
                        "screen_act",
                        {
                            "window": REMOTE,
                            "actions": [
                                {
                                    "action": "type",
                                    "x": x,
                                    "y": y,
                                    "target": "Annual value field",
                                    "text": "193725",
                                    "replace": True,
                                }
                            ],
                        },
                    )
                )
            if t == 2:
                x, y = _find(img, "Submit button")
                return call(
                    (
                        "screen_act",
                        {
                            "window": REMOTE,
                            "actions": [{"action": "click", "x": x, "y": y, "shot": "s2", "target": "Submit button"}],
                        },
                    )
                )
            return say("done")

        model = scripted_client(policy, name="sees", supports_vision=True, max_image_long_edge_px=800)
        a = await assemble(screen_config(drive, tmp_path), model=model, approvals=AutoApprovals(True))
        try:
            res = await a.harness.run("Set Halleck's annual value to 193725 in the remote billing session.")
        finally:
            await a.aclose()
    finally:
        app.terminate()

    assert res.status == "finished", tool_results(a)
    assert not any("FAILED" in r["content"] for r in tool_results(a)), tool_results(a)
    assert json.loads(saved.read_text())["annual_value"] == "193725"
    # The 1280x720 capture was sent at 800x450, and the model's pixels were mapped back.
    assert sizes[0] == (800, 450)
    acts = [e.data["arguments"] for e in a.log.of_type(L.TOOL_CALL) if e.data["name"] == "screen_act"]
    assert all("x" in x["actions"][0] for x in acts)
    # The rolling buffer: never more than three screenshots in one request, text before image.
    for msgs, _ in model.adapter.requests:
        assert sum(isinstance(p, ImagePart) for m in msgs for p in m.parts) <= 3
    last = model.adapter.requests[-1][0]
    tool_msg = next(m for m in reversed(last) if any(isinstance(p, ImagePart) for p in m.parts))
    assert tool_msg.role == "tool" and type(tool_msg.parts[0]).__name__ == "TextPart"


@needs_screen
async def test_pixels_only_after_the_ladder_and_never_escape(desktop, drive, tmp_path):
    saved = tmp_path / "saved.json"
    apps = [
        desktop.launch_remote(out=saved),
        desktop.launch("Legacy billing"),
        desktop.launch("Password manager"),
    ]
    try:
        script = iter(
            [
                call(("screen_act", {"window": REMOTE, "actions": [{"action": "key", "keys": "Tab"}]})),
                call(("desktop_inspect", {"window": "Legacy billing"})),
                call(("screen_act", {"window": "Legacy billing", "actions": [{"action": "key", "keys": "Tab"}]})),
                call(("screen_view", {"window": "Password manager"})),
                call(("desktop_inspect", {"window": REMOTE})),
                call(
                    (
                        "screen_act",
                        {
                            "window": REMOTE,
                            "actions": [
                                {"action": "key", "keys": "Escape"},
                                {"action": "click", "target": "the Submit button"},
                            ],
                        },
                    )
                ),
                call(
                    ("screen_act", {"window": REMOTE, "actions": [{"action": "click", "target": "the Submit button"}]})
                ),
                call(("screen_act", {"window": REMOTE, "actions": [{"action": "key", "keys": "ctrl+s"}]})),
                call(("screen_act", {"window": REMOTE, "actions": [{"action": "click", "x": 5, "y": 5}]})),
                call(("screen_act", {"window": REMOTE, "actions": [{"action": "scroll", "direction": "down"}]})),
                say("done"),
            ]
        )
        approvals = AutoApprovals(False, by="mara.okonjo")
        a = await assemble(
            screen_config(drive, tmp_path, pixel_only=[]),
            model=scripted_client(lambda m, t: next(script)),
            approvals=approvals,
        )
        try:
            await a.harness.run("poke at the windows")
        finally:
            await a.aclose()
    finally:
        for p in apps:
            p.terminate()

    r = [x["content"] for x in tool_results(a)]
    assert "Pixels are the last resort" in r[0]  # before the tree was read
    assert "Pixels are the last resort" in r[2]  # a window with real controls
    assert [d.data["reason"] for d in a.log.of_type(L.PERMISSION_DENIED)] == ["excluded_by_policy"]
    assert "Escape is reserved for the user" in r[5] and "2. click “the Submit button”: not executed" in r[5]
    assert "not approved by mara.okonjo" in r[6]  # the submit click, refused
    assert "not approved" in r[7]  # Ctrl+S may save: always asked
    assert "Say what you are clicking" in r[8]
    assert "1. scroll down 3: done at screen" in r[9] and "the middle of the window" in r[9]
    assert len(approvals.seen) == 2
    assert not saved.exists()


class _FakeUiTars(BaseHTTPRequestHandler):
    """A local OpenAI-compatible endpoint answering like UI-TARS, in its 0-1000
    relative coordinates. It stands in for a self-hosted grounding model; its eyes
    are OCR, which is all this test needs."""

    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        from PIL import Image

        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).seen.append({"model": body["model"], "path": self.path})
        content = body["messages"][0]["content"]
        target = content[0]["text"].split("Element:")[-1].strip()
        img = Image.open(io.BytesIO(base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1])))
        try:
            x, y = _find(img, target)
            answer = f"click(start_box='({round(x * 1000 / img.width)},{round(y * 1000 / img.height)})')"
        except Exception:
            answer = "I cannot find it."
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": answer}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@needs_screen
async def test_grounding_switches_to_a_local_model_by_configuration(desktop, drive, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUiTars)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeUiTars.seen = []
    saved = tmp_path / "saved.json"
    app = desktop.launch_remote(out=saved)
    try:
        grounding = {
            "provider": "vision",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model": "ui-tars-7b",
            "coords": "relative_1000",
        }
        a = await assemble(
            screen_config(drive, tmp_path, grounding=grounding),
            model=scripted_client(remote_app_policy(drive), supports_vision=False),
            approvals=AutoApprovals(True),
        )
        try:
            res = await a.harness.run("Update Halleck's annual value in the remote billing session.")
        finally:
            await a.aclose()
    finally:
        app.terminate()
        server.shutdown()

    assert res.status == "finished", tool_results(a)
    assert json.loads(saved.read_text())["annual_value"] == "193725"
    # Both targets were found by the local grounding model, over plain HTTP.
    assert [s["model"] for s in _FakeUiTars.seen] == ["ui-tars-7b", "ui-tars-7b"]
    assert all(s["path"] == "/v1/chat/completions" for s in _FakeUiTars.seen)
    hows = [e.data.get("how", "") for e in a.log.of_type(L.WINDOW_ACCESS) if e.data["op"] == "acted"]
    assert hows and all(h.startswith("vision: ui-tars-7b") for h in hows)


# -- no display needed ---------------------------------------------------------------------


def test_frames_map_both_ways():
    from ondo_agent.screen.geometry import Frame, Rect, fit

    assert fit(2560, 1600, 1280) == (1280, 800)
    assert fit(640, 360, 1280) == (640, 360)  # never upscaled
    assert fit(4000, 3000, 4000, max_pixels=1_000_000) == (1155, 866)
    # A Retina Mac: an 800x600-point window captured at 1600x1200, sent at 1280x960.
    f = Frame.for_capture("s1", "App", (800, 600), (1600, 1200), (1280, 960))
    assert f.to_screen(Rect(100, 50, 800, 600), 640, 480) == (500, 350)
    # The window moved: the same image pixel follows it.
    assert f.to_screen(Rect(300, 250, 800, 600), 640, 480) == (700, 550)
    assert f.same_size(Rect(0, 0, 801, 600)) and not f.same_size(Rect(0, 0, 900, 600))
    # Zoom into the top-left quarter, shown at 1280x960: 4x the detail.
    z = f.zoom("s2", 0, 0, 640, 480, (1280, 960))
    assert z.to_screen(Rect(100, 50, 800, 600), 1280, 960) == (500, 350)
    assert z.to_screen(Rect(100, 50, 800, 600), 0, 0) == (100, 50)


def test_grounding_model_formats():
    from ondo_agent.screen.grounding import parse_point, phrase_of

    assert parse_point("click(start_box='(512,300)')") == (512, 300)
    assert parse_point("<|box_start|>(100,200),(300,400)<|box_end|>") == (200, 300)
    assert parse_point("[0.1, 0.2, 0.3, 0.4]") == (0.2, 0.30000000000000004)
    assert parse_point("<point>40 60</point>") == (40, 60)
    assert parse_point("I cannot see it") is None
    assert phrase_of("the Submit button") == ["submit"]
    assert phrase_of('the "Save record" link') == ["save", "record"]


def test_escape_is_never_a_key_ondo_sends():
    from ondo_agent.screen.backend import parse_combo

    assert parse_combo("ctrl+a") == ["ctrl", "a"]
    assert parse_combo("Return") == ["enter"]
    for bad in ("Escape", "esc", "shift+Esc"):
        with pytest.raises(ValueError, match="reserved"):
            parse_combo(bad)
    with pytest.raises(ValueError):
        parse_combo("hyper+q")


def test_rolling_screenshot_buffer(tmp_path):
    from ondo_agent.blobs import BlobStore
    from ondo_agent.harness import context as ctx
    from ondo_agent.log import EventLog

    p = ctx.ContextPolicy(vision=True, keep_images=3, image_batch=3)
    # The cut moves in batches, so the cached prefix does not change on every shot.
    assert [ctx.kept_images(n, p) for n in range(1, 11)] == [1, 2, 3, 4, 5, 3, 4, 5, 3, 4]
    assert ctx.kept_images(50, ctx.ContextPolicy(vision=True, keep_images=30, max_images=20)) == 20

    log = EventLog.create(tmp_path / "runs")
    blobs = BlobStore.beside(log.path)
    log.append(L.USER_MESSAGE, "user", {"text": "look"})
    for i in range(6):
        sha = blobs.put(f"png-{i}".encode())
        log.append(
            L.MODEL_RESPONSE,
            "model",
            {"text": "", "tool_calls": [{"id": f"c{i}", "name": "screen_view", "arguments": {}}]},
        )
        img = {"sha256": sha, "media_type": "image/png", "width": 10, "height": 10, "label": f"s{i + 1} · App"}
        log.append(
            L.TOOL_RESULT,
            "tool",
            {"call_id": f"c{i}", "name": "screen_view", "content": "ok", "detail": {"images": [img]}},
        )
    msgs, _ = ctx.build(log.events, p, blobs=blobs)
    tools = [m for m in msgs if m.role == "tool"]
    assert [sum(isinstance(x, ImagePart) for x in m.parts) for m in tools] == [0, 0, 0, 1, 1, 1]
    assert "s1 · App: pruned from context" in tools[0].text and "attached" in tools[5].text
    # Without vision, no image is ever attached.
    msgs, _ = ctx.build(log.events, ctx.ContextPolicy(vision=False), blobs=blobs)
    assert not any(isinstance(x, ImagePart) for m in msgs for x in m.parts)
    assert "this model cannot see images" in msgs[-1].text
    # A blob that no longer matches its hash is not sent.
    (blobs.root / log.events[-1].data["detail"]["images"][0]["sha256"]).write_bytes(b"tampered")
    msgs, _ = ctx.build(log.events, p, blobs=blobs)
    assert sum(isinstance(x, ImagePart) for x in msgs[-1].parts) == 0
    # A fork carries its screenshots with it.
    child = log.fork(tmp_path / "runs", 4)
    assert (BlobStore.beside(child.path).root / log.events[2].data["detail"]["images"][0]["sha256"]).exists()


def test_tool_screenshots_in_both_wire_formats():
    from ondo_agent.models.adapters import messages as msgs_api
    from ondo_agent.models.adapters import openai_chat
    from ondo_agent.models.profile import ModelProfile
    from ondo_agent.models.types import Message, TextPart, ToolCall

    conv = [
        Message.system("sys"),
        Message.user("look"),
        Message("assistant", [], [ToolCall("c1", "screen_view", {"window": "App"})]),
        Message(
            "tool",
            [TextPart("Screenshot s1"), ImagePart("image/png", "AAAA", 10, 10)],
            tool_call_id="c1",
            tool_name="screen_view",
        ),
        Message("assistant", [TextPart("Done.")]),
    ]
    see = ModelProfile("v", "openai_chat", "m", supports_vision=True)
    body = openai_chat.build_request(see, conv, [])
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user", "assistant"]
    assert body["messages"][3]["content"] == "Screenshot s1"
    follow = body["messages"][4]["content"]
    assert follow[0]["type"] == "text" and follow[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    # A text-only profile never gets an image.
    blind = openai_chat.build_request(ModelProfile("t", "openai_chat", "m"), conv, [])
    assert "image_url" not in json.dumps(blind)

    body = msgs_api.build_request(ModelProfile("v", "messages", "m", supports_vision=True), conv, [])
    result = body["messages"][2]["content"][0]
    assert result["type"] == "tool_result"
    assert [b["type"] for b in result["content"]] == ["text", "image"]
