"""AltoClef-style inventory brain: protection stack, 4-tier garbage plan, auto-equip,
dual-view counts and nearby chest stashing.

Pure planning lives in :func:`plan_garbage_drops`; all network-touching helpers take a
live ``BotSession`` so the same module is exercised by offline unit tests and by the
`nju-beast` live acceptance scripts.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from ..errors import ApiError

# ---------------------------------------------------------------------------
# Static data
# ---------------------------------------------------------------------------

TOOL_SUFFIXES: tuple[str, ...] = ("_pickaxe", "_axe", "_shovel", "_hoe", "_sword")

ARMOR_SUFFIXES: tuple[str, ...] = (
    "_helmet",
    "_chestplate",
    "_leggings",
    "_boots",
)

ARMOR_TIERS: dict[str, int] = {
    "leather": 1,
    "golden": 2,
    "chainmail": 2,
    "iron": 3,
    "diamond": 4,
    "netherite": 5,
    "turtle": 3,
    "wolf": 3,
}

TOOL_TIERS: dict[str, int] = {
    "wooden": 1,
    "golden": 1,
    "stone": 2,
    "iron": 3,
    "diamond": 4,
    "netherite": 5,
}

# 1.21.x advancement-critical items.  Matching also uses suffix families below.
IMPORTANT_ITEMS: frozenset[str] = frozenset(
    {
        # ores / ingots / mineral blocks
        "coal", "charcoal", "raw_iron", "iron_ingot", "iron_block", "raw_copper", "copper_ingot",
        "copper_block", "raw_gold", "gold_ingot", "gold_block", "gold_nugget", "iron_nugget",
        "diamond", "diamond_block", "emerald", "emerald_block", "lapis_lazuli", "lapis_block",
        "redstone", "redstone_block", "quartz", "quartz_block", "amethyst_shard", "amethyst_block",
        "netherite_ingot", "netherite_scrap", "netherite_block", "ancient_debris", "echo_shard",
        # cross-dimension / endgame
        "ender_pearl", "ender_eye", "blaze_rod", "blaze_powder", "ghast_tear", "nether_wart",
        "obsidian", "crying_obsidian", "glowstone_dust", "glowstone", "dragon_breath",
        "shulker_shell", "totem_of_undying", "enchanted_golden_apple", "golden_apple", "trident",
        "heavy_core", "breeze_rod", "wind_charge", "mace", "elytra", "phantom_membrane",
        "firework_rocket", "respawn_anchor", "nether_star", "dragon_egg", "end_crystal",
        # 99-advancement scarce pieces
        "enchanted_book", "name_tag", "saddle", "lead", "goat_horn", "spyglass", "brush",
        "glow_ink_sac", "honey_bottle", "honeycomb", "turtle_scute", "armadillo_scute",
        "wolf_armor", "sniffer_egg", "ominous_bottle", "trial_key", "ominous_trial_key",
        "decorated_pot", "nautilus_shell", "heart_of_the_sea",
        "conduit", "prismarine_shard", "prismarine_crystals", "ink_sac", "book", "writable_book",
        "written_book", "milk_bucket", "honey_block",
        # utility / stations
        "crafting_table", "furnace", "blast_furnace", "smoker", "enchanting_table",
        "anvil", "chipped_anvil", "damaged_anvil", "brewing_stand", "grindstone",
        "smithing_table", "stonecutter", "loom", "cartography_table", "fletching_table",
        "lectern", "composter", "barrel", "chest", "trapped_chest", "beacon", "lodestone",
        "compass", "recovery_compass", "clock", "map", "filled_map", "fishing_rod",
        "flint_and_steel", "shears", "shield", "bow", "crossbow", "arrow", "spectral_arrow",
        "tipped_arrow", "carrot_on_a_stick", "warped_fungus_on_a_stick", "lead",
        "shulker_box", "bucket", "water_bucket", "lava_bucket", "milk_bucket", "powder_snow_bucket",
        "cod_bucket", "salmon_bucket", "pufferfish_bucket", "tropical_fish_bucket",
        "axolotl_bucket", "tadpole_bucket",
        "netherite_upgrade_smithing_template",
        "book_and_quill", "experience_bottle", "glass_bottle", "potion", "splash_potion",
        "lingering_potion", "dragon_breath",
    }
)

_FAMILY_PREFIXES: tuple[str, ...] = (
    "music_disc_",
)
_FAMILY_SUFFIXES: tuple[str, ...] = (
    "_smithing_template",
    "_shulker_box",
    "_bucket",
    "_bed",
    "_spawn_egg",
    "_banner_pattern",
    "_sherd",
    "_pottery_sherd",
    "_horse_armor",
)


def _clean(name: Any) -> str:
    return str(name or "").removeprefix("minecraft:").strip().lower()


def is_important_item(name: str) -> bool:
    clean = _clean(name)
    if not clean:
        return False
    if clean in IMPORTANT_ITEMS:
        return True
    if clean.startswith(_FAMILY_PREFIXES):
        return True
    if clean.endswith(_FAMILY_SUFFIXES):
        return True
    if clean.startswith("enchanted_") and clean not in {"enchanted_golden_apple"}:
        return False
    return False


# Burn seconds per unit (vanilla 1.21 fuel table, only whitelisted entries).
SUPPORTED_FUELS: dict[str, float] = {
    "coal": 8.0,
    "charcoal": 8.0,
    "coal_block": 80.0,
    "dried_kelp_block": 20.0,
    "bamboo": 0.25,
    "stick": 0.5,
    "oak_planks": 1.5,
    "spruce_planks": 1.5,
    "birch_planks": 1.5,
    "jungle_planks": 1.5,
    "acacia_planks": 1.5,
    "dark_oak_planks": 1.5,
    "mangrove_planks": 1.5,
    "cherry_planks": 1.5,
    "pale_oak_planks": 1.5,
    "crimson_planks": 1.5,
    "warped_planks": 1.5,
    "oak_log": 1.5,
    "spruce_log": 1.5,
    "birch_log": 1.5,
    "jungle_log": 1.5,
    "acacia_log": 1.5,
    "dark_oak_log": 1.5,
    "mangrove_log": 1.5,
    "cherry_log": 1.5,
    "pale_oak_log": 1.5,
    "crimson_stem": 1.5,
    "warped_stem": 1.5,
}

# Never burn these even if an upstream caller asks for them explicitly.
FORBIDDEN_FUELS: frozenset[str] = frozenset(
    {
        "blaze_rod",
        "crafting_table",
        "chest",
        "trapped_chest",
        "barrel",
        "bookshelf",
        "ladder",
        "scaffolding",
        "oak_boat",
        "spruce_boat",
        "birch_boat",
        "jungle_boat",
        "acacia_boat",
        "dark_oak_boat",
        "bamboo_raft",
        "wooden_pickaxe",
        "wooden_axe",
        "wooden_shovel",
        "wooden_hoe",
        "wooden_sword",
        "bow",
        "crossbow",
        "shield",
        "fishing_rod",
        "note_block",
        "jukebox",
        "lectern",
        "barrel",
    }
)
_FORBIDDEN_SUFFIXES: tuple[str, ...] = ("_door", "_trapdoor", "_fence", "_fence_gate", "_sign", "_bed", "_banner", "_button", "_pressure_plate")


def is_safe_fuel(name: str, protected: Iterable[str] = ()) -> bool:
    clean = _clean(name)
    if not clean:
        return False
    prot = {_clean(p) for p in protected}
    if clean in prot:
        return False
    if clean in FORBIDDEN_FUELS:
        return False
    if clean.endswith(_FORBIDDEN_SUFFIXES):
        return False
    if clean in SUPPORTED_FUELS:
        return True
    if clean.endswith(("_planks", "_log", "_wood", "_stem", "_hyphae")):
        return True
    return False


FOOD_HUNGER_VALUES: dict[str, int] = {
    "cooked_beef": 8,
    "cooked_porkchop": 8,
    "golden_carrot": 6,
    "cooked_mutton": 6,
    "cooked_chicken": 6,
    "cooked_salmon": 6,
    "cooked_cod": 5,
    "baked_potato": 5,
    "bread": 5,
    "cooked_rabbit": 5,
    "mushroom_stew": 6,
    "rabbit_stew": 10,
    "beetroot_soup": 6,
    "pumpkin_pie": 8,
    "apple": 4,
    "carrot": 3,
    "golden_apple": 4,
    "enchanted_golden_apple": 4,
    "melon_slice": 2,
    "sweet_berries": 2,
    "glow_berries": 2,
    "cookie": 2,
    "dried_kelp": 1,
    "beef": 3,
    "porkchop": 3,
    "chicken": 2,
    "mutton": 2,
    "cod": 2,
    "salmon": 2,
    "tropical_fish": 1,
    "potato": 1,
    "poisonous_potato": 2,
    "rotten_flesh": 4,
    "spider_eye": 2,
    "chorus_fruit": 4,
    "honey_bottle": 6,
}

# Drop order inside the building-block pool (decor first, cobble family last + capped).
BUILDING_BLOCK_PRIORITY: tuple[str, ...] = (
    "tuff",
    "calcite",
    "dripstone_block",
    "diorite",
    "andesite",
    "granite",
    "polished_diorite",
    "polished_andesite",
    "polished_granite",
    "sand",
    "red_sand",
    "gravel",
    "dirt",
    "coarse_dirt",
    "rooted_dirt",
    "podzol",
    "mycelium",
    "mud",
    "clay",
    "netherrack",
    "basalt",
    "blackstone",
    "soul_soil",
    "soul_sand",
    "magma_block",
    "end_stone",
    "cobbled_deepslate",
    "cobblestone",
)
COBBLE_FAMILY: frozenset[str] = frozenset({"cobblestone", "cobbled_deepslate"})
RESERVED_BUILDING_BLOCKS = 96
COBBLE_CAP = 192

TIER1_ZERO_KEEP: frozenset[str] = frozenset(
    {
        "oak_leaves",
        "spruce_leaves",
        "birch_leaves",
        "jungle_leaves",
        "acacia_leaves",
        "dark_oak_leaves",
        "mangrove_leaves",
        "cherry_leaves",
        "azalea_leaves",
        "flowering_azalea_leaves",
        "dead_bush",
        "fern",
        "short_grass",
        "tall_grass",
        "leaf_litter",
        "wildflowers",
        "kelp",
        "seagrass",
        "vine",
        "poisonous_potato",
        "wheat_seeds",
        "beetroot_seeds",
        "pumpkin_seeds",
        "melon_seeds",
        "torchflower_seeds",
        "pitcher_pod",
        "brown_mushroom",
        "red_mushroom",
        "sugar_cane",
        "nether_sprouts",
        "crimson_roots",
        "warped_roots",
        "cobweb",
        "string",
        "flint",
        "gunpowder",
        "rotten_flesh",
        "spider_eye",
    }
)

# Never dropped by tier 4 even when the bag is full.
PROTECTED_LOADOUT: frozenset[str] = frozenset(
    {
        "shield",
        "water_bucket",
        "bucket",
        "flint_and_steel",
        "crafting_table",
        "furnace",
        "torch",
        "bread",
        "cooked_beef",
        "cooked_porkchop",
        "cooked_chicken",
        "cooked_mutton",
        "baked_potato",
        "golden_carrot",
        "apple",
    }
)


# ---------------------------------------------------------------------------
# Protection stack
# ---------------------------------------------------------------------------


class ProtectionStack:
    """`with inv_mgr.protect(items):` — nested frames of items that must not be dropped/burnt."""

    def __init__(self, initial: Iterable[str] = ()) -> None:
        self._frames: list[set[str]] = []
        if initial:
            self._frames.append({_clean(i) for i in initial if _clean(i)})

    def push(self, items: Iterable[str] | str | None) -> None:
        if items is None:
            return
        if isinstance(items, str):
            items = [items]
        frame = {_clean(i) for i in items if _clean(i)}
        self._frames.append(frame)

    def pop(self) -> set[str]:
        return self._frames.pop() if self._frames else set()

    def active(self) -> frozenset[str]:
        out: set[str] = set()
        for frame in self._frames:
            out.update(frame)
        return frozenset(out)

    @contextmanager
    def frame(self, items: Iterable[str] | str | None) -> Iterator[None]:
        self.push(items)
        try:
            yield
        finally:
            self.pop()

    def __len__(self) -> int:
        return len(self._frames)


# ---------------------------------------------------------------------------
# Pure planning helpers
# ---------------------------------------------------------------------------


def _rows(inv: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(inv, dict):
        return []
    rows = []
    for row in inv.get("items") or []:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("name"))
        count = int(row.get("count") or 0)
        if not name or count <= 0:
            continue
        rows.append(
            {
                "name": name,
                "count": count,
                "slot": row.get("slot"),
                "durability": row.get("durability"),
                "enchantments": row.get("enchantments") or [],
                "custom_name": row.get("custom_name"),
            }
        )
    return rows


def _empty_slots(inv: dict[str, Any] | None) -> int:
    if not isinstance(inv, dict):
        return 0
    raw = inv.get("empty_slots")
    if isinstance(raw, int):
        return max(0, raw)
    used = 0
    for row in _rows(inv):
        stack_max = 1 if row["durability"] is not None else 64
        used += math.ceil(row["count"] / stack_max) if stack_max > 1 else 1
    return max(0, 36 - used)


def _hunger_score(counts: dict[str, int], values: dict[str, int] | None = None) -> int:
    table = values or FOOD_HUNGER_VALUES
    return sum(int(table.get(name, 0)) * int(cnt) for name, cnt in counts.items())


def _durability_score(row: dict[str, Any]) -> float:
    dur = row.get("durability")
    if isinstance(dur, (list, tuple)) and len(dur) >= 2:
        try:
            return float(dur[0]) / max(1.0, float(dur[1]))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    return 0.0


def _beneficial_enchantments(row: dict[str, Any]) -> bool:
    enchs = row.get("enchantments")
    if not isinstance(enchs, list):
        return False
    return any(
        isinstance(e, dict) and int(e.get("level") or 0) > 0 and str(e.get("name") or "").lower()
        not in {"vanishing_curse", "binding_curse"}
        for e in enchs
    )


def _material_of(name: str) -> str:
    base = _clean(name)
    for suffix in ("_pickaxe", "_axe", "_shovel", "_hoe", "_sword", *ARMOR_SUFFIXES):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return ""


def _tool_kind(name: str) -> str:
    base = _clean(name)
    for suffix in ("_pickaxe", "_axe", "_shovel", "_hoe", "_sword"):
        if base.endswith(suffix):
            return suffix[1:]
    return ""


# ---------------------------------------------------------------------------
# plan_garbage_drops: the 4-tier garbage selector (pure)
# ---------------------------------------------------------------------------


def plan_garbage_drops(
    inv: dict[str, Any] | None,
    *,
    min_free: int = 4,
    protected: Iterable[str] = (),
    food_values: dict[str, int] | None = None,
    reserved_blocks: int = RESERVED_BUILDING_BLOCKS,
    cobble_cap: int = COBBLE_CAP,
) -> dict[str, Any]:
    """Return a deterministic drop plan honouring the 4-tier AltoClef ladder.

    Tier 1 zero-keep clutter -> Tier 2 building-block surplus (>=96 reserved, cobble <=192)
    -> Tier 3 unenchanted tool/armor thinning -> Tier 4 emergency smallest-stack eviction.
    """
    rows = _rows(inv)
    empty_before = _empty_slots(inv)
    need = max(0, int(min_free) - empty_before)
    prot = {_clean(p) for p in protected}
    drops: list[dict[str, Any]] = []
    dropped_slots: set[Any] = set()
    planned_names: dict[str, int] = {}

    def _free_of(row: dict[str, Any]) -> int:
        return 1

    def _add(row: dict[str, Any], tier: int, reason: str, count: int | None = None) -> None:
        nonlocal need
        if need <= 0:
            return
        if row["slot"] in dropped_slots:
            return
        take = int(row["count"] if count is None else min(row["count"], count))
        if take <= 0:
            return
        drops.append(
            {
                "slot": row["slot"],
                "item": row["name"],
                "count": take,
                "tier": tier,
                "reason": reason,
            }
        )
        dropped_slots.add(row["slot"])
        planned_names[row["name"]] = planned_names.get(row["name"], 0) + take
        need -= _free_of(row)

    def _is_hard_protected(row: dict[str, Any]) -> bool:
        if row["name"] in prot or is_important_item(row["name"]):
            return True
        if _beneficial_enchantments(row) or row.get("custom_name"):
            return True
        return False

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["name"]] = counts.get(row["name"], 0) + row["count"]
    hunger = _hunger_score(counts, food_values)

    # --- Tier 1: zero-keep clutter -------------------------------------
    for row in rows:
        if need <= 0:
            break
        name = row["name"]
        if name in prot or is_important_item(name):
            continue
        if name not in TIER1_ZERO_KEEP:
            continue
        if name == "rotten_flesh" and hunger < 16:
            continue
        if name == "spider_eye" and "spider_eye" in prot:
            continue
        if name in {"wheat_seeds", "beetroot_seeds", "pumpkin_seeds", "melon_seeds"} and (
            "seeds" in prot or name in prot
        ):
            continue
        _add(row, 1, "tier1_zero_keep_clutter")

    # --- Tier 2: building-block pool ---------------
    if need > 0:
        total_building = sum(counts.get(n, 0) for n in BUILDING_BLOCK_PRIORITY)
        pool_excess = max(0, total_building - max(0, int(reserved_blocks)))
        if pool_excess > 0:
            by_name: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                if row["slot"] in dropped_slots:
                    continue
                if row["name"] in BUILDING_BLOCK_PRIORITY and row["name"] not in prot:
                    by_name.setdefault(row["name"], []).append(row)
            for name in BUILDING_BLOCK_PRIORITY:
                if need <= 0 or pool_excess <= 0:
                    break
                cands = by_name.get(name) or []
                if name in COBBLE_FAMILY:
                    keep = min(counts.get(name, 0), int(cobble_cap))
                    excess = counts.get(name, 0) - keep
                else:
                    excess = counts.get(name, 0)
                excess = min(excess, pool_excess)
                for row in sorted(cands, key=lambda r: int(r["count"])):
                    if need <= 0 or excess <= 0:
                        break
                    take = min(row["count"], excess)
                    _add(row, 2, "tier2_building_surplus", count=take)
                    excess -= take
                    pool_excess -= take

    # --- Tier 3: unenchanted tool/armor thinning -----------------------
    if need > 0:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["slot"] in dropped_slots or _is_hard_protected(row):
                continue
            kind = _tool_kind(row["name"])
            if kind:
                groups.setdefault(kind, []).append(row)
            elif row["name"].endswith(ARMOR_SUFFIXES):
                groups.setdefault("armor_" + _material_of(row["name"]) + row["name"].split("_")[-1], []).append(row)

        def _rank(row: dict[str, Any]) -> tuple[int, float]:
            return (TOOL_TIERS.get(_material_of(row["name"]), 0), _durability_score(row))

        for kind in ("pickaxe", "sword", "axe", "shovel", "hoe"):
            cands = groups.get(kind) or []
            if not cands:
                continue
            cands.sort(key=_rank, reverse=True)
            if kind == "pickaxe":
                mains = [r for r in cands if TOOL_TIERS.get(_material_of(r["name"]), 0) >= 3]
                stones = [r for r in cands if _material_of(r["name"]) == "stone"]
                weak = [r for r in cands if _material_of(r["name"]) in {"wooden", "golden"}]
                keep_main = mains[:2]
                keep_stone = stones[:2] if (mains or stones) else []
                keep_ids = {id(r) for r in keep_main + keep_stone}
                for row in cands:
                    if need <= 0:
                        break
                    if id(row) in keep_ids:
                        continue
                    reason = (
                        "tier3_weak_pickaxe"
                        if row["name"] in weak or _material_of(row["name"]) in {"wooden", "golden"}
                        else "tier3_surplus_pickaxe"
                    )
                    _add(row, 3, reason)
            else:
                keep = cands[0]
                for row in cands[1:]:
                    if need <= 0:
                        break
                    _add(row, 3, f"tier3_duplicate_{kind}")

        # spare armor not worn: only if a same-slot worn piece is at least as good
        armor_worn = (inv or {}).get("armor") if isinstance(inv, dict) else {}
        if isinstance(armor_worn, dict):
            suffix_for_part = {
                "head": "_helmet",
                "chest": "_chestplate",
                "legs": "_leggings",
                "feet": "_boots",
            }
            for part, worn in armor_worn.items():
                suffix = suffix_for_part.get(str(part))
                if not suffix or not isinstance(worn, dict) or not worn.get("name"):
                    continue
                worn_tier = ARMOR_TIERS.get(_material_of(worn.get("name")), 0)
                for row in rows:
                    if need <= 0:
                        break
                    if row["slot"] in dropped_slots or _is_hard_protected(row):
                        continue
                    if not row["name"].endswith(suffix):
                        continue
                    if ARMOR_TIERS.get(_material_of(row["name"]), 0) <= worn_tier:
                        _add(row, 3, "tier3_old_armor")

    # --- Tier 4: emergency smallest-stack eviction ---------------------
    if need > 0:
        keep_food = hunger < 16
        candidates = []
        for row in rows:
            if row["slot"] in dropped_slots:
                continue
            if _is_hard_protected(row) or row["name"] in PROTECTED_LOADOUT:
                continue
            if _gain_equipment(row, inv):
                continue
            if keep_food and row["name"] in FOOD_HUNGER_VALUES:
                continue
            candidates.append(row)
        candidates.sort(key=lambda r: (int(r["count"]), str(r["slot"])))
        for row in candidates:
            if need <= 0:
                break
            _add(row, 4, "tier4_emergency_smallest_stack")

    freed = len(dropped_slots)
    return {
        "drops": drops,
        "freed_slots": freed,
        "empty_slots_before": empty_before,
        "empty_slots_after": empty_before + freed,
        "min_free": int(min_free),
        "reached_target": empty_before + freed >= int(min_free),
        "food_score": hunger,
        "protected": sorted(prot),
        "tiers_used": sorted({d["tier"] for d in drops}),
    }


def _gain_equipment(row: dict[str, Any], inv: dict[str, Any] | None) -> bool:
    """Keep equipped/held gear and the best tool material in the bag."""
    if isinstance(inv, dict):
        held = inv.get("held")
        if isinstance(held, dict) and _clean(held.get("name")) == row["name"]:
            return True
        off = inv.get("offhand")
        if isinstance(off, dict) and _clean(off.get("name")) == row["name"]:
            return True
        armor = inv.get("armor")
        if isinstance(armor, dict):
            for worn in armor.values():
                if isinstance(worn, dict) and _clean(worn.get("name")) == row["name"]:
                    return True
    return False


# ---------------------------------------------------------------------------
# InventoryManager
# ---------------------------------------------------------------------------


class InventoryManager:
    """Stateful wrapper around a live ``BotSession`` plus optional ``WorldMemory``."""

    def __init__(self, bot: Any, *, world_memory: Any = None, skills: Any = None) -> None:
        self.bot = bot
        self.world_memory = world_memory
        self.skills = skills
        self.protection = ProtectionStack()
        self.auto_dropped_names: set[str] = set()

    # -- protection ------------------------------------------------------
    @contextmanager
    def protect(self, items: Iterable[str] | str | None) -> Iterator[None]:
        with self.protection.frame(items):
            yield

    def active_protected(self) -> frozenset[str]:
        return self.protection.active()

    # -- inventory views -------------------------------------------------
    def _inventory(self) -> dict[str, Any]:
        try:
            inv = self.bot.inventory()
        except ApiError:
            return {}
        return inv if isinstance(inv, dict) else {}

    def get_inventory_only_counts(self, inv: dict[str, Any] | None = None) -> dict[str, int]:
        inv = inv if isinstance(inv, dict) else self._inventory()
        out: dict[str, int] = {}
        for row in _rows(inv):
            out[row["name"]] = out.get(row["name"], 0) + int(row["count"])
        for key in ("held", "offhand"):
            row = inv.get(key)
            if isinstance(row, dict) and row.get("name"):
                name = _clean(row.get("name"))
                out[name] = out.get(name, 0) + int(row.get("count") or 1)
        armor = inv.get("armor")
        if isinstance(armor, dict):
            for row in armor.values():
                if isinstance(row, dict) and row.get("name"):
                    name = _clean(row.get("name"))
                    out[name] = out.get(name, 0) + int(row.get("count") or 1)
        return out

    def get_accessible_counts(
        self,
        inv: dict[str, Any] | None = None,
        *,
        origin: Iterable[float] | None = None,
        dim: str | None = None,
        max_chest_dist: float = 72.0,
    ) -> dict[str, int]:
        out = dict(self.get_inventory_only_counts(inv))
        wm = self.world_memory
        if wm is None:
            return out
        try:
            st = self.bot.status()
        except ApiError:
            st = {}
        dim = str(dim or st.get("dimension") or "overworld")
        pos = list(origin) if origin is not None else list(st.get("pos") or [0.0, 64.0, 0.0])
        for (cdim, cx, cy, cz), meta in getattr(wm, "containers", {}).items():
            if cdim != dim:
                continue
            if math.dist(pos, [cx + 0.5, cy + 0.5, cz + 0.5]) > max_chest_dist:
                continue
            for name, cnt in (meta.get("items") or {}).items():
                out[_clean(name)] = out.get(_clean(name), 0) + int(cnt)
        try:
            for name, cnt in (wm.get_in_conversion_counts(dim=dim) or {}).items():
                out[_clean(name)] = out.get(_clean(name), 0) + int(cnt)
        except (AttributeError, TypeError):
            pass
        return out

    # -- dropping --------------------------------------------------------
    def plan(self, inv: dict[str, Any] | None = None, *, min_free: int = 4) -> dict[str, Any]:
        return plan_garbage_drops(
            inv if isinstance(inv, dict) else self._inventory(),
            min_free=min_free,
            protected=self.active_protected(),
        )

    def free_slots(self, min_free: int = 4, *, turn_away: bool = True, max_rounds: int = 3) -> dict[str, Any]:
        inv = self._inventory()
        empty_before = _empty_slots(inv)
        if empty_before >= min_free:
            return {
                "ok": True,
                "dropped": [],
                "empty_slots_before": empty_before,
                "empty_slots_after": empty_before,
                "reason": "already_enough",
            }
        dropped: list[dict[str, Any]] = []
        turned = False
        turn_yaw: float | None = None
        for _ in range(max(1, max_rounds)):
            plan = self.plan(inv, min_free=min_free)
            if not plan.get("drops"):
                break
            if turn_away and not turned:
                try:
                    st = self.bot.status()
                    turn_yaw = float(st.get("yaw") or 0.0) + math.pi
                    self.bot.call("look", {"yaw": turn_yaw, "pitch": 0.0})
                    turned = True
                except ApiError:
                    pass
            for item in plan["drops"]:
                try:
                    self.bot.call("drop", {"item": item["item"], "count": int(item["count"])})
                    dropped.append(item)
                    self.auto_dropped_names.add(item["item"])
                except ApiError as err:
                    if err.code in ("death", "interrupted", "not_connected"):
                        return {
                            "ok": False,
                            "error": err.code,
                            "dropped": dropped,
                            "empty_slots_before": empty_before,
                            "empty_slots_after": _empty_slots(self._inventory()),
                        }
            # Walk a couple of blocks away from the pile we just threw (we are already facing away)
            # so the vanilla pickup radius cannot suck the junk straight back in.
            if turned and plan.get("drops"):
                for _ in range(2):
                    try:
                        self.bot.call("control", {"forward": True, "ms": 420, "stop_on": ["death"]})
                    except ApiError:
                        break
            inv = self._inventory()
            if _empty_slots(inv) >= min_free:
                break
        after = _empty_slots(inv)
        return {
            "ok": after >= min_free,
            "dropped": dropped,
            "empty_slots_before": empty_before,
            "empty_slots_after": after,
            "turned_away": turned,
            "turn_yaw": round(turn_yaw, 3) if turn_yaw is not None else None,
            "auto_dropped_names": sorted(self.auto_dropped_names),
        }

    # -- equipment -------------------------------------------------------
    def auto_equip_best(
        self,
        *,
        dimension: str = "overworld",
        preserve_offhand: bool = False,
        equip_aliases: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        inv = self._inventory()
        rows = _rows(inv)
        armor_worn = inv.get("armor") if isinstance(inv.get("armor"), dict) else {}
        part_suffix = {
            "head": "_helmet",
            "chest": "_chestplate",
            "legs": "_leggings",
            "feet": "_boots",
        }
        # Live-verified 1.21.11 slot names: head / torso / legs / feet / off-hand / hand.
        # `chest` is NOT a valid destination (ERR_ASSERTION: invalid destination: chest).
        aliases = equip_aliases or {
            "head": ("head", "helmet"),
            "chest": ("torso", "chest"),
            "legs": ("legs", "leggings"),
            "feet": ("feet", "boots"),
            "offhand": ("off-hand", "offhand"),
        }
        equipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        def _score(row: dict[str, Any]) -> tuple[int, int, float]:
            return (
                ARMOR_TIERS.get(_material_of(row["name"]), 0),
                1 if _beneficial_enchantments(row) else 0,
                _durability_score(row),
            )

        def _try_equip(item: str, part: str) -> bool:
            last: str | None = None
            for alias in aliases.get(part, (part,)):
                try:
                    self.bot.call("equip", {"item": item, "slot": alias})
                    equipped.append({"part": part, "item": item, "alias": alias})
                    return True
                except ApiError as err:
                    last = f"{err.code}@{alias}"
                    if err.code in ("death", "interrupted", "not_connected"):
                        break
            failed.append({"part": part, "item": item, "error": last})
            return False

        for part, suffix in part_suffix.items():
            worn = armor_worn.get(part)
            worn_score = _score(worn) if isinstance(worn, dict) and worn.get("name") else (-1, -1, -1.0)
            cands = [r for r in rows if r["name"].endswith(suffix)]
            if not cands:
                continue
            cands.sort(key=_score, reverse=True)
            best = cands[0]
            if _score(best) > worn_score:
                _try_equip(best["name"], part)

        if str(dimension) == "the_nether":
            inv2 = self._inventory()
            worn2 = inv2.get("armor") if isinstance(inv2.get("armor"), dict) else {}
            has_gold = any(
                isinstance(w, dict) and _material_of(w.get("name")) == "golden" for w in worn2.values()
            )
            if not has_gold:
                gold = [r for r in rows if _material_of(r["name"]) == "golden" and r["name"].endswith(ARMOR_SUFFIXES)]
                if gold:
                    gold.sort(key=_score, reverse=True)
                    part = next(
                        (p for p, s in part_suffix.items() if gold[0]["name"].endswith(s)),
                        "feet",
                    )
                    _try_equip(gold[0]["name"], part)

        if not preserve_offhand:
            off = inv.get("offhand")
            if not (isinstance(off, dict) and _clean(off.get("name"))):
                shields = [r for r in rows if r["name"] == "shield"]
                if shields:
                    _try_equip("shield", "offhand")

        return {
            "ok": not failed,
            "equipped": equipped,
            "failed": failed,
            "armor_before": {
                p: (_clean(w.get("name")) if isinstance(w, dict) else None) for p, w in armor_worn.items()
            },
            "offhand_before": (_clean((inv.get("offhand") or {}).get("name")) if isinstance(inv.get("offhand"), dict) else None),
        }

    # -- stashing --------------------------------------------------------
    def _known_containers(self, *, radius: float, dim: str, origin: list[float]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        wm = self.world_memory
        if wm is not None:
            for (cdim, cx, cy, cz), meta in getattr(wm, "containers", {}).items():
                if cdim != dim:
                    continue
                d = math.dist(origin, [cx + 0.5, cy + 0.5, cz + 0.5])
                if d <= radius:
                    out.append(
                        {
                            "pos": [cx, cy, cz],
                            "block": meta.get("block") or "chest",
                            "items": meta.get("items") or {},
                            "dist": round(d, 1),
                            "source": "memory",
                        }
                    )
        if not out:
            try:
                found = self.bot.call(
                    "find_blocks",
                    {"radius": int(min(48, max(4, radius))), "types": ["chest", "barrel", "trapped_chest"], "limit": 8},
                )
                for blk in found.get("blocks") or []:
                    if not isinstance(blk, dict) or not isinstance(blk.get("pos"), list):
                        continue
                    out.append(
                        {
                            "pos": [int(v) for v in blk["pos"][:3]],
                            "block": _clean(blk.get("name") or "chest"),
                            "items": {},
                            "dist": float(blk.get("dist") or 0.0),
                            "source": "find_blocks",
                        }
                    )
            except ApiError:
                pass
        out.sort(key=lambda c: c["dist"])
        return out

    def _dungeon_spawner_near(self, pos: list[int], *, radius: int = 6) -> bool:
        try:
            found = self.bot.call("find_blocks", {"radius": 12, "types": ["spawner"], "limit": 8})
        except ApiError:
            return False
        for blk in found.get("blocks") or []:
            if not isinstance(blk, dict) or not isinstance(blk.get("pos"), list):
                continue
            try:
                if math.dist(pos, [float(v) for v in blk["pos"][:3]]) <= radius:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def stash_surplus_nearby(
        self,
        *,
        radius: float = 16.0,
        dim: str | None = None,
        keep: Iterable[str] | None = None,
        craft_chest_if_full: bool = True,
    ) -> dict[str, Any]:
        inv = self._inventory()
        counts = self.get_inventory_only_counts(inv)
        if _empty_slots(inv) >= 12:
            return {"ok": True, "skipped": "inventory_not_full", "empty_slots": _empty_slots(inv)}
        try:
            st = self.bot.status()
        except ApiError:
            st = {}
        dim = str(dim or st.get("dimension") or "overworld")
        origin = [float(v) for v in (st.get("pos") or [0, 64, 0])]
        keep_set = {_clean(k) for k in (keep or ())} | set(PROTECTED_LOADOUT)
        # keep loadout: best pickaxe + combat weapon + food <=32 + blocks 64
        surplus: dict[str, int] = {}
        for name, cnt in counts.items():
            if name in keep_set or name in self.active_protected():
                continue
            if not is_important_item(name):
                continue
            if name.endswith(("_pickaxe", "_sword", "_axe", "_shovel", "_hoe")):
                continue
            surplus[name] = cnt
        if not surplus:
            return {"ok": True, "skipped": "no_surplus_important_items", "empty_slots": _empty_slots(inv)}

        chests = self._known_containers(radius=radius, dim=dim, origin=origin)
        skipped_dungeon: list[list[int]] = []
        target = None
        for c in chests:
            if self._dungeon_spawner_near(c["pos"]):
                skipped_dungeon.append(c["pos"])
                continue
            target = c
            break

        new_chest: list[int] | None = None
        if target is None:
            if not craft_chest_if_full:
                return {"ok": False, "error": "no_safe_container", "skipped_dungeon": skipped_dungeon}
            planks = sum(v for k, v in counts.items() if k.endswith("_planks"))
            if planks < 8:
                return {
                    "ok": False,
                    "error": "no_safe_container",
                    "message": f"附近没有非地牢箱子且木板仅 {planks} <8，无法自建箱子。",
                    "skipped_dungeon": skipped_dungeon,
                }
            try:
                self.bot.call("craft", {"item": "chest", "count": 1})
                placed = self.bot.call("place", {"item": "chest"})
                pos = placed.get("pos")
                if isinstance(pos, list) and len(pos) >= 3:
                    new_chest = [int(v) for v in pos[:3]]
                    target = {"pos": new_chest, "block": "chest", "items": {}, "dist": 0.0, "source": "placed"}
                    wm = self.world_memory
                    if wm is not None:
                        try:
                            wm.update_container(new_chest, {}, block_name="chest", dim=dim)
                        except TypeError:
                            wm.update_container(new_chest, {}, block_name="chest")
            except ApiError as err:
                return {"ok": False, "error": f"place_chest:{err.code}", "skipped_dungeon": skipped_dungeon}

        if target is None:
            return {"ok": False, "error": "no_safe_container", "skipped_dungeon": skipped_dungeon}

        deposit_res: dict[str, Any]
        try:
            if self.skills is not None and hasattr(self.skills, "container_deposit"):
                deposit_res = self.skills.container_deposit(
                    surplus, x=target["pos"][0], y=target["pos"][1], z=target["pos"][2]
                )
            else:
                deposit_res = self.bot.call(
                    "window/act",
                    {
                        "target": {"pos": target["pos"]},
                        "ops": [
                            {"op": "put", "item": name, "count": cnt, "to": "container"}
                            for name, cnt in surplus.items()
                        ],
                    },
                )
        except ApiError as err:
            deposit_res = {"ok": False, "error": err.code, "message": err.message}

        if not deposit_res.get("ok") and craft_chest_if_full:
            # chest full / no free section: craft and place a brand-new chest, then retry once
            new_pos = self._craft_and_place_chest(counts, dim)
            if new_pos is not None:
                new_chest = new_pos
                try:
                    if self.skills is not None and hasattr(self.skills, "container_deposit"):
                        deposit_res = self.skills.container_deposit(
                            surplus, x=new_pos[0], y=new_pos[1], z=new_pos[2]
                        )
                    else:
                        deposit_res = self.bot.call(
                            "window/act",
                            {
                                "target": {"pos": new_pos},
                                "ops": [
                                    {"op": "put", "item": name, "count": cnt, "to": "container"}
                                    for name, cnt in surplus.items()
                                ],
                            },
                        )
                    target = {"pos": new_pos, "block": "chest", "items": {}, "dist": 0.0, "source": "placed_after_full"}
                except ApiError as err:
                    deposit_res = {"ok": False, "error": err.code, "message": err.message}

        return {
            "ok": bool(deposit_res.get("ok")),
            "target": target["pos"],
            "new_chest": new_chest,
            "deposited": surplus,
            "result": deposit_res,
            "skipped_dungeon": skipped_dungeon,
            "empty_slots": _empty_slots(self._inventory()),
        }

    def _craft_and_place_chest(self, counts: dict[str, int], dim: str) -> list[int] | None:
        planks = sum(v for k, v in counts.items() if k.endswith("_planks"))
        if planks < 8:
            return None
        try:
            self.bot.call("craft", {"item": "chest", "count": 1})
            placed = self.bot.call("place", {"item": "chest"})
        except ApiError:
            return None
        pos = placed.get("pos") if isinstance(placed, dict) else None
        if not (isinstance(pos, list) and len(pos) >= 3):
            return None
        pt = [int(v) for v in pos[:3]]
        wm = self.world_memory
        if wm is not None:
            try:
                wm.update_container(pt, {}, block_name="chest", dim=dim)
            except TypeError:
                wm.update_container(pt, {}, block_name="chest")
        return pt


__all__ = [
    "IMPORTANT_ITEMS",
    "SUPPORTED_FUELS",
    "FORBIDDEN_FUELS",
    "FOOD_HUNGER_VALUES",
    "BUILDING_BLOCK_PRIORITY",
    "ProtectionStack",
    "InventoryManager",
    "plan_garbage_drops",
    "is_important_item",
    "is_safe_fuel",
]
