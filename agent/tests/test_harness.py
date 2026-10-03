"""The loop, the event log, and the model layer with two wire formats.

Shown here: a question about a local spreadsheet is answered; the run replays from
the log, and re-run it against the second provider by changing config.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from conftest import base_config, spreadsheet_question_policy

from ondo_agent import log as L
from ondo_agent.log import EventLog, verify_chain
from ondo_agent.models.adapters.messages import MessagesAdapter
from ondo_agent.models.adapters.openai_chat import OpenAIChatAdapter
from ondo_agent.models.adapters.scripted import ReplayModel, say
from ondo_agent.models.gateway import ModelClient, scripted_client
from ondo_agent.models.profile import ModelProfile
from ondo_agent.runtime import assemble
from ondo_agent.tools.files import file_tools
from ondo_agent.tools.spec import to_markdown, to_messages_tools, to_openai_tools


def _answer_from(tool_text: str) -> str:
    m = re.search(r"A14: ([^|]+) \|.*?E14: ([0-9.]+)", tool_text)
    assert m, tool_text[:400]
    return f"Row 14 is {m.group(1).strip()}. The workbook has a {m.group(2)}% uplift."


def fake_openai_provider(drive: Path, seen: list[dict]):
    """A stand-in for any OpenAI-compatible endpoint (LiteLLM, OpenRouter, vLLM)."""

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        assert req.url.path.endswith("/chat/completions")
        assert body["tools"][0]["type"] == "function"
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": str(drive / "Q3_Renewals.xlsx")}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": _answer_from(tool_msgs[-1]["content"])}
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "model": "gw-orchestrator",
                "choices": [{"message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 40},
            },
        )

    return handler


def fake_messages_provider(drive: Path, seen: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        assert req.url.path.endswith("/v1/messages")
        assert "input_schema" in body["tools"][0]
        assert body["system"][0]["text"].startswith("You are Ondo Quartermaster")
        results = [
            b for m in body["messages"] if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"
        ]
        if not results:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": str(drive / "Q3_Renewals.xlsx")},
                }
            ]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": _answer_from(results[-1]["content"])}]
            stop = "end_turn"
        return httpx.Response(
            200,
            json={
                "model": "msg-orchestrator",
                "content": content,
                "stop_reason": stop,
                "usage": {"input_tokens": 1100, "output_tokens": 35},
            },
        )

    return handler


def _client(profile: ModelProfile, handler) -> ModelClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = (OpenAIChatAdapter if profile.adapter == "openai_chat" else MessagesAdapter)(profile, "k", client=http)
    return ModelClient(profile, adapter)


PROFILES = {
    "gateway": {"adapter": "openai_chat", "model": "orchestrator", "base_url": "http://litellm.local:4000"},
    "messages": {
        "adapter": "messages",
        "model": "orchestrator-b",
        "base_url": "http://messages.local",
        "supports_prefix_cache": "explicit",
    },
}


async def test_ask_about_spreadsheet_replay_and_swap_provider(drive, tmp_path, approve_all):
    answers = {}
    for which, fake in (("gateway", fake_openai_provider), ("messages", fake_messages_provider)):
        # Changing providers is a config change: same request, same tools, same loop.
        cfg = base_config(drive, tmp_path, models={"orchestrator": which, "profiles": PROFILES})
        seen: list[dict] = []
        model = _client(cfg.profiles[which], fake(drive, seen))
        a = await assemble(cfg, model=model, approvals=approve_all)
        res = await a.harness.run("Why did row 14 not match the contract?")
        assert res.status == "finished", res
        answers[which] = res.answer
        assert "Halleck Logistics" in res.answer and "3.5%" in res.answer
        assert len(seen) == 2  # one tool turn, one answer turn

        # The log is the source of truth and is tamper-evident.
        log = EventLog.open(a.log.path)
        assert verify_chain(log.events)
        types = [e.type for e in log]
        assert types[:4] == [L.RUN_STARTED, L.SYSTEM_PROMPT, L.CONTEXT_INJECTION, L.USER_MESSAGE]
        assert types[-1] == L.RUN_FINISHED
        resp = log.of_type(L.MODEL_RESPONSE)
        assert resp[0].data["profile"] == which

        # Replay the run from the log alone: no provider involved.
        replay_log = EventLog.create(tmp_path / "replays")
        r = await assemble(
            cfg,
            model=ModelClient(ModelProfile("replay", "scripted", "replay"), ReplayModel(log.events)),
            approvals=approve_all,
            log=replay_log,
        )
        rr = await r.harness.run(log.events[0].data["request"])
        assert rr.answer == res.answer
        # The replay executed the same tool against the same file and saw the same data.
        orig_result = log.of_type(L.TOOL_RESULT)[0].data["content"]
        assert replay_log.of_type(L.TOOL_RESULT)[0].data["content"] == orig_result

    assert answers["gateway"] == answers["messages"]


async def test_fork_and_rerun_on_other_model(drive, tmp_path, approve_all):
    cfg = base_config(drive, tmp_path)
    a = await assemble(
        cfg, model=scripted_client(spreadsheet_question_policy(drive), name="model-a"), approvals=approve_all
    )
    await a.harness.run("Why did row 14 not match the contract?")
    # Fork just after the tool result and let a different model finish from the same history.
    cut = a.log.of_type(L.TOOL_RESULT)[0].seq
    fork = a.log.fork(cfg.runs_dir, cut)
    assert fork.events[0].data["forked_from"] == {"run_id": a.log.run_id, "seq": cut}
    b = await assemble(
        cfg, model=scripted_client(lambda m, t: say("model-b answer"), name="model-b"), approvals=approve_all, log=fork
    )
    res = await b.harness.run()
    assert res.answer == "model-b answer"
    assert fork.of_type(L.MODEL_RESPONSE)[-1].data["profile"] == "model-b"


def test_one_schema_many_wire_formats():
    tools = file_tools()
    oa, ms = to_openai_tools(tools), to_messages_tools(tools)
    assert [t["function"]["name"] for t in oa] == [t["name"] for t in ms]
    assert oa[1]["function"]["parameters"] == ms[1]["input_schema"]
    md = to_markdown(tools)
    assert "## `read_file`" in md and "Grant: `files`" in md


def test_trajectory_names_every_context_source(drive, tmp_path):
    import asyncio

    async def go():
        cfg = base_config(drive, tmp_path)
        a = await assemble(cfg, model=scripted_client(spreadsheet_question_policy(drive)), approvals=None)
        await a.harness.run("Why did row 14 not match the contract?")
        return a.log

    log = asyncio.run(go())
    sources = {e.type: e.source for e in log if e.enters_context}
    assert sources[L.SYSTEM_PROMPT] == "harness.prompt.base"
    assert sources[L.CONTEXT_INJECTION] == "harness.environment"
    assert sources[L.USER_MESSAGE] == "user:local-user"
    assert sources[L.TOOL_RESULT] == "tool:read_file"
    # And untrusted content is fenced with its origin.
    content = log.of_type(L.TOOL_RESULT)[0].data["content"]
    assert content.startswith('<untrusted_data origin="file:')
