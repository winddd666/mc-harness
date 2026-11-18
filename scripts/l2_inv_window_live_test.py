#!/usr/bin/env python3
"""`nju-beast` live acceptance for the AltoClef inventory brain + container self-heal + multi-furnace split."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import l2_pack_live_test as common  # noqa: E402
from mc_agent.errors import ApiError  # noqa: E402
from mc_agent.skills.inventory_manager import InventoryManager  # noqa: E402

BOT = common.BOT
Y = common.ARENA["y"]
# Inventory / window / furnace lab: south-east corner of the arena.
LAB_X, LAB_Z = common.ARENA["x0"] + 60, common.ARENA["z0"] + 100


def _tp(x: float, y: float, z: float) -> None:
    common.tp(x, y, z)


def _status(bot: Any) -> dict[str, Any]:
    return bot.status()


def _counts(bot: Any) -> dict[str, int]:
    inv = bot.inventory()
    out: dict[str, int] = {}
    for row in inv.get("items") or []:
        if isinstance(row, dict) and row.get("name"):
            name = str(row["name"]).removeprefix("minecraft:")
            out[name] = out.get(name, 0) + int(row.get("count") or 0)
    return out


def _build_lab() -> None:
    x, z = LAB_X, LAB_Z
    cmds = [
        f"fill {x - 6} {Y - 1} {z - 6} {x + 20} {Y - 1} {z + 20} minecraft:stone",
        f"fill {x - 6} {Y} {z - 6} {x + 20} {Y + 6} {z + 20} minecraft:air",
        "time set day",
    ]
    common.rcon_batch(cmds)


def _place_block(x: int, y: int, z: int, block: str) -> None:
    common.rcon_batch([f"setblock {x} {y} {z} minecraft:{block} replace"])


# ---------------------------------------------------------------------------
# INV1: 36/36 full bag -> real /drop with back-turn, no re-suck
# ---------------------------------------------------------------------------


def group_inv1(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _tp(x + 0.5, Y, z + 0.5)
    # 36 distinct-ish stacks of junk + 3 valuable stacks that must survive
    junk = [
        "oak_leaves", "spruce_leaves", "birch_leaves", "short_grass", "fern", "dead_bush",
        "poisonous_potato", "wheat_seeds", "beetroot_seeds", "kelp", "seagrass", "cobweb",
        "string", "flint", "gunpowder", "brown_mushroom", "red_mushroom", "sugar_cane",
        "dirt", "gravel", "sand", "tuff", "calcite", "diorite", "andesite", "granite",
        "netherrack", "basalt", "blackstone", "end_stone", "stick", "bowl", "torch",
    ]
    give = {name: 1 for name in junk[:33]}
    give.update({"diamond": 5, "emerald": 3, "iron_pickaxe": 1})
    common.prepare_player(give, effects=True)
    time.sleep(0.8)
    inv0 = s.bot.inventory()
    mgr = InventoryManager(s.bot, world_memory=s.world_memory, skills=s.skills)
    res = mgr.free_slots(min_free=4)
    time.sleep(2.5)  # give auto-pickup a chance to re-suck the drops
    counts_after = _counts(s.bot)
    inv_after = s.bot.inventory()
    kept_diamond = counts_after.get("diamond", 0)
    kept_emerald = counts_after.get("emerald", 0)
    kept_pick = counts_after.get("iron_pickaxe", 0)
    resucked = [d["item"] for d in res.get("dropped", []) if counts_after.get(d["item"], 0) > 0]
    return {
        "checks": {
            "INV1_empty_before": inv0.get("empty_slots"),
            "INV1_dropped": res.get("dropped"),
            "INV1_empty_after": inv_after.get("empty_slots"),
            "INV1_turned_away": res.get("turned_away"),
            "INV1_resucked": resucked,
            "INV1_kept_diamond": kept_diamond,
            "INV1_kept_emerald": kept_emerald,
            "INV1_kept_pickaxe": kept_pick,
        },
        "ok": bool(res.get("ok"))
        and int(inv_after.get("empty_slots") or 0) >= 4
        and res.get("turned_away") is True
        and not resucked
        and kept_diamond == 5
        and kept_emerald == 3
        and kept_pick == 1,
    }


# ---------------------------------------------------------------------------
# INV2: real /equip slot aliases + shield offhand + nether gold rule
# ---------------------------------------------------------------------------


def group_inv2(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _tp(x + 0.5, Y, z + 0.5)
    common.prepare_player(
        {
            "leather_helmet": 1,
            "leather_chestplate": 1,
            "iron_chestplate": 1,
            "iron_leggings": 1,
            "diamond_boots": 1,
            "shield": 1,
            "cooked_beef": 4,
        }
    )
    # wear the leather bits first via the real API
    time.sleep(0.8)
    alias_results: dict[str, Any] = {}
    for item, alias in (
        ("leather_helmet", "head"),
        ("leather_chestplate", "chest"),
        ("iron_leggings", "legs"),
    ):
        try:
            s.bot.call("equip", {"item": item, "slot": alias})
            alias_results[alias] = "ok"
        except ApiError as err:
            alias_results[alias] = f"{err.code}:{err.message[:80]}"
    for item, alias in (("iron_chestplate", "torso"), ("diamond_boots", "feet")):
        try:
            s.bot.call("equip", {"item": item, "slot": alias})
            alias_results[alias] = "ok"
        except ApiError as err:
            alias_results[alias] = f"{err.code}:{err.message[:80]}"
    time.sleep(0.5)
    mgr = InventoryManager(s.bot)
    res = mgr.auto_equip_best()
    st = _status(s.bot)
    inv = s.bot.inventory()
    armor = inv.get("armor") or {}
    return {
        "checks": {
            "INV2_alias_probe": alias_results,
            "INV2_equipped": res.get("equipped"),
            "INV2_failed": res.get("failed"),
            "INV2_armor": {k: (v or {}).get("name") if isinstance(v, dict) else v for k, v in armor.items()},
            "INV2_offhand": (inv.get("offhand") or {}).get("name") if isinstance(inv.get("offhand"), dict) else inv.get("offhand"),
            "INV2_armor_points": st.get("armor_points"),
        },
        "ok": (armor.get("chest") or {}).get("name") == "iron_chestplate"
        and (armor.get("feet") or {}).get("name") == "diamond_boots"
        and isinstance(inv.get("offhand"), dict)
        and inv["offhand"].get("name") == "shield"
        and float(st.get("armor_points") or 0) >= 5,
    }


# ---------------------------------------------------------------------------
# INV3: dual-view counts (inventory-only vs accessible with chest + in-flight smelt)
# ---------------------------------------------------------------------------


def group_inv3(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    s.world_memory.containers.clear()
    chest = [x + 4, Y, z]
    _place_block(*chest, "chest")
    _tp(x + 3.0, Y, z + 0.5)
    common.prepare_player({"stone": 3, "iron_ingot": 7, "coal": 5})
    time.sleep(0.5)
    put = s.skills.container_deposit({"iron_ingot": 7, "coal": 5}, x=chest[0], y=chest[1], z=chest[2])
    job = s.world_memory.register_conversion_job(
        pos=[x + 8, Y, z],
        input_item="cobblestone",
        output_item="stone",
        count=6,
        ready_at=time.time() + 120,
    )
    mgr = InventoryManager(s.bot, world_memory=s.world_memory)
    only = mgr.get_inventory_only_counts()
    accessible = mgr.get_accessible_counts(origin=[x, Y, z])
    s.world_memory.complete_conversion_job(job["id"], collected=6)
    return {
        "checks": {
            "INV3_deposit_ok": put.get("ok"),
            "INV3_deposit_error": {k: put.get(k) for k in ("error", "message", "context_keys")},
            "INV3_only": {k: only.get(k) for k in ("stone", "iron_ingot", "coal")},
            "INV3_accessible": {k: accessible.get(k) for k in ("stone", "iron_ingot", "coal")},
            "INV3_chest_memory": s.world_memory.containers.get(("overworld", chest[0], chest[1], chest[2]), {}).get("items"),
        },
        "ok": put.get("ok")
        and only.get("iron_ingot", 0) == 0
        and accessible.get("iron_ingot", 0) == 7
        and accessible.get("coal", 0) == 5
        and accessible.get("stone", 0) - only.get("stone", 0) == 6,
    }


# ---------------------------------------------------------------------------
# INV4: stash surplus into a safe chest, then craft a new chest when full
# ---------------------------------------------------------------------------


def group_inv4(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    chest = [x + 2, Y, z + 6]
    _place_block(*chest, "chest")
    _place_block(x + 4, Y, z + 6, "crafting_table")
    # pre-fill the chest to capacity (27 slots, 27 distinct items)
    fill = {f"minecraft:{n}": 1 for n in (
        "oak_planks", "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks",
        "dark_oak_planks", "mangrove_planks", "cherry_planks", "pale_oak_planks", "stick",
        "cobblestone", "stone", "andesite", "diorite", "granite", "tuff", "calcite",
        "dirt", "gravel", "sand", "netherrack", "basalt", "blackstone", "end_stone",
        "torch", "bowl", "flint",
    )}
    fill_cmds = [
        f"item replace block {chest[0]} {chest[1]} {chest[2]} container.{i} with {name} 1"
        for i, name in enumerate(fill)
    ]
    common.rcon_batch(fill_cmds)
    _tp(x + 4.5, Y, z + 6.5)
    time.sleep(0.6)
    filler = {name: 1 for name in (
        "oak_leaves", "short_grass", "fern", "dead_bush", "poisonous_potato", "wheat_seeds",
        "kelp", "seagrass", "cobweb", "string", "flint", "gunpowder", "brown_mushroom",
        "sugar_cane", "tuff", "calcite", "diorite", "andesite", "granite", "netherrack",
        "basalt", "blackstone", "end_stone", "stick", "bowl", "torch", "gravel", "sand",
    )}
    filler.update({"diamond": 6, "emerald": 4, "oak_planks": 24})
    common.prepare_player(filler)
    time.sleep(0.6)
    mgr = InventoryManager(s.bot, world_memory=s.world_memory, skills=s.skills)
    res = mgr.stash_surplus_nearby(radius=16.0)
    time.sleep(0.6)
    counts = _counts(s.bot)
    new_chest = res.get("new_chest")
    new_chest_items = None
    if new_chest:
        pos = new_chest
        new_chest_items = s.world_memory.containers.get(("overworld", pos[0], pos[1], pos[2]), {}).get("items")
    return {
        "checks": {
            "INV4_result": {k: res.get(k) for k in ("ok", "error", "target", "new_chest", "skipped_dungeon")},
            "INV4_raw": res.get("result"),
            "INV4_deposited": res.get("deposited"),
            "INV4_remaining_diamond": counts.get("diamond"),
            "INV4_remaining_emerald": counts.get("emerald"),
            "INV4_new_chest_items": new_chest_items,
        },
        "ok": bool(res.get("ok")) and counts.get("diamond", 0) == 0 and counts.get("emerald", 0) == 0,
    }


# ---------------------------------------------------------------------------
# INV5: dungeon chest (spawner within 6 blocks) is skipped
# ---------------------------------------------------------------------------


def group_inv5(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    # drop any previously cached safe chest so the dungeon chest is the only candidate
    s.world_memory.containers.clear()
    chest = [x + 8, Y, z + 6]
    _place_block(*chest, "chest")
    _place_block(x + 9, Y, z + 7, "spawner")
    _tp(x + 8.5, Y, z + 8.5)
    filler = {name: 1 for name in (
        "oak_leaves", "short_grass", "fern", "dead_bush", "poisonous_potato", "wheat_seeds",
        "kelp", "seagrass", "cobweb", "string", "flint", "gunpowder", "brown_mushroom",
        "sugar_cane", "tuff", "calcite", "diorite", "andesite", "granite", "netherrack",
        "basalt", "blackstone", "end_stone", "stick", "bowl", "torch", "gravel", "sand",
    )}
    filler["diamond"] = 3
    common.prepare_player(filler)
    time.sleep(0.6)
    mgr = InventoryManager(s.bot, world_memory=s.world_memory, skills=s.skills)
    res = mgr.stash_surplus_nearby(radius=10.0, craft_chest_if_full=False)
    return {
        "checks": {
            "INV5_skipped_dungeon": res.get("skipped_dungeon"),
            "INV5_ok": res.get("ok"),
            "INV5_error": res.get("error"),
        },
        "ok": res.get("ok") is False and [x + 8, Y, z + 6] in (res.get("skipped_dungeon") or []),
    }


# ---------------------------------------------------------------------------
# WIN1: empty chest -> missing_item self-heal + dig invalidation
# ---------------------------------------------------------------------------


def group_win1(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    chest = [x + 12, Y, z + 6]
    _place_block(*chest, "chest")
    _tp(x + 12.5, Y, z + 7.5)
    common.prepare_player({"redstone": 1})
    time.sleep(0.6)
    # 1 redstone in, then take it out
    put = s.skills.container_deposit({"redstone": 1}, x=chest[0], y=chest[1], z=chest[2])
    take1 = s.skills.container_withdraw({"redstone": 1}, x=chest[0], y=chest[1], z=chest[2])
    mem_after_take = s.world_memory.containers.get(("overworld", chest[0], chest[1], chest[2]), {}).get("items")
    # now the cache should be {} and a second take must be a real missing_item
    take2 = s.skills.container_withdraw({"redstone": 1}, x=chest[0], y=chest[1], z=chest[2])
    mem_after_err = s.world_memory.containers.get(("overworld", chest[0], chest[1], chest[2]), {}).get("items")
    error_ctx_keys = take2.get("context_keys")
    # dig the chest -> both tables must drop it
    s.world_memory.record_poi("chest", chest)
    dig = s.toolset.mc_dig(chest[0], chest[1], chest[2])
    time.sleep(0.4)
    in_containers = ("overworld", chest[0], chest[1], chest[2]) in s.world_memory.containers
    in_pois = ("overworld", chest[0], chest[1], chest[2]) in s.world_memory.pois
    return {
        "checks": {
            "WIN1_put_ok": put.get("ok"),
            "WIN1_take_ok": take1.get("ok"),
            "WIN1_mem_after_take": mem_after_take,
            "WIN1_take2_error": take2.get("error"),
            "WIN1_take2_message": take2.get("message"),
            "WIN1_error_ctx_keys": error_ctx_keys,
            "WIN1_mem_after_err": mem_after_err,
            "WIN1_dig": json.loads(dig) if isinstance(dig, str) else dig,
            "WIN1_in_containers": in_containers,
            "WIN1_in_pois": in_pois,
        },
        "ok": put.get("ok")
        and take1.get("ok")
        and take2.get("error") == "missing_item"
        and mem_after_err == {}
        and bool(error_ctx_keys)
        and not in_containers
        and not in_pois,
    }


# ---------------------------------------------------------------------------
# WIN2: probe_unknown_nearby_containers (natural chest cached, dungeon skipped)
# ---------------------------------------------------------------------------


def group_win2(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    natural = [x + 12, Y, z + 2]
    dungeon = [x + 18, Y, z + 10]
    _place_block(*natural, "barrel")
    _place_block(*dungeon, "chest")
    _place_block(x + 19, Y, z + 11, "spawner")
    _tp(x + 12.5, Y, z + 3.5)
    common.prepare_player({"coal": 3})
    time.sleep(0.6)
    # seed one item so the probe returns a non-empty snapshot
    s.skills.container_deposit({"coal": 3}, x=natural[0], y=natural[1], z=natural[2])
    s.world_memory.containers.pop(("overworld", natural[0], natural[1], natural[2]), None)
    res = s.skills.probe_unknown_nearby_containers(radius=12.0, max_probes=3)
    cached_natural = s.world_memory.containers.get(("overworld", natural[0], natural[1], natural[2]), {}).get("items")
    return {
        "checks": {
            "WIN2_probed": res.get("probed"),
            "WIN2_skipped": res.get("skipped"),
            "WIN2_cached_natural": cached_natural,
        },
        "ok": cached_natural == {"coal": 3}
        and any(sk.get("reason") == "dungeon_spawner" for sk in (res.get("skipped") or [])),
    }


# ---------------------------------------------------------------------------
# SM1: smelt_start(count=12) -> 2/3 furnaces, non-blocking, ledger 12
# ---------------------------------------------------------------------------


def group_sm1(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    # three furnaces in a row, 2 blocks from the bot
    for i, dx in enumerate((2, 3, 4)):
        _place_block(x + dx, Y, z + 12, "furnace")
    _tp(x + 3.5, Y, z + 14.5)
    common.prepare_player({"cobblestone": 24, "coal": 6})
    time.sleep(0.8)
    t0 = time.monotonic()
    ledger_before = s.world_memory.get_in_conversion_counts()
    res = s.skills.smelt_start("cobblestone", count=12)
    elapsed = time.monotonic() - t0
    ledger = s.world_memory.get_in_conversion_counts()
    ledger_delta = {k: v - ledger_before.get(k, 0) for k, v in ledger.items() if v - ledger_before.get(k, 0) != 0}
    return {
        "checks": {
            "SM1_start": {k: res.get(k) for k in ("ok", "error", "furnace_count", "output_item", "estimated_wait_s", "in_conversion_counts", "non_blocking", "failures")},
            "SM1_ledger_before": ledger_before,
            "SM1_ledger_delta": ledger_delta,
            "SM1_furnaces": res.get("furnaces"),
            "SM1_elapsed_s": round(elapsed, 3),
            "SM1_ledger": ledger,
        },
        "ok": bool(res.get("ok"))
        and elapsed < 6.0
        and res.get("furnace_count") == 2
        and ledger_delta == {"stone": 12},
    }


def group_sm2(s: common.Session) -> dict[str, Any]:
    """Wait for the smelt to finish and collect everything (ledger must settle to {})."""
    deadline = time.monotonic() + 110
    calls: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        res = s.skills.smelt_collect(wait_remaining_s=12.0)
        calls.append({"took": [c.get("took") for c in res.get("collected") or []], "ledger": res.get("in_conversion_counts")})
        if res.get("in_conversion_counts") == {}:
            break
        time.sleep(5)
    counts = _counts(s.bot)
    return {
        "checks": {
            "SM2_calls": calls,
            "SM2_stone_in_bag": counts.get("stone"),
            "SM2_ledger": s.world_memory.get_in_conversion_counts(),
        },
        "ok": counts.get("stone", 0) >= 12 and s.world_memory.get_in_conversion_counts() == {},
    }


def group_sm3(s: common.Session) -> dict[str, Any]:
    """Unsafe fuel must be rejected; 20 items must fan out to 3 furnaces."""
    x, z = LAB_X, LAB_Z
    _build_lab()
    for dx in (2, 3, 4):
        _place_block(x + dx, Y, z + 12, "furnace")
    _tp(x + 3.5, Y, z + 14.5)
    common.prepare_player({"cobblestone": 30, "white_bed": 2, "oak_planks": 40})
    time.sleep(0.8)
    bad = s.skills.smelt_start("cobblestone", count=12, fuel_item="white_bed")
    good = s.skills.smelt_start("cobblestone", count=20)
    ledger = s.world_memory.get_in_conversion_counts()
    # clean up the in-flight jobs so the report is not polluted
    for job in s.world_memory.get_conversion_jobs():
        s.world_memory.complete_conversion_job(job["id"], collected=job.get("remaining") or 0)
    return {
        "checks": {
            "SM3_bad_fuel": {k: bad.get(k) for k in ("ok", "error")},
            "SM3_good": {k: good.get(k) for k in ("ok", "error", "furnace_count", "in_conversion_counts")},
            "SM3_ledger": ledger,
        },
        "ok": bad.get("ok") is False
        and bad.get("error") == "missing_fuel"
        and bool(good.get("ok"))
        and good.get("furnace_count") == 3,
    }


GROUPS = {
    "INV1": group_inv1,
    "INV2": group_inv2,
    "INV3": group_inv3,
    "INV4": group_inv4,
    "INV5": group_inv5,
    "WIN1": group_win1,
    "WIN2": group_win2,
    "SM1": group_sm1,
    "SM2": group_sm2,
    "SM3": group_sm3,
}



# ---------------------------------------------------------------------------
# INV6: 36/36 bread-only bag must NOT trash emergency food
# ---------------------------------------------------------------------------


def group_inv6(s: common.Session) -> dict[str, Any]:
    """35 junk stacks + 1 bread: hunger < 16, so tier-4 must never evict the food."""
    x, z = LAB_X, LAB_Z
    _build_lab()
    _tp(x + 0.5, Y, z + 0.5)
    junk = [
        "oak_leaves", "spruce_leaves", "birch_leaves", "short_grass", "fern", "dead_bush",
        "poisonous_potato", "wheat_seeds", "beetroot_seeds", "kelp", "seagrass", "cobweb",
        "string", "flint", "gunpowder", "brown_mushroom", "red_mushroom", "sugar_cane",
        "dirt", "gravel", "sand", "tuff", "calcite", "diorite", "andesite", "granite",
        "netherrack", "basalt", "blackstone", "end_stone", "stick", "bowl", "torch",
        "crafting_table", "furnace",
    ]
    give = {name: 1 for name in junk}
    give["bread"] = 1
    common.prepare_player(give)
    time.sleep(0.8)
    mgr = InventoryManager(s.bot, world_memory=s.world_memory, skills=s.skills)
    plan = mgr.plan(min_free=4)
    res = mgr.free_slots(min_free=4)
    counts = _counts(s.bot)
    return {
        "checks": {
            "INV6_plan_tiers": plan.get("tiers_used"),
            "INV6_plan_food_score": plan.get("food_score"),
            "INV6_bread_in_plan": any(d["item"] == "bread" for d in plan.get("drops") or []),
            "INV6_bread_left": counts.get("bread"),
            "INV6_dropped_n": len(res.get("dropped") or []),
        },
        "ok": not any(d["item"] == "bread" for d in plan.get("drops") or [])
        and counts.get("bread", 0) == 1
        and len(res.get("dropped") or []) >= 4,
    }


# ---------------------------------------------------------------------------
# INV7: 300 cobblestone -> tier2 keeps 192 and frees slots
# ---------------------------------------------------------------------------


def group_inv7(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    _tp(x + 0.5, Y, z + 0.5)
    common.prepare_player({"cobblestone": 300, "dirt": 64, "tuff": 64})
    time.sleep(0.8)
    mgr = InventoryManager(s.bot, world_memory=s.world_memory, skills=s.skills)
    plan = mgr.plan(min_free=34)
    res = mgr.free_slots(min_free=34)
    counts = _counts(s.bot)
    return {
        "checks": {
            "INV7_plan": {k: plan.get(k) for k in ("freed_slots", "tiers_used", "food_score")},
            "INV7_drops": plan.get("drops"),
            "INV7_cobble_left": counts.get("cobblestone"),
            "INV7_tuff_left": counts.get("tuff"),
            "INV7_free_after": s.bot.inventory().get("empty_slots"),
        },
        "ok": bool(plan.get("drops"))
        and 96 <= int(counts.get("cobblestone") or 0) <= 192
        and int(s.bot.inventory().get("empty_slots") or 0) >= 4,
    }


# ---------------------------------------------------------------------------
# INV8: full chest depature -> section_full error carries a live window snapshot
# ---------------------------------------------------------------------------


def group_inv8(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    chest = [x + 6, Y, z + 6]
    _place_block(*chest, "chest")
    fill = [
        f"item replace block {chest[0]} {chest[1]} {chest[2]} container.{i} with minecraft:stone 64"
        for i in range(27)
    ]
    common.rcon_batch(fill)
    _tp(x + 6.5, Y, z + 8.0)
    common.prepare_player({"diamond": 3})
    time.sleep(0.8)
    res = s.skills.container_deposit({"diamond": 3}, x=chest[0], y=chest[1], z=chest[2])
    mem = s.world_memory.containers.get(("overworld", chest[0], chest[1], chest[2]), {}).get("items")
    return {
        "checks": {
            "INV8_error": {k: res.get(k) for k in ("ok", "error", "message", "context_keys", "memory_synced")},
            "INV8_mem_stone": (mem or {}).get("stone"),
            "INV8_window_sections": len(((res.get("window") or {}).get("sections") or {}).get("container") or []),
        },
        "ok": res.get("ok") is False
        and res.get("error") == "section_full"
        and res.get("memory_synced") is True
        and (mem or {}).get("stone") == 1728,
    }


# ---------------------------------------------------------------------------
# SM4: no furnace and not enough cobble buffer -> graceful need_furnace
# ---------------------------------------------------------------------------


def group_sm4(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    _tp(x + 0.5, Y, z + 0.5)
    common.prepare_player({"cobblestone": 10, "coal": 2})
    time.sleep(0.8)
    res = s.skills.smelt_start("cobblestone", count=8)
    return {
        "checks": {
            "SM4_result": {k: res.get(k) for k in ("ok", "error", "message")},
        },
        "ok": res.get("ok") is False and res.get("error") == "need_furnace",
    }


GROUPS.update({"INV6": group_inv6, "INV7": group_inv7, "INV8": group_inv8, "SM4": group_sm4})



# ---------------------------------------------------------------------------
# INV9: auto_equip upgrades leather -> iron (torso alias) + shield offhand
# ---------------------------------------------------------------------------


def group_inv9(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    _tp(x + 0.5, Y, z + 0.5)
    common.prepare_player(
        {"leather_helmet": 1, "leather_chestplate": 1, "iron_chestplate": 1, "diamond_helmet": 1, "shield": 1}
    )
    time.sleep(0.6)
    for item, alias in (("leather_helmet", "head"), ("leather_chestplate", "torso")):
        s.bot.call("equip", {"item": item, "slot": alias})
    mgr = InventoryManager(s.bot)
    res = mgr.auto_equip_best()
    inv = s.bot.inventory()
    armor = inv.get("armor") or {}
    return {
        "checks": {
            "INV9_equipped": res.get("equipped"),
            "INV9_failed": res.get("failed"),
            "INV9_armor": {k: (v or {}).get("name") if isinstance(v, dict) else v for k, v in armor.items()},
            "INV9_offhand": (inv.get("offhand") or {}).get("name") if isinstance(inv.get("offhand"), dict) else None,
        },
        "ok": (armor.get("chest") or {}).get("name") == "iron_chestplate"
        and (armor.get("head") or {}).get("name") == "diamond_helmet"
        and (inv.get("offhand") or {}).get("name") == "shield"
        and not res.get("failed"),
    }


# ---------------------------------------------------------------------------
# SM5: no furnace nearby -> auto-craft + place 2 furnaces and start smelting
# ---------------------------------------------------------------------------


def group_sm5(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X + 30, LAB_Z + 30
    common.rcon_batch([
        f"fill {x - 6} {Y - 1} {z - 6} {x + 10} {Y - 1} {z + 10} minecraft:stone",
        f"fill {x - 6} {Y} {z - 6} {x + 10} {Y + 5} {z + 10} minecraft:air",
        f"fill {x - 6} {Y - 2} {z - 6} {x + 10} {Y - 2} {z + 10} minecraft:stone",
    ])
    _tp(x + 0.5, Y, z + 0.5)
    common.prepare_player({"cobblestone": 60, "coal": 6, "oak_planks": 8})
    time.sleep(0.8)
    before = s.world_memory.get_in_conversion_counts()
    res = s.skills.smelt_start("cobblestone", count=12)
    placed = [f for f in res.get("furnaces") or [] if f.get("source") == "placed"]
    after = s.world_memory.get_in_conversion_counts()
    ledger = {k: v - before.get(k, 0) for k, v in after.items() if v - before.get(k, 0) != 0}
    # settle the ledger and reclaim the stone
    for job in s.world_memory.get_conversion_jobs():
        s.world_memory.complete_conversion_job(job["id"], collected=job.get("remaining") or 0)
    return {
        "checks": {
            "SM5_start": {k: res.get(k) for k in ("ok", "error", "furnace_count", "message")},
            "SM5_furnaces": res.get("furnaces"),
            "SM5_placed_n": len(placed),
            "SM5_ledger": ledger,
        },
        "ok": bool(res.get("ok")) and len(placed) == 2 and ledger == {"stone": 12},
    }


GROUPS.update({"INV9": group_inv9, "SM5": group_sm5})



# ---------------------------------------------------------------------------
# SM6: collect while the bag is 36/36 full of protected items -> inventory_full,
#       then free one slot and collect successfully
# ---------------------------------------------------------------------------


def group_sm6(s: common.Session) -> dict[str, Any]:
    x, z = LAB_X, LAB_Z
    _build_lab()
    _place_block(x + 2, Y, z + 12, "furnace")
    _tp(x + 2.5, Y, z + 14.0)
    common.prepare_player({"cobblestone": 1, "coal": 1})
    time.sleep(0.8)
    started = s.skills.smelt_start("cobblestone", count=1)
    # 36 stacks of a protection-listed item so the auto-cleaner can never free a slot
    common.rcon_batch([f"give {BOT} minecraft:diamond 64" for _ in range(40)])
    time.sleep(16)
    blocked = s.skills.smelt_collect(wait_remaining_s=20.0)
    inv_full = s.bot.inventory()
    # drop one whole diamond stack by moving it out via /clear + re-give a single one
    common.rcon_batch([f"clear {BOT} minecraft:diamond"])
    time.sleep(1.0)
    # walk two blocks away from the dropped overflow so pickups cannot refill the bag
    try:
        s.bot.call("control", {"forward": True, "ms": 600}, timeout_s=10.0)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)
    retry = s.skills.smelt_collect(wait_remaining_s=25.0)
    counts = _counts(s.bot)
    return {
        "checks": {
            "SM6_start": {k: started.get(k) for k in ("ok", "error", "furnace_count")},
            "SM6_blocked": {k: blocked.get(k) for k in ("ok", "collected", "failures", "in_conversion_counts")},
            "SM6_inv_empty_slots": inv_full.get("empty_slots"),
            "SM6_retry": {k: retry.get(k) for k in ("ok", "collected", "failures")},
            "SM6_stone": counts.get("stone"),
            "SM6_ledger": s.world_memory.get_in_conversion_counts(),
        },
        "ok": bool(started.get("ok"))
        and any(
            "inventory_full" in str(f.get("error"))
            for f in (blocked.get("failures") or [])
        )
        and counts.get("stone", 0) >= 1,
    }


GROUPS.update({"SM6": group_sm6})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    selected = list(GROUPS) if args.only == "all" else [x.strip().upper() for x in args.only.split(",") if x.strip()]
    s = common.Session()
    report: dict[str, Any] = {"bot": BOT, "groups": selected, "checks": {}, "ok": True}
    try:
        report["connect"] = common.status_brief(s.connect())
        report["arena_reset"] = common.arena_reset()[-4:]
        # deterministic ledger for the acceptance run: drop any stale in-flight jobs
        stale = list(getattr(s.world_memory, "conversion_jobs", {}).keys())
        if stale:
            s.world_memory.conversion_jobs.clear()
            s.world_memory.save(force=True)
        report["stale_jobs_pruned"] = stale
        _build_lab()
        s.skills.inventory_manager = None
        for key in selected:
            print(f"=== {key} ===", flush=True)
            try:
                res = GROUPS[key](s)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "exception": f"{type(exc).__name__}: {exc}"}
            report["checks"][key] = res
            if not res.get("ok"):
                report["ok"] = False
            print(json.dumps({"group": key, "ok": res.get("ok"), "checks": res.get("checks")}, ensure_ascii=False)[:1200], flush=True)
        report["final_status"] = common.status_brief(s.bot.status())
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            report["cleanup"] = common.arena_cleanup()[-3:]
        except Exception as exc:  # noqa: BLE001
            report["cleanup_error"] = str(exc)
        s.close()
    out = pathlib.Path(args.report) if args.report else ROOT / "runs" / f"l2-inv-window-live-{time.strftime('%Y%m%d-%H%M%S')}.json"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(out), "groups": {k: v.get("ok") for k, v in report["checks"].items()}}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
