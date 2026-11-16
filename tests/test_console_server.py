"""Unit and integration tests for standalone local `mc-console` server and `stop_check`."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from aiohttp.test_utils import TestClient, TestServer

from mc_agent.bot import BotSession
from mc_agent.config import Settings
from mc_agent.console.server import ConsoleConfig, create_console_app
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.control import check_control


def _make_mock_bot(stop_flag: threading.Event | None = None) -> BotSession:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "username": "ConsoleBot",
                        "connected": True,
                        "ready": True,
                        "pos": [-167.6, 71.0, -376.5],
                        "dimension": "overworld",
                        "biome": "birch_forest",
                        "health": 20,
                        "food": 19,
                        "held": {"name": "stone_pickaxe", "count": 1},
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "items": [
                            {"name": "stone_pickaxe", "count": 1, "slot": 36},
                            {"name": "birch_log", "count": 8, "slot": 0},
                        ],
                        "held": {"name": "stone_pickaxe", "count": 1},
                        "empty_slots": 34,
                    },
                },
            )
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"total": 2, "blocks": [{"name": "coal_ore", "pos": [-165, 70, -375], "dist": 3.1}]},
                },
            )
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/stop":
            if stop_flag is not None:
                stop_flag.set()
            return httpx.Response(200, json={"ok": True, "result": {"stopped": True}})
        raise AssertionError(f"Unexpected mock MC API path: {path}")

    return BotSession(
        "super-secret-bot-token-do-not-leak",
        bot_username="ConsoleBot",
        base_url="http://mock-mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )


def test_run_mc_agent_stop_check_interrupts_cleanly() -> None:
    bot = _make_mock_bot()
    stop_requested = False

    def fake_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        nonlocal stop_requested
        stop_requested = True
        if control is not None:
            control()
        return {"role": "assistant", "content": "should not deliver"}

    seen_events: list[dict[str, Any]] = []
    report = run_mc_agent(
        "执行测试",
        bot,
        chat_fn=fake_chat,
        max_calls=6,
        stream=False,
        on_event=seen_events.append,
        stop_check=lambda: stop_requested,
    )
    assert report["status"] == "interrupted"
    assert any(e["type"] == "run.finished" and e["status"] == "interrupted" for e in seen_events)


def test_console_static_state_and_origin_guard(tmp_path: Path) -> None:
    settings = Settings(
        mc_api_base_url="http://mock-mc.local/api/v1",
        skin_email="secret-email@example.com",
        skin_password="secret-password",
        nju_api_key="secret-nju-key",
        default_bot_username="ConsoleBot",
        state_dir=tmp_path / ".state",
    )

    class PollableMockBot(BotSession):
        async def poll_async_telemetry(self) -> tuple[dict[str, Any], dict[str, Any]]:
            return self.status(), self.inventory()

    bot = PollableMockBot(
        "super-secret-bot-token-do-not-leak",
        bot_username="ConsoleBot",
        base_url="mock://local",
        transport=_make_mock_bot()._client._transport,
    )
    cfg = ConsoleConfig(
        bot_username="ConsoleBot",
        runs_dir=tmp_path / "runs",
        telemetry_interval_s=0.0,
        connect_on_startup=True,
    )

    async def _run() -> None:
        app = create_console_app(
            cfg,
            settings=settings,
            session_factory=lambda _b: bot,
            chat_factory=lambda _p, _t, _m: ("mock-model", lambda *a, **kw: {"role": "assistant", "content": "ok"}),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await asyncio.sleep(0.05)

            idx = await client.get("/")
            assert idx.status == 200
            assert "MC-CODEX" in await idx.text()

            js = await client.get("/static/app.js")
            assert js.status == 200

            st_resp = await client.get("/api/state")
            assert st_resp.status == 200
            raw_text = await st_resp.text()
            for secret in ("super-secret-bot-token-do-not-leak", "secret-password", "secret-nju-key"):
                assert secret not in raw_text

            snap = json.loads(raw_text)
            assert snap["bot_username"] == "ConsoleBot"
            assert snap["telemetry"]["pos"] == {"x": -167.6, "y": 71.0, "z": -376.5}
            assert snap["telemetry"]["inventory_total"] == 9

            bad_post = await client.post(
                "/api/command",
                headers={"Origin": "https://evil.example"},
                json={"text": "hi"},
            )
            assert bad_post.status == 403

            bad_stop = await client.post("/api/stop", headers={"Origin": "https://evil.example"})
            assert bad_stop.status == 403
        finally:
            await client.close()

    asyncio.run(_run())


def test_console_run_ws_stream_jsonl_and_multi_turn(tmp_path: Path) -> None:
    settings = Settings(default_bot_username="ConsoleBot", state_dir=tmp_path / ".state")
    bot = _make_mock_bot()
    recorded_prompts: list[str] = []

    def fake_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        user_msg = messages[1]["content"]
        recorded_prompts.append(user_msg)
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if not has_tool_result:
            if on_event is not None:
                on_event({"type": "text.delta", "text": "先扫描周围方块。"})
            return {
                "role": "assistant",
                "content": "先扫描周围方块。",
                "tool_calls": [
                    {
                        "id": "call_find_1",
                        "type": "function",
                        "function": {"name": "mc_find_blocks", "arguments": '{"types":["coal_ore"],"radius":16}'},
                    }
                ],
            }
        final_text = f"已完成扫描，第 {len(recorded_prompts)} 次交付。"
        if on_event is not None:
            on_event({"type": "text.delta", "text": final_text})
        return {"role": "assistant", "content": final_text}

    runs_dir = tmp_path / "runs"
    cfg = ConsoleConfig(
        bot_username="ConsoleBot",
        runs_dir=runs_dir,
        telemetry_interval_s=0.0,
        connect_on_startup=False,
    )

    async def _run() -> None:
        app = create_console_app(
            cfg,
            settings=settings,
            session_factory=lambda _b: bot,
            chat_factory=lambda provider, thinking, _max_tok: (f"test-{provider}-t{int(thinking)}", fake_chat),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            async with client.session.ws_connect(client.make_url("/ws")) as ws:
                snap_msg = json.loads(await ws.receive_str())
                assert snap_msg["type"] == "snapshot"
                assert snap_msg["running"] is False

                resp = await client.post(
                    "/api/command",
                    json={"text": "找一下附近煤矿", "provider": "deepseek", "max_calls": 6, "enable_thinking": True},
                )
                assert resp.status == 200
                started_res = await resp.json()
                assert started_res["mode"] == "started"
                turn_id = started_res["turn_id"]

                seen_types: list[str] = []
                finished_msg: dict[str, Any] | None = None
                for _ in range(40):
                    frame = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3.0))
                    seen_types.append(frame["type"])
                    if frame["type"] == "turn_finished":
                        finished_msg = frame["turn"]
                        break

                assert finished_msg is not None
                assert finished_msg["turn_id"] == turn_id
                assert finished_msg["status"] == "answered"
                assert finished_msg["provider"] == "deepseek"
                assert finished_msg["model"] == "test-deepseek-t1"
                assert "turn_started" in seen_types
                assert "turn_item_upsert" in seen_types

                tool_items = [it for it in finished_msg["items"] if it["kind"] == "tool_call"]
                assert len(tool_items) == 1
                assert tool_items[0]["name"] == "mc_find_blocks"
                assert tool_items[0]["status"] == "ok"

                # Verify standard runs/<stem>.events.jsonl + .json files created for mc-live tailing
                jsonl_file = runs_dir / f"{turn_id}.events.jsonl"
                json_file = runs_dir / f"{turn_id}.json"
                assert jsonl_file.is_file()
                assert json_file.is_file()

                # Send a second follow-up command and confirm prior turn summary context was carried forward
                resp2 = await client.post("/api/command", json={"text": "继续汇报坐标"})
                assert resp2.status == 200
                for _ in range(40):
                    frame2 = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3.0))
                    if frame2["type"] == "turn_finished":
                        break

                assert any("本会话前序指令记录" in p and "找一下附近煤矿" in p for p in recorded_prompts)

                clr = await client.post("/api/clear", json={})
                assert clr.status == 200
                cleared_frame = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=2.0))
                assert cleared_frame["type"] == "session_cleared"
        finally:
            await client.close()

    asyncio.run(_run())


def test_console_mid_run_steer_and_stop(tmp_path: Path) -> None:
    settings = Settings(default_bot_username="ConsoleBot", state_dir=tmp_path / ".state")
    mc_stop_called = threading.Event()

    class MockWithStop(BotSession):
        async def stop_async(self) -> None:
            mc_stop_called.set()

    bot = MockWithStop(
        "tok",
        bot_username="ConsoleBot",
        base_url="mock://local",
        transport=_make_mock_bot(mc_stop_called)._client._transport,
    )

    chat_entered = threading.Event()
    saw_steered_user_msg = threading.Event()
    allow_finish_or_wait_stop = threading.Event()

    def blocking_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        chat_entered.set()
        for m in messages:
            if m.get("role") == "user" and "紧急改去挖煤" in str(m.get("content")):
                saw_steered_user_msg.set()
                # Now wait for stop button to fire
                for _ in range(50):
                    if control is not None:
                        control()
                    time.sleep(0.02)
        # First call waits briefly for /now interrupt signal
        for _ in range(60):
            check_control(control)
            if allow_finish_or_wait_stop.is_set():
                break
            time.sleep(0.02)
        return {"role": "assistant", "content": "done"}

    cfg = ConsoleConfig(
        bot_username="ConsoleBot",
        runs_dir=tmp_path / "runs",
        telemetry_interval_s=0.0,
        connect_on_startup=False,
    )

    async def _run() -> None:
        app = create_console_app(
            cfg,
            settings=settings,
            session_factory=lambda _b: bot,
            chat_factory=lambda _p, _t, _m: ("mock-model", blocking_chat),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            async with client.session.ws_connect(client.make_url("/ws")) as ws:
                _ = json.loads(await ws.receive_str())  # snapshot

                start_resp = await client.post("/api/command", json={"text": "砍树收集木头"})
                assert start_resp.status == 200

                assert await asyncio.to_thread(chat_entered.wait, 2.0)

                # Normal queued steer, then urgent /now steer
                q_resp = await client.post("/api/command", json={"text": "顺便看看附近羊群"})
                assert q_resp.status == 200
                q_data = await q_resp.json()
                assert q_data["mode"] == "steered"
                assert q_data["immediate"] is False

                u_resp = await client.post("/api/command", json={"text": "/now 紧急改去挖煤"})
                assert u_resp.status == 200
                u_data = await u_resp.json()
                assert u_data["mode"] == "steered"
                assert u_data["immediate"] is True

                # Wait until the worker receives the steered input
                assert await asyncio.to_thread(saw_steered_user_msg.wait, 3.0)

                # Can't clear while running
                bad_clr = await client.post("/api/clear", json={})
                assert bad_clr.status == 409

                # Now press Stop!
                stop_resp = await client.post("/api/stop", json={})
                assert stop_resp.status == 200
                stop_data = await stop_resp.json()
                assert stop_data["stopping"] is True

                finished_turn: dict[str, Any] | None = None
                for _ in range(50):
                    frame = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3.0))
                    if frame["type"] == "turn_finished":
                        finished_turn = frame["turn"]
                        break

                assert finished_turn is not None
                assert finished_turn["status"] == "interrupted"
                assert mc_stop_called.is_set()
        finally:
            allow_finish_or_wait_stop.set()
            await client.close()

    asyncio.run(_run())


def test_console_per_run_timeout_and_loaded_revision(tmp_path: Path) -> None:
    """A short per-run budget cancels a live model, without changing service defaults."""
    def slow_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        while True:
            control()
            time.sleep(0.005)

    async def run() -> None:
        cfg = ConsoleConfig(
            runs_dir=tmp_path / 'runs', connect_on_startup=False,
            telemetry_interval_s=0, run_timeout_s=600,
        )
        app = create_console_app(
            cfg, settings=Settings(state_dir=tmp_path / '.state'),
            session_factory=lambda _: _make_mock_bot(),
            chat_factory=lambda *_: ('slow-model', slow_chat),
        )
        async with TestClient(TestServer(app)) as client:
            for invalid in (0, -1, 18001, 'nope'):
                response = await client.post('/api/command', json={'text': 'test', 'run_timeout_s': invalid})
                assert response.status == 400
                assert (await response.json())['error'] == 'invalid_run_timeout'
            # Long-horizon 4h timeout and max_calls up to 600 should not fail parameter validation.
            ok_long = await client.post('/api/command', json={'text': 'long_check', 'max_calls': 400, 'run_timeout_s': 14400})
            assert ok_long.status == 200
            await client.post('/api/stop', json={})
            for _ in range(100):
                state_long = await (await client.get('/api/state')).json()
                if not state_long['running']:
                    break
                await asyncio.sleep(0.01)
            invalid_guard = await client.post('/api/command', json={'text': 'test', 'survival_guard': 'false'})
            assert invalid_guard.status == 400
            assert (await invalid_guard.json())['error'] == 'invalid_survival_guard'
            response = await client.post('/api/command', json={'text': 'test', 'run_timeout_s': 0.1, 'survival_guard': True})
            data = await response.json()
            assert response.status == 200
            assert data['turn']['run_timeout_s'] == 0.1
            assert data['turn']['survival_guard'] is True
            for _ in range(100):
                state = await (await client.get('/api/state')).json()
                if not state['running']:
                    break
                await asyncio.sleep(0.01)
            assert state['running'] is False
            assert state['turns'][-1]['status'] == 'timeout'
            assert '0.1s' in state['turns'][-1]['error']
            assert state['config']['run_timeout_s'] == 600
            assert state['config']['survival_guard'] is False
            revision = state['runtime']['source_revision']
            assert len(revision) == 16
            report = json.loads((cfg.runs_dir / (data['turn_id'] + '.json')).read_text())
            assert report['runtime']['source_revision'] == revision
            assert report['run_timeout_s'] == 0.1
            assert report['survival_guard'] is True
            assert report['guardian']['polls'] >= 1
    asyncio.run(run())
