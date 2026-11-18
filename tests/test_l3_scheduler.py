"""Tests for [L3-Sched-1/2/3] in isolated workspace:
- Persistent single-session McToolset + dynamic 5-tier budgets + material-progress strike immunity
- 0-LLM deterministic `ensure_items` (recipes + smelting + pickaxe tiers + chest withdraw + survival guard + zero soft gates)
- `select_next_target` joint bag/offhand/chest inventory + WorldMemory dynamic boosts + bounded island recon + 0-LLM pre-ensure completion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

import mc_agent
from mc_agent.bot import BotSession
from mc_agent.harness.curriculum import (
    CURRICULUM_PLAYBOOKS,
    CurriculumSupervisor,
    resolve_episode_budget,
    select_next_target,
)
from mc_agent.harness.mc_tools import McToolset
from mc_agent.harness.world_memory import WorldMemory
from mc_agent.skills import MacroSkills


def test_workspace_isolation_guard() -> None:
    """Ensure tests run strictly against the isolated `.worktrees/l3-sched/src` tree when present."""
    this_dir = Path(__file__).resolve().parent.parent
    mod_path = Path(mc_agent.__file__).resolve()
    assert mod_path.is_relative_to(this_dir / "src")


class _MockSurvivalWorld:
    """Deterministic mock survival server supporting wood, stone, iron, smelting, crafting, chests, and advancements."""

    def __init__(self) -> None:
        self.pos = [0.5, 64.0, 0.5]
        self.health = 20.0
        self.food = 20
        self.nearest_hostile: dict[str, Any] | None = None
        self.items: list[dict[str, Any]] = []
        self.offhand: dict[str, Any] | None = None
        self.armor: dict[str, dict[str, Any]] = {}
        self.held: dict[str, Any] | None = None
        self.completed_advs: set[str] = {"minecraft:story/root", "minecraft:adventure/root"}
        self.blocks: dict[tuple[int, int, int], str] = {
            (2, 64, 0): "oak_log",
            (2, 65, 0): "oak_log",
            (2, 66, 0): "oak_log",
            (2, 67, 0): "oak_log",
            (3, 64, 0): "stone",
            (3, 64, 1): "stone",
            (3, 64, 2): "stone",
            (3, 64, 3): "stone",
            (4, 64, 0): "stone",
            (4, 64, 1): "stone",
            (4, 64, 2): "stone",
            (4, 64, 3): "stone",
            (5, 64, 0): "stone",
            (5, 64, 1): "stone",
            (5, 64, 2): "stone",
            (5, 64, 3): "stone",
            (6, 64, 0): "coal_ore",
            (6, 64, 1): "coal_ore",
            (7, 64, 0): "iron_ore",
            (7, 64, 1): "iron_ore",
            (7, 64, 2): "iron_ore",
            (7, 64, 3): "iron_ore",
            (7, 64, 4): "iron_ore",
            (7, 64, 5): "iron_ore",
        }
        self.chest_items: dict[tuple[int, int, int], dict[str, int]] = {}
        self.smelt_calls = 0
        self.ore_mine_calls = 0

    def add_item(self, name: str, count: int = 1) -> None:
        if count <= 0:
            return
        for r in self.items:
            if r["name"] == name:
                r["count"] += count
                return
        self.items.append({"name": name, "count": count, "slot": 9 + len(self.items)})

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
                        "is_day": True,
                        "health": self.health,
                        "food": self.food,
                        "on_ground": True,
                        "in_water": False,
                        "in_lava": False,
                        "alive": True,
                        "ready": True,
                        "nearest_hostile": self.nearest_hostile,
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
                "minecraft:story/mine_diamond",
                "minecraft:story/shiny_gear",
                "minecraft:adventure/root",
                "minecraft:adventure/trade",
                "minecraft:adventure/sleep_in_bed",
                "minecraft:husbandry/make_a_sign_glow",
            ]
            rows = [
                {"id": aid, "status": "completed" if aid in self.completed_advs else "not_started"}
                for aid in all_ids
            ]
            return httpx.Response(200, json={"ok": True, "result": {"advancements": rows}})
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": []}})
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            bn = self.blocks.get(pt, "air" if pt[1] >= 64 else "stone")
            return httpx.Response(200, json={"ok": True, "result": {"name": bn, "pos": list(pt), "diggable": True}})
        if path == "/find_blocks":
            types = set(body.get("types") or [])
            found = []
            for pt, bn in sorted(self.blocks.items()):
                if bn == "air":
                    continue
                if types and bn not in types:
                    continue
                d = ((pt[0] - self.pos[0]) ** 2 + (pt[1] - self.pos[1]) ** 2 + (pt[2] - self.pos[2]) ** 2) ** 0.5
                if d <= float(body.get("radius", 32)):
                    found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
            return httpx.Response(200, json={"ok": True, "result": {"blocks": found[: int(body.get("limit", 20))]}})
        if path == "/move_to":
            if "x" in body and "z" in body:
                self.pos[0] = float(body["x"])
                self.pos[2] = float(body["z"])
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos), "arrived": True}})
        if path == "/look" or path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos)}})
        if path == "/equip":
            it = body["item"]
            slot = body.get("slot", "hand")
            if slot == "hand":
                self.held = {"name": it, "count": 1}
            elif slot == "off-hand":
                if self.consume_item(it, 1):
                    self.offhand = {"name": it, "count": 1}
            elif slot in ("head", "torso", "legs", "feet"):
                if self.consume_item(it, 1):
                    self.armor[slot] = {"name": it, "count": 1}
                    if it == "iron_boots":
                        self.completed_advs.add("minecraft:story/obtain_armor")
                    if it == "diamond_boots":
                        self.completed_advs.add("minecraft:story/shiny_gear")
            return httpx.Response(200, json={"ok": True, "result": {"equipped": it, "slot": slot}})
        if path == "/place":
            it = body["item"]
            if body.get("pos"):
                pos_p = tuple(int(v) for v in body["pos"])
            else:
                bx0, by0, bz0 = int(self.pos[0]), int(self.pos[1]), int(self.pos[2])
                pos_p = next(
                    (
                        (bx0 + dx, by0, bz0 + dz)
                        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1))
                        if self.blocks.get((bx0 + dx, by0, bz0 + dz), "air") == "air"
                    ),
                    (bx0 + 1, by0, bz0),
                )
            self.consume_item(it, 1)
            self.blocks[pos_p] = it
            return httpx.Response(200, json={"ok": True, "result": {"placed": it, "pos": list(pos_p)}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            bn = self.blocks.get(pt, "stone")
            if bn.endswith("_ore") or bn in ("stone", "cobblestone"):
                self.ore_mine_calls += 1
            self.blocks[pt] = "air"
            drop_map = {
                "stone": "cobblestone",
                "iron_ore": "raw_iron",
                "coal_ore": "coal",
                "diamond_ore": "diamond",
            }
            drop = drop_map.get(bn, bn)
            self.add_item(drop, 1)
            if drop == "cobblestone":
                self.completed_advs.add("minecraft:story/mine_stone")
            return httpx.Response(200, json={"ok": True, "result": {"dug": bn, "pos": list(pt)}})
        if path == "/craft":
            item = body["item"]
            want_total = int(body.get("count", 1))
            cur_have = self.count_item(item)
            delta = max(1, want_total - cur_have)
            if item == "oak_planks":
                logs = max(1, (delta + 3) // 4)
                self.consume_item("oak_log", logs)
                self.add_item("oak_planks", logs * 4)
            elif item == "stick":
                batches = max(1, (delta + 3) // 4)
                self.consume_item("any_planks", batches * 2)
                self.add_item("stick", batches * 4)
            elif item == "crafting_table":
                self.consume_item("any_planks", 4 * delta)
                self.add_item("crafting_table", delta)
            elif item == "furnace":
                self.consume_item("cobblestone", 8 * delta)
                self.add_item("furnace", delta)
            elif item == "wooden_pickaxe":
                self.consume_item("any_planks", 3)
                self.consume_item("stick", 2)
                self.add_item("wooden_pickaxe", 1)
            elif item == "stone_pickaxe":
                self.consume_item("cobblestone", 3)
                self.consume_item("stick", 2)
                self.add_item("stone_pickaxe", 1)
                self.completed_advs.add("minecraft:story/upgrade_tools")
            elif item == "iron_pickaxe":
                self.consume_item("iron_ingot", 3)
                self.consume_item("stick", 2)
                self.add_item("iron_pickaxe", 1)
                self.completed_advs.add("minecraft:story/iron_tools")
            elif item == "bucket":
                self.consume_item("iron_ingot", 3 * delta)
                self.add_item("bucket", delta)
            elif item == "iron_boots":
                self.consume_item("iron_ingot", 4)
                self.add_item("iron_boots", 1)
                self.completed_advs.add("minecraft:story/obtain_armor")
            else:
                return httpx.Response(400, json={"ok": False, "error": "no_recipe", "message": f"unknown {item}"})
            return httpx.Response(200, json={"ok": True, "result": {"crafted": item, "count": self.count_item(item)}})
        if path == "/window/act":
            target = body.get("target") or {}
            ops = body.get("ops") or []
            # Furnace smelting simulation
            if target.get("block") == "furnace" or (isinstance(target.get("pos"), list) and self.blocks.get(tuple(target["pos"])) == "furnace"):
                for op in ops:
                    if op.get("op") == "put" and op.get("to") == "input":
                        self.smelt_calls += 1
                        cnt = int(op.get("count", 1))
                        in_it = op["item"]
                        self.consume_item(in_it, cnt)
                        self._furnace_ready = ("iron_ingot" if in_it == "raw_iron" else "charcoal", cnt)
                    elif op.get("op") == "put" and op.get("to") == "fuel":
                        self.consume_item(op["item"], int(op.get("count", 1)))
                    elif op.get("op") == "take" and op.get("from") == "output":
                        out_it, out_cnt = getattr(self, "_furnace_ready", ("iron_ingot", 1))
                        self.add_item(out_it, out_cnt)
                        if out_it == "iron_ingot":
                            self.completed_advs.add("minecraft:story/smelt_iron")
                        self._furnace_ready = (out_it, 0)
                return httpx.Response(200, json={"ok": True, "result": {"done": ops, "window": {"window": "furnace", "sections": {"output": []}}}})
            # Chest withdraw simulation
            cpos = tuple(target.get("pos") or (10, 64, 0))
            box = self.chest_items.setdefault(cpos, {})
            for op in ops:
                if op.get("op") == "take":
                    it = op["item"]
                    take_n = min(int(op.get("count", 1)), box.get(it, 0))
                    box[it] = box.get(it, 0) - take_n
                    if box[it] <= 0:
                        box.pop(it, None)
                    self.add_item(it, take_n)
            rem_sec = [{"name": k, "count": v} for k, v in box.items() if v > 0]
            return httpx.Response(
                200,
                json={"ok": True, "result": {"done": ops, "window": {"window": "generic_9x3", "target": "chest", "sections": {"container": rem_sec}}}},
            )
        if path == "/window/view":
            out_it, out_cnt = getattr(self, "_furnace_ready", ("iron_ingot", 0))
            out_sec = [{"name": out_it, "count": out_cnt}] if out_cnt > 0 else []
            return httpx.Response(
                200,
                json={"ok": True, "result": {"window": "furnace", "sections": {"input": [], "output": out_sec}, "info": {"progress": 1.0}}},
            )
        if path == "/use_item":
            self.food = 20
            self.health = min(20.0, self.health + 4.0)
            return httpx.Response(200, json={"ok": True, "result": {"used": body.get("item")}})
        raise AssertionError(f"Unhandled mock endpoint: {path}")


def test_ensure_items_zero_llm_full_chain_from_bare_hands_without_soft_gates() -> None:
    """[L3-Sched-2] Bare-hands `ensure_items({'iron_pickaxe': 1, 'bucket': 1})` resolves wood->stone->iron->smelt->craft with 0 soft gates."""
    world = _MockSurvivalWorld()
    with BotSession("tok", bot_username="Builder", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.ensure_items({"iron_pickaxe": 1, "bucket": 1}, max_duration_s=90.0)

    assert res["ok"] is True
    assert res["remaining_deficit"] == {}
    assert world.count_item("iron_pickaxe") == 1
    assert world.count_item("bucket") == 1
    # Zero subjective soft gates: never forced crafting shield, bed, or armor!
    assert world.count_item("shield") == 0
    assert world.count_item("white_bed") == 0
    assert not world.armor


def test_ensure_items_prefers_storage_chest_and_reports_hunt_items_or_threat() -> None:
    """[L3-Sched-2] Withdraws stored ingots from WorldMemory chest before mining; cleanly hands back mob-drop items & combat hits."""
    world = _MockSurvivalWorld()
    world.chest_items[(10, 64, 0)] = {"iron_ingot": 6}
    wm = WorldMemory()
    wm.containers[("overworld", 10, 64, 0)] = {"block": "chest", "items": {"iron_ingot": 6}, "updated_at": 1.0}
    wm.record_poi("crafting_table", (1, 64, 0), dim="overworld")
    world.blocks[(1, 64, 0)] = "crafting_table"

    with BotSession("tok", bot_username="ChestUser", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills.world_memory = wm
        # 1) Crafts iron_boots by withdrawing 4 iron_ingot from chest at (10,64,0) with ZERO mining/smelting!
        res_boots = skills.ensure_items({"iron_boots": 1}, equip_slots={"iron_boots": "feet"})
        assert res_boots["ok"] is True
        assert world.smelt_calls == 0
        assert world.ore_mine_calls == 0
        assert world.armor.get("feet", {}).get("name") == "iron_boots"

        # 2) Requesting crossbow (needs mob-drop `string`) cleanly stops with `requires_world_hunt`
        world.add_item("oak_log", 2)
        res_hunt = skills.ensure_items({"crossbow": 1}, max_duration_s=30.0)
        assert res_hunt["ok"] is False
        assert res_hunt["stop_reason"] == "requires_world_hunt"
        assert "string" in res_hunt.get("missing_hunt_items", {})

        # 3) Fresh combat damage interrupts cleanly instead of blind execution
        world.health = 20.0
        def _hurt_after_status():
            st = {"pos": [0.5, 64.0, 0.5], "health": 15.0, "food": 20, "alive": True, "ready": True, "nearest_hostile": {"name": "skeleton", "dist": 5.0}}
            return st

        calls_n = {"k": 0}
        orig_status = bot.status
        def _status_seq():
            calls_n["k"] += 1
            if calls_n["k"] == 1:
                return orig_status()
            return _hurt_after_status()
        bot.status = _status_seq  # type: ignore[method-assign]
        res_thr = skills.ensure_items({"bucket": 2}, max_duration_s=20.0)
        assert res_thr["ok"] is False
        assert res_thr["stop_reason"] == "threat_interrupted"


def test_persistent_single_session_dynamic_budgets_and_material_progress_immunity(tmp_path: Path) -> None:
    """[L3-Sched-1] Shared McToolset persists WorldMemory & MiningDFS across episodes, scales budgets, and exempts diamond progress from strikes."""
    world = _MockSurvivalWorld()
    world.offhand = {"name": "iron_pickaxe", "count": 1}  # Offhand iron_pickaxe must be recognized!

    # Check 5-tier dynamic budget profiles when explicit overrides are None
    b_micro = resolve_episode_budget(CURRICULUM_PLAYBOOKS["minecraft:story/obtain_armor"])
    b_deep = resolve_episode_budget(CURRICULUM_PLAYBOOKS["minecraft:story/shiny_gear"])
    b_exp = resolve_episode_budget(CURRICULUM_PLAYBOOKS["minecraft:story/enter_the_nether"])
    assert b_micro == (8, 180.0)
    assert b_deep == (40, 1500.0)
    assert b_exp == (45, 1800.0)

    ep_counter = {"n": 0}
    seen_toolset_ids: list[int] = []

    with BotSession("tok", bot_username="PersistBot", transport=httpx.MockTransport(world.handler)) as bot:
        shared_ts = McToolset(bot, world_memory=WorldMemory())

        def fake_chat(messages, tools=None, **kwargs):
            ep_counter["n"] += 1
            seen_toolset_ids.append(id(shared_ts))
            if ep_counter["n"] == 1:
                # Episode 1 records a POI in WorldMemory and gains 2 diamonds (partial progress toward 4-diamond shiny_gear!)
                shared_ts.world_memory.record_poi("lava", (45, 63, -12), dim="overworld")
                world.add_item("diamond", 2)
            elif ep_counter["n"] == 2:
                # Episode 2 gains 1 more diamond -> still partial progress, must NOT trigger 2-strike cooldown!
                world.add_item("diamond", 1)
            return {"role": "assistant", "content": "阶段切片完成。"}

        sup = CurriculumSupervisor(
            bot,
            chat_fn=fake_chat,
            runs_dir=tmp_path / "runs",
            duration_s=120.0,
            strike_limit=2,
            toolset=shared_ts,
        )
        summary = sup.run(max_episodes=2)

    eps = summary["episodes"]
    assert len(eps) == 2
    # Both episodes selected shiny_gear (offhand iron_pickaxe recognized!) with `deep` budget (40 calls)
    assert eps[0]["target"] == "minecraft:story/shiny_gear"
    assert eps[0]["budget_class"] == "deep"
    assert eps[0]["budget_max_calls"] == 40
    assert eps[0]["material_progress"] is True
    assert eps[1]["target"] == "minecraft:story/shiny_gear"
    assert eps[1]["material_progress"] is True
    assert eps[1]["cooled_down"] is False  # Exempted from 2-strike penalty because diamonds increased!
    # WorldMemory POI recorded in Episode 1 survived intact in persistent toolset!
    assert ("overworld", 45, 63, -12) in shared_ts.world_memory.pois


def test_world_memory_boosts_villager_trade_chest_hints_island_recon_and_zero_llm_completion(tmp_path: Path) -> None:
    """[L3-Sched-3] WorldMemory boosts `adventure/trade` to Tier 1, injects chest hints, schedules bounded island recon once, and completes `obtain_armor` with 0 LLM calls."""
    world = _MockSurvivalWorld()
    world.completed_advs.update(
        {"minecraft:story/smelt_iron", "minecraft:story/shiny_gear", "minecraft:husbandry/make_a_sign_glow"}
    )
    # Iron pickaxe + 4 iron ingots are stored in home chest (bag is empty!)
    world.chest_items[(8, 64, 0)] = {"iron_pickaxe": 1, "iron_ingot": 4}
    world.blocks[(1, 64, 0)] = "crafting_table"

    wm = WorldMemory()
    wm.containers[("overworld", 8, 64, 0)] = {
        "block": "chest",
        "items": {"iron_pickaxe": 1, "iron_ingot": 4},
        "updated_at": 1.0,
    }

    with BotSession("tok", bot_username="SmartBot", transport=httpx.MockTransport(world.handler)) as bot:
        # 1) With <16 chunks and `enable_island_recon=True`, `island_recon_sweep` (20 pts, tier 1) wins over 15-pt branches!
        t_recon, _ = select_next_target(bot, world_memory=wm, enable_island_recon=True, recon_done=False)
        assert t_recon.key == "island_recon_sweep"
        assert "严禁挖矿" in t_recon.task

        # 2) Once villager habitat is discovered in WorldMemory and recon is done, `adventure/trade` (15 pts) is boosted to Tier 1 with coordinates + chest hint!
        wm.observe_entities([{"type": "passive", "name": "villager", "pos": [60.5, 65.0, -20.5]}], dim="overworld")
        cooled_other_t1 = {
            "minecraft:story/deflect_arrow": 9e9,
            "minecraft:story/form_obsidian": 9e9,
            "minecraft:story/lava_bucket": 9e9,
            "minecraft:adventure/crafters_crafting_crafters": 9e9,
            "minecraft:adventure/ol_betsy": 9e9,
            "minecraft:adventure/use_lodestone": 9e9,
            "minecraft:husbandry/tactical_fishing": 9e9,
        }
        t_trade, _ = select_next_target(
            bot,
            cooldowns=cooled_other_t1,
            now=1.0,
            world_memory=wm,
            enable_island_recon=True,
            recon_done=True,
        )
        assert t_trade.key == "minecraft:adventure/trade"
        assert t_trade.tier == 1
        assert "villager 聚落" in t_trade.task
        assert "仓储提示" in t_trade.task
        assert "mc_skill_trade" in t_trade.extra_tools
        assert "mc_skill_container" in t_trade.extra_tools

        # 3) Zero-LLM pre-ensure completion: when `obtain_armor` runs with `enable_pre_ensure=True`,
        # it withdraws 4 iron_ingot from chest, crafts & equips `iron_boots`, and finishes Episode with 0 LLM requests!
        llm_calls = {"n": 0}
        def never_called_chat(*args, **kwargs):
            llm_calls["n"] += 1
            return {"role": "assistant", "content": "不应调用"}

        cooled_for_armor = dict(cooled_other_t1)
        cooled_for_armor["minecraft:adventure/trade"] = 9e9
        cooled_for_armor["minecraft:story/mine_diamond"] = 9e9
        cooled_for_armor["minecraft:story/enchant_item"] = 9e9
        cooled_for_armor["minecraft:adventure/summon_iron_golem"] = 9e9

        ts = McToolset(bot, world_memory=wm)
        sup = CurriculumSupervisor(
            bot,
            chat_fn=never_called_chat,
            runs_dir=tmp_path / "runs",
            duration_s=60.0,
            toolset=ts,
            enable_pre_ensure=True,
        )
        sup.cooldowns.update(cooled_for_armor)
        summary = sup.run(max_episodes=1)

        assert summary["episodes"][0]["target"] == "minecraft:story/obtain_armor"
        assert summary["episodes"][0]["actor_requests"] == 0
        assert llm_calls["n"] == 0
        assert "minecraft:story/obtain_armor" in summary["episodes"][0]["gained_advancements"]
