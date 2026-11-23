"""Tests for the unified 1.21.11 world ecology & dimension physics oracle."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from mc_agent.bot import BotSession
from mc_agent.harness.hud import compute_goal_gap_hint
from mc_agent.harness.mc_tools import McToolset
from mc_agent.kb import assess_world_target
from mc_agent.skills.library import MacroSkills


def _mock_world_bot(
    *,
    pos: list[float],
    dimension: str = "overworld",
    biome: str = "",
    items: list[dict[str, Any]] | None = None,
    find_blocks_map: dict[tuple[str, ...], list[dict[str, Any]]] | None = None,
    block_name_fn=None,
) -> tuple[BotSession, list[tuple[str, Any]]]:
    calls: list[tuple[str, Any]] = []
    inv_items = list(items or [])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append((path, body))
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(pos),
                        "dimension": dimension,
                        "biome": biome,
                        "health": 20,
                        "food": 20,
                        "ready": True,
                        "in_water": False,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": inv_items}})
        if path == "/equip":
            return httpx.Response(200, json={"ok": True, "result": {"equipped": body.get("item")}})
        if path == "/find_blocks":
            types = tuple(body.get("types") or ())
            blocks = list((find_blocks_map or {}).get(types, []))
            return httpx.Response(200, json={"ok": True, "result": {"blocks": blocks}})
        if path == "/get_block":
            bpos = tuple(int(v) for v in (body.get("pos") or [0, 0, 0]))
            if block_name_fn is not None:
                return httpx.Response(200, json={"ok": True, "result": {"name": block_name_fn(bpos)}})
            if bpos[0] == math.floor(pos[0]) and bpos[2] == math.floor(pos[2]) and bpos[1] in (math.floor(pos[1]), math.floor(pos[1]) + 1):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "stone"}})
        if path in ("/place", "/interact"):
            return httpx.Response(200, json={"ok": True, "result": {"action": path.lstrip("/"), **body}})
        raise AssertionError(f"unexpected path: {path}")

    return BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(handler)), calls


def test_underground_trees_blocked_with_upward_directions_at_y29_and_y45() -> None:
    for y_val in (29.0, 45.0):
        bot, _ = _mock_world_bot(
            pos=[-172.3, y_val, -334.3],
            dimension="minecraft:overworld",
            items=[{"name": "wooden_pickaxe", "count": 1}, {"name": "cobblestone", "count": 64}],
        )
        with bot:
            skills = MacroSkills(bot)
            ts = McToolset(bot)
            h_res = skills.harvest_logs(target_logs=4, radius=16)
            assert h_res["ok"] is False
            assert h_res.get("underground_no_trees") is True
            assert h_res["ecology_check"]["verdict"] == "below_generation_band"
            assert h_res["ecology_check"]["direction_needed"] == "up"
            assert h_res.get("viable_up_directions")
            assert "mc_skill_staircase" in h_res["message"]

            f_res = json.loads(ts.mc_find_blocks(radius=16, types=["oak_log"]))
            assert f_res["ok"] is True
            eco = f_res["result"]["ecology_check"]
            assert eco["available"] is False
            assert eco["verdict"] == "below_generation_band"
            assert eco["direction_needed"] == "up"


def test_shallow_diamond_and_deep_coal_and_mixed_ore_queries() -> None:
    # 1) y=45 浅层找钻石：above_generation_band -> direction_needed="down", recommended_y=-54, 附 viable_down_directions
    bot_shallow, _ = _mock_world_bot(
        pos=[10.5, 45.0, -20.5],
        dimension="overworld",
        items=[{"name": "iron_pickaxe", "count": 1}, {"name": "cobblestone", "count": 32}],
    )
    with bot_shallow:
        m_res = MacroSkills(bot_shallow).mine_blocks(["diamond_ore", "deepslate_diamond_ore"], count=3, radius=32)
        assert m_res["ok"] is False
        assert m_res["ecology_check"]["verdict"] == "above_generation_band"
        assert m_res["ecology_check"]["direction_needed"] == "down"
        assert m_res["ecology_check"]["recommended_y"] == -54
        assert m_res.get("viable_down_directions")

        ts = McToolset(bot_shallow)
        f_diamond = json.loads(ts.mc_find_blocks(radius=16, types=["diamond_ore"]))
        assert f_diamond["result"]["ecology_check"]["verdict"] == "above_generation_band"
        assert f_diamond["result"]["ecology_check"]["recommended_y"] == -54

    # 2) y=-48 深层找煤矿：below_generation_band + charcoal 替代方案提示
    bot_deep, _ = _mock_world_bot(
        pos=[10.5, -48.0, -20.5],
        dimension="overworld",
        items=[{"name": "stone_pickaxe", "count": 1}],
    )
    with bot_deep:
        m_coal = MacroSkills(bot_deep).mine_blocks(["coal_ore"], count=4, radius=16)
        assert m_coal["ok"] is False
        assert m_coal["ecology_check"]["verdict"] == "below_generation_band"
        assert "charcoal" in m_coal["message"]

    # 3) y=-30 混合请求 ["coal_ore", "iron_ore"]：iron_ore 在该层可用，总体不得误判不可用
    mixed_check = assess_world_target(["coal_ore", "iron_ore"], dimension="overworld", y=-30, mode="find")
    assert mixed_check["available"] is True
    assert mixed_check["verdict"] == "ok"
    assert mixed_check["unavailable_subset"][0]["family_id"] == "coal_ore"


def test_cross_dimension_ores_nether_wood_and_biome_rules() -> None:
    # 1) 下界找主世界矿石 + 主世界找下界残骸/石英 -> wrong_dimension
    nether_iron = assess_world_target("iron_ore", dimension="minecraft:the_nether", y=48, mode="find")
    assert nether_iron["available"] is False
    assert nether_iron["verdict"] == "wrong_dimension"
    assert nether_iron["direction_needed"] == "portal"

    nether_diamond = assess_world_target("diamond_ore", dimension="the_nether", y=12, mode="find")
    assert nether_diamond["available"] is False
    assert nether_diamond["verdict"] == "wrong_dimension"

    overworld_debris = assess_world_target("ancient_debris", dimension="overworld", y=15, mode="find")
    assert overworld_debris["available"] is False
    assert overworld_debris["verdict"] == "wrong_dimension"

    overworld_quartz = assess_world_target("nether_quartz_ore", dimension="overworld", y=64, mode="find")
    assert overworld_quartz["available"] is False
    assert overworld_quartz["verdict"] == "wrong_dimension"

    # 2) 下界缺木时 ensure_crafting_table 与 HUD 均改指 crimson_stem / warped_stem
    nether_gap = compute_goal_gap_hint(
        {"wooden_pickaxe": 1, "cobblestone": 64},
        task="合成石镐",
        bot_y=40,
        dimension="the_nether",
    )
    assert "crimson_stem" in nether_gap and "warped_stem" in nether_gap

    bot_nether, _ = _mock_world_bot(pos=[0.5, 40.0, 0.5], dimension="minecraft:the_nether", items=[])
    with bot_nether:
        tbl_err = MacroSkills(bot_nether).ensure_crafting_table()
        assert tbl_err["ok"] is False
        assert "crimson_stem" in tbl_err["message"] or "warped_stem" in tbl_err["message"]

    # 3) 群系限制与恶地金矿特例
    plains_emerald = assess_world_target("emerald_ore", dimension="overworld", y=40, biome="minecraft:plains", mode="find")
    assert plains_emerald["available"] is False
    assert plains_emerald["verdict"] == "wrong_biome"

    badlands_gold = assess_world_target("gold_ore", dimension="overworld", y=80, biome="minecraft:badlands", mode="find")
    assert badlands_gold["available"] is True
    assert badlands_gold["verdict"] == "ok"

    plains_gold_high = assess_world_target("gold_ore", dimension="overworld", y=80, biome="plains", mode="find")
    assert plains_gold_high["available"] is False
    assert plains_gold_high["verdict"] == "above_generation_band"


def test_dimension_physics_blocks_evaporation_and_explosions_cleanly() -> None:
    # 下界放置水桶与交互床均被拦截（绝不发往 /place 或 /interact）
    bot_nether, calls_nether = _mock_world_bot(pos=[2.5, 60.0, 2.5], dimension="the_nether")
    with bot_nether:
        ts_n = McToolset(bot_nether)
        water_err = json.loads(ts_n.mc_place("water_bucket", 3, 60, 2))
        assert water_err["ok"] is False
        assert water_err["error"]["code"] == "invalid_dimension_physics"
        assert water_err["error"]["ecology_check"]["verdict"] == "evaporates_in_dimension"

        bed_err = json.loads(ts_n.mc_interact(3, 60, 2, item="white_bed"))
        assert bed_err["ok"] is False
        assert bed_err["error"]["code"] == "invalid_dimension_physics"
        assert bed_err["error"]["ecology_check"]["verdict"] == "explodes_in_dimension"

        # 下界使用 respawn_anchor 应放行
        anchor_ok = json.loads(ts_n.mc_interact(3, 60, 2, item="respawn_anchor"))
        assert anchor_ok["ok"] is True

        # assess_target & mc_kb_lookup 共享同一规则
        assert ts_n.assess_target("water_bucket", mode="place")["verdict"] == "evaporates_in_dimension"
        kb_dia = json.loads(ts_n.mc_kb_lookup("diamond_ore"))
        assert kb_dia["ecology_check"]["verdict"] == "wrong_dimension"

    assert all(p not in ("/place",) for p, _ in calls_nether)
    assert sum(1 for p, _ in calls_nether if p == "/interact") == 1

    # 主世界禁止激活重生锚，但允许放置水桶与床
    bot_over, calls_over = _mock_world_bot(pos=[2.5, 64.0, 2.5], dimension="overworld")
    with bot_over:
        ts_o = McToolset(bot_over)
        anchor_err = json.loads(ts_o.mc_interact(3, 64, 2, item="respawn_anchor"))
        assert anchor_err["ok"] is False
        assert anchor_err["error"]["code"] == "invalid_dimension_physics"
        assert anchor_err["error"]["ecology_check"]["verdict"] == "explodes_in_dimension"

        water_ok = json.loads(ts_o.mc_place("water_bucket", 3, 64, 2))
        bed_ok = json.loads(ts_o.mc_interact(3, 64, 2, item="white_bed"))
        assert water_ok["ok"] is True
        assert bed_ok["ok"] is True

    assert sum(1 for p, _ in calls_over if p == "/place") == 1
    assert sum(1 for p, _ in calls_over if p == "/interact") == 1


def test_harvest_logs_pit_unreachable_hint_and_unstick_preserves_failure_guard() -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.tool_registry import Tool

    # 1) 处于 y=68 凹坑且周围原木在 y=71+ 超距时，harvest_logs 返回明确爬坑/转移引导
    state = {"pos": [-138.6, 68.0, -383.4], "unstick_calls": 0}

    def pit_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(state["pos"]),
                        "dimension": "overworld",
                        "biome": "birch_forest",
                        "health": 17,
                        "food": 17,
                        "ready": True,
                        "in_water": False,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 64}]}})
        if path == "/find_blocks":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "blocks": [
                            {"name": "oak_log", "pos": [-127, 71, -389], "dist": 13.0},
                        ]
                    },
                },
            )
        if path == "/dig":
            return httpx.Response(
                400,
                json={"ok": False, "error": {"code": "out_of_reach", "message": "too far"}},
            )
        if path == "/get_block":
            bpos = tuple(int(v) for v in (body.get("pos") or [0, 0, 0]))
            px, py, pz = math.floor(state["pos"][0]), math.floor(state["pos"][1]), math.floor(state["pos"][2])
            if (bpos[0], bpos[2]) == (px, pz) and bpos[1] in (py + 1, py + 2, py + 3):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if (bpos[0], bpos[2]) == (px, pz - 1) and bpos[1] in (py, py + 1):
                return httpx.Response(200, json={"ok": True, "result": {"name": "dirt"}})
            if (bpos[0], bpos[2]) == (px, pz - 1) and bpos[1] in (py + 2, py + 3, py + 4):
                return httpx.Response(200, json={"ok": True, "result": {"name": "air"}})
            if (bpos[0], bpos[2]) in {(px, pz), (px, pz - 1)}:
                return httpx.Response(200, json={"ok": True, "result": {"name": "dirt"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": "bedrock"}})
        raise AssertionError(f"unexpected path: {path}")

    with BotSession("tok", bot_username="Bot", transport=httpx.MockTransport(pit_handler)) as pit_bot:
        h_res = MacroSkills(pit_bot).harvest_logs(target_logs=4, radius=16)
        assert h_res["ok"] is False
        assert h_res["error"] == "unreachable_targets"
        assert h_res["log_approach"]["log"] == [-127, 71, -389]
        assert h_res["log_approach"]["moved"] is False
        assert "不可达" in h_res["message"]

        # 2) _RepeatFailureGuard: unstick(含 dug 与微移1格) 不清空失败签名，且 8 格半径内 harvest_logs 维持屏蔽
        ts = McToolset(pit_bot)
        ts.update_block_pos([-139, 68, -384])
        guard = _RepeatFailureGuard(ts)

        wrapped_harvest = guard.wrap_tool(
            Tool(
                name="mc_skill_harvest_logs",
                description="harvest",
                parameters=[],
                handler=lambda **kw: json.dumps(
                    {
                        "ok": False,
                        "dug_count": 0,
                        "pos_before": list(state["pos"]),
                        "error": "no_targets_available",
                        "message": "no trees",
                    },
                    ensure_ascii=False,
                ),
                concurrency="exclusive",
            )
        )

        def _fake_unstick(**kw: Any) -> str:
            state["unstick_calls"] += 1
            before = list(state["pos"])
            state["pos"][0] += 1.0
            return json.dumps(
                {
                    "ok": True,
                    "pos_before": before,
                    "pos_after": list(state["pos"]),
                    "dug": [{"pos": [math.floor(before[0]) + 1, 69, -384], "block": "sand"}],
                },
                ensure_ascii=False,
            )

        wrapped_unstick = guard.wrap_tool(
            Tool(
                name="mc_skill_unstick",
                description="unstick",
                parameters=[],
                handler=_fake_unstick,
                concurrency="exclusive",
            )
        )

        # harvest_logs 连续失败两次 -> _tool_streaks 达到阈值 2 并触发局部屏蔽
        wrapped_harvest.handler(target_logs=4, radius=16)
        wrapped_harvest.handler(target_logs=8, radius=32)
        assert "mc_skill_harvest_logs" in guard.masked_tool_names()

        # unstick 执行并带 "dug" 微移 1 格 -> harvest_logs 在 8 格局部半径内仍被屏蔽！
        wrapped_unstick.handler()
        assert "mc_skill_harvest_logs" in guard.masked_tool_names()
        blocked_after_unstick = json.loads(wrapped_harvest.handler(target_logs=4, radius=16))
        assert blocked_after_unstick["ok"] is False
        assert blocked_after_unstick["error"]["code"] == "repeated_identical_failure_blocked"

        # 局部连续第 2 次 unstick 后，mc_skill_unstick 自身也被屏蔽且第 3 次调用被短路
        wrapped_unstick.handler()
        assert "mc_skill_unstick" in guard.masked_tool_names()
        unstick_blocked = json.loads(wrapped_unstick.handler())
        assert unstick_blocked["ok"] is False
        assert unstick_blocked["error"]["code"] == "repeated_identical_failure_blocked"
        assert state["unstick_calls"] == 2
