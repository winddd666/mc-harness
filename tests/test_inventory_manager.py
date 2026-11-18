"""Offline tests for AltoClef inventory manager: protection stack, 4-tier drops, equip, stash."""

from __future__ import annotations

import math
from typing import Any

import pytest

from mc_agent.errors import ApiError
from mc_agent.harness.world_memory import WorldMemory
from mc_agent.skills.inventory_manager import (
    BUILDING_BLOCK_PRIORITY,
    IMPORTANT_ITEMS,
    InventoryManager,
    ProtectionStack,
    is_important_item,
    is_safe_fuel,
    plan_garbage_drops,
)


def _row(name: str, count: int, slot: int, **extra: Any) -> dict[str, Any]:
    row = {"name": name, "count": count, "slot": slot}
    row.update(extra)
    return row


class InvBot:
    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        armor: dict[str, Any] | None = None,
        offhand: dict[str, Any] | None = None,
        held: dict[str, Any] | None = None,
        empty_slots: int = 30,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        dimension: str = "overworld",
    ) -> None:
        self.items = [dict(r) for r in (items or [])]
        self.armor = armor or {"head": None, "chest": None, "legs": None, "feet": None}
        self.offhand = offhand
        self.held = held
        self.empty_slots = empty_slots
        self.pos = list(pos)
        self.dimension = dimension
        self.yaw = 0.4
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.equip_reject: dict[str, str] = {}
        self.find_blocks_reply: dict[str, Any] = {"blocks": []}

    def status(self) -> dict[str, Any]:
        return {
            "pos": list(self.pos),
            "yaw": self.yaw,
            "dimension": self.dimension,
            "on_ground": True,
            "empty_slots": self.empty_slots,
        }

    def inventory(self) -> dict[str, Any]:
        return {
            "items": [dict(r) for r in self.items],
            "armor": dict(self.armor),
            "offhand": self.offhand,
            "held": self.held,
            "empty_slots": self.empty_slots,
        }

    def _drop(self, name: str, count: int) -> None:
        remaining = count
        kept: list[dict[str, Any]] = []
        for row in self.items:
            if row["name"] == name and remaining > 0:
                take = min(int(row["count"]), remaining)
                row["count"] -= take
                remaining -= take
                if row["count"] <= 0:
                    self.empty_slots += 1
                    continue
            kept.append(row)
        self.items = kept

    def call(self, path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        body = body or {}
        self.calls.append((path, dict(body)))
        if path == "drop":
            self._drop(str(body.get("item")), int(body.get("count") or 1))
            return {"inventory": self.inventory()}
        if path == "equip":
            slot = str(body.get("slot"))
            if slot in self.equip_reject:
                raise ApiError(self.equip_reject[slot], f"bad slot {slot}")
            name = str(body.get("item"))
            if slot in {"head", "chest", "torso", "legs", "feet"}:
                norm = {"torso": "chest", "chestplate": "chest"}.get(slot, slot)
                self.armor[norm] = {"name": name, "count": 1}
            return {"equipped": {"name": name, "slot": slot}}
        if path == "look":
            return {"yaw": body.get("yaw")}
        if path == "find_blocks":
            return self.find_blocks_reply
        if path == "window/act":
            return {"done": [{"op": "put"}], "window": {"window": "generic_9x3", "sections": {"container": []}}}
        if path == "craft":
            self.items.append(_row(str(body.get("item")), int(body.get("count") or 1), 20))
            return {"crafted": body.get("item")}
        if path == "place":
            return {"placed": body.get("item"), "pos": [2, 64, 0]}
        return {}


# ---------------------------------------------------------------------------
# Static tables
# ---------------------------------------------------------------------------


def test_important_items_cover_advancement_families() -> None:
    for name in (
        "diamond",
        "netherite_ingot",
        "netherite_upgrade_smithing_template",
        "music_disc_cat",
        "oak_bed",
        "shulker_box",
        "red_shulker_box",
        "powder_snow_bucket",
        "enchanted_book",
        "lodestone",
        "heavy_core",
    ):
        assert is_important_item(name), name
    for name in ("cobblestone", "dirt", "sweet_berries", "oak_planks"):
        assert not is_important_item(name), name
    assert len(IMPORTANT_ITEMS) >= 90
    assert "cobblestone" in BUILDING_BLOCK_PRIORITY


def test_safe_fuel_whitelist_blocks_beds_tools_and_blaze_rod() -> None:
    assert is_safe_fuel("coal")
    assert is_safe_fuel("oak_planks")
    assert is_safe_fuel("oak_log")
    assert is_safe_fuel("stick")
    for bad in ("white_bed", "wooden_pickaxe", "crafting_table", "oak_boat", "blaze_rod", "shield", "bow"):
        assert not is_safe_fuel(bad), bad
    assert not is_safe_fuel("oak_planks", {"oak_planks"})


def test_protection_stack_nested_frames() -> None:
    stack = ProtectionStack(["cobblestone"])
    assert stack.active() == frozenset({"cobblestone"})
    with stack.frame(["diamond", "iron_ingot"]):
        assert {"cobblestone", "diamond", "iron_ingot"} <= stack.active()
        with stack.frame("gold_ingot"):
            assert "gold_ingot" in stack.active()
    assert "gold_ingot" not in stack.active()
    assert "diamond" not in stack.active()
    assert "cobblestone" in stack.active()


# ---------------------------------------------------------------------------
# plan_garbage_drops
# ---------------------------------------------------------------------------


def test_protected_cobblestone_is_never_dropped_when_crafting_lodestone() -> None:
    inv = {
        "items": [_row("cobblestone", 300, 0), _row("stone", 64, 1)],
        "empty_slots": 34,
    }
    plan = plan_garbage_drops(inv, min_free=6, protected={"cobblestone"})
    assert all(d["item"] != "cobblestone" for d in plan["drops"])


def test_tier2_keeps_96_reserved_and_caps_cobblestone_at_192() -> None:
    inv = {
        "items": [
            _row("cobblestone", 64, 0),
            _row("cobblestone", 64, 1),
            _row("cobblestone", 64, 2),
            _row("cobblestone", 64, 3),
            _row("cobblestone", 44, 4),
        ],
        "empty_slots": 31,
    }
    plan = plan_garbage_drops(inv, min_free=33)
    dropped_cobble = sum(d["count"] for d in plan["drops"] if d["item"] == "cobblestone")
    assert dropped_cobble == 108
    assert 300 - dropped_cobble == 192
    assert {d["tier"] for d in plan["drops"]} == {2}


def test_tier3_drops_wooden_and_extra_stone_pickaxes() -> None:
    inv = {
        "items": [
            _row("wooden_pickaxe", 1, 0, durability=[10, 59]),
            _row("iron_pickaxe", 1, 1, durability=[200, 250]),
            _row("stone_pickaxe", 1, 2, durability=[80, 131]),
            _row("stone_pickaxe", 1, 3, durability=[60, 131]),
            _row("stone_pickaxe", 1, 4, durability=[40, 131]),
        ],
        "empty_slots": 31,
    }
    plan = plan_garbage_drops(inv, min_free=33)
    names = [d["item"] for d in plan["drops"]]
    assert "wooden_pickaxe" in names
    assert names.count("stone_pickaxe") == 1
    assert "iron_pickaxe" not in names


def test_tier3_deduplicates_unenchanted_swords() -> None:
    inv = {
        "items": [
            _row("iron_sword", 1, 0, durability=[200, 250]),
            _row("stone_sword", 1, 1, durability=[100, 131]),
            _row("wooden_sword", 1, 2, durability=[10, 59]),
        ],
        "empty_slots": 33,
    }
    plan = plan_garbage_drops(inv, min_free=35)
    names = [d["item"] for d in plan["drops"]]
    assert names == ["stone_sword", "wooden_sword"]


def test_tier4_emergency_evicts_smallest_stacks() -> None:
    items = [_row("stick", 1, 0 + i) for i in range(36)]
    inv = {"items": items, "empty_slots": 0}
    plan = plan_garbage_drops(inv, min_free=4)
    assert plan["freed_slots"] == 4
    assert plan["reached_target"] is True
    assert {d["tier"] for d in plan["drops"]} == {4}


def test_tier4_keeps_food_when_hunger_score_below_16() -> None:
    items = [_row("bread", 1, 0)] + [_row("stick", 1, 1 + i) for i in range(35)]
    inv = {"items": items, "empty_slots": 0}
    plan = plan_garbage_drops(inv, min_free=4)
    assert plan["food_score"] < 16
    assert all(d["item"] != "bread" for d in plan["drops"])
    assert plan["freed_slots"] == 4


def test_enchanted_and_named_items_are_never_garbage() -> None:
    inv = {
        "items": [
            _row("stone_pickaxe", 1, 0, enchantments=[{"name": "efficiency", "level": 3}]),
            _row("diamond_sword", 1, 1, custom_name="Blade"),
            _row("stick", 1, 2),
        ],
        "empty_slots": 33,
    }
    plan = plan_garbage_drops(inv, min_free=35)
    names = [d["item"] for d in plan["drops"]]
    assert "stick" in names
    assert "stone_pickaxe" not in names
    assert "diamond_sword" not in names


# ---------------------------------------------------------------------------
# InventoryManager actions
# ---------------------------------------------------------------------------


def test_free_slots_turns_away_before_first_drop() -> None:
    bot = InvBot(
        [_row("oak_leaves", 1, 0 + i) for i in range(30)],
        empty_slots=6,
    )
    mgr = InventoryManager(bot)
    res = mgr.free_slots(min_free=8)
    assert res["ok"] is True
    assert len(res["dropped"]) == 2
    paths = [p for p, _ in bot.calls]
    assert paths.index("look") < paths.index("drop")
    look_body = next(b for p, b in bot.calls if p == "look")
    assert abs(float(look_body["yaw"]) - (bot.yaw + math.pi)) < 1e-6
    assert res["auto_dropped_names"] == ["oak_leaves"]


def test_free_slots_respects_protection_context() -> None:
    bot = InvBot([_row("stick", 1, i) for i in range(36)], empty_slots=0)
    mgr = InventoryManager(bot)
    with mgr.protect(["stick"]):
        res = mgr.free_slots(min_free=4)
    assert res["ok"] is False
    assert res["dropped"] == []


def test_auto_equip_uses_working_slot_alias_and_offhand_shield() -> None:
    bot = InvBot(
        [_row("iron_chestplate", 1, 10), _row("diamond_helmet", 1, 11), _row("shield", 1, 12)],
        armor={"head": None, "chest": {"name": "leather_chestplate", "count": 1}, "legs": None, "feet": None},
        empty_slots=33,
    )
    bot.equip_reject["chest"] = "bad_args"
    mgr = InventoryManager(bot)
    res = mgr.auto_equip_best()
    alias_by_part = {(e["part"], e["alias"]) for e in res["equipped"]}
    assert ("chest", "torso") in alias_by_part
    assert ("head", "head") in alias_by_part
    assert any(e["item"] == "shield" for e in res["equipped"])
    assert bot.armor["chest"]["name"] == "iron_chestplate"


def test_auto_equip_nether_keeps_one_golden_piece() -> None:
    bot = InvBot(
        [_row("golden_boots", 1, 10)],
        armor={
            "head": {"name": "iron_helmet", "count": 1},
            "chest": {"name": "iron_chestplate", "count": 1},
            "legs": {"name": "iron_leggings", "count": 1},
            "feet": None,
        },
        empty_slots=35,
    )
    mgr = InventoryManager(bot)
    res = mgr.auto_equip_best(dimension="the_nether")
    assert any(e["item"] == "golden_boots" for e in res["equipped"])
    assert bot.armor["feet"]["name"] == "golden_boots"


def test_dual_view_counts_include_containers_and_in_flight_smelts() -> None:
    wm = WorldMemory()
    wm.update_container([5, 64, 0], {"stone": 10}, block_name="chest")
    job = wm.register_conversion_job(
        pos=[1, 64, 1], input_item="cobblestone", output_item="stone", count=6
    )
    bot = InvBot([_row("diamond", 3, 0)], empty_slots=35)
    mgr = InventoryManager(bot, world_memory=wm)
    inv_counts = mgr.get_inventory_only_counts()
    assert inv_counts["diamond"] == 3 and "stone" not in inv_counts
    accessible = mgr.get_accessible_counts(origin=[0, 64, 0])
    assert accessible["stone"] == 16
    wm.complete_conversion_job(job["id"], collected=6)
    assert mgr.get_accessible_counts(origin=[0, 64, 0])["stone"] == 10


def test_stash_skips_dungeon_chest_with_spawner() -> None:
    bot = InvBot([_row("diamond", 5, 0)], empty_slots=10)
    bot.find_blocks_reply = {"blocks": [{"name": "chest", "pos": [3, 64, 0], "dist": 3.0}]}
    calls: list[tuple[str, dict[str, Any]]] = []

    def _call(path: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        calls.append((path, dict(body or {})))
        if path == "find_blocks":
            body = body or {}
            if "spawner" in (body.get("types") or []):
                return {"blocks": [{"name": "spawner", "pos": [4, 65, 1], "dist": 4.0}]}
            return {"blocks": [{"name": "chest", "pos": [3, 64, 0], "dist": 3.0}]}
        return {}

    bot.call = _call  # type: ignore[assignment]
    mgr = InventoryManager(bot, world_memory=WorldMemory())
    res = mgr.stash_surplus_nearby(craft_chest_if_full=False)
    assert res["ok"] is False
    assert res["skipped_dungeon"] == [[3, 64, 0]]


def test_stash_deposits_surplus_into_safe_chest() -> None:
    bot = InvBot([_row("diamond", 5, 0), _row("emerald", 2, 1)], empty_slots=10)

    class _Skills:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def container_deposit(self, items: Any, **kw: Any) -> dict[str, Any]:
            self.calls.append({"items": items, **kw})
            return {"ok": True, "done": [{"op": "put"}]}

    skills = _Skills()
    wm = WorldMemory()
    wm.update_container([2, 64, 0], {}, block_name="chest")
    mgr = InventoryManager(bot, world_memory=wm, skills=skills)
    res = mgr.stash_surplus_nearby()
    assert res["ok"] is True
    assert skills.calls and skills.calls[0]["items"]["diamond"] == 5
    assert res["target"] == [2, 64, 0]


def test_free_slots_walks_away_after_dropping_to_avoid_resuck() -> None:
    bot = InvBot([_row("oak_leaves", 1, i) for i in range(36)], empty_slots=0)
    mgr = InventoryManager(bot)
    res = mgr.free_slots(min_free=4)
    controls = [b for p, b in bot.calls if p == "control"]
    assert controls and all(b.get("forward") for b in controls)
    assert res["ok"] is True


def test_stash_creates_new_chest_when_the_only_chest_is_full() -> None:
    bot = InvBot([_row("diamond", 4, 0), _row("oak_planks", 8, 1)], empty_slots=10)

    class _Skills:
        def __init__(self) -> None:
            self.calls = 0

        def container_deposit(self, items: Any, **kw: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "error": "section_full", "message": "container is occupied"}
            return {"ok": True, "done": [{"op": "put"}]}

    skills = _Skills()
    wm = WorldMemory()
    wm.update_container([2, 64, 0], {}, block_name="chest")
    mgr = InventoryManager(bot, world_memory=wm, skills=skills)
    res = mgr.stash_surplus_nearby()
    assert res["ok"] is True
    assert skills.calls == 2
    assert res["new_chest"] == [2, 64, 0]  # bot.place returns this position in the fake


def test_tier4_never_evicts_the_best_tools_or_spare_armor() -> None:
    items = [
        _row("wooden_pickaxe", 1, 0, durability=[59, 59]),
        _row("iron_pickaxe", 1, 1, durability=[250, 250]),
        _row("stone_pickaxe", 1, 2, durability=[131, 131]),
        _row("stone_pickaxe", 1, 3, durability=[131, 131]),
        _row("iron_chestplate", 1, 4),
    ] + [_row("stick", 1, 5 + i) for i in range(31)]
    inv = {"items": items, "empty_slots": 0}
    plan = plan_garbage_drops(inv, min_free=35)
    names = [d["item"] for d in plan["drops"]]
    assert "iron_pickaxe" not in names
    assert names.count("stone_pickaxe") <= 1
    assert "iron_chestplate" not in names
    assert "wooden_pickaxe" in names
