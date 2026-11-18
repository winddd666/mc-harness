"""Deterministic 0-LLM physical hard-prerequisite resolver (`ensure_items`).

Design boundaries (strict):
1. Encodes ONLY objective Minecraft 1.21.11 hard laws:
   - Crafting recipes (`RECIPES_1_21` + wood/stone equivalence),
   - Furnace smelting pairs (`raw_iron -> iron_ingot`, `cobblestone -> stone`, etc.),
   - Minimum pickaxe harvest tiers (`tier 1..4` for stone/iron/diamond/obsidian).
2. Zero subjective tactical gates: never forces shields, beds, armor sets, or food quotas
   unless explicitly requested by the caller.
3. Checks inventory + equipped offhand/armor FIRST, then nearby `WorldMemory` storage chests,
   before harvesting/mining/smelting/crafting.
4. Bounded wall-clock budget + between-step survival check (`check_and_stabilize`) so a long
   material chain never starves, drowns, or ignores fresh combat damage.
"""

from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Any

from ..errors import ApiError
from ..kb import RECIPES_1_21, normalize_dimension, resolve_item_alias
from .safety import check_and_stabilize

# Objective smelting input -> (raw_input_item, yield_per_input)
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

# Deterministic world-gatherable raw resources:
# item -> {"kind": "logs"|"blocks"|"dfs", "min_tier": int, ...}
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

# Extra objective vanilla recipes not always spelled out in small-model RECIPES_1_21
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
        for slot_key in ("head", "torso", "legs", "feet"):
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
            if k.endswith("_slab") and any(k.startswith(p) for p in ("oak_", "spruce_", "birch_", "jungle_", "acacia_", "dark_oak_", "cherry_", "mangrove_", "bamboo_", "crimson_", "warped_", "pale_oak_"))
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


class PhysicalPrerequisiteResolver:
    """Bounded, survival-guarded recursive physical material resolver."""

    def __init__(
        self,
        skills: Any,
        *,
        geofence: Any = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> None:
        self.skills = skills
        self.bot = skills.bot
        self.geofence = geofence
        self.stop_check = stop_check

    def _has_nearby_station(self, block_name: str, radius: int = 7) -> bool:
        try:
            found = self.bot.call("find_blocks", {"radius": radius, "types": [block_name], "limit": 1})
            blks = (found or {}).get("blocks") or []
            if blks:
                return True
            # Recall a station within 28m (either visible or in WorldMemory) and walk back to reuse it!
            found_far = self.bot.call("find_blocks", {"radius": 28, "types": [block_name], "limit": 1})
            far_blks = (found_far or {}).get("blocks") or []
            target_pos = far_blks[0].get("pos") if (far_blks and isinstance(far_blks[0], dict)) else None
            if target_pos is None:
                st = self.bot.status()
                pos = st.get("pos")
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    wm = self.skills._get_world_memory()
                    mem_hits = wm.query_pois([block_name], origin=pos, dim=normalize_dimension(st.get("dimension")), limit=1)
                    if mem_hits and float(mem_hits[0].get("dist", 999.0)) <= 28.0:
                        target_pos = mem_hits[0]["pos"]
            if isinstance(target_pos, list) and len(target_pos) >= 3:
                self.bot.call("move_to", {"x": float(target_pos[0]) + 0.5, "z": float(target_pos[2]) + 0.5, "range": 2.5})
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

        st0 = self.bot.status()
        hp_floor = float(st0.get("health") or 20.0)

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

            # Fresh combat hit with close hostile -> hand control back immediately
            if hp < hp_floor - 1.5 and nh_dist <= 12.0:
                return "threat_interrupted"

            if st.get("in_lava") or st.get("in_water") or hp <= 12.0 or food <= 12:
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
                if err_reason in {"interrupted", "time_budget_slice", "threat_interrupted", "not_ready"}:
                    break

        # Optional deterministic equip at the end (e.g., diamond_boots -> feet, shield -> off-hand)
        if equip_slots:
            inv_eq = self.bot.inventory()
            owned_eq = total_owned_counts(inv_eq)
            for eq_item, slot_name in equip_slots.items():
                canon = resolve_item_alias(eq_item)
                if owned_eq.get(canon, 0) > 0:
                    try:
                        self.bot.call("equip", {"item": canon, "slot": slot_name})
                        steps_log.append({"step": "equip", "item": canon, "slot": slot_name, "ok": True})
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
            return 0
        try:
            st = self.bot.status()
            pos = st.get("pos")
            if not isinstance(pos, (list, tuple)) or len(pos) < 3:
                return 0
            dim = normalize_dimension(st.get("dimension"))
            wm = self.skills._get_world_memory()
            hits = wm.query_container_item(item, origin=pos, dim=dim)
        except Exception:
            return 0

        withdrawn = 0
        for h in hits:
            if withdrawn >= deficit or float(h.get("dist", 999.0)) > 72.0:
                break
            cpos = h["pos"]
            take_n = min(deficit - withdrawn, int(h.get("count", 0)))
            if take_n <= 0:
                continue
            try:
                if float(h.get("dist", 0.0)) > 4.5:
                    self.skills.navigate_segmented(
                        float(cpos[0]) + 0.5,
                        float(cpos[2]) + 0.5,
                        range_m=3.0,
                        geofence=self.geofence,
                    )
                res = self.skills.container_withdraw(
                    {item: take_n},
                    block=str(h.get("block") or "chest"),
                    x=int(cpos[0]),
                    y=int(cpos[1]),
                    z=int(cpos[2]),
                    dim=dim,
                )
                if res.get("ok"):
                    withdrawn += take_n
                    steps_log.append({"step": "chest_withdraw", "item": item, "count": take_n, "pos": cpos, "ok": True})
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
        # 1) Check storage chests in WorldMemory first!
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
            r_log = self._satisfy_item(
                "any_log",
                effective_item_count("any_log", counts) + logs_needed,
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
            want_total_plank = counts_after_log.get(plank_name, 0) + logs_needed * 4
            try:
                self.bot.call("craft", {"item": plank_name, "count": want_total_plank})
                steps_log.append({"step": "craft", "item": plank_name, "count": logs_needed * 4, "ok": True})
            except ApiError:
                res_cg = self.skills.craft_gear(item=plank_name, count=logs_needed * 4)
                steps_log.append({"step": "craft_gear", "item": plank_name, "ok": bool(res_cg.get("ok"))})
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "craft_incomplete"

        # 3) Smelted ingots / stone / glass / charcoal
        if item in SMELT_SOURCES:
            raw_in, yield_per = SMELT_SOURCES[item]
            raw_needed = math.ceil(deficit / max(1, yield_per))
            cur_raw = effective_item_count(raw_in, counts)
            if cur_raw < raw_needed:
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
            # Ensure furnace stone (8 cobblestone) only if NO furnace in inventory AND NO furnace nearby on ground
            if (
                counts_mid.get("furnace", 0) == 0
                and effective_item_count("cobblestone", counts_mid) < 8
                and not self._has_nearby_station("furnace", radius=8)
            ):
                r_fstone = self._satisfy_item(
                    "cobblestone",
                    8,
                    deadline=deadline,
                    depth=depth + 1,
                    visited=branch_visited,
                    steps_log=steps_log,
                    hunt_missing=hunt_missing,
                    guard_fn=guard_fn,
                )
                if r_fstone is not None:
                    return r_fstone
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
            elif raw_in == "cobblestone" and counts_pre_smelt.get("cobblestone", 0) < raw_needed:
                # Vanilla stone smelting specifically requires cobblestone (not cobbled_deepslate)
                if item == "stone" and counts_pre_smelt.get("cobblestone", 0) < raw_needed:
                    res_cb = self.skills.mine_blocks(block_types=["stone", "cobblestone"], count=raw_needed, radius=32)
                    steps_log.append({"step": "mine_cobblestone_for_stone", "ok": bool(res_cb.get("ok"))})
            abort_s = guard_fn()
            if abort_s is not None:
                return abort_s
            smelt_res = self.skills.smelt_items(
                input_item=actual_input,
                count=raw_needed,
                wait_timeout_s=min(95.0, max(15.0, deadline - time.monotonic())),
            )
            steps_log.append({"step": "smelt", "input": actual_input, "output": item, "count": raw_needed, "ok": bool(smelt_res.get("ok"))})
            return None if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count else "smelt_incomplete"

        # 4) Craftable recipe in RECIPES_1_21 / EXTRA_OBJECTIVE_RECIPES
        recipe = _lookup_objective_recipe(item)
        if recipe is not None:
            yield_cnt = max(1, int(recipe.get("yield_count", 1)))
            batches = math.ceil(deficit / yield_cnt)
            ingredients: dict[str, int] = dict(recipe.get("ingredients") or {})
            for ing_name, ing_per in ingredients.items():
                counts_ing = total_owned_counts(self.bot.inventory())
                # If `craft_gear` can synthesize `stick` / `any_planks` directly from owned logs, still satisfy recursively so wood is harvested if short
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
            abort_c = guard_fn()
            if abort_c is not None:
                return abort_c
            craft_units = batches * yield_cnt
            cg_res = self.skills.craft_gear(item=item, count=craft_units)
            steps_log.append({"step": "craft_gear", "item": item, "count": craft_units, "ok": bool(cg_res.get("ok"))})
            if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
                return None
            return "craft_incomplete"

        # 5) Gatherable world resource (logs / stone / ores / surface blocks)
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
        guard_fn: Callable[[], str | None],
    ) -> str | None:
        kind = str(gather_spec["kind"])

        if kind == "logs":
            st = self.bot.status()
            pos = st.get("pos") or [0.0, 64.0, 0.0]
            dim = normalize_dimension(st.get("dimension"))
            # If underground in overworld, climb up before trying to harvest trees!
            if dim == "overworld" and float(pos[1]) < 52.0:
                for _ in range(4):
                    abort_u = guard_fn()
                    if abort_u is not None:
                        return abort_u
                    st_u = self.bot.status()
                    py_u = float((st_u.get("pos") or [0.0, 64.0, 0.0])[1])
                    if py_u >= 58.0:
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
                # Fallback if surface grass covers all stone: dig a short 4-step staircase down
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
            # Burn logs into charcoal if trees/logs are readily available
            if effective_item_count("any_log", counts_c2) >= rem_c2 + 1:
                log_k = next((k for k, v in counts_c2.items() if k.endswith("_log") and v > 0), "oak_log")
                sm_c = self.skills.smelt_items(input_item=log_k, count=rem_c2, wait_timeout_s=min(60.0, max(12.0, deadline - time.monotonic())))
                steps_log.append({"step": "smelt_charcoal", "count": rem_c2, "ok": bool(sm_c.get("ok"))})
                if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
                    return None
            # Otherwise run one bounded fishbone_scan slice for coal
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
            # Quick check for exposed ore nearby (only when standing near a plausible Y band for diamond)
            st_o = self.bot.status()
            cur_y = float((st_o.get("pos") or [0.0, 64.0, 0.0])[1])
            if item != "diamond" or cur_y <= 16.0:
                mb_o = self.skills.mine_blocks(block_types=list(gather_spec["blocks"]), count=min(8, rem_o), radius=28)
                if mb_o.get("ok"):
                    steps_log.append({"step": f"mine_exposed_{item}", "ok": True})
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
                    # Auto-resupply burner stone pickaxe if materials allow, else stop cleanly
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

        # blocks_only (obsidian / sand / sugar_cane / pumpkin / clay)
        rem_blk = target_count - effective_item_count(item, total_owned_counts(self.bot.inventory()))
        if rem_blk <= 0:
            return None
        mb_only = self.skills.mine_blocks(block_types=list(gather_spec["blocks"]), count=min(16, rem_blk), radius=40)
        steps_log.append({"step": f"mine_{item}", "need": rem_blk, "ok": bool(mb_only.get("ok"))})
        if effective_item_count(item, total_owned_counts(self.bot.inventory())) >= target_count:
            return None
        return "requires_world_hunt"
