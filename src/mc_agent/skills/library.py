"""L1 deterministic macro skills: spawn-protection blacklist, tree chopping, crafting, tiered mining, smelting, bed spawn."""

from __future__ import annotations

from copy import copy
import inspect
import math
import time
from typing import Any, Callable

import httpx

from ..bot import BotSession
from ..errors import ApiError
from ..kb import RECIPES_1_21, assess_world_target, normalize_dimension, resolve_item_alias
from .safety import check_and_stabilize
from .crafting import material_shortage, explain_craft_error

# Maximum local-recovery attempts inside `navigate_segmented` before returning a structured block.
MAX_LOCAL_RECOVERY_ATTEMPTS = 2

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

NETHER_STEM_TYPES: tuple[str, ...] = (
    "crimson_stem",
    "warped_stem",
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

STAIR_FILLER_BLOCKS: tuple[str, ...] = (
    "cobblestone", "cobbled_deepslate", "dirt", "stone", "deepslate",
    "netherrack", "blackstone", "basalt", "end_stone", "andesite", "diorite", "granite", "tuff",
)

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

ORE_COUNTERPARTS: dict[str, str] = {
    "coal_ore": "deepslate_coal_ore",
    "deepslate_coal_ore": "coal_ore",
    "iron_ore": "deepslate_iron_ore",
    "deepslate_iron_ore": "iron_ore",
    "copper_ore": "deepslate_copper_ore",
    "deepslate_copper_ore": "copper_ore",
    "lapis_ore": "deepslate_lapis_ore",
    "deepslate_lapis_ore": "lapis_ore",
    "gold_ore": "deepslate_gold_ore",
    "deepslate_gold_ore": "gold_ore",
    "redstone_ore": "deepslate_redstone_ore",
    "deepslate_redstone_ore": "redstone_ore",
    "diamond_ore": "deepslate_diamond_ore",
    "deepslate_diamond_ore": "diamond_ore",
    "emerald_ore": "deepslate_emerald_ore",
    "deepslate_emerald_ore": "emerald_ore",
}

EXPECTED_BLOCK_DROPS: dict[str, frozenset[str]] = {
    "iron_ore": frozenset({"raw_iron", "iron_ore", "deepslate_iron_ore"}),
    "deepslate_iron_ore": frozenset({"raw_iron", "iron_ore", "deepslate_iron_ore"}),
    "diamond_ore": frozenset({"diamond", "diamond_ore", "deepslate_diamond_ore"}),
    "deepslate_diamond_ore": frozenset({"diamond", "diamond_ore", "deepslate_diamond_ore"}),
    "coal_ore": frozenset({"coal", "coal_ore", "deepslate_coal_ore"}),
    "deepslate_coal_ore": frozenset({"coal", "coal_ore", "deepslate_coal_ore"}),
    "copper_ore": frozenset({"raw_copper", "copper_ore", "deepslate_copper_ore"}),
    "deepslate_copper_ore": frozenset({"raw_copper", "copper_ore", "deepslate_copper_ore"}),
    "gold_ore": frozenset({"raw_gold", "gold_ore", "deepslate_gold_ore"}),
    "deepslate_gold_ore": frozenset({"raw_gold", "gold_ore", "deepslate_gold_ore"}),
    "nether_gold_ore": frozenset({"gold_nugget", "nether_gold_ore"}),
    "lapis_ore": frozenset({"lapis_lazuli", "lapis_ore", "deepslate_lapis_ore"}),
    "deepslate_lapis_ore": frozenset({"lapis_lazuli", "lapis_ore", "deepslate_lapis_ore"}),
    "redstone_ore": frozenset({"redstone", "redstone_ore", "deepslate_redstone_ore"}),
    "deepslate_redstone_ore": frozenset({"redstone", "redstone_ore", "deepslate_redstone_ore"}),
    "emerald_ore": frozenset({"emerald", "emerald_ore", "deepslate_emerald_ore"}),
    "deepslate_emerald_ore": frozenset({"emerald", "emerald_ore", "deepslate_emerald_ore"}),
    "nether_quartz_ore": frozenset({"quartz", "nether_quartz_ore"}),
    "stone": frozenset({"cobblestone", "stone"}),
    "deepslate": frozenset({"cobbled_deepslate", "deepslate"}),
    "grass_block": frozenset({"dirt", "grass_block"}),
    "gravel": frozenset({"gravel", "flint"}),
    "clay": frozenset({"clay_ball", "clay"}),
    "glowstone": frozenset({"glowstone_dust", "glowstone"}),
    "amethyst_cluster": frozenset({"amethyst_shard", "amethyst_cluster"}),
}


def _expected_drops_for_blocks(block_types: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Return expected inventory item names produced when harvesting `block_types`."""
    expected: set[str] = set()
    for raw_b in block_types:
        b = str(raw_b).removeprefix("minecraft:").strip().lower()
        if not b:
            continue
        expected.update(EXPECTED_BLOCK_DROPS.get(b, (b,)))
    return frozenset(expected)

# Recipes requiring a 3x3 crafting table (auto-synced with RECIPES_1_21 + extra objective 3x3 items).
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
    "diamond_shovel",
    "diamond_hoe",
    "furnace",
    "chest",
    "shield",
    "bucket",
    "iron_helmet",
    "iron_chestplate",
    "iron_leggings",
    "iron_boots",
    "diamond_helmet",
    "diamond_chestplate",
    "diamond_leggings",
    "diamond_boots",
    "bow",
    "crossbow",
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
    "stone_brick_slab",
    "dropper",
    "crafter",
    "iron_block",
    "gold_block",
    "diamond_block",
    "hay_block",
    *{k for k, v in RECIPES_1_21.items() if v.get("table_3x3")},
}

DEFAULT_EQUIP_SLOTS: dict[str, str] = {
    "shield": "off-hand",
    "iron_helmet": "head",
    "iron_chestplate": "torso",
    "iron_leggings": "legs",
    "iron_boots": "feet",
    "diamond_helmet": "head",
    "diamond_chestplate": "torso",
    "diamond_leggings": "legs",
    "diamond_boots": "feet",
    "wooden_pickaxe": "hand",
    "stone_pickaxe": "hand",
    "iron_pickaxe": "hand",
    "diamond_pickaxe": "hand",
    "wooden_sword": "hand",
    "stone_sword": "hand",
    "iron_sword": "hand",
    "diamond_sword": "hand",
    "wooden_axe": "hand",
    "stone_axe": "hand",
    "iron_axe": "hand",
    "diamond_axe": "hand",
    "wooden_shovel": "hand",
    "stone_shovel": "hand",
    "iron_shovel": "hand",
    "diamond_shovel": "hand",
    "wooden_hoe": "hand",
    "stone_hoe": "hand",
    "iron_hoe": "hand",
    "diamond_hoe": "hand",
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
    "diamond_shovel": (0, 2),
    "diamond_hoe": (0, 2),
    "shield": (6, 0),
    "oak_boat": (5, 0),
    "birch_boat": (5, 0),
    "spruce_boat": (5, 0),
    "white_bed": (3, 0),
    "oak_sign": (6, 1),
    "bow": (0, 3),
    "fishing_rod": (0, 3),
    "crossbow": (2, 3),
    "ladder": (0, 7),
    "painting": (0, 8),
    "campfire": (0, 3),
    "torch": (0, 1),
    "lever": (0, 1),
    "redstone_torch": (0, 1),
}


def _inventory_counts_from_raw(inv: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in inv.get("items") or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        cnt = int(row.get("count") or 0)
        if name and cnt > 0:
            norm = str(name).removeprefix("minecraft:")
            counts[norm] = counts.get(norm, 0) + cnt
    for eq in (inv.get("armor") or {}).values():
        if isinstance(eq, dict) and eq.get("name"):
            norm = str(eq["name"]).removeprefix("minecraft:")
            counts[norm] = counts.get(norm, 0) + int(eq.get("count") or 1)
    offhand = inv.get("offhand")
    if isinstance(offhand, dict) and offhand.get("name"):
        norm = str(offhand["name"]).removeprefix("minecraft:")
        counts[norm] = counts.get(norm, 0) + int(offhand.get("count") or 1)
    return counts


def _inventory_snapshot_and_counts(bot: BotSession) -> tuple[dict[str, Any], dict[str, int]]:
    inv = bot.inventory()
    if not isinstance(inv, dict):
        return {}, {}
    return inv, _inventory_counts_from_raw(inv)


def _inventory_counts(bot: BotSession) -> dict[str, int]:
    return _inventory_counts_from_raw(bot.inventory())


def _window_item_entries(items: Any) -> list[dict[str, Any]]:
    """Normalize `{item: count}` or `[{item,count,...}]` into window-op entries."""
    if isinstance(items, dict):
        raw: list[Any] = [{"item": key, "count": value} for key, value in items.items()]
    elif isinstance(items, (list, tuple)):
        raw = list(items)
    else:
        return []
    entries: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, str):
            entries.append({"item": resolve_item_alias(row), "count": 1})
            continue
        if not isinstance(row, dict):
            continue
        name = row.get("item") or row.get("name")
        if not name:
            continue
        try:
            count = int(row.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            continue
        raw_name = str(name).removeprefix("minecraft:").strip()
        if raw_name == "water_bottle":
            # Java's registry item is potion{potion:water}, not water_bottle.
            entry: dict[str, Any] = {"item": "potion", "potion": "water", "count": count}
        else:
            entry = {"item": resolve_item_alias(raw_name), "count": count}
        for key in ("slot", "potion", "enchanted", "custom_name"):
            if row.get(key) is not None:
                entry[key] = row[key]
        entries.append(entry)
    return entries


def _log_to_planks_name(log_name: str) -> str:
    if log_name.endswith("_stem"):
        return log_name.removesuffix("_stem") + "_planks"
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


MELEE_WEAPONS_PRIORITY: tuple[str, ...] = (
    "netherite_sword",
    "diamond_sword",
    "iron_sword",
    "stone_sword",
    "netherite_axe",
    "diamond_axe",
    "iron_axe",
    "stone_axe",
    "wooden_sword",
    "wooden_axe",
)

SAFE_MELEE_MOBS: frozenset[str] = frozenset(
    {"zombie", "zombie_villager", "husk", "drowned", "spider", "cave_spider", "silverfish"}
)


def best_melee_weapon_in_inv(counts: dict[str, int]) -> str | None:
    """Return the highest-DPS melee sword/axe present in inventory counts."""
    for name in MELEE_WEAPONS_PRIORITY:
        if counts.get(name, 0) > 0:
            return name
    return None


PASSABLE_SIGHT_BLOCKS: set[str] = {
    "air",
    "cave_air",
    "void_air",
    "water",
    "flowing_water",
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    "torch",
    "wall_torch",
    "vine",
    "snow",
    "leaf_litter",
    "sugar_cane",
    "dandelion",
    "poppy",
    "dead_bush",
    "wildflowers",
    "bush",
    "firefly_bush",
    "open_eyeblossom",
    "closed_eyeblossom",
    "glass",
    "glass_pane",
    "bamboo",
    "cobweb",
    "sweet_berry_bush",
    "azalea",
    "flowering_azalea",
    "scaffolding",
    "kelp",
    "seagrass",
    "tall_seagrass",
    "pointed_dripstone",
    "ladder",
    "iron_bars",
    "chain",
}
PASSABLE_WALK_BLOCKS: set[str] = PASSABLE_SIGHT_BLOCKS - {"glass", "glass_pane", "bamboo", "iron_bars"}
NON_WALL_SIGHT_SUFFIXES: tuple[str, ...] = (
    "_leaves",
    "_fence",
    "_fence_gate",
    "_trapdoor",
    "_door",
    "_sapling",
    "_banner",
    "_carpet",
    "_sign",
    "_hanging_sign",
)


def is_passable_sight_block(bname: str) -> bool:
    """Return True if projectiles/sight/mob awareness can pass through or around this non-wall block."""
    norm = str(bname or "").removeprefix("minecraft:").strip()
    return norm in PASSABLE_SIGHT_BLOCKS or norm.endswith(NON_WALL_SIGHT_SUFFIXES)
SHAFT_SEAL_BLOCKS: tuple[str, ...] = (
    "cobblestone",
    "cobbled_deepslate",
    "dirt",
    "granite",
    "diorite",
    "andesite",
    "tuff",
)


def cardinal_bearing(dx: float, dz: float) -> str:
    """Map a horizontal offset vector (dx, dz) to the dominant cardinal direction."""
    if abs(dx) >= abs(dz):
        return "east" if dx >= 0 else "west"
    return "south" if dz >= 0 else "north"


def _cached_block_name(
    bot: Any,
    x: int,
    y: int,
    z: int,
    block_cache: dict[tuple[int, int, int], str] | None = None,
) -> str:
    key = (int(x), int(y), int(z))
    if block_cache is not None and key in block_cache:
        return block_cache[key]
    try:
        res = bot.call("get_block", {"pos": [key[0], key[1], key[2]]})
    except ApiError:
        return "unknown"
    raw = str(res.get("name") or res.get("block") or "").removeprefix("minecraft:").strip()
    name = raw or "unknown"
    if block_cache is not None:
        block_cache[key] = name
    return name


def ray_wall_blocker(
    bot: Any,
    start_xyz: tuple[float, float, float],
    end_xyz: tuple[float, float, float],
    block_cache: dict[tuple[int, int, int], str] | None = None,
) -> dict[str, Any] | None:
    """Cast a 3D eye-to-body ray and return the first solid blocker dict, or None if clear/unknown."""
    sx, sy, sz = float(start_xyz[0]), float(start_xyz[1]) + 1.62, float(start_xyz[2])
    ex, ey, ez = float(end_xyz[0]), float(end_xyz[1]) + 1.0, float(end_xyz[2])
    dist = math.sqrt((ex - sx) ** 2 + (ey - sy) ** 2 + (ez - sz) ** 2)
    if dist < 1.0:
        return None

    bx, by, bz = math.floor(start_xyz[0]), math.floor(start_xyz[1]), math.floor(start_xyz[2])
    hx, hy, hz = math.floor(end_xyz[0]), math.floor(end_xyz[1]), math.floor(end_xyz[2])
    skip_cells = {(bx, by, bz), (bx, by + 1, bz), (hx, hy, hz), (hx, hy + 1, hz)}

    steps = max(2, int(math.ceil(dist * 2.0)))
    visited: set[tuple[int, int, int]] = set()
    probes: list[tuple[int, int, int]] = []
    for idx in range(1, steps):
        t = idx / steps
        cell = (
            math.floor(sx + (ex - sx) * t),
            math.floor(sy + (ey - sy) * t),
            math.floor(sz + (ez - sz) * t),
        )
        if cell in skip_cells or cell in visited:
            continue
        visited.add(cell)
        probes.append(cell)
        if len(probes) >= 18:
            break

    for vx, vy, vz in probes:
        bname = _cached_block_name(bot, vx, vy, vz, block_cache)
        if bname == "unknown":
            return None
        if not is_passable_sight_block(bname):
            return {"name": bname, "pos": [vx, vy, vz]}
    return None


def inspect_local_enclosure(
    bot: Any,
    pos: list[float] | tuple[float, ...],
    block_cache: dict[tuple[int, int, int], str] | None = None,
) -> dict[str, Any]:
    """Classify local 3D geometry as sealed_bunker, carved_shaft, open_cave, surface, or unknown."""
    cache = block_cache if block_cache is not None else {}
    bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))

    ceiling_dy: int | None = None
    ceiling_block: str | None = None
    overhead_known = False
    for dy in (2, 3, 4):
        b = _cached_block_name(bot, bx, by + dy, bz, cache)
        if b != "unknown":
            overhead_known = True
        if b != "unknown" and b not in PASSABLE_WALK_BLOCKS and not b.endswith("_leaves"):
            ceiling_dy = dy
            ceiling_block = b
            break
    roofed = ceiling_dy is not None

    complete = overhead_known
    solid_wall_dirs: list[str] = []
    up_open_dirs: list[str] = []
    flat_or_down_dirs: list[str] = []
    open_other_dirs: list[str] = []

    for d_name, (dx, dz) in (("north", (0, -1)), ("south", (0, 1)), ("east", (1, 0)), ("west", (-1, 0))):
        b0 = _cached_block_name(bot, bx + dx, by, bz + dz, cache)
        b1 = _cached_block_name(bot, bx + dx, by + 1, bz + dz, cache)
        if "unknown" in (b0, b1):
            complete = False
        elif b0 not in PASSABLE_WALK_BLOCKS and b1 not in PASSABLE_WALK_BLOCKS:
            solid_wall_dirs.append(d_name)
        elif b0 not in PASSABLE_WALK_BLOCKS and b1 in PASSABLE_WALK_BLOCKS:
            b2 = _cached_block_name(bot, bx + dx, by + 2, bz + dz, cache)
            if b2 in PASSABLE_WALK_BLOCKS:
                up_open_dirs.append(d_name)
            else:
                open_other_dirs.append(d_name)
        else:
            flat_or_down_dirs.append(d_name)

    open_dirs = up_open_dirs + flat_or_down_dirs + open_other_dirs
    if not complete:
        structure = "unknown"
        label_zh = "未知地形"
    elif roofed and len(solid_wall_dirs) == 4:
        structure = "sealed_bunker"
        label_zh = "封闭掩体"
    elif roofed and len(solid_wall_dirs) >= 2:
        open_desc = "/".join(f"{d}上行" if d in up_open_dirs else d for d in open_dirs) or "无"
        structure = "carved_shaft"
        label_zh = f"探矿穴({len(solid_wall_dirs)}面岩壁,开:{open_desc})"
    elif roofed:
        structure = "open_cave"
        label_zh = "宽敞矿洞"
    else:
        structure = "surface"
        label_zh = "露天"

    return {
        "complete": complete,
        "structure": structure,
        "roofed": roofed,
        "ceiling_dy": ceiling_dy,
        "ceiling_block": ceiling_block,
        "solid_wall_dirs": solid_wall_dirs,
        "up_open_dirs": up_open_dirs,
        "open_dirs": open_dirs,
        "label_zh": label_zh,
    }


def assess_hostile_occlusion(
    bot: Any,
    pos: list[float] | tuple[float, ...],
    *,
    radius: int = 20,
    fallback_nearest: dict[str, Any] | None = None,
    block_cache: dict[tuple[int, int, int], str] | None = None,
) -> dict[str, Any]:
    """Separate truly unoccluded nearby hostiles from wall-occluded mobs via 3D raycasts."""
    cache = block_cache if block_cache is not None else {}
    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])

    complete = True
    try:
        raw_list = bot.call("get_entities", {"radius": radius, "types": ["hostile"]}).get("entities") or []
    except ApiError:
        complete = False
        raw_list = []

    merged: list[dict[str, Any]] = [e for e in raw_list if isinstance(e, dict)]
    seen_ids = {e.get("id") for e in merged if e.get("id") is not None}
    if isinstance(fallback_nearest, dict) and fallback_nearest.get("name"):
        fn_pos = fallback_nearest.get("pos")
        fn_id = fallback_nearest.get("id")
        if fn_id is None or fn_id not in seen_ids:
            if isinstance(fn_pos, (list, tuple)) and len(fn_pos) >= 3:
                merged.append(fallback_nearest)
            else:
                complete = False

    candidates: list[tuple[float, dict[str, Any], tuple[float, float, float]]] = []
    for ent in merged:
        epos = ent.get("pos")
        if not isinstance(epos, (list, tuple)) or len(epos) < 3:
            complete = False
            continue
        try:
            ex, ey, ez = float(epos[0]), float(epos[1]), float(epos[2])
        except (TypeError, ValueError):
            complete = False
            continue
        if not all(math.isfinite(v) for v in (ex, ey, ez)):
            complete = False
            continue
        candidates.append((math.dist((px, py, pz), (ex, ey, ez)), ent, (ex, ey, ez)))

    candidates.sort(key=lambda row: row[0])
    if len(candidates) > 5:
        complete = False

    unoccluded: list[dict[str, Any]] = []
    occluded: list[dict[str, Any]] = []
    for dist3d, ent, (ex, ey, ez) in candidates[:5]:
        blocker = ray_wall_blocker(bot, (px, py, pz), (ex, ey, ez), block_cache=cache)
        entry = {
            "id": ent.get("id"),
            "name": str(ent.get("name") or "").removeprefix("minecraft:"),
            "dist": round(dist3d, 1),
            "dy": round(ey - py, 1),
            "bearing": cardinal_bearing(ex - px, ez - pz),
            "pos": [ex, ey, ez],
            "blocked_by_wall": blocker is not None,
            "blocker": blocker,
        }
        if blocker is not None:
            occluded.append(entry)
        else:
            unoccluded.append(entry)

    return {
        "complete": complete,
        "unoccluded": unoccluded,
        "occluded": occluded,
        "checked": len(unoccluded) + len(occluded),
    }


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


_TRASH_TIER1_ZERO_KEEP: tuple[str, ...] = (
    "wildflowers",
    "leaf_litter",
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    "dead_bush",
    "dandelion",
    "poppy",
    "oxeye_daisy",
    "cornflower",
    "azure_bluet",
    "lily_of_the_valley",
    "blue_orchid",
    "allium",
    "kelp",
    "seagrass",
    "rotten_flesh",
    "poisonous_potato",
    "spider_eye",
    "wheat_seeds",
    "beetroot_seeds",
    "melon_seeds",
    "pumpkin_seeds",
    "oak_sapling",
    "birch_sapling",
    "spruce_sapling",
    "dark_oak_sapling",
    "jungle_sapling",
    "acacia_sapling",
    "cherry_sapling",
    "pale_oak_sapling",
    "egg",
    "feather",
    "snowball",
)

_TRASH_TIER2_DECOR_STONES: tuple[str, ...] = (
    "diorite",
    "granite",
    "andesite",
    "tuff",
    "calcite",
    "dripstone_block",
)

_TRASH_TIER2_LOOSE_BLOCKS: tuple[str, ...] = (
    "sand",
    "red_sand",
    "gravel",
)

_TRASH_TIER3_DIRT: tuple[str, ...] = (
    "dirt",
    "coarse_dirt",
    "mud",
)

_TRASH_TIER4_COBBLE: tuple[str, ...] = (
    "cobblestone",
    "cobbled_deepslate",
)

_TRASH_TIER_NETHER: tuple[str, ...] = ("netherrack",)

_UNSTACKABLE_ITEMS: frozenset[str] = frozenset(
    {
        *PICKAXE_TIERS.keys(),
        *AXE_PRIORITY,
        *MELEE_WEAPONS_PRIORITY,
        "wooden_shovel",
        "stone_shovel",
        "iron_shovel",
        "diamond_shovel",
        "netherite_shovel",
        "wooden_hoe",
        "stone_hoe",
        "iron_hoe",
        "diamond_hoe",
        "netherite_hoe",
        "iron_helmet",
        "iron_chestplate",
        "iron_leggings",
        "iron_boots",
        "diamond_helmet",
        "diamond_chestplate",
        "diamond_leggings",
        "diamond_boots",
        "shield",
        "bow",
        "fishing_rod",
        "shears",
        "flint_and_steel",
        "bucket",
        "water_bucket",
        "lava_bucket",
    }
)

_CRAFT_SLOT_INPUTS: dict[str, dict[str, int]] = {
    "crafting_table": {"_planks_any": 4},
    "stick": {"_planks_any": 2},
    "furnace": {"cobblestone": 8},
    "chest": {"_planks_any": 8},
    "shield": {"_planks_any": 6, "iron_ingot": 1},
    "bucket": {"iron_ingot": 3},
    "torch": {"coal": 1, "stick": 1},
    "wooden_pickaxe": {"_planks_any": 3, "stick": 2},
    "stone_pickaxe": {"cobblestone": 3, "stick": 2},
    "iron_pickaxe": {"iron_ingot": 3, "stick": 2},
    "diamond_pickaxe": {"diamond": 3, "stick": 2},
    "wooden_sword": {"_planks_any": 2, "stick": 1},
    "stone_sword": {"cobblestone": 2, "stick": 1},
    "iron_sword": {"iron_ingot": 2, "stick": 1},
    "diamond_sword": {"diamond": 2, "stick": 1},
    "wooden_axe": {"_planks_any": 3, "stick": 2},
    "stone_axe": {"cobblestone": 3, "stick": 2},
    "iron_axe": {"iron_ingot": 3, "stick": 2},
    "diamond_axe": {"diamond": 3, "stick": 2},
    "wooden_shovel": {"_planks_any": 1, "stick": 2},
    "stone_shovel": {"cobblestone": 1, "stick": 2},
    "iron_shovel": {"iron_ingot": 1, "stick": 2},
    "diamond_shovel": {"diamond": 1, "stick": 2},
    "wooden_hoe": {"_planks_any": 2, "stick": 2},
    "stone_hoe": {"cobblestone": 2, "stick": 2},
    "iron_hoe": {"iron_ingot": 2, "stick": 2},
    "diamond_hoe": {"diamond": 2, "stick": 2},
    "iron_helmet": {"iron_ingot": 5},
    "iron_chestplate": {"iron_ingot": 8},
    "iron_leggings": {"iron_ingot": 7},
    "iron_boots": {"iron_ingot": 4},
    "diamond_helmet": {"diamond": 5},
    "diamond_chestplate": {"diamond": 8},
    "diamond_leggings": {"diamond": 7},
    "diamond_boots": {"diamond": 4},
}


def craft_material_shortage(item: str, count: int, counts: dict[str, int]) -> dict[str, Any] | None:
    """Use the curated recipes and existing slot recipes, with material tags."""
    legacy = material_shortage(item, count, counts)
    if legacy is not None:
        return legacy
    recipe = RECIPES_1_21.get(item)
    if recipe is None:
        inputs = _CRAFT_SLOT_INPUTS.get(item)
        if item == "stone_bricks":
            recipe = {"ingredients": {"stone": 4}, "yield_count": 4}
        elif inputs:
            recipe = {"ingredients": {("any_planks" if key == "_planks_any" else key): value for key, value in inputs.items()}, "yield_count": 1}
        else:
            return None
    batches = math.ceil(max(1, int(count)) / max(1, int(recipe.get("yield_count", 1))))
    required = {name: int(amount) * batches for name, amount in recipe["ingredients"].items()}

    def _available(name: str) -> int:
        if name == "any_planks":
            return sum(v for k, v in counts.items() if k.endswith("_planks"))
        if name == "any_log":
            return sum(v for k, v in counts.items() if k.endswith(("_log", "_stem", "_wood", "_hyphae")))
        if name == "coal_or_charcoal":
            return counts.get("coal", 0) + counts.get("charcoal", 0)
        if name == "any_wooden_slab":
            woods = {n.removesuffix("_log") for n in LOG_TYPES} | {"crimson", "warped", "bamboo"}
            return sum(counts.get(f"{wood}_slab", 0) for wood in woods)
        if name == "cobblestone" and (item.startswith("stone_") or item == "furnace"):
            return sum(counts.get(n, 0) for n in ("cobblestone", "cobbled_deepslate", "blackstone"))
        return counts.get(name, 0)

    available = {name: _available(name) for name in required}
    missing = {name: amount - available[name] for name, amount in required.items() if available[name] < amount}
    return {
        "item": item, "target_count": count,
        "required_materials": required, "available_materials": available,
        "missing_materials": missing,
        "recipe_note": ("cobblestone 可由 cobbled_deepslate、blackstone 替代。"
                        if "cobblestone" in required and (item.startswith("stone_") or item == "furnace")
                        else str(recipe.get("notes") or "any_planks 可混用木板；材料需求已按配方产量换算。")),
    }


def explain_material_craft_error(item: str, count: int, error: dict[str, Any]) -> dict[str, Any]:
    enriched = explain_craft_error(item, count, error)
    if error.get("code") != "missing_ingredients" or not isinstance(error.get("inventory"), dict):
        return enriched
    plan = craft_material_shortage(resolve_item_alias(item), count, error["inventory"])
    if plan is None:
        return enriched
    enriched = {**enriched, **plan}
    if "example_recipe_needs" in enriched:
        enriched["server_recipe_example"] = enriched.pop("example_recipe_needs")
    if not plan["missing_materials"] and "server placed no result" in str(error.get("message", "")):
        enriched["code"] = "server_recipe_rejected"
    return enriched


def _compute_empty_slots(inv: dict[str, Any] | None) -> int:
    if not isinstance(inv, dict):
        return 36
    raw_empty = inv.get("empty_slots")
    if isinstance(raw_empty, int) and not isinstance(raw_empty, bool):
        return max(0, raw_empty)
    occupied = sum(
        1
        for row in (inv.get("items") or [])
        if isinstance(row, dict) and row.get("name") and int(row.get("count") or 0) > 0
    )
    return max(0, 36 - occupied)


def _is_stackable_item(item: str) -> bool:
    norm = str(item or "").removeprefix("minecraft:")
    if norm in _UNSTACKABLE_ITEMS or norm.endswith(("_bed", "_boat", "_pickaxe", "_sword", "_axe", "_shovel", "_hoe", "_helmet", "_chestplate", "_leggings", "_boots")):
        return False
    return True


def _can_fit_item_in_inventory(inv: dict[str, Any] | None, item: str, count: int = 1, *, check_craft_inputs: bool = True) -> bool:
    """Return True if adding `count` of `item` (or crafting it) will fit without hanging a 36/36 full inventory."""
    if _compute_empty_slots(inv) >= 1:
        return True
    if not isinstance(inv, dict):
        return False
    norm = str(item or "").removeprefix("minecraft:")
    rows = [r for r in (inv.get("items") or []) if isinstance(r, dict) and r.get("name") and int(r.get("count") or 0) > 0]
    if _is_stackable_item(norm):
        spare = sum(
            max(0, 64 - int(r.get("count") or 0))
            for r in rows
            if str(r.get("name")).removeprefix("minecraft:") == norm
        )
        if spare >= max(1, int(count)):
            return True
    if not check_craft_inputs:
        return False

    bag_counts: dict[str, int] = {}
    for r in rows:
        iname = str(r["name"]).removeprefix("minecraft:")
        bag_counts[iname] = bag_counts.get(iname, 0) + int(r.get("count") or 0)

    if norm.endswith("_planks"):
        stem = norm.removesuffix("_planks")
        log_name = f"{stem}_stem" if stem in ("crimson", "warped") else f"{stem}_log"
        need_logs = max(1, math.ceil(max(1, int(count)) / 4))
        if 0 < bag_counts.get(log_name, 0) <= need_logs:
            return True
        return False

    recipe = _CRAFT_SLOT_INPUTS.get(norm)
    if recipe is None:
        return False
    batches = max(1, math.ceil(max(1, int(count)) / 4)) if norm in ("stick", "torch") else max(1, int(count))
    total_planks = sum(v for k, v in bag_counts.items() if k.endswith("_planks"))
    for ing, per_batch in recipe.items():
        need = per_batch * batches
        if ing == "_planks_any":
            if 0 < total_planks <= need:
                return True
        elif 0 < bag_counts.get(ing, 0) <= need:
            return True
    return False


def _yaw_toward(px: float, pz: float, tx: float, tz: float) -> float:
    """Compute Mineflayer yaw (rad, forward=(-sin, -cos)) pointing toward (tx, tz)."""
    return math.atan2(-(tx - px), -(tz - pz))


_CONTAINER_TYPES: tuple[str, ...] = (
    "chest",
    "barrel",
    "trapped_chest",
    "shulker_box",
    "hopper",
    "dispenser",
    "dropper",
)

# input item -> furnace output (1.21 vanilla)
SMELT_OUTPUTS: dict[str, str] = {
    "cobblestone": "stone",
    "cobbled_deepslate": "deepslate",
    "sand": "glass",
    "red_sand": "glass",
    "clay_ball": "brick",
    "clay": "terracotta",
    "raw_iron": "iron_ingot",
    "raw_gold": "gold_ingot",
    "raw_copper": "copper_ingot",
    "iron_ore": "iron_ingot",
    "gold_ore": "gold_ingot",
    "copper_ore": "copper_ingot",
    "deepslate_iron_ore": "iron_ingot",
    "deepslate_gold_ore": "gold_ingot",
    "deepslate_copper_ore": "copper_ingot",
    "nether_gold_ore": "gold_ingot",
    "ancient_debris": "netherite_scrap",
    "netherrack": "nether_brick",
    "porkchop": "cooked_porkchop",
    "beef": "cooked_beef",
    "chicken": "cooked_chicken",
    "mutton": "cooked_mutton",
    "rabbit": "cooked_rabbit",
    "cod": "cooked_cod",
    "salmon": "cooked_salmon",
    "potato": "baked_potato",
    "kelp": "dried_kelp",
    "cactus": "green_dye",
    "sea_pickle": "lime_dye",
    "wet_sponge": "sponge",
    "chorus_fruit": "popped_chorus_fruit",
    "oak_log": "charcoal",
    "spruce_log": "charcoal",
    "birch_log": "charcoal",
    "jungle_log": "charcoal",
    "acacia_log": "charcoal",
    "dark_oak_log": "charcoal",
    "mangrove_log": "charcoal",
    "cherry_log": "charcoal",
    "pale_oak_log": "charcoal",
}


def _merge_counts(target: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    for name, cnt in extra.items():
        target[name] = target.get(name, 0) + int(cnt)
    return target


def _clean_block(raw: Any) -> str:
    return str(raw or "").removeprefix("minecraft:").strip().lower()



_HIGH_VALUE_DROPS: frozenset[str] = frozenset({
    "diamond",
    "raw_iron",
    "iron_ingot",
    "iron_ore",
    "deepslate_iron_ore",
    "raw_gold",
    "gold_ingot",
    "iron_pickaxe",
    "diamond_pickaxe",
})
_MID_VALUE_DROPS: frozenset[str] = frozenset({
    "coal",
    "charcoal",
    "raw_copper",
    "lapis_lazuli",
    "redstone",
})


def _normalize_drop_name(raw_name: Any) -> str:
    """Normalize dropped item labels like 'minecraft:raw_ironx2' -> 'raw_iron'."""
    name = str(raw_name or "").strip().removeprefix("minecraft:")
    if "x" in name:
        stem, suffix = name.rsplit("x", 1)
        if stem and suffix.isdigit():
            return stem
    return name


def _summarize_uncollected_drops(
    bot_pos: list[float] | None,
    attempts: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build directional recovery guidance for ground items that were skipped or stopped out of reach."""
    candidates: list[dict[str, Any]] = []
    for row in skipped:
        pos = row.get("item_pos")
        if row.get("reason") == "height_difference" and isinstance(pos, list) and len(pos) >= 3:
            candidates.append(row)
    for row in attempts:
        pos = row.get("item_pos")
        if not (isinstance(pos, list) and len(pos) >= 3):
            continue
        dist_after = row.get("distance_after")
        if (
            row.get("approach_error")
            or row.get("closer_error")
            or dist_after is None
            or float(dist_after) > 1.35
        ):
            candidates.append(row)
    if not candidates:
        return None

    bp = bot_pos if isinstance(bot_pos, list) and len(bot_pos) >= 3 else [0.0, 64.0, 0.0]
    cx, cy, cz = float(bp[0]), float(bp[1]), float(bp[2])

    def _rank(row: dict[str, Any]) -> tuple[int, float]:
        norm = _normalize_drop_name(row.get("item"))
        if norm in _HIGH_VALUE_DROPS:
            tier = 0
        elif norm in _MID_VALUE_DROPS or norm.endswith(("_log", "_stem")):
            tier = 1
        else:
            tier = 2
        p = row["item_pos"]
        return (tier, math.dist([cx, cy, cz], [float(p[0]), float(p[1]), float(p[2])]))

    candidates.sort(key=_rank)
    best = candidates[0]
    tx, ty, tz = (round(float(v), 1) for v in best["item_pos"][:3])
    dx, dy, dz = tx - cx, ty - cy, tz - cz
    bearing = cardinal_bearing(dx, dz)
    if dy <= -2.0:
        steps_down = min(10, max(3, int(abs(dy) + 1)))
        approach_hint = (
            f"地面掉落物 {best.get('item')} 掉落在下方低处 [{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})；"
            f"请朝 {bearing} 调 mc_skill_staircase(direction='{bearing}', vertical='down', steps={steps_down}, stop_on_ore=False) "
            "安全下探到该高度后再调 mc_skill_collect_drops 拾取，切勿继续在悬崖上方挖掘以免更多掉落物坠坑。"
        )
    elif dy >= 2.0:
        steps_up = min(10, max(3, int(abs(dy) + 1)))
        approach_hint = (
            f"地面掉落物 {best.get('item')} 位于上方高处 [{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})；"
            f"请朝 {bearing} 调 mc_skill_staircase(direction='{bearing}', vertical='up', steps={steps_up}, stop_on_ore=False) "
            "上行接近后再调 mc_skill_collect_drops 拾取。"
        )
    else:
        approach_hint = (
            f"地面掉落物 {best.get('item')} 位于同层附近 [{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f}) "
            "但被边缘地形卡住未入包；请微调站位或清理脚边阻挡方块后再调 mc_skill_collect_drops。"
        )
    return {
        "nearest": {
            "item": best.get("item"),
            "pos": [tx, ty, tz],
            "bearing": bearing,
            "dy": round(dy, 1),
        },
        "items": [
            {"item": r.get("item"), "pos": [round(float(v), 1) for v in r["item_pos"][:3]]}
            for r in candidates[:5]
        ],
        "approach_hint": approach_hint,
    }


class _StairDeadline(Exception):
    """Stop the whole staircase, including helpers that catch ApiError."""


class _StairSession:
    """Per-invocation deadline; never patch the shared BotSession/guardian."""

    def __init__(self, bot: BotSession, duration_s: float, *, deadline: float | None = None, per_step_timeout: float = 15.0) -> None:
        self.bot = bot
        params = inspect.signature(bot.call).parameters
        self.supports_timeout = "timeout_s" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        self.deadline = min(time.monotonic() + duration_s, float(deadline)) if deadline is not None else time.monotonic() + duration_s
        self.step_deadline = self.deadline
        self.per_step_timeout = max(0.05, float(per_step_timeout))
        self.last_status: dict[str, Any] = {}
        self.last_timeout_path: str | None = None
        self.last_retry_path: str | None = None
        self.last_dig_pos: list[Any] | None = None
        self.last_dig_block: str | None = None
        self.pickaxe_tier: int | None = None
        self.progress: dict[str, Any] = {"steps_done": 0, "dug_count": 0}

    _IDEMPOTENT_READS = frozenset({"status", "inventory", "get_block", "find_blocks"})

    def call(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        # A slow first status/probe under fleet load used to abort the whole
        # staircase as time_budget with zero steps. Retry one idempotent read
        # once; action calls still abort immediately (their lease may persist).
        explicit_timeout = kwargs.pop("timeout_s", None)
        for read_attempt in range(2):
            remaining = min(self.deadline, self.step_deadline) - time.monotonic()
            if remaining <= 0:
                raise _StairDeadline()
            # Obsidian needs ~9.4s with a diamond pickaxe (r14: fortress
            # staircase timed out on every first dig at the old 8s cap).
            cap = 6.0 if path == "move_to" else 15.0 if path == "dig" else 3.0
            timeout = min(remaining, cap, float(explicit_timeout if explicit_timeout is not None else cap))
            call_body = body
            if path == "move_to":
                call_body = {**(body or {}), "timeout": max(1, int(max(0.001, timeout - 0.2) * 1000))}
            elif path == "dig" and isinstance((body or {}).get("pos"), (list, tuple)):
                self.last_dig_pos = list(body["pos"])
            try:
                result = (self.bot.call(path, call_body, timeout_s=timeout, **kwargs)
                          if self.supports_timeout else self.bot.call(path, call_body))
            except httpx.TimeoutException:
                if read_attempt == 0 and path in self._IDEMPOTENT_READS and remaining > 1.0:
                    self.last_retry_path = path
                    continue
                self.last_timeout_path = path
                # A client timeout does not release the server action lease.
                raise _StairDeadline() from None
            break
        if path == "status":
            self.last_status = result
            if isinstance(result.get("pos"), (list, tuple)):
                self.progress.setdefault("pos_before", list(result["pos"]))
        elif path in {"move_to", "control"} and isinstance(result.get("pos"), (list, tuple)):
            self.last_status = {**self.last_status, "pos": list(result["pos"])}
        if path == "dig":
            self.progress["dug_count"] += 1
        elif path == "place":
            self.progress["placed_count"] = self.progress.get("placed_count", 0) + 1
        if time.monotonic() >= min(self.deadline, self.step_deadline):
            raise _StairDeadline()
        return result

    def _read(self, path: str) -> dict[str, Any]:
        if isinstance(self.bot, BotSession):
            return self.call(path, method="GET")
        # Lightweight in-process adapters expose reads as methods rather than
        # API paths. They still share the same checks before and after each read.
        if time.monotonic() >= min(self.deadline, self.step_deadline):
            raise _StairDeadline()
        result = getattr(self.bot, path)()
        if path == "status":
            self.last_status = result
            if isinstance(result.get("pos"), (list, tuple)):
                self.progress.setdefault("pos_before", list(result["pos"]))
        if time.monotonic() >= min(self.deadline, self.step_deadline):
            raise _StairDeadline()
        return result

    def status(self) -> dict[str, Any]:
        return self._read("status")

    def inventory(self) -> dict[str, Any]:
        return self._read("inventory")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bot, name)


class MacroSkills:
    """Stateful skills with permanent protection and separate expiring failure records."""

    def __init__(self, bot: BotSession) -> None:
        self.bot = bot
        self.protected_positions: set[tuple[int, int, int]] = set()
        self.temporary_avoidance: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.failed_stair_dirs: dict[tuple[int, int, int, str], dict[str, tuple[float, str]]] = {}
        self.failed_tunnel_dirs: dict[tuple[int, int, int], dict[str, tuple[float, str]]] = {}
        self._mining_dfs: Any = None
        self.world_memory: Any = None
        self._terrain_nav: Any = None
        self.inventory_manager: Any = None

    def _get_terrain_nav(self) -> Any:
        """Lazily build the L2 local-terrain navigator (breadcrumbs + 5 primitives)."""
        if self._terrain_nav is None:
            from .movement import TerrainNavigator

            self._terrain_nav = TerrainNavigator(self.bot, skills=self)
        return self._terrain_nav

    def _get_world_memory(self) -> Any:
        if self.world_memory is None:
            from ..harness.world_memory import WorldMemory

            self.world_memory = WorldMemory()
        return self.world_memory

    def navigate_segmented(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        range_m: float = 2.5,
        step_m: float = 24.0,
        geofence: Any = None,
        ignore_hurt: bool = False,
        skip_direct: bool = False,
        hop_timeout_ms: int = 20000,
    ) -> dict[str, Any]:
        """Relay long-distance navigation (>36m) in ~24m segments with ±35 deg detour fallback."""
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        cx, cy, cz = float(pos0[0]), float(pos0[1]), float(pos0[2])
        total_hdist = math.hypot(float(x) - cx, float(z) - cz)
        stop_on = ["death"] if (ignore_hurt or st0.get("in_water")) else ["hurt", "death"]

        if total_hdist <= 36.0:
            payload: dict[str, Any] = {
                "x": float(x),
                "z": float(z),
                "range": range_m,
                "timeout": max(4000, int(hop_timeout_ms)),
                "stop_on": stop_on,
            }
            if y is not None:
                payload["y"] = float(y)
            res = self.bot.call("move_to", payload)
            nav = self._get_terrain_nav()
            nav.note_status()
            return {"ok": True, "segmented": False, "segments_done": 1, "result": res}

        last_err: ApiError = ApiError("path_not_found", f"unable to reach ({x:.1f}, {z:.1f})")
        if not skip_direct:
            # Try direct first in case chunks are already loaded and unobstructed
            try:
                direct_payload: dict[str, Any] = {
                    "x": float(x),
                    "z": float(z),
                    "range": range_m,
                    "stop_on": stop_on,
                    "timeout": 30000,
                }
                if y is not None:
                    direct_payload["y"] = float(y)
                res_d = self.bot.call("move_to", direct_payload, timeout_s=42.0)
                return {"ok": True, "segmented": False, "segments_done": 1, "result": res_d}
            except ApiError as direct_err:
                if direct_err.code in {"hurt", "death", "interrupted"}:
                    raise
                if direct_err.code == "busy":
                    self._get_terrain_nav().release_stuck_action()
                last_err = direct_err
            except Exception as exc:  # noqa: BLE001 - long direct path may outlive the HTTP timeout
                last_err = ApiError(
                    code="timeout",
                    message=f"direct move_to HTTP timeout ({type(exc).__name__}); fall back to segmented hops",
                )

        max_hops = min(18, max(3, int(math.ceil(total_hdist / max(10.0, step_m))) + 3))
        segs_done = 0
        nav = self._get_terrain_nav()
        recoveries: list[dict[str, Any]] = []
        for _ in range(max_hops):
            st_cur = self.bot.status()
            p_cur = st_cur.get("pos") or [cx, cy, cz]
            cx, cy, cz = float(p_cur[0]), float(p_cur[1]), float(p_cur[2])
            rem = math.hypot(float(x) - cx, float(z) - cz)
            if rem <= max(range_m + 1.0, 3.5):
                return {
                    "ok": True,
                    "segmented": True,
                    "segments_done": segs_done,
                    "pos": p_cur,
                    "recoveries": recoveries,
                }

            if rem <= step_m + 4.0:
                hop_targets = [(float(x), float(z), y, range_m)]
            else:
                base_ang = math.atan2(float(z) - cz, float(x) - cx)
                hop_targets = [
                    (cx + math.cos(base_ang + ang_off) * step_m, cz + math.sin(base_ang + ang_off) * step_m, None, 3.0)
                    for ang_off in (0.0, 0.61, -0.61)
                ]

            advanced = False
            for hx, hz, hy, hr in hop_targets:
                if geofence is not None and geofence.check_point(hx, hz, action="navigate_segmented") is not None:
                    continue
                hop_payload: dict[str, Any] = {
                    "x": round(hx, 2),
                    "z": round(hz, 2),
                    "range": hr,
                    "stop_on": stop_on,
                    "timeout": max(4000, int(hop_timeout_ms)),
                }
                if hy is not None:
                    hop_payload["y"] = float(hy)
                try:
                    self.bot.call("move_to", hop_payload)
                    st_after = self.bot.status()
                    p_after = st_after.get("pos") or [cx, cy, cz]
                    progress = math.hypot(float(p_after[0]) - cx, float(p_after[2]) - cz)
                    hop_dist = math.hypot(float(hx) - cx, float(hz) - cz)
                    # A 24m hop that only creeps 1-2m is a stall, not progress: hand it to the
                    # local-recovery state machine instead of burning the whole hop budget.
                    need_m = max(2.0, min(8.0, hop_dist * 0.3))
                    if progress >= need_m or math.hypot(
                        float(x) - float(p_after[0]), float(z) - float(p_after[2])
                    ) <= max(range_m + 1.0, 3.5):
                        segs_done += 1
                        advanced = True
                        break
                except ApiError as hop_err:
                    if hop_err.code in {"hurt", "death", "interrupted"}:
                        raise
                    if hop_err.code == "busy":
                        # a previously hung action can wedge the server lock: free it and retry once
                        nav.release_stuck_action()
                        try:
                            self.bot.call("move_to", hop_payload, timeout_s=40.0)
                        except ApiError as retry_err:
                            if retry_err.code in {"hurt", "death", "interrupted"}:
                                raise
                            last_err = retry_err
                            continue
                        except Exception:  # noqa: BLE001
                            last_err = ApiError(code="timeout", message="hop retry HTTP timeout")
                            continue
                    last_err = hop_err
                    continue
                except Exception as exc:  # noqa: BLE001 - treat a hung hop as a failed hop
                    last_err = ApiError(
                        code="timeout",
                        message=f"hop move_to HTTP timeout ({type(exc).__name__})",
                    )
                    continue
            if not advanced:
                if len(recoveries) < MAX_LOCAL_RECOVERY_ATTEMPTS:
                    rec = nav.recover(x=float(x), z=float(z), y=y, attempt=len(recoveries) + 1)
                    recoveries.append(
                        {
                            "op": rec.get("op"),
                            "diagnosis": rec.get("diagnosis"),
                            "ok": bool(rec.get("ok")),
                            "error": rec.get("error"),
                        }
                    )
                    if rec.get("ok"):
                        segs_done += 1
                        nav.note_status()
                    if rec.get("ok") or len(recoveries) < MAX_LOCAL_RECOVERY_ATTEMPTS:
                        continue
                raise ApiError(
                    code=last_err.code,
                    message=(
                        f"{last_err.message}（navigate_segmented 已完成 {segs_done} 段，"
                        f"局部恢复 {len(recoveries)} 次仍不通）"
                    ),
                    http_status=last_err.http_status,
                    ms=last_err.ms,
                    context={
                        **dict(last_err.context or {}),
                        "segments_done": segs_done,
                        "recoveries": recoveries,
                        "blocked_reason": (recoveries[-1] if recoveries else "no_recovery"),
                        "pos": self.bot.status().get("pos"),
                    },
                )
            nav.note_status()

        st_fin = self.bot.status()
        p_fin = st_fin.get("pos") or [cx, cy, cz]
        if math.hypot(float(x) - float(p_fin[0]), float(z) - float(p_fin[2])) <= max(range_m + 2.0, 4.5):
            return {"ok": True, "segmented": True, "segments_done": segs_done, "pos": p_fin}
        raise last_err

    def _ensure_free_slots(
        self,
        min_free: int = 3,
        inv_snapshot: dict[str, Any] | None = None,
        *,
        turn_away_yaw: float | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Drop low-value clutter deterministically when inventory is nearly full (< min_free empty slots)."""
        inv = inv_snapshot if isinstance(inv_snapshot, dict) else self.bot.inventory()
        if not isinstance(inv, dict):
            return {}, []
        empty_now = _compute_empty_slots(inv)
        pre_counts: dict[str, int] = {}
        for row in inv.get("items") or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            iname = str(row["name"]).removeprefix("minecraft:")
            if iname in _TRASH_TIER4_COBBLE or iname in _TRASH_TIER_NETHER or iname in _TRASH_TIER3_DIRT:
                pre_counts[iname] = pre_counts.get(iname, 0) + int(row.get("count") or 0)
        pre_over_cap = (
            any(pre_counts.get(n, 0) > 192 for n in _TRASH_TIER4_COBBLE)
            or any(pre_counts.get(n, 0) > 128 for n in _TRASH_TIER_NETHER)
            or any(pre_counts.get(n, 0) > 64 for n in _TRASH_TIER3_DIRT)
        )
        if empty_now >= min_free and not pre_over_cap:
            return inv, []

        max_drop_ops = 12
        dropped: list[dict[str, Any]] = []
        drop_ops = 0
        turned_away = False
        for _pass_idx in range(2):
            empty_now = _compute_empty_slots(inv)
            rows = [
                r
                for r in (inv.get("items") or [])
                if isinstance(r, dict) and r.get("name") and int(r.get("count") or 0) > 0
            ]
            per_item_counts: dict[str, int] = {}
            per_item_slots: dict[str, list[int]] = {}
            for r in rows:
                iname = str(r["name"]).removeprefix("minecraft:")
                cnt = int(r.get("count") or 0)
                per_item_counts[iname] = per_item_counts.get(iname, 0) + cnt
                per_item_slots.setdefault(iname, []).append(cnt)

            over_cap = (
                any(per_item_counts.get(n, 0) > 192 for n in _TRASH_TIER4_COBBLE)
                or any(per_item_counts.get(n, 0) > 128 for n in _TRASH_TIER_NETHER)
                or any(per_item_counts.get(n, 0) > 64 for n in _TRASH_TIER3_DIRT)
            )
            if (empty_now >= min_free and not over_cap) or drop_ops >= max_drop_ops:
                break

            cobble_total = per_item_counts.get("cobblestone", 0) + per_item_counts.get("cobbled_deepslate", 0)
            tier2_keep = 0 if cobble_total >= 64 else 64
            plan_specs: list[tuple[str, int]] = [
                *((name, 0) for name in _TRASH_TIER1_ZERO_KEEP),
                *((name, tier2_keep) for name in _TRASH_TIER2_DECOR_STONES),
                *((name, tier2_keep) for name in _TRASH_TIER2_LOOSE_BLOCKS),
                *((name, 64) for name in _TRASH_TIER3_DIRT),
                *((name, 128) for name in _TRASH_TIER_NETHER),
                *((name, 192) for name in _TRASH_TIER4_COBBLE),
            ]
            est_free = empty_now
            made_drop = False
            for name, keep in plan_specs:
                is_capped = (
                    name in _TRASH_TIER4_COBBLE
                    or name in _TRASH_TIER_NETHER
                    or name in _TRASH_TIER3_DIRT
                )
                total = per_item_counts.get(name, 0)
                if drop_ops >= max_drop_ops or (est_free >= min_free and not (is_capped and total > keep)):
                    if drop_ops >= max_drop_ops:
                        break
                    if est_free >= min_free and not over_cap:
                        break
                    continue
                excess = total - keep
                if excess <= 0:
                    continue
                slot_list = per_item_slots.get(name) or [excess]
                smallest_slot = min(slot_list)
                # Avoid tiny remainder drops unless keeping=0 or freeing at least 1 full slot.
                if keep > 0 and excess < smallest_slot and len(slot_list) <= 1:
                    continue
                target_drop = (
                    excess
                    if (keep == 0 or is_capped)
                    else min(excess, max(0, min_free - est_free) * 64)
                )
                if keep > 0 and target_drop < smallest_slot:
                    continue
                remaining = target_drop
                while (
                    remaining > 0
                    and drop_ops < max_drop_ops
                    and (est_free < min_free or (is_capped and total > keep))
                ):
                    if turn_away_yaw is not None and not turned_away:
                        turned_away = True
                        try:
                            self.bot.call("look", {"yaw": float(turn_away_yaw), "pitch": 0.0})
                        except ApiError:
                            pass
                    batch = min(64, remaining)
                    try:
                        self.bot.call("drop", {"item": name, "count": batch})
                    except ApiError as err:
                        if err.code in ("interrupted", "dead", "disconnected"):
                            return self.bot.inventory(), dropped
                        break
                    drop_ops += 1
                    dropped.append({"item": name, "count": batch})
                    made_drop = True
                    remaining -= batch
                    total -= batch
                    per_item_counts[name] = total
                    slots_now = math.ceil(max(0, total) / 64)
                    if slots_now < len(slot_list):
                        est_free += len(slot_list) - slots_now
                        slot_list = [64] * slots_now
            if not made_drop:
                break
            inv = self.bot.inventory()
            if not isinstance(inv, dict):
                return {}, dropped
        return inv, dropped

    def _safe_craft(
        self,
        item: str,
        count: int = 1,
        *,
        inv_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Auto-clear clutter if full, fail fast when still un-fittable at empty_slots=0, else craft."""
        inv_now, dropped = self._ensure_free_slots(min_free=2, inv_snapshot=inv_snapshot)
        if _compute_empty_slots(inv_now) == 0 and not _can_fit_item_in_inventory(inv_now, item, count):
            raise ApiError(
                code="inventory_full",
                message=(
                    f"背包 36 格已满(empty_slots=0)且无可自动丢弃的杂物，直接合成 {item} x{count} "
                    "会导致 Mineflayer 无法存放产物而触发 30s 超时；请先消耗、放置或丢弃至少 1 格物品再重试。"
                ),
                context={"item": item, "count": count, "empty_slots": 0},
            )
        res = self.bot.call("craft", {"item": item, "count": count})
        if dropped and isinstance(res, dict):
            return {**res, "auto_dropped": dropped}
        return res

    def _record_failed_stair_dir(
        self,
        cell: tuple[int, int, int, str],
        d_key: str,
        reason: str = "failed",
        ttl_s: float = 180.0,
    ) -> None:
        bucket = self.failed_stair_dirs.setdefault(cell, {})
        bucket[d_key] = (time.monotonic() + ttl_s, reason)

    def _active_failed_stair_dirs(self, cell: tuple[int, int, int, str]) -> dict[str, str]:
        bucket = self.failed_stair_dirs.get(cell)
        if not bucket:
            return {}
        now = time.monotonic()
        expired = [k for k, (exp, _) in bucket.items() if exp <= now]
        for k in expired:
            del bucket[k]
        return {k: reason for k, (_, reason) in bucket.items()}

    def _record_failed_tunnel_dir(
        self,
        cell: tuple[int, int, int],
        d_key: str,
        reason: str = "failed",
        ttl_s: float = 180.0,
    ) -> None:
        bucket = self.failed_tunnel_dirs.setdefault(cell, {})
        bucket[d_key] = (time.monotonic() + ttl_s, reason)

    def _active_failed_tunnel_dirs(self, cell: tuple[int, int, int]) -> dict[str, str]:
        bucket = self.failed_tunnel_dirs.get(cell)
        if not bucket:
            return {}
        now = time.monotonic()
        expired = [k for k, (exp, _) in bucket.items() if exp <= now]
        for k in expired:
            del bucket[k]
        return {k: reason for k, (_, reason) in bucket.items()}

    def _probe_cached_block(
        self,
        pt: tuple[int, int, int],
        probe_cache: dict[tuple[int, int, int], str] | None = None,
    ) -> str:
        if probe_cache is not None and pt in probe_cache:
            return probe_cache[pt]
        res = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
        raw = res.get("name") or res.get("block")
        val = str(raw).removeprefix("minecraft:") if raw else "unknown"
        if probe_cache is not None:
            probe_cache[pt] = val
        return val

    def _probe_viable_tunnel_dirs(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        exclude_dirs: set[str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Probe 2-3 steps ahead for safe horizontal tunnel directions matching tunnel.py rules."""
        from . import tunnel as _tunnel_mod

        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        cache = probe_cache if probe_cache is not None else {}
        counts = inv_counts if inv_counts is not None else _inventory_counts(self.bot)
        pick, pick_tier = _best_pickaxe_in_inv(counts)
        if not pick:
            return []

        air_set = _tunnel_mod.AIR
        fluid_set = _tunnel_mod.FLUID
        ground_or_ore = _tunnel_mod.GROUND | _tunnel_mod.ORE
        falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
        failed_tunnel = self._active_failed_tunnel_dirs((bx, by, bz))
        excluded = set(exclude_dirs or ())

        def _step_safe(tx: int, tz: int) -> tuple[bool, bool]:
            floor = self._probe_cached_block((tx, by - 1, tz), cache)
            if floor not in ground_or_ore:
                return False, False
            faces = [(tx, by, tz), (tx, by + 1, tz)]
            f_names = [self._probe_cached_block(p, cache) for p in faces]
            for bname in f_names:
                if bname not in (air_set | ground_or_ore):
                    return False, False
                if bname not in air_set and BLOCK_MIN_PICKAXE_TIER.get(bname, 1) > pick_tier:
                    return False, False
            # Regardless of whether the face block is air or solid, check all 6 neighbors of both face blocks.
            neighbors = {
                (px + ax, py + ay, pz + az)
                for px, py, pz in faces
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
            }
            for nb in sorted(neighbors):
                nb_name = self._probe_cached_block(nb, cache)
                if nb_name in fluid_set or nb_name in falling_set:
                    return False, False
            has_solid = any(b not in air_set for b in f_names)
            return True, has_solid

        viable: list[str] = []
        for odir, (odx, odz) in dirs.items():
            if odir in excluded or odir in failed_tunnel:
                continue
            try:
                ok1, dig1 = _step_safe(bx + odx, bz + odz)
                if not ok1:
                    continue
                ok2, dig2 = _step_safe(bx + 2 * odx, bz + 2 * odz)
                if not ok2:
                    continue
                if not dig1 and not dig2:
                    ok3, _ = _step_safe(bx + 3 * odx, bz + 3 * odz)
                    if not ok3:
                        continue
                viable.append(odir)
            except ApiError:
                continue
        return viable

    def _probe_viable_up_stair_dirs(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        exclude_dirs: set[str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Probe which 4 cardinal directions can safely perform dig_staircase(vertical='up')."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        cache = probe_cache if probe_cache is not None else {}
        counts = inv_counts if inv_counts is not None else _inventory_counts(self.bot)
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
        has_filler = any(
            counts.get(c, 0) >= 1
            for c in STAIR_FILLER_BLOCKS
        ) or any(cnt >= 1 and name.endswith("_planks") for name, cnt in counts.items())

        def _non_air_has_adj_fluid(cells: list[tuple[int, int, int]]) -> bool:
            for cx, cy, cz in cells:
                if self._probe_cached_block((cx, cy, cz), cache) in air_set:
                    continue
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    if self._probe_cached_block((cx + ax, cy + ay, cz + az), cache) in fluids:
                        return True
            return False

        try:
            head_here = self._probe_cached_block((bx, by + 1, bz), cache)
            if head_here in fluid_or_bedrock:
                return []
            if _non_air_has_adj_fluid([(bx, by + 1, bz), (bx, by + 2, bz)]):
                return []
            if self._probe_cached_block((bx, by + 2, bz), cache) in fluid_or_bedrock:
                return []
            if self._probe_cached_block((bx, by + 3, bz), cache) in fluids:
                return []
        except ApiError:
            return []

        failed_up = self._active_failed_stair_dirs((bx, by, bz, "up"))
        excluded = set(exclude_dirs or ())
        viable_up: list[str] = []
        for odir, (odx, odz) in dirs.items():
            if odir in excluded or odir in failed_up:
                continue
            nx, nz = bx + odx, bz + odz
            try:
                if any(self._probe_cached_block(p, cache) in fluid_or_bedrock for p in ((nx, by + 1, nz), (nx, by + 2, nz))):
                    continue
                if self._probe_cached_block((nx, by + 3, nz), cache) in fluids:
                    continue
                if _non_air_has_adj_fluid([(nx, by + 1, nz), (nx, by + 2, nz)]):
                    continue
                step_sup = self._probe_cached_block((nx, by, nz), cache)
                if step_sup in fluid_or_bedrock:
                    continue
                is_non_solid = step_sup in non_solid or step_sup.endswith(("_leaves", "_sapling", "_carpet", "_banner"))
                if is_non_solid and not has_filler:
                    continue
                viable_up.append(odir)
            except ApiError:
                continue
        return viable_up

    def _probe_viable_down_stair_dirs(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        exclude_dirs: set[str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Probe which 4 cardinal directions can safely perform dig_staircase(vertical='down')."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        cache = probe_cache if probe_cache is not None else {}
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
        def _non_air_has_adj_fluid(cells: list[tuple[int, int, int]]) -> bool:
            for cx, cy, cz in cells:
                if self._probe_cached_block((cx, cy, cz), cache) in air_set:
                    continue
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    if self._probe_cached_block((cx + ax, cy + ay, cz + az), cache) in fluids:
                        return True
            return False

        failed_down = self._active_failed_stair_dirs((bx, by, bz, "down"))
        excluded = set(exclude_dirs or ())
        viable_down: list[str] = []
        for odir, (odx, odz) in dirs.items():
            if odir in excluded or odir in failed_down:
                continue
            nx, nz = bx + odx, bz + odz
            try:
                dig_cells = [(nx, by + 1, nz), (nx, by, nz), (nx, by - 1, nz)]
                if any(self._probe_cached_block(p, cache) in fluid_or_bedrock for p in dig_cells):
                    continue
                if self._probe_cached_block((nx, by + 2, nz), cache) in fluids:
                    continue
                if _non_air_has_adj_fluid(dig_cells):
                    continue
                step_sup = self._probe_cached_block((nx, by - 2, nz), cache)
                if step_sup in fluid_or_bedrock:
                    continue
                is_non_solid = step_sup in non_solid or step_sup.endswith(("_leaves", "_sapling", "_carpet", "_banner"))
                if is_non_solid:
                    continue
                viable_down.append(odir)
            except ApiError:
                continue
        return viable_down

    def _enrich_ecology_unavailable(
        self,
        res: dict[str, Any],
        *,
        targets: list[str] | tuple[str, ...],
        radius: int,
        counts: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Enrich an empty local search with world ecology diagnostics and staircase directions."""
        if not callable(getattr(self.bot, "status", None)):
            return None
        try:
            st = self.bot.status() or {}
        except Exception:
            return None
        dim = normalize_dimension(st.get("dimension"))
        biome = str(st.get("biome") or "")
        pos = st.get("pos")
        has_pos = isinstance(pos, (list, tuple)) and len(pos) >= 3
        px, py, pz = ([int(math.floor(float(v))) for v in pos[:3]] if has_pos else [0, 64, 0])
        check = assess_world_target(
            list(targets),
            dimension=dim,
            y=(py if has_pos else None),
            biome=biome,
            radius=radius,
            mode="find",
        )
        if check.get("available") is not False:
            return None

        out: dict[str, Any] = {
            **res,
            "requires_replan": True,
            "ecology_check": check,
        }
        if has_pos:
            out["pos"] = [px, py, pz]
        if check.get("family_id") == "surface_trees" and check.get("verdict") == "below_generation_band":
            out["underground_no_trees"] = True

        action_hint = ""
        dir_needed = check.get("direction_needed")
        probe_cache: dict[tuple[int, int, int], str] = {}
        if has_pos and dir_needed == "up":
            up_dirs = self._probe_viable_up_stair_dirs(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
            vpillar = False if up_dirs else self._probe_viable_pillar_up(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
            out["viable_up_directions"] = up_dirs
            out["viable_pillar_up"] = vpillar
            rec_y = int(check.get("recommended_y") or 66)
            steps_up = min(20, max(12, rec_y - py))
            if up_dirs:
                action_hint = (
                    f"请先调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps={steps_up}) "
                    f"向上返回目标高度带(可选上行: {','.join(up_dirs)})。"
                )
            elif vpillar:
                action_hint = f"请先调 mc_skill_staircase(vertical='pillar', steps={min(10, steps_up)}) 原地垫脚直上脱困。"
            else:
                action_hint = "请先调 mc_skill_staircase(vertical='up', steps=16) 或 mc_skill_unstick 脱困。"
        elif has_pos and dir_needed == "down":
            down_dirs = self._probe_viable_down_stair_dirs(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
            out["viable_down_directions"] = down_dirs
            rec_y = int(check.get("recommended_y") or 0)
            steps_down = min(16, max(6, py - rec_y))
            if down_dirs:
                action_hint = (
                    f"请调 mc_skill_staircase(direction='{down_dirs[0]}', vertical='down', steps={steps_down}, stop_on_ore=True) "
                    f"斜向下探接近 Y={rec_y}(可选下探: {','.join(down_dirs)})。"
                )
            else:
                action_hint = f"请调 mc_skill_staircase(vertical='down', steps={steps_down}, stop_on_ore=True) 下探至 Y={rec_y} 附近。"

        out["message"] = f"{check['hint']} {action_hint}".strip()
        return out

    def _probe_viable_pillar_up(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> bool:
        """Probe whether 1x1 straight-up pillar climbing (dig overhead + jump + place underfoot) is safe."""
        if "pillar" in self._active_failed_stair_dirs((bx, by, bz, "pillar")):
            return False
        cache = probe_cache if probe_cache is not None else {}
        counts = inv_counts if inv_counts is not None else _inventory_counts(self.bot)
        has_filler = any(
            counts.get(c, 0) >= 1
            for c in ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite", "granite", "tuff", "stone")
        ) or any(cnt >= 1 and name.endswith("_planks") for name, cnt in counts.items())
        if not has_filler:
            return False

        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
        hazard_set = fluids | falling_set
        _, pick_tier = _best_pickaxe_in_inv(counts)

        try:
            for cy in (by + 1, by + 2):
                bname = self._probe_cached_block((bx, cy, bz), cache)
                if bname in fluid_or_bedrock or bname == "unknown":
                    return False
            top3 = self._probe_cached_block((bx, by + 3, bz), cache)
            if top3 in hazard_set:
                return False
            overhead = self._probe_cached_block((bx, by + 2, bz), cache)
            if overhead not in air_set:
                req_tier = BLOCK_MIN_PICKAXE_TIER.get(overhead, 0)
                if req_tier > pick_tier:
                    return False
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = self._probe_cached_block((bx + ax, by + 2 + ay, bz + az), cache)
                    if nb in hazard_set:
                        return False
            return True
        except ApiError:
            return False

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
        probes = (
            (pos[0], pos[1] + 1, pos[2]),
            (pos[0], pos[1] + 2, pos[2]),
            (pos[0] + 1, pos[1], pos[2]),
            (pos[0] - 1, pos[1], pos[2]),
            (pos[0], pos[1], pos[2] + 1),
            (pos[0], pos[1], pos[2] - 1),
        )
        for pt in probes:
            try:
                name, _ = self._block_info(list(pt))
            except ApiError:
                continue
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
            for log_name in (*LOG_TYPES, *NETHER_STEM_TYPES):
                avail_logs = counts.get(log_name, 0)
                if avail_logs <= 0 or logs_to_craft <= 0:
                    continue
                use_logs = min(avail_logs, logs_to_craft)
                plank_name = _log_to_planks_name(log_name)
                try:
                    self._safe_craft(plank_name, use_logs * 4)
                    logs_to_craft -= use_logs
                except ApiError as err:
                    if err.code == "inventory_full":
                        raise
            counts = _inventory_counts(self.bot)

        if sticks_needed > counts.get("stick", 0) and _total_planks(counts) >= 2:
            need_crafts = math.ceil((sticks_needed - counts.get("stick", 0)) / 4)
            try:
                self._safe_craft("stick", need_crafts * 4)
            except ApiError as err:
                if err.code == "inventory_full":
                    raise
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

    def _record_utility_in_ledger(self, block_name: str, pos: Any) -> None:
        if self._mining_dfs is not None and isinstance(pos, (list, tuple)) and len(pos) >= 3:
            self._mining_dfs.ledger.record_utility(block_name, [int(pos[0]), int(pos[1]), int(pos[2])])

    def _navigate_to_distant_table(self, table_pos: list[int], *, cur_hp: float = 20.0) -> bool:
        """Navigate along active MiningDFS waypoints (if any) and move_to reach a placed crafting_table."""
        tx, ty, tz = int(table_pos[0]), int(table_pos[1]), int(table_pos[2])
        if self._mining_dfs is not None and self._mining_dfs.nodes:
            nearest_nid = min(
                self._mining_dfs.nodes.keys(),
                key=lambda nid: math.dist(self._mining_dfs.nodes[nid].anchor, (tx, ty, tz)),
            )
            if math.dist(self._mining_dfs.nodes[nearest_nid].anchor, (tx, ty, tz)) <= 16.0:
                try:
                    self._mining_dfs._navigate_to_node(nearest_nid, min_hp_ref=[cur_hp])
                except ApiError:
                    pass
        try:
            st_mid = self.bot.status()
            p_mid = st_mid.get("pos") or [9999.0, 9999.0, 9999.0]
            if math.dist((float(p_mid[0]), float(p_mid[1]), float(p_mid[2])), (tx + 0.5, ty + 0.5, tz + 0.5)) <= 3.8:
                return True
        except ApiError:
            pass
        try:
            self.bot.call("move_to", {"x": tx + 0.5, "y": ty, "z": tz + 0.5, "range": 2.2})
        except ApiError:
            try:
                self.bot.call("move_to", {"x": tx + 0.5, "z": tz + 0.5, "range": 2.2})
            except ApiError:
                pass
        try:
            st_after = self.bot.status()
            p_after = st_after.get("pos") or [9999.0, 9999.0, 9999.0]
            return math.dist((float(p_after[0]), float(p_after[1]), float(p_after[2])), (tx + 0.5, ty + 0.5, tz + 0.5)) <= 4.8
        except ApiError:
            return False

    @staticmethod
    def _has_non_wood_table_recipe_ready(target_item: str | None, counts: dict[str, int]) -> bool:
        if not target_item:
            return False
        cobble = counts.get("cobblestone", 0) + counts.get("cobbled_deepslate", 0) + counts.get("blackstone", 0)
        sticks = counts.get("stick", 0)
        reqs: dict[str, bool] = {
            "stone_pickaxe": cobble >= 3 and sticks >= 2,
            "stone_axe": cobble >= 3 and sticks >= 2,
            "stone_sword": cobble >= 2 and sticks >= 1,
            "stone_shovel": cobble >= 1 and sticks >= 2,
            "stone_hoe": cobble >= 2 and sticks >= 2,
            "furnace": cobble >= 8,
            "iron_pickaxe": counts.get("iron_ingot", 0) >= 3 and sticks >= 2,
            "iron_axe": counts.get("iron_ingot", 0) >= 3 and sticks >= 2,
            "iron_sword": counts.get("iron_ingot", 0) >= 2 and sticks >= 1,
            "iron_shovel": counts.get("iron_ingot", 0) >= 1 and sticks >= 2,
            "diamond_pickaxe": counts.get("diamond", 0) >= 3 and sticks >= 2,
            "diamond_sword": counts.get("diamond", 0) >= 2 and sticks >= 1,
        }
        return bool(reqs.get(target_item, False))

    def ensure_crafting_table(
        self,
        *,
        force_recarve: bool = False,
        target_item: str | None = None,
    ) -> dict[str, Any]:
        """Reuse a nearby same-level crafting_table or craft & place one (carving a wall pocket if needed)."""
        st = self.bot.status()
        if bool(st.get("in_water")):
            self.escape_water()
            st = self.bot.status()
        px, py, pz = [int(math.floor(v)) for v in (st.get("pos") or [0, 64, 0])]
        cur_hp = float(st.get("health") if isinstance(st.get("health"), (int, float)) else 20.0)

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
                self._record_utility_in_ledger("crafting_table", pos)
                if float(nearest.get("dist", 99)) > 3.2 and pos:
                    try:
                        self.bot.call("move_to", {"x": pos[0], "y": pos[1], "z": pos[2], "range": 2.0})
                        return {"ok": True, "reused": True, "pos": pos}
                    except ApiError:
                        pass
                else:
                    return {"ok": True, "reused": True, "pos": pos}

        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        if counts.get("crafting_table", 0) < 1:
            inv_raw, _ = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
            try:
                counts = self._ensure_planks_and_sticks(4, 0)
            except ApiError as err:
                return {"ok": False, "error": err.code, "requires_replan": True, "message": err.message, **err.context}
            if _total_planks(counts) < 4:
                # Out of wood to build a new crafting_table: check wider 32-block radius & WorldLedger for an already-placed table!
                distant_candidates: list[list[int]] = []
                if not force_recarve:
                    try:
                        wide_found = self.bot.call("find_blocks", {"radius": 32, "types": ["crafting_table"], "limit": 8})
                        for wb in sorted(wide_found.get("blocks") or [], key=lambda b: float(b.get("dist", 999))):
                            wpos = wb.get("pos")
                            if isinstance(wpos, list) and len(wpos) >= 3:
                                pt_list = [int(wpos[0]), int(wpos[1]), int(wpos[2])]
                                self._record_utility_in_ledger("crafting_table", pt_list)
                                if pt_list not in distant_candidates:
                                    distant_candidates.append(pt_list)
                    except ApiError:
                        pass
                    if self.world_memory is not None:
                        wm_hits = self.world_memory.query_pois(
                            ["crafting_table"],
                            origin=(px, py, pz),
                            dim=normalize_dimension(st.get("dimension")),
                            limit=2,
                        )
                        for wh in wm_hits:
                            if wh["dist"] <= 48.0 and wh["pos"] not in distant_candidates:
                                distant_candidates.append(list(wh["pos"]))
                    if self._mining_dfs is not None:
                        ledger_tbl = self._mining_dfs.ledger.nearest_utility("crafting_table", (px, py, pz), max_dist=48.0)
                        if ledger_tbl is not None and list(ledger_tbl) not in distant_candidates:
                            try:
                                b_chk = self.bot.call("get_block", {"pos": list(ledger_tbl)})
                                if str(b_chk.get("name") or b_chk.get("block") or "").removeprefix("minecraft:") == "crafting_table":
                                    distant_candidates.append(list(ledger_tbl))
                            except ApiError:
                                pass

                for cand_pos in distant_candidates[:2]:
                    if self._navigate_to_distant_table(cand_pos, cur_hp=cur_hp):
                        return {"ok": True, "reused": True, "pos": cand_pos}

                if distant_candidates:
                    tpos0 = distant_candidates[0]
                    dy_table = int(tpos0[1]) - int(py)
                    approach_msg = (
                        f"该工作台位于上方 {tpos0}（高差 +{dy_table} 格，直接 mc_move_to 不可达），请先调 mc_skill_mine_dfs(action='climb_out') 爬回地表后再靠近合成。"
                        if dy_table >= 8
                        else "无需回地表砍树，请先调 mc_skill_mine_dfs(action='return_collar') 或 mc_move_to 靠近该工作台后再合成。"
                    )
                    return {
                        "ok": False,
                        "error": "need_crafting_table",
                        "requires_replan": True,
                        "pos": [px, py, pz],
                        "nearest_crafting_table": tpos0,
                        "message": f"包内无富余木板新建工作台，但附近已放置有现成 crafting_table 位于 {tpos0}；{approach_msg}",
                    }

                if self._has_non_wood_table_recipe_ready(target_item, counts):
                    return {
                        "ok": False,
                        "error": "need_crafting_table",
                        "requires_replan": True,
                        "pos": [px, py, pz],
                        "item": target_item,
                        "message": (
                            f"合成 {target_item} 的原料已在背包中齐备，但 32 格内未发现已放置的 crafting_table 且包内无 4 块木板新建工作台；"
                            "请返回之前放有 crafting_table 的井口/基地合成，或补充 1 块原木制作工作台。"
                        ),
                    }

                dim = normalize_dimension(st.get("dimension"))
                biome = str(st.get("biome") or "")
                wood_targets = list(NETHER_STEM_TYPES) if dim == "the_nether" else list(LOG_TYPES)
                base_err: dict[str, Any] = {
                    "ok": False,
                    "error": "missing_ingredients",
                    "message": (
                        f"合成工作台需要至少 4 块木板或 1 块原木；当前位于下界({dim})，请先采集 {', '.join(NETHER_STEM_TYPES)} 菌柄合成木板。"
                        if dim == "the_nether"
                        else "合成工作台需要至少 4 块木板或 1 块原木。"
                    ),
                }
                wood_check = assess_world_target(wood_targets, dimension=dim, y=py, biome=biome, mode="find")
                if not wood_check.get("available"):
                    probe_cache: dict[tuple[int, int, int], str] = {}
                    up_dirs = (
                        self._probe_viable_up_stair_dirs(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
                        if wood_check.get("direction_needed") == "up"
                        else []
                    )
                    vpillar = (
                        False
                        if up_dirs or wood_check.get("direction_needed") != "up"
                        else self._probe_viable_pillar_up(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
                    )
                    up_hint = (
                        f"请先调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=16) 返回目标高度带"
                        if up_dirs
                        else (
                            "请先调 mc_skill_staircase(vertical='pillar', steps=8) 原地垫脚升层"
                            if vpillar
                            else ""
                        )
                    )
                    return {
                        "ok": False,
                        "error": "missing_ingredients",
                        "requires_replan": True,
                        "pos": [px, py, pz],
                        "ecology_check": wood_check,
                        "underground_no_wood": wood_check.get("verdict") == "below_generation_band",
                        "viable_up_directions": up_dirs,
                        "viable_pillar_up": vpillar,
                        "message": f"合成工作台需要至少 4 块木板或 1 块原木；{wood_check['hint']} {up_hint}".strip(),
                    }
                return base_err
            try:
                self._safe_craft("crafting_table", 1)
            except ApiError as err:
                return {"ok": False, "error": err.code, "requires_replan": True, "message": err.message, **err.context}

        # Place nearby using auto-placement first, then fallback open offsets, then wall-pocket carving
        try:
            placed = self.bot.call("place", {"item": "crafting_table"})
            self._record_utility_in_ledger("crafting_table", placed.get("pos"))
            return {"ok": True, "reused": False, **placed}
        except ApiError as err:
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 1, 0)):
                try:
                    placed = self.bot.call("place", {"item": "crafting_table", "pos": [px + dx, py + dy, pz + dz]})
                    self._record_utility_in_ledger("crafting_table", placed.get("pos") or [px + dx, py + dy, pz + dz])
                    return {"ok": True, "reused": False, **placed}
                except ApiError:
                    continue
            pocket = self._carve_and_place_table(px, py, pz)
            if pocket is not None:
                self._record_utility_in_ledger("crafting_table", pocket.get("pos") or pocket.get("carved_pocket"))
                return pocket
            return {"ok": False, "error": err.code, "message": err.message}

    def _collect_nearby_items(
        self,
        *,
        bot_y: float,
        radius: int = 8,
        max_pickups: int = 5,
        fallback_coord: bool = True,
        target_items: set[str] | frozenset[str] | None = None,
        exclude_items: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Record bounded approach attempts, without claiming they prove item pickup."""
        attempts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            ents = self.bot.call("get_entities", {"radius": radius, "types": ["item"]})
        except ApiError as err:
            return {"attempts": [], "skipped": [], "error": {"code": err.code, "message": err.message, **err.context}}
        cached_inv = getattr(self.bot, "_last_inventory", None)
        low_slots = isinstance(cached_inv, dict) and _compute_empty_slots(cached_inv) <= 6
        trash_all = frozenset(
            {
                *_TRASH_TIER1_ZERO_KEEP,
                *_TRASH_TIER2_DECOR_STONES,
                *_TRASH_TIER2_LOOSE_BLOCKS,
                *_TRASH_TIER3_DIRT,
                *_TRASH_TIER4_COBBLE,
            }
        )
        candidates: list[tuple[tuple[int, float], dict[str, Any]]] = []
        for idx, ent in enumerate(ents.get("entities") or []):
            pos = ent.get("pos") or []
            row: dict[str, Any] = {"id": ent.get("id"), "item": ent.get("item"), "item_pos": pos}
            if len(pos) < 3 or ent.get("id") is None:
                skipped.append({**row, "reason": "invalid_entity"})
                continue
            norm = _normalize_drop_name(ent.get("item"))
            in_target = bool(target_items and norm and norm in target_items)
            if exclude_items and norm and norm in exclude_items and not in_target:
                skipped.append({**row, "reason": "excluded_auto_dropped"})
                continue
            if target_items and norm and not in_target:
                skipped.append({**row, "reason": "non_target_item"})
                continue
            if low_slots and norm in _TRASH_TIER1_ZERO_KEEP and not in_target:
                skipped.append({**row, "reason": "ignored_clutter_low_slots"})
                continue
            if abs(float(pos[1]) - bot_y) > 4.5:
                skipped.append({**row, "reason": "height_difference", "height_difference": float(pos[1]) - bot_y})
                continue
            if in_target or norm in _HIGH_VALUE_DROPS:
                tier = 0
            elif norm in _MID_VALUE_DROPS or norm.endswith(("_log", "_stem")):
                tier = 1
            elif norm in trash_all:
                tier = 3
            else:
                tier = 2
            dist_val = float(ent.get("dist") if ent.get("dist") is not None else idx)
            candidates.append(((tier, dist_val), row))

        candidates.sort(key=lambda pair: pair[0])
        for _rank_key, row in candidates[:max_pickups]:
            pos = row["item_pos"]
            attempts.append(row)
            mpos: list[float] = []
            try:
                mv = self.bot.call("move_to", {"entity_id": row["id"], "range": 0.8, "timeout": 4000})
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
            cur_pos = row.get("pos") or mpos
            if fallback_coord and len(cur_pos) >= 3:
                dy_item = float(pos[1]) - float(cur_pos[1])
                hdist_item = math.hypot(float(pos[0]) - float(cur_pos[0]), float(pos[2]) - float(cur_pos[2]))
                if abs(dy_item) <= 0.75 and 1.05 < math.dist(cur_pos[:3], pos[:3]) <= 2.35:
                    try:
                        ctrl = self.bot.call(
                            "control",
                            {
                                "forward": True,
                                "sneak": True,
                                "yaw": _yaw_toward(
                                    float(cur_pos[0]), float(cur_pos[2]), float(pos[0]), float(pos[2])
                                ),
                                "ms": 380,
                                "stop_on": ["hurt", "death"],
                            },
                        )
                        if isinstance(ctrl.get("pos"), list) and len(ctrl["pos"]) >= 3:
                            row["pos"] = ctrl["pos"]
                    except ApiError as err:
                        row["closer_error"] = {"code": err.code, "message": err.message, **err.context}
                        if err.code in ("interrupted", "dead", "disconnected"):
                            return {"attempts": attempts, "skipped": skipped, "error": row["closer_error"]}
                elif 0.75 < dy_item <= 1.85 and hdist_item <= 2.4:
                    try:
                        ctrl = self.bot.call(
                            "control",
                            {
                                "forward": True,
                                "jump": True,
                                "yaw": _yaw_toward(
                                    float(cur_pos[0]), float(cur_pos[2]), float(pos[0]), float(pos[2])
                                ),
                                "ms": 420,
                                "stop_on": ["hurt", "death"],
                            },
                        )
                        if isinstance(ctrl.get("pos"), list) and len(ctrl["pos"]) >= 3:
                            row["pos"] = ctrl["pos"]
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
        inv_raw, inv_before = _inventory_snapshot_and_counts(self.bot)
        inv_raw, auto_dropped = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
        if auto_dropped:
            inv_before = _inventory_counts_from_raw(inv_raw)
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
        if auto_dropped:
            out["auto_dropped"] = auto_dropped
        if approaches.get("error"):
            out["error"] = approaches["error"]["code"]
            out["details"] = approaches["error"]
        elif not gained:
            out["error"] = "no_drops_collected" if attempts else ("drops_unreachable" if approaches["skipped"] else "no_drops_found")
            base_msg = "未确认任何物品入包；走近成功不代表拾取成功。请检查高差、障碍与实际背包，改变站位或路线后再试，勿据此继续合成。"
            drop_summary = _summarize_uncollected_drops(
                state_after.get("pos") or st.get("pos"), attempts, approaches["skipped"]
            )
            if drop_summary is not None:
                out["nearest_unreachable_drop"] = drop_summary["nearest"]
                out["approach_hint"] = drop_summary["approach_hint"]
                out["message"] = f"{base_msg} {drop_summary['approach_hint']}"
            else:
                out["message"] = base_msg
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
        auto_dropped_names: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Shared candidate scan, vertical reachability ranking, blacklist handling, and drop rescue."""
        expected_drops = _expected_drops_for_blocks(block_types)

        def _target_gain_sum(gain_map: dict[str, int]) -> int:
            return sum(
                int(v)
                for k, v in gain_map.items()
                if str(k).removeprefix("minecraft:") in expected_drops and int(v) > 0
            )

        gained: dict[str, int] = {}
        dug_count = 0
        protected_hits = 0
        water_skipped = 0
        last_drop_scan: dict[str, Any] | None = None
        last_bot_pos: list[float] | None = None
        mine_deadline = time.monotonic() + max(45.0, min(90.0, float(count) * 12.0))
        deadline_hit = False
        try:
            dig_call_params = inspect.signature(self.bot.call).parameters
            dig_supports_timeout = "timeout_s" in dig_call_params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in dig_call_params.values()
            )
        except (TypeError, ValueError):
            dig_supports_timeout = False

        for _ in range(count * 3):
            if dug_count >= count:
                break
            if time.monotonic() >= mine_deadline:
                deadline_hit = True
                break
            for apos in list(self.temporary_avoidance):
                self._is_block_avoided(apos)
            scan_limit = min(60, 30 + len(self.temporary_avoidance))
            found = self.bot.call(
                "find_blocks",
                {"radius": max(4, min(64, radius)), "types": block_types, "limit": scan_limit},
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
                break
            pos = tuple(target["pos"])
            remaining = mine_deadline - time.monotonic()
            if remaining <= 1.0:
                deadline_hit = True
                break
            # A single un-timed dig can eat the whole slice (r12 obsidian:
            # 4 blocks / 300s). Keep a hard slice cap, but do not starve each
            # dig by dividing the remainder by the remaining blocks (r14:
            # normal stone/log digs need >8s under fleet load). The 15s floor
            # still leaves the total bounded by mine_deadline.
            per_dig = max(15.0, min(30.0, remaining / max(1, count - dug_count)))
            try:
                dig_body = {"pos": list(pos), "collect": True}
                res = (
                    self.bot.call("dig", dig_body, timeout_s=min(remaining, per_dig))
                    if dig_supports_timeout
                    else self.bot.call("dig", dig_body)
                )
                dug_count += 1
                for k, v in (res.get("gained") or {}).items():
                    gained[k] = gained.get(k, 0) + int(v)
                if avoid_water and bool(self.bot.status().get("in_water")):
                    esc = self.escape_water()
                    if esc.get("in_water"):
                        break
                # Guard against repeatedly digging blocks whose expected target drops fall out of reach
                if dug_count >= 2 and _target_gain_sum(gained) == 0:
                    last_bot_pos = st.get("pos")
                    last_drop_scan = self._collect_nearby_items(
                        bot_y=bot_y,
                        radius=12,
                        target_items=expected_drops,
                        exclude_items=auto_dropped_names,
                    )
                    if inv_before is not None:
                        inv_delta = _positive_inv_delta(inv_before, _inventory_counts(self.bot))
                        for k, v in inv_delta.items():
                            gained[k] = max(int(gained.get(k) or 0), int(v))
                        if _target_gain_sum(gained) > 0:
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
                    avoid_secs = (
                        90.0
                        if err.code == "out_of_reach" and float(pos[1]) > bot_y + above_tol
                        else 15.0
                    )
                    self._avoid_temporarily(pos, err.code, seconds=avoid_secs)
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
            except httpx.TimeoutException as err:
                # Do not let one server-side dig outlive the slice. Release the
                # pending action before returning the confirmed partial result.
                self._avoid_temporarily(pos, "dig_timeout", 30.0)
                try:
                    self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
                return {
                    "ok": False,
                    "requires_replan": True,
                    "stop_reason": "time_budget",
                    "dig_timeout_s": round(min(remaining, per_dig), 2),
                    "dug_count": dug_count,
                    "gained": gained,
                    "partial_error": {"code": "mine_timeout", "message": "单次挖掘未在切片预算内确认；已停止并保留已入包产物。"},
                    "message": "本轮挖掘预算已用尽，单次 dig 超时；请换到目标旁 2 格内或降低 count 后重试，勿等待同一调用完成。",
                    **self._avoidance_summary(),
                }

        if dug_count > 0 and _target_gain_sum(gained) == 0:
            if last_drop_scan is None:
                st = self.bot.status()
                last_bot_pos = st.get("pos")
                bot_y = float((last_bot_pos or [0, 64, 0])[1])
                last_drop_scan = self._collect_nearby_items(
                    bot_y=bot_y,
                    radius=12,
                    target_items=expected_drops,
                    exclude_items=auto_dropped_names,
                )
            if inv_before is not None:
                inv_delta = _positive_inv_delta(inv_before, _inventory_counts(self.bot))
                for k, v in inv_delta.items():
                    gained[k] = max(int(gained.get(k) or 0), int(v))

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

        target_gained = {
            k: int(v)
            for k, v in gained.items()
            if str(k).removeprefix("minecraft:") in expected_drops and int(v) > 0
        }
        incidental_gained = {
            k: int(v)
            for k, v in gained.items()
            if str(k).removeprefix("minecraft:") not in expected_drops and int(v) > 0
        }

        out: dict[str, Any] = {
            "ok": dug_count >= count and bool(target_gained),
            "requested_count": count,
            "dug_count": dug_count,
            "gained": gained,
            "target_gained": target_gained,
            "water_skipped": water_skipped,
            **self._avoidance_summary(),
        }
        if incidental_gained:
            out["incidental_gained"] = incidental_gained
        if dug_count > 0 and not target_gained:
            out["error"] = "no_drops_collected"
            out["requires_replan"] = True
            drop_summary = _summarize_uncollected_drops(
                last_bot_pos,
                (last_drop_scan or {}).get("attempts") or [],
                (last_drop_scan or {}).get("skipped") or [],
            )
            inc_note = (
                f"（途中仅误吸无关杂物 {incidental_gained}，目标预期产物 {sorted(expected_drops)} 均未入包）"
                if incidental_gained
                else ""
            )
            if drop_summary is not None:
                out["nearby_drops"] = drop_summary["items"]
                out["nearest_unreachable_drop"] = drop_summary["nearest"]
                out["approach_hint"] = drop_summary["approach_hint"]
                out["message"] = f"连续挖掘 {dug_count} 格但目标掉落物未入包！{inc_note}{drop_summary['approach_hint']}"
            else:
                out["message"] = (
                    f"连续挖掘 {dug_count} 格但目标掉落物均未拾取{inc_note}（可能掉入深坑、被地形卡住或工具等级不匹配），"
                    "已停止继续损耗耐久，请移动到安全平层/斜坡挖掘或用 mc_get_entities 检查地面物品。"
                )
        elif dug_count < count:
            out["error"] = "partial_collection" if dug_count else "no_targets_available"
            if (
                dug_count == 0
                and not self.protected_positions
                and not self.temporary_avoidance
            ):
                out["message"] = (
                    f"半径 {radius} 格内未扫描到任何匹配目标方块（{', '.join(block_types)}），"
                    "请先水平移动或使用 mc_skill_staircase 改变海拔后再探测，勿原地仅修改 count 重试。"
                )
            else:
                out["message"] = f"本次只挖掘 {dug_count}/{count} 格；请按 gained 检查实际入包量。临时避让与保护区分别报告，改变站位或等待冷却后可重试临时失败。"
            out["requires_replan"] = True
        if deadline_hit:
            out["stop_reason"] = "time_budget"
            out["requires_replan"] = True
            out["message"] = (
                f"本轮挖掘预算已用尽，仅完成 {dug_count}/{count} 格；已保留入包产物，"
                "请换到目标旁 2 格内或降低 count 后重试，勿等待同一调用完成。"
            )
        return out

    def harvest_logs(self, target_logs: int = 4, radius: int = 32) -> dict[str, Any]:
        """Chop up to target_logs trees (or nether stems in the_nether) while blacklisting protected blocks."""
        target_logs = max(1, min(32, int(target_logs)))
        radius = max(4, min(64, int(radius)))
        st0: dict[str, Any] = {}
        if callable(getattr(self.bot, "status", None)):
            try:
                st0 = self.bot.status() or {}
            except Exception:
                st0 = {}
        raw_yaw = st0.get("yaw")
        away_yaw = (float(raw_yaw) + math.pi) if isinstance(raw_yaw, (int, float)) else None
        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        inv_raw, auto_dropped = self._ensure_free_slots(
            min_free=8,
            inv_snapshot=inv_raw,
            turn_away_yaw=away_yaw,
        )
        if auto_dropped:
            counts = _inventory_counts_from_raw(inv_raw)
        auto_dropped_names = frozenset(
            str(d["item"]).removeprefix("minecraft:")
            for d in auto_dropped
            if isinstance(d, dict) and d.get("item")
        )
        equipped = self._equip_if_present(AXE_PRIORITY)
        dim = normalize_dimension(st0.get("dimension")) if st0 else "overworld"
        wood_targets = list(NETHER_STEM_TYPES) if dim == "the_nether" else list(LOG_TYPES)
        res = self._dig_target_blocks(
            block_types=wood_targets,
            count=target_logs,
            radius=radius,
            protected_msg="周围原木被服务端明确拒绝为 protected_region，请离开该保护区域后再砍树。",
            below_tol=1.5,
            above_tol=3.5,
            inv_before=counts,
            auto_dropped_names=auto_dropped_names or None,
        )
        if res.get("error") == "protected_region":
            return res
        if res.get("dug_count", 0) == 0 and res.get("error") == "no_targets_available":
            enriched = self._enrich_ecology_unavailable(res, targets=wood_targets, radius=radius, counts=counts)
            if enriched is not None:
                return {**enriched, "equipped_axe": equipped}
            if radius < 32:
                retry_res = self._dig_target_blocks(
                    block_types=wood_targets,
                    count=target_logs,
                    radius=32,
                    protected_msg="周围原木被服务端明确拒绝为 protected_region，请离开该保护区域后再砍树。",
                    below_tol=1.5,
                    above_tol=3.5,
                    inv_before=counts,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if retry_res.get("error") == "protected_region":
                    return retry_res
                res = retry_res

        if int(res.get("dug_count") or 0) == 0 and self.temporary_avoidance:
            reach_entries = [
                (pos_k, rec)
                for pos_k, rec in self.temporary_avoidance.items()
                if rec.get("reason") in {"out_of_reach", "path_not_found", "no_path"}
            ]
            if reach_entries:
                try:
                    cur_p = (self.bot.status() or {}).get("pos") or [0.0, 64.0, 0.0]
                    cx, cy, cz = float(cur_p[0]), float(cur_p[1]), float(cur_p[2])
                    px, py, pz = math.floor(cx), math.floor(cy), math.floor(cz)
                    tx, ty, tz = min(
                        (pos_k for pos_k, _ in reach_entries),
                        key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2,
                    )
                    dx, dy, dz = tx - cx, float(ty) - cy, tz - cz
                    bearing = cardinal_bearing(dx, dz)
                    probe_cache: dict[tuple[int, int, int], str] = {}
                    up_dirs = self._probe_viable_up_stair_dirs(
                        px,
                        py,
                        pz,
                        probe_cache=probe_cache,
                        inv_counts=counts,
                    )
                    vpillar = (
                        False
                        if up_dirs
                        else self._probe_viable_pillar_up(
                            px,
                            py,
                            pz,
                            probe_cache=probe_cache,
                            inv_counts=counts,
                        )
                    )
                    best_up = bearing if bearing in up_dirs else (up_dirs[0] if up_dirs else bearing)
                    ux, uz = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}.get(best_up, (0, -1))
                    esc_x, esc_z = px + ux * 24, pz + uz * 24
                    steps_up = max(3, min(8, int(abs(dy) + 1)))
                    if dy >= 2.0:
                        if up_dirs:
                            approach_hint = (
                                f"当前处于低洼/凹坑(y={py})或周围原木仅剩高处残冠（最近原木位于[{tx},{ty},{tz}], 相对朝向={bearing}, dy={dy:+.1f}），"
                                f"直接砍树或原地 mc_skill_unstick 无法够到！请立即调用 "
                                f"mc_skill_staircase(direction='{best_up}', vertical='up', steps={steps_up}) 爬出低坑登顶平层，"
                                f"或调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格寻找整棵新树；"
                                "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                            )
                        elif vpillar:
                            approach_hint = (
                                f"周围原木仅剩高处目标（最近位于[{tx},{ty},{tz}], dy={dy:+.1f}）且四向阶梯受阻，"
                                f"请先用 mc_skill_staircase(direction='pillar', vertical='pillar', steps={min(6, steps_up)}) 垫脚升层，"
                                f"或调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格寻找整棵新树；"
                                "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                            )
                        else:
                            approach_hint = (
                                f"周围原木均为高处残冠或地形阻挡不可达（最近位于[{tx},{ty},{tz}], 相对朝向={bearing}, dy={dy:+.1f}），"
                                f"请调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格以上换一片树林；"
                                "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                            )
                    else:
                        approach_hint = (
                            f"周围原木均被地形/树干遮挡或超距不可达（最近位于[{tx},{ty},{tz}], 相对朝向={bearing}, dy={dy:+.1f}），"
                            f"请朝 {best_up} 调 mc_skill_staircase(direction='{best_up}', vertical='up', steps=4) 跨越地形，"
                            f"或调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格换一片树林；"
                            "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                        )
                    res["requires_replan"] = True
                    res["nearest_unreachable_target"] = {
                        "pos": [tx, ty, tz],
                        "bearing": bearing,
                        "dy": round(dy, 1),
                    }
                    res["viable_up_directions"] = up_dirs
                    res["viable_pillar_up"] = vpillar
                    res["approach_hint"] = approach_hint
                    res["message"] = approach_hint
                except Exception:
                    pass
        out_logs: dict[str, Any] = {
            **res,
            "equipped_axe": equipped,
        }
        if auto_dropped:
            out_logs["auto_dropped"] = auto_dropped
        return out_logs

    def _attach_shortage_memory_hint(self, out_short: dict[str, Any], missing_keys: Any) -> dict[str, Any]:
        if self.world_memory is not None:
            try:
                st_m = self.bot.status()
                mem_tip = self.world_memory.shortage_memory_hint(
                    missing_keys,
                    origin=st_m.get("pos"),
                    dim=normalize_dimension(st_m.get("dimension")),
                )
                if mem_tip:
                    out_short["memory_hint"] = mem_tip
                    if "message" in out_short:
                        out_short["message"] = f"{out_short['message']}（{mem_tip}）"
            except Exception:
                pass
        return out_short

    def craft_gear(
        self,
        item: str,
        count: int = 1,
        equip_slot: str | None = None,
    ) -> dict[str, Any]:
        """Auto-craft intermediate planks/sticks, ensure crafting_table when needed, craft item, and optionally equip."""
        item = resolve_item_alias(item)
        count = max(1, min(64, count))
        inv_raw, initial = _inventory_snapshot_and_counts(self.bot)
        if item in {"planks", "any_planks"} or (item == "oak_planks" and initial.get("oak_log", 0) == 0):
            for k_log, v_log in initial.items():
                if v_log > 0 and k_log.endswith(("_log", "_stem")):
                    cand_planks = _log_to_planks_name(k_log)
                    if cand_planks in RECIPES_1_21 or cand_planks.endswith("_planks"):
                        item = cand_planks
                        break
        plan = craft_material_shortage(item, count, initial)
        if plan and plan["missing_materials"]:
            # Logs/planks and sticks can be prepared automatically, but
            # absent stone/ingots/wool cannot be repaired by rebuilding a table.
            non_wood_missing = {k: v for k, v in plan["missing_materials"].items() if k not in {"stick", "any_planks"}}
            if non_wood_missing:
                return self._attach_shortage_memory_hint(
                    {
                        "ok": False,
                        "error": "missing_ingredients",
                        "requires_replan": True,
                        "inventory": initial,
                        **plan,
                    },
                    non_wood_missing.keys(),
                )
        inv_raw, pre_dropped = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
        planks_per, sticks_per = TOOL_WOOD_NEEDS.get(item, (0, 0))
        planks_needed = math.ceil(count / 4) * 2 if item == "stick" else planks_per * count
        sticks_needed = math.ceil(count / 4) if item == "torch" else sticks_per * count
        extra_table_planks = 4 if item in TABLE_RECIPES else 0
        # Only pre-craft sticks/planks up front if we might need them; let server check exact 3x3 items.
        try:
            prepared = self._ensure_planks_and_sticks(planks_needed, sticks_needed)
        except ApiError as err:
            return {
                "ok": False,
                "error": err.code,
                "requires_replan": True,
                "message": err.message,
                **err.context,
            }
        if item == "stick" and _total_planks(prepared) < planks_needed:
            return {
                "ok": False,
                "error": "missing_ingredients",
                "requires_replan": True,
                "inventory": prepared,
                "missing_materials": {"planks_or_log": planks_needed - _total_planks(prepared)},
                "message": (
                    f"合成 {count} 个 stick 需要至少 {planks_needed} 块木板（可用原木自动合成），"
                    f"当前可用木板仅 {_total_planks(prepared)}。"
                ),
            }
        plan = craft_material_shortage(item, count, prepared)
        if plan and plan["missing_materials"]:
            return self._attach_shortage_memory_hint(
                {
                    "ok": False,
                    "error": "missing_ingredients",
                    "requires_replan": True,
                    "inventory": prepared,
                    **plan,
                },
                plan["missing_materials"].keys(),
            )

        if item in TABLE_RECIPES:
            table_res = self.ensure_crafting_table(target_item=item)
            if not table_res.get("ok"):
                return table_res
            # Re-ensure planks if crafting table consumed some
            if extra_table_planks and planks_per > 0:
                try:
                    self._ensure_planks_and_sticks(planks_per * count, sticks_needed)
                except ApiError as err:
                    return {
                        "ok": False,
                        "error": err.code,
                        "requires_replan": True,
                        "message": err.message,
                        **err.context,
                    }

        try:
            craft_res = self._safe_craft(item, count)
        except ApiError as err:
            if err.code == "missing_ingredients" and "server placed no result" in err.message:
                try:
                    counts_now = _inventory_counts(self.bot)
                except ApiError:
                    counts_now = prepared
                plan_now = craft_material_shortage(item, count, counts_now)
                return {
                    "ok": False, "requires_replan": True,
                    "error": "missing_ingredients" if plan_now and plan_now["missing_materials"] else "server_recipe_rejected",
                    "message": err.message, "inventory": counts_now,
                    **(plan_now or {}),
                }
            if err.code == "inventory_full":
                return {
                    "ok": False,
                    "error": err.code,
                    "requires_replan": True,
                    "message": err.message,
                    **err.context,
                }
            if item in TABLE_RECIPES and (
                "windowOpen" in err.message
                or "server placed no result" in err.message
                or err.code in ("path_not_found", "need_crafting_table", "out_of_reach", "no_window")
            ):
                try:
                    if "server placed no result" in err.message:
                        self._ensure_free_slots(min_free=6)
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
                        inv_now = self._ensure_planks_and_sticks(4, 0)
                        if _total_planks(inv_now) >= 4:
                            self._safe_craft("crafting_table", 1)
                    if _inventory_counts(self.bot).get("crafting_table", 0) >= 1:
                        pos = self.bot.status()["pos"]
                        px, py, pz = math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2])
                        self._carve_and_place_table(px, py, pz)
                    craft_res = self._safe_craft(item, count)
                except ApiError as retry_err:
                    return {
                        "ok": False,
                        "error": retry_err.code,
                        "requires_replan": retry_err.code == "inventory_full",
                        "message": retry_err.message,
                        **retry_err.context,
                    }
            else:
                return {
                    "ok": False,
                    "error": err.code,
                    "message": err.message,
                    **{k: v for k, v in explain_craft_error(item, count, {"code": err.code, **err.context}).items() if k != "code"},
                }

        slot = equip_slot or DEFAULT_EQUIP_SLOTS.get(item)
        equipped_res = None
        if slot:
            try:
                equipped_res = self.bot.call("equip", {"item": item, "slot": slot})
            except ApiError:
                equipped_res = None

        out_craft: dict[str, Any] = {
            "ok": True,
            **craft_res,
            "equipped_slot": slot if equipped_res else None,
        }
        if pre_dropped:
            merged_drops = list(pre_dropped)
            if isinstance(out_craft.get("auto_dropped"), list):
                merged_drops.extend(out_craft["auto_dropped"])
            out_craft["auto_dropped"] = merged_drops
        return out_craft

    def mine_blocks(
        self,
        block_types: list[str],
        count: int = 3,
        radius: int = 32,
    ) -> dict[str, Any]:
        """Validate pickaxe tier against Minecraft harvest rules, equip best pickaxe, and dig count blocks."""
        if not block_types:
            return {"ok": False, "error": "bad_args", "message": "block_types 不能为空。"}
        # Natural terrain has `stone`/`deepslate` rather than `cobblestone`; auto-expand so cobblestone requests work!
        block_types = list(block_types)
        if "cobblestone" in block_types and "stone" not in block_types:
            block_types.append("stone")
        count = max(1, min(32, int(count)))
        radius = max(4, min(64, int(radius)))
        req_tier = max((BLOCK_MIN_PICKAXE_TIER.get(b, 0) for b in block_types), default=0)

        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
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
        raw_yaw = st.get("yaw")
        away_yaw = (float(raw_yaw) + math.pi) if isinstance(raw_yaw, (int, float)) else None
        inv_raw, auto_dropped = self._ensure_free_slots(
            min_free=8,
            inv_snapshot=inv_raw,
            turn_away_yaw=away_yaw,
        )
        if auto_dropped:
            counts = _inventory_counts_from_raw(inv_raw)
            best_pick, best_tier = _best_pickaxe_in_inv(counts)
        auto_dropped_names = frozenset(
            str(d["item"]).removeprefix("minecraft:")
            for d in auto_dropped
            if isinstance(d, dict) and d.get("item")
        )

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
            auto_dropped_names=auto_dropped_names or None,
        )
        if res.get("error") == "protected_region":
            return res

        if (
            int(res.get("dug_count") or 0) == 0
            and res.get("error") == "no_targets_available"
            and not self.temporary_avoidance
        ):
            alt_types = list(
                dict.fromkeys(
                    alt
                    for b in block_types
                    if (alt := ORE_COUNTERPARTS.get(b))
                    and alt not in block_types
                    and BLOCK_MIN_PICKAXE_TIER.get(alt, 0) <= best_tier
                )
            )
            if alt_types:
                alt_res = self._dig_target_blocks(
                    block_types=alt_types,
                    count=count,
                    radius=radius,
                    protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
                    below_tol=1.5,
                    above_tol=2.5,
                    inv_before=counts,
                    avoid_water=True,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if int(alt_res.get("dug_count") or 0) > 0 or alt_res.get("error") != "no_targets_available":
                    res = {**alt_res, "auto_paired_types": alt_types}

        # 若近距（<=10m、同层|dy|<=2）有暴露矿石但因隔墙/寻路失败暂避导致 0 挖掘，先水平破墙再清障重试一次
        has_reach_failure = any(
            rec.get("reason") in {"path_not_found", "out_of_reach"}
            for rec in self.temporary_avoidance.values()
        )
        effective_types = list(res.get("auto_paired_types") or block_types)
        if int(res.get("dug_count") or 0) == 0 and has_reach_failure:
            tunnel_attempt = self._approach_occluded_ore(block_types=effective_types, radius=min(12, radius))
            if tunnel_attempt is not None:
                if int(tunnel_attempt.get("steps_done") or 0) > 0 or tunnel_attempt.get("stop_reason") in {
                    "ore_at_face",
                    "ore_detected",
                }:
                    for pos_k in [
                        k for k, v in self.temporary_avoidance.items()
                        if v.get("reason") in {"path_not_found", "out_of_reach"}
                    ]:
                        del self.temporary_avoidance[pos_k]
                res2 = self._dig_target_blocks(
                    block_types=effective_types,
                    count=count,
                    radius=radius,
                    protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
                    below_tol=1.5,
                    above_tol=2.5,
                    inv_before=counts,
                    avoid_water=True,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if int(res2.get("dug_count") or 0) > 0:
                    return {
                        **res2,
                        "pickaxe_used": best_pick,
                        "occluded_tunnel": tunnel_attempt,
                    }
                res["occluded_tunnel"] = tunnel_attempt

        if int(res.get("dug_count") or 0) == 0 and self.temporary_avoidance:
            reach_entries = [
                (pos_k, rec)
                for pos_k, rec in self.temporary_avoidance.items()
                if rec.get("reason") in {"out_of_reach", "path_not_found", "no_path"}
            ]
            if reach_entries:
                try:
                    cur_p = self.bot.status().get("pos") or [0.0, 64.0, 0.0]
                    cx, cy, cz = float(cur_p[0]), float(cur_p[1]), float(cur_p[2])
                    tx, ty, tz = min(
                        (pos_k for pos_k, _ in reach_entries),
                        key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2,
                    )
                    dx, dy, dz = tx - cx, float(ty) - cy, tz - cz
                    bearing = cardinal_bearing(dx, dz)
                    if dy <= -3.0:
                        approach_hint = (
                            f"最近不可达矿石位于[{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})，"
                            f"请朝 {bearing} 调 mc_skill_staircase(direction='{bearing}', vertical='down', steps={min(10, max(3, int(abs(dy) + 1)))}, stop_on_ore=True) 下探接近后再挖！"
                        )
                    elif dy >= 3.0:
                        approach_hint = (
                            f"最近不可达矿石位于[{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})，"
                            f"请朝 {bearing} 调 mc_skill_staircase(direction='{bearing}', vertical='up', steps={min(10, max(3, int(abs(dy) + 1)))}, stop_on_ore=True) 上行接近后再挖！"
                        )
                    else:
                        approach_hint = (
                            f"最近不可达矿石位于[{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})，"
                            f"请朝 {bearing} 调 mc_skill_tunnel(direction='{bearing}', steps={min(8, max(2, int(max(abs(dx), abs(dz)) + 1)))}, stop_on_ore=True) 开矿道接近后再挖！"
                        )
                    res["nearest_unreachable_target"] = {
                        "pos": [tx, ty, tz],
                        "bearing": bearing,
                        "dy": round(dy, 1),
                    }
                    res["approach_hint"] = approach_hint
                except Exception:
                    pass

        if (
            int(res.get("dug_count") or 0) == 0
            and res.get("error") == "no_targets_available"
            and "nearest_unreachable_target" not in res
            and self.world_memory is not None
        ):
            from ..harness.world_memory import is_tracked_poi_block

            mem_targets = [b for b in block_types if is_tracked_poi_block(b)]
            if mem_targets:
                mem_recall = self._recall_and_mine_memory_poi(
                    mem_targets=mem_targets,
                    count=count,
                    radius=radius,
                    best_pick=best_pick,
                    inv_before=counts,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if mem_recall is not None:
                    if auto_dropped:
                        mem_recall["auto_dropped"] = auto_dropped
                    return mem_recall

        if (
            int(res.get("dug_count") or 0) == 0
            and res.get("error") == "no_targets_available"
            and "nearest_unreachable_target" not in res
        ):
            enriched = self._enrich_ecology_unavailable(res, targets=block_types, radius=radius, counts=counts)
            if enriched is not None:
                res = enriched

        out_mine: dict[str, Any] = {
            **res,
            "pickaxe_used": best_pick,
        }
        if auto_dropped:
            out_mine["auto_dropped"] = auto_dropped
        # BUG-029: a long mining slice can leave hundreds of low-value blocks
        # even when several slots are still empty; enforce retention caps here.
        try:
            _, cleaned = self._ensure_free_slots(min_free=3)
        except Exception:
            cleaned = []
        if cleaned:
            out_mine["inventory_cleaned"] = [
                {"item": d.get("item"), "count": d.get("count")} for d in cleaned
            ]
        return out_mine

    def _recall_and_mine_memory_poi(
        self,
        *,
        mem_targets: list[str],
        count: int,
        radius: int,
        best_pick: str | None,
        inv_before: dict[str, int],
        auto_dropped_names: frozenset[str] | None,
    ) -> dict[str, Any] | None:
        """Recall nearest known non-ore POI from WorldMemory, relay-navigate, lazily prune/cool bad points, and mine."""
        wm = self.world_memory
        if wm is None:
            return None

        def _probe_live(pt: tuple[int, int, int]) -> str | None:
            try:
                b = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
                return str(b.get("name") or b.get("block") or "air").removeprefix("minecraft:")
            except ApiError:
                return None

        target_clean_set = {str(t).removeprefix("minecraft:").strip().lower() for t in mem_targets}
        recalled_log: list[dict[str, Any]] = []

        for _attempt in range(4):
            try:
                st_now = self.bot.status()
            except ApiError:
                break
            cur_pos = st_now.get("pos") or [0.0, 64.0, 0.0]
            dim_now = normalize_dimension(st_now.get("dimension"))
            hits = wm.query_pois(
                mem_targets,
                origin=cur_pos,
                dim=dim_now,
                limit=1,
                verify_fn=_probe_live,
                verify_radius=32.0,
            )
            if not hits:
                break
            cand = hits[0]
            cpos = cand["pos"]
            dist_c = float(cand["dist"])

            if dist_c > 12.0:
                try:
                    self.navigate_segmented(float(cpos[0]) + 0.5, float(cpos[2]) + 0.5, range_m=2.5)
                except ApiError as nav_err:
                    if nav_err.code in {"hurt", "death", "interrupted"}:
                        raise
                    wm.mark_unreachable(cpos, dim=dim_now, reason=nav_err.code, cooldown_s=300.0)
                    recalled_log.append({"pos": cpos, "status": "cooled_unreachable", "reason": nav_err.code})
                    continue

            live_after_nav = _probe_live((int(cpos[0]), int(cpos[1]), int(cpos[2])))
            if live_after_nav is not None and live_after_nav.lower() not in target_clean_set:
                wm.remove_poi(cpos, dim=dim_now)
                recalled_log.append({"pos": cpos, "status": "removed_stale", "actual": live_after_nav})
                continue

            if best_pick:
                try:
                    self.bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass

            dig_res = self._dig_target_blocks(
                block_types=mem_targets,
                count=count,
                radius=max(16, min(32, radius)),
                protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
                below_tol=2.5,
                above_tol=3.0,
                inv_before=inv_before,
                avoid_water=True,
                auto_dropped_names=auto_dropped_names,
            )
            if int(dig_res.get("dug_count") or 0) > 0 and dig_res.get("ok"):
                wm.remove_poi(cpos, dim=dim_now)
                return {
                    **dig_res,
                    "pickaxe_used": best_pick,
                    "recalled_from_memory": cand,
                    "memory_attempts": recalled_log,
                }

            # Reached vicinity, block still exists, yet digging could not reach it (e.g. walled off by bedrock) -> cool down 300s!
            wm.mark_unreachable(cpos, dim=dim_now, reason=str(dig_res.get("error") or "unreachable"), cooldown_s=300.0)
            recalled_log.append({"pos": cpos, "status": "cooled_dig_blocked", "reason": dig_res.get("error")})

        return None

    def _approach_occluded_ore(self, *, block_types: list[str], radius: int = 12) -> dict[str, Any] | None:
        """当同层近距目标被石墙隔开时，朝主水平轴短距开矿道接近。"""
        try:
            st = self.bot.status()
            if st.get("in_water"):
                return None
            pos = st.get("pos") or [0.0, 64.0, 0.0]
            bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
            found = self.bot.call("find_blocks", {"radius": max(4, radius), "types": block_types, "limit": 8})
        except Exception:
            return None
        cands = []
        for b in found.get("blocks") or []:
            if not isinstance(b, dict) or not isinstance(b.get("pos"), list) or len(b["pos"]) < 3:
                continue
            pos_t = (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2]))
            if pos_t in self.protected_positions:
                continue
            avoid = self.temporary_avoidance.get(pos_t)
            if avoid is not None and avoid.get("reason") not in {"path_not_found", "out_of_reach"}:
                continue
            if abs(pos_t[1] - by) <= 2 and float(b.get("dist", 99.0)) <= float(radius):
                cands.append(b)
        if not cands:
            return None
        nearest = min(cands, key=lambda b: float(b.get("dist", 99.0)))
        tx, _ty, tz = int(nearest["pos"][0]), int(nearest["pos"][1]), int(nearest["pos"][2])
        dx, dz = tx - bx, tz - bz
        if abs(dx) + abs(dz) <= 1:
            return None
        if abs(dx) >= abs(dz):
            direction = "east" if dx > 0 else "west"
            steps = min(4, max(1, abs(dx) - 1))
        else:
            direction = "south" if dz > 0 else "north"
            steps = min(4, max(1, abs(dz) - 1))
        try:
            return self.dig_level_tunnel(direction=direction, steps=steps, stop_on_ore=True)
        except Exception:
            return None

    # ---- L2 Window-Sync: container snapshots, error self-healing, natural chest probing ----

    def _window_target(
        self,
        *,
        block: str | None = None,
        entity_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any] | None:
        if entity_id is not None:
            return {"entity_id": int(entity_id)}
        if x is not None and y is not None and z is not None:
            return {"pos": [int(x), int(y), int(z)]}
        if block:
            return {"block": str(block).removeprefix("minecraft:")}
        return None

    def _current_dim(self, dim: str | None = None) -> str:
        if dim:
            return normalize_dimension(dim)
        try:
            return normalize_dimension(self.bot.status().get("dimension"))
        except ApiError:
            return "overworld"

    def _resolve_nearby_container_pos(self, block: str | None = None, *, radius: int = 16) -> list[int] | None:
        names = [str(block).removeprefix("minecraft:")] if block else list(_CONTAINER_TYPES)
        try:
            found = self.bot.call("find_blocks", {"radius": max(4, min(48, int(radius))), "types": names, "limit": 8})
        except ApiError:
            return None
        blocks = [b for b in (found.get("blocks") or []) if isinstance(b, dict) and isinstance(b.get("pos"), list)]
        if not blocks:
            return None
        nearest = min(blocks, key=lambda b: float(b.get("dist") or 99))
        return [int(v) for v in nearest["pos"][:3]]

    def _container_block_at(self, pos: list[int]) -> str | None:
        try:
            res = self.bot.call("get_block", {"pos": [int(pos[0]), int(pos[1]), int(pos[2])]})
        except ApiError:
            return None
        return str(res.get("name") or res.get("block") or "").removeprefix("minecraft:").strip().lower()

    def _sync_container_window(
        self,
        payload: dict[str, Any] | None,
        *,
        pos: list[int] | None = None,
        block: str | None = None,
        dim: str | None = None,
    ) -> bool:
        """Overwrite `WorldMemory.containers` from any payload carrying a `window` snapshot.

        An empty `sections.container: []` must clear the cached phantom contents (`{}`).
        """
        if not isinstance(payload, dict):
            return False
        window = payload.get("window") if isinstance(payload.get("window"), dict) else payload
        resolved_pos = pos
        if resolved_pos is None:
            raw = payload.get("pos") or (payload.get("target") or {}).get("pos") if isinstance(payload.get("target"), dict) else payload.get("pos")
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                resolved_pos = [int(raw[0]), int(raw[1]), int(raw[2])]
        if resolved_pos is None and block:
            resolved_pos = self._resolve_nearby_container_pos(block)
        mem = self._get_world_memory()
        try:
            synced = bool(
                mem.update_container_from_window(
                    window,
                    fallback_pos=resolved_pos,
                    fallback_block=block,
                    dim=self._current_dim(dim),
                )
            )
        except TypeError:
            synced = bool(mem.update_container_from_window(window, fallback_pos=resolved_pos, fallback_block=block))
        if synced and resolved_pos is not None:
            live = self._container_block_at(resolved_pos)
            if live in ("air", "cave_air", "void_air"):
                mem.invalidate_container(resolved_pos, dim=self._current_dim(dim), block_name=block)
                synced = False
        return synced

    def _window_api_error(
        self,
        err: ApiError,
        *,
        pos: list[int] | None = None,
        block: str | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Self-heal container cache from a failed `window/act` payload (context.window)."""
        ctx = dict(getattr(err, "context", None) or {})
        window = ctx.get("window") if isinstance(ctx.get("window"), dict) else None
        synced = False
        if window is not None:
            synced = self._sync_container_window(window, pos=pos, block=block, dim=dim)
        elif pos is not None and err.code in ("no_window", "not_found", "window_closed"):
            live = self._container_block_at(pos)
            if live in ("air", "cave_air", "void_air"):
                self._get_world_memory().invalidate_container(
                    pos, dim=self._current_dim(dim), block_name=block
                )
        return {
            **ctx,
            "ok": False,
            "error": err.code,
            "message": err.message,
            "failed_op": ctx.get("failed_op"),
            "done": ctx.get("done"),
            "window": window,
            "container_pos": pos,
            "memory_synced": synced,
            "context_keys": sorted(ctx.keys()),
        }

    def container_batch(
        self,
        items: Any,
        *,
        action: str = "put",
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Batch `put`/`take` on the `container` section of a chest/barrel window."""
        if action not in ("put", "take"):
            return {"ok": False, "error": "bad_args", "message": "action 必须是 put 或 take。"}
        entries = _window_item_entries(items)
        if not entries:
            return {"ok": False, "error": "bad_args", "message": "items 不能为空。"}
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}

        ops: list[dict[str, Any]] = []
        for entry in entries:
            op: dict[str, Any] = {
                "op": action,
                "item": entry["item"],
                "count": entry["count"],
                "to" if action == "put" else "from": "container",
            }
            if action == "put":
                for key in ("slot", "potion", "enchanted", "custom_name"):
                    if key in entry:
                        op[key] = entry[key]
            ops.append(op)

        pos = [int(x), int(y), int(z)] if x is not None and y is not None and z is not None else None
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            if pos is None:
                pos = self._resolve_nearby_container_pos(block)
            return self._window_api_error(err, pos=pos, block=block, dim=dim)

        if pos is None:
            pos = self._resolve_nearby_container_pos(block)
        synced = self._sync_container_window(result, pos=pos, block=block, dim=dim)
        return {
            "ok": True,
            "action": action,
            "ops": ops,
            "done": result.get("done"),
            "window": result.get("window"),
            "container_pos": pos,
            "memory_synced": synced,
        }

    def container_deposit(
        self,
        items: Any,
        *,
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        return self.container_batch(items, action="put", block=block, x=x, y=y, z=z, dim=dim)

    def container_withdraw(
        self,
        items: Any,
        *,
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        return self.container_batch(items, action="take", block=block, x=x, y=y, z=z, dim=dim)

    def probe_unknown_nearby_containers(
        self,
        radius: float = 8.0,
        max_probes: int = 2,
        *,
        dim: str | None = None,
        skip_dungeon: bool = True,
    ) -> dict[str, Any]:
        """Passively build cache entries for nearby chests that are not known yet."""
        mem = self._get_world_memory()
        norm_dim = self._current_dim(dim)
        probed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": max(4, min(32, int(radius))), "types": ["chest", "barrel", "trapped_chest"], "limit": 8},
            )
        except ApiError as err:
            return {"ok": False, "error": err.code, "probed": [], "skipped": []}
        spawners: list[list[int]] = []
        if skip_dungeon:
            try:
                sp = self.bot.call("find_blocks", {"radius": 16, "types": ["spawner"], "limit": 8})
                spawners = [
                    [int(v) for v in b["pos"][:3]]
                    for b in (sp.get("blocks") or [])
                    if isinstance(b, dict) and isinstance(b.get("pos"), list)
                ]
            except ApiError:
                spawners = []
        for blk in found.get("blocks") or []:
            if len(probed) >= max(1, int(max_probes)):
                break
            if not isinstance(blk, dict) or not isinstance(blk.get("pos"), list):
                continue
            pos = [int(v) for v in blk["pos"][:3]]
            key = (norm_dim, pos[0], pos[1], pos[2])
            if key in getattr(mem, "containers", {}):
                skipped.append({"pos": pos, "reason": "already_cached"})
                continue
            if skip_dungeon and any(
                math.dist(pos, sp) <= 6.0 for sp in spawners
            ):
                skipped.append({"pos": pos, "reason": "dungeon_spawner"})
                continue
            try:
                view = self.bot.call("window/view", {"target": {"pos": pos}})
            except ApiError as err:
                skipped.append({"pos": pos, "reason": f"view:{err.code}"})
                continue
            synced = self._sync_container_window(view, pos=pos, block=_clean_block(blk.get("name")), dim=norm_dim)
            probed.append({"pos": pos, "block": _clean_block(blk.get("name")), "synced": synced})
        return {"ok": bool(probed), "probed": probed, "skipped": skipped, "dim": norm_dim}

    # ---- L2 Smelt-Split: non-blocking multi-furnace start/collect ----

    def _safe_fuel_counts(self) -> dict[str, int]:
        from .inventory_manager import is_safe_fuel

        counts = _inventory_counts(self.bot)
        protected = set()
        if self.inventory_manager is not None:
            try:
                protected = set(self.inventory_manager.active_protected())
            except (AttributeError, TypeError):
                protected = set()
        return {name: cnt for name, cnt in counts.items() if cnt > 0 and is_safe_fuel(name, protected)}

    def _choose_smelt_fuel(
        self,
        *,
        smelt_n: int,
        fuel_item: str | None,
        counts: dict[str, int],
        input_item: str,
    ) -> tuple[str | None, int, float]:
        from .inventory_manager import SUPPORTED_FUELS, is_safe_fuel

        protected = set()
        if self.inventory_manager is not None:
            try:
                protected = set(self.inventory_manager.active_protected())
            except (AttributeError, TypeError):
                protected = set()
        order = ["coal", "charcoal", "coal_block", "dried_kelp_block", "stick", "bamboo"]
        order += [n for n in SUPPORTED_FUELS if n not in order and n.endswith("_planks")]
        if fuel_item:
            clean = str(fuel_item).removeprefix("minecraft:")
            if is_safe_fuel(clean, protected) and counts.get(clean, 0) > 0:
                order = [clean] + [n for n in order if n != clean]
        for cand in order:
            if not is_safe_fuel(cand, protected):
                continue
            avail = int(counts.get(cand, 0)) - (smelt_n if cand == input_item else 0)
            if avail <= 0:
                continue
            per = float(SUPPORTED_FUELS.get(cand) or (1.5 if cand.endswith("_planks") else 1.5))
            need = max(1, math.ceil(smelt_n / max(0.25, per)))
            if avail < need:
                continue
            return cand, need, per
        # second pass: accept partial fuel (a partial batch still smelts fewer items)
        for cand in order:
            if not is_safe_fuel(cand, protected):
                continue
            avail = int(counts.get(cand, 0)) - (smelt_n if cand == input_item else 0)
            if avail <= 0:
                continue
            per = float(SUPPORTED_FUELS.get(cand) or 1.5)
            return cand, min(avail, max(1, math.ceil(smelt_n / max(0.25, per)))), per
        return None, 0, 0.0

    def smelt_start(
        self,
        input_item: str,
        count: int = 1,
        fuel_item: str | None = None,
        *,
        allow_multi_furnace: bool = True,
        per_item_s: float = 10.0,
    ) -> dict[str, Any]:
        """Non-blocking furnace start: split the batch across 2-3 parallel furnaces and return at once."""
        clean_input = str(input_item or "").removeprefix("minecraft:").strip().lower()
        if not clean_input:
            return {"ok": False, "error": "bad_args", "message": "input_item 不能为空。"}
        output_item = SMELT_OUTPUTS.get(clean_input, clean_input)
        counts = _inventory_counts(self.bot)
        avail_in = int(counts.get(clean_input, 0))
        if avail_in < 1:
            return {"ok": False, "error": "missing_ingredients", "message": f"背包没有 {clean_input}。"}
        smelt_n = min(max(1, int(count)), avail_in)
        self._ensure_free_slots(min_free=3)
        counts = _inventory_counts(self.bot)

        if fuel_item:
            from .inventory_manager import is_safe_fuel

            protected_fuel = set()
            if self.inventory_manager is not None:
                try:
                    protected_fuel = set(self.inventory_manager.active_protected())
                except (AttributeError, TypeError):
                    protected_fuel = set()
            requested = str(fuel_item).removeprefix("minecraft:").strip().lower()
            if not is_safe_fuel(requested, protected_fuel):
                return {
                    "ok": False,
                    "error": "missing_fuel",
                    "message": (
                        f"拒绝把 {requested} 当燃料：安全白名单仅允许煤/木炭/煤块/干海带块/竹/木棍/未保护木板，"
                        "严禁烧床、木工具、工作台、箱子、船、弓弩、盾牌、烈焰棒。"
                    ),
                }
        fuel, fuel_total, _per_fuel = self._choose_smelt_fuel(
            smelt_n=smelt_n, fuel_item=fuel_item, counts=counts, input_item=clean_input
        )
        if not fuel or fuel_total < 1:
            return {
                "ok": False,
                "error": "missing_fuel",
                "message": "背包没有安全燃料（煤/木炭/煤块/干海带块/竹/木棍/木板），禁止烧床/工具/工作台/船/烈焰棒。",
            }

        desired_k = min(3, max(1, smelt_n // 6)) if (allow_multi_furnace and smelt_n >= 8) else 1
        st = self.bot.status()
        try:
            found = self.bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": 6})
        except ApiError:
            found = {}
        existing: list[list[int]] = []
        for blk in found.get("blocks") or []:
            if isinstance(blk, dict) and isinstance(blk.get("pos"), list):
                pos = [int(v) for v in blk["pos"][:3]]
                if pos not in existing:
                    existing.append(pos)
        existing.sort(key=lambda p: math.dist(p, st.get("pos") or [0, 64, 0]))

        furnaces: list[dict[str, Any]] = []
        placed_new: list[list[int]] = []
        try:
            for pos in existing[:desired_k]:
                furnaces.append({"pos": pos, "source": "existing"})
            need_new = max(0, desired_k - len(furnaces))
            if need_new > 0:
                # keep >=16 cobblestone after crafting furnaces
                cobble = int(counts.get("cobblestone", 0))
                can_craft = min(need_new, max(0, (cobble - 16) // 8))
                if can_craft > 0:
                    try:
                        self._safe_craft("furnace", can_craft)
                    except ApiError as craft_err:
                        if craft_err.code == "need_crafting_table":
                            # craft_gear builds/borrows a crafting table (live bot usually has planks)
                            try:
                                cg = self.craft_gear("furnace", can_craft)
                                if not isinstance(cg, dict) or not cg.get("ok"):
                                    can_craft = 0
                            except ApiError:
                                can_craft = 0
                        else:
                            can_craft = 0
                owned_furnaces = int(_inventory_counts(self.bot).get("furnace", 0))
                to_place = min(need_new, owned_furnaces)
                for _ in range(to_place):
                    pos = self._place_furnace_near(placed_new)
                    if pos is None:
                        break
                    placed_new.append(pos)
                    furnaces.append({"pos": pos, "source": "placed"})
            if not furnaces:
                return {
                    "ok": False,
                    "error": "need_furnace",
                    "message": "附近没有熔炉，且圆石不足（扣合炉消耗后需保底 >=16 圆石）无法补造。",
                }

            k = len(furnaces)
            base = smelt_n // k
            extra = smelt_n % k
            shares = [base + (1 if i < extra else 0) for i in range(k)]
            jobs: list[dict[str, Any]] = []
            now = time.time()
            remaining_fuel = fuel_total
            for idx, fur in enumerate(furnaces):
                part = shares[idx]
                if part <= 0:
                    continue
                per = float(_per_fuel or 1.5)
                fuel_need = max(1, math.ceil(part / max(0.25, per)))
                fuel_need = min(fuel_need, max(0, remaining_fuel))
                if fuel_need < 1:
                    continue
                remaining_fuel -= fuel_need
                try:
                    self.bot.call(
                        "window/act",
                        {
                            "target": {"pos": fur["pos"]},
                            "ops": [
                                {"op": "put", "item": clean_input, "count": part, "to": "input"},
                                {"op": "put", "item": fuel, "count": fuel_need, "to": "fuel"},
                            ],
                        },
                    )
                except ApiError as err:
                    jobs.append(
                        {
                            "pos": fur["pos"],
                            "error": err.code,
                            "message": err.message,
                            "input_count": part,
                        }
                    )
                    continue
                ready_at = now + per_item_s * part + 5.0
                job = self._get_world_memory().register_conversion_job(
                    pos=fur["pos"],
                    input_item=clean_input,
                    output_item=output_item,
                    count=part,
                    dim=self._current_dim(),
                    furnaces=1,
                    per_item_s=per_item_s,
                    ready_at=ready_at,
                )
                jobs.append(
                    {
                        "pos": fur["pos"],
                        "source": fur["source"],
                        "job_id": job["id"],
                        "input_count": part,
                        "fuel": {"item": fuel, "count": fuel_need},
                        "ready_at": ready_at,
                        "estimated_wait_s": round(per_item_s * part + 5.0, 1),
                    }
                )
        except ApiError as err:
            if err.code in ("hurt", "death", "interrupted"):
                raise
            return {"ok": False, "error": err.code, "message": err.message}

        ok_jobs = [j for j in jobs if "job_id" in j]
        total_ready = max((j["ready_at"] for j in ok_jobs), default=time.time())
        return {
            "ok": bool(ok_jobs),
            "input": clean_input,
            "output_item": output_item,
            "requested_count": smelt_n,
            "furnace_count": len(ok_jobs),
            "furnaces": ok_jobs,
            "failures": [j for j in jobs if "job_id" not in j],
            "fuel": {"item": fuel, "total": fuel_total},
            "estimated_wait_s": round(max((j["estimated_wait_s"] for j in ok_jobs), default=0.0), 1),
            "ready_at": total_ready,
            "in_conversion_counts": self._get_world_memory().get_in_conversion_counts(),
            "non_blocking": True,
        }

    def _place_furnace_near(self, placed: list[list[int]]) -> list[int] | None:
        st = self.bot.status()
        p = st.get("pos") or [0, 64, 0]
        bx, by, bz = math.floor(float(p[0])), math.floor(float(p[1])), math.floor(float(p[2]))
        offsets = [(1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (0, 2), (-2, 0), (0, -2)]
        for dx, dz in offsets:
            cell = [bx + dx, by, bz + dz]
            if cell in placed:
                continue
            try:
                below = self.bot.call("get_block", {"pos": [cell[0], cell[1] - 1, cell[2]]})
                here = self.bot.call("get_block", {"pos": cell})
            except ApiError:
                continue
            below_name = str(below.get("name") or "air").removeprefix("minecraft:")
            here_name = str(here.get("name") or "air").removeprefix("minecraft:")
            if below_name in ("air", "water", "lava") or here_name not in ("air", "cave_air"):
                continue
            try:
                res = self.bot.call("place", {"item": "furnace", "pos": cell})
                pos = res.get("pos") if isinstance(res, dict) else None
                return [int(v) for v in (pos or cell)[:3]]
            except ApiError:
                continue
        return None

    def smelt_collect(
        self,
        furnaces: Any = None,
        furnace_pos: Any = None,
        *,
        wait_remaining_s: float = 15.0,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Collect finished furnace outputs by coordinate and settle the conversion ledger."""
        mem = self._get_world_memory()
        norm_dim = self._current_dim(dim)
        targets: list[dict[str, Any]] = []
        raw_positions: list[list[int]] = []
        if furnace_pos is not None:
            raw_positions.append([int(v) for v in list(furnace_pos)[:3]])
        if isinstance(furnaces, (list, tuple)):
            for row in furnaces:
                if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple)):
                    raw_positions.append([int(v) for v in row["pos"][:3]])
                elif isinstance(row, (list, tuple)) and len(row) >= 3:
                    raw_positions.append([int(v) for v in row[:3]])
        jobs = mem.get_conversion_jobs(dim=norm_dim)
        if not raw_positions:
            raw_positions = [[int(v) for v in j["pos"][:3]] for j in jobs]
        for pos in raw_positions:
            job = next(
                (
                    j
                    for j in jobs
                    if [int(v) for v in j.get("pos", [])[:3]] == pos
                ),
                None,
            )
            targets.append({"pos": pos, "job": job})
        if not targets:
            return {"ok": True, "collected": [], "in_conversion_counts": {}, "reason": "no_jobs"}

        wall_deadline = time.time() + max(0.0, float(wait_remaining_s))
        collected: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for tgt in targets:
            pos = tgt["pos"]
            job = tgt["job"]
            try:
                self.bot.call("move_to", {"x": pos[0] + 0.5, "z": pos[2] + 0.5, "range": 2.5, "stop_on": ["death"]})
            except Exception:  # noqa: BLE001
                pass
            # short wait for tail-end smelting (never blocks longer than wait_remaining_s)
            take_ops: list[dict[str, Any]] = []
            got: dict[str, int] = {}
            while True:
                try:
                    view = self.bot.call("window/view", {"target": {"pos": pos}})
                except (ApiError, httpx.HTTPError) as err:
                    code = str(getattr(err, "code", type(err).__name__))
                    message = str(getattr(err, "message", err))
                    failures.append({"pos": pos, "error": code, "message": message})
                    # Furnace was broken/replaced: write the job off so the ledger cannot go stale.
                    if code in ("no_window", "not_found", "blocked", "cannot_interact") and job is not None:
                        try:
                            block_name = self._container_block_at(pos)
                        except Exception:  # noqa: BLE001
                            block_name = None
                        if block_name != "furnace":
                            mem.complete_conversion_job(
                                job.get("id"), collected=int(job.get("remaining") or 0)
                            )
                    break
                sections = view.get("sections") or {}
                outputs = [o for o in (sections.get("output") or []) if int(o.get("count") or 0) > 0]
                inputs_left = sum(int(x.get("count") or 0) for x in (sections.get("input") or []))
                burning = bool((view.get("info") or {}).get("burning"))
                out_total = sum(int(o.get("count") or 0) for o in outputs)
                job_remaining = int((job or {}).get("remaining") or 0)
                # Only take when the batch is really finished (or the caller's wait budget expired):
                # a mid-batch "not burning" gap must not trigger a premature partial take.
                finished = inputs_left == 0 and not burning
                complete_enough = job_remaining > 0 and out_total >= job_remaining and inputs_left == 0
                if outputs and (finished or complete_enough):
                    for out in outputs:
                        cnt = int(out.get("count") or 0)
                        name = _clean_block(out.get("name") or ((job or {}).get("output_item") or ""))
                        take_ops.append({"op": "take", "from": "output", "count": cnt})
                        got[name] = got.get(name, 0) + cnt
                    break
                if not burning and inputs_left == 0 and not outputs:
                    break
                if time.time() >= wall_deadline:
                    if outputs and (inputs_left == 0 or out_total >= job_remaining):
                        for out in outputs:
                            cnt = int(out.get("count") or 0)
                            name = _clean_block(out.get("name") or "")
                            take_ops.append({"op": "take", "from": "output", "count": cnt})
                            got[name] = got.get(name, 0) + cnt
                    break
                time.sleep(1.0)
            if not take_ops:
                if job is not None:
                    failures.append({"pos": pos, "error": "no_output_yet", "job_id": job.get("id")})
                continue
            try:
                inv_now, dropped = self._ensure_free_slots(min_free=2)
            except (ApiError, httpx.HTTPError) as err:
                failures.append({
                    "pos": pos,
                    "error": str(getattr(err, "code", type(err).__name__)),
                    "message": str(getattr(err, "message", err)),
                    "got": got,
                })
                continue
            if dropped:
                got.setdefault("_auto_dropped", 0)
            try:
                res = self.bot.call("window/act", {"target": {"pos": pos}, "ops": take_ops})
            except (ApiError, httpx.HTTPError) as err:
                failures.append({
                    "pos": pos,
                    "error": str(getattr(err, "code", type(err).__name__)),
                    "message": str(getattr(err, "message", err)),
                    "got": got,
                })
                continue
            taken: dict[str, int] = {}
            for step in res.get("done") or []:
                if isinstance(step, dict) and isinstance(step.get("took"), dict) and step["took"]:
                    _merge_counts(taken, {k: int(v) for k, v in step["took"].items()})
            if not taken:
                # some servers report the take without a `took` map; fall back to the planned amounts
                taken = dict(got)
            if job is not None:
                mem.complete_conversion_job(
                    job.get("id"),
                    collected=sum(int(v) for k, v in taken.items() if not str(k).startswith("_")),
                )
            collected.append({"pos": pos, "took": taken, "job_id": (job or {}).get("id")})

        return {
            "ok": bool(collected) or not failures,
            "collected": collected,
            "failures": failures,
            "in_conversion_counts": mem.get_in_conversion_counts(),
            "jobs_remaining": len(mem.get_conversion_jobs(dim=norm_dim)),
        }

    def smelt_items(
        self,
        input_item: str,
        count: int = 1,
        fuel_item: str | None = None,
        wait_timeout_s: float = 95.0,
    ) -> dict[str, Any]:
        """Ensure nearby furnace, compute exact ceil(n/8) coal/charcoal or wood fallback, smelt, guard health, and collect output."""
        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        avail_in = counts.get(input_item, 0)
        if avail_in < 1:
            return {"ok": False, "error": "missing_ingredients", "message": f"背包没有 {input_item}。"}
        smelt_n = min(max(1, count), avail_in)
        inv_raw, pre_dropped = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
        if pre_dropped:
            counts = _inventory_counts_from_raw(inv_raw)

        # Locate or build+place furnace
        found = self.bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": 5})
        furnaces = found.get("blocks") or []
        furnace_pos = None
        if furnaces:
            nearest = min(furnaces, key=lambda b: float(b.get("dist", 99)))
            furnace_pos = nearest["pos"]
            if float(nearest.get("dist", 99)) > 4.0:
                try:
                    self.bot.call("move_to", {"x": furnace_pos[0], "y": furnace_pos[1], "z": furnace_pos[2], "range": 2.5})
                except ApiError:
                    furnace_pos = None
        if furnace_pos is None:
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
                        self._safe_craft(plank, 4)
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
                    self._safe_craft(plank, 4)
                    counts = _inventory_counts(self.bot)
                except ApiError:
                    pass
                if counts.get(plank, 0) > 0:
                    chosen_fuel = plank
                    fuel_count = min(counts[plank], math.ceil(smelt_n / 1.5))

        if not chosen_fuel or fuel_count < 1:
            # P2-28/r20_long: the bare message left the model stuck in
            # bootstrap_iron_age with no next step.
            return {
                "ok": False,
                "requires_replan": True,
                "error": "missing_fuel",
                "next_action": "mc_skill_harvest_logs(target_logs=2)",
                "inventory": counts,
                "message": (
                    "背包没有煤炭、木炭、木板或原木燃料，熔炼无法开始（铁锭等产物仍在炉中）。"
                    "请先调 mc_skill_harvest_logs(target_logs=2, radius=32) 砍 2 个原木（自动转木板作燃料），"
                    "或用 mc_skill_mine_blocks(block_types=['coal_ore'], count=3) 采煤；"
                    "拿到燃料后再用相同 input_item 调用 mc_skill_smelt。不要无燃料重复调用。"
                ),
            }

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
            check_and_stabilize(self.bot, raise_on_low_health=False)
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
        outputs_to_take = [
            o for o in ((view.get("sections") or {}).get("output") or []) if int(o.get("count") or 0) > 0
        ]
        if outputs_to_take:
            inv_take, take_dropped = self._ensure_free_slots(min_free=2)
            if take_dropped:
                pre_dropped.extend(take_dropped)
            for out_item in outputs_to_take:
                cnt = int(out_item.get("count") or 0)
                out_name = str(out_item.get("name") or "").removeprefix("minecraft:")
                if _compute_empty_slots(inv_take) == 0 and not _can_fit_item_in_inventory(
                    inv_take, out_name, cnt, check_craft_inputs=False
                ):
                    return {
                        "ok": False,
                        "error": "inventory_full",
                        "requires_replan": True,
                        "furnace_pos": furnace_pos,
                        "pending_output": {"name": out_name, "count": cnt},
                        "message": (
                            f"熔炉产物 {out_name} x{cnt} 已就绪，但背包 36 格已满(empty_slots=0)且无法自动清出空槽；"
                            "请先消耗、放置或丢弃至少 1 格物品再取出，切勿重复开炉强行取物触发 30s 超时。"
                        ),
                    }
        for out_item in outputs_to_take:
            cnt = int(out_item.get("count") or 0)
            self.bot.call(
                "window/act",
                {"target": target, "ops": [{"op": "take", "from": "output", "count": cnt}]},
            )
            collected.append({"name": out_item.get("name"), "count": cnt})

        out_smelt: dict[str, Any] = {
            "ok": True,
            "furnace_pos": furnace_pos,
            "input": input_item,
            "requested_count": smelt_n,
            "fuel": {"item": chosen_fuel, "count": fuel_count},
            "collected": collected,
        }
        if pre_dropped:
            out_smelt["auto_dropped"] = pre_dropped
        return out_smelt

    # ---- 比赛窗口接口（window/view + window/act）无策略原子封装 ----
    # 以下函数只做“给定明确参数 -> 调窗口接口 -> 原样返回结果”，
    # 不判断前置、不排序、不自动跳转任务；调用方负责何时调用。

    def enchant_item_once(
        self,
        item: str,
        option: int,
        *,
        lapis_count: int | None = None,
        take: bool = True,
        block: str | None = "enchanting_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """One enchanting-table combo in a single `/window/act` call.

        Order is fixed by the API: put item -> put lapis -> select option -> take item.
        `option` is the API option index (0-based); lapis cost is `option + 1` when omitted.

        Upstream 1.3.14 note: repeated calls can leave mineflayer's window inventory
        stale even though the server applied the enchant. Prefer the tool-level
        `mc_skill_enchant` wrapper, which verifies `/inventory` and reconnects once.
        """
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}
        try:
            opt = int(option)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_args", "message": "option 必须是整数。"}
        if opt < 0:
            return {"ok": False, "error": "bad_args", "message": "option 从 0 开始。"}
        lapis = opt + 1 if lapis_count is None else int(lapis_count)
        ops: list[dict[str, Any]] = [
            {"op": "put", "item": resolve_item_alias(item), "to": "item"},
            {"op": "put", "item": "lapis_lazuli", "count": max(1, lapis), "to": "lapis"},
            {"op": "select", "option": opt},
        ]
        if take:
            ops.append({"op": "take", "from": "item"})

        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            return self._window_api_error(err)
        return {"ok": True, "option": opt, "ops": ops, "done": result.get("done"), "window": result.get("window")}

    def merchant_trade(
        self,
        *,
        entity_id: int,
        option: int | None = None,
        get_item: str | None = None,
        times: int = 1,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Read merchant `options`, then run `select(option, times)` in one `/window/act` call."""
        target = self._window_target(entity_id=entity_id)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 entity_id。"}
        try:
            view = self.bot.call("window/view", {"target": target})
        except ApiError as err:
            return self._window_api_error(err)
        options = [o for o in (view.get("options") or []) if isinstance(o, dict)]

        chosen: dict[str, Any] | None = None
        if option is not None:
            try:
                idx = int(option)
            except (TypeError, ValueError):
                return {"ok": False, "error": "bad_args", "message": "option 必须是整数。"}
            chosen = next((o for o in options if int(o.get("index", -1)) == idx), None)
            if chosen is None and 0 <= idx < len(options):
                chosen = options[idx]
        elif get_item:
            needle = resolve_item_alias(get_item)
            matches = [
                o
                for o in options
                if isinstance(o.get("get"), dict)
                and resolve_item_alias(str(o["get"].get("name") or "")) == needle
            ]
            available = [o for o in matches if o.get("available") is not False]
            chosen = (available or matches or [None])[0]
        else:
            return {"ok": False, "error": "bad_args", "message": "需要 option 或 get_item 之一。"}

        if chosen is None:
            return {
                "ok": False,
                "error": "no_matching_trade",
                "message": "没有匹配的交易选项。",
                "options": options,
            }

        idx = int(chosen.get("index", 0))
        count = max(1, int(times))
        try:
            result = self.bot.call(
                "window/act",
                {"target": target, "ops": [{"op": "select", "option": idx, "times": count}]},
            )
        except ApiError as err:
            return self._window_api_error(err)
        return {
            "ok": True,
            "option": idx,
            "trade": chosen,
            "done": result.get("done"),
            "window": result.get("window"),
        }

    def brew_once(
        self,
        ingredient_item: str,
        *,
        bottles: Any = None,
        fuel_item: str | None = None,
        wait: bool = True,
        wait_timeout_s: float = 30.0,
        poll_interval_s: float = 2.0,
        block: str | None = "brewing_stand",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Load a brewing stand (`bottles`/`ingredient`/`fuel`) and optionally poll `brew_time_left`."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}

        bottle_entries = _window_item_entries(bottles) if bottles else []
        ops: list[dict[str, Any]] = []
        for i, entry in enumerate(bottle_entries):
            op: dict[str, Any] = {
                "op": "put",
                "item": entry["item"],
                "count": entry["count"],
                "to": "bottles",
            }
            if "potion" in entry:
                op["potion"] = entry["potion"]
            if "slot" in entry:
                op["slot"] = entry["slot"]
            elif entry["count"] == 1 and len(bottle_entries) > 1:
                op["slot"] = i
            ops.append(op)
        ops.append(
            {"op": "put", "item": resolve_item_alias(ingredient_item), "count": 1, "to": "ingredient"}
        )
        if fuel_item:
            ops.append({"op": "put", "item": resolve_item_alias(fuel_item), "count": 1, "to": "fuel"})

        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            if err.code == "no_window" and x is not None and y is not None and z is not None and block:
                try:
                    got = self.bot.call("get_block", {"pos": [x, y, z]})
                    actual = str((got or {}).get("name") or "").removeprefix("minecraft:")
                except ApiError:
                    actual = ""
                if actual and actual != str(block).removeprefix("minecraft:"):
                    return {
                        "ok": False,
                        "error": "window_target_mismatch",
                        "target": [x, y, z],
                        "expected": block,
                        "actual": actual,
                        "message": (
                            f"目标 {[x, y, z]} 实际是 {actual}，不是 {block}；"
                            "请用 mc_find_blocks(types=['brewing_stand']) 重新定位，"
                            "或省略 x/y/z 让工具自动搜索附近酿造台。不要重复同一坐标。"
                        ),
                    }
            out = self._window_api_error(err)
            if err.code == "missing_item":
                try:
                    out.setdefault("inventory", _inventory_counts(self.bot))
                except ApiError:
                    pass
                out["message"] = (
                    f"{out.get('message') or err.message}；已附 inventory 计数，"
                    "请核对 ingredient/fuel 是否仍在背包，勿原参数重复。"
                )
            return out

        view: dict[str, Any] | None = None
        if wait:
            deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
            delay = 1.0
            while time.monotonic() < deadline:
                time.sleep(delay)
                delay = max(0.5, float(poll_interval_s))
                try:
                    view = self.bot.call("window/view", {"target": target})
                except ApiError as err:
                    return self._window_api_error(err)
                info = view.get("info") or {}
                try:
                    left = float(info.get("brew_time_left") or 0.0)
                except (TypeError, ValueError):
                    left = 0.0
                if left <= 0:
                    break

        out: dict[str, Any] = {
            "ok": True,
            "ops": ops,
            "done": result.get("done"),
            "window": (view or {}).get("window", result.get("window")),
            "sections": (view or {}).get("sections"),
            "info": (view or {}).get("info"),
        }
        return out

    def anvil_operate(
        self,
        left_item: str | dict[str, Any],
        *,
        right_item: str | dict[str, Any] | None = None,
        left_enchanted: bool | None = None,
        right_enchanted: bool | None = None,
        text: str | None = None,
        take: bool = True,
        block: str | None = "anvil",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Single `/window/act` anvil combo: put left/right, optional rename text, take output."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}

        def _build_anvil_put(spec: str | dict[str, Any], slot_name: str, ench_flag: bool | None) -> dict[str, Any]:
            if isinstance(spec, dict):
                op_d: dict[str, Any] = {
                    "op": "put",
                    "item": resolve_item_alias(str(spec.get("item") or spec.get("name") or "")),
                    "to": slot_name,
                }
                for fk in ("enchanted", "custom_name", "slot"):
                    if fk in spec and spec[fk] is not None:
                        op_d[fk] = spec[fk]
                if ench_flag is not None and "enchanted" not in op_d:
                    op_d["enchanted"] = bool(ench_flag)
                return op_d
            op_s: dict[str, Any] = {"op": "put", "item": resolve_item_alias(str(spec)), "to": slot_name}
            if ench_flag is not None:
                op_s["enchanted"] = bool(ench_flag)
            return op_s

        ops: list[dict[str, Any]] = [_build_anvil_put(left_item, "left", left_enchanted)]
        if right_item is not None:
            ops.append(_build_anvil_put(right_item, "right", right_enchanted))
        if text is not None:
            ops.append({"op": "text", "value": str(text)})
        if take:
            ops.append({"op": "take", "from": "output"})
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            err_payload = self._window_api_error(err)
            if err.code == "no_output":
                win = err_payload.get("window") if isinstance(err_payload.get("window"), dict) else {}
                secs = win.get("sections") if isinstance(win.get("sections"), dict) else {}
                left_rows = secs.get("left") or []
                right_rows = secs.get("right") or []
                l0 = left_rows[0] if left_rows and isinstance(left_rows[0], dict) else {}
                r0 = right_rows[0] if right_rows and isinstance(right_rows[0], dict) else {}
                rcost = int((win.get("info") or {}).get("repair_cost") or 0) if isinstance(win.get("info"), dict) else 0
                if l0 and r0 and not l0.get("durability_used") and not r0.get("enchantments"):
                    err_payload["hint"] = (
                        "铁砧无产物(no_output)：左槽物品为满耐久且右槽无附魔可叠加；"
                        "若要修耐久请将受损装备放 left，若要合附魔请用 left_enchanted=True / right_enchanted=True 指定附魔件。"
                    )
                elif l0.get("enchantments") and r0.get("enchantments"):
                    err_payload["hint"] = (
                        "铁砧无产物(no_output)：左右槽附魔可能互斥（如锋利/亡灵杀手/节肢克星互斥）或已达等级上限，或当前经验等级不足。"
                    )
                elif rcost > 0:
                    err_payload["hint"] = f"铁砧无产物或无法取出：所需经验等级 repair_cost={rcost}，请确认当前 XP 等级足够。"
            return err_payload
        return {"ok": True, "ops": ops, "done": result.get("done"), "window": result.get("window")}

    def smithing_apply(
        self,
        template_item: str,
        base_item: str,
        addition_item: str,
        *,
        take: bool = True,
        block: str | None = "smithing_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Single `/window/act` smithing combo: template + base + addition -> take output."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}
        ops: list[dict[str, Any]] = [
            {"op": "put", "item": resolve_item_alias(template_item), "to": "template"},
            {"op": "put", "item": resolve_item_alias(base_item), "to": "base"},
            {"op": "put", "item": resolve_item_alias(addition_item), "to": "addition"},
        ]
        if take:
            ops.append({"op": "take", "from": "output"})
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            return self._window_api_error(err)
        return {"ok": True, "ops": ops, "done": result.get("done"), "window": result.get("window")}

    def stonecutter_cut(
        self,
        input_item: str,
        option: int,
        *,
        count: int | None = None,
        take: bool = True,
        block: str | None = "stonecutter",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Single `/window/act` stonecutter combo: put input -> select option -> take output."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}
        try:
            opt = int(option)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_args", "message": "option 必须是整数。"}
        put_op: dict[str, Any] = {"op": "put", "item": resolve_item_alias(input_item), "to": "input"}
        if count is not None:
            put_op["count"] = max(1, int(count))
        ops: list[dict[str, Any]] = [put_op, {"op": "select", "option": opt}]
        if take:
            ops.append({"op": "take", "from": "output"})
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            return self._window_api_error(err)
        return {"ok": True, "option": opt, "ops": ops, "done": result.get("done"), "window": result.get("window")}

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
        if self.world_memory is not None:
            try:
                self.world_memory.record_bed_spawn([x, y, z], dim=normalize_dimension(st.get("dimension")))
            except Exception:
                pass
        return {"ok": True, "bed_pos": [x, y, z], **res}

    def escape_water(self) -> dict[str, Any]:
        """Float to the water/lava surface, carve an upper notch over a solid shoreline step if needed, and hop onto dry land."""
        st = self.bot.status()
        if not bool(st.get("in_water")) and not bool(st.get("in_lava")):
            return {
                "ok": True,
                "in_water": False,
                "in_lava": False,
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
                if above_name in fluids:
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
                if not bool(after_cur.get("in_water")) and not bool(after_cur.get("in_lava")):
                    return mv_res, after_cur
            return mv_res, after_cur

        mv_res, st_after = _step_onto_adjacent_bank()

        if bool(st_after.get("in_water")) or bool(st_after.get("in_lava")):
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
                            "netherrack",
                            "blackstone",
                            "basalt",
                            "soul_sand",
                            "soul_soil",
                            "nether_bricks",
                            "obsidian",
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

    def descend_canopy(self) -> dict[str, Any]:
        """当站在高处树冠(*_leaves)上方时，安全向下清理树叶/原木直到落地，防止直接边缘跳落摔伤。"""
        st = self.bot.status()
        pos_before = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = (
            math.floor(float(pos_before[0])),
            math.floor(float(pos_before[1])),
            math.floor(float(pos_before[2])),
        )
        try:
            below_name, _ = self._block_info([bx, by - 1, bz])
        except ApiError:
            return {"ok": False, "on_canopy": False}
        if not below_name.endswith("_leaves"):
            return {"ok": False, "on_canopy": False, "below": below_name}

        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        dug: list[dict[str, Any]] = []
        # 确认下方 2..16 格中存在空气或树干（即使站在树干列上方也能安全向下挖开下树）且不直接落入岩浆
        has_tree_or_air = False
        for dy in range(2, 16):
            try:
                bn, _ = self._block_info([bx, by - dy, bz])
            except ApiError:
                break
            if bn in {"lava", "flowing_lava"}:
                return {"ok": False, "on_canopy": True, "reason": "lava_below"}
            if bn in air_set or bn.endswith(("_log", "_stem")):
                has_tree_or_air = True
                break
        if not has_tree_or_air:
            return {"ok": False, "on_canopy": False, "below": below_name}

        for _ in range(10):
            cur = self.bot.status()
            p = cur.get("pos") or pos_before
            cx, cy, cz = math.floor(float(p[0])), math.floor(float(p[1])), math.floor(float(p[2]))
            try:
                under, diggable = self._block_info([cx, cy - 1, cz])
            except ApiError:
                break
            if not (under.endswith("_leaves") or under.endswith("_log")) or not diggable:
                break
            # 若脚下仅 1 层树叶下方就是 >5 格深的悬空落差，先探明下方落差，避免一脚摔重伤
            drop_air = 0
            unsafe_drop = False
            for d in range(2, 9):
                try:
                    bname, _ = self._block_info([cx, cy - d, cz])
                except ApiError:
                    break
                if bname in {"lava", "flowing_lava"}:
                    unsafe_drop = True
                    break
                if bname in air_set or bname.endswith("_leaves"):
                    drop_air += 1
                else:
                    break
            if unsafe_drop or drop_air >= 5:
                break
            try:
                self.bot.call("dig", {"pos": [cx, cy - 1, cz], "collect": True, "stop_on": ["hurt", "death"]})
                dug.append({"pos": [cx, cy - 1, cz], "block": under})
                time.sleep(0.25)
            except ApiError:
                break

        st_after = self.bot.status()
        return {
            "ok": bool(dug),
            "on_canopy": True,
            "dug": dug,
            "pos_before": pos_before,
            "pos_after": st_after.get("pos") or pos_before,
        }

    def unstick(self) -> dict[str, Any]:
        """Clear blocks overlapping the bot's 0.6x1.8 floor()-aligned hitbox and step out of pits/corners, canopy, or water."""
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

        canopy = self.descend_canopy()
        if canopy.get("on_canopy") and canopy.get("dug"):
            return {
                "ok": True,
                "descended_canopy": True,
                "foot_block_before": [bx, by, bz],
                "pos_before": pos_before,
                "pos_after": canopy.get("pos_after", pos_before),
                "dug": canopy.get("dug", []),
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
            for log_name in (*LOG_TYPES, *NETHER_STEM_TYPES):
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

        # A bot straddling the edge remains supported by its neighbor when
        # the nominal foot block is dug. Center it before opening the shaft.
        body_t = (st.get("last_death") or {}).get("t")
        for _ in range(5):
            actual = self.bot.status()
            cur = actual.get("pos") or pos
            if (math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])) != (bx, by, bz) or (actual.get("last_death") or {}).get("t") != body_t:
                return {"ok": False, "requires_replan": True, "reason": "position_changed_before_dig", "pos_after": cur}
            if max(abs(cur[0] - bx - 0.5), abs(cur[2] - bz - 0.5)) <= 0.16:
                break
            try:
                result = self.bot.call("control", {
                    "forward": True, "sneak": True,
                    "yaw": _yaw_toward(cur[0], cur[2], bx + 0.5, bz + 0.5),
                    "ms": 120, "stop_on": ["hurt", "death"],
                })
                if result.get("interrupted_by"):
                    return {"ok": False, "requires_replan": True, "reason": "centering_interrupted", "interrupted_by": result["interrupted_by"]}
                time.sleep(0.1)
            except ApiError as exc:
                return {"ok": False, "requires_replan": True, "reason": "centering_failed", "error": exc.code, "message": exc.message}
        else:
            return {"ok": False, "requires_replan": True, "reason": "centering_not_confirmed", "pos_after": self.bot.status().get("pos")}

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
                self.bot.call("dig", {"pos": s_pos, "collect": not urgent, "stop_on": ["hurt", "death"]})
                dug.append({"pos": s_pos, "block": b_name})
                # The dig response is not evidence the bot fell into its hole.
                # Never close the roof over an empty shaft next to the bot.
                for _ in range(10):
                    fell = self.bot.status()
                    fallen_pos = fell.get("pos") or pos
                    if fell.get("alive") is False or (fell.get("last_death") or {}).get("t") != body_t:
                        break
                    if abs(float(fallen_pos[1]) - s_pos[1]) <= 0.2 and max(abs(fallen_pos[0] - bx - 0.5), abs(fallen_pos[2] - bz - 0.5)) <= 0.2 and fell.get("on_ground") is not False:
                        break
                    time.sleep(0.1)
                if fell.get("alive") is False or (fell.get("last_death") or {}).get("t") != body_t or abs(float(fallen_pos[1]) - s_pos[1]) > 0.2 or max(abs(fallen_pos[0] - bx - 0.5), abs(fallen_pos[2] - bz - 0.5)) > 0.2:
                    return {"ok": False, "requires_replan": True, "reason": "shaft_entry_not_confirmed", "dug": dug, "pos_after": fallen_pos}
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
                roof_placed = _block_name(roof_pos) == roof_item
            except ApiError as exc:
                roof_error = {"code": exc.code, "message": exc.message}

        st_after = self.bot.status()
        final_pos = st_after.get("pos") or pos
        inside = abs(float(final_pos[1]) - (by - 3)) <= 0.2 and max(abs(final_pos[0] - bx - 0.5), abs(final_pos[2] - bz - 0.5)) <= 0.2
        return {
            "ok": roof_placed and inside,
            "requires_replan": not (roof_placed and inside),
            "inside_shaft": inside,
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

    def dig_cover_pocket(self) -> dict[str, Any]:
        """Step into a 1x2 wall pocket and seal it behind us to break a shooter's line of sight.

        Live s03: a skeleton in an open cave put 6 arrows into a fleeing bot (20 -> 0 hp).
        Running away in open air only extends the firing line; a sealed pocket ends it.
        """
        fluid_or_bedrock = {"water", "lava", "flowing_water", "flowing_lava", "bedrock"}
        _solid_pref = ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite", "granite", "netherrack")

        def _blk(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        counts = _inventory_counts(self.bot)
        filler = next((name for name in _solid_pref if counts.get(name, 0) >= 2), None)
        if filler is None:
            filler = next((name for name, cnt in sorted(counts.items())
                           if cnt >= 2 and name.endswith(("_planks", "_log", "_stem"))), None)
        if filler is None:
            return {"ok": False, "requires_replan": True, "reason": "no_filler_block"}

        st = self.bot.status()
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))

        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            feet = [bx + dx, by, bz + dz]
            head = [bx + dx, by + 1, bz + dz]
            back_deep = [bx + dx * 2, by, bz + dz * 2]
            try:
                feet_name, head_name, deep_name = _blk(feet), _blk(head), _blk(back_deep)
            except ApiError:
                continue
            if feet_name in fluid_or_bedrock or head_name in fluid_or_bedrock or deep_name in fluid_or_bedrock:
                continue
            if feet_name == "air" or head_name == "air" or "bedrock" in (feet_name, head_name):
                continue
            try:
                self._equip_if_present(tuple(PICKAXE_TIERS))
                # Deliberately do NOT stop on "hurt": an arrow landing mid-dig is exactly the
                # situation we are escaping, and aborting here would leave us still exposed.
                for cell in (feet, head):
                    self.bot.call("dig", {"pos": cell, "collect": True, "stop_on": ["death"]})
                self.bot.call("move_to", {
                    "x": feet[0] + 0.5, "y": feet[1], "z": feet[2] + 0.5,
                    "range": 0.8, "timeout": 2500, "stop_on": ["death"],
                })
                sealed = []
                for cell in (feet, head):
                    try:
                        self.bot.call("place", {"item": filler, "pos": cell})
                        sealed.append(_blk(cell) == filler)
                    except ApiError:
                        sealed.append(False)
                st_now = self.bot.status()
                return {
                    "ok": all(sealed),
                    "requires_replan": not all(sealed),
                    "mode": "cover_pocket",
                    "direction": [dx, dz],
                    "pocket": [feet, head],
                    "filler": filler,
                    "sealed": sealed,
                    "pos_after": st_now.get("pos"),
                }
            except ApiError:
                continue
        return {"ok": False, "requires_replan": True, "reason": "no_solid_wall"}

    def dig_level_tunnel(self, direction: str = "north", steps: int = 4, stop_on_ore: bool = True) -> dict[str, Any]:
        from .tunnel import dig_level_tunnel

        res = dig_level_tunnel(self.bot, direction, steps, stop_on_ore)
        d_key = str(direction).strip().lower()
        stop_reason = str(res.get("stop_reason") or "")
        terrain_failures = {
            "unsafe_or_unknown_floor",
            "fluid_adjacent",
            "falling_material_adjacent",
            "unsupported_face_block",
            "insufficient_pickaxe_tier",
            "dig_not_confirmed",
            "support_changed",
            "movement_not_confirmed",
        }
        if res.get("ok") is False and stop_reason in terrain_failures:
            def _floor3(raw_pos: Any) -> tuple[int, int, int] | None:
                if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) < 3:
                    return None
                try:
                    return (
                        math.floor(float(raw_pos[0])),
                        math.floor(float(raw_pos[1])),
                        math.floor(float(raw_pos[2])),
                    )
                except (TypeError, ValueError):
                    return None

            start_cell = _floor3(res.get("pos_before"))
            end_cell = _floor3(res.get("pos_after")) or start_cell
            blk = res.get("blocker")
            reason_label = (
                f"{stop_reason}:{blk.get('block')}"
                if isinstance(blk, dict) and blk.get("block")
                else stop_reason
            )
            if end_cell is not None:
                if d_key in {"north", "south", "east", "west"}:
                    self._record_failed_tunnel_dir(end_cell, d_key, reason_label)
                    if not res.get("dug") and start_cell is not None and start_cell != end_cell:
                        self._record_failed_tunnel_dir(start_cell, d_key, f"dead_end->{reason_label}")
                inv_after = res.get("inventory_after") if isinstance(res.get("inventory_after"), dict) else None
                probe_cache: dict[tuple[int, int, int], str] = {}
                vtun = self._probe_viable_tunnel_dirs(
                    end_cell[0],
                    end_cell[1],
                    end_cell[2],
                    probe_cache=probe_cache,
                    exclude_dirs={d_key} if d_key in {"north", "south", "east", "west"} else None,
                    inv_counts=inv_after,
                )
                vup = self._probe_viable_up_stair_dirs(
                    end_cell[0],
                    end_cell[1],
                    end_cell[2],
                    probe_cache=probe_cache,
                    inv_counts=inv_after,
                )
                res["viable_tunnel_directions"] = vtun
                res["viable_up_directions"] = vup
                orig_hint = str(res.get("hint") or "").strip()
                base_hint = orig_hint.rstrip("。")
                for suffix_phrase in ("，请换方向", "或换方向"):
                    if base_hint.endswith(suffix_phrase):
                        base_hint = base_hint[: -len(suffix_phrase)]
                if vtun:
                    up_opt = (
                        f"；也可改调 mc_skill_staircase(direction='{vup[0]}', vertical='up', steps=6) 向上升层"
                        if vup
                        else ""
                    )
                    res["hint"] = (
                        f"{base_hint}；当前坐标{list(end_cell)}实测可行水平开挖方向: {','.join(vtun)}"
                        f"（可改调 mc_skill_tunnel(direction='{vtun[0]}', steps=6)）{up_opt}。"
                    ).lstrip("；")
                elif vup:
                    res["hint"] = (
                        f"{base_hint}；当前坐标{list(end_cell)}水平四向均无法继续平推(viable_tunnel_directions=[])，"
                        f"严禁继续原地调用 mc_skill_tunnel！请改调 mc_skill_staircase(direction='{vup[0]}', vertical='up', steps=6) 向上退回上层再分支(可选上行方向: {','.join(vup)})。"
                    ).lstrip("；")
                else:
                    vpillar = self._probe_viable_pillar_up(
                        end_cell[0],
                        end_cell[1],
                        end_cell[2],
                        probe_cache=probe_cache,
                        inv_counts=inv_after,
                    )
                    res["viable_pillar_up"] = vpillar
                    if vpillar:
                        res["hint"] = (
                            f"{base_hint}；当前坐标{list(end_cell)}水平四向与斜向阶梯上行均受阻"
                            "(viable_tunnel_directions=[], viable_up_directions=[])，但头顶正上方安全且有垫脚方块(viable_pillar_up=true)，"
                            "请改调 mc_skill_staircase(vertical='pillar', steps=4) 原地垂直挖顶垫脚直上脱困！"
                        ).lstrip("；")
                    else:
                        res["hint"] = (
                            f"{base_hint}；当前坐标{list(end_cell)}水平通道与常规向上阶梯均受阻(viable_tunnel_directions=[], viable_up_directions=[], viable_pillar_up=false)，"
                            "严禁继续原地重复调用 mc_skill_tunnel/mc_skill_staircase；请调 mc_move_to 沿已挖矿道退回或确认头顶安全后用 mc_dig/mc_place 脱困。"
                        ).lstrip("；")

            head_order = (
                "ok",
                "requires_replan",
                "direction",
                "steps_requested",
                "steps_done",
                "stop_reason",
                "blocker",
                "last_error",
                "viable_tunnel_directions",
                "viable_up_directions",
                "viable_pillar_up",
                "hint",
            )
            ordered: dict[str, Any] = {}
            for k in head_order:
                if k in res:
                    ordered[k] = res[k]
            for k, v in res.items():
                if k not in ordered:
                    ordered[k] = v
            return ordered
        return res

    def _dig_pillar_up(self, steps: int = 4, stop_on_ore: bool = True) -> dict[str, Any]:
        """Dig 1x1 overhead safely and jump-place underfoot blocks to climb straight up."""
        steps = max(1, min(12, int(steps)))
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
        hazard_set = fluids | falling_set
        ore_types = ["iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore", "copper_ore"]
        iron_names = {"iron_ore", "deepslate_iron_ore"}
        filler_candidates = STAIR_FILLER_BLOCKS

        def _bname(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        def _pick_filler() -> str | None:
            counts = _inventory_counts(self.bot)
            for cand in filler_candidates:
                if counts.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(counts.items()):
                if cnt >= 1 and name.endswith("_planks"):
                    return name
            return None

        def _confirm_pillar_surface(cur: list[float]) -> dict[str, Any] | None:
            cx, cy, cz = (math.floor(float(v)) for v in cur)
            below = _bname([cx, cy - 1, cz])
            surface_ground = {"grass_block", "podzol", "mycelium", "sand", "red_sand", "snow_block"}
            if below in surface_ground and all(_bname([cx, cy + dy, cz]) in air_set for dy in (0, 1)):
                return {"floor": below, "pos": list(cur), "basis": "standing_on_surface_ground"}
            try:
                surf = self.bot.call("find_blocks", {"radius": 5, "types": ["grass_block", "podzol", "sand"], "limit": 8})
            except ApiError:
                return None
            same_level = [b for b in surf.get("blocks", []) if b.get("pos") and abs(float(b["pos"][1]) + 1 - cy) <= 1]
            if same_level and all(_bname([cx, cy + dy, cz]) in air_set for dy in range(2, 14)):
                return {"floor": below, "pos": list(cur), "basis": "same_level_surface_and_clear_column"}
            return None

        st0 = self.bot.status()
        pos_before = st0.get("pos") or [0.0, 64.0, 0.0]
        last_death = (st0.get("last_death") or {}).get("t")
        min_hp = st0.get("health")
        steps_done = 0
        dug_count = 0
        placed_count = 0
        stop_reason = "completed"
        blocker_info: dict[str, Any] | None = None
        last_error: dict[str, Any] | None = None
        ores_nearby: list[dict[str, Any]] = []
        surface_evidence: dict[str, Any] | None = None

        try:
            for step_idx in range(steps):
                if isinstance(self.bot, _StairSession):
                    self.bot.step_deadline = min(self.bot.deadline, time.monotonic() + self.bot.per_step_timeout)
                st = self.bot.status()
                if st.get("alive") is False or (st.get("last_death") or {}).get("t") != last_death:
                    stop_reason = "died"
                    break
                if st.get("ready") is False:
                    stop_reason = "not_ready"
                    break
                if st.get("in_water") or st.get("in_lava"):
                    stop_reason = "entered_fluid"
                    break
                hp_now = st.get("health")
                if isinstance(min_hp, (int, float)) and isinstance(hp_now, (int, float)) and hp_now < min_hp:
                    stop_reason = "hurt"
                    break
                if isinstance(hp_now, (int, float)):
                    min_hp = hp_now

                pos = st.get("pos") or pos_before
                px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
                bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

                hit_col: dict[str, Any] | None = None
                for sc in ([bx, by + 1, bz], [bx, by + 2, bz]):
                    sb = _bname(sc)
                    if sb in fluid_or_bedrock:
                        hit_col = {"pos": sc, "block": sb, "relation": "in_path"}
                        stop_reason = "fluid_or_bedrock"
                        break
                if hit_col is not None:
                    blocker_info = hit_col
                    break

                top3_pos = [bx, by + 3, bz]
                top3 = _bname(top3_pos)
                if top3 in fluids:
                    blocker_info = {"pos": top3_pos, "block": top3, "relation": "overhead_fluid"}
                    stop_reason = "fluid_or_bedrock"
                    break
                if top3 in falling_set:
                    blocker_info = {"pos": top3_pos, "block": top3, "relation": "falling_block"}
                    stop_reason = "falling_material_adjacent"
                    break

                dig_abort = False
                for tpos in ([bx, by + 1, bz], [bx, by + 2, bz]):
                    tname = _bname(tpos)
                    if tname in air_set:
                        continue
                    for ax, az in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nb_pos = [tpos[0] + ax, tpos[1], tpos[2] + az]
                        nb_name = _bname(nb_pos)
                        if nb_name in fluids:
                            blocker_info = {"pos": nb_pos, "block": nb_name, "relation": "adjacent_fluid", "target": tpos}
                            stop_reason = "fluid_or_bedrock"
                            dig_abort = True
                            break
                        if nb_name in falling_set:
                            blocker_info = {"pos": nb_pos, "block": nb_name, "relation": "falling_block", "target": tpos}
                            stop_reason = "falling_material_adjacent"
                            dig_abort = True
                            break
                    if dig_abort:
                        break
                    req_tier = BLOCK_MIN_PICKAXE_TIER.get(tname, 0)
                    pick_name, pick_tier = _best_pickaxe_in_inv(_inventory_counts(self.bot))
                    if req_tier > pick_tier:
                        blocker_info = {
                            "pos": tpos,
                            "block": tname,
                            "relation": "insufficient_pickaxe_tier",
                            "required_tier": req_tier,
                            "current_pickaxe": pick_name,
                            "current_tier": pick_tier,
                        }
                        stop_reason = "insufficient_pickaxe_tier"
                        dig_abort = True
                        break
                    if req_tier > 0 or any(tag in tname for tag in ("stone", "ore", "deepslate", "granite", "diorite", "andesite", "tuff")):
                        self._equip_if_present(tuple(PICKAXE_TIERS))
                    try:
                        self.bot.call("dig", {"pos": tpos, "collect": True, "stop_on": ["hurt", "death"]})
                    except ApiError as err:
                        last_error = {"code": err.code, "message": err.message, "pos": tpos}
                        stop_reason = "dig_failed"
                        dig_abort = True
                        break
                    if _bname(tpos) not in air_set:
                        last_error = {"code": "dig_not_confirmed", "message": f"挖掘 {tpos} 后仍非空气", "pos": tpos}
                        stop_reason = "dig_failed"
                        dig_abort = True
                        break
                    dug_count += 1
                if dig_abort:
                    break

                filler = _pick_filler()
                if not filler:
                    stop_reason = "missing_step_support"
                    break

                if max(abs(px - (bx + 0.5)), abs(pz - (bz + 0.5))) > 0.28:
                    try:
                        self.bot.call("move_to", {"x": bx + 0.5, "y": by, "z": bz + 0.5, "range": 0.2, "timeout": 1500})
                    except ApiError:
                        pass

                placed_ok = False
                place_failures = 0
                retry_after = 0.0
                self.bot.call("posture", {"sneak": False, "shield": False, "jump": True})
                try:
                    for attempt in range(48):
                        time.sleep(0.05)
                        st_j = self.bot.status()
                        if st_j.get("alive") is False or st_j.get("ready") is False:
                            stop_reason = "not_ready" if st_j.get("ready") is False else "died"
                            break
                        if time.monotonic() < retry_after:
                            continue
                        jpos = st_j.get("pos") or [bx + 0.5, by, bz + 0.5]
                        if float(jpos[1]) < by + 1.0 and attempt < 2:
                            continue
                        try:
                            self.bot.call("place", {"item": filler, "pos": [bx, by, bz]})
                            placed_ok = True
                            placed_count += 1
                            break
                        except ApiError as perr:
                            if perr.code == "blocked_by_self":
                                continue
                            last_error = {"code": perr.code, "message": perr.message, "pos": [bx, by, bz]}
                            if perr.code in {"place_failed", "no_support"} and place_failures < 2:
                                # r14 portal recovery: the first jump-place was
                                # rejected while airborne, then the next whole
                                # call succeeded. D1/BUG-024: the same path can
                                # answer no_support before the new floor settles.
                                # Wait one jump cycle and retry.
                                place_failures += 1
                                retry_after = time.monotonic() + 0.45
                                continue
                            break
                finally:
                    try:
                        self.bot.call("posture", {"sneak": False, "shield": False, "jump": False})
                    except ApiError:
                        pass
                if not placed_ok:
                    if stop_reason == "completed":
                        stop_reason = "place_failed"
                    break

                settled_up = False
                cur_pos = [bx + 0.5, float(by), bz + 0.5]
                for _ in range(8):
                    time.sleep(0.05)
                    st_after_step = self.bot.status()
                    raw_p = st_after_step.get("pos") or cur_pos
                    cur_pos = [float(raw_p[0]), float(raw_p[1]), float(raw_p[2])]
                    if math.floor(cur_pos[1]) >= by + 1:
                        settled_up = True
                        break
                if not settled_up:
                    stop_reason = "movement_not_confirmed"
                    break

                steps_done += 1
                if isinstance(self.bot, _StairSession):
                    self.bot.progress.update(steps_done=steps_done, dug_count=dug_count, placed_count=placed_count)
                if steps_done >= 2:
                    surface_evidence = _confirm_pillar_surface(cur_pos)
                    if surface_evidence:
                        stop_reason = "reached_surface"
                        break

                if stop_on_ore and (steps_done % 2 == 0 or step_idx == steps - 1):
                    try:
                        found_ores = self.bot.call("find_blocks", {"radius": 8, "types": ore_types, "limit": 10})
                        ores_nearby = found_ores.get("blocks") or []
                        if any(b.get("name") in iron_names for b in ores_nearby):
                            stop_reason = "iron_ore_found"
                            break
                    except ApiError:
                        pass
        except ApiError as exc:
            stop_reason = "api_error"
            last_error = {"code": exc.code, "message": exc.message, **exc.context}

        if isinstance(self.bot, _StairSession):
            self.bot.step_deadline = self.bot.deadline
        st_end = self.bot.status()
        end_pos = st_end.get("pos") or pos_before
        ok = steps_done > 0 and stop_reason in {"completed", "iron_ore_found", "reached_surface"}
        out_pillar: dict[str, Any] = {
            "ok": ok,
            "requires_replan": not ok,
            "direction": "pillar",
            "vertical": "pillar",
            "steps_requested": steps,
            "steps_done": steps_done,
            "dug_count": dug_count,
            "placed_count": placed_count,
            "stop_reason": stop_reason,
            "pos_before": pos_before,
            "pos_after": end_pos,
        }
        if blocker_info is not None:
            out_pillar["blocker"] = blocker_info
        if last_error is not None:
            out_pillar["last_error"] = last_error

        if not ok:
            # A failed jump-place is read back while the bot is still airborne
            # (r12: pos_after y=97.2 for a grounded start at y=96). With no
            # confirmed step the bot falls back to the start cell, so remember
            # the failure there; otherwise the next probe advertises
            # viable_pillar_up=true at the exact cell that just failed.
            record_pos = pos_before if steps_done == 0 else end_pos
            ebx, eby, ebz = (
                math.floor(float(record_pos[0])),
                math.floor(float(record_pos[1])),
                math.floor(float(record_pos[2])),
            )
            self._record_failed_stair_dir((ebx, eby, ebz, "pillar"), "pillar", stop_reason)
            if steps_done > 0 and st_end.get("on_ground") is False:
                # Partial climb: the last confirmed step is one block below
                # the airborne read-back; block retries there as well.
                self._record_failed_stair_dir((ebx, eby - 1, ebz, "pillar"), "pillar", stop_reason)
            probe_cache_p: dict[tuple[int, int, int], str] = {}
            inv_p = _inventory_counts(self.bot)
            up_dirs = self._probe_viable_up_stair_dirs(ebx, eby, ebz, probe_cache=probe_cache_p, inv_counts=inv_p)
            tunnel_dirs = self._probe_viable_tunnel_dirs(ebx, eby, ebz, probe_cache=probe_cache_p, inv_counts=inv_p)
            out_pillar["viable_up_directions"] = up_dirs
            out_pillar["viable_tunnel_directions"] = tunnel_dirs
            detail_p = (
                f"{stop_reason}({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
                if blocker_info
                else (
                    f"{stop_reason}({last_error['code']}@{last_error.get('pos')})"
                    if last_error and last_error.get("code")
                    else stop_reason
                )
            )
            if up_dirs:
                out_pillar["hint"] = (
                    f"原地垂直垫脚直上遇到{detail_p}；实测可改走斜向阶梯上行方向: {','.join(up_dirs)}"
                    f"（请调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6)）。"
                )
            elif tunnel_dirs:
                out_pillar["hint"] = (
                    f"原地垂直垫脚直上遇到{detail_p}；实测可改走水平矿道方向: {','.join(tunnel_dirs)}"
                    f"（请调 mc_skill_tunnel(direction='{tunnel_dirs[0]}', steps=6)）。"
                )
            else:
                out_pillar["hint"] = (
                    f"原地垂直垫脚直上遇到{detail_p}；当前坐标[{ebx},{eby},{ebz}]斜向上行与水平矿道也受阻，"
                    "请调 mc_move_to 退回安全矿道或先用原子工具清理周围障碍。"
                )

        if surface_evidence is not None:
            out_pillar["surface_evidence"] = surface_evidence
        out_pillar["ores_nearby"] = ores_nearby
        return out_pillar

    def dig_staircase(
        self,
        direction: str | None = None,
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool | None = None,
        *,
        deadline: float | None = None,
        per_step_timeout: float = 15.0,
    ) -> dict[str, Any]:
        session = _StairSession(self.bot, 45.0, deadline=deadline, per_step_timeout=per_step_timeout)
        scoped = copy(self)
        scoped.bot = session
        try:
            return scoped._dig_staircase(direction, steps, vertical, stop_on_ore)
        except _StairDeadline:
            cleanup: dict[str, Any] = {}
            try:
                cleanup = self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
            except (ApiError, httpx.HTTPError):
                pass
            if str(vertical).lower() == "pillar" or str(direction).lower() == "pillar":
                try:
                    # The scoped session has expired, so its ordinary finally
                    # cannot clear the persistent jump posture.
                    self.bot.call("posture", {"jump": False, "sneak": False, "shield": False}, timeout_s=1.0, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
            timeout_message = "阶梯总预算或单步等待耗尽；请根据实际位置重新规划。"
            pick_tier = getattr(session, "pickaxe_tier", None)
            low_tier_slow_dig = False
            if session.last_timeout_path == "dig":
                timeout_message = (
                    f"单步挖掘未在预算内确认（目标 {session.last_dig_pos}）。"
                    "若目标位于脚下、为黑曜石/高硬度方块，或同一平台连续超时，"
                    "先调 mc_skill_unstick 并离开该处 8~12 格后再挖；不要原地重复同一方向。"
                )
                # BUG-041/r22_long: with a wooden pickaxe every stone staircase
                # step costs ~25s, so the model loops for ~30s per 1 block.
                if isinstance(pick_tier, int) and pick_tier < 2:
                    low_tier_slow_dig = True
                    blk = session.last_dig_block or "硬质方块"
                    tier_label = {0: "无镐/空手", 1: "木镐"}.get(pick_tier, f"tier {pick_tier}")
                    timeout_message = (
                        f"单步挖掘未在预算内确认：当前镐等级 {pick_tier}（{tier_label}）挖 {blk} "
                        f"约需 25s/格，45s 总预算只够 1 格。"
                        "先调 mc_skill_craft_gear(item='stone_pickaxe')（有圆石时），"
                        "或改走泥土/砂砾/沙子等软方块方向，或先 mc_skill_harvest_logs 补木料；"
                        "也可先 mc_skill_unstick 让开该格；不要在硬岩上原地重复同一步梯。"
                    )
                if isinstance(session.last_dig_pos, (list, tuple)) and len(session.last_dig_pos) >= 3:
                    try:
                        self._avoid_temporarily(
                            (int(session.last_dig_pos[0]), int(session.last_dig_pos[1]), int(session.last_dig_pos[2])),
                            "dig_timeout",
                            30.0,
                        )
                    except (TypeError, ValueError):
                        pass
            return {
                "ok": False, "requires_replan": True, "stop_reason": "time_budget",
                "error": {"code": "time_budget", "message": timeout_message},
                "direction": direction, "vertical": vertical, "steps_requested": steps,
                **session.progress, "pos_after": session.last_status.get("pos"),
                "timeout_path": session.last_timeout_path,
                "retried_path": session.last_retry_path,
                "dig_target": session.last_dig_pos,
                "dig_block": session.last_dig_block,
                "pickaxe_tier": pick_tier,
                "low_tier_slow_dig": low_tier_slow_dig,
                "action_released": cleanup.get("released", cleanup.get("stopped", False)),
            }

    def _dig_staircase(
        self,
        direction: str | None = None,
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool | None = None,
    ) -> dict[str, Any]:
        """Dig a safe 3-high stepped staircase downward, upward, or straight-up pillar."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        d_key = str(direction or "").strip().lower()
        v_key = str(vertical or "down").strip().lower()
        if stop_on_ore is None:
            stop_on_ore = v_key == "down"
        if v_key == "pillar" or d_key == "pillar":
            return self._dig_pillar_up(steps=steps, stop_on_ore=stop_on_ore)
        if v_key not in {"down", "up"}:
            return {
                "ok": False,
                "error": "bad_args",
                "message": "direction 必须是 north/south/east/west，vertical 必须是 down/up/pillar。",
            }
        if not d_key or d_key in {"down", "up"}:
            chosen_dir = "north"
            try:
                st_probe = self.bot.status() or {}
                p_probe = st_probe.get("pos") or []
                if isinstance(p_probe, (list, tuple)) and len(p_probe) >= 3:
                    px, py, pz = math.floor(float(p_probe[0])), math.floor(float(p_probe[1])), math.floor(float(p_probe[2]))
                    cands = (
                        self._probe_viable_down_stair_dirs(px, py, pz)
                        if v_key == "down"
                        else self._probe_viable_up_stair_dirs(px, py, pz)
                    )
                    if cands:
                        chosen_dir = cands[0]
            except ApiError:
                pass
            d_key = chosen_dir
        if d_key not in dirs:
            return {
                "ok": False,
                "error": "bad_args",
                "message": "direction 必须是 north/south/east/west，vertical 必须是 down/up/pillar。",
            }

        dx, dz = dirs[d_key]
        steps = max(1, min(25, int(steps)))
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        # r14: a fortress/nether portal head cell was repeatedly dug and
        # returned undiggable for all four directions. Treat these as
        # obstacles, not as dig targets.
        undiggable_obstacles = {"nether_portal", "end_portal", "end_gateway", "barrier"}
        non_solid = air_set | fluids | {
            "short_grass",
            "tall_grass",
            "leaf_litter",
            "dandelion",
            "poppy",
            "oak_sapling",
        }
        ore_types = [
            "iron_ore",
            "deepslate_iron_ore",
            "diamond_ore",
            "deepslate_diamond_ore",
            "gold_ore",
            "deepslate_gold_ore",
            "coal_ore",
            "deepslate_coal_ore",
            "copper_ore",
        ]
        stop_ore_names = {
            "iron_ore",
            "deepslate_iron_ore",
            "diamond_ore",
            "deepslate_diamond_ore",
        }
        surface_evidence: dict[str, Any] | None = None

        def _filter_actionable_ores(
            blocks: list[dict[str, Any]], cur_xyz: list[float]
        ) -> list[dict[str, Any]]:
            cx = float(cur_xyz[0]) if len(cur_xyz) >= 3 else 0.0
            cy = float(cur_xyz[1]) if len(cur_xyz) >= 3 else 64.0
            cz = float(cur_xyz[2]) if len(cur_xyz) >= 3 else 0.0
            kept: list[dict[str, Any]] = []
            for blk in blocks:
                if blk.get("name") not in stop_ore_names:
                    continue
                bpos = blk.get("pos")
                if isinstance(bpos, list) and len(bpos) >= 3:
                    ox, oy, oz = int(bpos[0]), int(bpos[1]), int(bpos[2])
                    if self._is_block_avoided((ox, oy, oz)):
                        continue
                    forward_dot = (float(ox) - cx) * dx + (float(oz) - cz) * dz
                    if forward_dot < -1.5:
                        continue
                    if v_key == "down" and float(oy) > cy + 2.5:
                        continue
                    if v_key == "up" and float(oy) < cy - 2.5:
                        continue
                kept.append(blk)
            return kept

        def _block_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        def _is_non_solid_support(bname: str) -> bool:
            return bname in non_solid or bname.endswith(("_leaves", "_sapling", "_carpet", "_banner"))

        def _find_adjacent_fluid(
            targets: list[list[int]],
            cache: dict[tuple[int, int, int], str] | None = None,
        ) -> dict[str, Any] | None:
            def _cached_block(pt: tuple[int, int, int]) -> str:
                if cache is not None and pt in cache:
                    return cache[pt]
                val = _block_name([pt[0], pt[1], pt[2]])
                if cache is not None:
                    cache[pt] = val
                return val

            for pos_item in targets:
                px, py, pz = int(pos_item[0]), int(pos_item[1]), int(pos_item[2])
                if _cached_block((px, py, pz)) in air_set:
                    continue
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = (px + ax, py + ay, pz + az)
                    bname = _cached_block(nb)
                    if bname in fluids:
                        return {
                            "pos": [nb[0], nb[1], nb[2]],
                            "block": bname,
                            "relation": "adjacent_fluid",
                            "target": [px, py, pz],
                        }
            return None

        def _equip_best_pick() -> None:
            best_pick, _ = _best_pickaxe_in_inv(_inventory_counts(self.bot))
            if best_pick:
                try:
                    self.bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass

        def _pick_step_filler(exclude: set[str] | None = None) -> str | None:
            excluded = exclude or set()
            counts = _inventory_counts(self.bot)
            for cand in STAIR_FILLER_BLOCKS:
                if cand not in excluded and counts.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(counts.items()):
                if name not in excluded and cnt >= 1 and name.endswith("_planks"):
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
        # BUG-041: remember the pickaxe tier so a dig timeout can name the real
        # cause (wooden pickaxe on hard rock costs ~25s per block).
        try:
            self.bot.pickaxe_tier = _best_pickaxe_in_inv(_inventory_counts(self.bot))[1]
        except (ApiError, httpx.HTTPError, TypeError, ValueError):
            self.bot.pickaxe_tier = None
        st0 = self.bot.status()
        pos_before = st0.get("pos") or [0.0, 64.0, 0.0]
        steps_done = 0
        dug_count = 0
        placed_count = 0
        stop_reason = "completed"
        ores_nearby: list[dict[str, Any]] = []
        matched_ores: list[dict[str, Any]] = []
        last_error: dict[str, Any] | None = None
        blocker_info: dict[str, Any] | None = None
        b0x, b0y, b0z = (
            math.floor(float(pos_before[0])),
            math.floor(float(pos_before[1])),
            math.floor(float(pos_before[2])),
        )

        if v_key == "down":
            if _block_name([b0x, b0y - 1, b0z]).endswith("_leaves"):
                self.descend_canopy()
                st0 = self.bot.status()
                cur_p = st0.get("pos") or pos_before
                c0x, c0y, c0z = (
                    math.floor(float(cur_p[0])),
                    math.floor(float(cur_p[1])),
                    math.floor(float(cur_p[2])),
                )
                b0x, b0y, b0z = c0x, c0y, c0z
                if _block_name([c0x, c0y - 1, c0z]).endswith("_leaves"):
                    stop_reason = "on_tree_canopy"

        stair_deadline = self.bot.deadline
        for step_idx in range(steps if stop_reason == "completed" else 0):
            # BUG-037: the first step often needs walk+dig+confirm and was
            # timing out at per_step_timeout with zero progress. Give step 0
            # the remaining total budget; later steps keep the per-step cap.
            self.bot.step_deadline = (
                self.bot.deadline
                if step_idx == 0 and self.bot.per_step_timeout >= 10.0
                else min(self.bot.deadline, time.monotonic() + self.bot.per_step_timeout)
            )
            if step_idx > 0 and time.monotonic() >= stair_deadline:
                stop_reason = "time_budget"
                break
            if step_idx > 0 and step_idx % 6 == 0:
                _equip_best_pick()

            st = self.bot.status()
            pos = st.get("pos") or pos_before
            bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
            nx, nz = bx + dx, bz + dz

            # BUG-041/r23: when a dig cell already timed out here, every retry
            # burns another 15~45s on the same unreachable block. Fail fast and
            # tell the model to relocate instead.
            avoided_cell = next(
                (
                    cell
                    for cell in (
                        (bx, by + 1, bz), (bx, by + 2, bz), (bx, by - 1, bz),
                        (nx, by + 1, nz), (nx, by + 2, nz),
                    )
                    if self._is_block_avoided(cell)
                    and (self.temporary_avoidance.get(cell) or {}).get("reason") == "dig_timeout"
                ),
                None,
            )
            if avoided_cell is not None and step_idx == 0:
                stop_reason = "dig_cell_avoided"
                last_error = {
                    "code": "dig_cell_avoided",
                    "pos": list(avoided_cell),
                    "message": "该格刚发生 dig 超时，已临时避开 30s，未再次挖掘。",
                }
                break

            head_here = _block_name([bx, by + 1, bz])
            if head_here in fluid_or_bedrock:
                blocker_info = {"pos": [bx, by + 1, bz], "block": head_here, "relation": "overhead"}
                stop_reason = "fluid_or_bedrock"
                break
            if head_here in undiggable_obstacles:
                blocker_info = {"pos": [bx, by + 1, bz], "block": head_here, "relation": "undiggable"}
                last_error = {"code": "undiggable_block", "block": head_here, "pos": [bx, by + 1, bz]}
                stop_reason = "undiggable_block"
                break
            if head_here not in air_set:
                adj_head = _find_adjacent_fluid([[bx, by + 1, bz]])
                if adj_head is not None:
                    blocker_info = adj_head
                    stop_reason = "fluid_or_bedrock"
                    break
                try:
                    self.bot.call("dig", {"pos": [bx, by + 1, bz], "collect": True, "stop_on": ["death"]})
                    dug_count += 1
                except ApiError as err:
                    last_error = {"code": err.code, "message": err.message, "pos": [bx, by + 1, bz]}
                    stop_reason = "dig_failed"
                    break
            if v_key == "up":
                apex_here = _block_name([bx, by + 2, bz])
                if apex_here in fluid_or_bedrock:
                    blocker_info = {"pos": [bx, by + 2, bz], "block": apex_here, "relation": "overhead_apex"}
                    stop_reason = "fluid_or_bedrock"
                    break
                if apex_here in undiggable_obstacles:
                    blocker_info = {"pos": [bx, by + 2, bz], "block": apex_here, "relation": "overhead_undiggable"}
                    last_error = {"code": "undiggable_block", "block": apex_here, "pos": [bx, by + 2, bz]}
                    stop_reason = "undiggable_block"
                    break
                if apex_here not in air_set:
                    adj_apex = _find_adjacent_fluid([[bx, by + 2, bz]])
                    if adj_apex is not None:
                        blocker_info = adj_apex
                        stop_reason = "fluid_or_bedrock"
                        break
                    try:
                        self.bot.call("dig", {"pos": [bx, by + 2, bz], "collect": True, "stop_on": ["death"]})
                        dug_count += 1
                    except ApiError as err:
                        last_error = {"code": err.code, "message": err.message, "pos": [bx, by + 2, bz]}
                        stop_reason = "dig_failed"
                        break

            if v_key == "down":
                safety_coords = [[nx, by + 2, nz], [nx, by + 1, nz], [nx, by, nz], [nx, by - 1, nz]]
                hit_safety = None
                for sc in safety_coords:
                    sb = _block_name(sc)
                    if sb in fluid_or_bedrock:
                        hit_safety = {"pos": sc, "block": sb, "relation": "in_path"}
                        break
                if hit_safety is not None:
                    blocker_info = hit_safety
                    stop_reason = "fluid_or_bedrock"
                    break
                support_blk = _block_name([nx, by - 2, nz])
                if support_blk in fluids:
                    blocker_info = {"pos": [nx, by - 2, nz], "block": support_blk, "relation": "floor_fluid"}
                    stop_reason = "fluid_or_bedrock"
                    break
                if _is_non_solid_support(support_blk):
                    blocker_info = {"pos": [nx, by - 2, nz], "block": support_blk, "relation": "missing_floor_support"}
                    stop_reason = "cave_or_drop_ahead"
                    break
                down_targets = [[nx, by + 1, nz], [nx, by, nz], [nx, by - 1, nz]]
                adj_down = _find_adjacent_fluid(down_targets, {})
                if adj_down is not None:
                    blocker_info = adj_down
                    stop_reason = "fluid_or_bedrock"
                    break

                dig_failed = False
                for idx, dpos in enumerate(down_targets):
                    dname = _block_name(dpos)
                    if dname in undiggable_obstacles:
                        blocker_info = {"pos": dpos, "block": dname, "relation": "undiggable"}
                        last_error = {"code": "undiggable_block", "block": dname, "pos": dpos}
                        dig_failed = True
                        break
                    if dname not in air_set:
                        try:
                            self.bot.call("dig", {"pos": dpos, "collect": True})
                            dug_count += 1
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": dpos}
                            dig_failed = True
                            break
                        if idx >= 1:
                            for _ in range(2):
                                remaining_block = _block_name(dpos)
                                if remaining_block in air_set:
                                    break
                                if remaining_block in fluid_or_bedrock:
                                    last_error = {"code": "unsafe_refilled_cell", "pos": dpos, "block": remaining_block}
                                    dig_failed = True
                                    break
                                try:
                                    self.bot.last_dig_block = _block_name(dpos)
                                    self.bot.call("dig", {"pos": dpos, "collect": True, "stop_on": ["death"]})
                                    dug_count += 1
                                except ApiError as err:
                                    last_error = {"code": err.code, "message": err.message, "pos": dpos}
                                    dig_failed = True
                                    break
                            if not dig_failed and _block_name(dpos) in {"sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel"}:
                                last_error = {"code": "stair_cell_not_cleared", "pos": dpos}
                                dig_failed = True
                            if dig_failed:
                                break
                if dig_failed:
                    stop_reason = (
                        "undiggable_block"
                        if last_error and last_error.get("code") == "undiggable_block"
                        else "dig_failed"
                    )
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
                                        dug_count += 1
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
                hit_up = None
                for sc in ([bx, by + 2, bz], [nx, by + 1, nz], [nx, by + 2, nz]):
                    sb = _block_name(sc)
                    if sb in fluid_or_bedrock:
                        hit_up = {"pos": sc, "block": sb, "relation": "in_path"}
                        break
                if hit_up is not None:
                    blocker_info = hit_up
                    stop_reason = "fluid_or_bedrock"
                    break
                for sc in ([bx, by + 3, bz], [nx, by + 3, nz]):
                    sb = _block_name(sc)
                    if sb in fluids:
                        hit_up = {"pos": sc, "block": sb, "relation": "overhead_fluid"}
                        break
                if hit_up is not None:
                    blocker_info = hit_up
                    stop_reason = "fluid_or_bedrock"
                    break
                up_targets = [[bx, by + 2, bz], [nx, by + 2, nz], [nx, by + 1, nz]]
                adj_up = _find_adjacent_fluid(up_targets, {})
                if adj_up is not None:
                    blocker_info = adj_up
                    stop_reason = "fluid_or_bedrock"
                    break

                dig_failed = False
                for dpos in up_targets:
                    uname = _block_name(dpos)
                    if uname in undiggable_obstacles:
                        blocker_info = {"pos": dpos, "block": uname, "relation": "undiggable"}
                        last_error = {"code": "undiggable_block", "block": uname, "pos": dpos}
                        dig_failed = True
                        break
                    if uname not in air_set:
                        try:
                            self.bot.last_dig_block = uname
                            self.bot.call("dig", {"pos": dpos, "collect": True})
                            dug_count += 1
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": dpos}
                            dig_failed = True
                            break
                if dig_failed:
                    stop_reason = (
                        "undiggable_block"
                        if last_error and last_error.get("code") == "undiggable_block"
                        else "dig_failed"
                    )
                    break

                if _is_non_solid_support(_block_name([nx, by, nz])):
                    # BUG-024: in a self-dug 1x1 shaft the step cell can have
                    # no solid face at all; place would always answer no_support.
                    # One safe pillar step is the only way to keep climbing.
                    face_cells = (
                        (nx, by - 1, nz), (nx + 1, by, nz), (nx - 1, by, nz),
                        (nx, by, nz + 1), (nx, by, nz - 1), (nx, by + 1, nz),
                    )
                    has_place_face = any(
                        not _is_non_solid_support(_block_name(cell)) for cell in face_cells
                    )
                    if not has_place_face:
                        recovery = self._dig_pillar_up(steps=1, stop_on_ore=False)
                        if recovery.get("ok"):
                            steps_done += int(recovery.get("steps_done") or 0)
                            dug_count += int(recovery.get("dug_count") or 0)
                            placed_count += int(recovery.get("placed_count") or 0)
                            stop_reason = "pillar_fallback"
                        else:
                            last_error = recovery.get("last_error") or {
                                "code": "no_place_face", "pos": [nx, by, nz],
                            }
                            stop_reason = "missing_step_support"
                        break
                    # The client inventory can disagree with the server's
                    # findItem (r13: cobblestone reported but place said none).
                    # Fall through to other solid fillers before giving up.
                    rejected_fillers: set[str] = set()
                    filler = _pick_step_filler()
                    placed_ok = False
                    place_failures = 0
                    while filler:
                        try:
                            self.bot.call("place", {"item": filler, "pos": [nx, by, nz]})
                            placed_count += 1
                            placed_ok = True
                            break
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": [nx, by, nz]}
                            if err.code == "place_failed" and place_failures < 2:
                                # r14: an up-step filler placement was refused
                                # once while the bot settled, then succeeded on
                                # a later attempt.
                                place_failures += 1
                                time.sleep(0.25)
                                continue
                            if err.code != "missing_item":
                                break
                            rejected_fillers.add(filler)
                            filler = _pick_step_filler(exclude=rejected_fillers)
                    if not placed_ok:
                        stop_reason = "missing_step_support"
                        break

                cur = _advance_step(nx, by + 1, nz, bx, by, bz, jump=True)
                if math.floor(float(cur[1])) != by + 1 or math.dist([float(cur[0]), float(cur[2])], [nx + 0.5, nz + 0.5]) > 0.85:
                    stop_reason = "movement_not_confirmed"
                    break

                steps_done += 1
                self.bot.progress.update(steps_done=steps_done, dug_count=dug_count)
                if step_idx >= 1:
                    surface_evidence = _confirm_surface(cur)
                    if surface_evidence:
                        stop_reason = "reached_surface"
                        break

                if stop_on_ore and steps_done % 3 == 0:
                    found_ores = self.bot.call("find_blocks", {"radius": 12, "types": ore_types, "limit": 10})
                    ores_nearby = found_ores.get("blocks") or []
                    matched_ores = _filter_actionable_ores(ores_nearby, cur)
                    if matched_ores:
                        stop_reason = "ore_detected"
                        break
                continue

            steps_done += 1
            self.bot.progress.update(steps_done=steps_done, dug_count=dug_count)
            if stop_on_ore and steps_done % 3 == 0:
                found_ores = self.bot.call("find_blocks", {"radius": 12, "types": ore_types, "limit": 10})
                ores_nearby = found_ores.get("blocks") or []
                matched_ores = _filter_actionable_ores(ores_nearby, cur)
                if matched_ores:
                    stop_reason = "ore_detected"
                    break

        self.bot.step_deadline = self.bot.deadline
        st_end = self.bot.status()
        if stop_on_ore and stop_reason == "completed":
            found_ores = self.bot.call("find_blocks", {"radius": 12, "types": ore_types, "limit": 10})
            ores_nearby = found_ores.get("blocks") or []
            matched_ores = _filter_actionable_ores(ores_nearby, st_end.get("pos") or pos_before)
            if matched_ores:
                stop_reason = "ore_detected"

        out_stair: dict[str, Any] = {
            "ok": stop_reason in {"completed", "ore_detected", "reached_surface", "pillar_fallback"} and (steps_done > 0 or stop_reason == "ore_detected"),
            "requires_replan": stop_reason not in {"completed", "ore_detected", "reached_surface", "pillar_fallback"},
            "direction": d_key,
            "vertical": v_key,
            "steps_requested": steps,
            "steps_done": steps_done,
            "dug_count": dug_count,
            "stop_reason": stop_reason,
            "pos_before": pos_before,
            "pos_after": st_end.get("pos"),
        }
        if matched_ores:
            out_stair["ores_detected"] = [
                {"name": b.get("name"), "pos": b.get("pos"), "dist": b.get("dist")}
                for b in matched_ores[:5]
            ]
            if stop_reason == "ore_detected":
                detected_names = list(dict.fromkeys(str(b.get("name")) for b in matched_ores if b.get("name")))
                if detected_names:
                    out_stair["hint"] = (
                        f"前方阶梯视野内探测到暴露矿脉 {', '.join(detected_names[:3])}；"
                        f"如需开采请调 mc_skill_mine_blocks(block_types={detected_names[:2]}, count=6)；"
                        f"若仅需继续沿 {d_key} 下探而不在此矿停驻，请传 stop_on_ore=False。"
                    )
        if placed_count > 0:
            out_stair["placed_count"] = placed_count
        if blocker_info is not None:
            out_stair["blocker"] = blocker_info
        if last_error is not None:
            out_stair["last_error"] = last_error
        if stop_reason == "pillar_fallback":
            out_stair["fallback"] = "pillar"
            out_stair["hint"] = (
                "目标台阶四周没有可放置面（no_support），已原地安全垫高一格。"
                "请从新高度重新调用 mc_skill_staircase；若仍无支撑，继续 vertical='pillar'。"
            )
        if stop_reason == "dig_cell_avoided":
            out_stair["hint"] = (
                "该格刚发生 dig 超时（同一格已临时避开 30s），本次未再挖掘。"
                "请先用 mc_move_to 水平离开 8~12 格或 mc_skill_unstick 换站位，"
                "再换方向重新开阶梯；不要重复同一坐标/方向。"
            )
        end_pos = st_end.get("pos") or pos_before
        ebx, eby, ebz = math.floor(float(end_pos[0])), math.floor(float(end_pos[1])), math.floor(float(end_pos[2]))
        if stop_reason in {"cave_or_drop_ahead", "fluid_or_bedrock", "dig_failed", "undiggable_block", "movement_not_confirmed", "missing_step_support"}:
            reason_label = (
                f"{stop_reason}:{last_error['code']}"
                if last_error and last_error.get("code")
                else (f"{stop_reason}:{blocker_info['block']}" if blocker_info else stop_reason)
            )
            self._record_failed_stair_dir((ebx, eby, ebz, v_key), d_key, reason_label)
            if steps_done > 0 and dug_count == 0 and (ebx, eby, ebz) != (b0x, b0y, b0z):
                self._record_failed_stair_dir((b0x, b0y, b0z, v_key), d_key, f"dead_end->{reason_label}")

        if v_key == "down" and stop_reason in {"cave_or_drop_ahead", "fluid_or_bedrock", "on_tree_canopy", "dig_failed", "undiggable_block", "movement_not_confirmed"}:
            probe_cache: dict[tuple[int, int, int], str] = {}

            def _probe_block(px: int, py: int, pz: int) -> str:
                pt = (px, py, pz)
                if pt not in probe_cache:
                    probe_cache[pt] = _block_name([px, py, pz])
                return probe_cache[pt]

            viable_dirs: list[str] = []
            direction_status: dict[str, str] = {}
            if stop_reason == "on_tree_canopy":
                for odir in dirs:
                    direction_status[odir] = "blocked(on_tree_canopy)"
            else:
                failed_dirs = self._active_failed_stair_dirs((ebx, eby, ebz, "down"))
                for odir, (odx, odz) in dirs.items():
                    if odir == d_key:
                        if blocker_info:
                            direction_status[odir] = (
                                f"blocked({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
                            )
                        elif last_error:
                            direction_status[odir] = (
                                f"failed({stop_reason}:{last_error.get('code')}@{last_error.get('pos')})"
                            )
                        else:
                            direction_status[odir] = f"failed({stop_reason})"
                        continue
                    if odir in failed_dirs:
                        direction_status[odir] = f"failed_recently({failed_dirs[odir]})"
                        continue
                    try:
                        support_blk = _probe_block(ebx + odx, eby - 2, ebz + odz)
                        if support_blk in fluid_or_bedrock:
                            direction_status[odir] = f"blocked(floor:{support_blk}@[{ebx + odx},{eby - 2},{ebz + odz}])"
                            continue
                        if _is_non_solid_support(support_blk):
                            direction_status[odir] = f"blocked(cave_drop:{support_blk}@[{ebx + odx},{eby - 2},{ebz + odz}])"
                            continue
                        hit_path = None
                        for ky in (2, 1, 0, -1):
                            fb = _probe_block(ebx + odx, eby + ky, ebz + odz)
                            if fb in fluid_or_bedrock:
                                hit_path = f"blocked(in_path:{fb}@[{ebx + odx},{eby + ky},{ebz + odz}])"
                                break
                        if hit_path is not None:
                            direction_status[odir] = hit_path
                            continue
                        adj = _find_adjacent_fluid(
                            [[ebx + odx, eby + 1, ebz + odz], [ebx + odx, eby, ebz + odz], [ebx + odx, eby - 1, ebz + odz]],
                            probe_cache,
                        )
                        if adj is not None:
                            direction_status[odir] = f"blocked(adjacent_fluid:{adj['block']}@{adj['pos']})"
                            continue
                        direction_status[odir] = "viable"
                        viable_dirs.append(odir)
                    except ApiError:
                        direction_status[odir] = "unknown(probe_error)"
                        continue
            out_stair["direction_status"] = direction_status
            out_stair["viable_directions"] = viable_dirs
            if blocker_info:
                detail_desc = f"{stop_reason}({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
            elif last_error and last_error.get("code"):
                detail_desc = f"{stop_reason}({last_error['code']}@{last_error.get('pos')})"
            else:
                detail_desc = stop_reason
            if viable_dirs:
                out_stair["hint"] = (
                    f"前方({d_key})遇到{detail_desc}，禁止继续朝{d_key}重试；"
                    f"实测可换向安全下探: {','.join(viable_dirs)}。"
                )
            else:
                out_stair["all_directions_blocked"] = True
                try:
                    roof_blk = _probe_block(ebx, eby + 2, ebz)
                except ApiError:
                    roof_blk = "air"
                is_underground = (eby < 62) or (roof_blk not in air_set and not is_passable_sight_block(roof_blk))
                if is_underground:
                    inv_now = _inventory_counts(self.bot)
                    tunnel_dirs = self._probe_viable_tunnel_dirs(
                        ebx,
                        eby,
                        ebz,
                        probe_cache=probe_cache,
                        exclude_dirs={d_key} if stop_reason in {"dig_failed", "undiggable_block"} else None,
                        inv_counts=inv_now,
                    )
                    up_dirs = self._probe_viable_up_stair_dirs(
                        ebx,
                        eby,
                        ebz,
                        probe_cache=probe_cache,
                        inv_counts=inv_now,
                    )
                    out_stair["viable_tunnel_directions"] = tunnel_dirs
                    out_stair["viable_up_directions"] = up_dirs
                    if tunnel_dirs:
                        up_hint = (
                            f"mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6)"
                            if up_dirs
                            else "mc_skill_staircase(vertical='up', steps=4)"
                        )
                        out_stair["hint"] = (
                            f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面均无法继续阶梯下探(viable_directions=[])，"
                            f"严禁在当前位置换方向继续调 mc_skill_staircase！请改调 mc_skill_tunnel(direction='{tunnel_dirs[0]}', steps=6) "
                            f"水平开矿道转移后再下探，或调 {up_hint} 沿原矿道退回上层再分支下探！"
                        )
                    elif up_dirs:
                        out_stair["hint"] = (
                            f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面下探与水平通道均已堵死"
                            "(viable_directions=[], viable_tunnel_directions=[])，严禁在原地重试下探或调用 mc_skill_tunnel！"
                            f"请改调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6) 沿安全方向向上退回上层(可选上行方向: {','.join(up_dirs)})！"
                        )
                    else:
                        vpillar = self._probe_viable_pillar_up(
                            ebx,
                            eby,
                            ebz,
                            probe_cache=probe_cache,
                            inv_counts=inv_now,
                        )
                        out_stair["viable_pillar_up"] = vpillar
                        if vpillar:
                            out_stair["hint"] = (
                                f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面下探、水平通道与斜向阶梯上行均受阻"
                                "(viable_directions=[], viable_tunnel_directions=[], viable_up_directions=[])，"
                                "但头顶正上方安全且有垫脚方块(viable_pillar_up=true)，请改调 mc_skill_staircase(vertical='pillar', steps=4) 原地垂直挖顶垫脚直上脱困！"
                            )
                        else:
                            out_stair["hint"] = (
                                f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面下探、水平通道与常规向上阶梯均受阻"
                                "(viable_directions=[], viable_tunnel_directions=[], viable_up_directions=[], viable_pillar_up=false)，"
                                "严禁继续原地循环调用 mc_skill_staircase 或 mc_skill_tunnel！请先调 mc_move_to 沿已挖矿道走出死胡同，或用 mc_get_block 确认安全后以 mc_dig / mc_place 垫脚向上开路脱困！"
                            )
                else:
                    out_stair["hint"] = (
                        f"前方({d_key})遇到{detail_desc}；当前坐标[{ebx},{eby},{ebz}]四面下方均为空洞/悬崖/树冠(viable_directions=[])，原地4个方向全部无法阶梯下探！"
                        "严禁在当前位置换方向继续调 mc_skill_staircase！请先调 mc_skill_unstick 下树/脱困，或调 mc_move_to(只传x,z省略y) 水平走开 12-20 格到平坦实心陆地后再下探！"
                    )
        elif v_key == "up" and stop_reason in {"fluid_or_bedrock", "dig_failed", "undiggable_block", "movement_not_confirmed", "missing_step_support"}:
            probe_cache_up: dict[tuple[int, int, int], str] = {}
            up_inv = _inventory_counts(self.bot)
            up_dirs = self._probe_viable_up_stair_dirs(
                ebx,
                eby,
                ebz,
                probe_cache=probe_cache_up,
                exclude_dirs={d_key},
                inv_counts=up_inv,
            )
            tunnel_dirs = self._probe_viable_tunnel_dirs(
                ebx,
                eby,
                ebz,
                probe_cache=probe_cache_up,
                inv_counts=up_inv,
            )
            out_stair["viable_up_directions"] = up_dirs
            out_stair["viable_tunnel_directions"] = tunnel_dirs
            detail_up = (
                f"{stop_reason}({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
                if blocker_info
                else (
                    f"{stop_reason}({last_error['code']}@{last_error.get('pos')})"
                    if last_error and last_error.get("code")
                    else stop_reason
                )
            )
            if up_dirs:
                out_stair["hint"] = (
                    f"向上({d_key})遇到{detail_up}，禁止继续朝{d_key}上挖；实测可行向上阶梯方向: {','.join(up_dirs)}"
                    f"（请调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6)）"
                    + (f"，或平推方向: {','.join(tunnel_dirs)}。" if tunnel_dirs else "。")
                )
            elif tunnel_dirs:
                out_stair["hint"] = (
                    f"向上({d_key})遇到{detail_up}，当前位置无安全上挖方向(viable_up_directions=[])；"
                    f"实测可平推绕避方向: {','.join(tunnel_dirs)}（请改调 mc_skill_tunnel(direction='{tunnel_dirs[0]}', steps=6) 后再上探）。"
                )
            else:
                vpillar = self._probe_viable_pillar_up(
                    ebx,
                    eby,
                    ebz,
                    probe_cache=probe_cache_up,
                    inv_counts=up_inv,
                )
                out_stair["viable_pillar_up"] = vpillar
                if vpillar:
                    out_stair["hint"] = (
                        f"向上({d_key})遇到{detail_up}，当前坐标[{ebx},{eby},{ebz}]斜向上挖与水平通道均受阻"
                        "(viable_up_directions=[], viable_tunnel_directions=[])，但头顶正上方安全且有垫脚方块(viable_pillar_up=true)，"
                        "请改调 mc_skill_staircase(vertical='pillar', steps=4) 原地垂直挖顶垫脚直上脱困！"
                    )
                else:
                    out_stair["hint"] = (
                        f"向上({d_key})遇到{detail_up}，当前坐标[{ebx},{eby},{ebz}]上挖与水平通道均受阻"
                        "(viable_up_directions=[], viable_tunnel_directions=[], viable_pillar_up=false)，禁止继续重复调用 mc_skill_staircase/mc_skill_tunnel；"
                        "请先调 mc_move_to 沿既有矿道走出死胡同，或用 mc_get_block 确认安全后以 mc_dig / mc_place 脱困。"
                    )
        elif stop_reason in {"cave_or_drop_ahead", "fluid_or_bedrock", "on_tree_canopy", "undiggable_block"}:
            out_stair["hint"] = f"前方({d_key})遇到{stop_reason}，禁止原地继续朝{d_key}重试，请换其他方向！"
        if surface_evidence is not None:
            out_stair["surface_evidence"] = surface_evidence
        out_stair["ores_nearby"] = ores_nearby
        return out_stair

    def _try_seal_shaft_threat(
        self,
        pos0: list[float] | tuple[float, ...],
        enclosure: dict[str, Any],
        threat_pos: list[float] | tuple[float, ...] | None,
        counts: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Inside a carved shaft, seal a 1-wide open corridor facing an active threat instead of fleeing outside."""
        if enclosure.get("structure") != "carved_shaft":
            return None
        if not isinstance(threat_pos, (list, tuple)) or len(threat_pos) < 3:
            return None
        inv_counts = counts if counts is not None else _inventory_counts(self.bot)
        filler = next((b for b in SHAFT_SEAL_BLOCKS if inv_counts.get(b, 0) >= 2), None)
        if filler is None:
            return None

        t_dir = cardinal_bearing(float(threat_pos[0]) - float(pos0[0]), float(threat_pos[2]) - float(pos0[2]))
        open_dirs = enclosure.get("open_dirs") or []
        if t_dir not in open_dirs:
            return None

        dir_vecs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        sdx, sdz = dir_vecs[t_dir]
        px, pz = float(pos0[0]), float(pos0[2])
        h_dist = math.hypot(float(threat_pos[0]) - px, float(threat_pos[2]) - pz)
        pbx, pbz = math.floor(px), math.floor(pz)
        target_col = (pbx + sdx, pbz + sdz)
        overlaps_target = abs(px - (target_col[0] + 0.5)) < 0.75 and abs(pz - (target_col[1] + 0.5)) < 0.75

        if h_dist < 2.1 or overlaps_target:
            opp_vec = (-sdx, -sdz)
            retreat_dir = next((d for d in open_dirs if d != t_dir), None)
            if retreat_dir is not None:
                rdx, rdz = dir_vecs[retreat_dir]
            else:
                rdx, rdz = opp_vec
            try:
                self.bot.call(
                    "control",
                    {
                        "forward": True,
                        "sprint": True,
                        "jump": retreat_dir in (enclosure.get("up_open_dirs") or []),
                        "yaw": _yaw_toward(px, pz, px + rdx * 5.0, pz + rdz * 5.0),
                        "ms": 550,
                        "stop_on": ["death"],
                    },
                )
            except ApiError:
                pass

        st_cur = self.bot.status()
        if st_cur.get("alive") is False:
            return None
        cpos = st_cur.get("pos") or pos0
        cx, cy, cz = float(cpos[0]), float(cpos[1]), float(cpos[2])
        bx, by, bz = math.floor(cx), math.floor(cy), math.floor(cz)
        tx, tz = bx + sdx, bz + sdz
        if abs(cx - (tx + 0.5)) < 0.75 and abs(cz - (tz + 0.5)) < 0.75:
            return None

        b0 = _cached_block_name(self.bot, tx, by, tz, None)
        b1 = _cached_block_name(self.bot, tx, by + 1, tz, None)
        if b0 in PASSABLE_WALK_BLOCKS:
            targets = [[tx, by, tz], [tx, by + 1, tz]]
        elif b1 in PASSABLE_WALK_BLOCKS:
            targets = [[tx, by + 1, tz], [tx, by + 2, tz]]
        else:
            return None

        for cell in targets:
            cur_blk = _cached_block_name(self.bot, cell[0], cell[1], cell[2], None)
            if cur_blk != "unknown" and cur_blk not in PASSABLE_WALK_BLOCKS:
                continue
            try:
                self.bot.call("place", {"item": filler, "pos": cell})
            except ApiError:
                pass

        for cell in targets:
            fresh_blk = _cached_block_name(self.bot, cell[0], cell[1], cell[2], None)
            if fresh_blk == "unknown" or fresh_blk in PASSABLE_WALK_BLOCKS:
                return None

        return {
            "ok": True,
            "mode": "seal_shaft",
            "structure": "carved_shaft",
            "sealed_direction": t_dir,
            "filler": filler,
            "placed_positions": targets,
            "moved_dist": round(math.hypot(cx - float(pos0[0]), cz - float(pos0[2])), 1),
            "pos_before": [round(float(pos0[0]), 1), round(float(pos0[1]), 1), round(float(pos0[2]), 1)],
            "pos_after": cpos,
            "requires_replan": True,
            "note": f"已用 {filler} 封堵探矿穴 {t_dir} 侧来敌通道，保留当前探矿穴无需返回地表。",
        }

    def flee_hostiles(
        self,
        *,
        distance: float = 14.0,
        urgent: bool = False,
        geofence: Any | None = None,
        threat_pos: list[float] | None = None,
    ) -> dict[str, Any]:
        """Hold/seal inside a carved shaft when safe or sealable, else sprint-jump flee with death-only stop."""
        target_d = max(8.0, min(22.0, float(distance)))
        st0 = self.bot.status()
        if st0.get("alive") is False:
            return {"ok": False, "mode": "flee", "error": "dead", "requires_replan": True}

        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        px, py, pz = float(pos0[0]), float(pos0[1]), float(pos0[2])

        block_cache: dict[tuple[int, int, int], str] = {}
        enclosure = inspect_local_enclosure(self.bot, pos0, block_cache=block_cache)
        threat = assess_hostile_occlusion(
            self.bot,
            pos0,
            radius=18,
            fallback_nearest=st0.get("nearest_hostile"),
            block_cache=block_cache,
        )

        if (
            not urgent
            and enclosure.get("structure") in {"sealed_bunker", "carved_shaft"}
            and threat["complete"]
            and len(threat["unoccluded"]) == 0
            and threat_pos is None
        ):
            return {
                "ok": True,
                "mode": "hold_in_shaft",
                "structure": enclosure["structure"],
                "moved_dist": 0.0,
                "occluded_hostiles": [
                    {
                        "name": o["name"],
                        "dist": o["dist"],
                        "bearing": o["bearing"],
                        "blocker": (o.get("blocker") or {}).get("name"),
                    }
                    for o in threat["occluded"][:3]
                ],
                "pos_before": [round(px, 1), round(py, 1), round(pz, 1)],
                "pos_after": [round(px, 1), round(py, 1), round(pz, 1)],
                "requires_replan": False,
                "note": "当前位于封闭掩体/探矿穴内且周边怪物均被岩层阻隔（无视线接触），原地保留探矿穴，切勿弃坑逃跑。",
            }

        resolved_threat_pos: list[float] | None = None
        if isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3:
            resolved_threat_pos = [float(threat_pos[0]), float(threat_pos[1]), float(threat_pos[2])]
        elif threat["unoccluded"]:
            resolved_threat_pos = list(threat["unoccluded"][0]["pos"])

        # Skeleton/creeper at depth: never leave our own tunnel. Wall off the threat side, then
        # retreat along the recorded waypoints instead of free-pathfinding into open cave air.
        eng = self._mining_dfs
        if eng is not None and getattr(eng, "nodes", None):
            retreat = eng.retreat_into_shaft(
                min_hp_ref=[float(st0.get("health") or 20.0)],
                threat_pos=tuple(resolved_threat_pos) if resolved_threat_pos else None,
            )
            if retreat.get("ok"):
                return retreat
        else:
            nh = st0.get("nearest_hostile")
            if isinstance(nh, dict) and isinstance(nh.get("pos"), (list, tuple)) and len(nh["pos"]) >= 3:
                resolved_threat_pos = [float(nh["pos"][0]), float(nh["pos"][1]), float(nh["pos"][2])]
            elif threat["occluded"]:
                resolved_threat_pos = list(threat["occluded"][0]["pos"])

        if enclosure.get("structure") == "carved_shaft" and resolved_threat_pos is not None:
            sealed = self._try_seal_shaft_threat(pos0, enclosure, resolved_threat_pos)
            if sealed is not None:
                return sealed
            target_d = min(target_d, 8.0)

        hx: float | None = resolved_threat_pos[0] if resolved_threat_pos is not None else None
        hz: float | None = resolved_threat_pos[2] if resolved_threat_pos is not None else None

        if hx is not None and hz is not None:
            dx, dz = px - hx, pz - hz
            norm = math.hypot(dx, dz)
            ux, uz = (dx / norm, dz / norm) if norm > 1e-4 else (0.0, -1.0)
        else:
            ux, uz = 0.0, -1.0

        def _rotated_vecs() -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                rad = math.radians(deg)
                c, s = math.cos(rad), math.sin(rad)
                out.append((ux * c - uz * s, ux * s + uz * c))
            return out

        rotated = _rotated_vecs()
        gf_enabled = geofence is not None and bool(getattr(geofence, "enabled", False))
        if gf_enabled:
            allowed = [
                (rx, rz)
                for rx, rz in rotated
                if geofence.check_point(px + rx * target_d, pz + rz * target_d, action="mc_skill_flee") is None
            ]
            if not allowed:
                short_d = min(target_d, 6.0)
                allowed = [
                    (rx, rz)
                    for rx, rz in rotated
                    if geofence.check_point(px + rx * short_d, pz + rz * short_d, action="mc_skill_flee") is None
                ]
            candidates = allowed or rotated
        else:
            candidates = rotated

        rx0, rz0 = candidates[0]
        try:
            self.bot.call(
                "control",
                {
                    "forward": True,
                    "sprint": True,
                    "jump": True,
                    "yaw": _yaw_toward(px, pz, px + rx0 * 10.0, pz + rz0 * 10.0),
                    "ms": 1200,
                    "stop_on": ["death"],
                },
            )
        except ApiError:
            pass

        st_mid = self.bot.status()
        if st_mid.get("alive") is False:
            return {"ok": False, "mode": "flee", "error": "dead", "requires_replan": True}

        mid_pos = st_mid.get("pos") or pos0
        moved = math.hypot(float(mid_pos[0]) - px, float(mid_pos[2]) - pz)
        nh_mid = st_mid.get("nearest_hostile")
        nh_mid_dist = float(nh_mid.get("dist", 99.0)) if isinstance(nh_mid, dict) else 99.0

        if nh_mid_dist < 9.0 or moved < 4.5:
            mv_ok = False
            for rx, rz in candidates[:2]:
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": round(px + rx * target_d, 1),
                            "z": round(pz + rz * target_d, 1),
                            "range": 2.0,
                            "stop_on": ["death"],
                            "timeout": 2800 if urgent else 7000,
                        },
                    )
                    mv_ok = True
                    break
                except ApiError:
                    continue
            if not mv_ok:
                rx1, rz1 = candidates[1] if len(candidates) > 1 else candidates[0]
                cur = self.bot.status().get("pos") or mid_pos
                cx, cz = float(cur[0]), float(cur[2])
                try:
                    self.bot.call(
                        "control",
                        {
                            "forward": True,
                            "sprint": True,
                            "jump": True,
                            "yaw": _yaw_toward(cx, cz, cx + rx1 * 10.0, cz + rz1 * 10.0),
                            "ms": 900,
                            "stop_on": ["death"],
                        },
                    )
                except ApiError:
                    pass

        st_end = self.bot.status()
        end_pos = st_end.get("pos") or mid_pos
        moved_dist = round(math.hypot(float(end_pos[0]) - px, float(end_pos[2]) - pz), 1)
        nh_after = st_end.get("nearest_hostile")
        nh_end_dist = float(nh_after.get("dist", 99.0)) if isinstance(nh_after, dict) else 99.0
        # A 7000-block "flee" is a death/respawn teleport, not a successful escape: never report it
        # as ok, or the model believes it got away when in fact it lost its gear.
        respawned = moved_dist > 64.0 or st_end.get("alive") is False
        return {
            "ok": False if respawned else (bool(st_end.get("alive", True)) and (nh_end_dist >= 7.0 or moved_dist >= 4.5)),
            "mode": "respawned" if respawned else "flee",
            "moved_dist": moved_dist,
            "respawned": respawned,
            "nearest_hostile_after": nh_after,
            "pos_before": [round(px, 1), round(py, 1), round(pz, 1)],
            "pos_after": end_pos,
            "requires_replan": True,
            **(
                {"note": "检测到位置瞬移超过 64 格：这是死亡重生，不是逃跑成功。装备已掉落，先确认背包再决定重挖还是重做工具。"}
                if respawned
                else {}
            ),
        }

    def defend_or_flee(
        self,
        *,
        attacker: dict[str, Any] | None = None,
        counts: dict[str, int] | None = None,
        health: float | None = None,
        urgent: bool = False,
        geofence: Any | None = None,
    ) -> dict[str, Any]:
        """Counter-attack a lone unoccluded melee zombie/spider when well-armed, else seal shaft or sprint-flee."""
        if counts is None:
            counts = _inventory_counts(self.bot)
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        if health is None:
            hp = float(st0.get("health") or 0.0)
        else:
            hp = float(health)

        threat = assess_hostile_occlusion(self.bot, pos0, radius=12, fallback_nearest=attacker)
        unoccluded = threat["unoccluded"]
        occluded = threat["occluded"]

        best_weapon = best_melee_weapon_in_inv(counts)
        danger_mobs = {"creeper", "enderman", "warden", "witch"}
        danger_mob_near = any(
            str(e.get("name") or "").removeprefix("minecraft:") in danger_mobs and float(e.get("dist", 99.0)) <= 8.0
            for e in unoccluded
        )
        close_unblocked = [e for e in unoccluded if float(e.get("dist", 99.0)) <= 6.5]

        target: dict[str, Any] | None = None
        target_occluded = False
        att_id = attacker.get("id") if isinstance(attacker, dict) else None
        if att_id is not None:
            match_u = next((e for e in unoccluded if e.get("id") == att_id), None)
            if match_u is not None:
                target = match_u
            else:
                match_o = next((e for e in occluded if e.get("id") == att_id), None)
                if match_o is not None:
                    target = match_o
                    target_occluded = True
                elif not threat["complete"]:
                    target = attacker
        elif len(close_unblocked) == 1:
            target = close_unblocked[0]

        t_id = target.get("id") if isinstance(target, dict) else None
        t_name = str(target.get("name") or "").removeprefix("minecraft:") if isinstance(target, dict) else ""
        t_dist = float(target.get("dist", 3.0)) if isinstance(target, dict) else 99.0
        t_pos = target.get("pos") if isinstance(target, dict) else None

        can_counter = (
            best_weapon is not None
            and hp >= 11.0
            and not danger_mob_near
            and len(close_unblocked) <= 1
            and t_id is not None
            and not target_occluded
            and t_name in SAFE_MELEE_MOBS
            and t_dist <= 5.5
        )
        if can_counter:
            try:
                self.bot.call("equip", {"item": best_weapon, "slot": "hand"})
            except ApiError:
                pass
            try:
                atk_res = self.bot.call(
                    "attack",
                    {"entity_id": int(t_id), "timeout": 4200 if urgent else 12000},
                )
                st_after = self.bot.status()
                nh_after = st_after.get("nearest_hostile")
                nh_after_dist = float(nh_after.get("dist", 99.0)) if isinstance(nh_after, dict) else 99.0
                killed = bool(atk_res.get("killed")) or not (
                    isinstance(nh_after, dict) and nh_after.get("id") == t_id
                )
                if killed or nh_after_dist >= 6.0:
                    return {
                        "ok": True,
                        "mode": "counter_attack",
                        "weapon": best_weapon,
                        "target": t_name,
                        "entity_id": int(t_id),
                        "killed": killed,
                        "attack": atk_res,
                        "pos_after": st_after.get("pos"),
                        "requires_replan": True,
                    }
            except ApiError:
                pass

        return self.flee_hostiles(
            distance=14.0 if not can_counter else 12.0,
            urgent=urgent,
            geofence=geofence,
            threat_pos=t_pos if isinstance(t_pos, list) else None,
        )

    def emergency_death_loop_shelter(
        self,
        *,
        hold_until_mono: float,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Shelter underground/bunker until the rolling 60s 3-death kick window clears."""
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        enclosure = inspect_local_enclosure(self.bot, pos0)
        sheltered = enclosure.get("structure") in {"sealed_bunker", "carved_shaft"}
        bunker_res: dict[str, Any] = {"ok": True, "already_sheltered": sheltered, "structure": enclosure.get("structure")}
        flee_res: dict[str, Any] | None = None

        if not sheltered:
            try:
                self.descend_canopy()
            except Exception:
                pass
            bunker_res = self.dig_safety_bunker(urgent=True)
            sheltered = bool(
                bunker_res.get("ok")
                or (bunker_res.get("inside_shaft") and bunker_res.get("roof_placed"))
            )
            if not sheltered:
                flee_res = self.flee_hostiles(distance=16.0, urgent=True)
                bunker_res = self.dig_safety_bunker(urgent=True)
                sheltered = bool(
                    bunker_res.get("ok")
                    or (bunker_res.get("inside_shaft") and bunker_res.get("roof_placed"))
                )

        held_s = 0.0
        if sheltered:
            wait_deadline = min(float(hold_until_mono), time.monotonic() + 55.0)
            started = time.monotonic()
            while True:
                if stop_check is not None and stop_check():
                    raise KeyboardInterrupt("防死亡循环掩体驻留期间收到停止信号")
                rem = wait_deadline - time.monotonic()
                if rem <= 0:
                    break
                time.sleep(min(0.25, rem))
            held_s = round(max(0.0, time.monotonic() - started), 2)

        return {
            "ok": sheltered,
            "sheltered": sheltered,
            "held_s": held_s,
            "bunker": bunker_res,
            "flee": flee_res,
        }

    @property
    def mining_dfs(self) -> Any:
        if self._mining_dfs is None:
            from .mining_dfs import MiningDFSEngine

            self._mining_dfs = MiningDFSEngine(self)
        return self._mining_dfs

    def mine_dfs(
        self,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        max_duration_s: float = 32.0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """Run path-compressed MiningDFS underground exploration, vein harvesting, or collar backtrack."""
        return self.mining_dfs.run(
            target=target,
            goal_count=goal_count,
            target_y=target_y,
            action=action,
            geofence=geofence,
            max_duration_s=max_duration_s,
            strategy=strategy,
        )

    def ensure_items(
        self,
        targets: dict[str, int] | list[dict[str, Any]],
        *,
        max_duration_s: float = 180.0,
        equip_slots: dict[str, str] | None = None,
        geofence: Any = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Deterministically resolve objective vanilla recipes, smelting, and harvest tiers with 0 LLM calls."""
        from .ensure import PhysicalPrerequisiteResolver

        resolver = PhysicalPrerequisiteResolver(self, geofence=geofence, stop_check=stop_check)
        return resolver.ensure_items(targets, max_duration_s=max_duration_s, equip_slots=equip_slots)

    def build_nether_portal(self, *, enter: bool = True, wait_s: float = 10.0) -> dict[str, Any]:
        """Deterministically clear, build a 4x5 obsidian Nether portal, ignite it, and step inside."""
        st = self.bot.status()
        dim = str(st.get("dimension") or "").lower()
        if "nether" in dim:
            return {
                "ok": True,
                "already_in_nether": True,
                "entered_nether": True,
                "dimension": dim,
                "message": "当前已身处下界(the_nether)，无需再搭传送门。",
            }

        entry_detail: dict[str, Any] = {}

        def _walk_into_portal(portal_blocks: list[dict[str, Any]]) -> tuple[bool, str]:
            # A pathfinder success only confirms its goal radius. Check actual
            # portal contact and use short controls when the portal plane is avoided.
            blocks = sorted(
                (b for b in portal_blocks if isinstance(b, dict)
                 and isinstance(b.get("pos"), (list, tuple)) and len(b["pos"]) >= 3),
                key=lambda b: (float(b["pos"][1]), float(b.get("dist", 99.0))),
            )
            if not blocks:
                entry_detail["error"] = {"code": "portal_not_found", "phase": "entry"}
                return False, dim
            target = [math.floor(float(v)) for v in blocks[0]["pos"][:3]]
            tx, ty, tz = target
            entry_detail["entry_target"] = target
            deadline = time.monotonic() + max(4.0, min(30.0, float(wait_s)))
            dim_now = dim

            def _entry_call(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ApiError("portal_entry_timeout", "入门预算耗尽")
                cap = min(float(kwargs.pop("timeout_s", 3.0)), remaining)
                if path == "move_to":
                    body = {**(body or {}), "timeout": max(1, int(max(0.001, cap - 0.2) * 1000))}
                return self.bot.call(path, body, timeout_s=cap, **kwargs)

            def _status() -> dict[str, Any]:
                nonlocal dim_now
                st_now = (_entry_call("status", method="GET", timeout_s=2.0)
                          if isinstance(self.bot, BotSession) else self.bot.status())
                dim_now = str(st_now.get("dimension") or "").lower()
                entry_detail["entry_pos"] = st_now.get("pos")
                return st_now

            try:
                # Break the vegetation/terrain on the nearest face, without
                # touching the frame, portal, or its supporting floor.
                block = _entry_call("get_block", {"pos": target})
                axis = (block.get("properties") or {}).get("axis", "x")
                normal = (0, 1) if axis == "x" else (1, 0)
                cur = _status().get("pos") or st.get("pos") or target
                sign = -1 if (float(cur[0]) - tx - 0.5) * normal[0] + (float(cur[2]) - tz - 0.5) * normal[1] <= 0 else 1
                ax, az = tx + sign * normal[0], tz + sign * normal[1]
                for y_cell in (ty, ty + 1, ty + 2):
                    cell = [ax, y_cell, az]
                    name = str(_entry_call("get_block", {"pos": cell}).get("name") or "").removeprefix("minecraft:")
                    if name in {"water", "lava", "flowing_water", "flowing_lava", "bedrock", "obsidian"}:
                        entry_detail["error"] = {"code": "portal_approach_blocked", "phase": "entry", "pos": cell, "block": name}
                        return False, dim_now
                    if name and name not in {"air", "cave_air", "void_air", "nether_portal", "fire"}:
                        _entry_call("dig", {"pos": cell, "collect": False}, timeout_s=min(3.0, max(0.05, deadline - time.monotonic())))
                try:
                    _entry_call("move_to", {"x": tx + 0.5, "y": ty, "z": tz + 0.5,
                                             "range": 0.2, "timeout": 2500}, timeout_s=3.0)
                except ApiError as err:
                    entry_detail["move_error"] = {"code": err.code, "message": err.message}
                contact_seen = False
                pulses = 0
                approach_recovered = False
                while time.monotonic() < deadline:
                    now = _status()
                    if "nether" in dim_now:
                        return True, dim_now
                    cur = now.get("pos") or []
                    if len(cur) < 3:
                        break
                    cx, cy, cz = map(float, cur[:3])
                    in_portal = abs(cx - tx - 0.5) <= 0.45 and abs(cz - tz - 0.5) <= 0.45 and ty <= cy < ty + 1
                    contact_seen = contact_seen or in_portal
                    normal_distance = abs((cx - tx - 0.5) * normal[0] + (cz - tz - 0.5) * normal[1])
                    lateral_distance = abs((cx - tx - 0.5) * normal[1] + (cz - tz - 0.5) * normal[0])
                    if (not approach_recovered and 1.1 < ty - cy <= 2.1
                            and 0.7 <= normal_distance <= 1.3 and lateral_distance <= 0.6):
                        # r12: pathfinding can fall to a low approach floor two
                        # blocks below the portal. One verified jump-place makes
                        # a one-block stair, without touching its obsidian bar.
                        floor_pos = [math.floor(cx), math.floor(cy) - 1, math.floor(cz)]
                        floor = str(_entry_call("get_block", {"pos": floor_pos}).get("name") or "").removeprefix("minecraft:")
                        if not floor or is_passable_sight_block(floor) or floor in {"lava", "flowing_lava", "magma_block", "cactus"}:
                            entry_detail["error"] = {"code": "portal_approach_unsupported", "phase": "entry", "pos": floor_pos, "block": floor}
                            return False, dim_now
                        approach_recovered = True
                        recovery = self.dig_staircase(vertical="pillar", steps=1, stop_on_ore=False,
                                                      deadline=deadline, per_step_timeout=3.0)
                        entry_detail["entry_recovery"] = {key: recovery.get(key) for key in ("ok", "steps_done", "pos_after", "stop_reason")}
                        if not recovery.get("ok"):
                            entry_detail["error"] = {"code": "portal_approach_step_failed", "phase": "entry", "cause": recovery.get("stop_reason")}
                            return False, dim_now
                        continue
                    if not in_portal and pulses < 8:
                        # Steer only within a single step of the portal floor.
                        if (cy < ty - 1.1 or cy > ty + 0.6) or math.hypot(cx - tx - 0.5, cz - tz - 0.5) > 2.2:
                            break
                        _entry_call("look", {"x": tx + 0.5, "y": cy + 1.62, "z": tz + 0.5}, timeout_s=1.0)
                        _entry_call("control", {"forward": True, "jump": cy < ty - 0.4, "ms": 150, "stop_on": ["hurt", "death"]}, timeout_s=1.0)
                        pulses += 1
                    else:
                        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                entry_detail["error"] = {
                    "code": "portal_transfer_timeout" if contact_seen else "portal_entry_not_reached",
                    "phase": "entry", "target": target, "contact_seen": contact_seen,
                    "message": (
                        "已检测到踏入传送门，但维度切换在预算内未完成；服务器可能延迟。"
                        "请先查询 status.dimension 等待传送，不要重复建门或再次入门。"
                        if contact_seen
                        else "未检测到踏入传送门；请检查门前两格净空、脚下支撑及 entry_pos，不要重复建门。"
                    ),
                }
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.TimeoutException):
                    try:
                        self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
                    except (ApiError, httpx.HTTPError):
                        pass
                entry_detail["error"] = {"code": "portal_entry_failed", "phase": "entry", "cause": getattr(err, "code", type(err).__name__)}
            return False, dim_now

        # 0) If a lit nether_portal already exists within 12 blocks, walk straight into it without needing 10 obsidian!
        try:
            existing = self.bot.call("find_blocks", {"types": ["nether_portal"], "radius": 12, "limit": 8})
            existing_blocks = existing.get("blocks") or []
        except Exception:
            existing_blocks = []
        if existing_blocks:
            if not enter:
                return {
                    "ok": True,
                    "reused_existing_portal": True,
                    "portal_ignited": True,
                    "entered_nether": False,
                    "dimension": dim,
                    "requires_replan": False,
                }
            entered_now, dim_now = _walk_into_portal(existing_blocks)
            return {
                "ok": bool(entered_now),
                "reused_existing_portal": True,
                "portal_ignited": True,
                "entered_nether": entered_now,
                "dimension": dim_now,
                "requires_replan": not entered_now,
                **entry_detail,
            }

        inv = self.bot.inventory()
        counts: dict[str, int] = {}
        for it in (inv.get("items") or []):
            if isinstance(it, dict) and it.get("name"):
                nm = str(it["name"]).removeprefix("minecraft:")
                counts[nm] = counts.get(nm, 0) + int(it.get("count", 0))
        obs_cnt = counts.get("obsidian", 0)
        fns_cnt = counts.get("flint_and_steel", 0)
        if obs_cnt < 10 or fns_cnt < 1:
            return {
                "ok": False,
                "requires_replan": True,
                "error": "missing_materials",
                "obsidian": obs_cnt,
                "flint_and_steel": fns_cnt,
                "message": f"材料不足：需要 obsidian>=10 且 flint_and_steel>=1（当前 obsidian={obs_cnt}, flint_and_steel={fns_cnt}）。",
            }

        if bool(st.get("in_water")):
            try:
                self.escape_water()
                st = self.bot.status()
            except Exception:
                pass

        pos = st.get("pos") or [0.0, 64.0, 0.0]
        px, py, pz = int(math.floor(float(pos[0]))), int(math.floor(float(pos[1]))), int(math.floor(float(pos[2])))
        # Place portal 2 blocks south of bot (along X axis: x = bx..bx+3, y = by..by+4, z = bz)
        bx, by, bz = px - 1, py, pz + 2

        scaffold_candidates = (
            "cobblestone",
            "cobbled_deepslate",
            "dirt",
            "netherrack",
            "stone",
            "oak_planks",
            "spruce_planks",
            "birch_planks",
            "dark_oak_planks",
            "andesite",
            "diorite",
            "granite",
        )
        scaffold_item: str | None = None
        for cand in scaffold_candidates:
            if counts.get(cand, 0) >= 3:
                scaffold_item = cand
                break
        if scaffold_item is None and obs_cnt >= 13:
            scaffold_item = "obsidian"
        if scaffold_item is None:
            for cand in scaffold_candidates:
                if counts.get(cand, 0) >= 1:
                    scaffold_item = cand
                    break

        non_diggable = {
            "air", "cave_air", "void_air", "water", "flowing_water", "lava", "flowing_lava", "fire",
        }
        solid_scaffold_ok = {
            "stone", "cobblestone", "dirt", "grass_block", "deepslate", "cobbled_deepslate",
            "netherrack", "obsidian", "sandstone", "andesite", "diorite", "granite",
            "oak_planks", "spruce_planks", "birch_planks", "dark_oak_planks",
        }

        def _blk_name(x: int, y: int, z: int) -> str:
            try:
                b = self.bot.call("get_block", {"pos": [x, y, z]})
                return str(b.get("name") or "").removeprefix("minecraft:").lower()
            except Exception:
                return ""

        def _clear_cell(x: int, y: int, z: int) -> None:
            nm = _blk_name(x, y, z)
            if nm and nm not in non_diggable and nm != "obsidian":
                try:
                    self.bot.call("dig", {"pos": [x, y, z]})
                except Exception:
                    pass

        def _ensure_block(x: int, y: int, z: int, item_name: str) -> bool:
            nm = _blk_name(x, y, z)
            if nm == item_name:
                return True
            if item_name != "obsidian" and nm in solid_scaffold_ok:
                return True  # Existing truly solid block already serves as scaffold!
            if nm and nm not in non_diggable:
                try:
                    self.bot.call("dig", {"pos": [x, y, z]})
                except Exception:
                    pass
            # Stand near the frame front without colliding with the target block (closer for top row y>=by+3)
            stand_z = float(bz - 1.2) if y >= by + 3 else float(bz - 2.0)
            try:
                self.bot.call("move_to", {"x": bx + 1.5, "y": by, "z": stand_z, "range": 1.5})
            except Exception:
                pass
            try:
                self.bot.call("place", {"pos": [x, y, z], "item": item_name})
                return True
            except Exception:
                # Retry once after stepping closer to the specific column
                try:
                    self.bot.call("move_to", {"x": float(x), "y": float(by), "z": float(bz - 1.2), "range": 1.2})
                    self.bot.call("place", {"pos": [x, y, z], "item": item_name})
                    return True
                except Exception:
                    return False

        # 1) Clear standing space in front and portal interior
        for cx in (bx + 1, bx + 2):
            for cy in (by + 1, by + 2, by + 3):
                _clear_cell(cx, cy, bz)
            for cy in (by, by + 1):
                _clear_cell(cx, cy, bz - 1)

        # 2) Ensure solid floor beneath bottom bar & corners
        if scaffold_item:
            for fx in range(bx, bx + 4):
                nm = _blk_name(fx, by - 1, bz)
                if nm not in solid_scaffold_ok:
                    _ensure_block(fx, by - 1, bz, scaffold_item)

        # 3) Build 4x5 frame (bottom bar -> bottom corners -> left/right pillars -> top corner -> top bar)
        placed_obs = 0
        failed_cells: list[list[int]] = []
        frame_steps: list[tuple[int, int, int, str, bool]] = [
            (bx + 1, by, bz, "obsidian", True),
            (bx + 2, by, bz, "obsidian", True),
            (bx, by, bz, scaffold_item or "obsidian", False),
            (bx + 3, by, bz, scaffold_item or "obsidian", False),
            (bx, by + 1, bz, "obsidian", True),
            (bx, by + 2, bz, "obsidian", True),
            (bx, by + 3, bz, "obsidian", True),
            (bx + 3, by + 1, bz, "obsidian", True),
            (bx + 3, by + 2, bz, "obsidian", True),
            (bx + 3, by + 3, bz, "obsidian", True),
            (bx, by + 4, bz, scaffold_item or "obsidian", False),
            (bx + 1, by + 4, bz, "obsidian", True),
            (bx + 2, by + 4, bz, "obsidian", True),
        ]
        for tx, ty, tz, item_nm, is_obs in frame_steps:
            if _ensure_block(tx, ty, tz, item_nm):
                if is_obs:
                    placed_obs += 1
            elif is_obs:
                failed_cells.append([tx, ty, tz])

        # Re-clear interior in case any stray block remains
        for cx in (bx + 1, bx + 2):
            for cy in (by + 1, by + 2, by + 3):
                _clear_cell(cx, cy, bz)

        if failed_cells:
            return {
                "ok": False, "requires_replan": True, "portal_origin": [bx, by, bz],
                "placed_obsidian": placed_obs, "failed_cells": failed_cells,
                "portal_ignited": False, "entered_nether": False,
                "error": {"code": "portal_frame_incomplete", "phase": "placement", "failed_cells": failed_cells},
            }

        # 4) Ignite the bottom interior of the frame
        try:
            self.bot.call("move_to", {"x": bx + 1.5, "y": by, "z": bz - 2.0, "range": 1.8})
        except Exception:
            pass
        ignited = False
        lit_blocks: list[dict[str, Any]] = []
        for ix in (bx + 1, bx + 2):
            try:
                self.bot.call("interact", {"pos": [ix, by, bz], "item": "flint_and_steel"})
            except Exception:
                pass
            time.sleep(0.5)
            try:
                fb = self.bot.call("find_blocks", {"types": ["nether_portal"], "radius": 8, "limit": 6})
                if fb.get("blocks"):
                    ignited = True
                    lit_blocks = list(fb.get("blocks") or [])
                    break
            except Exception:
                pass

        entered_nether = False
        dim_after = dim
        if ignited and enter:
            entered_nether, dim_after = _walk_into_portal(
                lit_blocks or [{"pos": [bx + 1, by + 1, bz], "dist": 1.0}]
            )

        return {
            "ok": bool(ignited and (entered_nether or not enter)),
            "portal_origin": [bx, by, bz],
            "placed_obsidian": placed_obs,
            "failed_cells": failed_cells,
            "portal_ignited": ignited,
            "entered_nether": entered_nether,
            "dimension": dim_after,
            "requires_replan": not (ignited and (entered_nether or not enter)),
            **({"error": {"code": "portal_ignition_failed", "phase": "ignition"}} if not ignited else entry_detail),
        }
