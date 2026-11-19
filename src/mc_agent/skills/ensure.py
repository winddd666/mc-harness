"""Deterministic 0-LLM physical hard-prerequisite resolver (`ensure_items`) & async furnace helpers.

Design boundaries (strict):
1. Encodes ONLY objective Minecraft 1.21.11 hard laws:
   - Crafting recipes (`RECIPES_1_21` + wood/stone equivalence),
   - Furnace smelting pairs (`raw_iron -> iron_ingot`, `cobblestone -> stone`, etc.),
   - Minimum pickaxe harvest tiers (`tier 1..4` for stone/iron/diamond/obsidian).
2. Zero subjective tactical gates: never forces shields, beds, armor sets, or food quotas
   unless explicitly requested by the caller.
3. Checks inventory + equipped offhand/armor FIRST, then nearby `WorldMemory` storage chests
   (within 72m) and active `WorldJobRegistry` furnace conversions, before gathering/smelting/crafting.
4. By default (`allow_underground_dfs=False`), NEVER opens long unguarded underground `mine_dfs`
   shafts inside `pre_ensure`; missing underground ores return `requires_world_hunt` in `< 0.5s`
   so the main Harness loop (with full Subcortical Reflex & `episode_gate`) handles deep mining.
"""

from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Any

from ..errors import ApiError
from ..kb import RECIPES_1_21, normalize_dimension, resolve_item_alias
from .safety import check_and_stabilize

# Objective smelting output -> (raw_input_item, yield_per_input)
SMELT_SOURCES: dict[str, tuple[str, int]] = {
    "iron_ingot": ("raw_iron", 1),
    "gold_ingot": ("raw_gold", 1),
    "copper_ingot": ("raw_copper", 1),
    "stone": ("cobblestone", 1),
    "glass": ("sand", 1),
    "charcoal": ("any_log", 1),
    "terracotta": ("clay", 1),
    "brick": ("clay_ball", 1),
}

# Safe fuels ONLY (never burn beds, wooden tools, crafting tables, boats, or blaze rods!)
SAFE_FURNACE_FUELS: tuple[tuple[str, float], ...] = (
    ("coal", 8.0),
    ("charcoal", 8.0),
    ("coal_block", 80.0),
    ("dried_kelp_block", 20.0),
)

# Minimum pickaxe tier required by vanilla harvest physics
TIER_TO_PICKAXE: dict[int, str] = {
    1: "wooden_pickaxe",
    2: "stone_pickaxe",
    3: "iron_pickaxe",
    4: "diamond_pickaxe",
}

PICKAXE_TO_TIER: dict[str, int] = {
    "wooden_pickaxe": 1,
    "golden_pickaxe": 1,
    "stone_pickaxe": 2,
    "iron_pickaxe": 3,
    "diamond_pickaxe": 4,
    "netherite_pickaxe": 5,
}

GATHER_SOURCES: dict[str, dict[str, Any]] = {
    "any_log": {"kind": "logs", "min_tier": 0},
    "oak_log": {"kind": "logs", "min_tier": 0},
    "cobblestone": {
        "kind": "blocks_or_tunnel",
        "blocks": ["cobblestone", "stone", "deepslate", "cobbled_deepslate"],
        "min_tier": 1,
    },
    "coal": {
        "kind": "coal_or_dfs",
        "blocks": ["coal_ore", "deepslate_coal_ore"],
        "dfs_target": "coal",
        "dfs_y": 44,
        "min_tier": 1,
    },
    "coal_or_charcoal": {
        "kind": "coal_or_charcoal",
        "blocks": ["coal_ore", "deepslate_coal_ore"],
        "min_tier": 1,
    },
    "raw_iron": {
        "kind": "blocks_then_dfs",
        "blocks": ["iron_ore", "deepslate_iron_ore"],
        "dfs_target": "iron",
        "dfs_y": 16,
        "min_tier": 2,
    },
    "raw_copper": {
        "kind": "blocks_then_dfs",
        "blocks": ["copper_ore", "deepslate_copper_ore"],
        "dfs_target": "copper",
        "dfs_y": 44,
        "min_tier": 2,
    },
    "lapis_lazuli": {
        "kind": "blocks_then_dfs",
        "blocks": ["lapis_ore", "deepslate_lapis_ore"],
        "dfs_target": "lapis",
        "dfs_y": 0,
        "min_tier": 2,
    },
    "raw_gold": {
        "kind": "blocks_then_dfs",
        "blocks": ["gold_ore", "deepslate_gold_ore"],
        "dfs_target": "gold",
        "dfs_y": -16,
        "min_tier": 3,
    },
    "redstone": {
        "kind": "blocks_then_dfs",
        "blocks": ["redstone_ore", "deepslate_redstone_ore"],
        "dfs_target": "redstone",
        "dfs_y": -54,
        "min_tier": 3,
    },
    "diamond": {
        "kind": "blocks_then_dfs",
        "blocks": ["diamond_ore", "deepslate_diamond_ore"],
        "dfs_target": "diamond",
        "dfs_y": -54,
        "min_tier": 3,
    },
    "obsidian": {
        "kind": "blocks_only",
        "blocks": ["obsidian"],
        "min_tier": 4,
    },
    "sand": {"kind": "blocks_only", "blocks": ["sand", "red_sand"], "min_tier": 0},
    "gravel": {"kind": "blocks_only", "blocks": ["gravel"], "min_tier": 0},
    "flint": {"kind": "flint_from_gravel", "blocks": ["gravel"], "min_tier": 0},
    "sugar_cane": {"kind": "blocks_only", "blocks": ["sugar_cane"], "min_tier": 0},
    "pumpkin": {"kind": "blocks_only", "blocks": ["pumpkin"], "min_tier": 0},
    "clay_ball": {"kind": "blocks_only", "blocks": ["clay"], "min_tier": 0},
}

EXTRA_OBJECTIVE_RECIPES: dict[str, dict[str, Any]] = {
    "stone_bricks": {"table_3x3": False, "yield_count": 4, "ingredients": {"stone": 4}},
    "stone_brick_slab": {"table_3x3": True, "yield_count": 6, "ingredients": {"stone_bricks": 3}},
    "chiseled_stone_bricks": {"table_3x3": False, "yield_count": 1, "ingredients": {"stone_brick_slab": 2}},
    "dropper": {"table_3x3": True, "yield_count": 1, "ingredients": {"cobblestone": 7, "redstone": 1}},
    "crafter": {
        "table_3x3": True,
        "yield_count": 1,
        "ingredients": {"iron_ingot": 5, "crafting_table": 1, "redstone": 1, "dropper": 1},
    },
    "iron_block": {"table_3x3": True, "yield_count": 1, "ingredients": {"iron_ingot": 9}},
    "gold_block": {"table_3x3": True, "yield_count": 1, "ingredients": {"gold_ingot": 9}},
    "diamond_block": {"table_3x3": True, "yield_count": 1, "ingredients": {"diamond": 9}},
}


def total_owned_counts(inv: dict[str, Any]) -> dict[str, int]:
    """Count items across main inventory, offhand, and worn armor slots."""
    counts: dict[str, int] = {}
    for row in inv.get("items") or []:
        if not isinstance(row, dict):
            continue
        nm = str(row.get("name") or "").removeprefix("minecraft:")
        cnt = int(row.get("count") or 0)
        if nm and cnt > 0:
            counts[nm] = counts.get(nm, 0) + cnt

    off = inv.get("offhand")
    if isinstance(off, dict) and off.get("name"):
        onm = str(off["name"]).removeprefix("minecraft:")
        if onm and onm != "air":
            ocnt = max(1, int(off.get("count") or 1))
            counts[onm] = counts.get(onm, 0) + ocnt

    armor = inv.get("armor")
    if isinstance(armor, dict):
        for slot_key in ("head", "chest", "torso", "legs", "feet"):
            piece = armor.get(slot_key)
            if isinstance(piece, dict) and piece.get("name"):
                anm = str(piece["name"]).removeprefix("minecraft:")
                if anm and anm != "air":
                    counts[anm] = counts.get(anm, 0) + 1
    return counts


def effective_item_count(item: str, counts: dict[str, int]) -> int:
    """Evaluate how many units of `item` (including wildcard tags like `any_planks`) we currently own."""
    key = str(item or "").removeprefix("minecraft:")
    if key == "any_log":
        return sum(v for k, v in counts.items() if k.endswith(("_log", "_stem")))
    if key == "any_planks":
        return sum(v for k, v in counts.items() if k.endswith("_planks"))
    if key == "any_wool":
        return sum(v for k, v in counts.items() if k.endswith("_wool"))
    if key == "any_wooden_slab":
        return sum(
            v
            for k, v in counts.items()
            if k.endswith("_slab")
            and any(
                k.startswith(p)
                for p in (
                    "oak_",
                    "spruce_",
                    "birch_",
                    "jungle_",
                    "acacia_",
                    "dark_oak_",
                    "cherry_",
                    "mangrove_",
                    "bamboo_",
                    "crimson_",
                    "warped_",
                    "pale_oak_",
                )
            )
        )
    if key == "coal_or_charcoal":
        return counts.get("coal", 0) + counts.get("charcoal", 0)
    if key == "cobblestone":
        return counts.get("cobblestone", 0) + counts.get("cobbled_deepslate", 0)
    return counts.get(key, 0)


def highest_pickaxe_tier(counts: dict[str, int]) -> int:
    best = 0
    for name, tier in PICKAXE_TO_TIER.items():
        if counts.get(name, 0) > 0:
            best = max(best, tier)
    return best


def _lookup_objective_recipe(item: str) -> dict[str, Any] | None:
    if item in RECIPES_1_21:
        return RECIPES_1_21[item]
    if item in EXTRA_OBJECTIVE_RECIPES:
        return EXTRA_OBJECTIVE_RECIPES[item]
    return None


def _select_safe_fuel(counts: dict[str, int], *, input_item: str, smelt_n: int) -> tuple[str | None, float, int]:
    """Choose a strictly safe furnace fuel `(fuel_name, items_per_fuel, total_avail)`."""
    for fname, per_f in SAFE_FURNACE_FUELS:
        avail = counts.get(fname, 0) - (smelt_n if fname == input_item else 0)
        if avail > 0:
            return fname, per_f, avail
    for k in sorted(counts):
        if k.endswith("_planks"):
            avail = counts.get(k, 0) - (smelt_n if k == input_item else 0)
            if avail > 0:
                return k, 1.5, avail
    if counts.get("stick", 0) >= 2 and input_item != "stick":
        return "stick", 0.5, counts["stick"]
    return None, 0.0, 0


def start_smelt_batch_nonblocking(
    skills: Any,
    *,
    input_item: str,
    output_item: str,
    count: int,
    allow_multi_furnace: bool = True,
) -> dict[str, Any]:
    """Ignite 1..3 furnaces in parallel and return immediately (`0` sleep!).

    Automatically delegates to `skills.smelt_start(...)` once `l2-live-pack` is merged.
    """
    if hasattr(skills, "smelt_start"):
        res = skills.smelt_start(input_item, count=count, allow_multi_furnace=allow_multi_furnace)
        if isinstance(res, dict) and res.get("ok"):
            res.setdefault("output_item", output_item)
            # Normalize wall-clock `time.time()` from `smelt_start` to `time.monotonic()` for `WorldJobRegistry`!
            now_m = time.monotonic()
            est_s = float(res.get("estimated_wait_s") or 0.0)
            res["ready_at"] = now_m + est_s
            for f_row in res.get("furnaces") or []:
                if isinstance(f_row, dict) and "estimated_wait_s" in f_row:
                    f_row["ready_at"] = round(now_m + float(f_row["estimated_wait_s"]), 2)
        return res

    bot = skills.bot
    inv = bot.inventory()
    counts = total_owned_counts(inv)
    avail_in = counts.get(input_item, 0)
    if avail_in < 1:
        return {"ok": False, "error": "missing_ingredients", "message": f"背包没有 {input_item}。"}
    smelt_n = min(max(1, int(count)), avail_in)

    desired_k = min(3, max(1, smelt_n // 6)) if (allow_multi_furnace and smelt_n >= 8) else 1
    furnace_positions: list[list[int]] = []
    try:
        found = bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": 5})
        for b in (found or {}).get("blocks") or []:
            if isinstance(b, dict) and isinstance(b.get("pos"), list) and len(b["pos"]) >= 3:
                p = [int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2])]
                if p not in furnace_positions:
                    furnace_positions.append(p)
                if len(furnace_positions) >= desired_k:
                    break
    except Exception:
        pass

    st = bot.status()
    pos = st.get("pos") or [0.0, 64.0, 0.0]
    bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
    offsets = [(1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 0, 1), (-1, 0, -1), (1, 0, -1), (-1, 0, 1)]

    while len(furnace_positions) < desired_k:
        counts = total_owned_counts(bot.inventory())
        if counts.get("furnace", 0) < 1:
            # Reserve cobblestone if smelting cobblestone -> stone!
            cobble_reserve = smelt_n if input_item == "cobblestone" else 0
            if counts.get("cobblestone", 0) - 8 < cobble_reserve:
                if furnace_positions:
                    break
                return {"ok": False, "error": "missing_furnace", "message": "缺少熔炉且圆石不足。"}
            cg = skills.craft_gear("furnace", 1)
            if not cg.get("ok"):
                if furnace_positions:
                    break
                return cg

        placed_pos: list[int] | None = None
        for dx, dy, dz in offsets:
            cand = [bx + dx, by + dy, bz + dz]
            if cand in furnace_positions:
                continue
            try:
                pl = bot.call("place", {"item": "furnace", "pos": cand})
                p_out = pl.get("pos") or cand
                placed_pos = [int(p_out[0]), int(p_out[1]), int(p_out[2])]
                break
            except ApiError:
                continue
        if placed_pos is None:
            try:
                pl0 = bot.call("place", {"item": "furnace"})
                p_out0 = pl0.get("pos")
                if isinstance(p_out0, list) and len(p_out0) >= 3:
                    placed_pos = [int(p_out0[0]), int(p_out0[1]), int(p_out0[2])]
            except ApiError:
                pass
        if placed_pos is None:
            break
        furnace_positions.append(placed_pos)

    if not furnace_positions:
        return {"ok": False, "error": "place_furnace_failed", "message": "无法放置或定位熔炉。"}

    counts = total_owned_counts(bot.inventory())
    fuel_name, per_fuel, fuel_avail = _select_safe_fuel(counts, input_item=input_item, smelt_n=smelt_n)
    if fuel_name is None:
        # Convert 1 log into 4 planks safely if logs exist
        log_k = next((k for k, v in counts.items() if k.endswith(("_log", "_stem")) and v > (1 if k == input_item else 0)), None)
        if log_k:
            plank_nm = log_k.replace("_log", "_planks").replace("_stem", "_planks")
            try:
                skills._safe_craft(plank_nm, 4)
                counts = total_owned_counts(bot.inventory())
                fuel_name, per_fuel, fuel_avail = _select_safe_fuel(counts, input_item=input_item, smelt_n=smelt_n)
            except Exception:
                pass
    if fuel_name is None or fuel_avail < 1:
        return {"ok": False, "error": "missing_fuel", "message": "缺少安全熔炉燃料。"}

    k_eff = min(len(furnace_positions), smelt_n)
    base_chunk = smelt_n // k_eff
    rem_chunk = smelt_n % k_eff
    furnaces_started: list[dict[str, Any]] = []
    fuel_remaining = fuel_avail
    max_chunk = 0
    now_mono = time.monotonic()

    for idx in range(k_eff):
        chunk_n = base_chunk + (1 if idx < rem_chunk else 0)
        if chunk_n <= 0 or fuel_remaining <= 0:
            continue
        f_need = min(fuel_remaining, max(1, math.ceil(chunk_n / max(0.25, per_fuel))))
        fpos = furnace_positions[idx]
        try:
            st_cur = bot.status()
            cpos = st_cur.get("pos") or pos
            if math.hypot(float(cpos[0]) - fpos[0], float(cpos[2]) - fpos[2]) > 4.2:
                bot.call("move_to", {"x": fpos[0], "y": fpos[1], "z": fpos[2], "range": 2.5})
            bot.call(
                "window/act",
                {
                    "target": {"pos": fpos},
                    "ops": [
                        {"op": "put", "item": input_item, "count": chunk_n, "to": "input"},
                        {"op": "put", "item": fuel_name, "count": f_need, "to": "fuel"},
                    ],
                },
            )
            fuel_remaining -= f_need
            max_chunk = max(max_chunk, chunk_n)
            furnaces_started.append(
                {
                    "pos": fpos,
                    "count": chunk_n,
                    "fuel": {"item": fuel_name, "count": f_need},
                    "ready_at": round(now_mono + 10.0 * chunk_n + 2.0, 2),
                }
            )
        except ApiError:
            continue

    if not furnaces_started:
        return {"ok": False, "error": "ignite_failed", "message": "向熔炉投料失败。"}

    est_wait_s = round(10.0 * max_chunk + 2.0, 2)
    total_started = sum(int(f["count"]) for f in furnaces_started)
    return {
        "ok": True,
        "input": input_item,
        "output_item": output_item,
        "requested_count": total_started,
        "furnace_pos": furnaces_started[0]["pos"],
        "furnaces": furnaces_started,
        "estimated_wait_s": est_wait_s,
        "ready_at": now_mono + est_wait_s,
    }


def collect_smelt_batch(
    skills: Any,
    furnaces: list[dict[str, Any]],
    *,
    wait_remaining_s: float = 12.0,
) -> dict[str, Any]:
    """Collect finished outputs from one or more furnace positions."""
    if hasattr(skills, "smelt_collect"):
        if furnaces:
            f0 = furnaces[0].get("pos") if isinstance(furnaces[0], dict) else furnaces[0]
            if isinstance(f0, (list, tuple)) and len(f0) >= 3:
                try:
                    st0 = skills.bot.status()
                    cp0 = st0.get("pos") or f0
                    if math.hypot(float(cp0[0]) - (float(f0[0]) + 0.5), float(cp0[2]) - (float(f0[2]) + 0.5)) > 4.2:
                        skills.navigate_segmented(float(f0[0]) + 0.5, float(f0[2]) + 0.5, range_m=2.6)
                except Exception:
                    pass
        res_sc = skills.smelt_collect(furnaces, wait_remaining_s=wait_remaining_s)
        if isinstance(res_sc, dict) and res_sc.get("failures"):
            rem_f = [
                {"pos": f["pos"]}
                for f in res_sc["failures"]
                if isinstance(f, dict) and f.get("pos") and f.get("error") not in {"no_window", "not_found"}
            ]
            if rem_f:
                res_sc["remaining_furnaces"] = rem_f
                res_sc["still_burning"] = True
        return res_sc

    bot = skills.bot
    collected: list[dict[str, Any]] = []
    remaining_furnaces: list[dict[str, Any]] = []
    still_burning = False
    had_error = False

    for f_entry in furnaces:
        fpos = f_entry.get("pos") if isinstance(f_entry, dict) else f_entry
        if not isinstance(fpos, (list, tuple)) or len(fpos) < 3:
            continue
        pos_list = [int(fpos[0]), int(fpos[1]), int(fpos[2])]
        try:
            st = bot.status()
            cpos = st.get("pos") or pos_list
            dist_xz = math.hypot(float(cpos[0]) - (pos_list[0] + 0.5), float(cpos[2]) - (pos_list[2] + 0.5))
            dy = float(pos_list[1]) - float(cpos[1])
            if dist_xz > 4.0 or abs(dy) >= 1.5:
                try:
                    skills.navigate_segmented(float(pos_list[0]) + 0.5, float(pos_list[2]) + 0.5, range_m=2.6)
                except Exception:
                    pass
                st2 = bot.status()
                cpos2 = st2.get("pos") or cpos
                if math.hypot(float(cpos2[0]) - (pos_list[0] + 0.5), float(cpos2[2]) - (pos_list[2] + 0.5)) > 4.5 or (float(pos_list[1]) - float(cpos2[1])) >= 1.5:
                    try:
                        skills.unstick()
                        skills.navigate_segmented(float(pos_list[0]) + 0.5, float(pos_list[2]) + 0.5, range_m=2.6)
                    except Exception:
                        bot.call("move_to", {"x": pos_list[0], "z": pos_list[2], "range": 2.5})
            deadline = time.monotonic() + max(0.0, float(wait_remaining_s))
            while True:
                view = bot.call("window/view", {"target": {"pos": pos_list}})
                sections = (view or {}).get("sections") or {}
                inputs_left = sum(int(x.get("count") or 0) for x in (sections.get("input") or []))
                info = (view or {}).get("info") or {}
                burning = bool(info.get("burning"))
                if inputs_left == 0 or not burning or time.monotonic() >= deadline:
                    if inputs_left > 0 and burning:
                        still_burning = True
                    break
                time.sleep(2.0)

            view_end = bot.call("window/view", {"target": {"pos": pos_list}})
            outputs = [
                o
                for o in (((view_end or {}).get("sections") or {}).get("output") or [])
                if isinstance(o, dict) and int(o.get("count") or 0) > 0
            ]
            for out_row in outputs:
                cnt = int(out_row.get("count") or 0)
                bot.call(
                    "window/act",
                    {"target": {"pos": pos_list}, "ops": [{"op": "take", "from": "output", "count": cnt}]},
                )
                collected.append(
                    {
                        "name": str(out_row.get("name") or "").removeprefix("minecraft:"),
                        "count": cnt,
                        "pos": pos_list,
                    }
                )
            if still_burning:
                remaining_furnaces.append(f_entry if isinstance(f_entry, dict) else {"pos": pos_list})
        except ApiError:
            had_error = True
            remaining_furnaces.append(f_entry if isinstance(f_entry, dict) else {"pos": pos_list})
            continue

    return {
        "ok": (len(collected) > 0 or not still_burning) and not had_error,
        "collected": collected,
        "still_burning": still_burning,
        "remaining_furnaces": remaining_furnaces,
    }


class PhysicalPrerequisiteResolver:
    """Bounded, survival-guarded recursive physical material resolver."""

    def __init__(
        self,
        skills: Any,
        *,
        geofence: Any = None,
        stop_check: Callable[[], bool] | None = None,
        allow_underground_dfs: bool = False,
        world_jobs: Any | None = None,
        parent_target_key: str | None = None,
        async_smelt_threshold_s: float = 25.0,
    ) -> None:
        self.skills = skills
        self.bot = skills.bot
        self.geofence = geofence
        self.stop_check = stop_check
        self.allow_underground_dfs = bool(allow_underground_dfs)
        self.world_jobs = world_jobs
        self.parent_target_key = parent_target_key
        self.async_smelt_threshold_s = max(10.0, float(async_smelt_threshold_s))

    def _has_nearby_station(self, block_name: str, radius: int = 7) -> bool:
        try:
            found = self.bot.call("find_blocks", {"radius": radius, "types": [block_name], "limit": 1})
            blks = (found or {}).get("blocks") or []
            if blks:
                return True
            found_far = self.bot.call("find_blocks", {"radius": 32, "types": [block_name], "limit": 1})
            far_blks = (found_far or {}).get("blocks") or []
            target_pos = far_blks[0].get("pos") if (far_blks and isinstance(far_blks[0], dict)) else None
            st = self.bot.status()
            pos = st.get("pos")
            if target_pos is None and isinstance(pos, (list, tuple)) and len(pos) >= 3:
                wm = self.skills._get_world_memory()
                mem_hits = wm.query_pois([block_name], origin=pos, dim=normalize_dimension(st.get("dimension")), limit=1)
                if mem_hits and float(mem_hits[0].get("dist", 999.0)) <= 64.0 and abs(float(mem_hits[0]["pos"][1]) - float(pos[1])) <= 12.0:
                    target_pos = mem_hits[0]["pos"]
            if isinstance(target_pos, list) and len(target_pos) >= 3:
                self.skills.navigate_segmented(
                    float(target_pos[0]) + 0.5,
                    float(target_pos[2]) + 0.5,
                    range_m=2.6,
                    geofence=self.geofence,
                )
                chk = self.bot.call("find_blocks", {"radius": 6, "types": [block_name], "limit": 1})
                if (chk or {}).get("blocks"):
                    return True
        except Exception:
            return False
        return False

    def ensure_items(
        self,
        targets: dict[str, int] | list[dict[str, Any]],
        *,
        max_duration_s: float = 180.0,
        equip_slots: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        req_map = self._normalize_targets(targets)
        if not req_map:
            return {"ok": False, "error": "bad_args", "message": "targets 不能为空。"}

        started = time.monotonic()
        deadline = started + max(5.0, float(max_duration_s))
        steps_log: list[dict[str, Any]] = []
        hunt_missing: dict[str, int] = {}
        spawned_job_id: str | None = None

        st0 = self.bot.status()
        hp_floor = float(st0.get("health") or 20.0)
        last_stab_mono = [0.0]

        def _guard() -> str | None:
            if self.stop_check is not None and self.stop_check():
                return "interrupted"
            if time.monotonic() >= deadline:
                return "time_budget_slice"
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                return "not_ready"
            hp = float(st.get("health") or 20.0)
            food = int(st.get("food") or 20)
            nh = st.get("nearest_hostile")
            nh_dist = float(nh.get("dist", 999.0)) if isinstance(nh, dict) and nh.get("name") else 999.0

            if hp < hp_floor - 1.5 and nh_dist <= 12.0:
                return "threat_interrupted"

            now_g = time.monotonic()
            in_lava = bool(st.get("in_lava"))
            in_water = bool(st.get("in_water"))
            if in_lava or ((in_water or hp <= 12.0 or food <= 12) and (now_g - last_stab_mono[0] >= 10.0)):
                last_stab_mono[0] = now_g
                try:
                    stab = check_and_stabilize(self.bot)
                    steps_log.append({"step": "stabilize", "ok": bool(stab.get("ok", True))})
                except ApiError:
                    pass
                st_after = self.bot.status()
                hp = float(st_after.get("health") or hp)
                if st_after.get("in_lava") or (hp <= 8.0 and nh_dist <= 10.0):
                    return "threat_interrupted"
            return None

        stop_reason: str | None = None
        for item_name, want_cnt in req_map.items():
            abort = _guard()
            if abort is not None:
                stop_reason = abort
                break
            err_reason = self._satisfy_item(
                item_name,
                want_cnt,
                deadline=deadline,
                depth=0,
                visited=set(),
                steps_log=steps_log,
                hunt_missing=hunt_missing,
                guard_fn=_guard,
            )
            if err_reason is not None and err_reason not in {"satisfied"}:
                stop_reason = err_reason
                if err_reason == "waiting_for_world_job":
                    last_step = steps_log[-1] if steps_log else {}
                    spawned_job_id = last_step.get("job_id")
                    break
                if err_reason in {"interrupted", "time_budget_slice", "threat_interrupted", "not_ready"}:
                    break

        if equip_slots:
            inv_eq = self.bot.inventory()
            owned_eq = total_owned_counts(inv_eq)
            for eq_item, slot_name in equip_slots.items():
                canon = resolve_item_alias(eq_item)
                eff_slot = "torso" if slot_name == "chest" else slot_name
                if owned_eq.get(canon, 0) > 0:
                    try:
                        self.bot.call("equip", {"item": canon, "slot": eff_slot})
                        steps_log.append({"step": "equip", "item": canon, "slot": eff_slot, "ok": True})
                    except ApiError:
                        pass

        inv_final = self.bot.inventory()
        final_counts = total_owned_counts(inv_final)
        remaining_deficit: dict[str, int] = {}
        for item_name, want_cnt in req_map.items():
            have_now = effective_item_count(item_name, final_counts)
            if have_now < want_cnt:
                remaining_deficit[item_name] = want_cnt - have_now

        ok = len(remaining_deficit) == 0
        out: dict[str, Any] = {
            "ok": ok,
            "targets": req_map,
            "remaining_deficit": remaining_deficit,
            "steps_executed": len(steps_log),
            "steps": steps_log[-16:],
            "elapsed_s": round(time.monotonic() - started, 2),
        }
        if spawned_job_id:
            out["waiting_job_id"] = spawned_job_id
        if not ok:
            eff_stop = stop_reason or ("requires_world_hunt" if hunt_missing else "material_unavailable")
            out["stop_reason"] = eff_stop
            out["requires_replan"] = eff_stop in {"threat_interrupted", "requires_world_hunt", "material_unavailable"}
            out["partial_progress"] = len(steps_log) > 0
            if hunt_missing:
                out["missing_hunt_items"] = hunt_missing
            wm = self.skills._get_world_memory()
            try:
                st_now = self.bot.status()
                pos_now = st_now.get("pos")
                dim_now = normalize_dimension(st_now.get("dimension"))
                hint = wm.shortage_memory_hint(
                    list(hunt_missing.keys()) + list(remaining_deficit.keys()),
                    origin=pos_now,
                    dim=dim_now,
                )
                if hint:
                    out["memory_hint"] = hint
            except Exception:
                pass
        return out

    @staticmethod
    def _normalize_targets(raw: dict[str, int] | list[dict[str, Any]]) -> dict[str, int]:
        merged: dict[str, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                nm = resolve_item_alias(str(k))
                cnt = int(v or 0)
                if nm and cnt > 0:
                    merged[nm] = merged.get(nm, 0) + cnt
        elif isinstance(raw, list):
            for row in raw:
                if isinstance(row, dict):
                    nm = resolve_item_alias(str(row.get("item") or row.get("name") or ""))
                    cnt = int(row.get("count") or 1)
                    if nm and cnt > 0:
                        merged[nm] = merged.get(nm, 0) + cnt
        return merged

    def _try_withdraw_from_memory_chest(
        self,
        item: str,
        deficit: int,
        *,
        steps_log: list[dict[str, Any]],
    ) -> int:
        if item.startswith("any_") or item == "coal_or_charcoal":
            if item == "any_log":
                candidates = [
                    k
                    for k in (
                        "oak_log",
                        "spruce_log",
                        "birch_log",
                        "jungle_log",
                        "acacia_log",
                        "dark_oak_log",
                        "mangrove_log",
                        "cherry_log",
                        "pale_oak_log",
                        "crimson_stem",
                        "warped_stem",
                    )
                ]
            elif item == "any_planks":
                candidates = [
                    f"{p}_planks"
                    for p in (
                        "oak",
                        "spruce",
                        "birch",
                        "jungle",
                        "acacia",
                        "dark_oak",
                        "mangrove",
                        "cherry",
                        "pale_oak",
                        "crimson",
                        "warped",
                    )
                ]
            elif item == "coal_or_charcoal":
                candidates = ["coal", "charcoal"]
            else:
                candidates = []
        else:
            candidates = [item]
        if not candidates:
            return 0
        try:
            st = self.bot.status()
            pos = st.get("pos")
            if not isinstance(pos, (list, tuple)) or len(pos) < 3:
                return 0
            dim = normalize_dimension(st.get("dimension"))
            wm = self.skills._get_world_memory()
        except Exception:
            return 0

        withdrawn = 0
        for candidate in candidates:
            if withdrawn >= deficit:
                break
            try:
                hits = wm.query_container_item(candidate, origin=pos, dim=dim)
            except Exception:
                continue
            for h in hits:
                if withdrawn >= deficit or float(h.get("dist", 999.0)) > 72.0:
                    break
                cpos = h["pos"]
                take_n = min(deficit - withdrawn, int(h.get("count", 0)))
                if take_n <= 0:
                    continue
                try:
                    if float(h.get("dist", 0.0)) > 4.2 or abs(float(pos[1]) - float(cpos[1])) >= 1.5:
                        self.skills.navigate_segmented(
                            float(cpos[0]) + 0.5,
                            float(cpos[2]) + 0.5,
                            range_m=2.8,
                            geofence=self.geofence,
                        )
                        st_chk = self.bot.status()
                        p_chk = st_chk.get("pos") or pos
                        if math.hypot(float(p_chk[0]) - (cpos[0] + 0.5), float(p_chk[2]) - (cpos[2] + 0.5)) > 4.6 or (float(cpos[1]) - float(p_chk[1])) >= 1.5:
                            try:
                                self.skills.unstick()
                                self.skills.navigate_segmented(
                                    float(cpos[0]) + 0.5,
                                    float(cpos[2]) + 0.5,
                                    range_m=2.8,
                                    geofence=self.geofence,
                                )
                            except Exception:
                                pass
                    res = self.skills.container_withdraw(
                        {candidate: take_n},
                        block=str(h.get("block") or "chest"),
                        x=int(cpos[0]),
                        y=int(cpos[1]),
                        z=int(cpos[2]),
                        dim=dim,
                    )
                    if res.get("ok"):
                        withdrawn += take_n
                        steps_log.append(
                            {"step": "chest_withdraw", "item": candidate, "count": take_n, "pos": cpos, "ok": True}
                        )
                    else:
                        # Self-heal WorldMemory on `missing_item` / stale chest snapshot!
                        ctx_dict = res.get("context") if isinstance(res.get("context"), dict) else {}
                        err_win = res.get("window") if isinstance(res.get("window"), dict) else ctx_dict.get("window")
                        err_code = str(res.get("error") or "")
                        if isinstance(err_win, dict):
                            wm.update_container_from_window(
                                err_win,
                                fallback_pos=cpos,
                                fallback_block=str(h.get("block") or "chest"),
                                dim=dim,
                            )
                        elif err_code == "missing_item":
                            ckey = (dim, int(cpos[0]), int(cpos[1]), int(cpos[2]))
                            cmeta = wm.containers.get(ckey)
                            if isinstance(cmeta, dict) and isinstance(cmeta.get("items"), dict):
                                cmeta["items"].pop(candidate, None)
                        elif err_code in {
                            "block_not_found",
                            "no_target_block",
                            "not_a_container",
                            "invalid_target",
                        }:
                            wm.containers.pop((dim, int(cpos[0]), int(cpos[1]), int(cpos[2])), None)
                        steps_log.append(
                            {
                                "step": "chest_withdraw_failed_synced",
                                "item": candidate,
                                "pos": cpos,
                                "error": res.get("error"),
                                "ok": False,
                            }
                        )
                except Exception:
                    continue
        return withdrawn

    def _ensure_pickaxe_tier(
        self,
        min_tier: int,
        *,
        deadline: float,
        depth: int,
        visited: set[str],
        steps_log: list[dict[str, Any]],
        hunt_missing: dict[str, int],
        guard_fn: Callable[[], str | None],
    ) -> str | None:
        if min_tier <= 0:
            return None
        counts = total_owned_counts(self.bot.inventory())
        if highest_pickaxe_tier(counts) >= min_tier:
            return None
        req_pick = TIER_TO_PICKAXE.get(min_tier)
        if not req_pick:
            return "unsupported_tier"
        return self._satisfy_item(
            req_pick,
            1,
            deadline=deadline,
            depth=depth + 1,
            visited=visited,
            steps_log=steps_log,
            hunt_missing=hunt_missing,
            guard_fn=guard_fn,
        )

    def _satisfy_item(
        self,
        item: str,
        target_count: int,
        *,
        deadline: float,
        depth: int,
        visited: set[str],
        steps_log: list[dict[str, Any]],
        hunt_missing: dict[str, int],
        guard_fn: Callable[[], str | None],
    ) -> str | None:
        if depth > 16:
            return "recursion_limit"
        abort = guard_fn()
        if abort is not None:
            return abort

        counts = total_owned_counts(self.bot.inventory())
        have = effective_item_count(item, counts)
        if have >= target_count:
            return None

        deficit = target_count - have

        # 0) Check active `WorldJobRegistry` furnace jobs first (collect if ready, else yield!)
        if self.world_jobs is not None and hasattr(self.world_jobs, "find_jobs_for_output"):
            try:
                st_j = self.bot.status()
                pos_j = st_j.get("pos") or [0.0, 64.0, 0.0]
                dim_j = normalize_dimension(st_j.get("dimension"))
                matching_jobs = self.world_jobs.find_jobs_for_output(item, origin=pos_j, dim=dim_j, max_dist=96.0)
                for mjob in matching_jobs:
                    rem_wait = float(mjob.ready_at) - time.monotonic()
                    if rem_wait <= 12.0:
                        self.world_jobs.collect_job(self.skills, mjob.job_id, wait_remaining_s=max(2.0, rem_wait + 3.0))
                        steps_log.append({"step": "collect_world_job", "job_id": mjob.job_id, "item": item, "ok": True})
                    else:
                        steps_log.append(
                            {
                                "step": "awaiting_world_job",
                                "job_id": mjob.job_id,
                                "item": item,
                                "remaining_s": round(rem_wait, 1),
                                "ok": True,
                            }
                        )
                        return "waiting_for_world_job"
                counts = total_owned_counts(self.bot.inventory())
                have = effective_item_count(item, counts)
                if have >= target_count:
                    return None
                deficit = target_count - have
            except Exception:
                pass

        # 1) Check storage chests in WorldMemory within 72m!
        got_chest = self._try_withdraw_from_memory_chest(item, deficit, steps_log=steps_log)
        if got_chest > 0:
            counts = total_owned_counts(self.bot.inventory())
            have = effective_item_count(item, counts)
            if have >= target_count:
                return None
            deficit = target_count - have

        if item in visited:
            return "cycle_detected"
        branch_visited = set(visited)
        branch_visited.add(item)

        # 2) Wildcard planks: craft from any owned or harvested logs
        if item == "any_planks":
            logs_needed = max(1, math.ceil(deficit / 4))
            if effective_item_count("any_log", counts) < logs_needed:
                r_log = self._satisfy_item(
                    "any_log",
                    logs_needed,
                    deadline=deadline,
                    depth=depth + 1,
                    visited=branch_visited,
                    steps_log=steps_log,
                    hunt_missing=hunt_missing,
                    guard_fn=guard_fn,
                )
                if r_log is not None:
                    return r_log
            counts_after_log = total_owned_counts(self.bot.inventory())
            log_type = next(
                (k for k, v in counts_after_log.items() if k.endswith(("_log", "_stem")) and v > 0),
                None,
            )
            if not log_type:
                return "material_unavailable"
            plank_name = log_type.replace("_log", "_planks").replace("_stem", "_planks")
            craft_plank_n = logs_needed * 4
            try:
                self.skills._safe_craft(plank_name, craft_plank_n)
                steps_log.append({"step": "craft", "item": plank_name, "count": craft_plank_n, "ok": True})
            except ApiError:
                res_cg = self.skills.craft_gear(item=plank_name, count=craft_plank_n)
                steps_log.append({"step": "craft_gear", "item": plank_name, "ok": bool(res_cg.get("ok"))})
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "craft_incomplete"

        # 3) Smelted ingots / stone / glass / charcoal
        if item in SMELT_SOURCES:
            raw_in, yield_per = SMELT_SOURCES[item]
            raw_needed = math.ceil(deficit / max(1, yield_per))
            cur_raw = counts.get("cobblestone", 0) if item == "stone" else effective_item_count(raw_in, counts)
            if cur_raw < raw_needed:
                if item == "stone":
                    r_pick_st = self._ensure_pickaxe_tier(
                        1,
                        deadline=deadline,
                        depth=depth + 1,
                        visited=branch_visited,
                        steps_log=steps_log,
                        hunt_missing=hunt_missing,
                        guard_fn=guard_fn,
                    )
                    if r_pick_st is not None:
                        return r_pick_st
                    need_cb = raw_needed - counts.get("cobblestone", 0)
                    res_cb0 = self.skills.mine_blocks(block_types=["stone", "cobblestone"], count=need_cb, radius=32)
                    steps_log.append({"step": "mine_cobblestone_for_stone", "need": need_cb, "ok": bool(res_cb0.get("ok"))})
                    if total_owned_counts(self.bot.inventory()).get("cobblestone", 0) < raw_needed:
                        return "material_unavailable"
                else:
                    r_raw = self._satisfy_item(
                        raw_in,
                        raw_needed,
                        deadline=deadline,
                        depth=depth + 1,
                        visited=branch_visited,
                        steps_log=steps_log,
                        hunt_missing=hunt_missing,
                        guard_fn=guard_fn,
                    )
                    if r_raw is not None:
                        return r_raw

            counts_mid = total_owned_counts(self.bot.inventory())
            fuel_cnt = effective_item_count("coal_or_charcoal", counts_mid)
            planks_or_logs = effective_item_count("any_planks", counts_mid) + effective_item_count("any_log", counts_mid) * 4
            want_furnaces = 2 if (self.world_jobs is not None and raw_needed >= 8) else 1
            nearby_furnaces = 0
            try:
                fb_f = self.bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": want_furnaces})
                nearby_furnaces = len((fb_f or {}).get("blocks") or [])
            except Exception:
                nearby_furnaces = 1 if self._has_nearby_station("furnace", radius=8) else 0
            need_furnace_items = max(0, want_furnaces - nearby_furnaces)
            if counts_mid.get("furnace", 0) < need_furnace_items:
                r_furnace = self._satisfy_item(
                    "furnace",
                    need_furnace_items,
                    deadline=deadline,
                    depth=depth + 1,
                    visited=branch_visited,
                    steps_log=steps_log,
                    hunt_missing=hunt_missing,
                    guard_fn=guard_fn,
                )
                if r_furnace is not None and (nearby_furnaces + total_owned_counts(self.bot.inventory()).get("furnace", 0)) < 1:
                    return r_furnace
            if fuel_cnt < math.ceil(raw_needed / 8) and planks_or_logs < max(2, raw_needed):
                r_fuel = self._satisfy_item(
                    "coal_or_charcoal",
                    max(1, math.ceil(raw_needed / 8)),
                    deadline=deadline,
                    depth=depth + 1,
                    visited=branch_visited,
                    steps_log=steps_log,
                    hunt_missing=hunt_missing,
                    guard_fn=guard_fn,
                )
                if r_fuel is not None:
                    return r_fuel

            counts_pre_smelt = total_owned_counts(self.bot.inventory())
            actual_input = raw_in
            if raw_in == "any_log":
                actual_input = next((k for k, v in counts_pre_smelt.items() if k.endswith("_log") and v > 0), "oak_log")
            elif raw_in == "cobblestone" and item == "stone" and counts_pre_smelt.get("cobblestone", 0) < raw_needed:
                top_up_cb = raw_needed - counts_pre_smelt.get("cobblestone", 0)
                res_cb = self.skills.mine_blocks(block_types=["stone", "cobblestone"], count=top_up_cb, radius=32)
                steps_log.append({"step": "mine_cobblestone_for_stone", "need": top_up_cb, "ok": bool(res_cb.get("ok"))})

            abort_s = guard_fn()
            if abort_s is not None:
                return abort_s

            # Async WorldJob path when `world_jobs` is wired and estimated burn >= threshold (default 25s -> >=3 items)!
            est_single_s = 10.0 * raw_needed + 2.0
            if self.world_jobs is not None and est_single_s >= self.async_smelt_threshold_s:
                batch_res = start_smelt_batch_nonblocking(
                    self.skills,
                    input_item=actual_input,
                    output_item=item,
                    count=raw_needed,
                    allow_multi_furnace=True,
                )
                if batch_res.get("ok"):
                    st_sm = self.bot.status()
                    dim_sm = normalize_dimension(st_sm.get("dimension"))
                    job = self.world_jobs.register_furnace_job(
                        dim=dim_sm,
                        furnaces=batch_res["furnaces"],
                        input_item=actual_input,
                        output_item=item,
                        total_count=int(batch_res["requested_count"]),
                        ready_at=float(batch_res["ready_at"]),
                        parent_target_key=self.parent_target_key,
                    )
                    steps_log.append(
                        {
                            "step": "async_smelt_started",
                            "job_id": job.job_id,
                            "input": actual_input,
                            "output": item,
                            "count": batch_res["requested_count"],
                            "furnace_count": len(batch_res["furnaces"]),
                            "estimated_wait_s": batch_res["estimated_wait_s"],
                            "ok": True,
                        }
                    )
                    return "waiting_for_world_job"

            smelt_res = self.skills.smelt_items(
                input_item=actual_input,
                count=raw_needed,
                wait_timeout_s=min(180.0, max(20.0, deadline - time.monotonic())),
            )
            steps_log.append(
                {
                    "step": "smelt",
                    "input": actual_input,
                    "output": item,
                    "count": raw_needed,
                    "ok": bool(smelt_res.get("ok")),
                    "collected": smelt_res.get("collected"),
                    "fuel": smelt_res.get("fuel"),
                    "requested_count": smelt_res.get("requested_count"),
                }
            )
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "smelt_incomplete"

        # 4) Craftable recipe in RECIPES_1_21 / EXTRA_OBJECTIVE_RECIPES
        recipe = _lookup_objective_recipe(item)
        if recipe is not None:
            yield_cnt = max(1, int(recipe.get("yield_count", 1)))
            batches = math.ceil(deficit / yield_cnt)
            ingredients: dict[str, int] = dict(recipe.get("ingredients") or {})
            for ing_name, ing_per in ingredients.items():
                counts_ing = total_owned_counts(self.bot.inventory())
                need_ing_total = int(ing_per) * batches
                if effective_item_count(ing_name, counts_ing) < need_ing_total:
                    r_ing = self._satisfy_item(
                        ing_name,
                        need_ing_total,
                        deadline=deadline,
                        depth=depth + 1,
                        visited=branch_visited,
                        steps_log=steps_log,
                        hunt_missing=hunt_missing,
                        guard_fn=guard_fn,
                    )
                    if r_ing is not None:
                        return r_ing
            if recipe.get("table_3x3"):
                counts_tb = total_owned_counts(self.bot.inventory())
                if (
                    counts_tb.get("crafting_table", 0) == 0
                    and effective_item_count("any_planks", counts_tb) < 4
                    and effective_item_count("any_log", counts_tb) < 1
                    and not self._has_nearby_station("crafting_table", radius=6)
                ):
                    r_tb = self._satisfy_item(
                        "crafting_table",
                        1,
                        deadline=deadline,
                        depth=depth + 1,
                        visited=branch_visited,
                        steps_log=steps_log,
                        hunt_missing=hunt_missing,
                        guard_fn=guard_fn,
                    )
                    if r_tb is not None:
                        return r_tb
            # Top-up pass: ingredient sub-crafts/table placement may consume planks needed for final craft
            for ing_name, ing_per in ingredients.items():
                need_ing_total = int(ing_per) * batches
                counts_top = total_owned_counts(self.bot.inventory())
                if effective_item_count(ing_name, counts_top) < need_ing_total:
                    r_top = self._satisfy_item(
                        ing_name,
                        need_ing_total,
                        deadline=deadline,
                        depth=depth + 1,
                        visited=branch_visited,
                        steps_log=steps_log,
                        hunt_missing=hunt_missing,
                        guard_fn=guard_fn,
                    )
                    if r_top is not None:
                        return r_top
            abort_c = guard_fn()
            if abort_c is not None:
                return abort_c
            craft_units = batches * yield_cnt
            cg_res = self.skills.craft_gear(item=item, count=craft_units)
            steps_log.append({"step": "craft_gear", "item": item, "count": craft_units, "ok": bool(cg_res.get("ok"))})
            if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
                return None
            return "craft_incomplete"

        # 5) Gatherable world resource (logs / stone / exposed ores / surface blocks)
        gather = GATHER_SOURCES.get(item)
        if gather is not None:
            min_tier = int(gather.get("min_tier", 0))
            r_pick = self._ensure_pickaxe_tier(
                min_tier,
                deadline=deadline,
                depth=depth,
                visited=branch_visited,
                steps_log=steps_log,
                hunt_missing=hunt_missing,
                guard_fn=guard_fn,
            )
            if r_pick is not None:
                return r_pick
            return self._gather_resource(
                item,
                target_count=target_count,
                gather_spec=gather,
                deadline=deadline,
                steps_log=steps_log,
                hunt_missing=hunt_missing,
                guard_fn=guard_fn,
            )

        # 6) Non-deterministic / mob-drop / loot-only item -> report cleanly so LLM handles world hunt
        hunt_missing[item] = max(hunt_missing.get(item, 0), deficit)
        return "requires_world_hunt"

    def _gather_resource(
        self,
        item: str,
        *,
        target_count: int,
        gather_spec: dict[str, Any],
        deadline: float,
        steps_log: list[dict[str, Any]],
        hunt_missing: dict[str, int],
        guard_fn: Callable[[], str | None],
    ) -> str | None:
        kind = str(gather_spec["kind"])

        if kind == "logs":
            st = self.bot.status()
            pos = st.get("pos") or [0.0, 64.0, 0.0]
            dim = normalize_dimension(st.get("dimension"))
            if dim == "overworld" and float(pos[1]) < 52.0:
                counts_pre_asc = total_owned_counts(self.bot.inventory())
                # Never dig blind staircases from deep underground without a pickaxe or inside `pre_ensure` (`allow_underground_dfs=False`)!
                if highest_pickaxe_tier(counts_pre_asc) <= 0 or (not self.allow_underground_dfs and float(pos[1]) < 42.0):
                    rem_logs = max(1, target_count - effective_item_count(item, counts_pre_asc))
                    hunt_missing[item] = max(hunt_missing.get(item, 0), rem_logs)
                    return "requires_world_hunt"
                for _ in range(4):
                    abort_u = guard_fn()
                    if abort_u is not None:
                        return abort_u
                    st_u = self.bot.status()
                    py_u = float((st_u.get("pos") or [0.0, 64.0, 0.0])[1])
                    if py_u >= 58.0:
                        break
                    if highest_pickaxe_tier(total_owned_counts(self.bot.inventory())) <= 0:
                        break
                    dirs = self.skills._probe_viable_up_stair_dirs(
                        math.floor(float(st_u["pos"][0])),
                        math.floor(py_u),
                        math.floor(float(st_u["pos"][2])),
                    )
                    up_dir = dirs[0] if dirs else "north"
                    up_res = self.skills.dig_staircase(direction=up_dir, steps=min(14, max(4, int(62 - py_u))), vertical="up")
                    steps_log.append({"step": "ascend_for_wood", "dir": up_dir, "ok": bool(up_res.get("ok"))})
                    if not up_res.get("ok"):
                        break
            counts0 = total_owned_counts(self.bot.inventory())
            need_logs = max(1, target_count - effective_item_count(item, counts0))
            h_res = self.skills.harvest_logs(target_logs=need_logs, radius=40)
            steps_log.append({"step": "harvest_logs", "need": need_logs, "ok": bool(h_res.get("ok"))})
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "material_unavailable"

        if kind == "blocks_or_tunnel":
            for _ in range(3):
                abort_b = guard_fn()
                if abort_b is not None:
                    return abort_b
                counts_b = total_owned_counts(self.bot.inventory())
                rem_b = target_count - effective_item_count(item, counts_b)
                if rem_b <= 0:
                    return None
                mb = self.skills.mine_blocks(block_types=list(gather_spec["blocks"]), count=min(16, rem_b), radius=28)
                steps_log.append({"step": "mine_stone", "need": rem_b, "ok": bool(mb.get("ok"))})
                if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
                    return None
                st_res = self.skills.dig_staircase(direction="north", steps=4, vertical="down", stop_on_ore=False)
                steps_log.append({"step": "stair_for_stone", "ok": bool(st_res.get("ok"))})
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "material_unavailable"

        if kind == "coal_or_charcoal":
            counts_c = total_owned_counts(self.bot.inventory())
            rem_c = target_count - effective_item_count(item, counts_c)
            if rem_c <= 0:
                return None
            mb_c = self.skills.mine_blocks(block_types=["coal_ore", "deepslate_coal_ore"], count=min(8, rem_c), radius=32)
            steps_log.append({"step": "mine_exposed_coal", "ok": bool(mb_c.get("ok"))})
            counts_c2 = total_owned_counts(self.bot.inventory())
            rem_c2 = target_count - effective_item_count(item, counts_c2)
            if rem_c2 <= 0:
                return None
            if effective_item_count("any_log", counts_c2) >= rem_c2 + 1:
                log_k = next((k for k, v in counts_c2.items() if k.endswith("_log") and v > 0), "oak_log")
                sm_c = self.skills.smelt_items(
                    input_item=log_k,
                    count=rem_c2,
                    wait_timeout_s=min(60.0, max(12.0, deadline - time.monotonic())),
                )
                steps_log.append({"step": "smelt_charcoal", "count": rem_c2, "ok": bool(sm_c.get("ok"))})
                if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
                    return None
            if not self.allow_underground_dfs:
                hunt_missing[item] = max(hunt_missing.get(item, 0), rem_c2)
                return "requires_world_hunt"
            dfs_c = self.skills.mine_dfs(
                target="coal",
                goal_count=rem_c2,
                target_y=44,
                geofence=self.geofence,
                max_duration_s=min(32.0, max(8.0, deadline - time.monotonic())),
            )
            steps_log.append({"step": "mine_dfs_coal", "ok": bool(dfs_c.get("ok"))})
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "material_unavailable"

        if kind in {"blocks_then_dfs", "coal_or_dfs"}:
            counts_o = total_owned_counts(self.bot.inventory())
            rem_o = target_count - effective_item_count(item, counts_o)
            if rem_o <= 0:
                return None
            st_o = self.bot.status()
            cur_y = float((st_o.get("pos") or [0.0, 64.0, 0.0])[1])
            if item != "diamond" or cur_y <= 16.0:
                mb_o = self.skills.mine_blocks(block_types=list(gather_spec["blocks"]), count=min(8, rem_o), radius=28)
                if mb_o.get("ok"):
                    steps_log.append({"step": f"mine_exposed_{item}", "ok": True})
            counts_after_exposed = total_owned_counts(self.bot.inventory())
            rem_after_exposed = target_count - effective_item_count(item, counts_after_exposed)
            if rem_after_exposed <= 0:
                return None

            # Hard boundary: NEVER run unguarded underground `mine_dfs` inside `pre_ensure`!
            if not self.allow_underground_dfs:
                hunt_missing[item] = max(hunt_missing.get(item, 0), rem_after_exposed)
                return "requires_world_hunt"

            while time.monotonic() < deadline:
                abort_d = guard_fn()
                if abort_d is not None:
                    return abort_d
                counts_now = total_owned_counts(self.bot.inventory())
                rem_now = target_count - effective_item_count(item, counts_now)
                if rem_now <= 0:
                    return None
                slice_s = min(34.0, max(6.0, deadline - time.monotonic()))
                dfs_res = self.skills.mine_dfs(
                    target=str(gather_spec["dfs_target"]),
                    goal_count=rem_now,
                    target_y=int(gather_spec["dfs_y"]),
                    geofence=self.geofence,
                    max_duration_s=slice_s,
                )
                sr = str(dfs_res.get("stop_reason") or "")
                steps_log.append(
                    {
                        "step": f"mine_dfs_{gather_spec['dfs_target']}",
                        "target_gained": dfs_res.get("target_gained", 0),
                        "stop_reason": sr,
                        "ok": bool(dfs_res.get("ok")),
                    }
                )
                if sr in {"pickaxe_low", "insufficient_pickaxe_tier"}:
                    counts_pk = total_owned_counts(self.bot.inventory())
                    if effective_item_count("cobblestone", counts_pk) >= 3 and (
                        effective_item_count("any_planks", counts_pk) + effective_item_count("any_log", counts_pk) * 4 >= 2
                        or counts_pk.get("stick", 0) >= 2
                    ):
                        self.skills.craft_gear(item="stone_pickaxe", count=1)
                        steps_log.append({"step": "auto_resupply_stone_pickaxe", "ok": True})
                        continue
                    return "material_unavailable"
                if sr in {"hurt", "not_ready", "geofence_blocked"}:
                    return "threat_interrupted" if sr == "hurt" else "material_unavailable"
                if sr == "shaft_exhausted":
                    self.skills.mine_dfs(action="reset", geofence=self.geofence)
            return "time_budget_slice"

        if kind == "flint_from_gravel":
            for _ in range(3):
                abort_f = guard_fn()
                if abort_f is not None:
                    return abort_f
                if effective_item_count("flint", total_owned_counts(self.bot.inventory())) >= target_count:
                    return None
                mb_g = self.skills.mine_blocks(block_types=["gravel"], count=4, radius=36)
                steps_log.append({"step": "mine_gravel_for_flint", "ok": bool(mb_g.get("ok"))})
                if not mb_g.get("ok"):
                    break
            return None if effective_item_count("flint", total_owned_counts(self.bot.inventory())) >= target_count else "requires_world_hunt"

        rem_blk = target_count - effective_item_count(item, total_owned_counts(self.bot.inventory()))
        if rem_blk <= 0:
            return None
        mb_only = self.skills.mine_blocks(block_types=list(gather_spec["blocks"]), count=min(16, rem_blk), radius=40)
        steps_log.append({"step": f"mine_{item}", "need": rem_blk, "ok": bool(mb_only.get("ok"))})
        if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
            return None
        return "requires_world_hunt"
