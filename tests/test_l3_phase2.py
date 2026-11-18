"""Phase-2 unit tests for L3/L4 Core Architecture:
- [D1] `_can_attempt_target` objective hard prerequisites (`form_obsidian`, `use_lodestone`, `crafters`, `enchant_item`)
- [D2] `CurriculumTarget.is_objectively_satisfied` preventing historical `story/iron_tools` false-complete after tool loss
- [D3] `ensure_items` `allow_underground_dfs=False` returning `requires_world_hunt` with 0 blind `mine_dfs` shafts
- [D4] Stale chest `missing_item` self-healing in `WorldMemory`, `72m` chest radius cap, and `post_ensure_task` re-rendering
- [L3-Sched-Gate] Immediate 0-extra-LLM `COMPLETE`, `shiny_gear` `150s` bonus window + `<=35` durability cutoff, `ABORT` on lost pickaxe
- [L3-Sched-PauseResume] `Y < 32` deep-shaft DND vs `Y >= 48` 1-level preemption + Clean Snapshot Resume
- [L3-Sched-WorldJobs] `>=25s` dual-furnace non-blocking ignition, `in_conversion_counts`, `80m` leash, soft ready collection & parent resume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from mc_agent.bot import BotSession
from mc_agent.harness.curriculum import (
    CURRICULUM_PLAYBOOKS,
    CurriculumSupervisor,
    SuspendedEpisode,
    WorldJobRegistry,
    _can_attempt_target,
    _combined_available_counts,
    _compute_reserved_materials,
    select_next_target,
)
from mc_agent.harness.mc_tools import McToolset
from mc_agent.harness.world_memory import WorldMemory
from mc_agent.skills import MacroSkills
from mc_agent.skills.ensure import PhysicalPrerequisiteResolver


class _Phase2World:
    """Deterministic survival mock supporting multi-furnace positions, stale chest errors, durability, and interactions."""

    def __init__(self) -> None:
        self.pos = [0.5, 64.0, 0.5]
        self.is_day = True
        self.health = 20.0
        self.food = 20
        self.items: list[dict[str, Any]] = []
        self.offhand: dict[str, Any] | None = None
        self.armor: dict[str, dict[str, Any]] = {}
        self.held: dict[str, Any] | None = None
        self.completed_advs: set[str] = {
            "minecraft:story/root",
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
            "minecraft:adventure/root",
            "minecraft:husbandry/root",
        }
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.entities: list[dict[str, Any]] = []
        self.chest_items: dict[tuple[int, int, int], dict[str, int]] = {}
        self.furnaces: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.mine_dfs_calls = 0

    def add_item(self, name: str, count: int = 1, *, remaining_durability: int | None = None) -> None:
        if count <= 0:
            return
        for r in self.items:
            if r["name"] == name and remaining_durability is None and "remaining_durability" not in r:
                r["count"] += count
                return
        entry: dict[str, Any] = {"name": name, "count": count, "slot": 9 + len(self.items)}
        if remaining_durability is not None:
            entry["remaining_durability"] = remaining_durability
        self.items.append(entry)

    def count_item(self, name: str) -> int:
        return sum(int(r["count"]) for r in self.items if r["name"] == name)

    def consume_item(self, name: str, count: int) -> bool:
        if name == "any_planks":
            for r in list(self.items):
                if r["name"].endswith("_planks") and r["count"] >= count:
                    return self.consume_item(r["name"], count)
            return False
        if self.count_item(name) < count:
            return False
        rem = count
        kept = []
        for r in self.items:
            if r["name"] == name and rem > 0:
                t = min(rem, r["count"])
                r["count"] -= t
                rem -= t
                if r["count"] > 0:
                    kept.append(r)
            else:
                kept.append(r)
        self.items = kept
        return True

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content.decode("utf-8")) if req.content else {}

        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(self.pos),
                        "dimension": "overworld",
                        "biome": "plains",
                        "is_day": self.is_day,
                        "health": self.health,
                        "food": self.food,
                        "on_ground": True,
                        "in_water": False,
                        "in_lava": False,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": None,
                        "held": self.held,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "items": [dict(r) for r in self.items],
                        "offhand": dict(self.offhand) if self.offhand else None,
                        "armor": {k: dict(v) for k, v in self.armor.items()},
                        "held": dict(self.held) if self.held else None,
                        "empty_slots": max(0, 36 - len(self.items)),
                    },
                },
            )
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"last_seq": 0, "events": []}})
        if path == "/advancements":
            all_ids = [
                "minecraft:story/root",
                "minecraft:story/mine_stone",
                "minecraft:story/upgrade_tools",
                "minecraft:story/smelt_iron",
                "minecraft:story/iron_tools",
                "minecraft:story/obtain_armor",
                "minecraft:story/lava_bucket",
                "minecraft:story/form_obsidian",
                "minecraft:story/mine_diamond",
                "minecraft:story/shiny_gear",
                "minecraft:story/enchant_item",
                "minecraft:adventure/root",
                "minecraft:adventure/trade",
                "minecraft:adventure/use_lodestone",
                "minecraft:adventure/crafters_crafting_crafters",
                "minecraft:adventure/sleep_in_bed",
                "minecraft:husbandry/root",
                "minecraft:husbandry/plant_seed",
                "minecraft:husbandry/make_a_sign_glow",
            ]
            rows = [
                {"id": aid, "status": "completed" if aid in self.completed_advs else "not_started"}
                for aid in all_ids
            ]
            return httpx.Response(200, json={"ok": True, "result": {"advancements": rows}})
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": list(self.entities)}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            bn = self.blocks.get(pt, "air" if pt[1] >= 64 else "stone")
            return httpx.Response(200, json={"ok": True, "result": {"name": bn, "pos": list(pt), "diggable": True}})
        if path == "/find_blocks":
            types = set(body.get("types") or [])
            found = []
            for pt, bn in sorted(self.blocks.items()):
                if bn == "air" or (types and bn not in types):
                    continue
                d = ((pt[0] - self.pos[0]) ** 2 + (pt[1] - self.pos[1]) ** 2 + (pt[2] - self.pos[2]) ** 2) ** 0.5
                if d <= float(body.get("radius", 32)):
                    found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
            return httpx.Response(200, json={"ok": True, "result": {"blocks": found[: int(body.get("limit", 20))]}})
        if path in ("/move_to", "/look", "/control", "/stop", "/posture"):
            if path == "/move_to" and "x" in body and "z" in body:
                self.pos[0] = float(body["x"])
                self.pos[2] = float(body["z"])
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos), "arrived": True}})
        if path == "/equip":
            it = body["item"]
            slot = body.get("slot", "hand")
            if slot == "hand":
                self.held = {"name": it, "count": 1}
            elif slot == "feet" and self.consume_item(it, 1):
                self.armor["feet"] = {"name": it, "count": 1}
                if it == "diamond_boots":
                    self.completed_advs.add("minecraft:story/shiny_gear")
            return httpx.Response(200, json={"ok": True, "result": {"equipped": it, "slot": slot}})
        if path == "/place":
            it = body["item"]
            if body.get("pos"):
                pos_p = tuple(int(v) for v in body["pos"])
            else:
                bx0, by0, bz0 = int(self.pos[0]), int(self.pos[1]), int(self.pos[2])
                pos_p = (bx0 + 1, by0, bz0)
            if self.blocks.get(pos_p, "air") != "air":
                return httpx.Response(400, json={"ok": False, "error": "occupied", "message": "occupied"})
            self.consume_item(it, 1)
            self.blocks[pos_p] = it
            if it == "furnace":
                self.furnaces[pos_p] = {"output": {}}
            return httpx.Response(200, json={"ok": True, "result": {"placed": it, "pos": list(pos_p)}})
        if path == "/interact":
            it = body.get("item")
            pos_i = tuple(int(v) for v in body["pos"]) if body.get("pos") else None
            if it == "compass" and pos_i and self.blocks.get(pos_i) == "lodestone":
                self.completed_advs.add("minecraft:adventure/use_lodestone")
            return httpx.Response(200, json={"ok": True, "result": {"interacted": True}})
        if path == "/craft":
            item = body["item"]
            delta = max(1, int(body.get("count", 1)))
            if item == "furnace":
                self.consume_item("cobblestone", 8 * delta)
                self.add_item("furnace", delta)
            elif item == "stone_bricks":
                batches = max(1, (delta + 3) // 4)
                self.consume_item("stone", 4 * batches)
                self.add_item("stone_bricks", 4 * batches)
            elif item == "stone_brick_slab":
                batches = max(1, (delta + 5) // 6)
                self.consume_item("stone_bricks", 3 * batches)
                self.add_item("stone_brick_slab", 6 * batches)
            elif item == "chiseled_stone_bricks":
                self.consume_item("stone_brick_slab", 2 * delta)
                self.add_item("chiseled_stone_bricks", delta)
            elif item == "lodestone":
                self.consume_item("chiseled_stone_bricks", 8)
                self.consume_item("iron_ingot", 1)
                self.add_item("lodestone", 1)
            elif item == "compass":
                self.consume_item("iron_ingot", 4)
                self.consume_item("redstone", 1)
                self.add_item("compass", 1)
            elif item == "iron_pickaxe":
                self.consume_item("iron_ingot", 3)
                self.consume_item("stick", 2)
                self.add_item("iron_pickaxe", 1)
                self.completed_advs.add("minecraft:story/iron_tools")
            elif item == "diamond_boots":
                self.consume_item("diamond", 4)
                self.add_item("diamond_boots", 1)
            elif item.endswith("_planks"):
                log_nm = item.replace("_planks", "_log")
                batches = max(1, (delta + 3) // 4)
                if not self.consume_item(log_nm, batches):
                    return httpx.Response(400, json={"ok": False, "error": "missing_ingredient", "message": log_nm})
                self.add_item(item, 4 * batches)
            elif item == "stick":
                batches = max(1, (delta + 3) // 4)
                plank_nm = next((r["name"] for r in self.items if r["name"].endswith("_planks") and r["count"] >= 2 * batches), None)
                if not plank_nm or not self.consume_item(plank_nm, 2 * batches):
                    return httpx.Response(400, json={"ok": False, "error": "missing_ingredient", "message": "planks"})
                self.add_item("stick", 4 * batches)
            elif item == "diamond_pickaxe":
                has_table = any(
                    bn == "crafting_table" and ((pt[0] - self.pos[0]) ** 2 + (pt[1] - self.pos[1]) ** 2 + (pt[2] - self.pos[2]) ** 2) ** 0.5 <= 5.5
                    for pt, bn in self.blocks.items()
                )
                if not has_table or not self.consume_item("diamond", 3) or not self.consume_item("stick", 2):
                    return httpx.Response(400, json={"ok": False, "error": "no_table_or_ing", "message": item})
                self.add_item("diamond_pickaxe", 1)
            else:
                return httpx.Response(400, json={"ok": False, "error": "no_recipe", "message": item})
            return httpx.Response(200, json={"ok": True, "result": {"crafted": item, "count": self.count_item(item)}})
        if path == "/window/act":
            target = body.get("target") or {}
            ops = body.get("ops") or []
            if "entity_id" in target:
                self.consume_item("emerald", 1)
                self.add_item("lantern", 1)
                self.completed_advs.add("minecraft:adventure/trade")
                return httpx.Response(200, json={"ok": True, "result": {"done": ops, "window": {"window": "merchant"}}})
            tpos = tuple(int(v) for v in target["pos"]) if isinstance(target.get("pos"), list) else (10, 64, 0)
            if self.blocks.get(tpos) == "furnace" or target.get("block") == "furnace":
                fstate = self.furnaces.setdefault(tpos, {"output": {}})
                for op in ops:
                    if op.get("op") == "put" and op.get("to") == "input":
                        cnt = int(op.get("count", 1))
                        in_it = op["item"]
                        self.consume_item(in_it, cnt)
                        out_it = "stone" if in_it == "cobblestone" else "iron_ingot"
                        fstate["output"][out_it] = fstate["output"].get(out_it, 0) + cnt
                    elif op.get("op") == "put" and op.get("to") == "fuel":
                        self.consume_item(op["item"], int(op.get("count", 1)))
                    elif op.get("op") == "take" and op.get("from") == "output":
                        for out_it, out_cnt in list(fstate["output"].items()):
                            if out_cnt > 0:
                                self.add_item(out_it, out_cnt)
                        fstate["output"] = {}
                return httpx.Response(200, json={"ok": True, "result": {"done": ops, "window": {"window": "furnace"}}})

            # Chest withdraw with `missing_item` error + empty window snapshot when chest was looted!
            box = self.chest_items.setdefault(tpos, {})
            for op in ops:
                if op.get("op") == "take":
                    it = op["item"]
                    if box.get(it, 0) <= 0:
                        rem_sec = [{"name": k, "count": v} for k, v in box.items() if v > 0]
                        return httpx.Response(
                            400,
                            json={
                                "ok": False,
                                "error": {
                                    "code": "missing_item",
                                    "message": f"chest has no {it}",
                                    "window": {
                                        "window": "generic_9x3",
                                        "target": "chest",
                                        "sections": {"container": rem_sec},
                                    },
                                },
                            },
                        )
                    take_n = min(int(op.get("count", 1)), box[it])
                    box[it] -= take_n
                    if box[it] <= 0:
                        box.pop(it, None)
                    self.add_item(it, take_n)
            rem_sec = [{"name": k, "count": v} for k, v in box.items() if v > 0]
            return httpx.Response(
                200,
                json={"ok": True, "result": {"done": ops, "window": {"window": "generic_9x3", "target": "chest", "sections": {"container": rem_sec}}}},
            )
        if path == "/window/view":
            target = body.get("target") or {}
            if "entity_id" in target:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "window": "merchant",
                            "options": [{"index": 0, "available": True, "get": {"name": "lantern", "count": 1}}],
                        },
                    },
                )
            tpos = tuple(int(v) for v in target["pos"]) if isinstance(target.get("pos"), list) else (1, 64, 0)
            fstate = self.furnaces.get(tpos, {"output": {}})
            out_sec = [{"name": k, "count": v} for k, v in fstate["output"].items() if v > 0]
            return httpx.Response(
                200,
                json={"ok": True, "result": {"window": "furnace", "sections": {"input": [], "output": out_sec}, "info": {"burning": False}}},
            )
        raise AssertionError(f"Unhandled mock endpoint: {path}")


def test_d1_hard_physical_prerequisites_and_72m_chest_radius() -> None:
    """[D1 + D4] Empty-bag iron_pickaxe cannot unlock `form_obsidian`, `use_lodestone`, `crafters`, or `enchant_item`; >72m chests are ignored."""
    only_iron_pick = {"iron_pickaxe": 1}
    assert _can_attempt_target("minecraft:story/shiny_gear", only_iron_pick, has_iron_or_better=True) is True
    assert _can_attempt_target("minecraft:story/form_obsidian", only_iron_pick, has_iron_or_better=True) is False
    assert _can_attempt_target("minecraft:adventure/use_lodestone", only_iron_pick, has_iron_or_better=True) is False
    assert _can_attempt_target("minecraft:adventure/crafters_crafting_crafters", only_iron_pick, has_iron_or_better=True) is False
    assert _can_attempt_target("minecraft:story/enchant_item", only_iron_pick, has_iron_or_better=True) is False

    assert _can_attempt_target("minecraft:story/form_obsidian", {"diamond_pickaxe": 1}, has_iron_or_better=True) is True
    assert _can_attempt_target("minecraft:adventure/use_lodestone", {"iron_ingot": 5, "redstone": 1}, has_iron_or_better=True) is True

    # >72m chest must NOT pollute `_combined_available_counts`
    wm = WorldMemory()
    wm.containers[("overworld", 10, 64, 0)] = {"block": "chest", "items": {"iron_ingot": 5}, "updated_at": 1.0}
    wm.containers[("overworld", 180, 64, 0)] = {"block": "chest", "items": {"diamond_pickaxe": 1}, "updated_at": 1.0}
    _, chest_c, comb_c = _combined_available_counts(
        {"items": []}, wm, origin=[0.0, 64.0, 0.0], dim="overworld", max_chest_dist=72.0
    )
    assert chest_c == {"iron_ingot": 5}
    assert comb_c.get("diamond_pickaxe", 0) == 0


def test_d2_d3_d4_objective_bootstrap_no_blind_dfs_and_stale_chest_self_heal(tmp_path: Path) -> None:
    """[D2 + D3 + D4] Historical `story/iron_tools` never causes false 0-LLM completion; `allow_underground_dfs=False` never calls `mine_dfs`; stale empty chest self-heals."""
    world = _Phase2World()
    # Bot died earlier AFTER unlocking `story/iron_tools`, now only holds `stone_pickaxe`!
    world.completed_advs.add("minecraft:story/iron_tools")
    world.add_item("stone_pickaxe", 1)

    pb_iron = CURRICULUM_PLAYBOOKS["bootstrap_iron_age"]
    assert pb_iron.is_objectively_satisfied(world.completed_advs, {"stone_pickaxe": 1}) is False
    assert pb_iron.is_objectively_satisfied(world.completed_advs, {"iron_pickaxe": 1}) is True

    wm = WorldMemory()
    # Stale chest in WorldMemory claims 6 iron_ingot, but real `world.chest_items[(6,64,0)]` is EMPTY!
    wm.containers[("overworld", 6, 64, 0)] = {"block": "chest", "items": {"iron_ingot": 6}, "updated_at": 1.0}
    world.blocks[(6, 64, 0)] = "chest"
    world.chest_items[(6, 64, 0)] = {}

    with BotSession("tok", bot_username="SelfHealBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills.world_memory = wm
        orig_dfs = skills.mine_dfs

        def _forbid_dfs(*args: Any, **kwargs: Any) -> dict[str, Any]:
            world.mine_dfs_calls += 1
            return orig_dfs(*args, **kwargs)

        skills.mine_dfs = _forbid_dfs  # type: ignore[method-assign]
        resolver = PhysicalPrerequisiteResolver(skills, allow_underground_dfs=False)
        res = resolver.ensure_items({"iron_pickaxe": 1}, max_duration_s=20.0)

        assert res["ok"] is False
        assert res["stop_reason"] == "requires_world_hunt"
        assert "raw_iron" in res.get("missing_hunt_items", {})
        assert world.mine_dfs_calls == 0
        # Stale chest was self-healed to empty `{}`!
        assert wm.containers[("overworld", 6, 64, 0)]["items"] == {}

        # Now run `CurriculumSupervisor` on `bootstrap_iron_age`: must NOT falsely skip LLM due to historical `story/iron_tools`!
        world.blocks[(7, 64, 0)] = "crafting_table"
        seen_prompts: list[str] = []

        def fake_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
            user_msgs = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
            seen_prompts.append("\n".join(user_msgs))
            # Craft iron_pickaxe during Episode -> `episode_gate` must stop immediately after Turn 1!
            world.add_item("iron_ingot", 3)
            world.add_item("stick", 2)
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_craft_ip",
                        "type": "function",
                        "function": {"name": "mc_skill_craft_gear", "arguments": json.dumps({"item": "iron_pickaxe"})},
                    }
                ],
            }

        ts = McToolset(bot, world_memory=wm)
        sup = CurriculumSupervisor(
            bot,
            chat_fn=fake_chat,
            runs_dir=tmp_path / "runs",
            duration_s=60.0,
            toolset=ts,
            enable_pre_ensure=True,
        )
        summary = sup.run(max_episodes=1)

    ep0 = summary["episodes"][0]
    assert ep0["target"] == "bootstrap_iron_age"
    # `episode_gate` intercepted right after `mc_skill_craft_gear('iron_pickaxe')` -> ONLY 1 LLM request!
    assert ep0["actor_requests"] == 1
    assert ep0["objective_verified"] is True
    assert len(seen_prompts) == 1
    assert "石镐已备妥" in seen_prompts[0]
    assert "仓储提示" not in seen_prompts[0]


def test_episode_gate_shiny_gear_bonus_window_durability_floor_and_abort(tmp_path: Path) -> None:
    """[L3-Sched-Gate] `shiny_gear` opens 150s bonus window after boots, stops immediately when pickaxe durability <= 35, and `mine_diamond` aborts on lost pickaxe."""
    world = _Phase2World()
    world.add_item("iron_pickaxe", 1, remaining_durability=120)
    world.add_item("stone_pickaxe", 1)
    world.add_item("diamond", 4)
    world.blocks[(1, 64, 0)] = "crafting_table"

    turn = {"n": 0}
    seen_notes: list[str] = []

    with BotSession("tok", bot_username="GateBot", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot, world_memory=WorldMemory())

        def shiny_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
            turn["n"] += 1
            seen_notes.append("\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user"))
            if turn["n"] == 1:
                # Equip diamond_boots -> unlocks `story/shiny_gear`, gate should keep bonus window open!
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "mc_skill_craft_gear",
                                "arguments": json.dumps({"item": "diamond_boots", "equip_slot": "feet"}),
                            },
                        }
                    ],
                }
            if turn["n"] == 2:
                # Simulate iron_pickaxe wearing down to 34 (<= 35 floor!) during bonus mining
                for r in world.items:
                    if r["name"] == "iron_pickaxe":
                        r["remaining_durability"] = 34
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c2", "type": "function", "function": {"name": "mc_find_blocks", "arguments": json.dumps({"types": ["diamond_ore"], "radius": 8})}}
                    ],
                }
            raise AssertionError("Gate failed to terminate shiny_gear when main pick durability hit <= 35!")

        sup = CurriculumSupervisor(bot, chat_fn=shiny_chat, runs_dir=tmp_path / "runs1", duration_s=90.0, toolset=ts)
        s1 = sup.run(max_episodes=1)
        assert s1["episodes"][0]["target"] == "minecraft:story/shiny_gear"
        assert s1["episodes"][0]["actor_requests"] == 2
        assert "顺手摸奖窗口" in seen_notes[1]

        # Test `ABORT` gate when `iron_pickaxe` breaks/disappears mid-episode on `mine_diamond`
        world.completed_advs.add("minecraft:story/iron_tools")
        world.items = [{"name": "iron_pickaxe", "count": 1, "slot": 9}]

        def lose_pick_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
            world.items.clear()  # Iron pickaxe lost!
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c_lost", "type": "function", "function": {"name": "mc_find_blocks", "arguments": json.dumps({"types": ["diamond_ore"], "radius": 8})}}],
            }

        sup2 = CurriculumSupervisor(bot, chat_fn=lose_pick_chat, runs_dir=tmp_path / "runs2", duration_s=60.0, toolset=ts)
        now_m = 9e9
        for k in (
            "minecraft:husbandry/make_a_sign_glow",
            "minecraft:story/deflect_arrow",
            "minecraft:adventure/ol_betsy",
            "minecraft:husbandry/tame_an_animal",
        ):
            sup2.cooldowns[k] = now_m
        s2 = sup2.run(max_episodes=1)
        assert s2["episodes"][0]["target"] == "minecraft:story/mine_diamond"
        assert s2["episodes"][0]["status"] == "aborted"
        assert s2["episodes"][0]["actor_requests"] == 1


def test_single_level_surface_preempt_pause_dnd_underground_and_clean_resume(tmp_path: Path) -> None:
    """[L3-Sched-PauseResume] Deep `Y < 32` is strictly DND; surface `Y >= 48` pauses once for `adventure/trade` (<=32m) and resumes cleanly."""
    world = _Phase2World()
    world.completed_advs.add("minecraft:story/shiny_gear")
    world.add_item("iron_pickaxe", 1)
    world.add_item("emerald", 2)

    with BotSession("tok", bot_username="PreemptBot", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot, world_memory=WorldMemory())
        sup = CurriculumSupervisor(bot, chat_fn=lambda *a, **k: {"role": "assistant", "content": "ok"}, runs_dir=tmp_path / "runs", toolset=ts)

        # 1) Underground Y=-54 DND: even with villager 10m horizontally away, `_detect_surface_preempt_opportunity` MUST return None!
        ts.world_memory.observe_entities([{"type": "passive", "name": "villager", "pos": [10.5, 64.0, 0.5]}], dim="overworld")
        assert (
            sup._detect_surface_preempt_opportunity(
                current_key="minecraft:story/mine_diamond",
                pos=[0.5, -54.0, 0.5],
                dim="overworld",
                is_day=True,
                completed_advs=world.completed_advs,
                combined_counts={"iron_pickaxe": 1, "emerald": 2},
            )
            is None
        )

        # 2) Clear memory; run `make_a_sign_glow` at Y=64 -> Turn 1 spots villager at 12m -> PAUSE -> Ep 2 runs `trade` -> Ep 3 RESUME `make_a_sign_glow`!
        ts.world_memory = WorldMemory()
        ts.world_memory.observe_entities([{"type": "passive", "name": "glow_squid", "pos": [65.0, 50.0, 65.0]}], dim="overworld")
        world.entities = [{"id": 77, "type": "passive", "name": "villager", "pos": [12.5, 64.0, 0.5], "dist": 12.0}]
        # Also put a lava pool at (15, 64, 0) + bucket so we verify Ep 2 (`is_preempt_branch=True`) does NOT nested-pause a 2nd level!
        world.blocks[(15, 64, 0)] = "lava"
        world.add_item("bucket", 1)

        ep_prompts: list[tuple[int, str]] = []

        def preempt_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
            ep_no = len(sup2.episodes) + 1
            joined = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
            ep_prompts.append((ep_no, joined))
            if ep_no == 1:
                # Scan entities & blocks -> discovers villager (12m) & lava (15m) -> triggers PAUSE!
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "scan_e", "type": "function", "function": {"name": "mc_get_entities", "arguments": json.dumps({"radius": 32})}},
                        {"id": "scan_b", "type": "function", "function": {"name": "mc_find_blocks", "arguments": json.dumps({"types": ["lava"], "radius": 32})}},
                    ],
                }
            if ep_no == 2:
                # Preempting `adventure/trade` episode executes merchant trade!
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "tr_1", "type": "function", "function": {"name": "mc_skill_trade", "arguments": json.dumps({"entity_id": 77, "option": 0})}}
                    ],
                }
            # Ep 3: Resumed `make_a_sign_glow`
            world.pos = [22.5, 64.0, -4.5]
            return {"role": "assistant", "content": "恢复执行告示牌探索完毕。"}

        sup2 = CurriculumSupervisor(bot, chat_fn=preempt_chat, runs_dir=tmp_path / "runs_p", duration_s=180.0, toolset=ts)
        for skip_k in ("minecraft:story/lava_bucket", "minecraft:husbandry/tactical_fishing", "minecraft:story/deflect_arrow"):
            if skip_k != "minecraft:story/lava_bucket":
                sup2.cooldowns[skip_k] = 9e9
        # Make `make_a_sign_glow` (30pt, tier 1) win Episode 1 before villager is known; keep `lava_bucket` uncooled so Ep 2 tests 1-level cap!
        summary = sup2.run(max_episodes=3)

    eps = summary["episodes"]
    assert [e["target"] for e in eps] == [
        "minecraft:husbandry/make_a_sign_glow",
        "minecraft:adventure/trade",
        "minecraft:husbandry/make_a_sign_glow",
    ]
    assert eps[0]["status"] == "paused"
    assert sup2.strikes.get("minecraft:husbandry/make_a_sign_glow", 0) <= 1  # Pause in Ep 1 gave 0 strikes!
    assert "minecraft:adventure/trade" in eps[1]["gained_advancements"]
    # Episode 3 received Clean Snapshot Resume Briefing!
    assert "挂起恢复简报" in ep_prompts[-1][1]


def test_world_job_registry_dual_furnace_nonblocking_leash_and_soft_resume(tmp_path: Path) -> None:
    """[L3-Sched-WorldJobs] Smelting 12 stone (>=25s) splits 6+6 across 2 furnaces, pauses parent with 80m leash, then soft-collects & resumes `use_lodestone` with `post_ensure_task`."""
    world = _Phase2World()
    world.completed_advs.update({"minecraft:story/shiny_gear", "minecraft:husbandry/make_a_sign_glow"})
    world.add_item("iron_pickaxe", 1)
    world.add_item("cobblestone", 12)
    world.add_item("furnace", 2)
    world.add_item("coal", 4)
    world.add_item("iron_ingot", 5)
    world.add_item("redstone", 1)
    world.blocks[(0, 64, 2)] = "crafting_table"

    seen_ep_tasks: list[tuple[int, str]] = []

    with BotSession("tok", bot_username="FurnaceBot", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot, world_memory=WorldMemory())

        def job_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
            ep_idx = len(sup.episodes) + 1
            joined = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
            seen_ep_tasks.append((ep_idx, joined))
            if ep_idx == 2:
                # Opportunistic episode while dual furnaces burn -> fast-forward furnace `ready_at` before Episode 3!
                for j in sup.world_jobs.jobs.values():
                    assert j.total_count == 12
                    assert len(j.furnaces) == 2
                    assert sup.world_jobs.in_conversion_counts(dim="overworld").get("stone") == 12
                    assert sup.world_jobs.active_leash(dim="overworld") is not None
                    j.ready_at = 0.0
                return {"role": "assistant", "content": "熔炉旁顺手整备完成。"}
            if ep_idx == 3:
                # Resumed `use_lodestone` AFTER soft collection + `pre_ensure` crafted `lodestone` & `compass`!
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "pl_lode", "type": "function", "function": {"name": "mc_place", "arguments": json.dumps({"item": "lodestone", "x": 2, "y": 64, "z": 2})}},
                        {"id": "bind_lode", "type": "function", "function": {"name": "mc_interact", "arguments": json.dumps({"x": 2, "y": 64, "z": 2, "item": "compass"})}},
                    ],
                }
            return {"role": "assistant", "content": "ok"}

        sup = CurriculumSupervisor(
            bot,
            chat_fn=job_chat,
            runs_dir=tmp_path / "runs_f",
            duration_s=180.0,
            toolset=ts,
            enable_pre_ensure=True,
            enable_async_world_jobs=True,
        )
        for k in (
            "minecraft:story/deflect_arrow",
            "minecraft:story/lava_bucket",
            "minecraft:husbandry/tactical_fishing",
            "minecraft:adventure/ol_betsy",
        ):
            sup.cooldowns[k] = 9e9

        summary = sup.run(max_episodes=3)

    eps = summary["episodes"]
    # Ep 1: `use_lodestone` ignited 2 furnaces (6+6 stone) and yielded with 0 LLM calls!
    assert eps[0]["target"] == "minecraft:adventure/use_lodestone"
    assert eps[0]["status"] == "paused_for_world_job"
    assert eps[0]["actor_requests"] == 0
    # Ep 2: Opportunistic short/micro target ran under 80m furnace leash!
    assert eps[1]["target"] != "minecraft:adventure/use_lodestone"
    assert "后台熔炉保活围栏" in seen_ep_tasks[0][1] or eps[1]["target"] == "furnace_leash_prep"
    # Ep 3: Soft-collected 12 stone -> crafted `lodestone` + `compass` in `pre_ensure` -> rendered `post_ensure_task` -> unlocked `use_lodestone` in 1 LLM call!
    assert eps[2]["target"] == "minecraft:adventure/use_lodestone"
    assert "minecraft:adventure/use_lodestone" in eps[2]["gained_advancements"]
    assert eps[2]["actor_requests"] == 1
    assert "背包已备齐 lodestone×1 与 compass×1" in seen_ep_tasks[-1][1]
    assert sup.world_jobs.jobs == {}


def test_suspended_parent_material_reservation_prevents_opportunistic_iron_cannibalization() -> None:
    """When `use_lodestone` (needs 5 iron_ingot + 1 redstone) is suspended on furnace burn, opportunistic 5-pt `obtain_armor` cannot eat its reserved iron."""
    world = _Phase2World()
    world.completed_advs.update(
        {"minecraft:story/shiny_gear", "minecraft:husbandry/make_a_sign_glow", "minecraft:story/lava_bucket"}
    )
    world.add_item("iron_pickaxe", 1)
    # 6 iron_ingot left in chest; `use_lodestone` reserves 5 -> only 1 unreserved (<4 needed by `obtain_armor`!)
    wm = WorldMemory()
    wm.containers[("overworld", 2, 64, 0)] = {
        "block": "chest",
        "items": {"iron_ingot": 6, "redstone": 1},
        "updated_at": 1.0,
    }
    susp = SuspendedEpisode(
        target=CURRICULUM_PLAYBOOKS["minecraft:adventure/use_lodestone"],
        remaining_calls=20,
        remaining_timeout_s=500.0,
        anchor_pos=[0.0, 64.0, 0.0],
        reason="waiting_for_world_job",
        progress_digest="burning stone",
        waiting_job_id="fjob-1",
    )
    reserved = _compute_reserved_materials(susp, {"iron_pickaxe": 1})
    assert reserved.get("iron_ingot") == 5
    assert reserved.get("redstone") == 1

    with BotSession("tok", bot_username="ReserveBot", transport=httpx.MockTransport(world.handler)) as bot:
        # Without reservation, `obtain_armor` would be selectable; WITH reservation, `obtain_armor` is blocked!
        cooled = {
            "minecraft:story/deflect_arrow": 9e9,
            "minecraft:husbandry/tactical_fishing": 9e9,
            "minecraft:adventure/ol_betsy": 9e9,
            "minecraft:husbandry/tame_an_animal": 9e9,
        }
        nxt, _ = select_next_target(
            bot,
            cooldowns=cooled,
            now=1.0,
            world_memory=wm,
            leash=([0, 64, 0], 80.0),
            exclude_keys={"minecraft:adventure/use_lodestone"},
            reserved_counts=reserved,
        )
        assert nxt.key != "minecraft:story/obtain_armor"


def test_interact_with_air_and_blocked_by_self_circuit_breaker() -> None:
    """Clicking empty air with `mc_interact` converts to `interacted_with_air` and trips `_RepeatFailureGuard` instead of spinning 12 turns."""
    from mc_agent.harness.agent import run_mc_agent

    def air_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "lodestone", "count": 1}, {"name": "compass", "count": 1}]}})
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": []}})
        if path == "/place":
            return httpx.Response(400, json={"ok": False, "error": {"code": "blocked_by_self", "message": "standing in cell"}})
        if path == "/interact":
            return httpx.Response(200, json={"ok": True, "result": {"block": "air", "before": {}, "after": {"name": "air"}, "changed": False}})
        raise AssertionError(path)

    turn = {"n": 0}
    tool_outputs: list[str] = []

    def chat_fn(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        turn["n"] += 1
        for m in messages:
            if m.get("role") == "tool":
                tool_outputs.append(str(m.get("content") or ""))
        if turn["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "p1", "type": "function", "function": {"name": "mc_place", "arguments": json.dumps({"item": "lodestone", "x": 0, "y": 64, "z": 0})}}],
            }
        if turn["n"] in (2, 3):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"i{turn['n']}", "type": "function", "function": {"name": "mc_interact", "arguments": json.dumps({"item": "compass", "x": 0, "y": 64, "z": 0})}}],
            }
        return {"role": "assistant", "content": "done"}

    with BotSession("tok", bot_username="AirBot", transport=httpx.MockTransport(air_handler)) as bot:
        rep = run_mc_agent(
            "放置并绑定磁石",
            bot,
            chat_fn=chat_fn,
            max_calls=5,
            include_initial_snapshot=False,
            extra_tools=("mc_interact",),
        )
    assert rep["status"] == "answered"
    assert any("不传 x/y/z" in o for o in tool_outputs)
    assert any("interacted_with_air" in o for o in tool_outputs)
    assert any("repeated_identical_failure_blocked" in o for o in tool_outputs)


def test_collect_job_preserves_world_job_when_furnace_unreachable_on_api_error() -> None:
    """`WorldJobRegistry.collect_job` MUST NOT pop a finished furnace job if `ApiError` prevented reaching/opening the furnace."""
    world = _Phase2World()
    world.blocks[(10, 64, 0)] = "furnace"
    world.furnaces[(10, 64, 0)] = {"output": {"stone": 6}}

    fail_move = {"on": True}
    orig_handler = world.handler

    def flaky_handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        if fail_move["on"] and path in ("/move_to", "/window/view"):
            return httpx.Response(400, json={"ok": False, "error": {"code": "no_path", "message": "blocked"}})
        return orig_handler(req)

    reg = WorldJobRegistry()
    job = reg.register_furnace_job(
        dim="overworld",
        furnaces=[{"pos": [10, 64, 0], "count": 6}],
        input_item="cobblestone",
        output_item="stone",
        total_count=6,
        ready_at=0.0,
    )

    with BotSession("tok", bot_username="SafeCollect", transport=httpx.MockTransport(flaky_handler)) as bot:
        skills = MacroSkills(bot)
        res1 = reg.collect_job(skills, job.job_id, wait_remaining_s=1.0)
        assert res1["ok"] is False
        assert job.job_id in reg.jobs  # Job preserved!

        fail_move["on"] = False
        res2 = reg.collect_job(skills, job.job_id, wait_remaining_s=1.0)
        assert res2["ok"] is True
        assert job.job_id not in reg.jobs
        assert world.count_item("stone") == 6


def test_ensure_items_throttles_stabilize_when_hungry_without_food() -> None:
    """When `food <= 12` and bag has no food, recursive `_satisfy_item` calls must throttle `stabilize` instead of spamming 9+ times."""
    world = _Phase2World()
    world.food = 10  # Hungry, no food in inventory
    world.add_item("stone_pickaxe", 1)

    with BotSession("tok", bot_username="HungryBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = PhysicalPrerequisiteResolver(skills, allow_underground_dfs=False).ensure_items(
            {"iron_pickaxe": 1, "bucket": 1},
            max_duration_s=15.0,
        )
    stab_steps = [s for s in res.get("steps", []) if s.get("step") == "stabilize"]
    assert len(stab_steps) == 1


def test_deep_underground_no_pickaxe_never_punches_deepslate_and_reuses_memory_table() -> None:
    """At Y=-54 with 0 pickaxes, `ensure_items` must NEVER call `dig_staircase` bare-handed, and must reuse a WorldMemory crafting_table within 64m."""
    world = _Phase2World()
    world.pos = [0.0, -54.0, 0.0]
    world.add_item("diamond", 3)
    world.add_item("birch_log", 1)  # Only 4 planks: enough for sticks ONLY if existing crafting_table is reused!
    world.blocks[(42, -54, 0)] = "crafting_table"

    wm = WorldMemory()
    wm.record_poi("crafting_table", [42, -54, 0], dim="overworld")

    stair_calls = {"n": 0}
    with BotSession("tok", bot_username="DeepBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills.world_memory = wm

        def _forbid_stair(*args: Any, **kwargs: Any) -> dict[str, Any]:
            stair_calls["n"] += 1
            return {"ok": False}

        skills.dig_staircase = _forbid_stair  # type: ignore[method-assign]
        res = PhysicalPrerequisiteResolver(skills, allow_underground_dfs=False).ensure_items(
            {"diamond_pickaxe": 1},
            max_duration_s=20.0,
        )
        assert res["ok"] is True
        assert world.count_item("diamond_pickaxe") == 1
        assert stair_calls["n"] == 0

        # Now empty all wood & pickaxes at Y=-54 -> must return `requires_world_hunt` immediately without calling `dig_staircase`!
        world.items = []
        res_empty = PhysicalPrerequisiteResolver(skills, allow_underground_dfs=False).ensure_items(
            {"stone_pickaxe": 1},
            max_duration_s=20.0,
        )
        assert res_empty["ok"] is False
        assert res_empty["stop_reason"] == "requires_world_hunt"
        assert stair_calls["n"] == 0
