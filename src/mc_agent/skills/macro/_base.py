"""L1 deterministic macro skills: spawn-protection blacklist, tree chopping, crafting, tiered mining, smelting, bed spawn."""

from __future__ import annotations

from copy import copy
from functools import partial
import inspect
import math
import re
import time
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from ...bot import BotSession
from ...errors import ApiError, AuthError, DeathLoopCooldownError
from ...kb import RECIPES_1_21, assess_world_target, normalize_dimension, resolve_item_alias
from ..safety import check_and_stabilize
from ..crafting import material_shortage, explain_craft_error

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

BED_BLOCK_TYPES: tuple[str, ...] = tuple(
    f"{color}_bed"
    for color in (
        "white", "orange", "magenta", "light_blue", "yellow", "lime",
        "pink", "gray", "light_gray", "cyan", "purple", "blue",
        "brown", "green", "red", "black",
    )
)
VILLAGE_LANDMARK_BLOCKS = ("bell", "hay_block", "composter", "lectern", "fletching_table", "chest", *BED_BLOCK_TYPES)
VILLAGE_SURVEY_CLUTTER = frozenset({"dirt_path", "farmland"})
SURFACE_SCOUT_DIRECTIONS = ("east", "southeast", "south", "southwest", "west", "northwest", "north", "northeast")
SURFACE_SCOUT_ENTITIES = ("villager", "iron_golem")
SURFACE_SCOUT_LANDMARKS = ("lava", "obsidian")
NETHER_SCOUT_TARGET_BLOCKS = ("nether_bricks", "spawner", "nether_wart")
NETHER_SCOUT_TARGET_ENTITIES = ("blaze", "wither_skeleton")
NETHER_TUNNEL_UNSUPPORTED_FLOORS = frozenset({"netherrack", "nether_bricks", "basalt", "blackstone", "obsidian"})
SURFACE_SCOUT_PATH_ERRORS = frozenset({"path_not_found", "no_path", "timeout", "move_timeout", "stalled"})


def rejected_find_block(
    code: str | None, message: str, names: list[str] | None
) -> str | None:
    if code != "bad_args" or not names:
        return None
    match = re.search(r"\bunknown block\s+(\S+)", message, re.IGNORECASE)
    name = match.group(1).lower().removeprefix("minecraft:") if match else None
    return name if name in names else None

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
    "golden_boots",
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
_WORKBENCH_OPEN_ERRORS = frozenset({"path_not_found", "need_crafting_table", "out_of_reach", "no_window"})

DEFAULT_EQUIP_SLOTS: dict[str, str] = {
    "shield": "off-hand",
    "iron_helmet": "head",
    "iron_chestplate": "torso",
    "iron_leggings": "legs",
    "iron_boots": "feet",
    "golden_boots": "feet",
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

_REUSABLE_CRAFT_ITEMS = frozenset({
    "flint_and_steel", "diamond_pickaxe", "iron_pickaxe", "stone_pickaxe", "diamond_sword", "iron_sword",
    "shield", "bucket", "shears", "compass", "lodestone", "enchanting_table", "anvil", "brewing_stand", "crafting_table",
})

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
    log_name = log_name.removeprefix("stripped_")
    if log_name.endswith("_stem"):
        return log_name.removesuffix("_stem") + "_planks"
    return log_name.removesuffix("_log") + "_planks"


_PLANK_LOG_SOURCES = {
    _log_to_planks_name(log): (log, f"stripped_{log}") for log in (*LOG_TYPES, *NETHER_STEM_TYPES)
}
_PLANK_SOURCE_LOGS = frozenset(log for logs in _PLANK_LOG_SOURCES.values() for log in logs)


def _canonical_plank_recipe(item: str) -> str:
    # Existing preparation callers can derive a stripped plank ID from an inventory log.
    candidate = item.removeprefix("stripped_")
    return candidate if candidate in _PLANK_LOG_SOURCES else item


def _total_planks(counts: dict[str, int]) -> int:
    return sum(v for k, v in counts.items() if k.endswith("_planks"))


def _plank_equivalent(counts: dict[str, int]) -> int:
    return _total_planks(counts) + 4 * sum(counts.get(log, 0) for log in _PLANK_SOURCE_LOGS)


def _stick_craft_batches(needed: int, available: int) -> int:
    return math.ceil(max(0, needed - available) / 4)


def _wood_budget_shortage(
    item: str, counts: dict[str, int], required: int, *, count_logs: bool = True,
) -> dict[str, Any] | None:
    available = _plank_equivalent(counts) if count_logs else _total_planks(counts)
    if available >= required:
        return None
    missing = required - available
    logs_needed = math.ceil(missing / 4)
    message = (
        f"合成 {item} 及所需备料共需 {required} 块木板，现有木板和可拆原木合计可用 {available} 块。"
        f"还需补 {missing} 块木板，或补 {logs_needed} 根原木（菌柄也可）。"
        if count_logs else
        f"合成 {item} 及所需备料共需 {required} 块木板，拆木后实际只有 {available} 块。"
        f"请先备齐差额 {missing} 块木板。"
    )
    return {
        "ok": False,
        "error": "missing_ingredients",
        "requires_replan": True,
        "item": item,
        "inventory": counts,
        "required_materials": {"any_planks": required},
        "available_materials": {"any_planks": available},
        "missing_materials": {"any_planks": missing},
        "message": message,
        **({"logs_needed": logs_needed} if count_logs else {}),
    }


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
_COMMON_GATHER_DROPS = frozenset({
    *LOG_TYPES, *NETHER_STEM_TYPES, "stone", "deepslate", "grass_block", "flint",
    *_TRASH_TIER2_DECOR_STONES, *_TRASH_TIER2_LOOSE_BLOCKS, *_TRASH_TIER3_DIRT,
    *_TRASH_TIER4_COBBLE, *_TRASH_TIER_NETHER,
})
_MINING_RETENTION_CAPS = {
    **dict.fromkeys(_TRASH_TIER3_DIRT, 64),
    **dict.fromkeys(_TRASH_TIER_NETHER, 128),
    **dict.fromkeys(_TRASH_TIER4_COBBLE, 192),
}


def _inventory_retention_plan(counts: dict[str, int]) -> list[tuple[str, int]]:
    cobble = sum(counts.get(name, 0) for name in _TRASH_TIER4_COBBLE)
    tier2_keep = 0 if cobble >= 64 else 64
    return [
        *((name, 0) for name in _TRASH_TIER1_ZERO_KEEP),
        *((name, tier2_keep) for name in _TRASH_TIER2_DECOR_STONES),
        *((name, tier2_keep) for name in _TRASH_TIER2_LOOSE_BLOCKS),
        *_MINING_RETENTION_CAPS.items(),
    ]


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
    "golden_boots": {"gold_ingot": 4},
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
    plank_logs = _PLANK_LOG_SOURCES.get(item)
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
        if plank_logs and name == plank_logs[0]:
            return sum(counts.get(log, 0) for log in plank_logs)
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


def _reserved_craft_materials(item: str, count: int) -> dict[str, int]:
    plan = craft_material_shortage(item, count, {}) or {}
    materials = dict(plan.get("required_materials") or {})
    if "cobblestone" in materials and (item.startswith("stone_") or item == "furnace"):
        materials["cobbled_deepslate"] = materials["cobblestone"]
    return materials


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
        sources = _PLANK_LOG_SOURCES.get(norm, (log_name,))
        source_counts = [bag_counts.get(source, 0) for source in sources]
        # The server selects a recipe variant. A singleton in just one variant cannot guarantee room.
        return max(source_counts) == 1 or 0 < sum(source_counts) <= need_logs

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


def _validate_smelt_input(raw: str) -> tuple[str, dict[str, Any] | None]:
    item = str(raw or "").strip().lower().removeprefix("minecraft:")
    if item == "coal":
        return item, {
            "ok": False,
            "error": "not_smeltable",
            "input_item": item,
            "requires_replan": True,
            "message": "coal 已是煤炭，只能用作 fuel_item。input_item 请填写背包中的可烧制原料，例如 raw_iron 或 coal_ore。",
        }
    return item, None


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

_PICKUP_INTERRUPTION_CODES = frozenset({
    "interrupted", "hurt", "death", "dead", "disconnected", "not_connected", "busy", "action_busy",
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
    *,
    gathering: bool = False,
) -> dict[str, Any] | None:
    """Build directional recovery guidance for ground items that were skipped or stopped out of reach."""
    candidates: list[dict[str, Any]] = []
    for row in skipped:
        pos = row.get("item_pos")
        reason = row.get("reason")
        summarize = reason == "height_difference" or (gathering and reason == "unsafe_route")
        if summarize and isinstance(pos, list) and len(pos) >= 3:
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
    if gathering and _normalize_drop_name(best.get("item")) in _COMMON_GATHER_DROPS:
        approach_hint = (
            f"未能沿安全短路径捡到 {best.get('item')} [{tx},{ty},{tz}]（高度差 {dy:+.1f} 格）。"
            "请放弃这堆掉落物，改采同层、近处且有支撑的目标。"
        )
    elif dy <= -2.0:
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
        self.dig_cap: float | None = None
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
            # BUG-037/r24: a first step on a loaded server can stall 20~30s
            # even with a tier>=2 pickaxe, so allow the caller to raise the
            # dig cap for the first step only.
            base_cap = 6.0 if path == "move_to" else 15.0 if path == "dig" else 3.0
            if path == "dig" and self.dig_cap:
                base_cap = max(base_cap, float(self.dig_cap))
            cap = base_cap
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


class _SkillDeadline(Exception):
    """Stop a skill when its shared request budget is spent."""


class _SkillBudgetSession:
    """Bound one skill invocation without rebinding the shared bot."""

    # window/view opens a GUI under the server's action lease.
    _READS = frozenset({"status", "inventory", "get_block", "find_blocks", "get_entities"})

    def __init__(
        self, bot: BotSession, max_duration_s: float, *,
        bound_transport_retries: bool = False, single_attempt: bool = False,
    ) -> None:
        self.bot = bot
        self.bound_transport_retries = bound_transport_retries or single_attempt
        self.single_attempt = single_attempt
        budget = max(0.0, float(max_duration_s))
        self.finish_at = time.monotonic() + budget
        self.deadline = self.finish_at - min(1.0, budget / 4)
        params = inspect.signature(bot.call).parameters
        self.supports_timeout = "timeout_s" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        self.timed_out = False
        self.pending_action = False
        self.progress: dict[str, Any] = {}

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.timed_out or remaining <= 0:
            self.timed_out = True
            raise _SkillDeadline()
        return remaining

    @staticmethod
    def _retry_timeout(remaining: float, read_cap: float, *, single_attempt: bool = False) -> httpx.Timeout:
        # Reserve the request phases and, when enabled, the retry after a 150 ms pause.
        per_attempt = remaining if single_attempt else (remaining - 0.15) / 2
        if per_attempt <= 0:
            raise _SkillDeadline()
        phase = min(0.25, per_attempt / (12 if single_attempt else 4))
        return httpx.Timeout(min(read_cap, per_attempt - 3 * phase), pool=phase, connect=phase, write=phase)

    @staticmethod
    def _single_attempt_call(call: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        if getattr(call, "__func__", None) is not BotSession.call:
            return call
        bot = copy(call.__self__)
        client = bot._client
        bot._recovering_quarantine = True

        def send(request: Callable[..., httpx.Response], *args: Any, **kwargs: Any) -> httpx.Response:
            try:
                return request(*args, **kwargs)
            except httpx.RemoteProtocolError as error:
                # The missing reply can follow an executed write. Use the normal timeout/stop path.
                raise httpx.ReadTimeout(str(error)) from error

        bot._client = SimpleNamespace(get=partial(send, client.get), request=partial(send, client.request))

        def call_once(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            try:
                return call.__func__(bot, path, body, **kwargs)
            except ApiError as error:
                if error.code in {"action_timeout", "action_quarantined"}:
                    raise httpx.ReadTimeout(error.message) from error
                raise

        return call_once

    def _mining_budget_scope(self, deadline: float, *, single_attempt: bool) -> Any | None:
        if getattr(self.bot, "_mining_deadline", None) is None:
            return None
        from ..mining_dfs import _BudgetSession

        owner = getattr(self.bot.call, "__self__", None)
        if not isinstance(owner, _BudgetSession):
            return None
        scoped = copy(owner)
        scoped.deadline = min(deadline, owner.deadline)
        scoped.timeout_path = None
        if single_attempt:
            scoped._call = self._single_attempt_call(scoped._call)
        return scoped

    def _call_in_mining_budget(
        self, scoped: Any, path: str, body: dict[str, Any] | None, *,
        stop_deadline: float, single_attempt: bool, **kwargs: Any,
    ) -> dict[str, Any]:
        from ..mining_dfs import _MiningDeadline

        owner = self.bot.call.__self__
        try:
            result = scoped.call(path, body, **kwargs)
        except (_MiningDeadline, httpx.RemoteProtocolError) as error:
            if path == "stop":
                return {}
            timeout_path = path if isinstance(error, httpx.RemoteProtocolError) else scoped.timeout_path
            self.pending_action = timeout_path is not None and path not in self._READS
            released = not self.pending_action or self.stop_pending(
                deadline=stop_deadline, single_attempt=single_attempt,
            )
            if not released or scoped.deadline >= owner.deadline or owner.deadline - time.monotonic() <= 0.2:
                owner.timeout_path = timeout_path
                raise _MiningDeadline() from error
            raise _SkillDeadline() from None
        owner.first_pos = scoped.first_pos
        owner.last_status = scoped.last_status
        owner.first_inventory = scoped.first_inventory
        owner.last_inventory = scoped.last_inventory
        return result

    def call(
        self, path: str, body: dict[str, Any] | None = None, *, timeout_s: float | None = None,
        _transport_budget: tuple[float, float, bool] | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        deadline, finish_at = self.deadline, self.finish_at
        if _transport_budget is not None:
            finish_at = min(finish_at, _transport_budget[1])
            deadline = finish_at if path == "stop" else min(deadline, _transport_budget[0])
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (self.timed_out and path != "stop"):
            if _transport_budget is None:
                self.timed_out = True
            raise _SkillDeadline()
        timeout = remaining if timeout_s is None else min(remaining, timeout_s)
        request_timeout: float | httpx.Timeout = timeout
        bounded = self.bound_transport_retries or _transport_budget is not None
        single_attempt = self.single_attempt or (_transport_budget is not None and _transport_budget[2])
        call = self.bot.call
        if bounded:
            if isinstance(self.bot, _SkillBudgetSession):
                kwargs["_transport_budget"] = (deadline, finish_at, single_attempt)
            elif (mining_scope := self._mining_budget_scope(deadline, single_attempt=single_attempt)) is not None:
                return self._call_in_mining_budget(
                    mining_scope, path, body, stop_deadline=finish_at, single_attempt=single_attempt,
                    timeout_s=timeout, **kwargs,
                )
            elif getattr(call, "__func__", None) is BotSession.call:
                request_timeout = self._retry_timeout(deadline - time.monotonic(), timeout, single_attempt=single_attempt)
                kwargs["allow_reauth"] = False
                if single_attempt:
                    call = self._single_attempt_call(call)
                if path in {"move_to", "control"} and body is not None:
                    field = "timeout" if path == "move_to" else "ms"
                    milliseconds = int((request_timeout.read - 0.2) * 1000)
                    if milliseconds < 50:
                        raise _SkillDeadline()
                    body = {**body, field: min(milliseconds, int(body.get(field, milliseconds)))}
        try:
            if self.supports_timeout:
                return call(path, body, timeout_s=request_timeout, **kwargs)
            return call(path, body)
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as error:
            # The initiating scope owns timeout state and the stop reserve.
            if _transport_budget is not None or (isinstance(error, httpx.RemoteProtocolError) and not bounded):
                raise
            self.timed_out = True
            self.pending_action = path not in self._READS
            if path == "dig" and isinstance(error, httpx.TimeoutException):
                raise  # The dig loop records the target and preserves partial progress.
            raise _SkillDeadline() from error

    def status(self) -> dict[str, Any]:
        self._remaining()
        if isinstance(self.bot, BotSession):
            return self.call("status", method="GET")
        return self.bot.status()

    def inventory(self) -> dict[str, Any]:
        self._remaining()
        if isinstance(self.bot, BotSession):
            inv = self.call("inventory", method="GET")
            self.bot._last_inventory = inv
            return inv
        return self.bot.inventory()

    def stop_pending(self, *, deadline: float | None = None, single_attempt: bool = False) -> bool:
        finish_at = self.finish_at if deadline is None else min(deadline, self.finish_at)
        remaining = finish_at - time.monotonic()
        single_attempt = self.single_attempt or single_attempt
        bounded = self.bound_transport_retries or single_attempt
        if self.pending_action and remaining > 0 and self.supports_timeout:
            self.pending_action = False
            try:
                timeout = min(1.0, remaining)
                if bounded:
                    stopped = self.call(
                        "stop", {}, timeout_s=timeout, allow_reauth=False,
                        _transport_budget=(finish_at, finish_at, single_attempt),
                    )
                else:
                    stopped = self.bot.call("stop", {}, timeout_s=timeout, allow_reauth=False)
            except (ApiError, httpx.HTTPError, _SkillDeadline):
                return False
            return stopped.get("released") is True or (
                stopped.get("stopped") is True and "action" in stopped and stopped["action"] is None
            )
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bot, name)
