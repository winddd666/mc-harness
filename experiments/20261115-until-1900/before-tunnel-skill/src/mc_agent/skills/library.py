"""L1 deterministic macro skills: spawn-protection blacklist, tree chopping, crafting, tiered mining, smelting, bed spawn."""

from __future__ import annotations

import math
import time
from typing import Any

from ..bot import BotSession
from ..errors import ApiError
from ..kb import resolve_item_alias
from .safety import check_and_stabilize

LOG_TYPES: tuple[str, ...] = (
    "oak_log",
    "birch_log",
    "spruce_log",
    "dark_oak_log",
    "acacia_log",
    "jungle_log",
    "cherry_log",
    "mangrove_log",
    "pale_oak_log",
)

AXE_PRIORITY: tuple[str, ...] = (
    "netherite_axe",
    "diamond_axe",
    "iron_axe",
    "stone_axe",
    "wooden_axe",
    "golden_axe",
)

PICKAXE_TIERS: dict[str, int] = {
    "wooden_pickaxe": 1,
    "golden_pickaxe": 1,
    "stone_pickaxe": 2,
    "iron_pickaxe": 3,
    "diamond_pickaxe": 4,
    "netherite_pickaxe": 5,
}

BLOCK_MIN_PICKAXE_TIER: dict[str, int] = {
    "stone": 1,
    "cobblestone": 1,
    "deepslate": 1,
    "cobbled_deepslate": 1,
    "andesite": 1,
    "diorite": 1,
    "granite": 1,
    "coal_ore": 1,
    "deepslate_coal_ore": 1,
    "nether_quartz_ore": 1,
    "iron_ore": 2,
    "deepslate_iron_ore": 2,
    "copper_ore": 2,
    "deepslate_copper_ore": 2,
    "lapis_ore": 2,
    "deepslate_lapis_ore": 2,
    "gold_ore": 3,
    "deepslate_gold_ore": 3,
    "redstone_ore": 3,
    "deepslate_redstone_ore": 3,
    "diamond_ore": 3,
    "deepslate_diamond_ore": 3,
    "emerald_ore": 3,
    "deepslate_emerald_ore": 3,
    "obsidian": 4,
}

# Recipes requiring a 3x3 crafting table.
TABLE_RECIPES: set[str] = {
    "wooden_pickaxe",
    "wooden_axe",
    "wooden_sword",
    "wooden_shovel",
    "wooden_hoe",
    "stone_pickaxe",
    "stone_axe",
    "stone_sword",
    "stone_shovel",
    "stone_hoe",
    "iron_pickaxe",
    "iron_axe",
    "iron_sword",
    "iron_shovel",
    "iron_hoe",
    "diamond_pickaxe",
    "diamond_axe",
    "diamond_sword",
    "furnace",
    "chest",
    "shield",
    "bucket",
    "iron_helmet",
    "iron_chestplate",
    "iron_leggings",
    "iron_boots",
    "bow",
    "fishing_rod",
    "shears",
    "flint_and_steel",
    "stonecutter",
    "blast_furnace",
    "smoker",
    "campfire",
    "oak_boat",
    "birch_boat",
    "spruce_boat",
    "white_bed",
}

DEFAULT_EQUIP_SLOTS: dict[str, str] = {
    "shield": "off-hand",
    "iron_helmet": "head",
    "iron_chestplate": "torso",
    "iron_leggings": "legs",
    "iron_boots": "feet",
    "wooden_pickaxe": "hand",
    "stone_pickaxe": "hand",
    "iron_pickaxe": "hand",
    "diamond_pickaxe": "hand",
    "wooden_sword": "hand",
    "stone_sword": "hand",
    "iron_sword": "hand",
    "wooden_axe": "hand",
    "stone_axe": "hand",
    "iron_axe": "hand",
    "wooden_hoe": "hand",
    "stone_hoe": "hand",
    "iron_hoe": "hand",
}

# Material requirements (planks_needed, sticks_needed) for plank/stick auto-crafting helpers.
TOOL_WOOD_NEEDS: dict[str, tuple[int, int]] = {
    "crafting_table": (4, 0),
    "chest": (8, 0),
    "wooden_pickaxe": (3, 2),
    "wooden_axe": (3, 2),
    "wooden_sword": (2, 1),
    "wooden_shovel": (1, 2),
    "wooden_hoe": (2, 2),
    "stone_pickaxe": (0, 2),
    "stone_axe": (0, 2),
    "stone_sword": (0, 1),
    "stone_shovel": (0, 2),
    "stone_hoe": (0, 2),
    "iron_pickaxe": (0, 2),
    "iron_axe": (0, 2),
    "iron_sword": (0, 1),
    "iron_shovel": (0, 2),
    "iron_hoe": (0, 2),
    "diamond_pickaxe": (0, 2),
    "diamond_axe": (0, 2),
    "diamond_sword": (0, 1),
    "shield": (6, 0),
    "oak_boat": (5, 0),
    "campfire": (0, 3),
    "torch": (0, 1),
}


def _inventory_counts(bot: BotSession) -> dict[str, int]:
    inv = bot.inventory()
    counts: dict[str, int] = {}
    for row in inv.get("items") or []:
        name = row.get("name")
        cnt = int(row.get("count") or 0)
        if name and cnt > 0:
            counts[name] = counts.get(name, 0) + cnt
    for eq in (inv.get("armor") or {}).values():
        if isinstance(eq, dict) and eq.get("name"):
            counts[eq["name"]] = counts.get(eq["name"], 0) + int(eq.get("count") or 1)
    offhand = inv.get("offhand")
    if isinstance(offhand, dict) and offhand.get("name"):
        counts[offhand["name"]] = counts.get(offhand["name"], 0) + int(offhand.get("count") or 1)
    return counts


def _log_to_planks_name(log_name: str) -> str:
    return log_name.removesuffix("_log") + "_planks"


def _total_planks(counts: dict[str, int]) -> int:
    return sum(v for k, v in counts.items() if k.endswith("_planks"))


def _best_pickaxe_in_inv(counts: dict[str, int]) -> tuple[str | None, int]:
    best_name: str | None = None
    best_tier = 0
    for name, tier in PICKAXE_TIERS.items():
        if counts.get(name, 0) > 0 and tier > best_tier:
            best_name = name
            best_tier = tier
    return best_name, best_tier


def _is_protected_err(err: ApiError) -> bool:
    return err.code == "protected_region"


_TRANSIENT_BLOCK_ERRORS = (
    "no_path",
    "path_not_found",
    "timeout",
    "unreachable",
    "too_far",
    "out_of_reach",
    "not_loaded",
    "dig_failed",
    "stalled",
    "dig_unconfirmed",
)


def _positive_inv_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in after if after[k] > before.get(k, 0)}


def _yaw_toward(px: float, pz: float, tx: float, tz: float) -> float:
    """Compute Mineflayer yaw (rad, forward=(-sin, -cos)) pointing toward (tx, tz)."""
    return math.atan2(-(tx - px), -(tz - pz))


class MacroSkills:
    """Stateful skills with permanent protection and separate expiring failure records."""

    def __init__(self, bot: BotSession) -> None:
        self.bot = bot
        self.protected_positions: set[tuple[int, int, int]] = set()
        self.temporary_avoidance: dict[tuple[int, int, int], dict[str, Any]] = {}

    def _avoid_temporarily(self, pos: tuple[int, int, int], reason: str, seconds: float = 15.0) -> None:
        self.temporary_avoidance[pos] = {"reason": reason, "until": time.monotonic() + seconds}

    def _is_block_avoided(self, pos: tuple[int, int, int]) -> bool:
        record = self.temporary_avoidance.get(pos)
        if record and record["until"] <= time.monotonic():
            del self.temporary_avoidance[pos]
            record = None
        return pos in self.protected_positions or record is not None

    def _avoidance_summary(self) -> dict[str, Any]:
        for pos in list(self.temporary_avoidance):
            self._is_block_avoided(pos)
        return {
            "protected_blacklisted": len(self.protected_positions),
            "temporarily_avoided": len(self.temporary_avoidance),
            "temporary_reasons": sorted({r["reason"] for r in self.temporary_avoidance.values()}),
        }

    def _block_info(self, pos_xyz: list[int]) -> tuple[str, bool]:
        res = self.bot.call("get_block", {"pos": pos_xyz})
        raw = res.get("name") or res.get("block") or "air"
        return str(raw).removeprefix("minecraft:"), bool(res.get("diggable", False))

    def _is_water_covered(self, pos: tuple[int, int, int]) -> bool:
        water_names = {"water", "flowing_water"}
        for dy in (1, 2):
            try:
                name, _ = self._block_info([pos[0], pos[1] + dy, pos[2]])
            except ApiError:
                return False
            if name in water_names:
                return True
        return False

    def _equip_if_present(self, candidates: tuple[str, ...], slot: str = "hand") -> str | None:
        counts = _inventory_counts(self.bot)
        for item in candidates:
            if counts.get(item, 0) > 0:
                try:
                    self.bot.call("equip", {"item": item, "slot": slot})
                    return item
                except ApiError:
                    return None
        return None

    def _ensure_planks_and_sticks(self, planks_needed: int, sticks_needed: int) -> dict[str, int]:
        counts = _inventory_counts(self.bot)
        # Convert sticks shortage -> additional planks needed (2 planks -> 4 sticks)
        cur_sticks = counts.get("stick", 0)
        extra_stick_crafts = math.ceil(max(0, sticks_needed - cur_sticks) / 4)
        total_planks_needed = planks_needed + extra_stick_crafts * 2

        cur_planks = _total_planks(counts)
        if cur_planks < total_planks_needed:
            logs_to_craft = math.ceil((total_planks_needed - cur_planks) / 4)
            for log_name in LOG_TYPES:
                avail_logs = counts.get(log_name, 0)
                if avail_logs <= 0 or logs_to_craft <= 0:
                    continue
                use_logs = min(avail_logs, logs_to_craft)
                plank_name = _log_to_planks_name(log_name)
                try:
                    self.bot.call("craft", {"item": plank_name, "count": use_logs * 4})
                    logs_to_craft -= use_logs
                except ApiError:
                    pass
            counts = _inventory_counts(self.bot)

        if sticks_needed > counts.get("stick", 0) and _total_planks(counts) >= 2:
            need_crafts = math.ceil((sticks_needed - counts.get("stick", 0)) / 4)
            try:
                self.bot.call("craft", {"item": "stick", "count": need_crafts * 4})
            except ApiError:
                pass
            counts = _inventory_counts(self.bot)

        return counts

    def _carve_and_place_table(
        self,
        px: int,
        py: int,
        pz: int,
        item: str = "crafting_table",
    ) -> dict[str, Any] | None:
        """Carve a 1-block wall pocket over a solid floor and place a utility block there."""
        air_set = {"air", "cave_air", "void_air"}
        fluid_or_bedrock = {"water", "lava", "flowing_water", "flowing_lava", "bedrock"}
        non_solid = air_set | fluid_or_bedrock | {"short_grass", "tall_grass", "leaf_litter"}

        def _blk_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            floor_pos = [px + dx, py - 1, pz + dz]
            wall_pos = [px + dx, py, pz + dz]
            try:
                if _blk_name(floor_pos) in non_solid:
                    continue
                w_name = _blk_name(wall_pos)
                if w_name in fluid_or_bedrock:
                    continue
                if w_name not in air_set:
                    if w_name in BLOCK_MIN_PICKAXE_TIER or "stone" in w_name or "deepslate" in w_name:
                        self._equip_if_present(tuple(PICKAXE_TIERS))
                    self.bot.call("dig", {"pos": wall_pos, "collect": True, "stop_on": ["death"]})
                placed = self.bot.call("place", {"item": item, "pos": wall_pos})
                return {"ok": True, "reused": False, "carved_pocket": wall_pos, **placed}
            except ApiError:
                continue
        return None

    def ensure_crafting_table(self, *, force_recarve: bool = False) -> dict[str, Any]:
        """Reuse a nearby same-level crafting_table or craft & place one (carving a wall pocket if needed)."""
        st = self.bot.status()
        px, py, pz = [int(math.floor(v)) for v in (st.get("pos") or [0, 64, 0])]

        if not force_recarve:
            found = self.bot.call("find_blocks", {"radius": 8, "types": ["crafting_table"], "limit": 5})
            blocks = [
                b
                for b in (found.get("blocks") or [])
                if b.get("pos")
                and abs(int(b["pos"][1]) - py) <= 2
                and float(b.get("dist", 99)) <= 4.5
            ]
            if blocks:
                nearest = min(blocks, key=lambda b: float(b.get("dist", 999)))
                pos = nearest.get("pos")
                if float(nearest.get("dist", 99)) > 3.2 and pos:
                    self.bot.call("move_to", {"x": pos[0], "y": pos[1], "z": pos[2], "range": 2.0})
                return {"ok": True, "reused": True, "pos": pos}

        counts = _inventory_counts(self.bot)
        if counts.get("crafting_table", 0) < 1:
            counts = self._ensure_planks_and_sticks(4, 0)
            if _total_planks(counts) < 4:
                return {
                    "ok": False,
                    "error": "missing_ingredients",
                    "message": "合成工作台需要至少 4 块木板或 1 块原木。",
                }
            self.bot.call("craft", {"item": "crafting_table", "count": 1})

        # Place nearby using auto-placement first, then fallback open offsets, then wall-pocket carving
        try:
            placed = self.bot.call("place", {"item": "crafting_table"})
            return {"ok": True, "reused": False, **placed}
        except ApiError as err:
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 1, 0)):
                try:
                    placed = self.bot.call("place", {"item": "crafting_table", "pos": [px + dx, py + dy, pz + dz]})
                    return {"ok": True, "reused": False, **placed}
                except ApiError:
                    continue
            pocket = self._carve_and_place_table(px, py, pz)
            if pocket is not None:
                return pocket
            return {"ok": False, "error": err.code, "message": err.message}

    def _collect_nearby_items(
        self,
        *,
        bot_y: float,
        radius: int = 8,
        max_pickups: int = 5,
        fallback_coord: bool = True,
    ) -> dict[str, Any]:
        """Record bounded approach attempts, without claiming they prove item pickup."""
        attempts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            ents = self.bot.call("get_entities", {"radius": radius, "types": ["item"]})
        except ApiError as err:
            return {"attempts": [], "skipped": [], "error": {"code": err.code, "message": err.message, **err.context}}
        for ent in (ents.get("entities") or [])[:max_pickups]:
            pos = ent.get("pos") or []
            row: dict[str, Any] = {"id": ent.get("id"), "item": ent.get("item"), "item_pos": pos}
            if len(pos) < 3 or ent.get("id") is None:
                skipped.append({**row, "reason": "invalid_entity"})
                continue
            if abs(float(pos[1]) - bot_y) > 4.5:
                skipped.append({**row, "reason": "height_difference", "height_difference": float(pos[1]) - bot_y})
                continue
            attempts.append(row)
            mpos: list[float] = []
            try:
                mv = self.bot.call("move_to", {"entity_id": ent["id"], "range": 0.8, "timeout": 4000})
                mpos = mv.get("pos") or []
                row["pos"] = mpos
            except ApiError as err:
                row["approach_error"] = {"code": err.code, "message": err.message, **err.context}
                if err.code in ("interrupted", "dead", "disconnected"):
                    return {"attempts": attempts, "skipped": skipped, "error": row["approach_error"]}
            # The current API clamps entity goals to >=2 blocks. A successful
            # move may still be outside pickup reach, including vertically.
            needs_closer = len(mpos) < 3 or math.dist(mpos[:3], pos[:3]) > 1.0
            if fallback_coord and needs_closer and abs(float(pos[1]) - bot_y) <= 2.5:
                try:
                    mv = self.bot.call("move_to", {"x": float(pos[0]), "y": float(pos[1]), "z": float(pos[2]),
                                                    "range": 0.3, "timeout": 4000})
                    row["pos"] = mv.get("pos") or []
                except ApiError as err:
                    row["closer_error"] = {"code": err.code, "message": err.message, **err.context}
                    if err.code in ("interrupted", "dead", "disconnected"):
                        return {"attempts": attempts, "skipped": skipped, "error": row["closer_error"]}
            if row.get("pos"):
                row["distance_after"] = round(math.dist(row["pos"][:3], pos[:3]), 2)
                time.sleep(0.25)  # Allow server inventory packets to arrive before verification.
        return {"attempts": attempts, "skipped": skipped}

    def collect_drops(self, radius: int = 12, max_pickups: int = 8) -> dict[str, Any]:
        """Collect nearby ground item entities and report actual inventory delta."""
        inv_before = _inventory_counts(self.bot)
        st = self.bot.status()
        bot_y = float((st.get("pos") or [0, 64, 0])[1])
        approaches = self._collect_nearby_items(
            bot_y=bot_y,
            radius=max(2, min(32, radius)),
            max_pickups=max(1, min(20, max_pickups)),
            fallback_coord=True,
        )
        inv_after = _inventory_counts(self.bot)
        gained = _positive_inv_delta(inv_before, inv_after)
        state_after = self.bot.status()
        attempts = approaches["attempts"]
        out: dict[str, Any] = {
            "ok": bool(gained) and not approaches.get("error"),
            "attempted_pickups": len(attempts),
            "attempts": attempts,
            "skipped": approaches["skipped"],
            "moved_to": [row for row in attempts if row.get("pos")],
            "gained": gained,
            "inventory": inv_after,
            "pos_after": state_after.get("pos"),
            "health": state_after.get("health"),
        }
        if approaches.get("error"):
            out["error"] = approaches["error"]["code"]
            out["details"] = approaches["error"]
        elif not gained:
            out["error"] = "no_drops_collected" if attempts else ("drops_unreachable" if approaches["skipped"] else "no_drops_found")
            out["message"] = "未确认任何物品入包；走近成功不代表拾取成功。请检查高差、障碍与实际背包，改变站位或路线后再试，勿据此继续合成。"
        return out

    def _dig_target_blocks(
        self,
        *,
        block_types: list[str],
        count: int,
        radius: int,
        protected_msg: str,
        below_tol: float,
        above_tol: float,
        inv_before: dict[str, int] | None = None,
        avoid_water: bool = False,
    ) -> dict[str, Any]:
        """Shared candidate scan, vertical reachability ranking, blacklist handling, and drop rescue."""
        gained: dict[str, int] = {}
        dug_count = 0
        protected_hits = 0
        water_skipped = 0

        for _ in range(count * 3):
            if dug_count >= count:
                break
            found = self.bot.call(
                "find_blocks",
                {"radius": max(4, min(64, radius)), "types": block_types, "limit": 30},
            )
            candidates = [
                b
                for b in (found.get("blocks") or [])
                if not self._is_block_avoided(tuple(b.get("pos") or ()))
            ]
            if not candidates:
                break
            st = self.bot.status()
            bot_y = float((st.get("pos") or [0, 64, 0])[1])
            candidates.sort(
                key=lambda b: (
                    (
                        max(0.0, (bot_y - below_tol) - float(b["pos"][1]))
                        + max(0.0, float(b["pos"][1]) - (bot_y + above_tol))
                    )
                    * 4.5
                    + float(b.get("dist", 99))
                )
            )
            target = None
            for cand in candidates:
                cpos = tuple(cand["pos"])
                if avoid_water and float(cand["pos"][1]) < bot_y - 0.5 and self._is_water_covered(cpos):
                    self._avoid_temporarily(cpos, "water_covered", 30.0)
                    water_skipped += 1
                    continue
                target = cand
                break
            if target is None:
                continue
            pos = tuple(target["pos"])
            try:
                res = self.bot.call("dig", {"pos": list(pos), "collect": True})
                dug_count += 1
                for k, v in (res.get("gained") or {}).items():
                    gained[k] = gained.get(k, 0) + int(v)
                if avoid_water and bool(self.bot.status().get("in_water")):
                    esc = self.escape_water()
                    if esc.get("in_water"):
                        break
                # Guard against repeatedly digging blocks whose drops fall out of reach
                if dug_count >= 2 and sum(gained.values()) == 0:
                    self._collect_nearby_items(bot_y=bot_y)
                    if inv_before is not None:
                        inv_delta = _positive_inv_delta(inv_before, _inventory_counts(self.bot))
                        if inv_delta:
                            gained = dict(inv_delta)
                            continue
                    break
            except ApiError as err:
                if _is_protected_err(err):
                    self.protected_positions.add(pos)
                    protected_hits += 1
                    if protected_hits >= 3 and dug_count == 0:
                        return {
                            "ok": False,
                            "error": "protected_region",
                            "message": protected_msg,
                            **self._avoidance_summary(),
                        }
                elif err.code in _TRANSIENT_BLOCK_ERRORS:
                    self._avoid_temporarily(pos, err.code)
                elif err.code == "interrupted":
                    self._avoid_temporarily(pos, err.code, 3.0)
                    esc = None
                    if bool(self.bot.status().get("in_water")):
                        esc = self.escape_water()
                    out_err: dict[str, Any] = {
                        "ok": False,
                        "requires_replan": True,
                        "dug_count": dug_count,
                        "gained": gained,
                        "partial_error": {"code": err.code, "message": err.message, **err.context},
                        **self._avoidance_summary(),
                    }
                    if esc is not None:
                        out_err["water_escape"] = esc
                    return out_err
                else:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "dug_count": dug_count,
                        "gained": gained,
                        "partial_error": {"code": err.code, "message": err.message, **err.context},
                    }

        if dug_count > 0 and sum(gained.values()) == 0:
            st = self.bot.status()
            bot_y = float((st.get("pos") or [0, 64, 0])[1])
            self._collect_nearby_items(bot_y=bot_y)
            if inv_before is not None:
                inv_delta = _positive_inv_delta(inv_before, _inventory_counts(self.bot))
                if inv_delta:
                    gained = dict(inv_delta)

        if dug_count == 0 and water_skipped > 0:
            return {
                "ok": False,
                "error": "underwater_targets_skipped",
                "message": (
                    f"周围扫描到的 {water_skipped} 处目标均位于水下，已暂时跳过 30 秒；"
                    "请远离当前水域并在干燥地面用 mc_skill_staircase(vertical='down', stop_on_ore=True) 下矿或寻找旱地露头矿。"
                ),
                "dug_count": 0,
                "gained": gained,
                "water_skipped": water_skipped,
                **self._avoidance_summary(),
            }

        out: dict[str, Any] = {
            "ok": dug_count >= count and bool(gained),
            "requested_count": count,
            "dug_count": dug_count,
            "gained": gained,
            "water_skipped": water_skipped,
            **self._avoidance_summary(),
        }
        if dug_count > 0 and not gained:
            out["error"] = "no_drops_collected"
            out["message"] = (
                f"连续挖掘 {dug_count} 格但掉落物均未拾取（可能掉入深坑或工具等级不匹配），"
                "已停止继续损耗耐久，请移动到安全平层/斜坡挖掘或用 mc_get_entities 检查地面物品。"
            )
        elif dug_count < count:
            out["error"] = "partial_collection" if dug_count else "no_targets_available"
            out["message"] = f"本次只挖掘 {dug_count}/{count} 格；请按 gained 检查实际入包量。临时避让与保护区分别报告，改变站位或等待冷却后可重试临时失败。"
            out["requires_replan"] = True
        return out

    def harvest_logs(self, target_logs: int = 4, radius: int = 32) -> dict[str, Any]:
        """Chop up to target_logs trees while blacklisting protected-region blocks."""
        target_logs = max(1, min(32, target_logs))
        counts = _inventory_counts(self.bot)
        equipped = self._equip_if_present(AXE_PRIORITY)
        res = self._dig_target_blocks(
            block_types=list(LOG_TYPES),
            count=target_logs,
            radius=radius,
            protected_msg="周围原木被服务端明确拒绝为 protected_region，请离开该保护区域后再砍树。",
            below_tol=1.5,
            above_tol=3.5,
            inv_before=counts,
        )
        if res.get("error") == "protected_region":
            return res
        return {
            **res,
            "equipped_axe": equipped,
        }

    def craft_gear(
        self,
        item: str,
        count: int = 1,
        equip_slot: str | None = None,
    ) -> dict[str, Any]:
        """Auto-craft intermediate planks/sticks, ensure crafting_table when needed, craft item, and optionally equip."""
        item = resolve_item_alias(item)
        count = max(1, min(64, count))
        planks_per, sticks_per = TOOL_WOOD_NEEDS.get(item, (0, 0))
        sticks_needed = math.ceil(count / 4) if item == "torch" else sticks_per * count
        extra_table_planks = 4 if item in TABLE_RECIPES else 0
        # Only pre-craft sticks/planks up front if we might need them; let server check exact 3x3 items.
        self._ensure_planks_and_sticks(planks_per * count, sticks_needed)

        if item in TABLE_RECIPES:
            table_res = self.ensure_crafting_table()
            if not table_res.get("ok"):
                return table_res
            # Re-ensure planks if crafting table consumed some
            if extra_table_planks and planks_per > 0:
                self._ensure_planks_and_sticks(planks_per * count, sticks_needed)

        try:
            craft_res = self.bot.call("craft", {"item": item, "count": count})
        except ApiError as err:
            if item in TABLE_RECIPES and "windowOpen" in err.message:
                try:
                    nearby_tables = self.bot.call(
                        "find_blocks",
                        {"radius": 5, "types": ["crafting_table"], "limit": 4},
                    ).get("blocks", [])
                    for blk in sorted(
                        nearby_tables,
                        key=lambda b: float(b.get("dist", 99.0)),
                    )[:2]:
                        bpos = blk.get("pos")
                        if isinstance(bpos, list) and len(bpos) == 3:
                            try:
                                self.bot.call("dig", {"pos": bpos, "stop_on": ["death"]})
                            except ApiError:
                                pass
                    if _inventory_counts(self.bot).get("crafting_table", 0) < 1:
                        self._ensure_planks_and_sticks(4, 0)
                        self.bot.call("craft", {"item": "crafting_table", "count": 1})
                    pos = self.bot.status()["pos"]
                    px, py, pz = math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2])
                    self._carve_and_place_table(px, py, pz)
                    craft_res = self.bot.call("craft", {"item": item, "count": count})
                except ApiError as retry_err:
                    return {
                        "ok": False,
                        "error": retry_err.code,
                        "message": retry_err.message,
                        **retry_err.context,
                    }
            else:
                return {
                    "ok": False,
                    "error": err.code,
                    "message": err.message,
                    **err.context,
                }

        slot = equip_slot or DEFAULT_EQUIP_SLOTS.get(item)
        equipped_res = None
        if slot:
            try:
                equipped_res = self.bot.call("equip", {"item": item, "slot": slot})
            except ApiError:
                equipped_res = None

        return {
            "ok": True,
            **craft_res,
            "equipped_slot": slot if equipped_res else None,
        }

    def mine_blocks(
        self,
        block_types: list[str],
        count: int = 3,
        radius: int = 32,
    ) -> dict[str, Any]:
        """Validate pickaxe tier against Minecraft harvest rules, equip best pickaxe, and dig count blocks."""
        if not block_types:
            return {"ok": False, "error": "bad_args", "message": "block_types 不能为空。"}
        count = max(1, min(32, count))
        req_tier = max((BLOCK_MIN_PICKAXE_TIER.get(b, 0) for b in block_types), default=0)

        counts = _inventory_counts(self.bot)
        best_pick, best_tier = _best_pickaxe_in_inv(counts)
        if req_tier > 0 and best_tier < req_tier:
            tier_names = {1: "wooden_pickaxe(木镐)", 2: "stone_pickaxe(石镐)", 3: "iron_pickaxe(铁镐)", 4: "diamond_pickaxe(钻石镐)"}
            allowed = [b for b in block_types if BLOCK_MIN_PICKAXE_TIER.get(b, 0) <= best_tier]
            blocked = {b: BLOCK_MIN_PICKAXE_TIER[b] for b in block_types if BLOCK_MIN_PICKAXE_TIER.get(b, 0) > best_tier}
            return {
                "ok": False,
                "error": "tool_tier_insufficient",
                "message": f"本次混合请求包含工具等级不足的目标 {list(blocked)}，当前最高 {best_pick or '无镐'}。当前允许 {allowed}，可单独请求这些类型；其余至少需要 {tier_names.get(req_tier, req_tier)}。本次没有执行挖掘。",
                "required_tier": req_tier,
                "current_pickaxe": best_pick,
                "allowed_types": allowed,
                "blocked_types": blocked,
            }

        st = self.bot.status()
        if bool(st.get("in_water")):
            esc = self.escape_water()
            if esc.get("in_water"):
                return {
                    "ok": False,
                    "error": "in_water",
                    "message": "角色当前仍在水中，水下挖掘极慢且易溺水中断；请先脱离水体登上干燥地面再挖矿。",
                    "water_escape": esc,
                }
            counts = _inventory_counts(self.bot)
            best_pick, _ = _best_pickaxe_in_inv(counts)

        if best_pick:
            try:
                self.bot.call("equip", {"item": best_pick, "slot": "hand"})
            except ApiError:
                pass

        res = self._dig_target_blocks(
            block_types=block_types,
            count=count,
            radius=radius,
            protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
            below_tol=1.5,
            above_tol=2.5,
            inv_before=counts,
            avoid_water=True,
        )
        if res.get("error") == "protected_region":
            return res
        return {
            **res,
            "pickaxe_used": best_pick,
        }

    def smelt_items(
        self,
        input_item: str,
        count: int = 1,
        fuel_item: str | None = None,
        wait_timeout_s: float = 95.0,
    ) -> dict[str, Any]:
        """Ensure nearby furnace, compute exact ceil(n/8) coal/charcoal or wood fallback, smelt, guard health, and collect output."""
        counts = _inventory_counts(self.bot)
        avail_in = counts.get(input_item, 0)
        if avail_in < 1:
            return {"ok": False, "error": "missing_ingredients", "message": f"背包没有 {input_item}。"}
        smelt_n = min(max(1, count), avail_in)

        # Locate or build+place furnace
        found = self.bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": 5})
        furnaces = found.get("blocks") or []
        furnace_pos = None
        if furnaces:
            nearest = min(furnaces, key=lambda b: float(b.get("dist", 99)))
            furnace_pos = nearest["pos"]
            if float(nearest.get("dist", 99)) > 4.0:
                self.bot.call("move_to", {"x": furnace_pos[0], "y": furnace_pos[1], "z": furnace_pos[2], "range": 2.5})
        else:
            if counts.get("furnace", 0) < 1:
                cg = self.craft_gear("furnace", 1)
                if not cg.get("ok"):
                    return cg
            try:
                placed = self.bot.call("place", {"item": "furnace"})
                furnace_pos = placed.get("pos")
            except ApiError as err:
                st_f = self.bot.status()
                fpos = st_f.get("pos") or [0, 64, 0]
                fx, fy, fz = math.floor(float(fpos[0])), math.floor(float(fpos[1])), math.floor(float(fpos[2]))
                for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 1, 0)):
                    try:
                        placed = self.bot.call("place", {"item": "furnace", "pos": [fx + dx, fy + dy, fz + dz]})
                        furnace_pos = placed.get("pos") or [fx + dx, fy + dy, fz + dz]
                        break
                    except ApiError:
                        continue
                if furnace_pos is None:
                    pocket = self._carve_and_place_table(fx, fy, fz, item="furnace")
                    if pocket is not None:
                        furnace_pos = pocket.get("pos") or pocket.get("carved_pocket")
                    else:
                        return {"ok": False, "error": err.code, "message": err.message, **err.context}

        target = {"pos": furnace_pos} if furnace_pos else {"block": "furnace"}
        counts = _inventory_counts(self.bot)

        # Pick fuel & calculate exact fuel count, reserving smelt_n items if fuel matches input_item.
        chosen_fuel = fuel_item
        fuel_count = 0
        if chosen_fuel:
            per_fuel = 8.0 if chosen_fuel in ("coal", "charcoal") else 1.5
            avail_fuel = counts.get(chosen_fuel, 0) - (smelt_n if chosen_fuel == input_item else 0)
            fuel_count = min(max(0, avail_fuel), math.ceil(smelt_n / per_fuel))
        else:
            for cand, per_fuel in (
                ("coal", 8.0),
                ("charcoal", 8.0),
                *((p, 1.5) for p in sorted(counts) if p.endswith("_planks")),
                *((l, 1.5) for l in LOG_TYPES),
            ):
                avail_fuel = counts.get(cand, 0) - (smelt_n if cand == input_item else 0)
                if avail_fuel <= 0:
                    continue
                if cand in LOG_TYPES:
                    plank = _log_to_planks_name(cand)
                    try:
                        self.bot.call("craft", {"item": plank, "count": 4})
                        counts = _inventory_counts(self.bot)
                        if counts.get(plank, 0) > 0:
                            chosen_fuel = plank
                            fuel_count = min(counts[plank], math.ceil(smelt_n / 1.5))
                            break
                    except ApiError:
                        pass
                chosen_fuel = cand
                fuel_count = min(avail_fuel, math.ceil(smelt_n / per_fuel))
                break
            if (not chosen_fuel or fuel_count < 1) and input_item in LOG_TYPES and counts.get(input_item, 0) >= 2:
                # Convert 1 log into 4 planks for furnace fuel (smelts up to 6 items) while keeping >=1 log to smelt.
                if smelt_n >= counts.get(input_item, 0):
                    smelt_n = counts[input_item] - 1
                plank = _log_to_planks_name(input_item)
                try:
                    self.bot.call("craft", {"item": plank, "count": 4})
                    counts = _inventory_counts(self.bot)
                except ApiError:
                    pass
                if counts.get(plank, 0) > 0:
                    chosen_fuel = plank
                    fuel_count = min(counts[plank], math.ceil(smelt_n / 1.5))

        if not chosen_fuel or fuel_count < 1:
            return {"ok": False, "error": "missing_fuel", "message": "背包没有煤炭、木炭、木板或原木燃料。"}

        self.bot.call(
            "window/act",
            {
                "target": target,
                "ops": [
                    {"op": "put", "item": input_item, "count": smelt_n, "to": "input"},
                    {"op": "put", "item": chosen_fuel, "count": fuel_count, "to": "fuel"},
                ],
            },
        )

        # Poll window/view while running L0 health/hunger/water guard
        deadline = time.monotonic() + max(15.0, wait_timeout_s)
        while time.monotonic() < deadline:
            check_and_stabilize(self.bot)
            view = self.bot.call("window/view", {"target": target})
            sections = view.get("sections") or {}
            inputs_left = sum(int(x.get("count") or 0) for x in (sections.get("input") or []))
            outputs = sections.get("output") or []
            out_total = sum(int(x.get("count") or 0) for x in outputs)
            info = view.get("info") or {}
            burning = bool(info.get("burning"))
            if out_total >= smelt_n or (inputs_left == 0 and not burning):
                break
            time.sleep(2.5)

        collected: list[dict[str, Any]] = []
        view = self.bot.call("window/view", {"target": target})
        for out_item in (view.get("sections") or {}).get("output") or []:
            cnt = int(out_item.get("count") or 0)
            if cnt > 0:
                self.bot.call(
                    "window/act",
                    {"target": target, "ops": [{"op": "take", "from": "output", "count": cnt}]},
                )
                collected.append({"name": out_item.get("name"), "count": cnt})

        return {
            "ok": True,
            "furnace_pos": furnace_pos,
            "input": input_item,
            "requested_count": smelt_n,
            "fuel": {"item": chosen_fuel, "count": fuel_count},
            "collected": collected,
        }

    def bind_bed_spawn(self, x: int, y: int, z: int) -> dict[str, Any]:
        """Move near a bed and interact reliably day or night to set respawn point."""
        self.bot.call("move_to", {"x": x, "y": y, "z": z, "range": 2.5})
        st = self.bot.status()
        payload: dict[str, Any] = {"pos": [x, y, z]}
        if st.get("is_day"):
            counts = _inventory_counts(self.bot)
            for hand_item in ("stick", "wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "stone_sword", "iron_sword", "bone"):
                if counts.get(hand_item, 0) > 0:
                    payload["item"] = hand_item
                    break
        res = self.bot.call("interact", payload)
        return {"ok": True, "bed_pos": [x, y, z], **res}

    def escape_water(self) -> dict[str, Any]:
        """Float to the water surface, carve an upper notch over a solid shoreline step if needed, and hop onto dry land."""
        st = self.bot.status()
        if not bool(st.get("in_water")):
            return {
                "ok": True,
                "in_water": False,
                "already_dry": True,
                "pos_after": st.get("pos"),
                "dug": [],
                "control": {},
            }

        if not bool(st.get("jumping")):
            try:
                self.bot.call(
                    "posture",
                    {
                        "sneak": False,
                        "shield": False,
                        "jump": True,
                    },
                )
            except ApiError:
                pass

        water_names = {"water", "flowing_water"}
        fluids = {"water", "flowing_water", "lava", "flowing_lava"}
        passable = {
            "air",
            "cave_air",
            "void_air",
            "short_grass",
            "tall_grass",
            "fern",
            "large_fern",
            "leaf_litter",
            "snow",
            "vine",
            "dead_bush",
            "dandelion",
            "poppy",
        }
        dug: list[dict[str, Any]] = []
        mv_res: dict[str, Any] = {}
        pick_equipped = False

        def _try_dig_above(cx: int, cy: int, cz: int) -> bool:
            nonlocal pick_equipped
            if self._is_block_avoided((cx, cy, cz)):
                return False
            try:
                bname, diggable = self._block_info([cx, cy, cz])
            except ApiError:
                return False
            if bname in passable:
                return True
            if bname in fluids or bname == "bedrock" or not diggable:
                return False
            if not pick_equipped and (
                bname in BLOCK_MIN_PICKAXE_TIER or "stone" in bname or "ore" in bname or "deepslate" in bname
            ):
                pick_equipped = self._equip_if_present(tuple(PICKAXE_TIERS)) is not None
            try:
                self.bot.call("dig", {"pos": [cx, cy, cz], "collect": True, "stop_on": ["death"]})
                dug.append({"pos": [cx, cy, cz], "block": bname})
                return True
            except ApiError as err:
                if _is_protected_err(err):
                    self.protected_positions.add((cx, cy, cz))
                else:
                    self._avoid_temporarily((cx, cy, cz), err.code)
                return False

        def _step_onto_adjacent_bank() -> tuple[dict[str, Any], dict[str, Any]]:
            nonlocal mv_res
            cur = self.bot.status()
            pos = cur.get("pos") or [0.0, 64.0, 0.0]
            px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
            bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

            # Float to water surface layer wy if currently submerged
            wy = by
            for _ in range(4):
                try:
                    above_name, _ = self._block_info([bx, wy + 1, bz])
                except ApiError:
                    break
                if above_name in water_names:
                    try:
                        self.bot.call("control", {"jump": True, "ms": 450, "stop_on": ["death"]})
                    except ApiError:
                        pass
                    wy += 1
                else:
                    break

            cur = self.bot.status()
            pos = cur.get("pos") or [px, float(wy), pz]
            px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
            bx, bz = math.floor(px), math.floor(pz)
            wy = max(wy, math.floor(py))

            # Clear any non-fluid roof/wall overlapping the bot above water surface (never dig wy itself)
            xs = sorted({math.floor(px - 0.35), bx, math.floor(px + 0.35)})
            zs = sorted({math.floor(pz - 0.35), bz, math.floor(pz + 0.35)})
            for cy in (wy + 1, wy + 2):
                for cx in xs:
                    for cz in zs:
                        _try_dig_above(cx, cy, cz)

            # Inspect 4 cardinal banks around (bx, wy, bz), preserving solid step (ex, wy, ez)
            candidates: list[tuple[int, int, int, int, str, str]] = []
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ex, ez = bx + dx, bz + dz
                try:
                    bm1, _ = self._block_info([ex, wy - 1, ez])
                    b0, _ = self._block_info([ex, wy, ez])
                    b1, d1 = self._block_info([ex, wy + 1, ez])
                    b2, d2 = self._block_info([ex, wy + 2, ez])
                except ApiError:
                    continue
                if b1 in (fluids | {"bedrock"}) or b2 in (fluids | {"bedrock"}):
                    continue
                is_step_a = b0 not in (passable | fluids)
                is_step_b = b0 in passable and bm1 not in (passable | fluids)
                if not (is_step_a or is_step_b):
                    continue
                need_dig = (0 if b1 in passable else 1) + (0 if b2 in passable else 1)
                if b1 not in passable and (not d1 or self._is_block_avoided((ex, wy + 1, ez))):
                    continue
                if b2 not in passable and (not d2 or self._is_block_avoided((ex, wy + 2, ez))):
                    continue
                candidates.append((need_dig, 0 if is_step_a else 1, ex, ez, b1, b2))

            candidates.sort(key=lambda item: (item[0], item[1]))
            after_cur = cur
            for _, _, ex, ez, b1, b2 in candidates:
                if b1 not in passable and not _try_dig_above(ex, wy + 1, ez):
                    continue
                if b2 not in passable and not _try_dig_above(ex, wy + 2, ez):
                    continue
                cur2 = self.bot.status()
                p2 = cur2.get("pos") or [px, float(wy), pz]
                yaw = _yaw_toward(float(p2[0]), float(p2[2]), ex + 0.5, ez + 0.5)
                try:
                    mv_res = self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "yaw": yaw, "ms": 1150, "stop_on": ["death"]},
                    )
                except ApiError as err:
                    mv_res = {"error": err.code, "message": err.message}
                after_cur = self.bot.status()
                if not bool(after_cur.get("in_water")):
                    return mv_res, after_cur
            return mv_res, after_cur

        mv_res, st_after = _step_onto_adjacent_bank()

        if bool(st_after.get("in_water")):
            pos_now = st_after.get("pos") or st.get("pos") or [0.0, 64.0, 0.0]
            by_now = math.floor(float(pos_now[1]))
            shore_blocks: list[dict[str, Any]] = []
            try:
                found = self.bot.call(
                    "find_blocks",
                    {
                        "radius": 16,
                        "types": [
                            "grass_block",
                            "dirt",
                            "sand",
                            "gravel",
                            "stone",
                            "cobblestone",
                            "andesite",
                            "diorite",
                            "granite",
                            "clay",
                            "mud",
                        ],
                        "limit": 30,
                    },
                )
                shore_blocks = list(found.get("blocks") or [])
            except ApiError:
                shore_blocks = []

            shore_blocks.sort(key=lambda b: float(b.get("dist", 99)))
            dry_targets: list[list[int]] = []
            for blk in shore_blocks:
                bpos = blk.get("pos")
                if not isinstance(bpos, list) or len(bpos) < 3:
                    continue
                if abs(int(bpos[1]) - by_now) > 3:
                    continue
                if not self._is_water_covered((int(bpos[0]), int(bpos[1]), int(bpos[2]))):
                    dry_targets.append([int(bpos[0]), int(bpos[1]), int(bpos[2])])
                    if len(dry_targets) >= 3:
                        break

            for tx, _ty, tz in dry_targets:
                try:
                    mv_res = self.bot.call(
                        "move_to",
                        {"x": tx + 0.5, "z": tz + 0.5, "range": 1.2, "stop_on": ["death"]},
                    )
                except ApiError:
                    cur3 = self.bot.status()
                    p3 = cur3.get("pos") or pos_now
                    yaw3 = _yaw_toward(float(p3[0]), float(p3[2]), tx + 0.5, tz + 0.5)
                    try:
                        mv_res = self.bot.call(
                            "control",
                            {"forward": True, "jump": True, "yaw": yaw3, "ms": 1800, "stop_on": ["death"]},
                        )
                    except ApiError as err:
                        mv_res = {"error": err.code, "message": err.message}
                st_after = self.bot.status()
                if not bool(st_after.get("in_water")):
                    break
                mv_res, st_after = _step_onto_adjacent_bank()
                if not bool(st_after.get("in_water")):
                    break

        st_after = self.bot.status()
        still_in_water = bool(st_after.get("in_water"))
        if not still_in_water and bool(st_after.get("jumping")):
            try:
                self.bot.call(
                    "posture",
                    {
                        "sneak": bool(st_after.get("sneaking")),
                        "shield": bool(st_after.get("blocking")),
                        "jump": False,
                    },
                )
            except ApiError:
                pass
            st_after = self.bot.status()
            still_in_water = bool(st_after.get("in_water"))

        return {
            "ok": not still_in_water,
            "in_water": still_in_water,
            "pos_after": st_after.get("pos"),
            "dug": dug,
            "control": mv_res,
        }

    def unstick(self) -> dict[str, Any]:
        """Clear blocks overlapping the bot's 0.6x1.8 floor()-aligned hitbox and step out of pits/corners or water."""
        st = self.bot.status()
        pos_before = st.get("pos") or [0.0, 64.0, 0.0]
        px, py, pz = float(pos_before[0]), float(pos_before[1]), float(pos_before[2])
        bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

        xs = sorted({math.floor(px - 0.38), bx, math.floor(px + 0.38)})
        zs = sorted({math.floor(pz - 0.38), bz, math.floor(pz + 0.38)})
        if bool(st.get("in_water")):
            esc = self.escape_water()
            return {
                "ok": True,
                "foot_block_before": [bx, by, bz],
                "hitbox_xs": xs,
                "hitbox_zs": zs,
                "pos_before": pos_before,
                "pos_after": esc.get("pos_after", pos_before),
                "dug": esc.get("dug", []),
                "control": esc.get("control", {}),
                "escaped_water": esc.get("ok", False),
                "in_water": esc.get("in_water", False),
            }

        skip_names = {"air", "cave_air", "void_air", "water", "lava", "bedrock"}
        dug: list[dict[str, Any]] = []
        pick_equipped = False

        def _try_dig_if_solid(cx: int, cy: int, cz: int, *, retries: int = 1) -> None:
            nonlocal pick_equipped
            for _attempt in range(retries):
                try:
                    blk = self.bot.call("get_block", {"pos": [cx, cy, cz]})
                except ApiError:
                    return
                bname = str(blk.get("name") or "air")
                if bname in skip_names or not blk.get("diggable", False):
                    return
                if self._is_block_avoided((cx, cy, cz)):
                    return
                if not pick_equipped and (
                    bname in BLOCK_MIN_PICKAXE_TIER or "stone" in bname or "ore" in bname or "deepslate" in bname
                ):
                    pick_equipped = self._equip_if_present(tuple(PICKAXE_TIERS)) is not None
                try:
                    self.bot.call("dig", {"pos": [cx, cy, cz], "collect": True, "stop_on": ["death"]})
                    dug.append({"pos": [cx, cy, cz], "block": bname})
                except ApiError as err:
                    if _is_protected_err(err):
                        self.protected_positions.add((cx, cy, cz))
                    else:
                        self._avoid_temporarily((cx, cy, cz), err.code)
                    return

        # 1. Clear full horizontal hitbox footprint at head (by+1) and feet (by), plus overhead (by+2)
        for cy in (by + 1, by):
            for cx in xs:
                for cz in zs:
                    _try_dig_if_solid(cx, cy, cz, retries=2)
        _try_dig_if_solid(bx, by + 2, bz, retries=2)

        # 2. Clear one cardinal 3-high escape slot and nudge out with control()
        mv_res: dict[str, Any] = {}
        exit_yaw = -math.pi / 2
        for dx, dz, yaw in (
            (1, 0, -math.pi / 2),
            (0, -1, 0.0),
            (-1, 0, math.pi / 2),
            (0, 1, math.pi),
        ):
            ex, ez = bx + dx, bz + dz
            _try_dig_if_solid(ex, by + 1, ez)
            _try_dig_if_solid(ex, by + 2, ez)
            _try_dig_if_solid(ex, by, ez)
            exit_yaw = yaw
            break

        try:
            mv_res = self.bot.call(
                "control",
                {"forward": True, "jump": True, "yaw": exit_yaw, "ms": 900, "stop_on": []},
            )
        except ApiError as err:
            mv_res = {"error": err.code, "message": err.message}

        st_after = self.bot.status()
        pos_after = st_after.get("pos") or pos_before
        return {
            "ok": True,
            "foot_block_before": [bx, by, bz],
            "hitbox_xs": xs,
            "hitbox_zs": zs,
            "pos_before": pos_before,
            "pos_after": pos_after,
            "dug": dug,
            "control": mv_res,
        }

    def dig_safety_bunker(self, *, urgent: bool = False) -> dict[str, Any]:
        """Dig a 1x3 vertical shaft under the bot and seal the top to block night mobs."""
        st = self.bot.status()
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))

        fluid_or_bedrock = {"water", "lava", "flowing_water", "flowing_lava", "bedrock"}
        non_solid_floor = {"air", "cave_air", "void_air", "water", "lava", "flowing_water", "flowing_lava"}

        def _block_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            raw = res.get("name") or res.get("block") or "air"
            return str(raw).removeprefix("minecraft:")

        floor_pos = [bx, by - 4, bz]
        floor_block = _block_name(floor_pos)
        if floor_block in non_solid_floor:
            return {
                "ok": False,
                "reason": "unsafe_floor",
                "floor_pos": floor_pos,
                "floor_block": floor_block,
            }

        shaft_positions = [[bx, by - 1, bz], [bx, by - 2, bz], [bx, by - 3, bz]]
        shaft_blocks: list[tuple[list[int], str]] = []
        for s_pos in shaft_positions:
            b_name = _block_name(s_pos)
            if b_name in fluid_or_bedrock:
                return {
                    "ok": False,
                    "reason": "unsafe_shaft_block",
                    "block_pos": s_pos,
                    "block": b_name,
                }
            shaft_blocks.append((s_pos, b_name))

        if urgent:
            if st.get("on_ground") is not True:
                return {"ok": False, "reason": "not_grounded"}
            # Never open a fluid pocket or an existing cave beside an emergency shaft.
            for cy in (by - 1, by - 2, by - 3):
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbor = [bx + dx, cy, bz + dz]
                    name = _block_name(neighbor)
                    if name in non_solid_floor:
                        return {"ok": False, "reason": "unsafe_open_side", "block_pos": neighbor, "block": name}

        roof_candidates = ("dirt", "cobblestone", "cobbled_deepslate", "andesite", "diorite", "granite")

        def _pick_roof_item(inv: dict[str, int]) -> str | None:
            for cand in roof_candidates:
                if inv.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(inv.items()):
                if cnt >= 1 and name.endswith("_planks"):
                    return name
            return None

        counts = _inventory_counts(self.bot)
        crafted_planks: str | None = None
        if _pick_roof_item(counts) is None:
            if urgent:
                return {"ok": False, "reason": "no_roof_block"}
            for log_name in LOG_TYPES:
                if counts.get(log_name, 0) >= 1:
                    planks_name = _log_to_planks_name(log_name)
                    try:
                        self.bot.call("craft", {"item": planks_name, "count": 4})
                        crafted_planks = planks_name
                    except ApiError as exc:
                        return {
                            "ok": False,
                            "reason": "no_roof_block_craft_failed",
                            "error": exc.code,
                            "message": exc.message,
                        }
                    counts = _inventory_counts(self.bot)
                    break

        if _pick_roof_item(counts) is None:
            return {"ok": False, "reason": "no_roof_block"}

        if urgent and any(name in BLOCK_MIN_PICKAXE_TIER for _, name in shaft_blocks):
            best, tier = _best_pickaxe_in_inv(counts)
            if best is None or any(BLOCK_MIN_PICKAXE_TIER.get(name, 0) > tier for _, name in shaft_blocks):
                return {"ok": False, "reason": "missing_shaft_tool"}
            self.bot.call("equip", {"item": best, "slot": "hand"})

        dug: list[dict[str, Any]] = []
        for s_pos, b_name in shaft_blocks:
            if b_name in {"air", "cave_air", "void_air"}:
                continue
            try:
                # An urgent bunker already has roof material. Chasing drops
                # between each dig can add eight seconds per block.
                self.bot.call("dig", {"pos": s_pos, "collect": not urgent})
                dug.append({"pos": s_pos, "block": b_name})
            except ApiError as exc:
                return {
                    "ok": False,
                    "reason": "protected" if _is_protected_err(exc) else "dig_failed",
                    "failed_pos": s_pos,
                    "error": exc.code,
                    "message": exc.message,
                    "dug": dug,
                }

        counts_after = _inventory_counts(self.bot)
        roof_item = _pick_roof_item(counts_after)
        roof_pos = [bx, by - 1, bz]
        roof_placed = False
        roof_error: dict[str, str] | None = None
        if roof_item:
            try:
                self.bot.call("place", {"item": roof_item, "pos": roof_pos})
                roof_placed = True
            except ApiError as exc:
                roof_error = {"code": exc.code, "message": exc.message}

        st_after = self.bot.status()
        return {
            "ok": roof_placed,
            "origin": [bx, by, bz],
            "floor_pos": floor_pos,
            "floor_block": floor_block,
            "crafted_planks": crafted_planks,
            "dug": dug,
            "roof_pos": roof_pos,
            "roof_item": roof_item,
            "roof_placed": roof_placed,
            "roof_error": roof_error,
            "pos_after": st_after.get("pos"),
        }

    def dig_staircase(
        self,
        direction: str = "north",
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool = True,
    ) -> dict[str, Any]:
        """Dig a safe 3-high stepped staircase downward or upward while guarding fluids and drops."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        d_key = str(direction).strip().lower()
        v_key = str(vertical).strip().lower()
        if d_key not in dirs or v_key not in {"down", "up"}:
            return {
                "ok": False,
                "error": "bad_args",
                "message": "direction 必须是 north/south/east/west，vertical 必须是 down/up。",
            }

        dx, dz = dirs[d_key]
        steps = max(1, min(25, int(steps)))
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        non_solid = air_set | fluids | {
            "short_grass",
            "tall_grass",
            "leaf_litter",
            "dandelion",
            "poppy",
            "oak_sapling",
        }
        ore_types = ["iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore", "copper_ore"]
        iron_names = {"iron_ore", "deepslate_iron_ore"}
        surface_evidence: dict[str, Any] | None = None

        def _block_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        def _equip_best_pick() -> None:
            best_pick, _ = _best_pickaxe_in_inv(_inventory_counts(self.bot))
            if best_pick:
                try:
                    self.bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass

        def _pick_step_filler() -> str | None:
            counts = _inventory_counts(self.bot)
            for cand in ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite", "granite"):
                if counts.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(counts.items()):
                if cnt >= 1 and name.endswith("_planks"):
                    return name
            return None

        def _settled_position(cur: list[float]) -> list[float]:
            for _ in range(6):
                time.sleep(0.1)
                settled = self.bot.status()
                cur = settled.get("pos") or cur
                if settled.get("on_ground") is not False:
                    break
            return [float(cur[0]), float(cur[1]), float(cur[2])]

        def _advance_step(tx: int, ty: int, tz: int, bx: int, by: int, bz: int, *, jump: bool) -> list[float]:
            fx, fy, fz = tx + 0.5, float(ty), tz + 0.5
            try:
                mv = self.bot.call("move_to", {"x": fx, "y": ty, "z": fz, "range": 0.5})
                cur = mv.get("pos") or self.bot.status().get("pos") or [bx + 0.5, float(by), bz + 0.5]
            except ApiError:
                cur = self.bot.status().get("pos") or [bx + 0.5, float(by), bz + 0.5]

            if math.floor(float(cur[0])) == bx and math.floor(float(cur[2])) == bz:
                try:
                    self.bot.call("look", {"x": fx, "y": fy, "z": fz})
                except ApiError:
                    pass
                ctrl_body: dict[str, Any] = {
                    "forward": True,
                    "ms": 550 if jump else 420,
                    "stop_on": ["hurt", "death"],
                }
                if jump:
                    ctrl_body["jump"] = True
                try:
                    ctrl = self.bot.call("control", ctrl_body)
                    cur = ctrl.get("pos") or self.bot.status().get("pos") or cur
                except ApiError:
                    cur = self.bot.status().get("pos") or cur
            # Control results can be captured at the jump apex. Confirm the
            # settled position instead of counting a jump that lands back below.
            return _settled_position(cur)

        def _confirm_surface(cur: list[float]) -> dict[str, Any] | None:
            cx, cy, cz = (math.floor(float(v)) for v in cur)
            below = _block_name([cx, cy - 1, cz])
            surface_ground = {"grass_block", "podzol", "mycelium", "sand", "red_sand", "snow_block"}
            if below in surface_ground and all(_block_name([cx, cy + dy, cz]) in air_set for dy in (0, 1)):
                return {"floor": below, "pos": list(cur), "basis": "standing_on_surface_ground"}
            # Nearby roots/grass above the ceiling do not mean the bot reached
            # that level. Stone steps need a same-level surface and an open sky column.
            surf = self.bot.call("find_blocks", {"radius": 5, "types": ["grass_block", "podzol", "sand"], "limit": 8})
            same_level = [b for b in surf.get("blocks", []) if b.get("pos") and abs(float(b["pos"][1]) + 1 - cy) <= 1]
            if same_level and all(_block_name([cx, cy + dy, cz]) in air_set for dy in range(2, 14)):
                return {"floor": below, "pos": list(cur), "basis": "same_level_surface_and_clear_column"}
            return None

        _equip_best_pick()
        st0 = self.bot.status()
        pos_before = st0.get("pos") or [0.0, 64.0, 0.0]
        steps_done = 0
        stop_reason = "completed"
        ores_nearby: list[dict[str, Any]] = []
        last_error: dict[str, Any] | None = None

        for step_idx in range(steps):
            if step_idx > 0 and step_idx % 6 == 0:
                _equip_best_pick()

            st = self.bot.status()
            pos = st.get("pos") or pos_before
            bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
            nx, nz = bx + dx, bz + dz

            head_here = _block_name([bx, by + 1, bz])
            if head_here in fluid_or_bedrock:
                stop_reason = "fluid_or_bedrock"
                break
            if head_here not in air_set:
                try:
                    self.bot.call("dig", {"pos": [bx, by + 1, bz], "collect": True, "stop_on": ["death"]})
                except ApiError as err:
                    last_error = {"code": err.code, "message": err.message, "pos": [bx, by + 1, bz]}
                    stop_reason = "dig_failed"
                    break

            if v_key == "down":
                safety_coords = [[nx, by + 2, nz], [nx, by + 1, nz], [nx, by, nz], [nx, by - 1, nz]]
                if any(_block_name(c) in fluid_or_bedrock for c in safety_coords):
                    stop_reason = "fluid_or_bedrock"
                    break
                if _block_name([nx, by - 2, nz]) in non_solid:
                    stop_reason = "cave_or_drop_ahead"
                    break

                dig_failed = False
                for idx, dpos in enumerate([[nx, by + 1, nz], [nx, by, nz], [nx, by - 1, nz]]):
                    if _block_name(dpos) not in air_set:
                        try:
                            self.bot.call("dig", {"pos": dpos, "collect": True})
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": dpos}
                            dig_failed = True
                            break
                        if idx >= 1 and _block_name(dpos) not in air_set:
                            try:
                                self.bot.call("dig", {"pos": dpos, "collect": True, "stop_on": ["death"]})
                            except ApiError as err:
                                last_error = {"code": err.code, "message": err.message, "pos": dpos}
                                dig_failed = True
                                break
                if dig_failed:
                    stop_reason = "dig_failed"
                    break

                cur = _advance_step(nx, by - 1, nz, bx, by, bz, jump=False)
                if math.floor(cur[0]) == bx and math.floor(cur[2]) == bz:
                    xs = sorted({math.floor(cur[0] - 0.3), math.floor(cur[0] + 0.3)})
                    zs = sorted({math.floor(cur[2] - 0.3), math.floor(cur[2] + 0.3)})
                    for hx in xs:
                        for hz in zs:
                            for hy in (by, by + 1):
                                hpos = [hx, hy, hz]
                                hb = _block_name(hpos)
                                if hb not in air_set and hb not in fluid_or_bedrock:
                                    try:
                                        self.bot.call("dig", {"pos": hpos, "collect": True, "stop_on": ["death"]})
                                    except ApiError:
                                        pass
                    try:
                        mv2 = self.bot.call("move_to", {"x": nx + 0.5, "y": by - 1, "z": nz + 0.5, "range": 0.5})
                        cur = mv2.get("pos") or self.bot.status().get("pos") or cur
                    except ApiError:
                        cur = self.bot.status().get("pos") or cur
                    cur = _settled_position(cur)
                if math.floor(float(cur[1])) != by - 1 or math.dist([float(cur[0]), float(cur[2])], [nx + 0.5, nz + 0.5]) > 0.85:
                    stop_reason = "movement_not_confirmed"
                    break
            else:
                if any(_block_name(c) in fluid_or_bedrock for c in ([bx, by + 2, bz], [nx, by + 1, nz], [nx, by + 2, nz])):
                    stop_reason = "fluid_or_bedrock"
                    break
                if any(_block_name(c) in fluids for c in ([bx, by + 3, bz], [nx, by + 3, nz])):
                    stop_reason = "fluid_or_bedrock"
                    break

                dig_failed = False
                for dpos in [[bx, by + 2, bz], [nx, by + 2, nz], [nx, by + 1, nz]]:
                    if _block_name(dpos) not in air_set:
                        try:
                            self.bot.call("dig", {"pos": dpos, "collect": True})
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": dpos}
                            dig_failed = True
                            break
                if dig_failed:
                    stop_reason = "dig_failed"
                    break

                if _block_name([nx, by, nz]) in non_solid:
                    filler = _pick_step_filler()
                    if not filler:
                        stop_reason = "missing_step_support"
                        break
                    try:
                        self.bot.call("place", {"item": filler, "pos": [nx, by, nz]})
                    except ApiError:
                        stop_reason = "missing_step_support"
                        break

                cur = _advance_step(nx, by + 1, nz, bx, by, bz, jump=True)
                if math.floor(float(cur[1])) != by + 1 or math.dist([float(cur[0]), float(cur[2])], [nx + 0.5, nz + 0.5]) > 0.85:
                    stop_reason = "movement_not_confirmed"
                    break

                steps_done += 1
                if step_idx >= 1:
                    surface_evidence = _confirm_surface(cur)
                    if surface_evidence:
                        stop_reason = "reached_surface"
                        break

                if stop_on_ore and steps_done % 3 == 0:
                    found_ores = self.bot.call("find_blocks", {"radius": 12, "types": ore_types, "limit": 10})
                    ores_nearby = found_ores.get("blocks") or []
                    if any(b.get("name") in iron_names for b in ores_nearby):
                        stop_reason = "ore_detected"
                        break
                continue

            steps_done += 1
            if stop_on_ore and steps_done % 3 == 0:
                found_ores = self.bot.call("find_blocks", {"radius": 12, "types": ore_types, "limit": 10})
                ores_nearby = found_ores.get("blocks") or []
                if any(b.get("name") in iron_names for b in ores_nearby):
                    stop_reason = "ore_detected"
                    break

        if stop_on_ore and stop_reason == "completed":
            found_ores = self.bot.call("find_blocks", {"radius": 12, "types": ore_types, "limit": 10})
            ores_nearby = found_ores.get("blocks") or []
            if any(b.get("name") in iron_names for b in ores_nearby):
                stop_reason = "ore_detected"

        st_end = self.bot.status()
        out_stair: dict[str, Any] = {
            "ok": stop_reason in {"completed", "ore_detected", "reached_surface"} and (steps_done > 0 or stop_reason == "ore_detected"),
            "requires_replan": stop_reason not in {"completed", "ore_detected", "reached_surface"},
            "direction": d_key,
            "vertical": v_key,
            "steps_requested": steps,
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "pos_before": pos_before,
            "pos_after": st_end.get("pos"),
            "ores_nearby": ores_nearby,
        }
        if last_error is not None:
            out_stair["last_error"] = last_error
        if surface_evidence is not None:
            out_stair["surface_evidence"] = surface_evidence
        return out_stair
