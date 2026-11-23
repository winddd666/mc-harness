"""Unit and integration tests for the Nano-Agent based Minecraft Qwen Harness."""

from __future__ import annotations

import io
import json
import pathlib
from typing import Any

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.model import QwenSpec, _stream_message, resolve_qwen_endpoint


def test_resolve_qwen_endpoint_nju_and_dashscope(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("QWEN_API_KEY", "NJU_API_KEY", "DASHSCOPE_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "NJU_MODEL"):
        monkeypatch.delenv(key, raising=False)

    env_nju = tmp_path / ".env.nju"
    env_nju.write_text("NJU_API_KEY=test_nju_secret\nNJU_RPM_BUDGET=24\n", encoding="utf-8")
    r1 = resolve_qwen_endpoint(QwenSpec(dotenv_path=str(env_nju), fallback_dotenv_path=None))
    assert r1.base_url == "https://api.nju-aia.com/v1"
    assert r1.endpoint == "https://api.nju-aia.com/v1/chat/completions"
    assert r1.model == "default"
    assert r1.rpm_budget == 24
    assert r1.extra_body(enable_thinking=False) == {"chat_template_kwargs": {"enable_thinking": False}}

    r1_norm = resolve_qwen_endpoint(
        QwenSpec(dotenv_path=str(env_nju), fallback_dotenv_path=None, model_override="qwen3.6-35b-a3b")
    )
    assert r1_norm.model == "default"

    env_dash = tmp_path / ".env.dash"
    env_dash.write_text("DASHSCOPE_API_KEY=test_dash_secret\n", encoding="utf-8")
    r2 = resolve_qwen_endpoint(QwenSpec(dotenv_path=str(env_dash), fallback_dotenv_path=None))
    assert "dashscope.aliyuncs.com" in r2.base_url
    assert r2.model == "qwen-plus"
    assert r2.extra_body(enable_thinking=False) == {"enable_thinking": False}


def test_stream_message_qwen_tool_calls_and_trailing_empty_choices_usage() -> None:
    sse_lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"mc_find_blocks","arguments":"{\\"rad"}}]},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ius\\": 12}"}}]},"finish_reason":"tool_calls"}]}',
        "",
        'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":18,"total_tokens":138}}',
        "",
        "data: [DONE]",
        "",
    ]
    raw_bytes = ("\r\n".join(sse_lines)).encode("utf-8")
    events: list[dict[str, Any]] = []
    msg = _stream_message(io.BytesIO(raw_bytes), events.append)
    assert msg["role"] == "assistant"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["tool_calls"][0]["function"]["name"] == "mc_find_blocks"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"radius": 12}
    assert any(e["type"] == "usage" and e["usage"]["total_tokens"] == 138 for e in events)


def test_run_mc_agent_loop_water_safety_and_error_recovery() -> None:
    recorded_Bodies: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else None
        recorded_Bodies.append((path, body))
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "username": "TestBot_01",
                        "pos": [-135.5, 62.0, 72.3],
                        "health": 18,
                        "food": 20,
                        "in_water": True,
                        "ready": True,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "oak_log", "count": 1, "slot": 0}], "empty_slots": 35}},
            )
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"total": 1, "blocks": [{"name": "oak_log", "pos": [-134, 69, 72], "dist": 2.1}]}},
            )
        if path == "/craft":
            if body.get("item") == "wooden_pickaxe":
                return httpx.Response(
                    200,
                    json={
                        "ok": False,
                        "error": {"code": "need_crafting_table", "message": "3x3 recipe requires nearby crafting_table"},
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"crafted": body["item"], "gained": 4}})
        if path == "/move_to":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [body["x"], 69.0, body["z"]]}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="TestBot_01", transport=httpx.MockTransport(handler))
    turn = 0

    def fake_qwen_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        nonlocal turn
        turn += 1
        if turn == 1:
            # Verify initial snapshot was injected with in_water=True, then test parallel shared + exclusive move_to + failed craft
            assert "[开局实时状态快照]" in messages[1]["content"]
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "mc_find_blocks", "arguments": '{"types":["oak_log"],"radius":16}'}},
                    {"id": "c2", "type": "function", "function": {"name": "mc_move_to", "arguments": '{"x":-134.5,"z":72.0}'}},
                    {"id": "c3", "type": "function", "function": {"name": "mc_craft", "arguments": '{"item":"wooden_pickaxe"}'}},
                ],
            }
        if turn == 2:
            # Check error recovery payload from c3 is visible to model
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            assert any("need_crafting_table" in m["content"] for m in tool_msgs)
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c4", "type": "function", "function": {"name": "mc_craft", "arguments": '{"item":"oak_planks","count":1}'}},
                ],
            }
        return {"role": "assistant", "content": "已上岸并将橡木原木合成为 4 块橡木木板。"}

    report = run_mc_agent(
        "上岸并合成木板",
        bot,
        chat_fn=fake_qwen_chat,
        max_calls=6,
        stream=False,
        on_event=lambda _: None,
    )
    assert report["status"] == "answered"
    assert "4 块橡木木板" in report["answer"]
    assert report["actor_requests"] == 3

    # Verify water safety automatically injected stop_on=["death"] into move_to!
    move_calls = [b for p, b in recorded_Bodies if p == "/move_to"]
    assert len(move_calls) == 1
    assert move_calls[0]["stop_on"] == ["death"]


def test_action_chain_circuit_breaker_skips_subsequent_actions_on_failure() -> None:
    executed_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        executed_paths.append(path)
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"username": "TestBot_01", "pos": [0, 64, 0], "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [], "empty_slots": 36}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"total": 0, "blocks": []}})
        if path == "/craft":
            return httpx.Response(200, json={"ok": False, "error": {"code": "missing_items", "message": "no planks"}})
        if path == "/place":
            return httpx.Response(200, json={"ok": True, "result": {"placed": "crafting_table"}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="TestBot_01", transport=httpx.MockTransport(handler))
    turn = 0

    def fake_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        nonlocal turn
        turn += 1
        if turn == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a1", "type": "function", "function": {"name": "mc_craft", "arguments": '{"item":"crafting_table"}'}},
                    {"id": "a2", "type": "function", "function": {"name": "mc_place", "arguments": '{"item":"crafting_table"}'}},
                    {"id": "s1", "type": "function", "function": {"name": "mc_inventory", "arguments": "{}"}},
                ],
            }
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert any("previous_action_failed" in m["content"] for m in tool_msgs)
        return {"role": "assistant", "content": "材料不足，已停止后续放置。"}

    report = run_mc_agent("测试连招熔断", bot, chat_fn=fake_chat, max_calls=4, stream=False, on_event=lambda _: None)
    assert report["status"] == "answered"
    assert "/place" not in executed_paths
    assert executed_paths.count("/inventory") == 2  # initial snapshot + trailing shared observation s1


def test_v1_3_7_tools_and_campus_default_url(tmp_path) -> None:
    import json

    from mc_agent.config import (
        DEFAULT_MC_API_BASE_URL,
        PUBLIC_MC_API_BASE_URL,
        Settings,
        should_trust_env,
    )

    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")
    settings = Settings.load(dotenv_path=empty_env, environ={})
    assert DEFAULT_MC_API_BASE_URL == "https://apiin.nmo.net.cn/api/v1"
    assert PUBLIC_MC_API_BASE_URL == "https://mc-api.windywindie.top/api/v1"
    assert settings.mc_api_base_url == DEFAULT_MC_API_BASE_URL
    assert should_trust_env(settings.mc_api_base_url) is False

    recorded: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.method == "POST":
            recorded.append((path, body))
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"username": "TestBot_01", "pos": [0, 64, 0], "speed": 0, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [], "empty_slots": 36}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"total": 0, "blocks": []}})
        if path == "/posture":
            return httpx.Response(200, json={"ok": True, "result": {"sneaking": False, "jumping": True, "blocking": False}})
        if path == "/look":
            return httpx.Response(200, json={"ok": True, "result": {"yaw": 1.2, "pitch": 0.0}})
        if path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"moved": 0.8, "hits": [{"t_ms": 0}, {"t_ms": 500}]}})
        if path == "/attack":
            return httpx.Response(200, json={"ok": True, "result": {"killed": False, "hits": 1}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="TestBot_01", transport=httpx.MockTransport(handler))
    turn = 0

    def fake_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        nonlocal turn
        turn += 1
        if turn == 1:
            by_name = {t["function"]["name"]: t["function"] for t in tools or []}
            assert "mc_control" in by_name
            assert "mc_look" in by_name
            assert "speed" in by_name["mc_status"]["description"]
            assert "jump" in by_name["mc_posture"]["parameters"]["properties"]
            assert "max_hits" in by_name["mc_attack"]["parameters"]["properties"]
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "p1", "type": "function", "function": {"name": "mc_posture", "arguments": '{"jump": true}'}},
                    {"id": "l1", "type": "function", "function": {"name": "mc_look", "arguments": '{"entity_id": 812}'}},
                    {"id": "c1", "type": "function", "function": {"name": "mc_control", "arguments": '{"forward": true, "jump": true, "hit": 2, "hit_interval_ms": 500, "ms": 700, "stop_on": []}'}},
                ],
            }
        if turn == 2:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a1", "type": "function", "function": {"name": "mc_attack", "arguments": '{"entity_id": 812, "max_hits": 1, "interval_ms": 300}'}},
                ],
            }
        return {"role": "assistant", "content": "v1.3.7 工具测试完成。"}

    report = run_mc_agent("测试v1.3.7工具", bot, chat_fn=fake_chat, max_calls=6, stream=False, on_event=lambda _: None)
    assert report["status"] == "answered"
    assert ("/posture", {"sneak": False, "shield": False, "jump": True}) in recorded
    assert ("/look", {"entity_id": 812}) in recorded
    assert ("/control", {"forward": True, "jump": True, "hit": 2, "hit_interval_ms": 500, "ms": 700, "stop_on": []}) in recorded
    assert ("/attack", {"entity_id": 812, "max_hits": 1, "interval_ms": 300, "timeout": 20000, "feedback": True}) in recorded


def test_toolset_auto_reconnects_on_not_connected() -> None:
    from mc_agent.harness.mc_tools import McToolset

    connected = False
    connect_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal connected, connect_calls
        path = request.url.path.removeprefix("/api/v1")
        if path == "/connect":
            connect_calls += 1
            connected = True
            return httpx.Response(200, json={"ok": True, "result": {"status": "connecting"}})
        if not connected:
            return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "bot not connected"}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"username": "TestBot_01", "pos": [1, 64, 1], "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "oak_log", "count": 4, "slot": 0}], "empty_slots": 35}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="TestBot_01", transport=httpx.MockTransport(handler))
    toolset = McToolset(bot)
    out = json.loads(toolset.mc_inventory())
    assert out["ok"] is True
    assert connect_calls == 1
    assert out["result"]["items"][0]["name"] == "oak_log"

    def cooldown_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/connect":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"code": "death_loop_cooldown", "message": "cooldown", "cooldown_remaining_seconds": 120}},
            )
        return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "bot not connected"}})

    cd_bot = BotSession("tok", bot_username="TestBot_01", transport=httpx.MockTransport(cooldown_handler))
    cd_toolset = McToolset(cd_bot)
    with pytest.raises(RuntimeError, match="MC API 会话不可继续"):
        cd_toolset.mc_inventory()
