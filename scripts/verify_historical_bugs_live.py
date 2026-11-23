#!/usr/bin/env python3
"""Targeted RCON micro-fixture verifier for historical bugs on live 5ease server.

Each case constructs a deterministic, isolated arena far from wild endurance bots
(around X=80000, Z=80000), sets up exact preconditions via RCON, invokes real
SkillLibrary / McToolset methods against the live Leaves + mc-agent-api stack,
and reports PASS/FAIL with structured diagnostics.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
import time
from typing import Any, Callable

WT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_ROOT = pathlib.Path("/home/xzq/projects/mc")
sys.path.insert(0, str(WT_ROOT / "src"))
sys.path.insert(0, str(WT_ROOT / "scripts"))

from l3_wild_endurance_runner import (  # noqa: E402
    build_token_refresher,
    ensure_bot_ready,
    rcon_batch,
)
from mc_agent.auth import TokenStore  # noqa: E402
from mc_agent.bot import BotSession  # noqa: E402
from mc_agent.config import Settings  # noqa: E402
from mc_agent.harness.mc_tools import McToolset  # noqa: E402
from mc_agent.skills.library import MacroSkills  # noqa: E402


def _as_dict(res: Any) -> dict[str, Any]:
    if isinstance(res, dict):
        return res
    if isinstance(res, str):
        try:
            parsed = json.loads(res)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {"raw": res}
    return {"raw": res}


def _extract_xyz(obj: Any) -> tuple[float, float, float] | None:
    if isinstance(obj, (list, tuple)) and len(obj) >= 3:
        return (float(obj[0]), float(obj[1]), float(obj[2]))
    if isinstance(obj, dict):
        if all(k in obj for k in ("x", "y", "z")):
            return (float(obj["x"]), float(obj["y"]), float(obj["z"]))
        for key in ("pos", "position"):
            sub = obj.get(key)
            got = _extract_xyz(sub)
            if got is not None:
                return got
    return None


def _inv_counts(bot: BotSession) -> dict[str, int]:
    inv = bot.inventory() or {}
    counts: dict[str, int] = {}
    for item in inv.get("items") or []:
        name = str(item.get("name") or "").removeprefix("minecraft:")
        if name:
            counts[name] = counts.get(name, 0) + int(item.get("count") or 0)
    return counts


def _reset_arena(bot_name: str, cx: int, cy: int, cz: int, *, radius: int = 10, height: int = 10) -> None:
    """Build a clean, flat, well-lit isolated overworld stone pad and teleport the bot there."""
    cmds = [
        f"forceload add {cx - 16} {cz - 16} {cx + 16} {cz + 16}",
        f"execute in minecraft:overworld run fill {cx - radius} {cy - 2} {cz - radius} {cx + radius} {cy - 1} {cz + radius} minecraft:stone",
        f"execute in minecraft:overworld run fill {cx - radius} {cy} {cz - radius} {cx + radius} {cy + height} {cz + radius} minecraft:air",
        f"execute in minecraft:overworld run setblock {cx - 4} {cy} {cz - 4} minecraft:torch",
        f"execute in minecraft:overworld run setblock {cx + 4} {cy} {cz + 4} minecraft:torch",
        f"execute in minecraft:overworld run kill @e[type=!player,x={cx},y={cy},z={cz},distance=..24]",
        f"gamemode survival {bot_name}",
        f"clear {bot_name}",
        f"effect clear {bot_name}",
        f"effect give {bot_name} minecraft:instant_health 1 10 true",
        f"effect give {bot_name} minecraft:saturation 5 10 true",
        f"execute in minecraft:overworld run spawnpoint {bot_name} {cx} {cy} {cz}",
        f"execute in minecraft:overworld run tp {bot_name} {cx + 0.5} {cy} {cz + 0.5}",
    ]
    rcon_batch(cmds, timeout=60)
    time.sleep(1.5)


def case_bug138_full_bag_smelt_craft(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-138: 35-slot full bag + raw_iron + coal + furnace -> smelt_items + craft_gear(iron_pickaxe)."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    # Give 31 stacks of filler junk + sticks + crafting_table + furnace + raw_iron + coal = 36 full slots
    cmds = [
        f"give {bot_name} minecraft:furnace 1",
        f"give {bot_name} minecraft:crafting_table 1",
        f"give {bot_name} minecraft:raw_iron 3",
        f"give {bot_name} minecraft:coal 2",
        f"give {bot_name} minecraft:stick 4",
    ]
    for _ in range(16):
        cmds.append(f"give {bot_name} minecraft:granite 64")
    for _ in range(15):
        cmds.append(f"give {bot_name} minecraft:diorite 64")
    rcon_batch(cmds, timeout=60)
    time.sleep(1.0)

    inv_before = bot.inventory() or {}
    slots_before = len(inv_before.get("items") or [])
    skills = MacroSkills(bot)
    smelt_res = skills.smelt_items("raw_iron", count=3, fuel_item="coal", wait_timeout_s=85.0)
    craft_res = skills.craft_gear("iron_pickaxe", count=1, max_duration_s=45.0)
    counts_after = _inv_counts(bot)
    passed = bool(smelt_res.get("ok")) and bool(craft_res.get("ok")) and counts_after.get("iron_pickaxe", 0) >= 1
    return {
        "passed": passed,
        "slots_before": slots_before,
        "smelt_ok": smelt_res.get("ok"),
        "smelt_error": smelt_res.get("error"),
        "craft_ok": craft_res.get("ok"),
        "craft_error": craft_res.get("error"),
        "iron_pickaxe_after": counts_after.get("iron_pickaxe", 0),
        "iron_ingot_after": counts_after.get("iron_ingot", 0),
    }


def case_bug142_elevated_hillside_tree(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-142: Hillside elevated oak tree (trunk starts at Y+2 on a 2-block dirt ledge)."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    tx, ty, tz = base_x + 4, base_y, base_z
    cmds = [
        # Build a 2-step hillside ledge at X+3..X+5
        f"execute in minecraft:overworld run fill {tx - 1} {ty} {tz - 2} {tx + 2} {ty} {tz + 2} minecraft:dirt",
        f"execute in minecraft:overworld run fill {tx} {ty + 1} {tz - 1} {tx + 1} {ty + 1} {tz + 1} minecraft:dirt",
        # Place 5 oak logs starting at Y+2
        f"execute in minecraft:overworld run fill {tx} {ty + 2} {tz} {tx} {ty + 6} {tz} minecraft:oak_log",
        f"give {bot_name} minecraft:stone_axe 1",
    ]
    rcon_batch(cmds, timeout=60)
    time.sleep(1.0)

    skills = MacroSkills(bot)
    res = skills.harvest_logs(target_logs=3, radius=16, max_duration_s=45.0)
    counts_after = _inv_counts(bot)
    logs_after = counts_after.get("oak_log", 0)
    passed = logs_after >= 3
    return {
        "passed": passed,
        "skill_ok": res.get("ok"),
        "stop_reason": res.get("stop_reason"),
        "error": res.get("error"),
        "oak_log_after": logs_after,
        "skill_gained": res.get("gained"),
        "skill_message": res.get("message"),
    }


def case_bug002_build_and_enter_portal(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-002: Build 4x5 Nether portal with minor obstacles nearby, ignite, and step into the_nether."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    cmds = [
        # Add minor terrain clutter around the pad so obstacle clearing is exercised
        f"execute in minecraft:overworld run setblock {base_x + 2} {base_y} {base_z} minecraft:dirt",
        f"execute in minecraft:overworld run setblock {base_x + 3} {base_y} {base_z + 1} minecraft:cobblestone",
        f"give {bot_name} minecraft:obsidian 14",
        f"give {bot_name} minecraft:flint_and_steel 1",
        f"give {bot_name} minecraft:iron_pickaxe 1",
        f"give {bot_name} minecraft:cobblestone 16",
    ]
    rcon_batch(cmds, timeout=60)
    time.sleep(1.0)

    skills = MacroSkills(bot)
    res = skills.build_nether_portal(enter=True, wait_s=15.0, max_duration_s=95.0)
    st = bot.status() or {}
    dim = str(st.get("dimension") or "").lower()
    passed = bool(res.get("ok")) and "nether" in dim
    # Return bot to overworld so subsequent tests stay clean
    if "nether" in dim:
        rcon_batch([f"execute in minecraft:overworld run tp {bot_name} {base_x + 0.5} {base_y} {base_z + 0.5}"], timeout=30)
        time.sleep(1.5)
    return {
        "passed": passed,
        "skill_ok": res.get("ok"),
        "entered_nether": res.get("entered_nether"),
        "dimension_after": dim,
        "error": res.get("error"),
    }


def case_bug007_craft_stone_pickaxe_exact(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-007: Exact 4 cobble + 2 sticks + 3 planks + placed table -> must craft; 1 cobble control -> structured missing."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    rcon_batch([
        f"execute in minecraft:overworld run setblock {base_x + 2} {base_y} {base_z} minecraft:crafting_table",
        f"give {bot_name} minecraft:cobblestone 4",
        f"give {bot_name} minecraft:stick 2",
        f"give {bot_name} minecraft:oak_planks 3",
    ], timeout=60)
    time.sleep(1.0)

    skills = MacroSkills(bot)
    ok_res = skills.craft_gear("stone_pickaxe", count=1, max_duration_s=30.0)
    counts_ok = _inv_counts(bot)

    # Control: insufficient materials (1 cobble + 2 sticks)
    rcon_batch([
        f"clear {bot_name}",
        f"give {bot_name} minecraft:cobblestone 1",
        f"give {bot_name} minecraft:stick 2",
    ], timeout=60)
    time.sleep(1.0)
    miss_res = skills.craft_gear("stone_pickaxe", count=1, max_duration_s=20.0)

    passed = (
        bool(ok_res.get("ok"))
        and counts_ok.get("stone_pickaxe", 0) >= 1
        and (not miss_res.get("ok"))
        and bool(miss_res.get("missing_materials") or miss_res.get("missing") or "missing" in str(miss_res.get("error") or ""))
    )
    return {
        "passed": passed,
        "exact_ok": ok_res.get("ok"),
        "stone_pickaxe_after": counts_ok.get("stone_pickaxe", 0),
        "exact_error": ok_res.get("error"),
        "control_ok": miss_res.get("ok"),
        "control_error": miss_res.get("error"),
        "control_missing": miss_res.get("missing_materials") or miss_res.get("missing"),
        "control_message": miss_res.get("message"),
    }


def case_bug155_move_to_float_range(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-155 + v1.3.21 move_to(range=0.0): real float 3D range guard & precise <=0.35m alignment."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    toolset = McToolset(bot)
    st0 = bot.status() or {}
    x0, y0, z0 = _extract_xyz(st0) or (base_x + 0.5, float(base_y), base_z + 0.5)

    # 1) Target 1.2m away with range=2.0 -> must return ok=True, already_within_range=True
    res_in = _as_dict(toolset.mc_move_to(x=round(x0 + 1.2, 2), y=round(y0, 2), z=round(z0, 2), range=2.0))
    res_in_body = res_in.get("result") if isinstance(res_in.get("result"), dict) else res_in
    # 2) Target 3.0m away with range=0.0 -> must walk and align within 0.35m
    tx, ty, tz = float(base_x + 3) + 0.5, float(base_y), float(base_z) + 0.5
    res_zero = _as_dict(toolset.mc_move_to(x=tx, y=ty, z=tz, range=0.0))
    st1 = bot.status() or {}
    x1, y1, z1 = _extract_xyz(st1) or (0.0, 0.0, 0.0)
    dist_after = math.hypot(x1 - tx, z1 - tz)

    already_in = bool(
        res_in.get("already_within_range")
        or res_in_body.get("already_within_range")
        or (isinstance(res_in_body.get("dist"), (int, float)) and float(res_in_body["dist"]) <= 2.05)
    )
    passed = (
        bool(res_in.get("ok"))
        and already_in
        and bool(res_zero.get("ok"))
        and dist_after <= 0.35
    )
    return {
        "passed": passed,
        "in_range_res": res_in,
        "zero_range_ok": res_zero.get("ok"),
        "zero_range_error": res_zero.get("error"),
        "pos_after": [round(x1, 3), round(y1, 3), round(z1, 3)],
        "dist_after_zero_range": round(dist_after, 3),
    }


def case_v1321_find_blocks_128(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """v1.3.21 find_blocks(radius=128): verify palette-prefiltered 90m distant block lookup."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    fx, fy, fz = base_x + 90, base_y + 1, base_z
    rcon_batch([
        f"forceload add {fx - 8} {fz - 8} {fx + 8} {fz + 8}",
        f"execute in minecraft:overworld run setblock {fx} {fy} {fz} minecraft:diamond_block",
    ], timeout=60)
    time.sleep(1.5)

    toolset = McToolset(bot)
    t0 = time.monotonic()
    res = _as_dict(toolset.mc_find_blocks(types=["diamond_block"], radius=128, limit=5))
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)

    rcon_batch([
        f"execute in minecraft:overworld run setblock {fx} {fy} {fz} minecraft:air",
        f"forceload remove {fx - 8} {fz - 8} {fx + 8} {fz + 8}",
    ], timeout=30)

    blocks = res.get("blocks") or (res.get("result") or {}).get("blocks") or []
    found_target = False
    for b in blocks:
        xyz = _extract_xyz(b)
        if xyz and abs(xyz[0] - fx) <= 1 and abs(xyz[1] - fy) <= 1 and abs(xyz[2] - fz) <= 1:
            found_target = True
            break
    passed = bool(res.get("ok")) and found_target
    return {
        "passed": passed,
        "elapsed_ms": elapsed_ms,
        "ok": res.get("ok"),
        "found_target": found_target,
        "blocks_count": len(blocks),
        "first_block": blocks[0] if blocks else None,
        "error": res.get("error"),
    }


def case_bug129_villager_trade(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-129: Villager merchant_trade live end-to-end verification."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    vx, vy, vz = base_x + 3, base_y, base_z
    nbt = (
        '{NoAI:1b,Invulnerable:1b,VillagerData:{level:2,profession:"minecraft:farmer",type:"minecraft:plains"},'
        'Offers:{Recipes:[{buy:{id:"minecraft:emerald",count:1},sell:{id:"minecraft:bread",count:2},maxUses:16}]}}'
    )
    rcon_batch([
        f"execute in minecraft:overworld run summon minecraft:villager {vx + 0.5} {vy} {vz + 0.5} {nbt}",
        f"give {bot_name} minecraft:emerald 8",
    ], timeout=60)
    time.sleep(1.5)

    toolset = McToolset(bot)
    ent_res = _as_dict(toolset.mc_get_entities(radius=16))
    ents = ent_res.get("entities") or (ent_res.get("result") or {}).get("entities") or []
    villager = next((e for e in ents if "villager" in str(e.get("name") or e.get("type") or "").lower()), None)
    if not villager:
        return {"passed": False, "error": "villager_not_found_in_entities", "entities_count": len(ents)}
    vid = int(villager["id"])
    skills = MacroSkills(bot)
    res = skills.merchant_trade(entity_id=vid, get_item="bread", times=1, max_duration_s=25.0)
    counts = _inv_counts(bot)
    passed = bool(res.get("ok")) and counts.get("bread", 0) >= 2
    return {
        "passed": passed,
        "villager_id": vid,
        "skill_ok": res.get("ok"),
        "error": res.get("error"),
        "bread_after": counts.get("bread", 0),
        "emerald_after": counts.get("emerald", 0),
    }


def case_bug154_furnace_smelt_start_collect(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-154: Non-blocking smelt_start -> wait -> smelt_collect live furnace take verification."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    rcon_batch([
        f"give {bot_name} minecraft:furnace 1",
        f"give {bot_name} minecraft:raw_iron 2",
        f"give {bot_name} minecraft:coal 1",
    ], timeout=60)
    time.sleep(1.0)

    skills = MacroSkills(bot)
    start_res = skills.smelt_start("raw_iron", count=2, fuel_item="coal", allow_multi_furnace=False)
    if not start_res.get("ok"):
        return {"passed": False, "phase": "smelt_start", "start_res": start_res}
    time.sleep(21.0)
    collect_res = skills.smelt_collect(furnaces=start_res.get("furnaces"), wait_remaining_s=15.0)
    counts = _inv_counts(bot)
    passed = bool(collect_res.get("ok")) and counts.get("iron_ingot", 0) >= 2
    return {
        "passed": passed,
        "start_ok": start_res.get("ok"),
        "collect_ok": collect_res.get("ok"),
        "collect_error": collect_res.get("error"),
        "iron_ingot_after": counts.get("iron_ingot", 0),
    }


def case_bug134_lava_obsidian_cast(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """BUG-134: Cast obsidian from a 3x3 lava pool with water_bucket and mine >=2 obsidian."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    lx, ly, lz = base_x + 3, base_y - 1, base_z
    rcon_batch([
        # Dig a 3x3 lava pool flush with the floor, with a cobblestone curb adjacent for safe water pouring
        f"execute in minecraft:overworld run fill {lx} {ly} {lz - 1} {lx + 2} {ly} {lz + 1} minecraft:lava",
        f"execute in minecraft:overworld run setblock {lx - 1} {base_y} {lz} minecraft:cobblestone",
        f"give {bot_name} minecraft:water_bucket 1",
        f"give {bot_name} minecraft:diamond_pickaxe 1",
    ], timeout=60)
    time.sleep(1.0)

    toolset = McToolset(bot)
    # Pour water on the curb block so water flows across the lava pool, casting obsidian
    pour_res = _as_dict(toolset.mc_interact(x=lx - 1, y=base_y, z=lz, item="water_bucket"))
    time.sleep(2.5)
    # Pick water back up from the curb top
    pickup_res = _as_dict(toolset.mc_interact(x=lx - 1, y=base_y + 1, z=lz, item="bucket"))
    time.sleep(2.0)

    skills = MacroSkills(bot)
    mine_res = skills.mine_blocks(["obsidian"], count=2, radius=10, max_duration_s=50.0)
    counts = _inv_counts(bot)
    passed = counts.get("obsidian", 0) >= 2
    return {
        "passed": passed,
        "pour_ok": pour_res.get("ok"),
        "pickup_ok": pickup_res.get("ok"),
        "mine_ok": mine_res.get("ok"),
        "mine_error": mine_res.get("error"),
        "obsidian_after": counts.get("obsidian", 0),
    }


def case_v1321_move_to_allow_partial_hazard(bot: BotSession, bot_name: str, base_x: int, base_y: int, base_z: int) -> dict[str, Any]:
    """v1.3.21 move_to(allow_partial=True) + cactus avoidance against an impassable 3-high wall."""
    _reset_arena(bot_name, base_x, base_y, base_z)
    wx = base_x + 4
    rcon_batch([
        # Full 3-high sealed room around target (base_x+7, base_z) so target is unreachable
        f"execute in minecraft:overworld run fill {wx} {base_y} {base_z - 5} {wx + 6} {base_y + 3} {base_z + 5} minecraft:cobblestone hollow",
    ], timeout=60)
    time.sleep(1.0)

    st0 = bot.status() or {}
    hp_before = float(st0.get("health") or 20.0)
    toolset = McToolset(bot)
    res = _as_dict(toolset.mc_move_to(x=base_x + 7.5, y=float(base_y), z=base_z + 0.5, range=1.0, allow_partial=True))
    body = res.get("result") if isinstance(res.get("result"), dict) else res
    st1 = bot.status() or {}
    hp_after = float(st1.get("health") or 0.0)
    x1, _, _ = _extract_xyz(st1) or (float(base_x), float(base_y), float(base_z))
    progressed = x1 > base_x + 1.5
    passed = bool(res.get("ok")) and bool(body.get("partial") or res.get("partial")) and progressed and hp_after >= hp_before
    return {
        "passed": passed,
        "ok": res.get("ok"),
        "partial": body.get("partial") or res.get("partial"),
        "dist": body.get("dist"),
        "x_after": round(x1, 2),
        "hp_before": hp_before,
        "hp_after": hp_after,
        "error": res.get("error"),
    }


CASES: dict[str, Callable[[BotSession, str, int, int, int], dict[str, Any]]] = {
    "bug155_move_to_float_range": case_bug155_move_to_float_range,
    "v1321_find_blocks_128": case_v1321_find_blocks_128,
    "bug007_craft_stone_pickaxe_exact": case_bug007_craft_stone_pickaxe_exact,
    "bug142_elevated_hillside_tree": case_bug142_elevated_hillside_tree,
    "bug138_full_bag_smelt_craft": case_bug138_full_bag_smelt_craft,
    "bug002_build_and_enter_portal": case_bug002_build_and_enter_portal,
    "bug129_villager_trade": case_bug129_villager_trade,
    "bug154_furnace_smelt_start_collect": case_bug154_furnace_smelt_start_collect,
    "bug134_lava_obsidian_cast": case_bug134_lava_obsidian_cast,
    "v1321_move_to_allow_partial_hazard": case_v1321_move_to_allow_partial_hazard,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run targeted live RCON micro-fixture bug verifications.")
    parser.add_argument("--bot", default="HY_5E_4")
    parser.add_argument("--token-file", default=str(MAIN_ROOT / ".state-5ease" / "tokens.json"))
    parser.add_argument("--env-file", default=os.environ.get("MC_ENV_FILE", str(MAIN_ROOT / ".env.local")))
    parser.add_argument("--cases", nargs="*", default=list(CASES.keys()), choices=list(CASES.keys()))
    parser.add_argument("--arena-x", type=int, default=80000)
    parser.add_argument("--arena-y", type=int, default=140)
    parser.add_argument("--arena-z", type=int, default=80000)
    parser.add_argument("--out", default=str(WT_ROOT / "runs" / "historical_bug_verify_latest.json"))
    args = parser.parse_args()

    settings = Settings.load(dotenv_path=pathlib.Path(args.env_file))
    token_file = pathlib.Path(args.token_file)
    rec = TokenStore(token_file).get(args.bot)
    if rec is None:
        raise SystemExit(f"Missing cached token for {args.bot} in {token_file}")

    results: dict[str, Any] = {
        "bot": args.bot,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "api_base_url": settings.mc_api_base_url,
        "cases": {},
    }

    with BotSession(
        rec.token,
        bot_username=rec.bot_username,
        base_url=settings.mc_api_base_url,
        reauth_callback=build_token_refresher(args.bot, token_file, settings),
    ) as bot:
        ensure_bot_ready(bot, timeout_s=45.0)
        for idx, name in enumerate(args.cases):
            fn = CASES[name]
            cx = args.arena_x + idx * 64
            cz = args.arena_z
            t0 = time.monotonic()
            print(f"[RUN] {name} @ ({cx}, {args.arena_y}, {cz}) ...", flush=True)
            try:
                res = fn(bot, args.bot, cx, args.arena_y, cz)
            except Exception as exc:
                res = {"passed": False, "exception": f"{exc.__class__.__name__}: {exc}"}
            res["elapsed_s"] = round(time.monotonic() - t0, 2)
            results["cases"][name] = res
            status = "PASS" if res.get("passed") else "FAIL"
            print(f"[{status}] {name} ({res['elapsed_s']}s): {json.dumps(res, ensure_ascii=False)}", flush=True)

        # Clean up only the forceloaded chunks around our test strip
        rcon_batch(
            [
                f"forceload remove {args.arena_x - 32} {args.arena_z - 32} "
                f"{args.arena_x + len(args.cases) * 64 + 128} {args.arena_z + 32}"
            ],
            timeout=30,
        )

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed_n = sum(1 for v in results["cases"].values() if v.get("passed"))
    total_n = len(results["cases"])
    print(f"\n=== SUMMARY: {passed_n}/{total_n} PASSED -> {out_path} ===", flush=True)
    return 0 if passed_n == total_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
