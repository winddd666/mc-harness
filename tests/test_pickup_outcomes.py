"""Pickup regressions reproduced on the tree canopy: API success without inventory gain."""
import json
from typing import Any

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.harness.mc_tools import McToolset
from mc_agent.skills.library import MacroSkills


@pytest.mark.parametrize("closer_fails", [False, True])
def test_successful_approach_without_gain_is_failure(monkeypatch, closer_fails):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    moves = []

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "inventory":
            result = {"items": []}
        elif action == "status":
            result = {"pos": [-162.6, 76, -372.5], "health": 13.3}
        elif action == "get_entities":
            result = {"entities": [{"id": 22546, "item": "birch_logx2", "pos": [-161.1, 74, -371.1]}]}
        elif action == "move_to":
            moves.append(body)
            if "x" in body and closer_fails:
                return httpx.Response(200, json={"ok": False, "error": {"code": "no_path", "message": "canopy hole"}})
            result = {"pos": [-162.6, 76, -372.5]}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(handle)) as bot:
        out = MacroSkills(bot).collect_drops()
    assert out["ok"] is False
    assert out["error"] == "no_drops_collected"
    assert out["inventory"] == out["gained"] == {}
    assert out["attempted_pickups"] == 1
    assert len(moves) == 2
    assert out["pos_after"] == [-162.6, 76, -372.5]
    assert out["moved_to"][0]["pos"] != out["moved_to"][0]["item_pos"]
    if closer_fails:
        assert out["attempts"][0]["closer_error"]["code"] == "no_path"


@pytest.mark.parametrize("mode,expected", [("deep", "drops_unreachable"), ("none", "no_drops_found"), ("hurt", "interrupted")])
def test_skipped_and_interrupted_pickups_explain_failure(monkeypatch, mode, expected):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    moves = []

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "inventory":
            result = {"items": []}
        elif action == "status":
            result = {"pos": [0, 76, 0], "health": 8}
        elif action == "get_entities":
            result = {"entities": [] if mode == "none" else [
                {"id": 1, "item": "birch_logx2", "pos": [1, 70 if mode == "deep" else 76, 1]},
                {"id": 2, "item": "stickx1", "pos": [2, 70 if mode == "deep" else 76, 1]},
            ]}
        elif action == "move_to":
            moves.append(1)
            return httpx.Response(200, json={"ok": False, "error": {
                "code": "interrupted", "message": "hurt", "health": 8, "cause": "arrow",
            }})
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(handle)) as bot:
        out = MacroSkills(bot).collect_drops()
    assert out["ok"] is False
    assert out["error"] == expected
    if mode == "hurt":
        assert len(moves) == 1  # No retry or second item after genuine hurt.
        assert out["details"]["cause"] == "arrow"
        assert out["details"]["health"] == 8
    elif mode == "deep":
        assert not moves
        assert len(out["skipped"]) == 2
        assert out["skipped"][0]["reason"] == "height_difference"


def test_pickup_success_requires_server_inventory_gain(monkeypatch):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    picked_up = False

    def handle(request):
        nonlocal picked_up
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "inventory":
            result = {"items": [{"name": "birch_log", "count": 2}] if picked_up else []}
        elif action == "status":
            result = {"pos": [0.5, 70, 0.5], "health": 20}
        elif action == "get_entities":
            result = {"entities": [{"id": 1, "item": "birch_logx2", "pos": [0.5, 70, 0.5]}]}
        elif action == "move_to":
            picked_up = True
            result = {"pos": [0.5, 70, 0.5]}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(handle)) as bot:
        out = MacroSkills(bot).collect_drops()
    assert out["ok"] is True
    assert out["inventory"] == out["gained"] == {"birch_log": 2}


def test_same_level_sneak_nudge_and_fallen_ore_descent_guidance(monkeypatch):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)

    # 1) Same-level 1.98m ledge clamp triggers sneak forward micro-step and collects raw_ironx2
    nudge_state = {
        "pos": [-157.5, 44.0, -400.7],
        "controls": [],
        "collected": False,
    }

    def nudge_handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "status":
            result = {"pos": list(nudge_state["pos"]), "health": 20}
        elif action == "inventory":
            items = [{"name": "raw_iron", "count": 2}] if nudge_state["collected"] else []
            result = {"items": items}
        elif action == "get_entities":
            ents = [] if nudge_state["collected"] else [
                {"id": 3001, "item": "raw_ironx2", "pos": [-158.9, 44.0, -402.1]}
            ]
            result = {"entities": ents}
        elif action == "move_to":
            result = {"pos": list(nudge_state["pos"])}
        elif action == "control":
            nudge_state["controls"].append(dict(body))
            nudge_state["pos"] = [-158.9, 44.0, -402.1]
            nudge_state["collected"] = True
            result = {"pos": list(nudge_state["pos"])}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(nudge_handle)) as bot_nudge:
        nudge_out = MacroSkills(bot_nudge).collect_drops()
    assert nudge_out["ok"] is True
    assert nudge_out["gained"] == {"raw_iron": 2}
    assert len(nudge_state["controls"]) == 1
    assert nudge_state["controls"][0]["forward"] is True
    assert nudge_state["controls"][0]["sneak"] is True

    # 2) mine_blocks detects fallen chasm raw_iron at dy=-6.0 and emits staircase(down) recovery hint
    def mine_handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            result = {"pos": [-157.5, 44.0, -400.7], "health": 20, "in_water": False}
        elif action == "inventory":
            result = {"items": [{"name": "stone_pickaxe", "count": 1}]}
        elif action == "equip":
            result = {"held": {"name": "stone_pickaxe"}}
        elif action == "find_blocks":
            result = {
                "blocks": [
                    {"name": "iron_ore", "pos": [-159, 44, -404], "dist": 3.5},
                    {"name": "iron_ore", "pos": [-160, 44, -404], "dist": 4.1},
                ]
            }
        elif action == "get_block":
            result = {"name": "stone"}
        elif action == "dig":
            result = {"dug": "iron_ore", "gained": {}}
        elif action == "get_entities":
            result = {
                "entities": [
                    {"id": 4001, "item": "raw_ironx2", "pos": [-158.8, 38.0, -403.6]},
                ]
            }
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(mine_handle)) as bot_mine:
        mine_out = MacroSkills(bot_mine).mine_blocks(["iron_ore"], count=2, radius=16)
    assert mine_out["ok"] is False
    assert mine_out["error"] == "no_drops_collected"
    assert mine_out["requires_replan"] is True
    assert mine_out["nearest_unreachable_drop"]["pos"] == [-158.8, 38.0, -403.6]
    assert "mc_skill_staircase" in mine_out["approach_hint"]
    assert "vertical='down'" in mine_out["approach_hint"]

    # 3) dig_staircase(south, down) ignores behind-above overhang iron_ore, stops on forward deepslate_diamond_ore
    stair_state = {"pos": [-157.5, 43.0, -401.5], "mode": "ignore_behind"}

    def stair_handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "status":
            result = {"pos": list(stair_state["pos"]), "health": 20, "on_ground": True}
        elif action == "inventory":
            result = {"items": [{"name": "iron_pickaxe", "count": 1}]}
        elif action == "equip":
            result = {"held": {"name": "iron_pickaxe"}}
        elif action == "get_block":
            px, py, pz = (int(v) for v in (body.get("pos") or [0, 0, 0]))
            cur_y = int(stair_state["pos"][1])
            result = {"name": "stone" if py >= cur_y - 2 else "stone"}
        elif action == "dig":
            result = {"dug": "stone", "gained": {"cobblestone": 1}}
        elif action == "move_to":
            stair_state["pos"] = [float(body["x"]), float(body["y"]), float(body["z"])]
            result = {"pos": list(stair_state["pos"])}
        elif action == "find_blocks":
            if stair_state["mode"] == "ignore_behind":
                result = {"blocks": [{"name": "iron_ore", "pos": [-160, 44, -403], "dist": 6.5}]}
            else:
                result = {
                    "blocks": [
                        {"name": "iron_ore", "pos": [-160, 44, -403], "dist": 6.5},
                        {"name": "deepslate_diamond_ore", "pos": [-158, 36, -393], "dist": 3.2},
                    ]
                }
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(stair_handle)) as bot_stair:
        skills_stair = MacroSkills(bot_stair)
        res_ignore = skills_stair.dig_staircase(direction="south", steps=3, vertical="down", stop_on_ore=True)
        assert res_ignore["ok"] is True
        assert res_ignore["steps_done"] == 3
        assert res_ignore["stop_reason"] == "completed"

        stair_state["mode"] = "forward_diamond"
        res_dia = skills_stair.dig_staircase(direction="south", steps=3, vertical="down", stop_on_ore=True)
        assert res_dia["ok"] is True
        assert res_dia["stop_reason"] == "ore_detected"
        assert res_dia["ores_detected"][0]["name"] == "deepslate_diamond_ore"
        assert "mc_skill_mine_blocks" in str(res_dia.get("hint"))


def test_full_inventory_auto_trash_and_fast_fail_plus_diamond_armor_and_up_stair(monkeypatch) -> None:
    """Verify full-bag auto-trash before craft/smelt, inventory_full fast-fail, diamond armor crafting, and upward staircase defaults."""
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    # 1) 36/36 full bag with trash clears slots via /drop and crafts stone_pickaxe without hanging
    trash_items = [
        {"name": "cobblestone", "count": 64, "slot": 9 + i} for i in range(4)
    ] + [
        {"name": "wildflowers", "count": 12, "slot": 13},
        {"name": "leaf_litter", "count": 7, "slot": 14},
        {"name": "rotten_flesh", "count": 1, "slot": 15},
        {"name": "stick", "count": 4, "slot": 16},
    ] + [
        {"name": "stone_sword", "count": 1, "slot": 17 + i} for i in range(28)
    ]
    bag_state: dict[str, Any] = {
        "items": list(trash_items),
        "drops": [],
        "crafted": [],
        "equipped": [],
        "window_takes": 0,
    }

    def full_handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.removeprefix("/api/v1/")
        body = json.loads(request.content) if request.content else {}
        if action == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "health": 20, "food": 20, "ready": True}
        elif action == "get_entities":
            result = {"entities": []}
        elif action == "inventory":
            occupied = len(bag_state["items"])
            result = {"items": list(bag_state["items"]), "empty_slots": max(0, 36 - occupied)}
        elif action == "find_blocks":
            btypes = body.get("types") or []
            if "crafting_table" in btypes:
                result = {"blocks": [{"name": "crafting_table", "pos": [1, 64, 0], "dist": 1.2}]}
            elif "furnace" in btypes:
                result = {"blocks": [{"name": "furnace", "pos": [2, 64, 0], "dist": 1.8}]}
            else:
                result = {"blocks": []}
        elif action == "drop":
            dname = body["item"]
            dcnt = int(body["count"])
            bag_state["drops"].append((dname, dcnt))
            rem = dcnt
            new_items = []
            for row in bag_state["items"]:
                if row["name"] == dname and rem > 0:
                    take = min(rem, int(row["count"]))
                    rem -= take
                    left = int(row["count"]) - take
                    if left > 0:
                        new_items.append({**row, "count": left})
                else:
                    new_items.append(row)
            bag_state["items"] = new_items
            result = {"dropped": dname, "count": dcnt}
        elif action == "craft":
            bag_state["crafted"].append((body["item"], body["count"]))
            result = {"crafted": body["item"], "count": body["count"]}
        elif action == "equip":
            bag_state["equipped"].append((body["item"], body["slot"]))
            result = {"equipped": body["item"], "slot": body["slot"]}
        elif action == "window/view":
            result = {"sections": {"input": [], "output": [{"name": "iron_ingot", "count": 1}]}, "info": {"burning": False}}
        elif action == "window/act":
            if any(op.get("op") == "take" for op in (body.get("ops") or [])):
                bag_state["window_takes"] += 1
            result = {"ok": True}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(full_handler)) as bot_full:
        skills_full = MacroSkills(bot_full)
        cg_ok = skills_full.craft_gear("stone_pickaxe")
        assert cg_ok["ok"] is True
        assert ("stone_pickaxe", 1) in bag_state["crafted"]
        assert len(bag_state["drops"]) >= 3
        assert cg_ok.get("auto_dropped")

        # 2) All 36 slots un-droppable valuables -> fast fail inventory_full before /craft or window/act take
        bag_state["crafted"].clear()
        bag_state["drops"].clear()
        bag_state["items"] = (
            [{"name": "cobblestone", "count": 64, "slot": 9}]
            + [{"name": "stick", "count": 4, "slot": 10}]
            + [{"name": "oak_planks", "count": 5, "slot": 11}]
            + [{"name": "iron_pickaxe", "count": 1, "slot": 12 + i} for i in range(33)]
        )
        cg_blocked = skills_full.craft_gear("stone_pickaxe")
        assert cg_blocked["ok"] is False
        assert cg_blocked["error"] == "inventory_full"
        assert cg_blocked["requires_replan"] is True
        assert bag_state["crafted"] == []

        ts = McToolset(bot_full)
        inv_json = json.loads(ts.mc_inventory())
        assert inv_json["ok"] is True
        raw_craft_blocked = json.loads(ts.mc_craft("stone_pickaxe", 1))
        assert raw_craft_blocked["ok"] is False
        assert raw_craft_blocked["error"]["code"] == "inventory_full"
        assert bag_state["crafted"] == []

        smelt_blocked = skills_full.smelt_items("oak_planks", 1, fuel_item="oak_planks")
        assert smelt_blocked["ok"] is False
        assert smelt_blocked["error"] == "inventory_full"
        assert bag_state["window_takes"] == 0

    # 3) diamond_boots shortage pre-check & table + feet equip
    bag_state["crafted"].clear()
    bag_state["equipped"].clear()
    bag_state["items"] = [{"name": "diamond", "count": 2, "slot": 9}]
    with BotSession("test", transport=httpx.MockTransport(full_handler)) as bot_dia:
        skills_dia = MacroSkills(bot_dia)
        miss = skills_dia.craft_gear("diamond_boots")
        assert miss["ok"] is False
        assert miss["error"] == "missing_ingredients"
        assert miss["missing_materials"] == {"diamond": 2}
        assert bag_state["crafted"] == []

        bag_state["items"] = [{"name": "diamond", "count": 4, "slot": 9}]
        made = skills_dia.craft_gear("diamond_boots")
        assert made["ok"] is True
        assert ("diamond_boots", 1) in bag_state["crafted"]
        assert ("diamond_boots", "feet") in bag_state["equipped"]

    # 4) dig_staircase(direction=None, vertical='up') auto-picks viable dir (skipping north lava) & defaults stop_on_ore=False
    up_state = {"pos": [0.5, 40.0, 0.5]}

    def up_handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "status":
            result = {"pos": list(up_state["pos"]), "health": 20, "on_ground": True}
        elif action == "inventory":
            result = {"items": [{"name": "iron_pickaxe", "count": 1}, {"name": "cobblestone", "count": 16}], "empty_slots": 34}
        elif action == "equip":
            result = {"held": {"name": "iron_pickaxe"}}
        elif action == "get_block":
            px, py, pz = (int(v) for v in (body.get("pos") or [0, 0, 0]))
            # Block north stair steps with bedrock so east is auto-selected cleanly
            if (px, pz) == (0, -1) and py in (41, 42):
                result = {"name": "bedrock"}
            else:
                result = {"name": "stone"}
        elif action == "dig":
            result = {"dug": "stone", "gained": {"cobblestone": 1}}
        elif action == "move_to":
            up_state["pos"] = [float(body["x"]), float(body["y"]), float(body["z"])]
            result = {"pos": list(up_state["pos"])}
        elif action == "find_blocks":
            result = {"blocks": [{"name": "iron_ore", "pos": [3, 43, 0], "dist": 2.0}]}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("test", transport=httpx.MockTransport(up_handler)) as bot_up:
        ts_up = McToolset(bot_up)
        res_up = json.loads(ts_up.mc_skill_staircase(vertical="up", steps=2))
        assert res_up["ok"] is True
        assert res_up["direction"] == "south"
        assert res_up["stop_reason"] == "completed"
        assert res_up["steps_done"] == 2
