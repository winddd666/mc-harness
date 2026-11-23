"""v2 Harness 单元测试：RULES.md 99项对账、电子围栏、WorldHUD、滑动账本、model抗拥塞、静默进食、Curriculum、树冠/隔墙物理修复。"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
import re
import threading
from typing import Any

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.errors import ApiError
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.context import RequestContext, _compact_tool_outcome, extract_failed_coords
from mc_agent.harness.control import RequestCancelled
from mc_agent.harness.events import EventSink
from mc_agent.harness.guardian import GenerationGuard
from mc_agent.harness.curriculum import (
    RULES_ADVANCEMENT_POINTS,
    UNCONDITIONAL_ADVANCEMENTS,
    CurriculumSupervisor,
    _is_transient_llm_error,
    select_next_target,
)
from mc_agent.harness.geofence import VoidGeofence
from mc_agent.harness.hud import build_world_hud
from mc_agent.harness.mc_tools import COMPACT_CORE_TOOLS, McToolset
from mc_agent.harness.model import (
    OutputTruncatedError,
    QwenModel,
    QwenSpec,
    _chat_direct,
    _process_request,
    _stream_message,
)
from mc_agent.kb.lookup import _load_full_advancement_kb, lookup_kb
from mc_agent.skills.library import MacroSkills, ray_wall_blocker


def test_rules_md_table_matches_curriculum_points_exactly() -> None:
    rules_path = Path(__file__).resolve().parents[1] / "RULES.md"
    assert rules_path.is_file()
    row_re = re.compile(r"^\|\s*`(minecraft:[^`]+)`\s*\|\s*[^|]+\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$")

    parsed_points: dict[str, int] = {}
    parsed_unconditional: set[str] = set()
    for line in rules_path.read_text(encoding="utf-8").splitlines():
        m = row_re.match(line.strip())
        if not m:
            continue
        adv_id, pts_raw, cond = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if pts_raw.isdigit():
            parsed_points[adv_id] = int(pts_raw)
            if cond == "计分":
                parsed_unconditional.add(adv_id)

    assert len(parsed_points) == 99
    assert parsed_points == RULES_ADVANCEMENT_POINTS
    assert len(parsed_unconditional) == 45
    assert parsed_unconditional == set(UNCONDITIONAL_ADVANCEMENTS)


def test_void_geofence_blocks_out_of_bounds_actions_and_allows_inside() -> None:
    gf = VoidGeofence(center_x=100.0, center_z=-200.0, radius=60.0, margin=5.0)
    called_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        called_paths.append(path)
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [150.5, 64.0, -200.0], "ready": True}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=gf)

        mv_out = json.loads(ts.mc_move_to(x=160.0, z=-200.0))
        assert mv_out["ok"] is False
        assert mv_out["error"]["code"] == "void_geofence_blocked"

        dig_out = json.loads(ts.mc_dig(160, 64, -200))
        assert dig_out["ok"] is False
        assert dig_out["error"]["code"] == "void_geofence_blocked"

        stair_out = json.loads(ts.mc_skill_staircase(direction="east", steps=8, vertical="down"))
        assert stair_out["ok"] is False
        assert stair_out["error"]["code"] == "void_geofence_blocked"

        tunnel_out = json.loads(ts.mc_skill_tunnel(direction="east", steps=6))
        assert tunnel_out["ok"] is False
        assert tunnel_out["error"]["code"] == "void_geofence_blocked"

        assert "/move_to" not in called_paths
        assert "/dig" not in called_paths

        mv_in = json.loads(ts.mc_move_to(x=120.0, z=-200.0))
        assert mv_in["ok"] is True
        assert "/move_to" in called_paths


def test_bug048_geofence_clamps_mild_overshoot_and_escalates_repeated_or_implausible_blocks() -> None:
    gf = VoidGeofence(center_x=100.0, center_z=-200.0, radius=220.0, margin=8.0)  # safe_radius = 212.0
    move_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [308.5, 64.0, -200.0], "ready": True}})
        if path == "/move_to":
            move_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=gf)

        # 1) Mild overshoot (dist=240 <= 212 * 1.25 = 265) clamps inside safe_radius-1 and executes.
        clamped_out = json.loads(ts.mc_move_to(x=340.0, z=-200.0))
        assert clamped_out["ok"] is True
        assert clamped_out["geofence_clamped"] is True
        assert clamped_out["geofence_clamped_xz"] == [311.0, -200.0]
        assert move_payloads[-1]["x"] == 311.0
        assert move_payloads[-1]["z"] == -200.0

        # 2) Implausible coordinate (> 3 * safe_radius) immediately demands replan.
        far_out = json.loads(ts.mc_move_to(x=1500.0, z=-200.0))
        assert far_out["ok"] is False
        assert far_out["requires_replan"] is True
        assert far_out["error"]["code"] == "void_geofence_blocked"
        assert far_out["error"]["reason"] == "implausible_distance"
        assert far_out["geofence_blocked_count"] == 1

        # 3) Distinct moderate out-of-bounds targets escalate on 3rd hit; tunnel suggests inward dirs.
        b2 = json.loads(ts.mc_move_to(x=450.0, z=-200.0))
        assert b2["ok"] is False
        assert b2.get("requires_replan") is not True
        assert b2["geofence_blocked_count"] == 2

        b3 = json.loads(ts.mc_skill_tunnel(direction="east", steps=6))
        assert b3["ok"] is False
        assert b3["error"]["code"] == "void_geofence_blocked"
        assert b3["geofence_blocked_count"] == 3
        assert b3["requires_replan"] is True
        assert "west" in b3["suggested_safe_directions"]

        # 4) Episode rebind resets the counter.
        ts.rebind_runtime_hooks(geofence=gf)
        b_reset = json.loads(ts.mc_move_to(x=451.0, z=-200.0))
        assert b_reset["geofence_blocked_count"] == 1
        assert b_reset.get("requires_replan") is not True


def test_bug058_path_not_found_suggests_dig_toward_and_escalates_repeats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [100.5, 64.0, 200.5], "dimension": "overworld", "ready": True}},
            )
        if path == "/move_to":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"code": "path_not_found", "message": "No path"}},
            )
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))

        first = json.loads(ts.mc_move_to(x=105.0, y=64.0, z=200.0, range=1.0))
        assert first["ok"] is False
        assert first["suggested"]["mode"] == "dig_toward"
        assert first["path_not_found_count"] == 1
        assert first.get("requires_replan") is not True

        # Second failure within 8m escalates to requires_replan=True
        second = json.loads(ts.mc_move_to(x=106.0, y=64.0, z=201.0, range=1.0))
        assert second["ok"] is False
        assert second["path_not_found_count"] == 2
        assert second["requires_replan"] is True

        # Rebinding episode clears the counter
        ts.rebind_runtime_hooks()
        third = json.loads(ts.mc_move_to(x=120.0, y=64.0, z=200.0, range=1.0))
        assert third["path_not_found_count"] == 1
        assert third.get("requires_replan") is not True


def test_bug047_mine_dfs_clamps_to_55s_and_episode_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.monotonic", lambda: now[0])
    seen_budgets: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))
        monkeypatch.setattr(
            ts.skills,
            "mine_dfs",
            lambda **kwargs: (seen_budgets.append(float(kwargs["max_duration_s"])) or {"ok": True}),
        )
        monkeypatch.setattr(ts.skills, "_ensure_free_slots", lambda min_free=3: (True, []))

        # 1) Unbounded episode caps oversized request to 90s.
        json.loads(ts.mc_skill_mine_dfs(target="ancient_debris", max_duration_s=150.0))
        assert seen_budgets[-1] == 90.0

        # 2) Tight episode deadline (20s remaining) clamps to 16s (reserve 4s).
        ts.rebind_runtime_hooks(episode_deadline_mono=1020.0)
        json.loads(ts.mc_skill_mine_dfs(target="ancient_debris", max_duration_s=90.0))
        assert seen_budgets[-1] == 16.0

        # 3) Imminent deadline (5s remaining) short-circuits without calling mine_dfs.
        ts.rebind_runtime_hooks(episode_deadline_mono=1005.0)
        count_before = len(seen_budgets)
        out = json.loads(ts.mc_skill_mine_dfs(target="ancient_debris", max_duration_s=90.0))
        assert out["ok"] is False
        assert out["error"]["code"] == "episode_deadline_imminent"
        assert len(seen_budgets) == count_before


def test_bug064_scan_ceiling_surfaces_clamp_and_stops_radius_doubling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [100.5, 64.0, 200.5], "dimension": "overworld", "ready": True}},
            )
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [], "total": 0}})
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": []}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))

        first = json.loads(ts.mc_find_blocks(radius=256, types=["lava"]))
        assert first["result"]["radius_clamped_from"] == 256
        assert first["result"]["radius_effective"] == 64
        assert first.get("requires_replan") is not True

        second = json.loads(ts.mc_find_blocks(radius=512, types=["lava"]))
        assert second["requires_replan"] is True
        assert second["scan_exhausted"] is True
        assert second["empty_max_scan_count"] == 2

        e1 = json.loads(ts.mc_get_entities(radius=2048, types=["wolf"]))
        assert e1["result"]["radius_clamped_from"] == 2048
        assert e1.get("requires_replan") is not True

        e2 = json.loads(ts.mc_get_entities(radius=4096, types=["wolf"]))
        assert e2["requires_replan"] is True
        assert e2["scan_exhausted"] is True

        ts.rebind_runtime_hooks()
        e3 = json.loads(ts.mc_get_entities(radius=64, types=["wolf"]))
        assert e3.get("requires_replan") is not True


def test_bug047_move_to_clamps_timeout_and_stops_cleanly_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [2000.0]
    monkeypatch.setattr("mc_agent.harness.mc_tools.time.monotonic", lambda: now[0])
    seen_calls: list[tuple[str, float | None]] = []

    class FakeBot:
        def call(self, path: str, body=None, **kwargs):
            seen_calls.append((path, kwargs.get("timeout_s")))
            if path == "status":
                return {"pos": [100.5, 64.0, 200.5], "dimension": "overworld", "ready": True}
            if path == "move_to":
                raise httpx.ReadTimeout("move timed out")
            return {"ok": True}

    bot = FakeBot()
    ts = McToolset(bot, geofence=VoidGeofence(radius=0.0))  # type: ignore[arg-type]

    # 1) Tight episode deadline (11s remaining -> 8s budget) stops cleanly without raising.
    ts.rebind_runtime_hooks(episode_deadline_mono=2011.0)
    out = json.loads(ts.mc_move_to(x=120.0, z=200.0))
    assert out["ok"] is False
    assert out["requires_replan"] is True
    assert out["error"]["code"] == "move_timeout"
    assert ("move_to", 8.0) in seen_calls
    assert seen_calls[-1][0] == "stop"

    # 2) Imminent deadline (4s remaining) short-circuits before issuing move_to.
    ts.rebind_runtime_hooks(episode_deadline_mono=2004.0)
    before_n = len(seen_calls)
    out2 = json.loads(ts.mc_move_to(x=120.0, z=200.0))
    assert out2["ok"] is False
    assert out2["error"]["code"] == "episode_deadline_imminent"
    assert len(seen_calls) == before_n or all(p != "move_to" for p, _ in seen_calls[before_n:])


def test_world_hud_renders_compact_coords_durability_and_gaps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [-3.4, 72.0, -5.8],
                        "dimension": "overworld",
                        "biome": "birch_forest",
                        "is_day": True,
                        "health": 18.0,
                        "food": 17,
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
                            {"name": "stone_pickaxe", "count": 1, "durability_used": 31},
                            {"name": "iron_ingot", "count": 2},
                            {"name": "raw_iron", "count": 4},
                            {"name": "oak_log", "count": 3},
                        ],
                        "offhand": {"name": "shield", "count": 1},
                        "empty_slots": 31,
                    },
                },
            )
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "iron_ore", "pos": [-1, 70, -4], "dist": 3.2},
                            {"name": "crafting_table", "pos": [-3, 72, -5], "dist": 1.1},
                            {"name": "oak_log", "pos": [1, 72, -6], "dist": 4.4},
                        ]
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot, geofence=VoidGeofence(center_x=0, center_z=0, radius=100, margin=5))
        hud = build_world_hud(
            ts,
            task="制作铁镐",
            target_advancement="minecraft:story/shiny_gear",
            failed_coords=["[-10,70,-10]"],
        )
    assert len(hud) < 650
    assert "[-4,72,-6]" in hud
    assert "剩100" in hud
    assert "iron_ore" in hud
    assert "diamond_boots" in hud
    assert "[-10,70,-10]" in hud


def test_sliding_context_compresses_old_turns_and_ephemeral_hud_never_mutates_history() -> None:
    history: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for idx in range(1, 10):
        history.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"c{idx}", "type": "function", "function": {"name": "mc_dig", "arguments": json.dumps({"x": idx, "y": 64, "z": 0})}}
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": f"c{idx}",
                "content": json.dumps({"ok": idx != 2, "error": {"code": "path_not_found"}} if idx == 2 else {"ok": True, "gained": {"stone": 1}}),
            }
        )

    before_len = len(history)
    before_last = history[-1]["content"]
    ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4, max_ledger_entries=16)
    built = ctx.build(history, ephemeral_hud="[实时HUD]\n位置=[9,64,0]")

    assert len(history) == before_len
    assert history[-1]["content"] == before_last
    assert "[已完成动作账本" in built[2]["content"]
    assert "#1 mc_dig" in built[2]["content"]
    assert "ERR:path_not_found" in built[2]["content"]
    assert built[-1]["content"].endswith("[实时HUD]\n位置=[9,64,0]")
    assert len(built) == 2 + 1 + 4 * 2


def test_model_length_recovery_http_retries_and_thread_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. 流式 finish_reason=length：保留完整前缀 tool_calls，丢弃截断尾巴
    sse_lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"c0","type":"function","function":{"name":"mc_dig","arguments":"{\\"x\\":1,\\"y\\":64,\\"z\\":2}"}},{"index":1,"id":"c1","type":"function","function":{"name":"mc_place","arguments":"{\\"item\\":\\"st"}}]},"finish_reason":"length"}]}',
        "",
        "data: [DONE]",
        "",
    ]
    msg = _stream_message(io.BytesIO("\r\n".join(sse_lines).encode("utf-8")), None)
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "mc_dig"

    # 2. 流式 finish_reason=length 且无完整 tool_calls：抛 OutputTruncatedError，run_mc_agent 内可自愈一次
    sse_bad = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"长篇思考未完"},"finish_reason":"length"}]}',
        "",
        "data: [DONE]",
        "",
    ]
    with pytest.raises(OutputTruncatedError):
        _stream_message(io.BytesIO("\r\n".join(sse_bad).encode("utf-8")), None)

    def handle_bot(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [], "empty_slots": 36}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    turn = 0

    def chat_trunc_then_ok(messages, tools=None, **kwargs):
        nonlocal turn
        turn += 1
        assert tools is not None
        if turn == 1:
            raise OutputTruncatedError("truncated")
        assert any("上轮输出超长被截断" in str(m.get("content", "")) for m in messages)
        return {"role": "assistant", "content": "已完成。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_bot)) as bot:
        rep = run_mc_agent(
            "恢复截断",
            bot,
            chat_fn=chat_trunc_then_ok,
            max_calls=4,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
            include_initial_snapshot=False,
        )
    assert rep["status"] == "answered"
    assert rep["actor_requests"] == 2

    # 3. _chat_direct 对 524 / 429 退避重试 3 次成功
    monkeypatch.setattr("mc_agent.harness.model.time.sleep", lambda _: None)
    env_p = tmp_path / ".env.test"
    env_p.write_text("NJU_API_KEY=sk-test\n", encoding="utf-8")
    spec = QwenSpec(dotenv_path=str(env_p), fallback_dotenv_path=None)
    calls_http = 0

    def retry_transport(req: httpx.Request) -> httpx.Response:
        nonlocal calls_http
        calls_http += 1
        if calls_http == 1:
            return httpx.Response(524, text="cf timeout")
        if calls_http == 2:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="rate limit")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})

    client = httpx.Client(transport=httpx.MockTransport(retry_transport))
    try:
        out = _chat_direct(spec, [{"role": "user", "content": "hi"}], stream=False, client=client)
    finally:
        client.close()
    assert out["content"] == "ok"
    assert calls_http == 3

    # 4. 线程化 _process_request 可被 control 立即取消并触发 on_cancel
    cancelled_cb = False

    def slow_transport(req: httpx.Request) -> httpx.Response:
        import time as _t
        _t.sleep(0.5)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "late"}}]})

    slow_client = httpx.Client(transport=httpx.MockTransport(slow_transport))

    def on_cancel() -> None:
        nonlocal cancelled_cb
        cancelled_cb = True

    checks = 0

    def control_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    with pytest.raises(RequestCancelled):
        _process_request(
            spec,
            [{"role": "user", "content": "hi"}],
            None,
            False,
            on_event=None,
            control=control_cancel,
            client=slow_client,
            on_cancel=on_cancel,
        )
    slow_client.close()
    assert cancelled_cb is True


def test_guardian_silent_eat_does_not_cancel_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from mc_agent.harness.control import check_control

    clock = [10.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    state = {"food": 14, "ate": False}
    events: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        if action == "events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if action == "status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20.0, "food": state["food"], "alive": True, "ready": True}},
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "bread", "count": 3}]}})
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "use_item":
            state["ate"] = True
            state["food"] = 19
            return httpx.Response(200, json={"ok": True, "result": {"used": "bread"}})
        raise AssertionError(action)

    def chat(messages, tools=None, *, control=None, **kwargs):
        clock[0] += 1.5
        # 轮询仅触发纯进食，requires_replan=False，check_control 不应抛 RequestCancelled！
        check_control(control)
        return {"role": "assistant", "content": "未被打断，正常完成。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        rep = run_mc_agent(
            "测试静默进食",
            bot,
            chat_fn=chat,
            max_calls=3,
            survival_guard=True,
            include_initial_snapshot=False,
            on_event=events.append,
        )
    assert rep["status"] == "answered"
    assert state["ate"] is True
    assert rep["guardian"]["incidents"] == 1
    assert rep["guardian"]["cancelled_generations"] == 0
    assert any(e["type"] == "guardian.incident" and e["requires_replan"] is False for e in events)


def test_curriculum_target_priority_and_two_strike_cooldown(tmp_path: Path) -> None:
    inv_items = [{"name": "iron_pickaxe", "count": 1}]
    adv_rows = [
        {"id": "minecraft:story/root", "status": "completed"},
        {"id": "minecraft:husbandry/root", "status": "completed"},
        {"id": "minecraft:adventure/root", "status": "completed"},
        {"id": "minecraft:story/mine_stone", "status": "completed"},
        {"id": "minecraft:story/upgrade_tools", "status": "completed"},
        {"id": "minecraft:story/smelt_iron", "status": "completed"},
        {"id": "minecraft:story/iron_tools", "status": "completed"},
        {"id": "minecraft:story/lava_bucket", "status": "not_started"},
        {"id": "minecraft:adventure/use_lodestone", "status": "not_started"},
        {"id": "minecraft:husbandry/make_a_sign_glow", "status": "not_started"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(inv_items)}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": list(adv_rows)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(path)

    targets_seen: list[str] = []
    tool_counts_seen: list[int] = []

    def chat_ep(messages, tools=None, **kwargs):
        tool_counts_seen.append(len(tools or []))
        return {"role": "assistant", "content": "本轮尝试结束。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        first_target, _ = select_next_target(bot)
        assert first_target.key == "minecraft:story/shiny_gear"
        assert first_target.points == 60

        sup = CurriculumSupervisor(
            bot,
            chat_fn=chat_ep,
            runs_dir=tmp_path / "runs",
            duration_s=60.0,
            max_calls_per_episode=2,
            cooldown_s=600.0,
            strike_limit=2,
        )
        summary = sup.run(max_episodes=3)
        targets_seen = [ep["target"] for ep in summary["episodes"]]

    # 前两次 shiny_gear 无进展后第 2 次打标冷却，第 3 次自动切换至下一个最高分主世界目标（make_a_sign_glow 30分）
    assert targets_seen[0] == "minecraft:story/shiny_gear"
    assert targets_seen[1] == "minecraft:story/shiny_gear"
    assert summary["episodes"][1]["cooled_down"] is True
    assert targets_seen[2] == "minecraft:husbandry/make_a_sign_glow"
    # compact profile 下每局工具数严格精简（<=22，不含全量 28 个工具）
    assert all(cnt <= len(COMPACT_CORE_TOOLS) + 5 for cnt in tool_counts_seen)


def test_canopy_descend_and_occluded_ore_tunnel() -> None:
    blocks: dict[tuple[int, int, int], str] = {
        (0, 79, 0): "oak_leaves",
        (0, 78, 0): "air",
        (0, 77, 0): "oak_leaves",
        (0, 76, 0): "grass_block",
    }
    pos_state = [0.5, 80.0, 0.5]

    def canopy_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content.decode()) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(pos_state), "in_water": False}})
        if path == "/get_block":
            p = tuple(body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": blocks.get(p, "air"), "diggable": blocks.get(p, "air") != "air"}})
        if path == "/dig":
            p = tuple(body["pos"])
            blocks[p] = "air"
            if p == (0, 79, 0):
                pos_state[1] = 78.0
            elif p == (0, 77, 0):
                pos_state[1] = 77.0
            return httpx.Response(200, json={"ok": True, "result": {"dug": "oak_leaves"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(canopy_handler)) as bot:
        res = MacroSkills(bot).unstick()
        assert res["ok"] is True
        assert res["descended_canopy"] is True
        assert pos_state[1] == 77.0

    # 隔墙矿石：第一次 find_blocks 发现同层 3m 外有 iron_ore 但 dig 报 path_not_found；
    # mine_blocks 自动调 tunnel 后重试 dig 成功！
    wall_dug = False
    ore_gained = False
    ore_pos = [3, 64, 0]
    bot_pos = [0.5, 64.0, 0.5]

    def ore_handler(req: httpx.Request) -> httpx.Response:
        nonlocal wall_dug, ore_gained, bot_pos
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content.decode()) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(bot_pos), "health": 20, "food": 20, "on_ground": True, "ready": True, "alive": True}})
        if path == "/inventory":
            items = [{"name": "stone_pickaxe", "count": 1}]
            if ore_gained:
                items.append({"name": "raw_iron", "count": 1})
            return httpx.Response(200, json={"ok": True, "result": {"items": items}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": ore_pos, "dist": 2.8}]}})
        if path == "/get_block":
            pos = tuple(body["pos"])
            if pos == tuple(ore_pos):
                return httpx.Response(200, json={"ok": True, "result": {"name": "iron_ore", "diggable": True}})
            if pos[1] == 63:
                return httpx.Response(200, json={"ok": True, "result": {"name": "stone", "diggable": True}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if wall_dug else "stone", "diggable": True}})
        if path == "/dig":
            if body["pos"] == ore_pos and not wall_dug:
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "wall"}})
            if body["pos"] == ore_pos:
                ore_gained = True
                return httpx.Response(200, json={"ok": True, "result": {"dug": "iron_ore", "gained": {"raw_iron": 1}}})
            wall_dug = True
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {}}})
        if path == "/move_to":
            wall_dug = True
            bot_pos = [float(body.get("x", 2.5)), 64.0, float(body.get("z", 0.5))]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(bot_pos)}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(ore_handler)) as bot:
        mres = MacroSkills(bot).mine_blocks(["iron_ore"], count=1, radius=16)
        assert mres["ok"] is True
        assert mres["gained"].get("raw_iron", 0) >= 1
        assert "occluded_tunnel" in mres


def test_cancel_aborts_zombie_retries_and_enforces_post_cancel_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mc_agent.harness.model.time.sleep", lambda _: None)
    env_p = tmp_path / ".env.v2"
    env_p.write_text("NJU_API_KEY=sk-test\n", encoding="utf-8")
    spec = QwenSpec(dotenv_path=str(env_p), fallback_dotenv_path=None, post_cancel_cooldown_s=1.0)

    # 1. cancel_event 已置位时，HTTPError 立即抛 RequestCancelled，绝不再发出第 2 次僵尸重试
    zombie_calls = 0
    cancel_ev = threading.Event()

    def abort_transport(req: httpx.Request) -> httpx.Response:
        nonlocal zombie_calls
        zombie_calls += 1
        cancel_ev.set()
        raise httpx.ReadError("client closed")

    abort_client = httpx.Client(transport=httpx.MockTransport(abort_transport))
    try:
        with pytest.raises(RequestCancelled):
            _chat_direct(
                spec,
                [{"role": "user", "content": "hi"}],
                stream=False,
                client=abort_client,
                cancel_event=cancel_ev,
            )
    finally:
        abort_client.close()
    assert zombie_calls == 1

    # 2. Harness concurrency-limit 429（哪怕带 Retry-After: 2）升级为 >=6.0s 阶梯退避且最多 5 次成功
    attempts = 0
    retry_events: list[dict[str, Any]] = []

    def conc_transport(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 5:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"error": {"message": "Harness group concurrency limit: 2 simultaneous requests"}},
            )
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "recovered"}}]})

    conc_client = httpx.Client(transport=httpx.MockTransport(conc_transport))
    try:
        out = _chat_direct(
            spec,
            [{"role": "user", "content": "hi"}],
            stream=False,
            on_event=retry_events.append,
            client=conc_client,
        )
    finally:
        conc_client.close()
    assert out["content"] == "recovered"
    assert attempts == 5
    waits = [e["wait_s"] for e in retry_events if e["type"] == "http_retry"]
    assert len(waits) == 4
    for actual, base in zip(waits, [6.0, 10.0, 15.0, 20.0]):
        assert base <= actual <= base + 3.0
    assert all(e["concurrency_limit"] is True for e in retry_events if e["type"] == "http_retry")

    # 3. QwenModel 在一次取消后会进入 post_cancel_cooldown_s，并于下次请求触发 cancel_cooldown_wait
    # 首客户端用事件阻塞促使 _process_request 真正进入 worker 等待后再被 control 取消，旋转后的客户端立刻返回 200
    call_phase = {"first": True}
    started_ev = threading.Event()
    release_ev = threading.Event()

    def factory_seq(s: QwenSpec, base_url: str) -> httpx.Client:
        if call_phase["first"]:
            call_phase["first"] = False

            def slow_h(req: httpx.Request) -> httpx.Response:
                started_ev.set()
                release_ev.wait(timeout=1.0)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "late"}}]})

            return httpx.Client(transport=httpx.MockTransport(slow_h))

        def fast_h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok2"}}]})

        return httpx.Client(transport=httpx.MockTransport(fast_h))

    monkeypatch.setattr("mc_agent.harness.model._make_http_client", factory_seq)
    qwen = QwenModel(spec)
    try:
        with pytest.raises(RequestCancelled):
            qwen.chat([{"role": "user", "content": "1"}], stream=False, control=started_ev.is_set)

        cd_events: list[dict[str, Any]] = []
        res2 = qwen.chat(
            [{"role": "user", "content": "2"}],
            stream=False,
            on_event=cd_events.append,
            control=lambda: False,
        )
        assert res2["content"] == "ok2"
        assert any(e["type"] == "cancel_cooldown_wait" and e["wait_s"] > 0 for e in cd_events)
    finally:
        release_ev.set()
        qwen.close()


def test_curriculum_exempts_transient_429_from_target_strikes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mc_agent.harness.curriculum.time.sleep", lambda _: None)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0, 64, 0], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": []}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        raise AssertionError(path)

    turn = 0

    def chat_ep(messages, tools=None, **kwargs):
        nonlocal turn
        turn += 1
        if turn <= 2:
            raise RuntimeError('Qwen API 返回 HTTP 429：{"error":{"message":"Harness group concurrency limit: 2 simultaneous requests"}}')
        return {"role": "assistant", "content": "暂无进展。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot:
        sup = CurriculumSupervisor(
            bot,
            chat_fn=chat_ep,
            runs_dir=tmp_path / "runs",
            duration_s=60.0,
            max_calls_per_episode=1,
            cooldown_s=600.0,
            strike_limit=2,
            api_error_backoff_s=8.0,
        )
        summary = sup.run(max_episodes=4)

    eps = summary["episodes"]
    assert len(eps) == 4
    assert eps[0]["target"] == "bootstrap_stone_age"
    assert eps[0]["transient_api_error"] is True
    assert eps[0]["cooled_down"] is False
    assert eps[1]["target"] == "bootstrap_stone_age"
    assert eps[1]["transient_api_error"] is True
    assert eps[1]["cooled_down"] is False
    # 非 429 的正常无进展两次才触发 2-strike 冷却
    assert eps[2]["target"] == "bootstrap_stone_age"
    assert eps[2]["transient_api_error"] is False
    assert eps[2]["cooled_down"] is False
    assert eps[3]["target"] == "bootstrap_stone_age"
    assert eps[3]["cooled_down"] is True


def test_compact_tool_schemas_fit_sub_3k_budget() -> None:
    def dummy(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(dummy)) as bot:
        ts = McToolset(bot)
        s_core = ts.registry(profile="compact", extra_tools=()).schemas()
        s_ext = ts.registry(
            profile="compact",
            extra_tools=("mc_interact", "mc_use_item", "mc_posture"),
        ).schemas()
        assert len(s_core) == 15
        assert len(s_ext) == 18
        assert len(json.dumps(s_core, ensure_ascii=False)) < 4300
        assert len(json.dumps(s_ext, ensure_ascii=False)) < 5250

        compact_by_name = {sc["function"]["name"]: sc["function"] for sc in s_core}
        assert "mc_attack" in compact_by_name
        assert "mc_skill_flee" in compact_by_name
        assert "mc_craft" not in compact_by_name
        assert "mc_equip" not in compact_by_name
        assert "mc_skill_collect_drops" not in compact_by_name
        assert "mc_skill_bunker" not in compact_by_name
        assert compact_by_name["mc_skill_staircase"]["parameters"]["properties"]["direction"]["description"] == "north/south/east/west"
        assert compact_by_name["mc_skill_tunnel"]["parameters"]["properties"]["direction"]["description"] == "north/south/east/west"

        called_dirs: list[tuple[str, int, str]] = []
        ts.skills.dig_staircase = lambda direction="north", steps=12, vertical="down", stop_on_ore=True: (
            called_dirs.append((direction, steps, vertical)) or {"ok": True, "direction": direction, "vertical": vertical}
        )
        coerced = json.loads(ts.mc_skill_staircase(direction="down", steps=2, vertical="down"))
        assert coerced["ok"] is True
        assert called_dirs == [("north", 2, "down")]

        full_by_name = {sc["function"]["name"]: sc["function"] for sc in ts.registry().schemas()}
        assert "speed" in full_by_name["mc_status"]["description"]
        assert full_by_name["mc_status"]["parameters"]["additionalProperties"] is False


def test_kb_lookup_covers_all_125_advancements_and_scoring_metadata() -> None:
    catalog = _load_full_advancement_kb()
    assert len(catalog) == 125

    res_trident = lookup_kb("trident")
    assert res_trident["ok"] is True
    assert res_trident["count"] >= 2
    by_id = {item["id"]: item for item in res_trident["results"]}
    assert "drowned_trident" in by_id
    assert by_id["drowned_trident"]["kind"] == "mob"
    assert "minecraft:adventure/throw_trident" in by_id
    throw_entry = by_id["minecraft:adventure/throw_trident"]
    assert throw_entry["kind"] == "advancement"
    assert throw_entry["points"] == 60
    assert throw_entry["scored"] is True
    ids_in_order = [item["id"] for item in res_trident["results"]]
    if "minecraft:adventure/very_very_frightening" in ids_in_order:
        assert ids_in_order.index("minecraft:adventure/throw_trident") < ids_in_order.index(
            "minecraft:adventure/very_very_frightening"
        )

    res_zh = lookup_kb("抖包袱", category="advancement")
    assert res_zh["count"] >= 1
    assert res_zh["results"][0]["id"] == "minecraft:adventure/throw_trident"
    assert res_zh["results"][0]["points"] == 60

    res_glow = lookup_kb("make_a_sign_glow")
    assert res_glow["count"] >= 1
    top_glow = res_glow["results"][0]
    assert top_glow["id"] == "minecraft:husbandry/make_a_sign_glow"
    assert top_glow["points"] == 30
    assert top_glow["unconditional"] is True
    assert "minecraft:story/mine_stone" not in {r["id"] for r in res_glow["results"]}

    res_lode = lookup_kb("lodestone")
    assert res_lode["count"] >= 2
    lode_ids = {item["id"]: item for item in res_lode["results"]}
    assert "lodestone" in lode_ids
    assert lode_ids["lodestone"]["kind"] == "recipe"
    assert "chiseled_stone_bricks" in lode_ids["lodestone"]["ingredients"]
    assert "minecraft:adventure/use_lodestone" in lode_ids
    assert lode_ids["minecraft:adventure/use_lodestone"]["points"] == 15


def test_zombie_counter_attack_and_knockback_immune_flee(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1) Armed 1v1 zombie counter-attack in GenerationGuard
    calls_1: list[tuple[str, dict[str, Any]]] = []
    state_1: dict[str, Any] = {"killed": False, "hurt_emitted": False}

    def handle_armed(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            nh = None if state_1["killed"] else {"id": 77, "name": "zombie", "dist": 1.8, "pos": [0.0, 64.0, -2.0]}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.0, 64.0, 0.0],
                        "health": 17,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "on_ground": True,
                        "nearest_hostile": nh,
                    },
                },
            )
        if action == "events":
            rows = (
                [
                    {
                        "seq": 1,
                        "type": "hurt",
                        "health": 17,
                        "source": "mob_attack",
                        "source_entity": {"id": 77, "name": "zombie", "dist": 1.8, "pos": [0.0, 64.0, -2.0]},
                    }
                ]
                if not state_1["hurt_emitted"]
                else []
            )
            state_1["hurt_emitted"] = True
            return httpx.Response(200, json={"ok": True, "result": {"events": rows, "last_seq": 1}})
        if action == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_sword", "count": 1}, {"name": "stone_pickaxe", "count": 1}]}},
            )
        if action == "get_entities":
            ents = [] if state_1["killed"] else [{"id": 77, "name": "zombie", "dist": 1.8, "pos": [0.0, 64.0, -2.0]}]
            return httpx.Response(200, json={"ok": True, "result": {"entities": ents}})
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "equip":
            calls_1.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "attack":
            calls_1.append((action, body))
            state_1["killed"] = True
            return httpx.Response(200, json={"ok": True, "result": {"killed": True}})
        raise AssertionError(f"unexpected action in armed counter-attack: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_armed)) as bot1:
        guard1 = GenerationGuard(bot1, EventSink(lambda _: None), checkpoint=lambda: None)
        incident1 = guard1.poll()
    assert incident1 is not None
    assert incident1["actions"][0]["action"] == "combat_or_flee"
    assert incident1["actions"][0]["ok"] is True
    assert incident1["actions"][0]["result"]["mode"] == "counter_attack"
    assert incident1["actions"][0]["result"]["weapon"] == "stone_sword"
    assert incident1["actions"][0]["result"]["killed"] is True
    assert [c[0] for c in calls_1] == ["equip", "attack"]
    assert calls_1[0][1]["item"] == "stone_sword"
    assert calls_1[1][1]["entity_id"] == 77

    # 2) Unarmed airborne knockback flee in GenerationGuard (respecting VoidGeofence)
    calls_2: list[tuple[str, dict[str, Any]]] = []
    state_2: dict[str, Any] = {"pos": [0.0, 64.0, 0.0], "hurt_emitted": False}

    def handle_flee(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            dist = abs(state_2["pos"][2] - (-2.0))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state_2["pos"]),
                        "health": 15,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "on_ground": False,
                        "nearest_hostile": {"id": 77, "name": "zombie", "dist": dist, "pos": [0.0, 64.0, -2.0]},
                    },
                },
            )
        if action == "events":
            rows = (
                [
                    {
                        "seq": 1,
                        "type": "hurt",
                        "health": 15,
                        "source": "mob_attack",
                        "source_entity": {"id": 77, "name": "zombie", "dist": 2.0, "pos": [0.0, 64.0, -2.0]},
                    }
                ]
                if not state_2["hurt_emitted"]
                else []
            )
            state_2["hurt_emitted"] = True
            return httpx.Response(200, json={"ok": True, "result": {"events": rows, "last_seq": 1}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if action == "get_entities":
            dist = abs(state_2["pos"][2] - (-2.0))
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 77, "name": "zombie", "dist": dist, "pos": [0.0, 64.0, -2.0]}]}},
            )
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "control":
            calls_2.append((action, body))
            assert body["sprint"] is True
            assert body["jump"] is True
            assert body["stop_on"] == ["death"]
            state_2["pos"] = [0.0, 64.0, 6.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_2["pos"])}})
        if action == "move_to":
            calls_2.append((action, body))
            assert body["stop_on"] == ["death"]
            state_2["pos"] = [0.0, 64.0, 14.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_2["pos"])}})
        if action == "attack":
            raise AssertionError("unarmed bot must not attack")
        raise AssertionError(f"unexpected action in airborne flee: {action}")

    gf = VoidGeofence(center_x=0.0, center_z=0.0, radius=55.0, margin=5.0)
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_flee)) as bot2:
        guard2 = GenerationGuard(bot2, EventSink(lambda _: None), checkpoint=lambda: None, geofence=gf)
        incident2 = guard2.poll()
    assert incident2 is not None
    assert incident2["actions"][0]["action"] == "combat_or_flee"
    assert incident2["actions"][0]["ok"] is True
    assert incident2["actions"][0]["result"]["mode"] == "flee"
    assert incident2["actions"][0]["result"]["moved_dist"] >= 5.0
    assert not any(a == "attack" for a, _ in calls_2)
    assert any(a == "control" for a, _ in calls_2)

    # 3) Creeper override even when armed (iron_sword + health=20)
    calls_3: list[tuple[str, dict[str, Any]]] = []
    state_3: dict[str, Any] = {"pos": [0.0, 64.0, 0.0]}

    def handle_creeper(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            dist = abs(state_3["pos"][2] - 3.0)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state_3["pos"]),
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 88, "name": "creeper", "dist": dist, "pos": [0.0, 64.0, 3.0]},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_sword", "count": 1}]}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 88, "name": "creeper", "dist": 3.5, "pos": [0.0, 64.0, 3.0]}]}},
            )
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "control":
            calls_3.append((action, body))
            state_3["pos"] = [0.0, 64.0, -7.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_3["pos"])}})
        if action == "move_to":
            calls_3.append((action, body))
            state_3["pos"] = [0.0, 64.0, -14.0]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state_3["pos"])}})
        if action == "attack":
            raise AssertionError("must refuse /attack near creeper")
        raise AssertionError(f"unexpected action near creeper: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_creeper)) as bot3:
        res3 = MacroSkills(bot3).defend_or_flee(
            attacker={"id": 88, "name": "creeper", "dist": 3.5, "pos": [0.0, 64.0, 3.0]},
            urgent=True,
        )
    assert res3["mode"] == "flee"
    assert res3["ok"] is True
    assert not any(a == "attack" for a, _ in calls_3)

    # 4) McToolset.mc_attack auto-equip & WorldHUD #id rendering
    calls_4: list[tuple[str, dict[str, Any]]] = []

    def handle_tool(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.0, 64.0, 0.0],
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 77, "name": "zombie", "dist": 1.8},
                    },
                },
            )
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [{"id": 77, "name": "zombie", "type": "hostile", "dist": 1.8, "pos": [0.0, 64.0, -1.8]}]
                    },
                },
            )
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "held": {"name": "stone_pickaxe"},
                        "items": [{"name": "stone_sword", "count": 1}, {"name": "stone_pickaxe", "count": 1}],
                    },
                },
            )
        if action == "equip":
            calls_4.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "attack":
            calls_4.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {"killed": True}})
        raise AssertionError(f"unexpected action in mc_attack: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_tool)) as bot4:
        ts4 = McToolset(bot4)
        atk_out = json.loads(ts4.mc_attack(77))
        hud_txt = build_world_hud(ts4)
    assert atk_out["ok"] is True
    assert atk_out["result"]["killed"] is True
    assert [a for a, _ in calls_4] == ["equip", "attack"]
    assert calls_4[0][1] == {"item": "stone_sword", "slot": "hand"}
    assert calls_4[1][1] == {"entity_id": 77, "timeout": 20000}
    assert "zombie#77@1.8m" in hud_txt
    assert "近敌(无遮挡!)" in hud_txt


def test_cave_shaft_structure_occluded_calmness_and_shaft_seal() -> None:
    # 1) Carved shaft + occluded skeleton: HUD shows shaft & 隔墙怪, mc_skill_flee holds inside shaft
    shaft_moves: list[tuple[str, dict[str, Any]]] = []

    def shaft_block(bx: int, by: int, bz: int) -> str:
        # 1x2 pocket at (0, 57..58, 0), roofed at (0, 59..60, 0), east/west/north walls solid stone, south staircase up
        if bx == 0 and bz == 0 and by in (57, 58):
            return "air"
        if bx == 0 and bz == 1 and by in (58, 59):
            return "air"
        return "stone"

    def handle_shaft_occluded(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 57.0, 0.5],
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 51, "name": "skeleton", "dist": 6.3, "pos": [6.5, 59.0, 0.5]},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 16}]}},
            )
        if action == "find_blocks":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"blocks": [{"name": "stone", "pos": [1, 57, 0], "dist": 1.0}]}},
            )
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [
                            {"id": 51, "name": "skeleton", "type": "hostile", "dist": 6.3, "pos": [6.5, 59.0, 0.5]}
                        ]
                    },
                },
            )
        if action == "get_block":
            pos = body.get("pos") or [0, 0, 0]
            return httpx.Response(200, json={"ok": True, "result": {"name": shaft_block(int(pos[0]), int(pos[1]), int(pos[2]))}})
        if action in {"control", "move_to", "attack"}:
            shaft_moves.append((action, body))
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(f"unexpected action in shaft occluded test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_shaft_occluded)) as bot1:
        ts1 = McToolset(bot1)
        hud1 = build_world_hud(ts1)
        flee1 = json.loads(ts1.mc_skill_flee(distance=20))
    assert "地形=探矿穴(3面岩壁,开:south上行)" in hud1
    assert "隔墙怪=skeleton@6.3m" in hud1
    assert "stone阻隔,勿弃坑" in hud1
    assert "近敌(无遮挡!)" not in hud1
    assert flee1["ok"] is True
    assert flee1["mode"] == "hold_in_shaft"
    assert flee1["moved_dist"] == 0.0
    assert shaft_moves == []

    # 2) Carved shaft + unoccluded intruding creeper: seals north corridor with cobblestone & stays at Y=57
    seal_blocks: dict[tuple[int, int, int], str] = {}
    seal_calls: list[tuple[str, dict[str, Any]]] = []

    def seal_block_at(bx: int, by: int, bz: int) -> str:
        if (bx, by, bz) in seal_blocks:
            return seal_blocks[(bx, by, bz)]
        if bx == 0 and bz == 0 and by in (57, 58):
            return "air"
        if bx == 0 and bz <= -1 and by in (57, 58):
            return "air"
        if bx == 0 and bz == 1 and by in (58, 59):
            return "air"
        return "stone"

    def handle_shaft_seal(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 57.0, 0.5],
                        "health": 20,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 91, "name": "creeper", "dist": 4.0, "pos": [0.5, 57.0, -3.5]},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 16}]}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [{"id": 91, "name": "creeper", "type": "hostile", "dist": 4.0, "pos": [0.5, 57.0, -3.5]}]
                    },
                },
            )
        if action == "get_block":
            pos = body.get("pos") or [0, 0, 0]
            return httpx.Response(200, json={"ok": True, "result": {"name": seal_block_at(int(pos[0]), int(pos[1]), int(pos[2]))}})
        if action == "place":
            seal_calls.append((action, body))
            p = body["pos"]
            seal_blocks[(int(p[0]), int(p[1]), int(p[2]))] = str(body["item"])
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"]}})
        if action == "attack":
            raise AssertionError("must not attack creeper")
        raise AssertionError(f"unexpected action in shaft seal test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_shaft_seal)) as bot2:
        seal_res = MacroSkills(bot2).defend_or_flee(
            attacker={"id": 91, "name": "creeper", "dist": 4.0, "pos": [0.5, 57.0, -3.5]},
            urgent=True,
        )
    assert seal_res["ok"] is True
    assert seal_res["mode"] == "seal_shaft"
    assert seal_res["sealed_direction"] == "north"
    assert seal_res["placed_positions"] == [[0, 57, -1], [0, 58, -1]]
    assert seal_res["pos_after"][1] == 57.0
    assert seal_blocks[(0, 57, -1)] == "cobblestone"
    assert seal_blocks[(0, 58, -1)] == "cobblestone"

    # 3) dig_staircase cave_or_drop_ahead reports viable_directions
    def handle_stair_drop(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "alive": True, "ready": True}},
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "get_block":
            pos = body.get("pos") or [0, 64, 0]
            bx, by, bz = int(pos[0]), int(pos[1]), int(pos[2])
            if (bx, by, bz) == (0, 65, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if (bx, by, bz) == (0, 62, -1):
                # Drop ahead to the north
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if bx == -1 and by == 62 and bz == 0:
                # Lava pit to the west (unsafe)
                return httpx.Response(200, json={"ok": True, "result": {"name": "lava"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        raise AssertionError(f"unexpected action in staircase drop test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_stair_drop)) as bot3:
        stair_res = MacroSkills(bot3).dig_staircase(direction="north", steps=4, vertical="down")
    assert stair_res["stop_reason"] == "cave_or_drop_ahead"
    assert set(stair_res["viable_directions"]) == {"south", "east"}
    assert "实测可换向安全下探" in stair_res["hint"]


def test_leaves_not_wall_all_dirs_blocked_iron_gate_and_no_food_guard() -> None:
    # 1a) birch_leaves 绝不当作实心阻隔墙体
    def handle_leaves(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "get_block":
            pos = tuple(body["pos"])
            if pos[0] == -2:
                return httpx.Response(200, json={"ok": True, "result": {"name": "birch_leaves"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_leaves)) as bot_l:
        assert ray_wall_blocker(bot_l, (0.5, 70.0, 0.5), (-5.5, 70.0, 0.5)) is None

    # 1b) 露天石头暂挡输出“遮挡怪=...露天注意绕行”而非“勿弃坑”，且低血 0 食物输出禁空吃提醒
    def handle_surface_hud(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 70.0, 0.5],
                        "health": 2.0,
                        "food": 17,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": {"id": 88, "name": "skeleton", "dist": 6.0},
                    },
                },
            )
        if action == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 12}]}},
            )
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"entities": [{"id": 88, "name": "skeleton", "type": "hostile", "pos": [-5.5, 70.0, 0.5]}]},
                },
            )
        if action == "get_block":
            bx, by, bz = (int(v) for v in body["pos"])
            if (bx, by, bz) == (-2, 71, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_surface_hud)) as bot_h:
        hud_text = build_world_hud(McToolset(bot_h))
    assert "遮挡怪=skeleton@" in hud_text
    assert "露天注意绕行" in hud_text
    assert "勿弃坑" not in hud_text
    assert "包内无食物(禁调mc_use_item空吃)" in hud_text

    # 2) 四面悬空时 dig_staircase 返回 viable_directions=[] + all_directions_blocked=True，且 extract_failed_coords 收录浮点坐标
    def handle_all_drop(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [-155.7, 75.0, -381.5], "health": 20, "food": 20, "alive": True, "ready": True}},
            )
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if action == "get_block":
            pos = body.get("pos") or [0, 75, 0]
            bx, by, bz = int(pos[0]), int(pos[1]), int(pos[2])
            if (bx, by, bz) == (-156, 74, -382):
                return httpx.Response(200, json={"ok": True, "result": {"name": "grass_block"}})
            if (bx, by, bz) == (-156, 76, -382):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if by == 73:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "birch_leaves"}})
        raise AssertionError(f"unexpected action in all-drop test: {action}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_all_drop)) as bot_d:
        res_drop = MacroSkills(bot_d).dig_staircase(direction="west", steps=6, vertical="down")
    assert res_drop["stop_reason"] == "cave_or_drop_ahead"
    assert res_drop["viable_directions"] == []
    assert res_drop["all_directions_blocked"] is True
    assert "严禁在当前位置换方向继续调 mc_skill_staircase" in res_drop["hint"]

    extracted = extract_failed_coords(
        [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"west"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps(res_drop, ensure_ascii=False)},
        ]
    )
    assert extracted == ["[-156,75,-382]"]

    # 3) 流断连报错被 _is_transient_llm_error 豁免，且 0 铁时即使 bootstrap 冷却也绝不派发 crafters_crafting_crafters / ol_betsy
    assert _is_transient_llm_error({"status": "runtime_error", "error": "模型请求线程意外退出，未取得完整响应。"}) is True

    def handle_zero_iron(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "stick", "count": 4}]}},
            )
        if path == "/advancements":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "advancements": [
                            {"id": "minecraft:story/root", "status": "completed"},
                            {"id": "minecraft:story/mine_stone", "status": "completed"},
                            {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                            {"id": "minecraft:adventure/root", "status": "completed"},
                            {"id": "minecraft:adventure/kill_a_mob", "status": "completed"},
                            {"id": "minecraft:husbandry/root", "status": "completed"},
                            {"id": "minecraft:adventure/crafters_crafting_crafters", "status": "unlocked"},
                            {"id": "minecraft:adventure/ol_betsy", "status": "unlocked"},
                            {"id": "minecraft:story/iron_tools", "status": "unlocked"},
                            {"id": "minecraft:adventure/sleep_in_bed", "status": "unlocked"},
                        ]
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_zero_iron)) as bot_c:
        picked, _ = select_next_target(bot_c, cooldowns={"bootstrap_iron_age": 999999.0}, now=1.0)
    assert picked.key not in {"minecraft:adventure/crafters_crafting_crafters", "minecraft:adventure/ol_betsy"}

    # 4) mc_use_item("golden_apple") 在 0 食物背包报错时禁止继续空吃且不提示金苹果配方
    def handle_no_food(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/use_item":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": {
                        "code": "missing_item",
                        "message": "no golden_apple in inventory",
                        "inventory": {"cobblestone": 190, "birch_log": 3, "stone_pickaxe": 1},
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_no_food)) as bot_f:
        raw_err = McToolset(bot_f).mc_use_item("golden_apple")
    parsed_err = json.loads(raw_err)["error"]
    assert parsed_err["no_food_in_inventory"] is True
    assert "严禁继续调用 mc_use_item" in parsed_err["kb_hint"]
    assert "gold_ingot" not in parsed_err["kb_hint"]


def test_craft_gear_underground_path_not_found_recovery_and_unreachable_ore_hint() -> None:
    # 1) craft_gear 在地下遇到 path_not_found（试图寻路地表工作台）时就地掏墙龛放置工作台并重试合成 stone_pickaxe
    craft_state: dict[str, Any] = {
        "inv": [
            {"name": "cobblestone", "count": 64},
            {"name": "stick", "count": 4},
            {"name": "crafting_table", "count": 1},
            {"name": "wooden_pickaxe", "count": 1},
        ],
        "table_placed_at": None,
        "craft_attempts": 0,
    }

    def handle_craft_recovery(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 50.0, 0.5], "in_water": False, "alive": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(craft_state["inv"])}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/get_block":
            pos = tuple(body["pos"])
            if pos == (1, 49, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
            if pos == (1, 50, 0):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air" if craft_state["table_placed_at"] == [1, 50, 0] else "stone"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/dig":
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {"cobblestone": 1}}})
        if path == "/place":
            if body.get("pos") is None or body.get("pos") != [1, 50, 0]:
                return httpx.Response(400, json={"ok": False, "error": {"code": "no_place", "message": "cramped"}})
            craft_state["table_placed_at"] = [1, 50, 0]
            craft_state["inv"] = [r for r in craft_state["inv"] if r["name"] != "crafting_table"]
            return httpx.Response(200, json={"ok": True, "result": {"placed": "crafting_table", "pos": [1, 50, 0]}})
        if path == "/craft":
            craft_state["craft_attempts"] += 1
            if craft_state["craft_attempts"] == 1:
                return httpx.Response(
                    400,
                    json={"ok": False, "error": {"code": "path_not_found", "message": "No path to the goal!", "from": [0.5, 50, 0.5], "to": [3, 72, 0]}},
                )
            craft_state["inv"].append({"name": "stone_pickaxe", "count": 1})
            return httpx.Response(200, json={"ok": True, "result": {"crafted": "stone_pickaxe", "gained": 1}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_craft_recovery)) as bot_cg:
        cg_res = MacroSkills(bot_cg).craft_gear("stone_pickaxe", count=1)
    assert cg_res["ok"] is True
    assert cg_res["crafted"] == "stone_pickaxe"
    assert craft_state["craft_attempts"] == 2
    assert craft_state["table_placed_at"] == [1, 50, 0]

    # 2) mine_blocks 目标下方 out_of_reach 且掘进失败时附带 nearest_unreachable_target 与 approach_hint
    def handle_unreachable_ore(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 56.0, 0.5], "in_water": False, "alive": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": [0, 48, 4], "dist": 8.9}]}},
            )
        if path == "/get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/dig":
            return httpx.Response(400, json={"ok": False, "error": {"code": "out_of_reach", "message": "too far"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_unreachable_ore)) as bot_mine:
        m_res = MacroSkills(bot_mine).mine_blocks(["iron_ore"], count=3, radius=16)
    assert m_res["ok"] is False
    assert m_res["nearest_unreachable_target"]["pos"] == [0, 48, 4]
    assert m_res["nearest_unreachable_target"]["bearing"] == "south"
    assert "mc_skill_staircase(direction='south', vertical='down'" in m_res["approach_hint"]


def test_quarantine_recovery_adjacent_fluid_guard_and_staircase_dir_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.bot.time.sleep", lambda _: None)

    # 1) action_quarantined 自动 disconnect -> wait not_connected -> connect -> ready 并重试；
    #    action_timeout 清理隔离重连后仍抛出 action_timeout（绝不重跑超时动作）
    q_state: dict[str, Any] = {
        "quarantined": True,
        "connected": True,
        "events": [],
        "dig_calls": 0,
    }

    def handle_quarantine(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        q_state["events"].append(path)
        if path == "/disconnect":
            q_state["connected"] = False
            q_state["quarantined"] = False
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        if path == "/status":
            if not q_state["connected"]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "not connected"}})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 50.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True}})
        if path == "/connect":
            q_state["connected"] = True
            return httpx.Response(200, json={"ok": True, "result": {"ok": True, "reconnect": True}})
        if path == "/dig":
            q_state["dig_calls"] += 1
            if q_state["dig_calls"] == 1:
                return httpx.Response(200, json={"ok": False, "error": {"code": "action_quarantined", "message": "previous action timed out; disconnect and reconnect before resuming"}})
            if q_state["dig_calls"] == 2:
                return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
            if q_state["dig_calls"] == 3:
                q_state["quarantined"] = True
                return httpx.Response(200, json={"ok": False, "error": {"code": "action_timeout", "message": "action cancelled: action_timeout"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", base_url="http://100.93.211.71:18900/api/v1", transport=httpx.MockTransport(handle_quarantine)) as bot_q:
        res_ok = bot_q.call("dig", {"pos": [1, 50, 0]})
        assert res_ok == {"dug": "stone"}
        assert q_state["dig_calls"] == 2
        assert q_state["events"][:5] == ["/dig", "/disconnect", "/status", "/connect", "/status"]

        q_state["events"].clear()
        with pytest.raises(ApiError) as exc_info:
            bot_q.call("dig", {"pos": [1, 50, 1]})
        assert exc_info.value.code == "action_timeout"
        assert q_state["dig_calls"] == 3
        assert q_state["quarantined"] is False
        assert q_state["connected"] is True
        assert q_state["events"] == ["/dig", "/disconnect", "/status", "/connect", "/status"]

    # 2 & 3) dig_staircase 侧面藏水不破壁（dug==[]）+ 同格多方向失败记忆彻底消解跨方向死循环
    mine_state: dict[str, Any] = {"dug": []}

    def handle_stair_guard(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [-157.5, 50.0, -383.4], "health": 20, "food": 20, "ready": True, "alive": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "/get_block":
            pos = tuple(int(v) for v in body["pos"])
            if pos in {(-158, 50, -384), (-158, 51, -384)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if pos in {(-160, 49, -384), (-158, 49, -382)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "water"}})
            if pos == (-157, 48, -384):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/dig":
            mine_state["dug"].append(body.get("pos"))
            return httpx.Response(400, json={"ok": False, "error": {"code": "dig_blocked", "message": "cannot break", "pos": body.get("pos")}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_stair_guard)) as bot_s:
        skills = MacroSkills(bot_s)
        # west 前方石墙侧面(-160,49,-384)藏水：未敲任何方块即提前拦停
        west_res = skills.dig_staircase(direction="west", steps=4, vertical="down")
        assert west_res["stop_reason"] == "fluid_or_bedrock"
        assert mine_state["dug"] == []
        assert west_res["blocker"]["block"] == "water"
        assert west_res["blocker"]["relation"] == "adjacent_fluid"
        assert west_res["blocker"]["pos"] == [-160, 49, -384]
        # north 探测正常石墙但 dig_blocked 失败：记入失败方向记忆
        north_res = skills.dig_staircase(direction="north", steps=4, vertical="down")
        assert north_res["stop_reason"] == "dig_failed"
        assert north_res["viable_directions"] == []
        # east 遇 cave_or_drop_ahead 时不再回指刚刚失败的 north，返回 all_directions_blocked + 地下隧道/上退指引
        east_res = skills.dig_staircase(direction="east", steps=4, vertical="down")
        assert east_res["stop_reason"] == "cave_or_drop_ahead"
        assert east_res["viable_directions"] == []
        assert east_res["all_directions_blocked"] is True
        assert east_res["direction_status"]["north"].startswith("failed_recently(")
        assert "water" in east_res["direction_status"]["south"]
        assert "water" in east_res["direction_status"]["west"]
        assert "mc_skill_tunnel" in east_res["hint"]
        assert "vertical='up'" in east_res["hint"]

    # 4) extract_failed_coords 对可换向阶梯标注方向，对 all_directions_blocked 标注整格
    extracted_dir = extract_failed_coords(
        [
            {"role": "assistant", "tool_calls": [{"id": "s1", "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"south"}'}}]},
            {
                "role": "tool",
                "tool_call_id": "s1",
                "content": json.dumps(
                    {
                        "ok": False,
                        "direction": "south",
                        "viable_directions": ["north"],
                        "pos_after": [-157.5, 50.0, -383.4],
                    },
                    ensure_ascii=False,
                ),
            },
            {"role": "assistant", "tool_calls": [{"id": "s2", "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"east"}'}}]},
            {"role": "tool", "tool_call_id": "s2", "content": json.dumps(east_res, ensure_ascii=False)},
        ]
    )
    assert "[-158,50,-384:south]" in extracted_dir
    assert "[-158,50,-384]" in extracted_dir


def test_long_horizon_anti_wedge_context_tunnel_blocker_and_repeat_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) _compact_tool_outcome 带尾随 [期间新事件] 仍精准保留 stop_reason/blocker/viable_directions
    raw_with_events = (
        json.dumps(
            {
                "ok": False,
                "stop_reason": "cave_or_drop_ahead",
                "blocker": {"pos": [-170, 64, -381], "block": "air", "relation": "missing_floor_support"},
                "viable_directions": ["north", "east"],
            },
            ensure_ascii=False,
        )
        + "\n[期间新事件] [受伤 health=17 by mob_attack]"
    )
    compact = _compact_tool_outcome(raw_with_events)
    assert "cave_or_drop_ahead" in compact
    assert "air@[-170,64,-381]" in compact
    assert "viable=north,east" in compact

    # 2) RequestContext(1600, mode="sliding") 不在 1000 字符处误截断 1360 字符工具输出，并折叠连续重复失败
    payload_1360 = json.dumps(
        {
            "ok": False,
            "stop_reason": "cave_or_drop_ahead",
            "blocker": {"pos": [-170, 64, -381], "block": "air"},
            "viable_directions": ["north"],
            "pad": "x" * 1200,
        },
        ensure_ascii=False,
    )
    assert 1250 < len(payload_1360) < 1550
    dup_args = json.dumps({"direction": "south", "steps": 10, "vertical": "down"})
    dup_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 5):
        dup_hist.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"d{i}", "type": "function", "function": {"name": "mc_skill_staircase", "arguments": dup_args}}],
            }
        )
        dup_hist.append({"role": "tool", "tool_call_id": f"d{i}", "content": payload_1360})

    ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4)
    built_dup = ctx.build(dup_hist, record=True)
    assert built_dup[2]["role"] == "user" and built_dup[2]["name"] == "ledger"
    assert "连续重复失败×3已折叠" in built_dup[2]["content"]
    assert "ERR:cave_or_drop_ahead(air@[-170,64,-381],viable=north)" in built_dup[2]["content"]
    assert sum(1 for m in built_dup if m.get("role") == "assistant") == 1
    assert ctx.requests[-1]["truncated_tool_results"] == []
    assert built_dup[-1]["content"] == payload_1360

    # 3) dig_level_tunnel 报错补齐 blocker/hint 且诊断字段排在 inventory_after 之前；dig_staircase 诊断排在 ores_nearby 之前
    class _TunnelMock:
        def __init__(self, overrides: dict[tuple[int, int, int], str], pick: str = "stone_pickaxe") -> None:
            self.overrides = overrides
            self.pick = pick

        def status(self) -> dict[str, Any]:
            return {"pos": [-157.5, 50.0, -383.5], "health": 20, "ready": True, "alive": True, "on_ground": True}

        def inventory(self) -> dict[str, Any]:
            return {"items": [{"name": self.pick, "count": 1}, {"name": "cobblestone", "count": 64}, {"name": "coal", "count": 8}]}

        def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            if action == "get_block":
                pt = tuple(int(v) for v in (body or {})["pos"])
                return {"name": self.overrides.get(pt, "stone")}
            if action in {"equip"}:
                return {}
            raise AssertionError(action)

    tun_unsup = MacroSkills(_TunnelMock({(-158, 50, -385): "oak_planks"})).dig_level_tunnel("north", 4)
    assert tun_unsup["stop_reason"] == "unsupported_face_block"
    assert tun_unsup["blocker"] == {"pos": [-158, 50, -385], "block": "oak_planks", "relation": "unsupported_face_block"}
    assert "oak_planks" in tun_unsup["hint"]
    keys_unsup = list(tun_unsup.keys())
    assert keys_unsup.index("blocker") < keys_unsup.index("inventory_after")
    assert keys_unsup.index("hint") < keys_unsup.index("inventory_after")

    tun_tier = MacroSkills(_TunnelMock({(-158, 50, -385): "diamond_ore"}, pick="stone_pickaxe")).dig_level_tunnel(
        "north", 4, stop_on_ore=False
    )
    assert tun_tier["stop_reason"] == "insufficient_pickaxe_tier"
    assert tun_tier["blocker"]["block"] == "diamond_ore"
    assert tun_tier["blocker"]["required_tier"] == 3

    def handle_stair_order(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-169.5, 66.0, -381.5], "health": 20, "ready": True, "alive": True, "on_ground": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "copper_ore", "pos": [-164, 65, -383], "dist": 5.9}]}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            if pt in {(-170, 66, -382), (-170, 67, -382), (-170, 68, -382), (-170, 64, -381)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_stair_order)) as bot_order:
        stair_res = MacroSkills(bot_order).dig_staircase(direction="south", steps=6, vertical="down")
    stair_keys = list(stair_res.keys())
    assert stair_keys.index("blocker") < stair_keys.index("ores_nearby")
    assert stair_keys.index("viable_directions") < stair_keys.index("ores_nearby")
    assert stair_keys.index("hint") < stair_keys.index("ores_nearby")

    # 4) mc_attack 零命中暴露失败 + GenerationGuard 反射后排空新死亡事件
    def handle_zero_hit(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_sword", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": "stone_sword"}})
        if path == "/attack":
            return httpx.Response(200, json={"ok": True, "result": {"killed": False, "hits": 0, "ms": 5004}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_zero_hit)) as bot_atk:
        atk_out = json.loads(McToolset(bot_atk).mc_attack(entity_id=50790))
    assert atk_out["ok"] is False
    assert atk_out["error"]["code"] == "attack_zero_hits"

    guard_clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: guard_clock[0])
    g_state = {"fled": False, "events_calls": 0}

    def handle_guard_drain(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [-169.5, 73.0, -388.5] if g_state["fled"] else [-155.4, 45.5, -383.7],
                        "health": 20 if g_state["fled"] else 14,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "on_ground": True,
                        "nearest_hostile": None if g_state["fled"] else {"id": 9, "name": "zombie", "dist": 1.8, "pos": [-155.0, 45.5, -382.0]},
                    },
                },
            )
        if action == "events":
            g_state["events_calls"] += 1
            since = int(req.url.params.get("since", 0))
            if g_state["events_calls"] == 1:
                rows = [{"seq": 10, "type": "hurt", "health": 14, "source": "mob_attack", "source_entity": {"id": 9, "name": "zombie", "dist": 1.8}}]
            elif g_state["fled"] and since < 12:
                rows = [{"seq": 11, "type": "death", "pos": [-155.4, 45.5, -383.7]}, {"seq": 12, "type": "spawn", "pos": [-169.5, 73.0, -388.5]}]
            else:
                rows = []
            max_seq = max([since] + [r["seq"] for r in rows])
            return httpx.Response(200, json={"ok": True, "result": {"events": rows, "last_seq": max_seq}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if action == "get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": [] if g_state["fled"] else [{"id": 9, "name": "zombie", "dist": 1.8, "pos": [-155.0, 45.5, -382.0]}]}})
        if action == "get_block":
            by = int((body.get("pos") or [0, 64, 0])[1])
            return httpx.Response(200, json={"ok": True, "result": {"name": "air" if by >= 64 else "stone"}})
        if action in {"control", "move_to"}:
            g_state["fled"] = True
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-169.5, 73.0, -388.5]}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_guard_drain)) as bot_g:
        guard = GenerationGuard(bot_g, EventSink(lambda _: None), checkpoint=lambda: None)
        inc1 = guard.poll()
        guard_clock[0] += 5.0
        inc2 = guard.poll()
    assert inc1 is not None and "death_or_connection_changed" in inc1["reasons"]
    assert guard.cursor == 12
    assert inc2 is None  # 死亡与重生事件已被当场排空，不会重复打断下一轮生成

    # 5) run_mc_agent Harness 零进展重复失败熔断器：同一坐标第 2 次完全相同调用直接短路拦截
    api_calls: list[str] = []
    equip_calls = 0

    def handle_circuit(req: httpx.Request) -> httpx.Response:
        nonlocal equip_calls
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-169.5, 66.0, -381.5], "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            if pt in {(-170, 66, -382), (-170, 67, -382), (-170, 68, -382), (-170, 64, -381)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/equip":
            equip_calls += 1
            api_calls.append(path)
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path in {"/dig", "/move_to"}:
            api_calls.append(path)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    llm_turn = 0
    seen_blocked_payload = False

    def chat_repeat(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal llm_turn, seen_blocked_payload
        llm_turn += 1
        if llm_turn in (1, 2):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"rc{llm_turn}",
                        "type": "function",
                        "function": {
                            "name": "mc_skill_staircase",
                            "arguments": json.dumps({"direction": "south", "steps": 6, "vertical": "down"}),
                        },
                    }
                ],
            }
        assert llm_turn == 3
        seen_blocked_payload = any(
            m.get("role") == "tool" and "repeated_identical_failure_blocked" in str(m.get("content", ""))
            for m in messages
        )
        assert any("[Harness 防卡死提醒]" in str(m.get("content", "")) for m in messages)
        return {"role": "assistant", "content": "已收到防卡死拦截并准备换向。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_circuit)) as bot_c:
        rep_c = run_mc_agent(
            "防卡死测试",
            bot_c,
            chat_fn=chat_repeat,
            max_calls=5,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_c["status"] == "answered"
    assert seen_blocked_payload is True
    # 第 1 次 south 调用执行了 1 次 /equip，第 2 次在 Harness 层直接短路所以 /equip 仍仅调用 1 次
    assert equip_calls == 1


def test_anti_ping_pong_cul_de_sac_probe_and_multi_step_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) 实机双格水封死胡同 [-172,50,-384] <-> [-172,50,-385]：
    #    北面有 1 格既有空气 (-172,50,-385)，其后 (-172,50,-387) 为水；仅南面 (-172,50,-383) 可向上阶梯退回
    pocket_blocks: dict[tuple[int, int, int], str] = {
        (-172, 50, -384): "air",
        (-172, 51, -384): "air",
        (-172, 52, -384): "air",
        (-172, 50, -385): "air",
        (-172, 51, -385): "air",
        # north 阻断（y=53 不妨碍平推进 -385，仅阻断北向上探）
        (-172, 48, -385): "water",
        (-172, 50, -387): "water",
        (-172, 53, -385): "water",
        # east 阻断
        (-171, 49, -384): "water",
        (-171, 49, -385): "water",
        (-171, 52, -384): "water",
        # west 阻断
        (-173, 49, -384): "water",
        (-173, 49, -385): "water",
        (-174, 50, -384): "water",
        (-173, 52, -384): "water",
        # south 仅地板下方水阻塞下探与平推，但 y=50..53 为稳固石头允许 vertical='up'
        (-172, 49, -383): "water",
    }
    pocket_state = {"pos": [-171.5, 50.0, -383.5], "moves": 0}

    def handle_cul_de_sac(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(pocket_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 32}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": pocket_blocks.get(pt, "stone")}})
        if path == "/move_to":
            pocket_state["moves"] += 1
            target = [float(body["x"]), float(body["y"]), float(body["z"])]
            pocket_state["pos"] = target
            return httpx.Response(200, json={"ok": True, "result": {"pos": target}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_cul_de_sac)) as bot_p:
        skills_p = MacroSkills(bot_p)
        stair_p = skills_p.dig_staircase(direction="north", steps=6, vertical="down")
        assert stair_p["stop_reason"] == "fluid_or_bedrock"
        assert stair_p["viable_directions"] == []
        # 北侧虽有 1 格既有空气 (-172,50,-385)，但 2 步隧道预检看穿后面的 water@[-172,50,-387]，不再假阳性指向 north
        assert stair_p["viable_tunnel_directions"] == []
        assert stair_p["viable_up_directions"] == ["south"]
        assert "mc_skill_staircase(direction='south', vertical='up'" in stair_p["hint"]

        # 若直接从 -384 朝 north 平推：空走 1 格空气到达 -385 后被水阻断（dug==[]），起终点双坐标均记入 failed_tunnel_dirs
        tun_p = skills_p.dig_level_tunnel("north", 6)
        assert tun_p["ok"] is False
        assert tun_p["steps_done"] == 1
        assert tun_p["dug"] == []
        assert tun_p["stop_reason"] == "fluid_adjacent"
        assert "north" in skills_p.failed_tunnel_dirs[(-172, 50, -385)]
        assert "north" in skills_p.failed_tunnel_dirs[(-172, 50, -384)]
        assert tun_p["viable_tunnel_directions"] == []
        tkeys = list(tun_p.keys())
        assert tkeys.index("viable_tunnel_directions") < tkeys.index("inventory_after")
        assert tkeys.index("hint") < tkeys.index("inventory_after")

    # 2) 4 步跨坐标乒乓熔断：-384 tunnel(north,steps=6) -> -385 stair(south) -> -385 tunnel(south,steps=4) -> -384 stair(north)
    #    第 5 轮回到 -384 再发 tunnel(north,steps=8)（刻意改变 steps），仍被 _RepeatFailureGuard 当场短路拦截
    ping_state = {"pos": [-171.5, 50.0, -383.5], "moves": 0}
    ping_blocks = dict(pocket_blocks)
    # 为 [-172,50,-385] 周围也补齐地板水体边界使 4 步均能走入旧空气后零掘进失败
    ping_blocks[(-171, 49, -385)] = "water"
    ping_blocks[(-173, 49, -385)] = "water"
    ping_blocks[(-172, 48, -384)] = "water"

    def handle_ping_pong(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(ping_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": ping_blocks.get(pt, "stone")}})
        if path == "/move_to":
            ping_state["moves"] += 1
            target = [float(body["x"]), float(body["y"]), float(body["z"])]
            ping_state["pos"] = target
            return httpx.Response(200, json={"ok": True, "result": {"pos": target}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    cycle_calls = [
        ("mc_skill_tunnel", {"direction": "north", "steps": 6}),
        ("mc_skill_staircase", {"direction": "south", "steps": 6, "vertical": "down"}),
        ("mc_skill_tunnel", {"direction": "south", "steps": 4}),
        ("mc_skill_staircase", {"direction": "north", "steps": 6, "vertical": "down"}),
        ("mc_skill_tunnel", {"direction": "north", "steps": 8}),
    ]
    cycle_turn = 0
    blocked_err_obj: dict[str, Any] = {}
    saw_guard_notice = False

    def chat_ping_pong(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal cycle_turn, blocked_err_obj, saw_guard_notice
        cycle_turn += 1
        if cycle_turn <= len(cycle_calls):
            tname, targs = cycle_calls[cycle_turn - 1]
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"pp{cycle_turn}",
                        "type": "function",
                        "function": {"name": tname, "arguments": json.dumps(targs)},
                    }
                ],
            }
        assert cycle_turn == len(cycle_calls) + 1
        saw_guard_notice = any("[Harness 防卡死提醒]" in str(m.get("content", "")) for m in messages)
        for m in messages:
            if m.get("role") == "tool" and "repeated_identical_failure_blocked" in str(m.get("content", "")):
                parsed_blk = json.loads(str(m["content"]).splitlines()[0])
                blocked_err_obj = parsed_blk.get("error") or {}
        return {"role": "assistant", "content": "已识破四步乒乓死循环并准备改走向上退路。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_ping_pong)) as bot_pp:
        rep_pp = run_mc_agent(
            "四步乒乓熔断测试",
            bot_pp,
            chat_fn=chat_ping_pong,
            max_calls=8,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_pp["status"] == "answered"
    assert saw_guard_notice is True
    assert blocked_err_obj.get("code") == "repeated_identical_failure_blocked"
    assert len(blocked_err_obj.get("nearby_failed_actions") or []) >= 2
    # Turn 1 与 Turn 3 各移动 1 次，Turn 5 在 Harness 层被直接拦截未触发任何 move_to
    assert ping_state["moves"] == 2

    # 3) 滑动上下文对周期性 A -> B -> A -> B 无成功间隔失败段去重并折叠进账本
    cyc_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    cycle_pattern = [
        ("mc_skill_tunnel", {"direction": "north", "steps": 6}, [-171.5, 50.0, -383.5], "fluid_adjacent"),
        ("mc_skill_staircase", {"direction": "south", "steps": 6, "vertical": "down"}, [-171.5, 50.0, -384.5], "fluid_or_bedrock"),
        ("mc_skill_tunnel", {"direction": "north", "steps": 6}, [-171.5, 50.0, -383.5], "fluid_adjacent"),
        ("mc_skill_staircase", {"direction": "south", "steps": 6, "vertical": "down"}, [-171.5, 50.0, -384.5], "fluid_or_bedrock"),
    ]
    for idx, (cname, cargs, cpos, creason) in enumerate(cycle_pattern, start=1):
        cyc_hist.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"cyc{idx}", "type": "function", "function": {"name": cname, "arguments": json.dumps(cargs)}}],
            }
        )
        cyc_hist.append(
            {
                "role": "tool",
                "tool_call_id": f"cyc{idx}",
                "content": json.dumps(
                    {"ok": False, "stop_reason": creason, "pos_before": cpos, "viable_tunnel_directions": [], "viable_up_directions": ["south"]},
                    ensure_ascii=False,
                ),
            }
        )

    cyc_ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4)
    built_cyc = cyc_ctx.build(cyc_hist, record=True)
    assert built_cyc[2]["role"] == "user" and built_cyc[2]["name"] == "ledger"
    assert "连续重复失败×1已折叠" in built_cyc[2]["content"]
    assert "up=south" in built_cyc[2]["content"]
    assert sum(1 for m in built_cyc if m.get("role") == "assistant") == 2


def test_silent_clamping_pillar_up_folding_and_hard_tool_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mc_agent.skills.tunnel.time.sleep", lambda _: None)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) 静默钳位：mc_skill_tunnel(steps=10 / steps=10.0)、mc_chat('/hi' + 'a'*150)，且 bad_args 不污染后续同坐标方向
    clamp_calls: list[tuple[str, Any]] = []
    clamp_state: dict[str, Any] = {"pos": [-171.5, 53.0, -386.5], "blocks": {(-172, 53, -387): "air", (-172, 54, -387): "air"}}

    def handle_clamp(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        clamp_calls.append((path, body))
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(clamp_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 64}]}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": clamp_state["blocks"].get(pt, "stone")}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            clamp_state["blocks"][pt] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
        if path == "/move_to":
            clamp_state["pos"] = [float(body["x"]), float(body["y"]), float(body["z"])]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(clamp_state["pos"])}})
        if path == "/chat":
            return httpx.Response(200, json={"ok": True, "result": {"sent": body.get("message")}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_clamp)) as bot_clamp:
        ts_clamp = McToolset(bot_clamp)
        reg_clamp = ts_clamp.registry(profile="compact")
        prep = reg_clamp.prepare(
            {"id": "p1", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"west","steps":10.0}'}}
        )
        assert not isinstance(prep, str)
        assert type(prep.arguments["steps"]) is int and prep.arguments["steps"] == 10
        tun_clamped = json.loads(prep.invoke())
        assert tun_clamped["ok"] is True
        assert tun_clamped["steps_requested"] == 8
        assert tun_clamped["steps_done"] == 8

        chat_out = json.loads(ts_clamp.mc_chat("/hi" + "a" * 150))
        assert chat_out["ok"] is True
        sent_msg = [b["message"] for p, b in clamp_calls if p == "/chat"][-1]
        assert not sent_msg.startswith("/") and len(sent_msg) == 100

    # bad_args 不入黑名单 + extract_failed_coords 跳过 bad_args 并对 tunnel 带方向标注
    bad_turn = 0
    clamp_state["pos"] = [-171.5, 53.0, -386.5]

    def chat_bad_then_valid(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal bad_turn
        bad_turn += 1
        if bad_turn == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "b1", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"invalid","steps":8}'}}
                ],
            }
        if bad_turn == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "b2", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"west","steps":8}'}}
                ],
            }
        assert json.loads(messages[-1]["content"].splitlines()[0])["ok"] is True
        return {"role": "assistant", "content": "bad_args 未污染有效方向，已成功西进。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_clamp)) as bot_bad:
        rep_bad = run_mc_agent(
            "bad_args 豁免测试",
            bot_bad,
            chat_fn=chat_bad_then_valid,
            max_calls=4,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_bad["status"] == "answered"

    extracted_tunnel = extract_failed_coords(
        [
            {"role": "assistant", "tool_calls": [{"id": "e1", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"invalid","steps":8}'}}]},
            {"role": "tool", "tool_call_id": "e1", "content": json.dumps({"ok": False, "error": "bad_args", "pos": [-171.5, 53, -386.5]})},
            {"role": "assistant", "tool_calls": [{"id": "e2", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"south","steps":8}'}}]},
            {
                "role": "tool",
                "tool_call_id": "e2",
                "content": json.dumps(
                    {
                        "ok": False,
                        "direction": "south",
                        "stop_reason": "unsafe_or_unknown_floor",
                        "viable_tunnel_directions": ["west"],
                        "viable_up_directions": ["west"],
                        "pos_after": [-171.5, 53.0, -386.5],
                    }
                ),
            },
        ]
    )
    assert extracted_tunnel == ["[-172,53,-387:south]"]

    # 2) 四向水平与斜向阶梯全水封时探测 viable_pillar_up=True，且 vertical='pillar' 完成挖顶跳跃垫脚直上
    #    3) 同时验证连续 3 次重复失败触发硬工具屏蔽，改走 pillar 升层后原工具在下一轮 Schema 中恢复
    pillar_blocks: dict[tuple[int, int, int], str] = {
        (-172, 53, -387): "air",
        (-172, 54, -387): "air",
        # 四周 y=54 水平头高处有水，同时封死 4 向 tunnel 与 4 向斜向上阶梯；南面 y=52 地板为空
        (-172, 54, -388): "water",
        (-172, 54, -386): "water",
        (-171, 54, -387): "water",
        (-173, 54, -387): "water",
        (-172, 52, -386): "air",
    }
    pillar_state: dict[str, Any] = {
        "pos": [-171.5, 53.0, -386.5],
        "jump": False,
        "cobblestone": 32,
        "dug_overhead": 0,
        "blocked_by_self_hits": 0,
        "posture_calls": [],
    }

    def handle_pillar(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(pillar_state["pos"]), "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": not pillar_state["jump"]}},
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": pillar_state["cobblestone"]}]}},
            )
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": pillar_blocks.get(pt, "stone")}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            pillar_blocks[pt] = "air"
            pillar_state["dug_overhead"] += 1
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
        if path == "/posture":
            pillar_state["jump"] = bool(body.get("jump"))
            pillar_state["posture_calls"].append(bool(body.get("jump")))
            return httpx.Response(200, json={"ok": True, "result": {"jump": pillar_state["jump"]}})
        if path == "/place":
            pt = tuple(int(v) for v in body["pos"])
            cur_y = float(pillar_state["pos"][1])
            if pillar_state["jump"] and math.floor(cur_y) == pt[1]:
                pillar_state["blocked_by_self_hits"] += 1
                pillar_state["pos"][1] = float(pt[1]) + 1.15
                return httpx.Response(400, json={"ok": False, "error": {"code": "blocked_by_self", "message": "bot is standing in target cell"}})
            pillar_blocks[pt] = str(body.get("item", "cobblestone"))
            pillar_state["cobblestone"] -= 1
            pillar_state["pos"][1] = float(pt[1]) + 1.0
            return httpx.Response(200, json={"ok": True, "result": {"placed": body.get("item"), "pos": list(pt)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_pillar)) as bot_pil:
        skills_pil = MacroSkills(bot_pil)
        dead_tun = skills_pil.dig_level_tunnel("south", 6)
        assert dead_tun["ok"] is False
        assert dead_tun["viable_tunnel_directions"] == []
        assert dead_tun["viable_up_directions"] == []
        assert dead_tun["viable_pillar_up"] is True
        assert "vertical='pillar'" in dead_tun["hint"]

        up_res = skills_pil.dig_staircase(direction="pillar", steps=3, vertical="pillar")
        assert up_res["ok"] is True
        assert up_res["steps_done"] == 3
        assert up_res["placed_count"] == 3
        assert up_res["dug_count"] >= 3
        assert math.floor(float(up_res["pos_after"][1])) == 56
        assert pillar_state["blocked_by_self_hits"] == 3
        assert pillar_state["jump"] is False

    # 重置在 [-172, 53, -387] 测试：连续 3 次同向 tunnel 后硬屏蔽 mc_skill_tunnel，改走 pillar 升 2 格后自动解封，且历史被折叠为 1 轮
    pillar_blocks.clear()
    pillar_blocks.update(
        {
            (-172, 53, -387): "air",
            (-172, 54, -387): "air",
            (-172, 54, -388): "water",
            (-172, 54, -386): "water",
            (-171, 54, -387): "water",
            (-173, 54, -387): "water",
            (-172, 52, -386): "air",
        }
    )
    pillar_state["pos"] = [-171.5, 53.0, -386.5]
    pillar_state["jump"] = False
    pillar_state["cobblestone"] = 32
    seen_tool_sets: list[set[str]] = []
    mask_turn = 0

    def chat_mask_recovery(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal mask_turn
        mask_turn += 1
        tool_names = {t["function"]["name"] for t in (tools or [])}
        seen_tool_sets.append(tool_names)
        if mask_turn <= 3:
            assert "mc_skill_tunnel" in tool_names
            return {
                "role": "assistant",
                "content": "意图：向南平推。",
                "tool_calls": [
                    {"id": f"m{mask_turn}", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"south","steps":8}'}}
                ],
            }
        if mask_turn == 4:
            # 第 3 次连续零进展后，第 4 轮请求必须将 mc_skill_tunnel 从工具 Schema 中临时屏蔽，且前 3 轮已被折叠为 1 轮活跃记录
            assert "mc_skill_tunnel" not in tool_names
            assert "mc_skill_staircase" in tool_names
            assert sum(1 for m in messages if m.get("role") == "assistant") == 1
            assert any(m.get("name") == "ledger" and "连续重复失败×2已折叠" in str(m.get("content", "")) for m in messages)
            return {
                "role": "assistant",
                "content": "意图：垂直垫脚直上脱困。",
                "tool_calls": [
                    {"id": "m4", "type": "function", "function": {"name": "mc_skill_staircase", "arguments": '{"vertical":"pillar","steps":2}'}}
                ],
            }
        assert mask_turn == 5
        assert "mc_skill_tunnel" in tool_names
        return {"role": "assistant", "content": "已通过原地垫脚升层并解除工具屏蔽。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_pillar)) as bot_mask:
        rep_mask = run_mc_agent(
            "防复读工具屏蔽与上下文折叠测试",
            bot_mask,
            chat_fn=chat_mask_recovery,
            max_calls=7,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_mask["status"] == "answered"
    assert len(seen_tool_sets) == 5
    assert math.floor(float(pillar_state["pos"][1])) == 55
    # 验证不含独立的 role=user [Harness 防卡死提醒] 消息，且内联提醒允许 6 连重复失败完全折叠进账本
    fold_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 7):
        fold_hist.append(
            {
                "role": "assistant",
                "content": "意图：向南平推。",
                "tool_calls": [{"id": f"f{i}", "type": "function", "function": {"name": "mc_skill_tunnel", "arguments": '{"direction":"south","steps":8}'}}],
            }
        )
        t_txt = json.dumps({"ok": False, "stop_reason": "unsafe_or_unknown_floor", "pos_before": [-172, 53, -387]})
        if i >= 2:
            t_txt += f"\n[Harness 防卡死提醒] 第 {i} 次拦截"
        fold_hist.append({"role": "tool", "tool_call_id": f"f{i}", "content": t_txt})
    built_fold = RequestContext(3200, mode="sliding", keep_recent_turns=6).build(fold_hist, record=True)
    assert sum(1 for m in built_fold if m.get("role") == "assistant") == 1
    assert any(m.get("name") == "ledger" and "连续重复失败×5已折叠" in str(m.get("content", "")) for m in built_fold)


def test_process_request_exit_race_drains_inbox_and_transient_llm_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import queue as _stdlib_queue
    import time as _stdlib_time
    import mc_agent.harness.model as model_mod

    RealSimpleQueue = _stdlib_queue.SimpleQueue

    class _RaceQueue:
        def __init__(self, expected_puts: int) -> None:
            self._inner = RealSimpleQueue()
            self._puts = 0
            self._expected_puts = expected_puts
            self._injected_empty = False

        def put(self, item: Any) -> None:
            self._inner.put(item)
            self._puts += 1

        def get(self, timeout: float | None = None) -> Any:
            if not self._injected_empty:
                self._injected_empty = True
                deadline = _stdlib_time.monotonic() + 1.0
                while self._puts < self._expected_puts and _stdlib_time.monotonic() < deadline:
                    _stdlib_time.sleep(0.002)
                _stdlib_time.sleep(0.02)
                raise _stdlib_queue.Empty
            return self._inner.get(timeout=timeout)

        def get_nowait(self) -> Any:
            return self._inner.get_nowait()

    # 子测试 A1：worker 投递末尾 event 与 result 并退出，首次 get(timeout) 恰好抛 Empty 时仍完整排空并返回 result
    race_mode = {"step": "ok"}

    def fake_chat_direct(spec, messages, tools=None, *, stream=False, on_event=None, client=None, cancel_event=None):
        if on_event is not None:
            on_event({"type": "tool.delta", "index": 0, "id": "c1", "name": "mc_status", "arguments": "{}"})
        if race_mode["step"] == "err":
            raise RuntimeError("模型流中断：缺少完整结束标记 [DONE] 或 finish_reason。")
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(model_mod, "_chat_direct", fake_chat_direct)
    monkeypatch.setattr(model_mod.queue, "SimpleQueue", lambda: _RaceQueue(expected_puts=2))

    seen_events: list[dict[str, Any]] = []
    res_msg = _process_request(
        QwenSpec(),
        [{"role": "user", "content": "hi"}],
        [],
        True,
        on_event=seen_events.append,
        control=lambda: False,
    )
    assert res_msg == {"role": "assistant", "content": "done"}
    assert any(e.get("type") == "tool.delta" and e.get("name") == "mc_status" for e in seen_events)

    # 子测试 A2：worker 投递真实流中断 error_exc 并退出，首次 get(timeout) 抛 Empty 时仍保留真实异常而非“线程意外退出”
    race_mode["step"] = "err"
    with pytest.raises(RuntimeError, match="模型流中断"):
        _process_request(
            QwenSpec(),
            [{"role": "user", "content": "hi"}],
            [],
            True,
            on_event=None,
            control=lambda: False,
        )

    # 子测试 B：局内瞬时 SSE 断流自动重连（退避且退还消耗额度），非瞬时错误直接暴露 runtime_error
    monkeypatch.setattr("mc_agent.harness.agent.time.sleep", lambda _s: None)

    def handle_bot(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        raise AssertionError(path)

    class _DummyRetryChat:
        def __init__(self) -> None:
            self.calls = 0
            self.rotated = 0

        def _abort_and_rotate_client(self) -> None:
            self.rotated += 1

        def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "查状态",
                    "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "mc_status", "arguments": "{}"}}],
                }
            if self.calls == 2:
                raise RuntimeError("模型流中断：缺少完整结束标记 [DONE] 或 finish_reason。")
            return {"role": "assistant", "content": "恢复成功"}

    dummy = _DummyRetryChat()
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_bot)) as bot_ok:
        rep_ok = run_mc_agent(
            "测试局内流断恢复",
            bot_ok,
            chat_fn=dummy.chat,
            max_calls=2,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_ok["status"] == "answered"
    assert rep_ok["answer"] == "恢复成功"
    assert dummy.calls == 3
    assert dummy.rotated == 1
    assert any(e.get("type") == "model.transient_retry" for e in rep_ok["events"])

    class _DummyFatalChat:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            raise RuntimeError("致命参数结构损坏")

    fatal = _DummyFatalChat()
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_bot)) as bot_fatal:
        rep_fatal = run_mc_agent(
            "测试非瞬时错误直透",
            bot_fatal,
            chat_fn=fatal.chat,
            max_calls=3,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_fatal["status"] == "runtime_error"
    assert "致命参数结构损坏" in str(rep_fatal.get("error"))
    assert fatal.calls == 1


def test_underground_wood_shortage_and_coordinate_drift_stagnation_masking() -> None:
    from mc_agent.harness.context import RequestContext
    from mc_agent.harness.hud import compute_goal_gap_hint
    from mc_agent.skills.library import MacroSkills

    # 1) 地下 y=29 且圆石 533、原木/木板/木棍为 0 时，缺口提示不得再误报“还差圆石0个”
    gap_under = compute_goal_gap_hint(
        {"wooden_pickaxe": 1, "cobblestone": 533},
        task="继续推进生存与地下采矿，收集铁矿石并熔炼合成装备 iron_pickaxe。",
        bot_y=29,
    )
    assert "还差圆石0个" not in gap_under
    assert "地下(y=29)无树" in gap_under
    assert "mc_skill_staircase" in gap_under

    # 2) harvest_logs 在地下 y=29 返回 no_targets_available 时明确指出地下无树并给出 viable_up_directions
    state: dict[str, Any] = {
        "pos": [-172.3, 29.0, -334.3],
        "dig_round": 0,
    }

    def handle_world(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 20, "food": 20, "ready": True, "in_water": False}},
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "held": {"name": "wooden_pickaxe", "count": 1, "durability_used": 39},
                        "items": [
                            {"name": "wooden_pickaxe", "count": 1, "durability_used": 39},
                            {"name": "cobblestone", "count": 533},
                        ],
                    },
                },
            )
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if path == "/get_block":
            bpos = body.get("pos") or [0, 0, 0]
            bx, by, bz = int(bpos[0]), int(bpos[1]), int(bpos[2])
            if (bx, bz) == (math.floor(state["pos"][0]), math.floor(state["pos"][2])) and by in (29, 30):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path == "/dig":
            state["dig_round"] += 1
            if state["dig_round"] <= 3:
                jitter_pos = (
                    [-172.4, 29.0, -335.4]
                    if state["dig_round"] < 3
                    else [-172.3, 29.0, -334.5]
                )
                state["pos"] = list(jitter_pos)
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error": {
                            "code": "out_of_reach",
                            "message": f"cannot reach stone at {body.get('pos')}",
                            "pos": jitter_pos,
                        },
                    },
                )
            # staircase 上行时逐块掘进成功
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {"cobblestone": 1}}})
        if path == "/move_to":
            state["pos"] = [float(body.get("x", state["pos"][0])), float(body.get("y", state["pos"][1])), float(body.get("z", state["pos"][2]))]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"])}})
        raise AssertionError(f"unexpected path: {path}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_world)) as bot_logs:
        h_res = MacroSkills(bot_logs).harvest_logs(target_logs=4, radius=10)
    assert h_res["ok"] is False
    assert h_res.get("underground_no_trees") is True
    assert h_res.get("viable_up_directions")
    assert "mc_skill_staircase" in str(h_res.get("message"))
    assert "严禁" in str(h_res.get("message"))

    # 3) 连续 3 轮沿不同坐标递推调用 mc_dig(out_of_reach) 且伴随亚格微漂移，第 4 轮必须硬屏蔽 mc_dig 并折叠历史
    state["pos"] = [-172.4, 29.0, -335.4]
    state["dig_round"] = 0
    seen_tools: list[set[str]] = []

    def chat_drift(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        req_no = len(seen_tools) + 1
        tool_names = {
            str(((t.get("function") or {}).get("name") or ""))
            for t in (tools or [])
            if isinstance(t, dict)
        }
        seen_tools.append(tool_names)
        if req_no <= 3:
            assert "mc_dig" in tool_names
            tx = -176 - req_no
            tz = -339 - req_no
            return {
                "role": "assistant",
                "content": f"意图：向西南盲挖寻找原木 {req_no}",
                "tool_calls": [
                    {
                        "id": f"drift_{req_no}",
                        "type": "function",
                        "function": {"name": "mc_dig", "arguments": json.dumps({"x": tx, "y": 29, "z": tz, "collect": True})},
                    }
                ],
            }
        if req_no == 4:
            assert "mc_dig" not in tool_names
            assert "mc_skill_staircase" in tool_names
            assert sum(1 for m in messages if m.get("role") == "assistant") == 1
            assert any(m.get("name") == "ledger" and "连续重复失败×2已折叠" in str(m.get("content", "")) for m in messages)
            assert any("严禁向更远坐标连续递推调用 mc_dig" in str(m.get("content", "")) for m in messages if m.get("role") == "tool")
            assert any("[Harness 防卡死提醒]" in str(m.get("content", "")) for m in messages if m.get("role") == "tool")
            return {
                "role": "assistant",
                "content": "意图：改用阶梯上行回地表。",
                "tool_calls": [
                    {
                        "id": "escape_up",
                        "type": "function",
                        "function": {"name": "mc_skill_staircase", "arguments": '{"direction":"north","vertical":"up","steps":3}'},
                    }
                ],
            }
        assert req_no == 5
        assert "mc_dig" in tool_names
        return {"role": "assistant", "content": "已止损递推盲挖并升层脱困。"}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handle_world)) as bot_drift:
        rep_drift = run_mc_agent(
            "收集铁矿石并合成 iron_pickaxe",
            bot_drift,
            chat_fn=chat_drift,
            max_calls=6,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep_drift["status"] == "answered"
    assert len(seen_tools) == 5

    # 4) 验证 path_not_found 坐标序列漂移同样被 sliding 上下文完全折叠
    pnf_hist: list[dict[str, Any]] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 5):
        pnf_hist.append(
            {
                "role": "assistant",
                "content": "继续西南挖",
                "tool_calls": [
                    {
                        "id": f"pnf_{i}",
                        "type": "function",
                        "function": {"name": "mc_dig", "arguments": json.dumps({"x": -193 - i, "y": 29, "z": -356 - i, "collect": True})},
                    }
                ],
            }
        )
        pnf_hist.append(
            {
                "role": "tool",
                "tool_call_id": f"pnf_{i}",
                "content": json.dumps(
                    {
                        "ok": False,
                        "error": {"code": "path_not_found", "from": [-172.3 - 0.1 * i, 29.0, -334.5]},
                    }
                ),
            }
        )
    built_pnf = RequestContext(3200, mode="sliding", keep_recent_turns=6).build(pnf_hist, record=True)
    assert sum(1 for m in built_pnf if m.get("role") == "assistant") == 1
    assert any(m.get("name") == "ledger" and "连续重复失败×3已折叠" in str(m.get("content", "")) for m in built_pnf)


def test_selective_thinking_dedup_extra_tools_shiny_gap_and_trunk_canopy(monkeypatch: pytest.MonkeyPatch) -> None:
    from mc_agent.harness.agent import _infer_extra_tools_for_task, _should_think_this_turn
    from mc_agent.harness.context import RequestContext, _compact_tool_outcome
    from mc_agent.harness.hud import compute_goal_gap_hint
    from mc_agent.skills.library import MacroSkills

    # 1) _should_think_this_turn opens thinking on Req1/fail/milestone/every-12 and closes on routine progress
    assert _should_think_this_turn(1, task_changed=False, had_incident=False, force_no_think=False, messages=[]) is True
    assert _should_think_this_turn(3, task_changed=False, had_incident=True, force_no_think=False, messages=[]) is True
    assert _should_think_this_turn(12, task_changed=False, had_incident=False, force_no_think=False, messages=[]) is True
    assert _should_think_this_turn(1, task_changed=True, had_incident=True, force_no_think=True, messages=[]) is False

    routine_msgs = [
        {"role": "assistant", "content": "下探一步", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": json.dumps({"ok": True, "steps_done": 5, "stop_reason": "completed"})},
    ]
    assert _should_think_this_turn(2, task_changed=False, had_incident=False, force_no_think=False, messages=routine_msgs) is False

    fail_msgs = [
        {"role": "assistant", "content": "合成", "tool_calls": [{"id": "t2"}]},
        {"role": "tool", "tool_call_id": "t2", "content": json.dumps({"ok": False, "error": "missing_ingredients"})},
    ]
    assert _should_think_this_turn(2, task_changed=False, had_incident=False, force_no_think=False, messages=fail_msgs) is True

    smelt_msgs = [
        {"role": "assistant", "content": "熔炼", "tool_calls": [{"id": "t3"}]},
        {"role": "tool", "tool_call_id": "t3", "content": json.dumps({"ok": True, "collected": [{"name": "iron_ingot", "count": 1}]})},
    ]
    assert _should_think_this_turn(2, task_changed=False, had_incident=False, force_no_think=False, messages=smelt_msgs) is True
    assert _compact_tool_outcome(smelt_msgs[-1]["content"]) == "ok iron_ingot+1"

    # 2) RequestContext strips reasoning_content from history and dedups repeated assistant slogans in sliding window
    ctx_hist = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "TASK"},
    ]
    for i in range(1, 5):
        ctx_hist.append(
            {
                "role": "assistant",
                "content": "重复口号！",
                "reasoning_content": f"内部思考_{i}",
                "tool_calls": [{"id": f"d{i}", "type": "function", "function": {"name": "mc_dig", "arguments": json.dumps({"x": i, "y": 64, "z": 0})}}],
            }
        )
        ctx_hist.append(
            {
                "role": "tool",
                "tool_call_id": f"d{i}",
                "content": json.dumps({"ok": True, "gained": {"stone": 1}}),
            }
        )
    built_ctx = RequestContext(1600, mode="sliding", keep_recent_turns=4).build(ctx_hist)
    assert all("reasoning_content" not in m for m in built_ctx)
    asst_texts = [m["content"] for m in built_ctx if m.get("role") == "assistant"]
    assert asst_texts == ["", "", "", "重复口号！"]
    assert all("reasoning_content" in m for m in ctx_hist if m.get("role") == "assistant")

    # 3) _infer_extra_tools_for_task keeps shiny_gear concise (0 extra tools => 19 compact core tools)
    clean_task = "自主推进长程生存，达成「钻石护体」(story/shiny_gear) 成就，全程保持存活。"
    assert _infer_extra_tools_for_task(clean_task) == set()

    # 4) shiny_gear compute_goal_gap_hint preserves original concise hint without spoon-feeding steps
    gap_no_iron = compute_goal_gap_hint(
        {"stone_pickaxe": 1, "iron_ingot": 1, "coal": 8},
        task=clean_task,
        bot_y=70,
    )
    assert "diamond_boots" in gap_no_iron
    assert "iron_pickaxe" in gap_no_iron

    # 5) descend_canopy cuts through leaves standing directly above a tree trunk (*_log)
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    trunk_blocks = {
        (-156, 77, -383): "birch_leaves",
        (-156, 76, -383): "birch_log",
        (-156, 75, -383): "birch_log",
        (-156, 74, -383): "grass_block",
    }
    trunk_pos = [-155.5, 78.0, -382.5]

    def trunk_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(trunk_pos), "in_water": False}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            bname = trunk_blocks.get(pt, "air")
            return httpx.Response(200, json={"ok": True, "result": {"name": bname, "diggable": bname != "air"}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            dug_blk = trunk_blocks.pop(pt, "air")
            trunk_pos[1] = float(pt[1])
            return httpx.Response(200, json={"ok": True, "result": {"dug": dug_blk}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(trunk_handler)) as bot_trunk:
        canopy_res = MacroSkills(bot_trunk).descend_canopy()
    assert canopy_res["ok"] is True
    assert canopy_res["on_canopy"] is True
    assert len(canopy_res["dug"]) == 3
    assert trunk_pos[1] == 75.0


def test_early_run_friction_fixes_max_tokens_stick_mine_dedup_and_mature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.model import QwenModel, QwenSpec

    # 1) QwenModel.chat sets max_tokens >= 4096 when enable_thinking=True
    seen_payloads: list[dict[str, Any]] = []

    def model_handler(req: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    env_p = tmp_path / ".env.test"
    env_p.write_text("NJU_API_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.setattr(
        "mc_agent.harness.model._make_http_client",
        lambda *_args, **_kw: httpx.Client(transport=httpx.MockTransport(model_handler)),
    )
    spec = QwenSpec(dotenv_path=str(env_p), fallback_dotenv_path=None, max_tokens=1024, enable_thinking=False)
    model = QwenModel(spec)
    model.chat([{"role": "user", "content": "hi"}], stream=False, enable_thinking=True, control=lambda: False)
    model.chat([{"role": "user", "content": "hi2"}], stream=False, enable_thinking=False, control=lambda: False)
    assert seen_payloads[0]["max_tokens"] == 4096
    assert seen_payloads[1]["max_tokens"] == 1024

    # 2) _RepeatFailureGuard._canonical_sig_args deduplicates mine_blocks across count/radius tweaks
    s1 = _RepeatFailureGuard._canonical_sig_args("mc_skill_mine_blocks", {"block_types": ["iron_ore", "coal_ore"], "count": 10, "radius": 16})
    s2 = _RepeatFailureGuard._canonical_sig_args("mc_skill_mine_blocks", {"block_types": ["coal_ore", "iron_ore"], "count": 2, "radius": 32})
    assert s1 == s2

    # 3) craft_gear("stick") converts oak_log -> oak_planks -> stick; empty inventory returns missing_ingredients
    inv_items: list[dict[str, Any]] = [{"name": "oak_log", "count": 2, "slot": 9}]
    crafted_calls: list[tuple[str, int]] = []

    def craft_handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(inv_items), "empty_slots": 35 - len(inv_items)}})
        if action == "craft":
            item_name = str(body["item"])
            cnt = int(body["count"])
            crafted_calls.append((item_name, cnt))
            if item_name == "oak_planks":
                inv_items[:] = [{"name": "oak_planks", "count": cnt, "slot": 9}]
            elif item_name == "stick":
                inv_items[:] = [{"name": "stick", "count": cnt, "slot": 9}]
            return httpx.Response(200, json={"ok": True, "result": {"crafted": item_name, "count": cnt}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(craft_handler)) as bot_craft:
        skills_craft = MacroSkills(bot_craft)
        stick_ok = skills_craft.craft_gear("stick", count=4)
        assert stick_ok["ok"] is True
        assert ("oak_planks", 4) in crafted_calls
        assert ("stick", 4) in crafted_calls

        inv_items.clear()
        stick_miss = skills_craft.craft_gear("stick", count=4)
        assert stick_miss["ok"] is False
        assert stick_miss["error"] == "missing_ingredients"
        assert "planks_or_log" in stick_miss["missing_materials"]

    # 4) mine_blocks 0-target message + mc_find_blocks dropping mature on non-crops
    find_payloads: list[dict[str, Any]] = []

    def find_handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 40.0, 0.5], "dimension": "overworld", "biome": "plains", "health": 20}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1, "slot": 9}], "empty_slots": 35}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": "iron_pickaxe"}}})
        if action == "find_blocks":
            find_payloads.append(dict(body))
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if action == "get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone", "diggable": True}})
        raise AssertionError(action)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(find_handler)) as bot_find:
        skills_find = MacroSkills(bot_find)
        zero_res = skills_find.mine_blocks(["iron_ore"], count=5, radius=12)
        assert zero_res["ok"] is False
        assert zero_res["error"] == "no_targets_available"
        assert "未扫描到任何匹配目标方块" in zero_res["message"]

        ts_find = McToolset(bot_find)
        ts_find.mc_find_blocks(radius=12, types=["iron_ore"], mature=True)
        ts_find.mc_find_blocks(radius=12, types=["wheat"], mature=True)
    assert "mature" not in find_payloads[-2]
    assert find_payloads[-1]["mature"] is True


def test_death_loop_cooldown_recovery_guard_status_and_api_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_agent.errors import ApiError
    from mc_agent.harness.agent import run_mc_agent
    from mc_agent.harness.events import EventSink
    from mc_agent.harness.guardian import GenerationGuard

    clock = [500.0]
    monkeypatch.setattr("mc_agent.bot.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.bot.time.sleep", lambda dt: clock.__setitem__(0, clock[0] + float(dt)))
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    # 1) GenerationGuard.poll() degrades cleanly when post_status() hits not_connected after reflex
    guard_calls = {"status": 0}

    def guard_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            guard_calls["status"] += 1
            if guard_calls["status"] == 1:
                return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 10.0, "food": 20, "ready": True, "alive": True, "on_ground": True}})
            return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "kicked"}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 2, "events": [{"seq": 2, "type": "hurt", "health": 10.0, "source": "arrow", "source_entity": {"id": 99}}]}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "shield", "count": 1}]}})
        if path in ("/look", "/posture"):
            return httpx.Response(200, json={"ok": True, "result": {"blocking": True}})
        raise AssertionError(path)

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(guard_handler)) as bot_g:
        bot_g._last_event_seq = 1
        guard = GenerationGuard(bot_g, EventSink(lambda _: None), checkpoint=lambda: None)
        inc = guard.poll()
    assert inc is not None
    assert inc["status"]["ready"] is False
    assert "death_or_connection_changed" in inc["reasons"]
    assert inc["requires_replan"] is True

    # 2) run_mc_agent auto-waits death_loop_cooldown and recovers without aborting the run
    cd_state = {"kicked": True, "connects": 0}

    def cd_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if path == "/connect":
            cd_state["connects"] += 1
            if cd_state["connects"] == 1:
                return httpx.Response(200, json={"ok": False, "error": {"code": "death_loop_cooldown", "message": "cooldown", "cooldown_remaining_seconds": 3}})
            cd_state["kicked"] = False
            return httpx.Response(200, json={"ok": True, "result": {"connected": True}})
        if path == "/status":
            if cd_state["kicked"]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "bot not connected"}})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True}})
        if path == "/inventory":
            if cd_state["kicked"]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "bot not connected"}})
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1}], "empty_slots": 35}})
        raise AssertionError(path)

    cd_turns = 0

    def cd_chat(messages, tools=None, **kwargs):
        nonlocal cd_turns
        cd_turns += 1
        if cd_turns == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "mc_inventory", "arguments": "{}"}}],
            }
        last_tool = next(m for m in reversed(messages) if m.get("role") == "tool")
        assert '"iron_pickaxe"' in last_tool["content"]
        return {"role": "assistant", "content": "冷却重连恢复，继续任务。"}

    cd_events: list[dict[str, Any]] = []
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(cd_handler)) as bot_cd:
        rep = run_mc_agent(
            "test cooldown",
            bot_cd,
            chat_fn=cd_chat,
            max_calls=4,
            survival_guard=False,
            include_initial_snapshot=False,
            on_event=cd_events.append,
        )
    assert rep["status"] == "answered"
    assert cd_state["connects"] == 2
    assert any(e["type"] == "bot.cooldown_wait" and e["reason"] == "death_loop_cooldown" for e in cd_events)

    # 3) Direct ApiError escaping run_mc_agent sets runtime_error in run.finished instead of leaving "running"
    err_events: list[dict[str, Any]] = []
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(cd_handler)) as bot_err:
        rep_err = run_mc_agent(
            "fail api",
            bot_err,
            chat_fn=lambda *a, **kw: (_ for _ in ()).throw(ApiError("not_connected", "boom")),
            max_calls=2,
            survival_guard=False,
            include_initial_snapshot=False,
            on_event=err_events.append,
        )
    assert rep_err["status"] == "runtime_error"
    finished_ev = [e for e in err_events if e["type"] == "run.finished"][-1]
    assert finished_ev["status"] == "runtime_error"


def test_target_drop_verification_cobble_cap_turn_away_and_jump_pickup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    base_items = (
        [{"name": "iron_pickaxe", "count": 1, "slot": 9}]
        + [{"name": "cobblestone", "count": 64, "slot": 10 + i} for i in range(14)]
        + [{"name": "cobblestone", "count": 4, "slot": 24}]
        + [{"name": "stone_sword", "count": 1, "slot": 25 + i} for i in range(15)]
    )
    state_a: dict[str, Any] = {
        "items": list(base_items),
        "actions": [],
        "drops": [],
        "moves": [],
        "controls": [],
        "pickup_raw_iron": False,
        "entities": [
            {"id": 10, "item": "dirtx64", "pos": [0.5, 40.0, 0.5], "dist": 0.2},
            {"id": 11, "item": "cobblestonex64", "pos": [0.5, 40.0, 0.5], "dist": 0.3},
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        state_a["actions"].append((action, body))
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 40.0, 0.5], "yaw": 0.0, "in_water": False, "alive": True}})
        if action == "inventory":
            items = list(state_a["items"])
            return httpx.Response(200, json={"ok": True, "result": {"items": items, "empty_slots": max(0, 36 - len(items))}})
        if action == "look":
            return httpx.Response(200, json={"ok": True, "result": {"yaw": body["yaw"]}})
        if action == "drop":
            dname = body["item"]
            dcnt = int(body["count"])
            state_a["drops"].append((dname, dcnt))
            rem = dcnt
            new_items = []
            for row in state_a["items"]:
                if row["name"] == dname and rem > 0:
                    take = min(rem, int(row["count"]))
                    rem -= take
                    left = int(row["count"]) - take
                    if left > 0:
                        new_items.append({**row, "count": left})
                else:
                    new_items.append(row)
            state_a["items"] = new_items
            return httpx.Response(200, json={"ok": True, "result": {"dropped": dname, "count": dcnt}})
        if action == "equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if action == "find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": [2, 41, 0], "dist": 2.0}]}})
        if action == "dig":
            return httpx.Response(200, json={"ok": True, "result": {"dug": "iron_ore", "gained": {"dirt": 1}}})
        if action == "get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": list(state_a["entities"])}})
        if action == "move_to":
            state_a["moves"].append(dict(body))
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 40.0, 0.5]}})
        if action == "control":
            state_a["controls"].append(dict(body))
            if state_a["pickup_raw_iron"] and body.get("forward") and body.get("jump"):
                state_a["items"].append({"name": "raw_iron", "count": 1, "slot": 20})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1.5, 41.0, 0.5]}})
        raise AssertionError(action)

    # A) Cobble capped <=192 after turning away (look yaw=pi); incidental dirt does NOT yield ok=True
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_mine:
        skills_mine = MacroSkills(bot_mine)
        res_a = skills_mine.mine_blocks(["iron_ore"], count=1, radius=16)

    look_idx = next(i for i, (act, _) in enumerate(state_a["actions"]) if act == "look")
    drop_idx = next(i for i, (act, _) in enumerate(state_a["actions"]) if act == "drop")
    assert look_idx < drop_idx
    assert abs(state_a["actions"][look_idx][1]["yaw"] - math.pi) < 1e-4
    cobble_left = sum(int(r["count"]) for r in state_a["items"] if r["name"] == "cobblestone")
    assert cobble_left <= 192
    assert state_a["moves"] == []  # Auto-dropped clutter dirt/cobblestone at feet were skipped
    assert res_a["ok"] is False
    assert res_a["error"] == "no_drops_collected"
    assert res_a["incidental_gained"] == {"dirt": 1}

    # B) Elevated raw_iron in 1x1 alcove (dy=+1.2) skips closer dirtx64, jumps to collect, and returns ok=True
    state_a["items"] = list(base_items)
    state_a["drops"].clear()
    state_a["moves"].clear()
    state_a["controls"].clear()
    state_a["pickup_raw_iron"] = True
    state_a["entities"] = [
        {"id": 20, "item": "dirtx64", "pos": [0.5, 40.0, 0.5], "dist": 0.2},
        {"id": 21, "item": "raw_ironx1", "pos": [2.0, 41.2, 0.5], "dist": 1.9},
    ]
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_mine_b:
        res_b = MacroSkills(bot_mine_b).mine_blocks(["iron_ore"], count=1, radius=16)
    assert all(m.get("entity_id") != 20 for m in state_a["moves"])
    assert any(c.get("forward") is True and c.get("jump") is True for c in state_a["controls"])
    assert res_b["ok"] is True
    assert res_b["target_gained"] == {"raw_iron": 1}


def test_night_double_death_triggers_emergency_shelter_before_third_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_agent.harness.agent import run_mc_agent

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.bot.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda dt: clock.__setitem__(0, clock[0] + float(dt)))

    world_state: dict[str, Any] = {
        "pos": [0.5, 70.0, 0.5],
        "on_ground": True,
        "placed": [],
        "dug": [],
        "seq_calls": 0,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if path == "/events":
            since = int(req.url.params.get("since", 0)) if req.method == "GET" else int(body.get("since", 0))
            world_state["seq_calls"] += 1
            if since == 0 and world_state["seq_calls"] == 1:
                return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
            if since < 2:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "last_seq": 2,
                            "events": [
                                {"seq": 1, "type": "death", "pos": [0, 70, 0], "cause": "mob_attack"},
                                {"seq": 2, "type": "death", "pos": [1, 70, 1], "cause": "mob_attack"},
                            ],
                        },
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 2}})
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(world_state["pos"]),
                        "health": 20,
                        "food": 20,
                        "is_day": False,
                        "alive": True,
                        "ready": True,
                        "on_ground": world_state["on_ground"],
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
                            {"name": "cobblestone", "count": 32, "slot": 9},
                            {"name": "stone_pickaxe", "count": 1, "slot": 10},
                        ],
                        "empty_slots": 34,
                    },
                },
            )
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": []}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": [], "summary": {"completed": 0}}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            if pt in world_state["placed"]:
                return httpx.Response(200, json={"ok": True, "result": {"name": "cobblestone", "diggable": True}})
            if pt in world_state["dug"]:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air", "diggable": False}})
            if pt[1] >= 70:
                return httpx.Response(200, json={"ok": True, "result": {"name": "air", "diggable": False}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone", "diggable": True}})
        if path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(world_state["pos"])}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            world_state["dug"].append(pt)
            if pt == (0, 69, 0):
                world_state["pos"] = [0.5, 69.0, 0.5]
            elif pt == (0, 68, 0):
                world_state["pos"] = [0.5, 68.0, 0.5]
            elif pt == (0, 67, 0):
                world_state["pos"] = [0.5, 67.0, 0.5]
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone"}})
        if path == "/place":
            world_state["placed"].append(tuple(int(v) for v in body["pos"]))
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"]}})
        raise AssertionError(path)

    turn_num = 0
    seen_shelter_msg = False

    def chat(messages, tools=None, **kwargs):
        nonlocal turn_num, seen_shelter_msg
        turn_num += 1
        if turn_num == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "mc_status", "arguments": "{}"}}],
            }
        seen_shelter_msg = any("[防死亡循环保护]" in str(m.get("content") or "") for m in messages)
        return {"role": "assistant", "content": "已在掩体安全躲过 60s 连死窗口。"}

    run_events: list[dict[str, Any]] = []
    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_s:
        rep = run_mc_agent(
            "survive night",
            bot_s,
            chat_fn=chat,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
            on_event=run_events.append,
        )
    assert rep["status"] == "answered"
    assert seen_shelter_msg is True
    shelter_evs = [e for e in run_events if e["type"] == "guardian.death_loop_shelter"]
    assert len(shelter_evs) == 1
    assert shelter_evs[0]["sheltered"] is True
    assert shelter_evs[0]["held_s"] == 55.0
    assert clock[0] >= 1055.0


def test_v1314_control_wait_and_shoot_charge_passthrough() -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        recorded.append((path, body))
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [10.0, 64.0, 10.0],
                        "ready": True,
                        "held_controls": {"keys": ["forward"], "remaining_ms": 250},
                    },
                },
            )
        if path == "/control":
            if body == {"wait": False}:
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"held": [], "released": ["forward"], "pos": [10.0, 64.0, 10.0]}},
                )
            return httpx.Response(
                200,
                json={"ok": True, "result": {"held": ["forward"], "release_in_ms": body.get("ms", 300)}},
            )
        if path == "/shoot":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"fired": True, "weapon": body.get("weapon"), "charge_ms": body.get("charge_ms")}},
            )
        raise AssertionError(f"unexpected path: {path}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_s:
        ts = McToolset(bot_s)
        res_start = json.loads(ts.mc_control(forward=True, ms=400, wait=False))
        assert res_start["ok"] is True
        res_stop = json.loads(ts.mc_control(wait=False))
        assert res_stop["ok"] is True
        st_res = json.loads(ts.mc_status())
        assert st_res["result"]["held_controls"] == {"keys": ["forward"], "remaining_ms": 250}
        res_shoot = json.loads(ts.mc_shoot(entity_id=1, weapon="bow", charge_ms=1000))
        assert res_shoot["ok"] is True

    control_calls = [body for path, body in recorded if path == "/control"]
    assert control_calls == [{"forward": True, "ms": 400, "wait": False}, {"wait": False}]
    shoot_calls = [body for path, body in recorded if path == "/shoot"]
    assert shoot_calls == [{"weapon": "bow", "entity_id": 1, "charge_ms": 1000}]


def test_curriculum_two_stone_picks_and_late_grind_husbandry_demoted() -> None:
    """bootstrap_iron_age ensures 2 stone pickaxes; rare 80m-island achievements stay at tier=3."""
    from mc_agent.harness.curriculum import CURRICULUM_PLAYBOOKS, select_next_target

    assert ("stone_pickaxe", 2) in CURRICULUM_PLAYBOOKS["bootstrap_iron_age"].pre_ensure

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": [0.5, 70.0, 0.5], "dimension": "minecraft:overworld", "time_of_day": 2000, "in_water": False, "alive": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_pickaxe", "count": 1}]}})
        if path == "/advancements":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "advancements": [
                            {"id": "minecraft:story/root", "status": "completed"},
                            {"id": "minecraft:story/mine_stone", "status": "completed"},
                            {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                            {"id": "minecraft:story/smelt_iron", "status": "completed"},
                            {"id": "minecraft:story/iron_tools", "status": "completed"},
                            {"id": "minecraft:husbandry/root", "status": "completed"},
                            {"id": "minecraft:husbandry/obtain_sniffer_egg", "status": "not_started"},
                            {"id": "minecraft:husbandry/allay_deliver_item_to_player", "status": "not_started"},
                            {"id": "minecraft:husbandry/ride_a_boat_with_a_goat", "status": "not_started"},
                            {"id": "minecraft:husbandry/breed_an_animal", "status": "not_started"},
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_c:
        picked, _ = select_next_target(
            bot_c,
            cooldowns={"minecraft:story/shiny_gear": 999999.0},
            now=1.0,
            enable_island_recon=False,
        )
    assert picked is not None
    assert picked.key == "minecraft:husbandry/make_a_sign_glow"
    assert picked.key not in {
        "minecraft:husbandry/obtain_sniffer_egg",
        "minecraft:husbandry/allay_deliver_item_to_player",
        "minecraft:husbandry/ride_a_boat_with_a_goat",
    }


def test_curriculum_death_drop_recovery_and_dusk_surface_filter() -> None:
    """Daytime safe death drop triggers micro recover_death_drop; dusk/night skips daytime surface chores."""
    from mc_agent.harness.curriculum import select_next_target
    from mc_agent.harness.world_memory import WorldMemory

    state = {"tod": 2000, "is_day": True, "inv": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [10.5, 68.0, 10.5],
                        "dimension": "minecraft:overworld",
                        "time_of_day": state["tod"],
                        "is_day": state["is_day"],
                        "in_water": False,
                        "alive": True,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["inv"])}})
        if path == "/advancements":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "advancements": [
                            {"id": "minecraft:story/root", "status": "completed"},
                            {"id": "minecraft:story/mine_stone", "status": "completed"},
                            {"id": "minecraft:story/upgrade_tools", "status": "completed"},
                            {"id": "minecraft:story/smelt_iron", "status": "completed"},
                            {"id": "minecraft:story/iron_tools", "status": "completed"},
                            {"id": "minecraft:husbandry/root", "status": "completed"},
                            {"id": "minecraft:husbandry/breed_an_animal", "status": "not_started"},
                            {"id": "minecraft:husbandry/plant_seed", "status": "not_started"},
                            {"id": "minecraft:story/shiny_gear", "status": "not_started"},
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    wm = WorldMemory()
    wm.last_death = {"dim": "overworld", "pos": [30, 66, 20], "t": 100.0}

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)) as bot_c:
        # 1) Daytime + recent (<260s) + close + high-value run -> recover_death_drop (short budget)
        t1, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1.key == "recover_death_drop"
        assert t1.budget_class == "short"

        # 1b) BUG-149: Deep diamond-layer death (y=-54) with naked surface respawn (y=64, gap>=18, no carried pickaxe)
        # must NOT waste an episode on recover_death_drop even if a nearby cached chest holds a stone_pickaxe
        wm.last_death = {"dim": "overworld", "pos": [100, -54, 40], "t": 100.0}
        t1b_empty, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1b_empty.key == "bootstrap_stone_age"
        wm.containers[("overworld", 11, 68, 11)] = {"items": {"stone_pickaxe": 1}}
        t1b, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1b.key != "recover_death_drop"
        wm.containers.clear()

        # 1c) Deep death when bot already holds a stone_pickaxe still allows recover_death_drop
        state["inv"] = [{"name": "stone_pickaxe", "count": 1}]
        t1c, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t1c.key == "recover_death_drop"
        assert t1c.budget_class == "short"
        state["inv"] = []

        # 2) Nighttime surface death -> must NOT run naked into night surface to recover corpse
        wm.last_death = {"dim": "overworld", "pos": [30, 66, 20], "t": 100.0}
        state["tod"] = 12000
        state["is_day"] = False
        t2, _ = select_next_target(bot_c, world_memory=wm, now=150.0, enable_island_recon=False)
        assert t2.key == "bootstrap_stone_age"

        # 3) Dusk/night with iron_pickaxe -> skips daytime surface chores (breed_an_animal/plant_seed)
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "shield", "count": 1}]
        t3, _ = select_next_target(
            bot_c,
            world_memory=wm,
            now=150.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t3.key not in {"minecraft:husbandry/breed_an_animal", "minecraft:husbandry/plant_seed"}


def test_bug118_125_131_curriculum_dimension_bare_kill_and_night_prep() -> None:
    """Regression for BUG-118 (day1_night_iron_prep), BUG-125 (overworld nether/*), BUG-131 (naked kill_a_mob)."""
    state: dict[str, Any] = {
        "dim": "overworld",
        "pos": [0.5, 64.0, 0.5],
        "is_day": False,
        "tod": 14000,
        "armor_points": 0,
        "inv": [{"name": "diamond_pickaxe", "count": 1}, {"name": "shield", "count": 1}],
        "completed": [
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/iron_tools",
            "minecraft:story/obtain_armor",
            "minecraft:story/mine_diamond",
            "minecraft:story/shiny_gear",
            "minecraft:story/form_obsidian",
            "minecraft:story/enter_the_nether",
            "minecraft:story/enchant_item",
            "minecraft:story/deflect_arrow",
            "minecraft:adventure/kill_a_mob",
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": state["dim"], "pos": state["pos"],
                "health": 20.0, "food": 20, "is_day": state["is_day"], "time_of_day": state["tod"],
                "armor_points": state["armor_points"],
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": state["inv"]}})
        if path == "/advancements":
            advs = [{"id": a, "status": "completed"} for a in state["completed"]]
            advs.extend([
                {"id": "minecraft:nether/root", "status": "completed"},
                {"id": "minecraft:adventure/root", "status": "completed"},
                {"id": "minecraft:nether/find_fortress", "status": "not_started"},
                {"id": "minecraft:nether/obtain_ancient_debris", "status": "not_started"},
                {"id": "minecraft:adventure/kill_a_mob", "status": "not_started"},
            ])
            return httpx.Response(200, json={"ok": True, "result": {"ready": True, "advancements": advs}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("tok", bot_username="CurP0", transport=httpx.MockTransport(handler)) as bot_c:
        # BUG-125: in overworld, nether/find_fortress must NOT be selected even if enter_the_nether is completed
        t_overworld, _ = select_next_target(bot_c, enable_island_recon=False, recon_done=True)
        assert not t_overworld.key.startswith("minecraft:nether/"), f"unexpected nether target in overworld: {t_overworld.key}"

        # In the_nether, nether/find_fortress IS eligible and prioritized
        state["dim"] = "the_nether"
        t_nether, _ = select_next_target(bot_c, enable_island_recon=False, recon_done=True)
        assert t_nether.key == "minecraft:nether/find_fortress"

        # BUG-131: naked bare without iron_pickaxe/shield/armor must NOT be assigned kill_a_mob when iron is cooled
        state["dim"] = "overworld"
        state["inv"] = [{"name": "stone_pickaxe", "count": 1}]
        state["completed"] = ["minecraft:story/mine_stone", "minecraft:story/upgrade_tools"]
        t_bare, _ = select_next_target(
            bot_c,
            cooldowns={"bootstrap_iron_age": 9999.0},
            now=100.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t_bare.key != "minecraft:adventure/kill_a_mob"

        # BUG-118: day1_night_iron_prep must NOT tell bot to chop 12 logs on night surface or force fuel_item='coal'
        from mc_agent.harness.world_memory import WorldMemory
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "raw_iron", "count": 3}]
        state["completed"] = ["minecraft:story/mine_stone", "minecraft:story/upgrade_tools", "minecraft:story/smelt_iron", "minecraft:story/iron_tools"]
        t_n1, _ = select_next_target(
            bot_c,
            world_memory=WorldMemory(),
            now=100.0,
            enable_island_recon=True,
            recon_done=True,
        )
        assert t_n1.key == "day1_night_iron_prep"
        assert "12 根原木" not in t_n1.task
        assert "严禁调 mc_skill_staircase(up)" in t_n1.task

        # BUG-139: nighttime without bed, >=3 wool, or known bed/village must NOT dispatch adventure/sleep_in_bed
        state["dim"] = "overworld"
        state["is_day"] = False
        state["tod"] = 14000
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "white_wool", "count": 2}]
        state["completed"] = [
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:story/smelt_iron",
            "minecraft:story/iron_tools",
            "minecraft:story/obtain_armor",
            "minecraft:story/mine_diamond",
            "minecraft:story/shiny_gear",
            "minecraft:story/form_obsidian",
            "minecraft:story/enter_the_nether",
            "minecraft:story/enchant_item",
            "minecraft:story/deflect_arrow",
            "minecraft:adventure/kill_a_mob",
        ]
        t_no_bed, _ = select_next_target(
            bot_c,
            world_memory=WorldMemory(),
            now=100.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t_no_bed.key != "minecraft:adventure/sleep_in_bed"

        # With >=3 wool at night, sleep_in_bed IS allowed
        state["inv"] = [{"name": "iron_pickaxe", "count": 1}, {"name": "white_wool", "count": 3}]
        t_has_wool, _ = select_next_target(
            bot_c,
            world_memory=WorldMemory(),
            now=100.0,
            enable_island_recon=False,
            recon_done=True,
        )
        assert t_has_wool.key == "minecraft:adventure/sleep_in_bed"


def test_bug155_move_to_within_range_and_bug121_forced_final_status() -> None:
    """Regression for BUG-155 (mc_move_to within real float range stays ok=True; outside range fails truthfully)
    and BUG-121 (force_final turn with 0 tools reports limit_reached instead of answered)."""
    pos_now = [219427.5, 69.0, 152037.5]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {
                "ready": True, "alive": True, "dimension": "overworld",
                "pos": list(pos_now), "health": 20.0, "food": 20, "on_ground": True,
            }})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/move_to":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(pos_now)}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    seen_results: list[dict[str, Any]] = []
    turn = 0

    def chat_move(messages: list[dict[str, Any]], tools=None, **kwargs: Any) -> dict[str, Any]:
        nonlocal turn
        turn += 1
        if turn == 1:
            # Same cell / within range=2 (dist ~0.71m <= 2.0) -> must stay ok=True, already_within_range=True
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "m1", "type": "function", "function": {
                    "name": "mc_move_to", "arguments": '{"x":219427,"z":152037,"range":2}',
                }}],
            }
        if turn == 2:
            seen_results.append(json.loads(messages[-1]["content"].splitlines()[0]))
            # Outside real float range=2 (219427.5, 152037.5 -> 219425, 152037 = 2.55m > 2.0) -> must fail truthfully
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "m2", "type": "function", "function": {
                    "name": "mc_move_to", "arguments": '{"x":219425,"z":152037,"range":2}',
                }}],
            }
        if turn == 3:
            seen_results.append(json.loads(messages[-1]["content"].splitlines()[0]))
            # Same XZ but Y=14 (dist=55m > range=2) -> must fail with 3D distance, not claim arrived
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "m3", "type": "function", "function": {
                    "name": "mc_move_to", "arguments": '{"x":219427,"y":14,"z":152037,"range":2}',
                }}],
            }
        last_tool_msg = next(m for m in reversed(messages) if m.get("role") == "tool")
        seen_results.append(json.loads(last_tool_msg["content"].splitlines()[0]))
        # Turn 4 is force_final (max_calls=4 -> tools=[]): plain text must report limit_reached
        assert tools == [] or tools is None
        return {"role": "assistant", "content": "预算耗尽总结"}

    with BotSession("tok", bot_username="MoveBug155", transport=httpx.MockTransport(handler)) as bot_m:
        rep = run_mc_agent(
            "测试 move_to 范围与末轮状态",
            bot_m,
            chat_fn=chat_move,
            max_calls=4,
            survival_guard=False,
            hud_mode="ephemeral",
            context_mode="sliding",
            tool_profile="compact",
        )
    assert rep["status"] == "limit_reached"
    assert len(seen_results) == 3
    assert seen_results[0]["ok"] is True and seen_results[0].get("already_within_range") is True
    assert seen_results[1]["ok"] is False and seen_results[1]["error"]["code"] == "movement_not_confirmed"
    assert "已在目标" not in seen_results[1]["error"]["message"]
    assert seen_results[2]["ok"] is False and seen_results[2]["error"]["code"] == "movement_not_confirmed"
    assert float(seen_results[2]["dist_remaining"]) > 50.0








