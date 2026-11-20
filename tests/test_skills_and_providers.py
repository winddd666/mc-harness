from __future__ import annotations

import json
import time

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.config import should_trust_env
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.context import RequestContext
from mc_agent.harness.model import (
    DEEPSEEK_DEFAULT_MODEL,
    QwenSpec,
    _stream_message,
    resolve_qwen_endpoint,
)
from mc_agent.skills import MacroSkills, SafetyEmergency, check_and_stabilize, compute_advancement_frontier


def test_deepseek_provider_and_direct_routing(monkeypatch, tmp_path) -> None:
    assert should_trust_env("https://api.deepseek.com/chat/completions") is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-placeholder")
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    ds = resolve_qwen_endpoint(
        QwenSpec(provider="deepseek", dotenv_path=str(empty_env), fallback_dotenv_path=None)
    )
    assert ds.endpoint == "https://api.deepseek.com/chat/completions"
    assert ds.model == DEEPSEEK_DEFAULT_MODEL
    assert ds.rpm_budget == 0
    assert ds.extra_body(enable_thinking=False) == {"thinking": {"type": "disabled"}}
    assert ds.extra_body(enable_thinking=True) == {"thinking": {"type": "enabled"}}

    monkeypatch.setenv("NJU_API_KEY", "nju-test-placeholder")
    nju = resolve_qwen_endpoint(
        QwenSpec(provider="nju", dotenv_path=str(empty_env), fallback_dotenv_path=None)
    )
    assert nju.endpoint == "https://api.nju-aia.com/v1/chat/completions"
    assert nju.model == "default"
    assert nju.rpm_budget == 24
    assert nju.extra_body(enable_thinking=False) == {"chat_template_kwargs": {"enable_thinking": False}}


def test_request_context_quantized_kv_prefix() -> None:
    ctx = RequestContext(tool_output_chars=400, archive_after_tools=4, archive_step=4)
    big = "X" * 1200
    history: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]

    for i in range(7):
        history.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"#{i}:" + big})
        built = ctx.build(history, record=False)
        # Fewer than 4+4 tools: tool #0 stays at full tool_output_chars=400 cap, no step shift yet
        assert len(built[2]["content"]) <= 400
        assert "旧轮次工具输出已折叠" not in built[2]["content"]

    # 8th tool triggers quantised folding of tools 0..3
    history.append({"role": "tool", "tool_call_id": "t7", "content": "#7:" + big})
    built8 = ctx.build(history, record=False)
    for idx in range(2, 6):
        assert "旧轮次工具输出已折叠" in built8[idx]["content"]
    snap_prefix = [built8[idx]["content"] for idx in range(6)]

    # Adding 9th tool must keep tools 0..3 byte-identical for KV cache stability
    history.append({"role": "tool", "tool_call_id": "t8", "content": "#8:" + big})
    built9 = ctx.build(history, record=False)
    assert [built9[idx]["content"] for idx in range(6)] == snap_prefix


def test_stream_reasoning_and_event_drain() -> None:
    lines = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"step1 "}}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"step2","content":"好的"}}]}',
        "",
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    ]
    msg = _stream_message(lines, None)
    assert msg["content"] == "好的"
    assert msg["reasoning_content"] == "step1 step2"

    seq_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seq_calls
        path = request.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"username": "5Ease_Bot", "ready": True, "pos": [0, 64, 0]}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [], "empty_slots": 36}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"total": 0, "blocks": []}})
        if path == "/events":
            seq_calls += 1
            if seq_calls == 1:
                return httpx.Response(200, json={"ok": True, "result": {"last_seq": 10, "events": [{"seq": 9, "type": "chat", "text": "<old> stale"}]}})
            if seq_calls == 2:
                return httpx.Response(200, json={"ok": True, "result": {"last_seq": 12, "events": [{"seq": 11, "type": "chat", "text": "<Alice> 来西边矿洞"}]}})
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 12, "events": []}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    turn = 0

    def fake_chat(messages, tools=None, *, stream=False, on_event=None, control=None):
        nonlocal turn
        turn += 1
        if turn == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "s1", "type": "function", "function": {"name": "mc_status", "arguments": "{}"}}],
            }
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert any("不可信玩家公屏消息" in m["content"] and "来西边矿洞" in m["content"] for m in tool_msgs)
        assert not any("stale" in m["content"] for m in tool_msgs)
        return {"role": "assistant", "content": "已收到队友聊天提示。"}

    report = run_mc_agent("测试事件同步", bot, chat_fn=fake_chat, max_calls=4, stream=False, on_event=lambda _: None)
    assert report["status"] == "answered"


def test_macro_skills_protection_tier_safety_and_frontier() -> None:
    dug_calls: list[list[int]] = []
    posture_calls: list[dict] = []
    used_items: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [10, 64, 10],
                        "health": 20,
                        "food": 12,
                        "in_water": True,
                        "submerged": True,
                        "oxygen": 10,
                        "jumping": False,
                        "on_ground": False,
                        "alive": True,
                        "ready": True,
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
                            {"name": "wooden_pickaxe", "count": 1, "slot": 36},
                            {"name": "bread", "count": 3, "slot": 37},
                        ]
                    },
                },
            )
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "total": 2,
                        "blocks": [
                            {"name": "oak_log", "pos": [1, 64, 1], "dist": 12.0},
                            {"name": "oak_log", "pos": [60, 64, 60], "dist": 70.0},
                        ],
                    },
                },
            )
        if path == "/dig":
            dug_calls.append(body["pos"])
            if body["pos"] == [1, 64, 1]:
                return httpx.Response(
                    200,
                    json={"ok": False, "error": {"code": "protected_region", "message": "protected"}},
                )
            return httpx.Response(
                200,
                json={"ok": True, "result": {"dug": "oak_log", "gained": {"oak_log": 1}}},
            )
        if path == "/posture":
            posture_calls.append(body)
            return httpx.Response(200, json={"ok": True, "result": body})
        if path == "/use_item":
            used_items.append(body["item"])
            return httpx.Response(200, json={"ok": True, "result": {"used": {body["item"]: 1}}})
        if path == "/advancements":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"advancements": [{"id": "minecraft:story/root", "status": "completed"}]}},
            )
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    skills = MacroSkills(bot)

    # 1. Pickaxe tier gate blocks wooden_pickaxe from attempting iron_ore
    tier_err = skills.mine_blocks(["iron_ore"], count=1)
    assert tier_err["ok"] is False
    assert tier_err["error"] == "tool_tier_insufficient"
    assert not dug_calls

    # 2. Protected dig blacklists [1, 64, 1] and falls back to [60, 64, 60]
    chop = skills.harvest_logs(target_logs=1)
    assert chop["ok"] is True
    assert chop["dug_count"] == 1
    assert (1, 64, 1) in skills.protected_positions

    # 3. L0 safety triggers water float + auto-eat bread
    stab = check_and_stabilize(bot)
    assert "posture_water_float" in stab["actions"]
    assert "ate:bread" in stab["actions"]
    assert posture_calls and posture_calls[0]["jump"] is True
    assert used_items == ["bread"]

    # 4. Advancement frontier lists story/mine_stone after story/root
    frontier = compute_advancement_frontier(bot, limit=15)
    assert frontier["completed_count"] == 1
    assert any(row["id"] == "minecraft:story/mine_stone" for row in frontier["frontier"])

    # 5. Low health raises SafetyEmergency
    danger_bot = BotSession(
        "tok",
        bot_username="5Ease_Bot",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"ok": True, "result": {"health": 4.0, "food": 20, "in_water": False, "in_lava": False, "alive": True}},
            )
        ),
    )
    with pytest.raises(SafetyEmergency):
        check_and_stabilize(danger_bot)


def test_mine_blocks_out_of_reach_ranking_and_drop_collection() -> None:
    dug_positions: list[list[int]] = []
    moved_to: list[dict] = []
    cobble = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cobble
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 100.0, 0.0], "health": 20, "food": 20}})
        if path == "/inventory":
            items = [{"name": "wooden_pickaxe", "count": 1, "slot": 36}]
            if cobble > 0:
                items.append({"name": "cobblestone", "count": cobble, "slot": 37})
            return httpx.Response(200, json={"ok": True, "result": {"items": items}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "stone", "pos": [0, 80, 0], "dist": 20.0},
                            {"name": "stone", "pos": [2, 100, 0], "dist": 2.0},
                            {"name": "stone", "pos": [3, 100, 0], "dist": 3.0},
                        ]
                    },
                },
            )
        if path == "/dig":
            dug_positions.append(body["pos"])
            if body["pos"] == [2, 100, 0]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "blocked"}})
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {}}})
        if path == "/get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"entities": [{"id": 9001, "type": "item", "item": "cobblestonex1", "pos": [3.0, 100.0, 0.0]}]},
                },
            )
        if path == "/move_to":
            moved_to.append(body)
            cobble += 1
            return httpx.Response(200, json={"ok": True, "result": {"pos": [3.0, 100.0, 0.0]}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    skills = MacroSkills(bot)
    res = skills.mine_blocks(["stone"], count=1, radius=24)
    assert res["ok"] is True
    assert res["dug_count"] == 1
    assert res["gained"].get("cobblestone") == 1
    # [0, 80, 0] is penalized vertically; [2, 100, 0] is tried first, blacklisted on out_of_reach, then [3, 100, 0] succeeds
    assert dug_positions == [[2, 100, 0], [3, 100, 0]]
    assert (2, 100, 0) not in skills.protected_positions
    assert skills.temporary_avoidance[(2, 100, 0)]["reason"] == "out_of_reach"
    assert moved_to and moved_to[0]["entity_id"] == 9001


def test_harvest_logs_stops_after_four_unreachable_targets(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    origins = [(x, 64, 0) for x in range(3, 9)]
    attempts = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": list(pos), "name": "oak_log", "dist": pos[0]} for pos in origins]}
        elif route == "get_block":
            result = {"name": "air"}
        elif route == "dig":
            attempts.append(tuple(json.loads(request.content)["pos"]))
            clock[0] += 2.75
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.harvest_logs(target_logs=16)
        assert attempts == origins[:4]
        assert clock[0] <= 12
        clock[0] += 16
        assert all(skills._is_block_avoided(pos) for pos in attempts)
        clock[0] += 45
        assert all(not skills._is_block_avoided(pos) for pos in attempts)

    assert result["stop_reason"] == "unreachable_targets"
    assert result["requires_replan"] is True
    assert result["dug_count"] == 0
    assert result["gained"] == {}


def test_harvest_logs_prefers_low_trunk_over_closer_canopy() -> None:
    low = [5, 64, 0]
    high = [2, 67, 0]
    attempted = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            result = {"pos": [0.5, 64.4, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            result = {"blocks": [
                {"pos": high, "name": "oak_log", "dist": 2.0},
                {"pos": low, "name": "oak_log", "dist": 5.0},
            ]}
        elif route == "dig":
            attempted.append(json.loads(request.content)["pos"])
            result = {"dug": "oak_log", "gained": {"oak_log": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).harvest_logs(target_logs=1)

    assert attempted == [low]
    assert result["ok"] is True
    assert result["target_gained"] == {"oak_log": 1}


def test_harvest_logs_budget_covers_setup_and_preserves_partial_dig(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    attempts = []
    calls = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "status":
            clock[0] += 1.0
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": [1 + len(attempts), 64, 0], "name": "oak_log", "dist": 1.0}]}
        elif route == "dig":
            timeout = request.extensions["timeout"]["read"]
            attempts.append(timeout)
            if len(attempts) == 2:
                clock[0] += timeout
                raise httpx.ReadTimeout("slow dig", request=request)
            clock[0] += 1.0
            result = {"dug": "oak_log", "gained": {"oak_log": 1}}
        elif route == "stop":
            clock[0] += 0.5
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).harvest_logs(target_logs=4, max_duration_s=8.0)

    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["gained"] == {"oak_log": 1}
    assert result["dug_count"] == 1
    assert attempts[1] <= 4.0
    assert calls[-1] == "stop"
    assert clock[0] <= 8.0


@pytest.mark.parametrize("timeout_after_column", [False, True])
def test_harvest_logs_chops_one_column_and_collects_at_the_end(monkeypatch, timeout_after_column) -> None:
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    standing = [0.5, 64.0, 0.5]
    trunk = [[5, y, 0] for y in range(64, 68)]
    scans = []
    moves = []
    digs = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": standing, "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": []}
        elif route == "find_blocks":
            scans.append(body)
            cells = [*trunk, [8, 64, 0]] if len(scans) == 1 else [[8, 64, 0]]
            result = {"blocks": [{"name": "oak_log", "pos": p, "dist": float(p[0])} for p in cells]}
        elif route == "move_to":
            moves.append(body)
            standing[:] = [4.1, 64.0, 0.5]
            clock[0] += 2.0
            result = {"pos": standing}
        elif route == "dig":
            digs.append(body)
            if len(digs) > 4:
                clock[0] += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("slow second trunk", request=request)
            clock[0] += 1.0
            result = {"dug": "oak_log", "gained": {"oak_log": 4} if body["collect"] else {}}
        elif route == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        outcome = MacroSkills(bot).harvest_logs(
            target_logs=6 if timeout_after_column else 4, max_duration_s=14.0,
        )

    assert [d["pos"] for d in digs[:4]] == trunk
    assert [d["collect"] for d in digs[:4]] == [False, False, False, True]
    assert len(moves) == 1
    assert [moves[0]["x"], moves[0]["z"]] == [5.5, 0.5]
    assert len(scans) == 1 + timeout_after_column
    assert outcome["ok"] is True
    assert outcome["dug_count"] == 4
    assert outcome["target_gained"] == {"oak_log": 4}
    if timeout_after_column:
        assert outcome["stop_reason"] == "time_budget"
        assert outcome["partial_progress"] is True
    else:
        assert clock[0] == 6.0


@pytest.mark.parametrize("actual_logs", [2, 6])
def test_harvest_logs_checks_inventory_when_cuts_outnumber_reported_pickups(actual_logs) -> None:
    cuts = 0
    pickup_scans = 0

    def handler(request):
        nonlocal cuts, pickup_scans
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": "spruce_log", "count": min(cuts, actual_logs)}]}
        elif route == "find_blocks":
            result = {"blocks": [{"name": "spruce_log", "pos": [cuts + 1, 64, 0], "dist": 1.0}]}
        elif route == "dig":
            cuts += 1
            result = {"dug": "spruce_log", "gained": {"spruce_log": 1} if cuts <= 2 else {}}
        elif route == "get_entities":
            pickup_scans += 1
            result = {"entities": []}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).harvest_logs(target_logs=6)

    assert cuts == 6
    assert result["target_gained"] == {"spruce_log": actual_logs}
    assert result["ok"] is (actual_logs == 6)
    if actual_logs == 2:
        assert result["error"] == "partial_collection"
        assert result["partial_progress"] is True
        assert pickup_scans == 1
    else:
        assert pickup_scans == 0


def test_mine_blocks_with_wooden_pick_skips_only_unmineable_types() -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    queries = []
    digs = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": "wooden_pickaxe", "count": 1}]}
        elif route == "status":
            result = {"pos": [0.5, 64.0, 0.5]}
        elif route == "equip":
            assert body["item"] == "wooden_pickaxe"
            result = {}
        elif route == "find_blocks":
            queries.append(body["types"])
            result = {"blocks": [{"name": "stone", "pos": [1, 64, 0], "dist": 1.0}]}
        elif route == "dig":
            digs.append(body["pos"])
            result = {"dug": "stone", "gained": {"cobblestone": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        outcome = json.loads(toolset.mc_skill_mine_blocks(["stone", "iron_ore", "coal_ore"], count=1))

    assert outcome["ok"] is True
    assert outcome["skipped_unmineable"] == ["iron_ore"]
    assert outcome["target_gained"] == {"cobblestone": 1}
    assert queries == [["stone", "coal_ore"]]
    assert digs == [[1, 64, 0]]


def test_mine_blocks_single_dig_cannot_outlive_slice_deadline(monkeypatch) -> None:
    """r12: one un-timed obsidian dig let mine_blocks run ~300s."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    dig_timeouts: list[float] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "find_blocks":
            result = {"blocks": [{"name": "obsidian", "pos": [1, 64, 1], "dist": 1.0}]}
        elif path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "health": 20.0}
        elif path == "dig":
            timeout = request.extensions["timeout"]["read"]
            dig_timeouts.append(timeout)
            clock[0] += timeout
            raise httpx.ReadTimeout("offline dig still pending", request=request)
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills._dig_target_blocks(
            block_types=["obsidian"],
            count=4,
            radius=8,
            protected_msg="protected",
            below_tol=2.0,
            above_tol=2.0,
        )
    assert skills.temporary_avoidance[(1, 64, 1)]["reason"] == "dig_timeout"
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["partial_error"]["code"] == "mine_timeout"
    assert result["dug_count"] == 0
    # r14: simple stone/log digs took 8~12s; the per-dig budget must not be
    # divided down to a few seconds just because count is large.
    assert dig_timeouts and dig_timeouts[0] == pytest.approx(15.0)
    assert clock[0] <= 48.0


def test_collect_drops_smelt_charcoal_reservation_and_toolset_registration() -> None:
    from mc_agent.harness.mc_tools import McToolset

    window_ops: list[dict] = []
    crafted: list[dict] = []
    inv_items: list[dict] = [
        {"name": "oak_log", "count": 4, "slot": 36},
        {"name": "white_bed", "count": 1, "slot": 37},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inv_items
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 64.0, 0.0], "health": 20, "food": 20, "is_day": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(inv_items)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "furnace", "pos": [1, 64, 0], "dist": 1.2}]}})
        if path == "/craft":
            crafted.append(body)
            if body.get("item") == "oak_planks":
                inv_items = [
                    {"name": "oak_log", "count": 3, "slot": 36},
                    {"name": "oak_planks", "count": 4, "slot": 38},
                ]
            return httpx.Response(200, json={"ok": True, "result": {"crafted": body["item"], "gained": body.get("count", 1)}})
        if path == "/window/act":
            window_ops.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"done": True}})
        if path == "/window/view":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"sections": {"input": [], "output": [{"name": "charcoal", "count": 3}]}, "info": {"burning": False}}},
            )
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": [{"id": 501, "type": "item", "item": "cobblestonex2", "pos": [2.0, 64.0, 0.0]}]}})
        if path == "/move_to":
            if body.get("entity_id") == 501:
                inv_items = [*inv_items, {"name": "cobblestone", "count": 2, "slot": 39}]
                return httpx.Response(200, json={"ok": True, "result": {"pos": [2.0, 64.0, 0.0]}})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 64.0, 0.0]}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    skills = MacroSkills(bot)
    smelt_res = skills.smelt_items("oak_log", count=3)
    assert smelt_res["ok"] is True
    assert smelt_res["fuel"]["item"] == "oak_planks"
    assert smelt_res["fuel"]["count"] == 2
    assert crafted and crafted[0]["item"] == "oak_planks"
    assert window_ops[0]["ops"][0] == {"op": "put", "item": "oak_log", "count": 3, "to": "input"}
    assert window_ops[0]["ops"][1] == {"op": "put", "item": "oak_planks", "count": 2, "to": "fuel"}

    drops_res = skills.collect_drops()
    assert drops_res["ok"] is True
    assert drops_res["gained"].get("cobblestone") == 2

    names = {s["function"]["name"] for s in McToolset(bot).registry().schemas()}
    assert "mc_skill_collect_drops" in names
    assert "mc_skill_bind_bed" in names
    assert "mc_skill_unstick" in names


def test_unstick_uses_floor_on_negative_coords_and_clears_hitbox() -> None:
    solid_blocks = {
        (-4, 103, -6): "dirt",
        (-4, 102, -5): "dirt",
        (-3, 103, -5): "dirt",
    }
    dug_coords: list[tuple[int, int, int]] = []
    controls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-3.3, 102.0, -5.3], "health": 20, "food": 20}})
        if path == "/get_block":
            pos = tuple(body["pos"])
            name = solid_blocks.get(pos, "stone" if pos[1] == 101 else "air")
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": name != "air"}})
        if path == "/dig":
            pos = tuple(body["pos"])
            dug_coords.append(pos)
            solid_blocks.pop(pos, None)
            return httpx.Response(200, json={"ok": True, "result": {"dug": "dirt", "gained": {"dirt": 1}}})
        if path == "/control":
            controls.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-2.2, 102.0, -5.3], "moved": 1.1}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    res = MacroSkills(bot).unstick()
    assert res["ok"] is True
    assert res["foot_block_before"] == [-4, 102, -6]
    assert res["hitbox_xs"] == [-4, -3]
    assert res["hitbox_zs"] == [-6, -5]
    # (-4, 103, -6) and (-4, 102, -5) must be cleared because floor(-3.3) == -4 and floor(-5.3) == -6!
    assert (-4, 103, -6) in dug_coords
    assert (-4, 102, -5) in dug_coords
    assert (-3, 103, -5) in dug_coords
    assert controls and controls[0]["jump"] is False


def test_kb_lookup_jit_hints_and_path_not_found_recovery() -> None:
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.kb import build_jit_error_hint, lookup_kb

    # 1. Alias resolution & recipe/ore queries
    res_bed = lookup_kb("bed", category="recipe")
    assert res_bed["resolved_alias"] == "white_bed"
    assert res_bed["results"][0]["id"] == "white_bed"
    assert res_bed["results"][0]["table_3x3"] is True

    res_iron = lookup_kb("铁矿", category="ore")
    assert any(r["id"] == "iron_ore" and "16" in r["peak_y"] for r in res_iron["results"])

    # 2. JIT error hints
    h_use = build_jit_error_hint("use_item", {"item": "water_bucket"}, "need_block_target", "need block")
    assert h_use and "mc_interact" in h_use
    h_move = build_jit_error_hint("move_to", {"x": 50, "y": 102, "z": -10}, "path_not_found", "No path")
    assert h_move and "省略 y" in h_move
    h_rec = build_jit_error_hint("craft", {"item": "steak"}, "no_recipe", "unknown")
    assert h_rec and "cooked_beef" in h_rec

    # 3. Toolset integrates JIT hint on ApiError + mc_kb_lookup + frontier playbooks + path_not_found blacklist
    dug_pos: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 64.0, 0.0], "health": 20, "food": 20}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "/use_item":
            return httpx.Response(200, json={"ok": False, "error": {"code": "need_block_target", "message": "need block"}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "oak_log", "pos": [10, 64, 0], "dist": 10.0},
                            {"name": "oak_log", "pos": [3, 64, 0], "dist": 12.0},
                        ]
                    },
                },
            )
        if path == "/dig":
            dug_pos.append(body["pos"])
            if body["pos"] == [10, 64, 0]:
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "No path"}})
            return httpx.Response(200, json={"ok": True, "result": {"dug": "oak_log", "gained": {"oak_log": 1}}})
        if path == "/advancements":
            return httpx.Response(200, json={"ok": True, "result": {"advancements": [{"id": "minecraft:story/root", "status": "completed"}]}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    ts = McToolset(bot)
    assert "mc_kb_lookup" in {s["function"]["name"] for s in ts.registry().schemas()}

    use_err = json.loads(ts.mc_use_item("bucket"))
    assert use_err["ok"] is False
    assert "mc_interact" in use_err["error"]["kb_hint"]

    # harvest_logs skips [10, 64, 0] after path_not_found and chops [3, 64, 0]
    chop = ts.skills.harvest_logs(target_logs=1)
    assert chop["ok"] is True
    assert dug_pos == [[10, 64, 0], [3, 64, 0]]
    assert (10, 64, 0) not in ts.skills.protected_positions
    assert ts.skills.temporary_avoidance[(10, 64, 0)]["reason"] == "path_not_found"

    frontier = compute_advancement_frontier(bot, limit=10)
    mine_stone = next(row for row in frontier["frontier"] if row["id"] == "minecraft:story/mine_stone")
    assert mine_stone.get("difficulty") == "easy"
    assert "how_to" in mine_stone


def test_dig_safety_bunker_and_registration() -> None:
    from mc_agent.harness.mc_tools import McToolset

    state: dict[str, Any] = {
        "pos": [59.5, 112.0, -25.5],
        "items": [{"name": "oak_log", "count": 2, "slot": 0}],
        "blocks": {
            (59, 108, -26): "water",
            (59, 111, -26): "stone",
            (59, 110, -26): "stone",
            (59, 109, -26): "stone",
        },
        "dug": [],
        "placed": [],
        "crafted": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 10.0, "food": 18, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": state["blocks"].get(key, "air"), "pos": list(key)}})
        if path == "/craft":
            item = body["item"]
            cnt = int(body.get("count", 1))
            state["crafted"].append((item, cnt))
            if item == "oak_planks":
                state["items"] = [
                    {"name": "oak_log", "count": 1, "slot": 0},
                    {"name": "oak_planks", "count": 4, "slot": 1},
                ]
            return httpx.Response(200, json={"ok": True, "result": {"item": item, "count": cnt}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            state["dug"].append(list(key))
            state["blocks"][key] = "air"
            state["pos"][1] = float(key[1])
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        if path == "/place":
            key = tuple(int(v) for v in body["pos"])
            state["placed"].append((body["item"], list(key)))
            state["blocks"][key] = body["item"]
            return httpx.Response(200, json={"ok": True, "result": {"ok": True}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-bunker",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        unsafe = skills.dig_safety_bunker()
        assert unsafe["ok"] is False
        assert unsafe["reason"] == "unsafe_floor"
        assert unsafe["floor_pos"] == [59, 108, -26]
        assert state["dug"] == []

        state["blocks"][(59, 108, -26)] = "stone"
        ok_res = skills.dig_safety_bunker()
        assert ok_res["ok"] is True
        assert ok_res["floor_pos"] == [59, 108, -26]
        assert ok_res["crafted_planks"] == "oak_planks"
        assert state["dug"] == [[59, 111, -26], [59, 110, -26], [59, 109, -26]]
        assert state["placed"] == [("oak_planks", [59, 111, -26])]
        assert ok_res["roof_placed"] is True
        assert ok_res["pos_after"] == [59.5, 109.0, -25.5]

        names = [s["function"]["name"] for s in McToolset(bot).registry().schemas()]
        assert "mc_skill_bunker" in names
    finally:
        bot.close()


def test_staircase_up_down_and_tight_table_pocket() -> None:
    from mc_agent.harness.mc_tools import McToolset

    state: dict[str, Any] = {
        "pos": [-2.3, 95.0, -2.7],
        "items": [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
            {"name": "cobblestone", "count": 5, "slot": 1},
            {"name": "stick", "count": 4, "slot": 2},
            {"name": "crafting_table", "count": 1, "slot": 3},
        ],
        "blocks": {},
        "dug": [],
        "placed": [],
        "mode": "down",
        "pocket_carved": False,
    }

    # Pre-seed stone underground, air corridor for up filler check, and tight walls around [-3, 95, -3]
    def _default_block(key: tuple[int, int, int]) -> str:
        if key in state["blocks"]:
            return state["blocks"][key]
        x, y, z = key
        if state["mode"] == "up" and (x, y, z) == (-3, 93, -4):
            return "grass_block"  # Actual floor after step two, not just a scan hint above a cave.
        if state["mode"] == "up" and x == -3 and z == -4 and y >= 94:
            return "air"
        if state["mode"] == "up" and y >= 94 and z <= -4 and (x, y, z) in {(-3, 93, -3)}:
            return "air"
        if state["mode"] == "up" and y == 92 and z == -5:
            return "air"  # missing step support at nx=-3, by=92, nz=-5 when stepping up
        if y <= 100:
            return "stone"
        return "air"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 20.0, "food": 20, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": _default_block(key), "pos": list(key)}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            state["dug"].append(list(key))
            state["blocks"][key] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": "stone", "gained": {"cobblestone": 1}}})
        if path == "/move_to":
            state["pos"] = [float(body["x"]), float(body["y"]), float(body["z"])]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"])}})
        if path == "/place":
            pos = body.get("pos")
            if pos is None:
                return httpx.Response(400, json={"ok": False, "error": {"code": "no_support", "message": "tight pocket"}})
            key = tuple(int(v) for v in pos)
            if body["item"] == "crafting_table" and _default_block(key) != "air":
                return httpx.Response(400, json={"ok": False, "error": {"code": "occupied", "message": "wall occupied"}})
            if body["item"] == "crafting_table":
                state["pocket_carved"] = True
            state["placed"].append((body["item"], list(key)))
            state["blocks"][key] = body["item"]
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"], "pos": list(key)}})
        if path == "/find_blocks":
            types = set(body.get("types") or [])
            if "crafting_table" in types:
                # Unreachable crafting_table 5 blocks above current y=95 -> must be ignored!
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"blocks": [{"name": "crafting_table", "pos": [-3, 100, -3], "dist": 5.0}]}},
                )
            if "iron_ore" in types:
                # Detect iron_ore once bot descends to y <= 92 (after 3 downward steps)
                if state["mode"] == "down" and state["pos"][1] <= 92.0:
                    return httpx.Response(
                        200,
                        json={"ok": True, "result": {"blocks": [{"name": "iron_ore", "pos": [-3, 92, -7], "dist": 2.1}]}},
                    )
                return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
            if "grass_block" in types:
                # Detect surface after 2 upward steps (from y=92 up to y=94)
                if state["mode"] == "up" and state["pos"][1] >= 94.0:
                    return httpx.Response(
                        200,
                        json={"ok": True, "result": {"blocks": [{"name": "grass_block", "pos": [-3, 94, -5], "dist": 1.5}]}},
                    )
                return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/craft":
            return httpx.Response(200, json={"ok": True, "result": {"crafted": body["item"], "gained": 1}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-stair",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        # (c) Distant Y=100 table ignored; open placements blocked -> carves pocket at [-2, 95, -3]
        state["blocks"][(-3, 95, -3)] = "air"
        state["blocks"][(-3, 96, -3)] = "air"
        table_res = skills.ensure_crafting_table()
        assert table_res["ok"] is True
        assert table_res["reused"] is False
        assert table_res["carved_pocket"] == [-2, 95, -3]
        assert [-2, 95, -3] in state["dug"]
        assert ("crafting_table", [-2, 95, -3]) in state["placed"]

        # (a) dig_staircase down stops at step 3 on iron_ore detection
        state["pos"] = [-2.3, 95.0, -2.7]
        state["dug"].clear()
        down_res = skills.dig_staircase(direction="north", steps=8, vertical="down", stop_on_ore=True)
        assert down_res["ok"] is True
        assert down_res["steps_done"] == 3
        assert down_res["stop_reason"] == "ore_detected"
        assert down_res["pos_after"] == [-2.5, 92.0, -5.5]
        # First step at [-3, 95, -3] heading north -> [-3, 96, -4], [-3, 95, -4], [-3, 94, -4]
        assert state["dug"][:3] == [[-3, 96, -4], [-3, 95, -4], [-3, 94, -4]]

        # (b) dig_staircase up places filler cobblestone step at [-3, 92, -5] and exits on surface
        state["mode"] = "up"
        state["blocks"].clear()
        state["placed"].clear()
        # Ensure step 1 sky check sees air at [-3, 96, -4] -> then grass_block triggers reached_surface
        state["blocks"][(-3, 96, -4)] = "air"
        up_res = skills.dig_staircase(direction="south", steps=6, vertical="up", stop_on_ore=False)
        assert up_res["ok"] is True
        assert up_res["steps_done"] == 2
        assert up_res["stop_reason"] == "reached_surface"
        assert ("cobblestone", [-3, 92, -5]) in state["placed"]

        # (d) Toolset registration
        names = [s["function"]["name"] for s in McToolset(bot).registry().schemas()]
        assert "mc_skill_staircase" in names
    finally:
        bot.close()


def test_unstick_ignores_suffocation_hurt_and_harvest_logs_inv_delta() -> None:
    state: dict[str, Any] = {
        "pos": [-2.3, 96.0, -1.5],
        "gravel_hits": 0,
        "dig_bodies": [],
        "items": [],
        "logs_dug": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 10.0, "food": 20, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            if key == (-3, 96, -2):
                # Gravel refills the cell once after the first dig, then clears on the second dig
                name = "gravel" if state["gravel_hits"] < 2 else "air"
                return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": name != "air"}})
            name = "stone" if key[1] == 95 else "air"
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": False}})
        if path == "/dig":
            state["dig_bodies"].append(body)
            pos = tuple(int(v) for v in body["pos"])
            if pos == (-3, 96, -2):
                # Suffocation hurt would interrupt unless stop_on=["death"]
                if body.get("stop_on") != ["death"]:
                    return httpx.Response(409, json={"ok": False, "error": {"code": "interrupted", "cause": "hurt"}})
                state["gravel_hits"] += 1
                return httpx.Response(200, json={"ok": True, "result": {"dug": "gravel", "gained": {}}})
            state["logs_dug"] += 1
            return httpx.Response(200, json={"ok": True, "result": {"dug": "oak_log", "gained": {}}})
        if path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-1.2, 96.0, -1.5], "moved": 1.1}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "oak_log", "pos": [-2, 97, -2], "dist": 1.4},
                            {"name": "oak_log", "pos": [-2, 98, -2], "dist": 2.2},
                        ]
                    },
                },
            )
        if path == "/get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 701, "name": "item", "item": "oak_logx2", "pos": [-2.0, 96.0, -2.0]}]}},
            )
        if path == "/move_to":
            # Moving over item entity silently puts 2 oak_log in inventory
            state["items"] = [{"name": "oak_log", "count": 2, "slot": 0}]
            return httpx.Response(200, json={"ok": True, "result": {"pos": [-2.0, 96.0, -2.0]}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-suff",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        u_res = skills.unstick()
        assert u_res["ok"] is True
        assert state["gravel_hits"] == 2
        assert all(d.get("stop_on") == ["death"] for d in state["dig_bodies"])

        h_res = skills.harvest_logs(target_logs=2, radius=16)
        assert h_res["ok"] is True
        assert h_res["gained"] == {"oak_log": 2}
    finally:
        bot.close()


def test_text_tool_calls_fallback_and_window_open_adjacent_table_recovery() -> None:
    import io

    from mc_agent.harness.model import _extract_text_tool_calls, _stream_message

    sse_text = (
        "先查桶的合成配方。\n\ntool_calls:\n"
        '```json\n[{"name": "mc_kb_lookup", "arguments": {"query": "bucket"}}]\n```'
    )
    chunk1 = json.dumps({"choices": [{"delta": {"content": sse_text}, "finish_reason": None}]})
    chunk2 = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    raw_sse = "\n".join([f"data: {chunk1}", "", f"data: {chunk2}", "", "data: [DONE]", ""]).encode("utf-8")
    events: list[dict[str, Any]] = []
    msg = _stream_message(io.BytesIO(raw_sse), events.append)
    assert msg["role"] == "assistant"
    assert msg["content"] == "先查桶的合成配方。"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "mc_kb_lookup"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"query": "bucket"}

    # Normal non-mc prose JSON code block must NOT be parsed as tool_calls
    prose = '示例：\n```json\n{"status": "ok"}\n```'
    cleaned_prose, extracted_none = _extract_text_tool_calls(prose)
    assert cleaned_prose == prose
    assert extracted_none == []

    state: dict[str, Any] = {
        "pos": [0.5, 101.0, -2.5],
        "items": [
            {"name": "cobblestone", "count": 6, "slot": 0},
            {"name": "stick", "count": 4, "slot": 1},
            {"name": "oak_planks", "count": 4, "slot": 2},
        ],
        "dug": [],
        "placed": [],
        "blocks": {},
        "buried_tables": {
            (-1, 101, -3): 1.8,
            (-2, 101, -3): 2.7,
            (3, 101, -2): 3.0,
        },
        "stone_pick_attempts": 0,
        "table_crafts": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"pos": list(state["pos"]), "health": 20.0, "food": 20, "alive": True, "ready": True}},
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/find_blocks":
            if "crafting_table" in set(body.get("types") or []):
                blocks = [
                    {"name": "crafting_table", "pos": list(k), "dist": d}
                    for k, d in sorted(state["buried_tables"].items(), key=lambda kv: kv[1])
                ]
                return httpx.Response(200, json={"ok": True, "result": {"blocks": blocks}})
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            if key in state["blocks"]:
                name = state["blocks"][key]
            elif key[1] <= 100 or key == (1, 101, -3):
                name = "stone"
            else:
                name = "air"
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "pos": list(key)}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            if key in state["buried_tables"]:
                assert body.get("stop_on") == ["death"]
            state["dug"].append(list(key))
            state["buried_tables"].pop(key, None)
            state["blocks"][key] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": "block", "gained": {}}})
        if path == "/move_to":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"])}})
        if path == "/place":
            key = tuple(int(v) for v in body["pos"])
            state["placed"].append((body["item"], list(key)))
            state["blocks"][key] = body["item"]
            state["items"] = [it for it in state["items"] if it["name"] != body["item"]]
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"], "pos": list(key)}})
        if path == "/craft":
            if body["item"] == "crafting_table":
                state["table_crafts"] += 1
                state["items"] = [it for it in state["items"] if it["name"] != "oak_planks"]
                state["items"].append({"name": "crafting_table", "count": 1, "slot": 3})
                return httpx.Response(200, json={"ok": True, "result": {"crafted": "crafting_table", "gained": 1}})
            if body["item"] == "stone_pickaxe":
                state["stone_pick_attempts"] += 1
                if state["stone_pick_attempts"] == 1:
                    return httpx.Response(
                        400,
                        json={"ok": False, "error": {"code": "craft_timeout", "message": "Event windowOpen did not fire within 20000ms"}},
                    )
                state["items"].append({"name": "stone_pickaxe", "count": 1, "slot": 4})
                return httpx.Response(200, json={"ok": True, "result": {"crafted": "stone_pickaxe", "gained": 1}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-table",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        skills = MacroSkills(bot)
        res = skills.craft_gear("stone_pickaxe", count=1)
        assert res["ok"] is True
        assert state["stone_pick_attempts"] == 2
        assert state["table_crafts"] == 1
        assert [-1, 101, -3] in state["dug"]
        assert [-2, 101, -3] in state["dug"]
        assert [3, 101, -2] not in state["dug"]
        assert any(item == "crafting_table" for item, _ in state["placed"])
    finally:
        bot.close()


def test_water_escape_and_underwater_mining_protection() -> None:
    state: dict[str, Any] = {
        "pos": [-169.3, 62.5, -390.7],
        "in_water": True,
        "submerged": False,
        "on_ground": False,
        "jumping": False,
        "oxygen": 230,
        "blocks": {
            # East/West/North around [-170, 62, -391] are open water
            (-169, 62, -391): "water",
            (-171, 62, -391): "water",
            (-170, 62, -392): "water",
            # South bank has solid stone base at y=62 and dirt blocking head at y=63
            (-170, 61, -390): "stone",
            (-170, 62, -390): "stone",
            (-170, 63, -390): "dirt",
            (-170, 64, -390): "air",
            # Underwater ore covered by water
            (-168, 59, -393): "iron_ore",
            (-168, 60, -393): "water",
            (-168, 61, -393): "water",
            # Dry ore
            (-160, 63, -390): "iron_ore",
            (-160, 64, -390): "air",
            # Hurt-interrupted ore
            (-160, 62, -389): "iron_ore",
            (-160, 63, -389): "air",
        },
        "dug": [],
        "controls": [],
        "postures": [],
        "items": [
            {"name": "stone_pickaxe", "count": 1, "slot": 0},
        ],
        "fail_interrupted_on": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state["pos"]),
                        "health": 16.0,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "in_water": state["in_water"],
                        "submerged": state["submerged"],
                        "on_ground": state["on_ground"],
                        "jumping": state["jumping"],
                        "oxygen": state["oxygen"],
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(state["items"])}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"held": {"name": body["item"]}}})
        if path == "/posture":
            state["postures"].append(dict(body))
            state["jumping"] = bool(body.get("jump", False))
            return httpx.Response(200, json={"ok": True, "result": dict(body)})
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            name = state["blocks"].get(key, "air")
            diggable = name not in {"air", "water", "flowing_water", "bedrock"}
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": diggable}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            state["dug"].append(key)
            if state["fail_interrupted_on"] == key:
                state["pos"] = [-169.3, 62.5, -390.7]
                state["in_water"] = True
                state["on_ground"] = False
                return httpx.Response(
                    409,
                    json={"ok": False, "error": {"code": "interrupted", "message": "stopped because of hurt"}},
                )
            bname = state["blocks"].pop(key, "stone")
            gained = {"raw_iron": 1} if bname == "iron_ore" else {}
            if gained:
                state["items"].append({"name": "raw_iron", "count": 1, "slot": 1})
            return httpx.Response(200, json={"ok": True, "result": {"dug": bname, "gained": gained}})
        if path == "/control":
            state["controls"].append(dict(body))
            if body.get("forward") and body.get("jump"):
                state["pos"] = [-169.5, 63.0, -389.5]
                state["in_water"] = False
                state["on_ground"] = True
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(state["pos"]), "moved": 1.2}})
        if path == "/find_blocks":
            dists = {
                (-168, 59, -393): 2.0,
                (-160, 63, -390): 18.0,
                (-160, 62, -389): 20.0,
            }
            avail = [
                {"name": "iron_ore", "pos": list(k), "dist": dists[k]}
                for k in ((-168, 59, -393), (-160, 63, -390), (-160, 62, -389))
                if state["blocks"].get(k) == "iron_ore"
            ]
            return httpx.Response(200, json={"ok": True, "result": {"blocks": avail}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-water",
        bot_username="5Ease_Bot",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. check_and_stabilize triggers float on 300-tick oxygen scale (oxygen=230 < 240)
        stab = check_and_stabilize(bot)
        assert "posture_water_float" in stab["actions"]
        assert state["jumping"] is True

        # 2. unstick() in water keeps solid bank step (-170, 62, -390), digs (-170, 63, -390), hops onto dry land & resets jump
        skills = MacroSkills(bot)
        u_res = skills.unstick()
        assert u_res["ok"] is True
        assert u_res["escaped_water"] is True
        assert u_res["in_water"] is False
        assert (-170, 62, -390) not in state["dug"]
        assert (-170, 63, -390) in state["dug"]
        assert state["controls"] and abs(abs(state["controls"][0]["yaw"]) - 3.14159) < 0.35
        assert state["postures"][-1]["jump"] is False

        # 3. mine_blocks() skips water-covered [-168, 59, -393] (blacklisting it) and mines dry [-160, 63, -390]
        state["dug"].clear()
        state["pos"] = [-166.5, 61.0, -392.5]
        state["in_water"] = False
        state["on_ground"] = True
        m_res = skills.mine_blocks(["iron_ore"], count=1, radius=16)
        assert m_res["ok"] is True
        assert (-168, 59, -393) not in skills.protected_positions
        assert skills.temporary_avoidance[(-168, 59, -393)]["reason"] == "water_covered"
        assert (-168, 59, -393) not in state["dug"]
        assert (-160, 63, -390) in state["dug"]

        # 4. interrupted error blacklists [-160, 62, -389] and escapes water if knocked in
        state["dug"].clear()
        state["blocks"].pop((-160, 63, -390), None)
        state["fail_interrupted_on"] = (-160, 62, -389)
        m_err = skills.mine_blocks(["iron_ore"], count=1, radius=16)
        assert m_err["ok"] is False
        assert m_err["partial_error"]["code"] == "interrupted"
        assert (-160, 62, -389) not in skills.protected_positions
        assert skills.temporary_avoidance[(-160, 62, -389)]["reason"] == "interrupted"
        assert m_err.get("water_escape", {}).get("in_water") is False
    finally:
        bot.close()


def test_event_cursor_pagination_wall_los_and_dig_tier_guard() -> None:
    from mc_agent.harness.mc_tools import McToolset

    all_events = [
        {"seq": i, "type": "hurt" if i % 2 else "death", "health": 0, "cause": "arrow", "pos": [-150, 72, -390]}
        for i in range(1, 71)
    ]
    event_queries: list[int] = []
    dug_calls: list[list[int]] = []
    equipped: list[str] = []
    placed_calls: list[dict] = []
    state: dict[str, Any] = {
        "pos": [-166.3, 63.0, -392.1],
        "held": None,
        "items": [
            {"name": "raw_iron", "count": 2, "slot": 0},
            {"name": "furnace", "count": 1, "slot": 1},
            {"name": "coal", "count": 1, "slot": 2},
        ],
        "blocks": {
            # Roofed air pocket ceiling
            (-167, 65, -393): "stone",
            (-167, 66, -393): "stone",
            # Solid wall around (-167, 63, -393) blocking path toward skeleton at (-166.5, 72, -382.5)
            (-167, 64, -392): "stone",
            (-167, 65, -392): "stone",
            (-167, 66, -391): "stone",
            # Target ore & closed pocket walls for carved furnace
            (-165, 63, -392): "iron_ore",
            (-166, 62, -393): "stone",
            (-166, 63, -393): "stone",
        },
        "window_ops": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/events":
            since = int(request.url.params.get("since", "0"))
            limit = int(request.url.params.get("limit", "50"))
            event_queries.append(since)
            page = [ev for ev in all_events if ev["seq"] > since][:limit]
            page_last = page[-1]["seq"] if page else 70
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": page_last, "events": page}})
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state["pos"]),
                        "health": 20.0,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "in_water": False,
                        "nearest_hostile": {"id": 88, "name": "skeleton", "dist": 12.6},
                    },
                },
            )
        if path == "/get_entities":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "entities": [
                            {"id": 88, "type": "hostile", "name": "skeleton", "pos": [-166.5, 72.0, -382.5], "dist": 12.6}
                        ]
                    },
                },
            )
        if path == "/get_block":
            key = tuple(int(v) for v in body["pos"])
            name = state["blocks"].get(key)
            if name is None:
                # Default rock layer above/around pocket unless open air in pocket
                name = "stone" if key[1] >= 65 or key in {(-167, 62, -393), (-166, 62, -393)} else "air"
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "diggable": name != "air"}})
        if path == "/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": list(state["items"]), "held": state["held"]}},
            )
        if path == "/equip":
            equipped.append(body["item"])
            state["held"] = {"name": body["item"], "count": 1}
            return httpx.Response(200, json={"ok": True, "result": {"held": state["held"]}})
        if path == "/dig":
            key = tuple(int(v) for v in body["pos"])
            dug_calls.append(list(key))
            bname = state["blocks"].pop(key, "stone")
            state["blocks"][key] = "air"
            return httpx.Response(200, json={"ok": True, "result": {"dug": bname, "gained": {}}})
        if path == "/place":
            placed_calls.append(dict(body))
            if "pos" not in body:
                return httpx.Response(400, json={"ok": False, "error": {"code": "no_surface", "message": "enclosed"}})
            key = tuple(int(v) for v in body["pos"])
            cur_blk = state["blocks"].get(key, "air")
            if key != (-166, 63, -393) or cur_blk != "air":
                return httpx.Response(400, json={"ok": False, "error": {"code": "blocked", "message": "solid"}})
            state["blocks"][key] = body["item"]
            return httpx.Response(200, json={"ok": True, "result": {"placed": body["item"], "pos": list(key)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/window/act":
            state["window_ops"].append(body)
            return httpx.Response(200, json={"ok": True, "result": {"done": True}})
        if path == "/window/view":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"sections": {"input": [], "output": [{"name": "iron_ingot", "count": 2}]}, "info": {"burning": False}}},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot = BotSession(
        "tok-pocket",
        bot_username="BeastBot_01",
        base_url="http://mc.local/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. Event pagination drains all 70 stale death/hurt events and persists cursor across toolsets
        ts1 = McToolset(bot)
        assert ts1.sync_event_cursor() == 70
        assert event_queries == [0, 50]
        assert ts1.drain_notable_events() == ""

        ts2 = McToolset(bot)
        assert ts2.sync_event_cursor() == 70
        assert event_queries[-1] == 70

        # 2. mc_status() enriches roofed pocket enclosure and ray-marched wall barrier assessment
        st = json.loads(ts2.mc_status())["result"]
        assert st["enclosure"]["roofed_pocket"] is True
        assert st["threat_assessment"]["hostile_blocked_by_wall"] is True
        assert st["threat_assessment"]["safe_in_place"] is False
        assert st["threat_assessment"]["all_checked_hostiles_occluded"] is True

        # 3. mc_dig blocks bare-hand iron_ore mining and auto-equips stone_pickaxe once present
        no_pick = json.loads(ts2.mc_dig(-165, 63, -392))
        assert no_pick["ok"] is False
        assert no_pick["error"]["code"] == "tool_tier_insufficient"
        assert not dug_calls

        state["items"].append({"name": "stone_pickaxe", "count": 1, "slot": 3})
        with_pick = json.loads(ts2.mc_dig(-165, 63, -392))
        assert with_pick["ok"] is True
        assert equipped == ["stone_pickaxe"]
        assert dug_calls == [[-165, 63, -392]]

        # 4. smelt_items carves a furnace pocket in a closed stone room when direct place fails
        smelt_res = ts2.skills.smelt_items("raw_iron", count=2)
        assert smelt_res["ok"] is True
        assert [-166, 63, -393] in dug_calls
        assert any(p.get("item") == "furnace" and p.get("pos") == [-166, 63, -393] for p in placed_calls)
    finally:
        bot.close()


@pytest.mark.parametrize("axis", ["x", "z"])
def test_portal_entry_clears_approach_and_confirms_dimension(monkeypatch, axis) -> None:
    # Reproduce r11: the frame is already lit, move_to reports success while
    # still outside (one block below the portal), then a jump enters the plane.
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    target = [0, 65, 2]
    approach = [0, 65, 1] if axis == "x" else [-1, 65, 2]
    state = {"pos": [0.5, 64.0, 1.5] if axis == "x" else [-0.5, 64.0, 2.5], "contact": None}
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append((path, body))
        if path == "status":
            assert request.method == "GET"
            transferred = state["contact"] is not None and clock[0] - state["contact"] >= 4
            result = {"pos": state["pos"], "dimension": "the_nether" if transferred else "overworld"}
        elif path == "find_blocks":
            # Deliberately give the top row first.
            result = {"blocks": [{"pos": [0, 67, 2]}, {"pos": target}]}
        elif path == "get_block":
            result = {"name": "nether_portal", "properties": {"axis": axis}} if body["pos"] == target else {"name": "wildflowers" if body["pos"] == approach else "air"}
        elif path == "control":
            assert body["jump"] is True
            state["pos"] = [0.5, 65.0, 2.5]
            state["contact"] = clock[0]
            result = {"pos": state["pos"]}
        elif path in {"move_to", "dig", "look"}:
            result = {"pos": state["pos"]}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(wait_s=8)
    assert result["ok"] is True
    assert result["entered_nether"] is True
    assert result["entry_target"] == target
    assert [body["pos"] for path, body in calls if path == "dig"] == [approach]
    assert sum(path == "control" for path, _ in calls) == 1
    assert not any(path in {"place", "inventory"} for path, _ in calls)


@pytest.mark.parametrize("mode,code", [("blocked", "portal_approach_blocked"), ("wrong_elevation", "portal_entry_not_reached"), ("no_transfer", "portal_transfer_timeout")])
def test_portal_entry_returns_recoverable_failure(monkeypatch, mode, code) -> None:
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    controls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 70 if mode == "wrong_elevation" else 65, 2.5], "dimension": "overworld"}
        elif path == "find_blocks":
            result = {"blocks": [{"pos": [0, 65, 2]}]}
        elif path == "get_block":
            result = {"name": "nether_portal" if body["pos"] == [0, 65, 2] else "lava" if mode == "blocked" else "air"}
        elif path == "move_to":
            result = {}  # A successful response is not evidence of portal contact.
        elif path == "control":
            controls.append(body)
            result = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(wait_s=4)
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == code
    if mode == "no_transfer":
        assert result["error"]["contact_seen"] is True
        assert "服务器可能延迟" in result["error"]["message"]
    assert controls == []


@pytest.mark.parametrize("release", [True, False])
def test_move_to_busy_retries_only_after_lease_release(release) -> None:
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "move_to" and calls.count(path) == 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "busy", "message": "running dig"}})
        if path == "move_to":
            result = {"pos": [4.5, 64, 0.5]}
        elif path == "stop":
            result = {"stopped": True, "released": release, "action": "dig"}
        elif path == "status":
            result = {"pos": [0.5, 64, 0.5], "dimension": "overworld"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        tools._is_in_water = lambda: False
        result = json.loads(tools.mc_move_to(x=4.5, z=0.5))
    assert result["ok"] is release
    assert calls.count("stop") == 1
    assert calls.count("move_to") == (2 if release else 1)
    if not release:
        assert result["requires_replan"] is True
        assert result["error"]["code"] == "busy"
        assert "动作锁仍被" in result["error"]["message"]


@pytest.mark.parametrize("timed_out", [False, True])
def test_move_to_far_coordinates_reports_one_waypoint_and_remaining_distance(timed_out) -> None:
    from mc_agent.harness.mc_tools import McToolset

    calls = []
    pos = [0.0, 70.0, 0.0]

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append((route, body))
        if route == "status":
            result = {"pos": list(pos), "dimension": "overworld"}
        elif route == "move_to":
            pos[:] = [6.0, 71.0, 8.0] if timed_out else [body["x"], 71.0, body["z"]]
            if timed_out:
                raise httpx.ReadTimeout("long walk", request=request)
            result = {"pos": list(pos)}
        elif route == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, episode_deadline_mono=time.monotonic() + 60)
        response = json.loads(toolset.mc_move_to(x=96, y=74, z=128))

    move_calls = [body for route, body in calls if route == "move_to"]
    assert len(move_calls) == 1
    assert move_calls[0]["x"] == pytest.approx(19.2)
    assert move_calls[0]["z"] == pytest.approx(25.6)
    assert "y" not in move_calls[0]
    assert response["ok"] is not timed_out
    result = response["result"]
    assert result["partial_move"] is True
    assert result["pos"] == pos
    assert result["target_xz"] == [96, 128]
    assert result["remaining_dist"] == pytest.approx(150.0 if timed_out else 128.0)
    if timed_out:
        assert response["error"]["code"] == "move_timeout"
        assert response["requires_replan"] is True


def test_move_to_fails_fast_when_action_lease_is_busy() -> None:
    """r12 P2-16: busy move_to blocked ~101s before returning."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "busy": "dig"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=4.5, z=0.5))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "action_busy"
    assert result["error"]["action"] == "dig"
    assert calls == ["status"]


def test_move_to_already_in_range_is_success_not_failure() -> None:
    """r15 top Slice C error: movement_not_confirmed at 33/10 bots."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.7, 64.0, 1000.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(
                200,
                json={"ok": False, "error": {"code": "movement_not_confirmed", "message": "no movement"}},
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=1000.8, z=1000.5, range=2.0))
    assert result["ok"] is True
    assert result["result"]["already_in_range"] is True
    assert result["result"]["distance"] < 0.2
    assert calls.count("move_to") == 1


def test_move_to_trusts_server_already_in_range_despite_client_desync() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1002.4, 64.0, 1000.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "error": {
                        "code": "movement_not_confirmed",
                        "message": "当前站位已在目标 range=1m 半径内，未发生实际位移",
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=1000.8, z=1000.5, range=1.0))
    assert result["ok"] is True
    assert result["result"]["already_in_range"] is True


def test_move_to_entity_trusts_server_already_in_range() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "pos_before": [0.5, 64.0, 0.5],
                    "pos_after": [0.5, 64.0, 0.5],
                    "error": {"code": "movement_not_confirmed", "message": "当前站位已在目标 range=1m 半径内，未发生实际位移"},
                },
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(entity_id=77, range=1.0))
    assert result["ok"] is True
    assert result["result"]["already_in_range"] is True
    assert result["result"]["entity_id"] == 77


def test_move_to_entity_reports_stale_id_with_replan_hint() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "busy": None}})
        if path == "move_to":
            return httpx.Response(200, json={"ok": False, "error": {"code": "no_such_entity", "message": "entity 214998 not visible"}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(entity_id=214998, range=15))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert "mc_get_entities" in result["error"]["message"]


def test_move_to_rejects_z_swapped_into_out_of_range_y() -> None:
    """r12 P2-17: y=-2814 was silently dropped, wasting a 90s path search."""
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        raise AssertionError("invalid y must be rejected before any API call")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_move_to(x=-7828, y=-2814, z=-2744))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "bad_args"
    assert "y=-2814" in result["error"]["message"]


@pytest.mark.parametrize("filler", ["stone", "netherrack", "blackstone", "cobblestone"])
def test_staircase_up_uses_available_solid_filler(filler) -> None:
    pos = [0.5, 30.0, 0.5]
    placed = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos, "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": filler, "count": 8}]}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"] == [0, 29, -1] else "air"}
        elif path == "place":
            placed.append(body)
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert placed == [{"item": filler, "pos": [0, 30, -1]}]


def test_staircase_down_stops_before_digging_nether_portal() -> None:
    """r14: the head cell was a nether_portal, so all four directions failed undiggable."""
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 30.0, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": "nether_portal" if body["pos"] == [0, 31, 0] else "air"}
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="down", steps=1)
    assert result["ok"] is False
    assert result["stop_reason"] == "undiggable_block"
    assert result["last_error"]["block"] == "nether_portal"
    assert "dig" not in calls


def test_staircase_up_retries_another_filler_when_server_rejects_item() -> None:
    """r13: count listed cobblestone but place returned missing_item."""
    pos = [0.5, 30.0, 0.5]
    placed = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos, "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}, {"name": "netherrack", "count": 8}]}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"] == [0, 29, -1] else "air"}
        elif path == "place":
            placed.append(body["item"])
            if body["item"] == "cobblestone":
                return httpx.Response(
                    409, json={"ok": False, "error": {"code": "missing_item", "message": "no cobblestone in inventory"}}
                )
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert placed == ["cobblestone", "netherrack"]


def test_staircase_cools_down_stand_after_zero_progress_dig_timeout(monkeypatch) -> None:
    """BUG-041/r23: same stand kept burning 45s per direction with 0 progress."""
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "monotonic", lambda: 1000.0)
    pos = [0.5, 64.0, 0.5]
    digs = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "diamond_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "diamond_pickaxe"}}
        elif path == "get_block":
            result = {"name": "basalt" if body["pos"][1] <= 66 else "air"}
        elif path == "dig":
            digs.append(body["pos"])
            raise httpx.ReadTimeout("offline dig stall", request=request)
        elif path == "move_to":
            result = {"pos": list(pos)}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": list(pos), "moved": 0.0}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        first = skills.dig_staircase(direction="north", vertical="up", steps=3)
        digs_after_first = len(digs)
        second = skills.dig_staircase(direction="south", vertical="up", steps=3)
    assert first["stop_reason"] == "time_budget"
    assert digs_after_first >= 1
    assert second["stop_reason"] == "stair_cell_cooldown"
    assert len(digs) == digs_after_first
    assert "8~12 格" in second["hint"]


def test_staircase_fails_fast_on_previously_timed_out_dig_cell(monkeypatch) -> None:
    """BUG-041/r23: retrying the same timed-out cell burned 15-45s each time."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    clock[0] = 1000.0
    digs = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "stone_pickaxe"}}
        elif path == "get_block":
            result = {"name": "blackstone" if body["pos"][1] <= 66 else "air"}
        elif path == "dig":
            digs.append(body["pos"])
            result = {}
        elif path == "move_to":
            result = {"pos": [0.5, 64.0, 0.5]}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": [0.5, 64.0, 0.5], "moved": 0.0}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills._avoid_temporarily((0, 65, 0), "dig_timeout", 30.0)
        result = skills.dig_staircase(direction="north", vertical="up", steps=3)
    assert result["ok"] is False
    assert result["stop_reason"] == "dig_cell_avoided"
    assert result["last_error"]["code"] == "dig_cell_avoided"
    assert digs == []
    assert "8~12 格" in result["hint"]


def test_staircase_dig_timeout_names_wooden_pickaxe_as_cause() -> None:
    """BUG-041/r22_long: wooden pickaxe + hard rock = ~25s per block, 1 step per call."""

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 36.0, 0.5], "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "wooden_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "wooden_pickaxe"}}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"][1] <= 36 else "air"}
        elif path == "dig":
            raise httpx.ReadTimeout("offline dig too slow", request=request)
        elif path == "move_to":
            result = {"pos": [0.5, 36.0, 0.5]}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": [0.5, 36.0, 0.5], "moved": 0.0}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.dig_staircase(direction="north", vertical="down", steps=1)
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["timeout_path"] == "dig"
    assert result["pickaxe_tier"] == 1
    assert result["low_tier_slow_dig"] is True
    assert "stone_pickaxe" in result["error"]["message"]


def test_staircase_up_falls_back_to_pillar_when_step_has_no_face() -> None:
    """BUG-024: a self-dug shaft step cell has no six-face support."""
    pos = [0.5, 30.0, 0.5]
    jumping = [False]
    placed = [False]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            y = 31.2 if jumping[0] else (31.0 if placed[0] else 30.0)
            result = {"pos": [0.5, y, 0.5], "on_ground": not jumping[0], "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            jumping[0] = bool(body.get("jump"))
            result = {}
        elif path == "place":
            placed[0] = True
            result = {"pos": body["pos"]}
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["stop_reason"] == "pillar_fallback"
    assert result["fallback"] == "pillar"
    assert result["steps_done"] == 1
    assert result["placed_count"] == 1


def test_staircase_up_retries_transient_place_failed() -> None:
    """r14: up-step filler was refused once, then a retry succeeded."""
    pos = [0.5, 30.0, 0.5]
    placement_attempts = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos, "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"] == [0, 29, -1] else "air"}
        elif path == "place":
            placement_attempts.append(body)
            if len(placement_attempts) <= 2:
                return httpx.Response(
                    409, json={"ok": False, "error": {"code": "place_failed", "message": "the block is still air"}}
                )
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert len(placement_attempts) == 3


@pytest.mark.parametrize(
    "timeout_path,episode_budget",
    [("get_block", None), ("move_to", None), ("dig", None), ("dig", 9.0)],
)
def test_staircase_deadline_covers_inner_calls_and_stops_pending_action(monkeypatch, timeout_path, episode_budget) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    blocks = {}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        timeout = request.extensions["timeout"]["read"]
        if path == timeout_path:
            # BUG-037/r24: the step-0 dig may use up to 40s of the session
            # budget instead of the old flat 15s cap; nothing may exceed it.
            limit = 40 if timeout_path == "dig" else 15
            if episode_budget is not None:
                limit = episode_budget - 3.0 - clock[0]
            assert 0 < timeout <= limit + 0.01
            if path == "get_block":
                # Reading safety probes also consumes the same total budget.
                clock[0] += 3
            else:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline pending action", request=request)
        if path == "status":
            result = {"pos": [0.5, 30.0, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": blocks.get(tuple(body["pos"]), "stone")}
        elif path == "dig":
            blocks[tuple(body["pos"])] = "air"
            result = {}
        elif path == "stop":
            assert timeout <= 1
            result = {"stopped": True, "released": True}
        elif path == "move_to":
            result = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        original_call = bot.call
        if episode_budget is None:
            skills = MacroSkills(bot)
            result = skills.dig_staircase(direction="north", steps=25, stop_on_ore=False)
        else:
            toolset = McToolset(bot, geofence=VoidGeofence())
            toolset._episode_deadline_mono = episode_budget
            skills = toolset.skills
            result = json.loads(toolset.mc_skill_staircase(direction="north", steps=6, stop_on_ore=False))
        assert skills.bot is bot
        assert bot.call == original_call
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["action_released"] is True
    assert clock[0] <= 45
    assert calls[-1] == "stop"
    assert calls.count("stop") == 1
    if timeout_path == "dig":
        assert result["dig_target"]
        if episode_budget is None:
            assert "mc_skill_unstick" in result["error"]["message"]
        else:
            assert clock[0] == episode_budget - 3.0
            assert skills.failed_stair_dirs == {}
            assert skills.temporary_avoidance == {}


def test_stair_session_leaves_time_for_obsidian_dig(monkeypatch) -> None:
    """r14: an 8s dig cap made every fortress obsidian step time out."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    dig_timeouts = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        assert path == "dig"
        dig_timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += 9.5
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        session = library._StairSession(bot, 45.0)
        session.call("dig", {"pos": [0, 0, 0]})
    assert session.per_step_timeout == pytest.approx(15.0)
    assert dig_timeouts[0] >= 9.5
    assert session.progress["dug_count"] == 1


def test_staircase_retries_one_slow_read_before_budget_failure(monkeypatch) -> None:
    """r12: a slow first status aborted as time_budget with zero steps."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    calls = [0]
    read_timeouts: list[float] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls[0] += 1
        assert path == "status"
        read_timeouts.append(request.extensions["timeout"]["read"])
        if calls[0] == 1:
            clock[0] += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("slow status", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 30.0, 0.5]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        session = library._StairSession(bot, 10.0)
        status = session.status()
        session.call("status", timeout_s=1.5)
    assert status["pos"] == [0.5, 30.0, 0.5]
    assert calls[0] == 3
    assert session.last_timeout_path is None
    assert session.last_retry_path == "status"
    assert read_timeouts[-1] == pytest.approx(1.5)


def test_staircase_time_budget_reports_timed_out_read(monkeypatch) -> None:
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        if path == "status":
            clock[0] += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("slow status", request=request)
        if path == "stop":
            return httpx.Response(200, json={"ok": True, "result": {"stopped": True, "released": True}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", steps=1, stop_on_ore=False)
    assert result["ok"] is False
    assert result["stop_reason"] == "time_budget"
    assert result["timeout_path"] == "status"
    assert result["retried_path"] == "status"
    assert result["action_released"] is True


@pytest.mark.parametrize("place_fails,code", [(True, "portal_frame_incomplete"), (False, "portal_ignition_failed")])
def test_portal_reports_construction_phase_failures(monkeypatch, place_fails, code) -> None:
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 64, 0.5], "dimension": "overworld"}
        elif path == "inventory":
            result = {"items": [{"name": "obsidian", "count": 10}, {"name": "flint_and_steel", "count": 1}, {"name": "cobblestone", "count": 12}]}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "place" and body["item"] == "obsidian" and place_fails:
            return httpx.Response(200, json={"ok": False, "error": {"code": "place_failed", "message": "obstructed"}})
        elif path in {"place", "move_to", "interact"}:
            result = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(enter=False)
    assert result["ok"] is False
    assert result["error"]["code"] == code
    if place_fails:
        assert result["failed_cells"]
        assert "interact" not in calls


@pytest.mark.parametrize("refills", [1, 100])
def test_staircase_down_verifies_refilled_floor_before_moving(monkeypatch, refills) -> None:
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    pos = [0.5, 30.0, 0.5]
    digs = []
    moves = []
    floor = [0, 29, -1]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            cell = body["pos"]
            name = "stone" if cell[1] <= 28 else "air"
            if cell == floor:
                name = "gravel" if len(digs) <= refills else "air"
            result = {"name": name}
        elif path == "dig":
            assert body["pos"] == floor
            digs.append(body)
            result = {}
        elif path == "move_to":
            moves.append(body)
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", steps=1, stop_on_ore=False)
    if refills == 1:
        assert result["ok"] is True
        assert len(digs) == 2
        assert len(moves) == 1
    else:
        assert result["ok"] is False
        assert result["stop_reason"] == "dig_failed"
        assert result["last_error"]["code"] == "stair_cell_not_cleared"
        assert len(digs) == 3
        assert moves == []


@pytest.mark.parametrize("used,gained,ok", [({}, {"lava_bucket": 1}, True), ({"bucket": 1}, {}, True), ({"bucket": 1}, {"lava_bucket": 1}, True), ({}, {}, False)])
def test_bucket_interact_inventory_delta_overrides_stale_block_snapshot(used, gained, ok) -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            result = {"pos": [0.5, 64, 0.5]}
        elif path == "get_block":
            result = {"name": "lava", "properties": {"level": 0}}
        elif path == "interact":
            result = {"before": "lava", "after": "lava", "changed": False, "used": used, "gained": gained}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_interact(x=1, y=64, z=0, item="bucket"))
    assert result["ok"] is ok
    if not ok:
        assert result["error"]["code"] == "interact_no_effect"
    else:
        assert result["result"]["gained"] == gained
        assert "error" not in result


@pytest.mark.parametrize(
    "item,level,changed,used,gained,filled",
    [
        ("glass_bottle", 1, False, {}, {}, False),
        ("glass_bottle", 8, False, {}, {}, False),
        ("glass_bottle", 0, False, {"glass_bottle": 1}, {}, False),
        ("glass_bottle", 0, True, {"glass_bottle": 1}, {"kelp": 1}, False),
        ("glass_bottle", 0, False, {"glass_bottle": 1}, {"potion": 1}, True),
        ("minecraft:glass_bottle", 0, False, {"glass_bottle": 1}, {"potion": 1}, True),
    ],
)
def test_glass_bottle_interact_with_water_requires_confirmed_fill(
    item, level, changed, used, gained, filled
) -> None:
    from mc_agent.harness.mc_tools import McToolset

    receipt = {
        "block": "water",
        "before": {"level": level},
        "after": {"name": "water", "properties": {"level": level}},
        "changed": changed,
    }
    if used:
        receipt["used"] = used
    if gained:
        receipt["gained"] = gained

    def handler(request):
        assert request.url.path.endswith("/interact")
        assert json.loads(request.content)["item"] == item
        return httpx.Response(200, json={"ok": True, "result": receipt})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_interact(x=1, y=64, z=0, item=item))

    assert result["ok"] is filled
    assert result["result"] == receipt
    if filled:
        assert "error" not in result
    else:
        assert result["requires_replan"] is True
        assert result["error"]["code"] == "interact_no_effect"
        assert "level=0" in result["error"]["message"]


@pytest.mark.parametrize(
    "item,interact_target",
    [
        ("fishing_rod", {"x": 1, "y": 62, "z": 3}),
        ("fishing_rod", {}),
        ("minecraft:fishing_rod", {"x": 1, "y": 62, "z": 3}),
    ],
)
def test_interact_with_fishing_rod_recommends_cast_at_open_water(item, interact_target) -> None:
    from mc_agent.harness.mc_tools import McToolset

    routes = []
    receipt = {"caught": {"cod": 1}, "casts": 1}

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        routes.append(route)
        if route == "interact":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"block": "water", "changed": False}},
            )
        assert route == "use_item"
        assert json.loads(request.content) == {"item": "fishing_rod", "target": {"pos": [1, 62, 3]}}
        return httpx.Response(200, json={"ok": True, "result": receipt})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        rejected = json.loads(toolset.mc_interact(item=item, **interact_target))
        assert rejected["ok"] is False
        assert rejected["requires_replan"] is True
        assert rejected["error"]["code"] == "use_item_api"
        assert "mc_use_item(item='fishing_rod'" in rejected["error"]["message"]
        assert routes == []

        cast = json.loads(toolset.mc_use_item(item="fishing_rod", x=1, y=62, z=3))

    assert routes == ["use_item"]
    assert cast == {"ok": True, "result": receipt}


def test_window_view_rejects_entity_at_12m_before_no_window() -> None:
    """r16 BUG-027: goNear silently failed, then no_window from 12m."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 81197, "pos": [100, 64, 100], "dist": 12.2}]}},
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_window_view(entity_id=81197))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "out_of_reach"
    assert result["error"]["dist"] == pytest.approx(12.2)
    assert "mc_move_to" in result["error"]["message"]
    assert calls == ["get_entities"]


def test_window_view_allows_near_entity() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "get_entities":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"entities": [{"id": 5, "pos": [1, 64, 1], "dist": 2.0}]}},
            )
        if path == "view":
            return httpx.Response(
                200, json={"ok": True, "result": {"window": {"type": "minecraft:merchant"}, "target": {"entity_id": 5}}}
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_window_view(entity_id=5))
    assert result["ok"] is True


def test_smelt_missing_fuel_gives_harvest_next_action() -> None:
    """P2-28/r20_long: bare 'no fuel' left ds_1_bare stuck in bootstrap_iron_age."""

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20}})
        if path == "inventory":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"items": [{"name": "raw_iron", "count": 3, "slot": 36}]}},
            )
        if path == "find_blocks":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"blocks": [{"name": "furnace", "pos": [1, 64, 0], "dist": 0.5}]}},
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).smelt_items("raw_iron", count=3)
    assert result["ok"] is False
    assert result["error"] == "missing_fuel"
    assert result["requires_replan"] is True
    assert result["next_action"] == "mc_skill_harvest_logs(target_logs=2)"
    assert result["inventory"]["raw_iron"] == 3
    assert "mc_skill_harvest_logs" in result["message"]


@pytest.mark.parametrize("episode_budget,cap", [(None, 55.0), (14.0, 10.0)])
def test_skill_smelt_collects_partial_output_within_tool_budget(monkeypatch, episode_budget, cap) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    operations = []

    def handler(request):
        route = request.url.path
        body = json.loads(request.content) if request.content else {}
        timeout = request.extensions["timeout"]["read"]
        if route.endswith("/inventory"):
            result = {"items": [{"name": "raw_copper", "count": 7}, {"name": "coal", "count": 1}]}
        elif route.endswith("/find_blocks"):
            assert timeout <= cap
            clock[0] += 2.0
            result = {"blocks": [{"name": "furnace", "pos": [1, 64, 0], "dist": 1.0}]}
        elif route.endswith("/status"):
            result = {"pos": [0.5, 64, 0.5], "health": 20, "food": 20}
        elif route.endswith("/window/view"):
            result = {
                "sections": {"input": [{"name": "raw_copper", "count": 5}], "output": [{"name": "copper_ingot", "count": 2}]},
                "info": {"burning": True},
            }
        elif route.endswith("/window/act"):
            operations.extend(body["ops"])
            result = {}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset._episode_deadline_mono = episode_budget
        outcome = json.loads(toolset.mc_skill_smelt("raw_copper", count=7))

    assert clock[0] <= cap
    assert outcome["ok"] is False
    assert outcome["error"] == "partial_smelt"
    assert outcome["requires_replan"] is True
    assert outcome["collected"] == [{"name": "copper_ingot", "count": 2}]
    assert operations[-1] == {"op": "take", "from": "output", "count": 2}


class _SurfaceScoutBackend:
    def __init__(self) -> None:
        self.pos = [0.5, 64, 0.5]
        self.biome = "plains"
        self.health = 20
        self.wet = False
        self.submerged = False
        self.hostile = None
        self.moves = []
        self.calls = []
        self.target = None
        self.move = self.arrive
        self.released = True

    def arrive(self, request, body):
        self.pos = [body["x"], body.get("y", 64), body["z"]]
        return self.ok({"pos": self.pos})

    @staticmethod
    def ok(result):
        return httpx.Response(200, json={"ok": True, "result": result})

    def handle(self, request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((route, self.pos[:], body))
        if route == "status":
            return self.ok({
                "pos": self.pos,
                "dimension": "overworld",
                "biome": self.biome,
                "health": self.health,
                "in_water": self.wet,
                "submerged": self.submerged,
                "nearest_hostile": self.hostile,
            })
        if route == "get_block":
            return self.ok({"name": "grass_block"})
        if route in {"find_blocks", "get_entities"}:
            assert body["radius"] == 64
            rows = []
            if self.pos[0] > 50 and self.target in body["types"]:
                row = {"name": self.target, "pos": [62, 64, 1], "biome": "meadow"}
                if route == "get_entities":
                    row["id"] = 17
                rows.append(row)
            if self.pos[0] > 20 and "obsidian" in body["types"]:
                rows.append({"name": "obsidian", "pos": [41, 63, 0]})
            key = "blocks" if route == "find_blocks" else "entities"
            return self.ok({key: rows})
        if route == "move_to":
            assert ("y" in body) is (body["range"] == 0.5)
            assert body["stop_on"] == ["hurt", "death"]
            self.moves.append(body)
            return self.move(request, body)
        if route == "stop":
            return self.ok({"stopped": True, "action": "move_to", "released": self.released})
        raise AssertionError(route)


@pytest.mark.parametrize("target,segments", [(None, 5), ("dirt_path", 5), ("bell", 2), ("villager", 2)])
def test_surface_scout_continues_and_scans_until_target(target, segments) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()
    backend.target = target
    arguments = {"target_types": ["minecraft:villager"]} if target == "villager" else {}
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        registry = toolset.registry(profile="compact", extra_tools=("mc_skill_scout_surface",))
        result = json.loads(registry.execute({
            "function": {"name": "mc_skill_scout_surface", "arguments": json.dumps(arguments)},
        }))
        assert toolset._last_block_pos == [28 * segments, 64, 0]

    assert result["ok"] is True
    hit = target in {"bell", "villager"}
    assert result["stop_reason"] == ("target_found" if hit else "segment_limit")
    assert result["segments_completed"] == len(backend.moves) == segments
    assert result["distance_traveled"] == 28 * segments
    assert result["final_pos"] == [28 * segments + 0.5, 64, 0.5]
    assert sum(route == "get_entities" for route, _, _ in backend.calls) == segments + 1
    assert [row["name"] for row in result["detected_pois"]].count("obsidian") == 1
    if hit:
        key = "detected_entities" if target == "villager" else "detected_pois"
        match = next(row for row in result[key] if row["name"] == target)
        assert match["pos"] == [62, 64, 1]
        assert match["dist"] == 5.5
        assert match["biome"] == "meadow"
        assert match["observer_biome"] == "plains"


@pytest.mark.parametrize("entry", ["find_blocks", "scout_surface"])
def test_village_scan_retains_beds_and_hay_amid_path_blocks(entry) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    crowded = [
        {"name": "dirt_path", "pos": [i % 10, 63, i // 10], "dist": 1.0}
        for i in range(200)
    ]
    anchors = [
        {"name": "hay_block", "pos": [2, 64, 0], "dist": 2.1},
        {"name": "yellow_bed", "pos": [3, 64, 0], "dist": 3.1},
    ]
    block_queries = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "find_blocks":
            block_queries.append(body["types"])
            rows = [row for row in [*crowded, *anchors] if row["name"] in body["types"]]
            result = {"blocks": rows[:body["limit"]], "total": len(rows)}
        elif route == "status":
            result = {"pos": [0.5, 64, 0.5], "biome": "plains"}
        elif route == "get_entities":
            result = {"entities": []}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        if entry == "find_blocks":
            out = json.loads(toolset.mc_find_blocks(
                radius=64, types=["dirt_path", "hay_block", "bed"], limit=20, mature=False,
            ))["result"]
            assert out["total"] == 202
            assert out["returned"] == len(out["blocks"]) == 20
            assert out["village_landmarks_prioritized"] is True
            assert ["hay_block"] in block_queries[1:]
            rows = out["blocks"]
        else:
            out = json.loads(toolset.mc_skill_scout_surface())
            assert out["stop_reason"] == "target_found"
            assert out["segments_completed"] == 0
            assert "dirt_path" not in block_queries[0]
            rows = out["detected_pois"]
        assert [row["name"] for row in rows[:2]] == ["hay_block", "yellow_bed"]
        remembered = toolset.world_memory.query_pois(["hay_block", "bed"], origin=[0.5, 64, 0.5], dim="overworld")
        assert {"hay_block", "yellow_bed"} <= {row["name"] for row in remembered}


@pytest.mark.parametrize(
    "target_types,chest_y,village_marker,expected_segments",
    [(None, 15, None, 5), (None, 64, None, 5), (None, 15, "bell", 2), (["chest"], 15, None, 0)],
)
def test_surface_scout_keeps_chest_observations_without_ending_village_search(
    target_types, chest_y, village_marker, expected_segments,
) -> None:
    backend = _SurfaceScoutBackend()
    backend.target = village_marker
    chest_queries = []

    def handle(request):
        if request.url.path.endswith("/find_blocks"):
            types = json.loads(request.content)["types"]
            if "chest" in types:
                chest_queries.append(types)
                return backend.ok({"blocks": [{"name": "chest", "pos": [1, chest_y, 0]}]})
            if types == ["dirt_path"]:
                return backend.ok({"blocks": [{"name": "dirt_path", "pos": [2, 64, 0]}]})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east", target_types=target_types)

    assert result["segments_completed"] == len(backend.moves) == expected_segments
    assert chest_queries and all(types == ["chest"] for types in chest_queries)
    observed = {row["name"]: row for row in result["detected_pois"]}
    assert observed["chest"]["height_delta"] == chest_y - 64
    assert result["stop_reason"] == ("target_found" if target_types or village_marker else "segment_limit")
    if target_types is None:
        assert "dirt_path" in observed


@pytest.mark.parametrize("valid_targets", [[], ["bell", "villager"]])
def test_surface_scout_recovers_from_unknown_block_in_target_list(valid_targets) -> None:
    backend = _SurfaceScoutBackend()
    backend.target = "bell"
    rejected = "d食物_path"
    queries = []

    def handle(request):
        if request.url.path.endswith("/find_blocks"):
            types = json.loads(request.content)["types"]
            queries.append(types)
            if rejected in types:
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "bad_args", "message": f"unknown block {rejected}"},
                })
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(target_types=[rejected, *valid_targets])

    assert result["ignored_types"] == [rejected]
    assert all(rejected not in query for query in queries[1:])
    if valid_targets:
        assert result["stop_reason"] == "target_found"
        assert result["segments_completed"] == 2
        assert any(row["name"] == "bell" for row in result["detected_pois"])
    else:
        assert result["stop_reason"] == "invalid_target_types"
        assert not backend.moves


def test_surface_scout_detours_around_fence_and_server_timeout() -> None:
    from mc_agent.harness.geofence import VoidGeofence

    backend = _SurfaceScoutBackend()
    backend.pos = [15.5, 64, 0.5]
    fence = VoidGeofence(radius=42, margin=0)

    def move(request, body):
        if len(backend.moves) == 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "timeout", "message": "path timeout"}})
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot, geofence=fence).scout_surface(direction="east")

    assert result["segments_completed"] >= 1
    assert backend.moves[0]["z"] < 0 < backend.moves[1]["z"]
    assert all(fence.check_point(body["x"], body["z"]) is None for body in backend.moves)


def test_surface_scout_checks_for_water_before_moving() -> None:
    backend = _SurfaceScoutBackend()

    def handle(request):
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"] == [14, 63, 0]:
            return backend.ok({"name": "water"})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert result["segments_completed"] == 5
    assert backend.moves[0]["z"] < 0
    assert len(backend.moves) == 5


@pytest.mark.parametrize("direction", ["auto", "east"])
def test_surface_scout_turns_inward_at_geofence_edge(direction) -> None:
    from mc_agent.harness.geofence import VoidGeofence

    backend = _SurfaceScoutBackend()
    backend.pos = [39.5, 64, 0.5]
    fence = VoidGeofence(radius=42, margin=0)
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot, geofence=fence).scout_surface(direction=direction)

    assert result["segments_completed"] > 0
    assert backend.moves[0]["x"] < 39.5
    assert all(fence.check_point(move["x"], move["z"]) is None for move in backend.moves)
    if direction == "auto":
        assert backend.moves[0]["z"] == 0.5


def test_surface_scout_turns_ninety_degrees_along_dry_bank() -> None:
    backend = _SurfaceScoutBackend()

    def handle(request):
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"][0] > 2:
            return backend.ok({"name": "water"})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert result["segments_completed"] == 5
    assert backend.moves[0]["x"] == 0.5
    assert backend.moves[0]["z"] == -27.5


@pytest.mark.parametrize("submerged", [False, True])
def test_surface_scout_can_take_one_short_step_out_of_shallow_water(submerged) -> None:
    backend = _SurfaceScoutBackend()
    backend.wet = True
    backend.submerged = submerged
    surfacing = []

    def handle(request):
        if request.url.path.endswith("/control"):
            surfacing.append(json.loads(request.content))
            backend.submerged = False
            return backend.ok({})
        if request.url.path.endswith("/find_blocks") and json.loads(request.content)["radius"] == 10:
            return backend.ok({"blocks": [{"name": "grass_block", "pos": [0, 63, 7]}]})
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"] in ([0, 64, 7], [0, 65, 7]):
            return backend.ok({"name": "air"})
        return backend.handle(request)

    def move(request, body):
        backend.wet = False
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert backend.moves[0]["range"] == 0.5
    assert [backend.moves[0][axis] for axis in ("x", "y", "z")] == [0.5, 64, 7.5]
    assert bool(surfacing) is submerged
    assert len(surfacing) <= 1
    assert result["shore_recoveries"] == 1
    assert result["segments_completed"] == 5
    assert result["distance_traveled"] == 147


@pytest.mark.parametrize("profile", ["full", "compact"])
def test_surface_scout_tool_registry_accepts_numeric_strings(profile) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()
    backend.biome = "deep_ocean"
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        registry = McToolset(bot, geofence=VoidGeofence()).registry(
            profile=profile, extra_tools=("mc_skill_scout_surface",),
        ).map_tools(lambda tool: tool)
        result = json.loads(registry.execute({
            "function": {
                "name": "mc_skill_scout_surface",
                "arguments": '{"direction":"east","max_duration_s":"45","step_dist":"30"}',
            },
        }))

    assert result["segments_completed"] == 5
    assert result["distance_traveled"] == 150
    assert result["biome"] == "deep_ocean"
    assert "方向" in result["biome_hint"]


@pytest.mark.parametrize("released", [True, False])
def test_surface_scout_confirmed_stop_before_retry_after_read_timeout(released) -> None:
    backend = _SurfaceScoutBackend()
    backend.released = released

    def move(request, body):
        if len(backend.moves) == 1:
            raise httpx.ReadTimeout("offline movement timeout", request=request)
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    actions = [route for route, _, _ in backend.calls if route in {"move_to", "stop"}]
    if released:
        assert actions[:3] == ["move_to", "stop", "move_to"]
        assert backend.moves[1]["z"] < 0
        assert result["segments_completed"] == 5
    else:
        assert actions == ["move_to", "stop"]
        assert result["stop_reason"] == "move_timeout_unreleased"
        assert result["requires_replan"] is True


@pytest.mark.parametrize("interrupted", [False, True])
def test_surface_scout_water_retreat_yields_to_hurt(interrupted) -> None:
    backend = _SurfaceScoutBackend()

    def move(request, body):
        backend.wet = len(backend.moves) == 1
        if backend.wet:
            body = {**body, "x": 3.5}
        receipt = backend.arrive(request, body)
        if backend.wet and interrupted:
            return backend.ok({"pos": backend.pos, "interrupted_by": "hurt"})
        return receipt

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    if interrupted:
        assert result["stop_reason"] == "hurt"
        assert len(backend.moves) == 1
    else:
        assert [backend.moves[1][axis] for axis in ("x", "z")] == [0.5, 0.5]
        assert backend.moves[2]["z"] < 0
        assert result["segments_completed"] == 5


def test_surface_scout_stops_for_hostile_at_landing() -> None:
    backend = _SurfaceScoutBackend()

    def move(request, body):
        backend.hostile = {"dist": 6.0, "name": "zombie"}
        return backend.arrive(request, body)

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east")

    assert result["stop_reason"] == "nearby_hostile"
    assert result["distance_traveled"] == 28
    assert len(backend.moves) == 1
    assert all(pos[0] == 0.5 for route, pos, _ in backend.calls if route == "find_blocks")


@pytest.mark.parametrize("episode_remaining,cap", [(None, 50.0), (18.0, 14.0), (5.0, 0.0)])
def test_surface_scout_clamps_entire_tool_budget(monkeypatch, episode_remaining, cap) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])

    def handle(request):
        timeout = request.extensions["timeout"]["read"]
        assert 0 < timeout <= cap - clock[0]
        if request.url.path.endswith("/move_to"):
            body = json.loads(request.content)
            assert body["timeout"] / 1000 < timeout
            step_time = min(10.0, body["timeout"] / 1000)
            clock[0] += step_time
            if step_time < 10.0:
                return httpx.Response(200, json={"ok": False, "error": {"code": "timeout", "message": "short hop"}})
        else:
            clock[0] += min(0.2, timeout)
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        toolset._episode_deadline_mono = episode_remaining
        result = json.loads(toolset.mc_skill_scout_surface(max_duration_s=120.0))

    assert clock[0] <= cap
    if cap == 0:
        assert result["stop_reason"] == "episode_deadline_imminent"
        assert backend.calls == []
    else:
        assert result["stop_reason"] == "time_budget"
        assert result["segments_completed"] == (4 if cap == 50 else 1)


@pytest.mark.parametrize("episode_remaining", [5.0, 13.0])
def test_dig_shares_episode_budget_across_pickaxe_preflight(monkeypatch, episode_remaining) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        calls.append(route)
        if route == "dig":
            assert timeout == pytest.approx(5.0)
            clock[0] += timeout
            raise httpx.ReadTimeout("offline slow dig", request=request)
        results = {
            "get_block": {"name": "iron_ore"},
            "inventory": {"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}]},
            "equip": {"equipped": "stone_pickaxe"},
            "stop": {"released": True},
        }
        clock[0] += min(1.0, timeout)
        return httpx.Response(200, json={"ok": True, "result": results[route]})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        tools._episode_deadline_mono = episode_remaining
        result = json.loads(tools.mc_dig(1, 64, 1))

    assert result["ok"] is False
    assert clock[0] <= episode_remaining
    if episode_remaining == 5.0:
        assert calls == []
        assert result["stop_reason"] == "episode_deadline_imminent"
    else:
        assert calls == ["get_block", "inventory", "equip", "dig", "stop"]
        assert result["stop_reason"] == "time_budget"
        assert result["stop_confirmed"] is True


@pytest.mark.parametrize("slow_route", ["craft", "equip"])
def test_craft_gear_shares_episode_budget_and_keeps_completed_craft(monkeypatch, slow_route) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        assert 0.0 < timeout <= 8.0 - clock[0]
        calls.append(route)
        if route == slow_route:
            assert timeout == pytest.approx(7.0 - clock[0])
            clock[0] += timeout
            raise httpx.ReadTimeout(f"offline slow {route}", request=request)
        results = {
            "inventory": {"items": [{"name": "oak_planks", "count": 8, "slot": 0}]},
            "craft": {"gained": {"stick": 4}},
            "stop": {"released": True},
        }
        clock[0] += min(1.0, timeout)
        return httpx.Response(200, json={"ok": True, "result": results[route]})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        tools._episode_deadline_mono = 12.0
        result = json.loads(tools.mc_skill_craft_gear("stick", equip_slot="hand"))

    assert calls[-2:] == [slow_route, "stop"]
    assert calls.count("craft") == 1
    assert clock[0] <= 12.0
    if slow_route == "craft":
        assert result["ok"] is False
        assert result["stop_reason"] == "time_budget"
    else:
        assert result["ok"] is True
        assert result["gained"] == {"stick": 4}
        assert result["deadline_hit"] is True
        assert result["equipped_slot"] is None


def test_smelt_collect_survives_read_timeout() -> None:
    """BUG-035: a transient ReadTimeout in world-job collection killed the run."""
    from mc_agent.harness.world_memory import WorldMemory

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.5, 64.0, 1000.5], "dimension": "overworld"}})
        if path == "move_to":
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "view":
            raise httpx.ReadTimeout("offline furnace timeout", request=request)
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills.world_memory = WorldMemory()
        result = skills.smelt_collect(furnace_pos=[1000, 64, 1000], wait_remaining_s=0)
    assert result["ok"] is False
    assert result["failures"]
    assert result["failures"][0]["error"] == "ReadTimeout"


def test_brew_bottle_alias_accepts_water_bottle_shape() -> None:
    from mc_agent.skills.library import _window_item_entries

    entries = _window_item_entries([{"item": "minecraft:water_bottle", "slot": 0}])
    assert entries == [{"item": "potion", "potion": "water", "count": 1, "slot": 0}]


def test_brew_missing_item_reports_inventory_counts() -> None:
    def handler(request):
        path = request.url.path
        if path.endswith("/window/act"):
            return httpx.Response(
                200, json={"ok": False, "error": {"code": "missing_item", "message": "no blaze_powder"}}
            )
        if path.endswith("/inventory"):
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 5}]}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).brew_once(
            "nether_wart",
            bottles=[{"item": "water_bottle"}],
            fuel_item="blaze_powder",
            block="brewing_stand",
            x=10,
            y=64,
            z=10,
            wait=False,
        )
    assert result["ok"] is False
    assert result["error"] == "missing_item"
    assert result["inventory"]["cobblestone"] == 5
    assert "inventory" in result["message"]


def test_brew_no_window_reports_wrong_block_at_coordinates() -> None:
    """r16 brewing used a coordinate that actually held cobblestone."""
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/window/act"):
            return httpx.Response(
                200, json={"ok": False, "error": {"code": "no_window", "message": "cobblestone did not open a GUI"}}
            )
        if path.endswith("/get_block"):
            return httpx.Response(200, json={"ok": True, "result": {"name": "cobblestone"}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).brew_once(
            "nether_wart",
            bottles=[{"item": "potion", "count": 1}],
            block="brewing_stand",
            x=10,
            y=64,
            z=10,
            wait=False,
        )
    assert result["ok"] is False
    assert result["error"] == "window_target_mismatch"
    assert result["actual"] == "cobblestone"
    assert len(calls) == 2
    assert calls[0].endswith("/window/act")
    assert calls[1].endswith("/get_block")


@pytest.mark.parametrize("item,initial,cap", [("cobblestone", 320, 192), ("netherrack", 200, 128), ("dirt", 200, 64)])
def test_free_slots_enforces_retention_cap_even_with_empty_slots(item, initial, cap) -> None:
    """BUG-029: over-cap blocks were never dropped when >=min_free slots remained."""
    state = {item: initial}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            rows = []
            left = state[item]
            while left > 0:
                rows.append({"name": item, "count": min(64, left)})
                left -= min(64, left)
            return httpx.Response(200, json={"ok": True, "result": {"items": rows}})
        if path == "drop":
            state[item] = max(0, state[item] - int(body.get("count") or 0))
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        inv_after, dropped = skills._ensure_free_slots(min_free=3)
    assert dropped
    assert state[item] == cap
    assert all(map(lambda d: d["item"] == item, dropped))


def test_mine_dfs_triggers_retention_cleanup() -> None:
    from mc_agent.harness.mc_tools import McToolset

    state = {"cobblestone": 300}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            rows = []
            left = state["cobblestone"]
            while left > 0:
                rows.append({"name": "cobblestone", "count": min(64, left)})
                left -= min(64, left)
            return httpx.Response(200, json={"ok": True, "result": {"items": rows}})
        if path == "drop":
            state["cobblestone"] -= int(body.get("count") or 0)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset.skills.mine_dfs = lambda **kwargs: {"ok": True, "dug_count": 1}
        result = json.loads(toolset.mc_skill_mine_dfs(target="stone", goal_count=1))
    assert result["inventory_cleaned"]
    assert state["cobblestone"] == 192


def test_staircase_first_step_uses_total_budget(monkeypatch) -> None:
    """BUG-037: a 20s first step must not die at the 15s per-step cap."""
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    pos = [0.5, 30.0, 0.5]
    delayed = [False]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            if body["pos"] == [0, 29, -1] and not delayed[0]:
                delayed[0] = True
                clock[0] += 20.0
                result = {"name": "stone"}
            else:
                result = {"name": "air"}
        elif path == "place":
            result = {"pos": body["pos"]}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1


@pytest.mark.parametrize(
    "episode_budget,cap,slow_action",
    [(None, 35.0, "move_to"), (13.0, 9.0, "place"), (17.0, 13.0, None)],
)
def test_flee_budget_reaches_shaft_waypoints_and_seals(monkeypatch, episode_budget, cap, slow_action) -> None:
    from mc_agent.harness.events import EventSink
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.guardian import GenerationGuard
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.skills.mining_dfs import MiningDFSEngine, ShaftEdge, ShaftNode

    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    pos = [0.5, 30, 2.5]
    moves = []
    placed = []
    actions = []
    start = clock[0]

    def walk_waypoints(engine, waypoints, *, reverse, min_hp_ref):
        assert reverse is True
        x, y, z = waypoints[0]
        engine.bot.call("move_to", {"x": x + 0.5, "y": y, "z": z + 0.5, "timeout": 60_000})
        engine.bot.status()
        return True, None

    monkeypatch.setattr(MiningDFSEngine, "_walk_waypoints", walk_waypoints)

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        assert 0.0 < timeout <= start + cap - clock[0]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos[:], "health": 20, "alive": True, "ready": True}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"][1] < 30 else "air"}
        elif path == "get_entities":
            result = {"entities": []}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 64}]}
        elif path == "move_to":
            actions.append(path)
            moves.append([body["x"], body["y"], body["z"]])
            assert 0 < body["timeout"] / 1000 < timeout
            if len(moves) == 2 and slow_action == path:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline slow shaft waypoint", request=request)
            clock[0] += min(1.0, timeout)
            pos[:] = moves[-1]
            result = {"pos": pos[:]}
        elif path == "place":
            actions.append(path)
            if slow_action == path:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline slow shaft seal", request=request)
            placed.append(tuple(body["pos"]))
            result = {"placed": True}
        elif path == "stop":
            actions.append(path)
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        toolset._episode_deadline_mono = None if episode_budget is None else start + episode_budget
        engine = toolset.skills.mining_dfs
        engine.nodes = {
            1: ShaftNode(1, "collar", (0, 30, 0), None, None, []),
            2: ShaftNode(2, "spine", (0, 30, 1), 1, ShaftEdge("spine_forward", "south", [(0, 30, 0)]), []),
            3: ShaftNode(3, "spine", (0, 30, 2), 2, ShaftEdge("spine_forward", "south", [(0, 30, 1)]), []),
        }
        engine.collar_node_id = 1
        engine.active_node_id = 3
        original_call = bot.call
        guard = GenerationGuard(bot, EventSink(on_event=lambda event: None), checkpoint=lambda: None)
        result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", toolset.mc_skill_flee))
        assert engine.active_node_id == (2 if slow_action else 1)
        assert engine.sealed_positions == set(placed)
        assert toolset.skills.mining_dfs is engine
        assert engine.bot is bot and bot.call == original_call

    assert clock[0] - start <= cap
    assert moves[0] == [0.5, 30, 1.5]
    if slow_action:
        assert result["ok"] is False
        assert result["stop_reason"] == "time_budget"
        assert result["timeout_path"] == slow_action
        assert result["pos_after"] == [0.5, 30, 1.5]
        assert actions[-2:] == [slow_action, "stop"]
    else:
        assert result["ok"] is True
        assert result["mode"] == "retreat_shaft"
        assert result["hops"] == 2
        assert result["sealed"] == 6
        assert "stop" not in actions


def test_staircase_tool_caps_long_steps() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        raise AssertionError("no API call expected")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        captured = {}
        toolset.skills.dig_staircase = lambda **kwargs: captured.update(kwargs) or {"ok": True, "steps_done": 10}
        result = json.loads(toolset.mc_skill_staircase(direction="north", steps=40))
    assert captured["steps"] == 10
    assert result["steps_capped_from"] == 40
    assert result["steps_clamped"] is True


def test_staircase_tool_auto_retries_viable_direction_on_zero_progress(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])

    def handler(request):
        raise AssertionError("no API call expected")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset._episode_deadline_mono = 109.0
        calls = []

        def fake_dig(**kwargs):
            assert kwargs["deadline"] == 106.0
            calls.append(kwargs["direction"])
            if kwargs["direction"] == "north":
                clock[0] += 4.0
                return {
                    "ok": False, "stop_reason": "movement_not_confirmed",
                    "steps_done": 0, "dug_count": 0,
                    "viable_directions": ["south", "east"],
                }
            return {"ok": True, "steps_done": 1, "stop_reason": "completed"}

        toolset.skills.dig_staircase = fake_dig
        result = json.loads(toolset.mc_skill_staircase(direction="north", steps=6))
    assert calls == ["north", "south"]
    assert result["ok"] is True
    assert result["auto_retry_direction"] == "south"


def test_staircase_tool_skips_action_with_no_episode_budget(monkeypatch) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: 100.0)

    def handler(request):
        raise AssertionError("no API call expected")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence(radius=100))
        toolset._episode_deadline_mono = 105.0
        result = json.loads(toolset.mc_skill_staircase(vertical="pillar"))

    assert result["stop_reason"] == "episode_deadline_imminent"
    assert result["steps_done"] == 0


@pytest.mark.parametrize("direction,vertical", [(None, "up"), ("pillar", "pillar")])
def test_staircase_tool_shares_episode_budget_with_direction_and_fence_probes(monkeypatch, direction, vertical) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    paths = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        paths.append(path)
        assert path in {"status", "inventory", "get_block"}
        timeout = request.extensions["timeout"]["read"]
        assert timeout <= 106.0 - clock[0]
        clock[0] += timeout if direction == "pillar" else min(1.5, timeout)
        result = {
            "status": {"pos": [0.5, 30, 0.5], "on_ground": True},
            "inventory": {"items": [{"name": "cobblestone", "count": 64}]},
            "get_block": {"name": "stone"},
        }
        if direction == "pillar":
            raise httpx.ReadTimeout("offline preflight timeout", request=request)
        return httpx.Response(200, json={"ok": True, "result": result[path]})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence(radius=100))
        toolset._episode_deadline_mono = 109.0
        result = json.loads(toolset.mc_skill_staircase(direction=direction, vertical=vertical))

    assert result["stop_reason"] == "time_budget"
    assert result["steps_done"] == 0
    assert clock[0] == 106.0
    assert ("get_block" in paths) is (direction is None)


def test_tunnel_triggers_retention_cleanup() -> None:
    from mc_agent.harness.mc_tools import McToolset

    state = {"cobblestone": 300}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.5, 64.0, 1000.5]}})
        if path == "inventory":
            rows = []
            left = state["cobblestone"]
            while left > 0:
                rows.append({"name": "cobblestone", "count": min(64, left)})
                left -= min(64, left)
            return httpx.Response(200, json={"ok": True, "result": {"items": rows}})
        if path == "drop":
            state["cobblestone"] -= int(body.get("count") or 0)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset.skills.dig_level_tunnel = lambda *args, **kwargs: {"ok": True, "steps_done": 1}
        result = json.loads(toolset.mc_skill_tunnel("north", 1))
    assert result["inventory_cleaned"]
    assert state["cobblestone"] == 192


def test_staircase_step0_dig_uses_budget_beyond_15s(monkeypatch) -> None:
    """BUG-037/r24: step 0 stalled ~28s in dig even with a stone pickaxe."""
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    dig_timeouts = []
    pos = [0.5, 36.0, 0.5]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": list(pos), "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}]}
        elif path == "equip":
            result = {"held": {"name": "stone_pickaxe"}}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"][1] <= 36 else "air"}
        elif path == "dig":
            dig_timeouts.append(request.extensions["timeout"]["read"])
            if len(dig_timeouts) == 1:
                # The first dig stalls ~28s: only the raised step-0 cap can wait.
                clock[0] += 28.0
            result = {}
        elif path == "move_to":
            pos[:] = [body["x"], body["y"], body["z"]]
            result = {"pos": list(pos)}
        elif path == "look":
            result = {}
        elif path == "control":
            result = {"pos": list(pos), "moved": 1.0}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="down", steps=1)
    assert dig_timeouts and dig_timeouts[0] > 15.0
    assert result["steps_done"] >= 1


def test_unstick_sends_stop_and_reports_deadline_hit(monkeypatch) -> None:
    """r22/r23/r24: unstick could run 150~270s; budget must cap it."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    calls: list[str] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "control" and body.get("yaw") == 0.0:
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "in_water": False}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": "stone", "diggable": True}
        elif path == "dig":
            clock[0] += min(20.0, float(request.extensions["timeout"]["read"]))
            result = {}
        elif path == "control":
            result = {"pos": [0.5, 64.0, 0.5], "moved": 0.0}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills.descend_canopy = lambda **kwargs: {"on_canopy": False}
        result = skills.unstick(max_duration_s=45.0)
    assert result["deadline_hit"] is True
    assert result["elapsed_s"] <= 45.5
    assert result["budget_s"] == pytest.approx(45.0)
    assert "不要连续重复调用 unstick" in result["hint"]
    assert "stop" in calls


def test_unstick_shares_deadline_with_water_escape(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    calls = []
    pos = [-7824.7, 110.2, -2771.3]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            result = {"pos": pos, "in_water": True, "jumping": True}
        elif path == "get_block":
            timeout = request.extensions["timeout"]["read"]
            clock[0] += min(2.5, timeout)
            if timeout < 2.5:
                raise httpx.ReadTimeout("water probe exceeded the remaining budget", request=request)
            result = {"name": "water", "diggable": False}
        elif path == "control":
            result = {"pos": pos}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "stop":
            clock[0] += 0.25
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_skill_unstick())
        assert toolset.skills.bot is bot

    assert result["ok"] is False
    assert result["in_water"] is True
    assert result["escaped_water"] is False
    assert result["pos_after"] == pos
    assert result["deadline_hit"] is True
    assert result["requires_replan"] is True
    assert clock[0] <= 45.0
    assert calls[-1] == "stop"


def test_unstick_reports_failure_when_still_in_water() -> None:
    from mc_agent.harness.mc_tools import McToolset

    pos = [-10945.7, 62.3, -11027.2]
    replies = {
        "status": {"pos": pos, "in_water": True, "jumping": True},
        "get_block": {"name": "water", "diggable": False},
        "control": {"pos": pos},
        "find_blocks": {"blocks": []},
    }

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"ok": True, "result": replies[route]})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_unstick())

    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "water_escape_failed"
    assert result["in_water"] is True
    assert result["escaped_water"] is False
    assert result["pos_after"] == pos
    assert "deadline_hit" not in result


@pytest.mark.parametrize("north_has_support", [True, False])
def test_unstick_uses_another_direction_when_east_has_no_landing(north_has_support) -> None:
    from mc_agent.harness.mc_tools import McToolset

    moves = []
    origin = [0.5, 64.0, 0.5]

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": origin, "on_ground": True, "ready": True, "alive": True}
        elif route == "get_block":
            x, y, z = body["pos"]
            ground = y == 63 and x == 0 and (z == 0 or (north_has_support and z < 0))
            result = {"name": "stone" if ground else "air", "diggable": ground}
        elif route == "control":
            moves.append(body)
            result = {"pos": [0.5, 64.0, -0.5], "moved": 1.0}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_unstick())

    assert result["ok"] is north_has_support
    if north_has_support:
        assert len(moves) == 1
        assert moves[0]["yaw"] == pytest.approx(0.0)
        assert moves[0]["jump"] is False
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert result["requires_replan"] is True
        assert moves == []


def test_unstick_fast_path_has_no_deadline_flag() -> None:
    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "in_water": False}
        elif path == "get_block":
            y = json.loads(request.content)["pos"][1]
            result = {"name": "stone" if y == 63 else "air", "diggable": False}
        elif path == "control":
            result = {"pos": [1.5, 64.0, 0.5], "moved": 1.0}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).unstick()
    assert result["ok"] is True
    assert "deadline_hit" not in result
    assert result["elapsed_s"] >= 0.0


def test_mc_dig_timeout_temporarily_avoids_same_cell() -> None:
    """r15 P2-21: dig timeout was retried on the same coordinate."""
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.5, 64.0, 1000.5]}})
        if path == "get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
        if path == "dig":
            return httpx.Response(200, json={"ok": False, "error": {"code": "timeout", "message": "dig timed out"}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_dig(1000, 64, 1000))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert "15 秒" in result["error"]["message"]
    assert toolset.skills.temporary_avoidance[(1000, 64, 1000)]["reason"] == "dig_timeout"


def test_bucket_interact_rejects_flowing_fluid_before_no_effect_roundtrip() -> None:
    """r13: bucket right-clicks on flowing lava repeated interact_no_effect."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "lava", "properties": {"level": 3}}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_interact(x=1, y=64, z=0, item="bucket"))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "interact_flowing_fluid"
    assert result["error"]["level"] == 3
    assert calls == ["get_block"]


@pytest.mark.parametrize("outer_budget,step_budget,expected", [(2.0, 10.0, 2.0), (45.0, 1.0, 1.0)])
def test_staircase_inherits_parent_deadline_and_caps_single_step(monkeypatch, outer_budget, step_budget, expected) -> None:
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 30, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            clock[0] += min(0.5, request.extensions["timeout"]["read"])
            result = {"name": "stone"}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="up", steps=25, deadline=100.0 + outer_budget, per_step_timeout=step_budget)
    assert result["stop_reason"] == "time_budget"
    assert clock[0] == pytest.approx(100.0 + expected)
    assert calls[-1] == "stop"
    assert "dig" not in calls


def test_find_blocks_expands_families_and_keeps_valid_part_of_mixed_query() -> None:
    from mc_agent.harness.mc_tools import McToolset

    rejected = {"shield", "sheep", "made_up_block"}
    queries = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        assert path == "find_blocks"
        queries.append(body["types"])
        bad = next((name for name in body["types"] if name in rejected), None)
        if bad:
            return httpx.Response(200, json={"ok": False, "error": {"code": "bad_args", "message": f"unknown block {bad}"}})
        return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "oak_log", "pos": [1, 64, 1]}]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_find_blocks(types=["minecraft:LOG", "planks", "bed", "wool", "grass", "portal", "nether_rack", "cave", "flowing_water", "shield", "sheep", "made_up_block", "future_valid_block"]))
    assert result["ok"] is True
    assert result["ignored_types"] == ["shield", "sheep", "made_up_block"]
    assert result["result"]["blocks"][0]["name"] == "oak_log"
    final = queries[-1]
    assert {"oak_log", "pale_oak_log", "birch_planks", "bamboo_planks", "white_bed", "black_wool", "short_grass", "tall_grass", "nether_portal", "netherrack", "cave_air", "water", "future_valid_block"} <= set(final)
    assert len(final) == len(set(final))
    assert len(queries) == 2


def test_find_blocks_uses_nether_stems_for_generic_log() -> None:
    from mc_agent.harness.mc_tools import McToolset

    queries = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        assert path == "find_blocks"
        queries.append(body["types"])
        return httpx.Response(200, json={"ok": True, "result": {"total": 0, "blocks": []}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0, 64, 0])
        toolset._last_dimension = "the_nether"
        result = json.loads(toolset.mc_find_blocks(types=["log", "planks", "stone"]))
    assert result["ok"] is True
    assert queries == [["crimson_stem", "warped_stem", "crimson_planks", "warped_planks", "stone"]]


def test_find_blocks_labels_backend_total_as_truncated_scan_count() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        assert request.url.path.endswith("/find_blocks")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "total": 10000,
                    "blocks": [
                        {"name": "netherrack", "pos": [0, 63, 0], "dist": 1.2},
                        {"name": "netherrack", "pos": [1, 63, 0], "dist": 1.2},
                    ],
                },
            },
        )

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_find_blocks(types=["netherrack"], radius=64, limit=2))
    assert result["result"]["total"] == 10000
    assert result["result"]["returned"] == 2
    assert result["result"]["truncated"] is True
    assert "后端在半径内扫描到的匹配总数" in result["result"]["total_note"]


@pytest.mark.parametrize("types,expected_queries", [(["shield", "leather"], 0), (["made_up_one", "made_up_two"], 2)])
def test_find_blocks_all_unknown_returns_example_without_unfiltered_fallback(types, expected_queries) -> None:
    from mc_agent.harness.mc_tools import McToolset

    queries = []

    def handler(request):
        body = json.loads(request.content)
        assert body["types"]
        queries.append(body["types"])
        return httpx.Response(200, json={"ok": False, "error": {"code": "bad_args", "message": f"unknown block {body['types'][0]}"}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_find_blocks(types=types))
    assert result["ok"] is False
    assert result["ignored_types"] == types
    assert "mc_find_blocks(types=" in result["error"]["message"]
    assert len(queries) == expected_queries


@pytest.mark.parametrize("item,counts,missing", [
    ("stone_pickaxe", {"cobblestone": 1, "oak_log": 2}, {"cobblestone": 2, "stick": 2}),
    ("stone_shovel", {"stick": 2}, {"cobblestone": 1}),
    ("shield", {"oak_planks": 6}, {"iron_ingot": 1}),
    ("stone_bricks", {"stone": 1}, {"stone": 3}),
])
def test_craft_gear_missing_materials_fail_before_any_mutation(item, counts, missing) -> None:
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        assert path == "inventory"
        return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": name, "count": count} for name, count in counts.items()]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).craft_gear(item)
    assert result["error"] == "missing_ingredients"
    assert result["requires_replan"] is True
    assert result["missing_materials"] == missing
    # r24/D1: 7 of 8 real failures had message=None, forcing the model to
    # parse the maps itself.
    assert result["message"]
    assert item in result["message"]
    assert "缺少材料" in result["message"]
    assert calls == ["inventory"]


def test_recipe_preflight_handles_stone_alternatives_and_batch_yield() -> None:
    from mc_agent.skills.library import craft_material_shortage

    stone = craft_material_shortage("stone_pickaxe", 1, {"cobblestone": 1, "cobbled_deepslate": 1, "blackstone": 1, "stick": 2})
    assert stone["missing_materials"] == {}
    assert stone["available_materials"]["cobblestone"] == 3
    torch = craft_material_shortage("torch", 5, {"charcoal": 2, "stick": 2})
    assert torch["missing_materials"] == {}
    assert torch["required_materials"] == {"coal_or_charcoal": 2, "stick": 2}
    bricks = craft_material_shortage("stone_bricks", 5, {"stone": 7})
    assert bricks["missing_materials"] == {"stone": 1}
    assert craft_material_shortage("unknown_new_recipe", 1, {}) is None


@pytest.mark.parametrize("lose_material,code", [(False, "server_recipe_rejected"), (True, "missing_ingredients")])
def test_craft_no_result_distinguishes_fresh_shortage_and_never_rebuilds_table(lose_material, code) -> None:
    crafted = []
    counts = {"cobblestone": 3, "stick": 2, "crafting_table": 1}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            result = {"items": [{"name": name, "count": count} for name, count in counts.items()], "empty_slots": 30}
            return httpx.Response(200, json={"ok": True, "result": result})
        assert path == "craft"  # No dig/find_blocks/move_to recovery on recipe rejection.
        crafted.append(body)
        if lose_material:
            counts["cobblestone"] = 1
        return httpx.Response(200, json={"ok": False, "error": {"code": "missing_ingredients", "message": "could not craft stone_pickaxe: server placed no result"}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills.ensure_crafting_table = lambda **kwargs: {"ok": True, "reused": True}
        result = skills.craft_gear("stone_pickaxe")
    assert result["error"] == code
    assert result["requires_replan"] is True
    assert crafted == [{"item": "stone_pickaxe", "count": 1}]
    assert result["missing_materials"] == ({"cobblestone": 2} if lose_material else {})


def test_atomic_craft_error_reports_actual_materials_instead_of_example_variant() -> None:
    from mc_agent.errors import ApiError
    from mc_agent.harness.mc_tools import McToolset

    err = ApiError("missing_ingredients", "server placed no result", context={"example_recipe_needs": {"cobbled_deepslate": 3, "stick": 2}, "inventory": {"cobblestone": 1, "stick": 2}})
    result = json.loads(McToolset._format_api_error("craft", {"item": "stone_pickaxe", "count": 1}, err))
    assert result["error"]["missing_materials"] == {"cobblestone": 2}
    assert "example_recipe_needs" not in result["error"]
    assert result["error"]["server_recipe_example"]["cobbled_deepslate"] == 3


@pytest.mark.parametrize("code,word", [("no_support", "支撑"), ("no_support", "侧袋"), ("blocked_by_self", "3 格"), ("place_failed", "mc_dig")])
def test_place_failure_has_specific_recovery_without_world_mutation(code, word) -> None:
    from mc_agent.errors import ApiError
    from mc_agent.harness.mc_tools import McToolset

    result = json.loads(McToolset._format_api_error("place", {"item": "crafting_table", "pos": [1, 64, 1]}, ApiError(code, "offline place failure")))
    assert result["error"]["code"] == code
    assert word in result["error"]["recovery_hint"]


@pytest.mark.parametrize("live_distance,attack_expected", [(45.0, False), (None, False), (2.0, True)])
def test_attack_rechecks_observed_distant_target_before_chasing(live_distance, attack_expected) -> None:
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "get_entities":
            dist = 45.0 if calls.count(path) == 1 else live_distance
            result = {"entities": [] if dist is None else [{"id": 77, "name": "glow_squid", "type": "animal", "dist": dist, "pos": [0.5, 64, dist]}]}
        elif path == "inventory":
            result = {"items": []}
        elif path == "attack":
            result = {"hits": 1, "killed": False}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        assert json.loads(tools.mc_get_entities(radius=64))["ok"] is True
        result = json.loads(tools.mc_attack(entity_id=77))
    assert ("attack" in calls) is attack_expected
    assert result["ok"] is attack_expected
    assert calls.count("get_entities") == 2
    if not attack_expected:
        assert "inventory" not in calls
        assert result["error"]["code"] in {"target_out_of_reach", "target_not_visible"}


@pytest.mark.parametrize("filler", ["stone", "netherrack", "tuff"])
def test_stair_direction_hints_share_filler_rules_and_avoid_recent_failure(filler) -> None:
    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        assert path == "get_block"
        return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills._record_failed_stair_dir((0, 30, 0, "up"), "north", "movement_not_confirmed")
        result = skills._probe_viable_up_stair_dirs(0, 30, 0, inv_counts={filler: 8})
        assert result == ["south", "east", "west"]
        # A down-step over open air is rejected by dig_staircase even with
        # filler available, so a hint must not claim that route is executable.
        down = skills._probe_viable_down_stair_dirs(0, 30, 0, inv_counts={filler: 8})
        assert down == []


def test_pillar_deadline_clears_persistent_jump_posture(monkeypatch) -> None:
    import mc_agent.skills.library as library

    clock = [100.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    posture = []
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "status":
            if posture and posture[-1]["jump"]:
                clock[0] += request.extensions["timeout"]["read"]
            result = {"pos": [0.5, 30, 0.5], "on_ground": True, "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "stone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            posture.append(body)
            result = {}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(vertical="pillar", deadline=100.5, per_step_timeout=0.2)
    assert result["stop_reason"] == "time_budget"
    assert clock[0] == pytest.approx(100.2)
    assert [item["jump"] for item in posture] == [True, False]
    assert calls[-2:] == ["stop", "posture"]


@pytest.mark.parametrize("failure_code", ["place_failed", "no_support"])
def test_pillar_retries_transient_place_failures(monkeypatch, failure_code) -> None:
    """r14/BUG-024: first one-step pillar jump-place failed, next attempt worked."""
    import mc_agent.skills.library as library

    clock = [500.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    jumping = [False]
    placed = [False]
    place_calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            y = 31.2 if jumping[0] else (31.0 if placed[0] else 30.0)
            result = {"pos": [0.5, y, 0.5], "on_ground": not jumping[0], "alive": True, "ready": True}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            jumping[0] = bool(body.get("jump"))
            result = {}
        elif path == "place":
            place_calls.append(body)
            if len(place_calls) <= 2:
                return httpx.Response(
                    409, json={"ok": False, "error": {"code": failure_code, "message": "placement refused"}}
                )
            placed[0] = True
            result = {"pos": body["pos"]}
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(vertical="pillar", steps=1)
    assert result["ok"] is True
    assert result["steps_done"] == 1
    assert result["placed_count"] == 1
    assert len(place_calls) == 3


def test_failed_pillar_blocks_landing_cell_not_airborne_cell(monkeypatch) -> None:
    """r12: a failed jump-place was remembered at y+1, so the next call still
    advertised viable_pillar_up=true at the same grounded cell."""
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "monotonic", lambda: 500.0)
    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    jumping = [False]
    status_reads = [0]
    place_calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            # First read is the grounded start; the r12 failure returned while
            # the jump/place attempt was still at y=31.2.
            y = 30.0 if status_reads[0] == 0 else 31.2
            status_reads[0] += 1
            result = {
                "pos": [0.5, y, 0.5],
                "on_ground": status_reads[0] == 1,
                "alive": True,
                "ready": True,
                "health": 20.0,
            }
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "posture":
            jumping[0] = bool(body.get("jump"))
            result = {}
        elif path == "place":
            place_calls.append(body)
            return httpx.Response(
                409, json={"ok": False, "error": {"code": "place_failed", "message": "the block is still air"}}
            )
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.dig_staircase(vertical="pillar", steps=1)
        assert result["stop_reason"] == "place_failed"
        assert place_calls
        assert skills._probe_viable_pillar_up(0, 30, 0, inv_counts={"cobblestone": 8}) is False


@pytest.mark.parametrize("floor", ["stone", "water"])
def test_portal_entry_builds_one_safe_step_from_two_blocks_below(monkeypatch, floor) -> None:
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    pos = [0.5, 64.0, 1.5]
    contact = [None]
    placed = []
    jumping = [False]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            nether = contact[0] is not None and clock[0] - contact[0] >= 4
            result = {"pos": list(pos), "dimension": "the_nether" if nether else "overworld", "alive": True, "ready": True, "on_ground": not jumping[0]}
        elif path == "find_blocks":
            result = {"blocks": [{"pos": [0, 66, 2], "name": "nether_portal"}]}
        elif path == "get_block":
            result = {"name": "nether_portal", "properties": {"axis": "x"}} if body["pos"] == [0, 66, 2] else {"name": floor if body["pos"][1] <= 63 else "air"}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "posture":
            jumping[0] = body["jump"]
            pos[1] = 65.1 if body["jump"] else 65.0
            result = {}
        elif path == "place":
            placed.append(body)
            result = {"pos": body["pos"]}
        elif path == "control":
            assert body["jump"] is True
            pos[:] = [0.5, 66.0, 2.5]
            contact[0] = clock[0]
            result = {"pos": list(pos)}
        elif path in {"move_to", "look"}:
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(wait_s=8)
    if floor == "stone":
        assert result["ok"] is True
        assert result["entered_nether"] is True
        assert result["entry_recovery"]["steps_done"] == 1
        assert placed == [{"item": "cobblestone", "pos": [0, 64, 1]}]
    else:
        assert result["ok"] is False
        assert result["error"]["code"] == "portal_approach_unsupported"
        assert placed == []
        assert contact[0] is None
    assert clock[0] <= 8


def test_staircase_timeout_preserves_confirmed_partial_excavation() -> None:
    blocks = {}
    dig_count = [0]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 30, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": blocks.get(tuple(body["pos"]), "stone")}
        elif path == "dig":
            dig_count[0] += 1
            if dig_count[0] == 2:
                raise httpx.ReadTimeout("second dig pending", request=request)
            blocks[tuple(body["pos"])] = "air"
            result = {"dug": "stone"}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", steps=3, stop_on_ore=False)
    assert result["stop_reason"] == "time_budget"
    assert result["dug_count"] == 1
    assert result["steps_done"] == 0
    assert result["pos_before"] == [0.5, 30, 0.5]
    assert result["pos_after"] == [0.5, 30, 0.5]
