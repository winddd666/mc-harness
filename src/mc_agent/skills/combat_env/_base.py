"""Spatial probes, tactical micro-delegates, and pluggable CombatControllerSlot (`CombatEnv` v1).

Implements `docs/combat-env-interface-spec.md`:
- Layer 1 (0 action-lock, cached 3.0s): `has_los`, `probe_ray`, `scan_safe_yaws`, `arena_snapshot`
- Layer 2 (action-lock delegates): `chase_to_range`, `flee`, `build_tactical`
- Pluggable L4 slot: `CombatControllerSlot` + `SubcorticalInstinctController` (pre-teammate aggressive
  instinct loop: counter-kill lone melee with weapon, seal/rush/evade skeletons & swarms, shield-retreat
  creepers, eat post-fight, and always reset `/posture {}` on exit).
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Collection, Literal, Protocol, Sequence, TypedDict

import httpx

from ...errors import ApiError
from ..safety import SAFE_FOOD_PRIORITY, auto_equip_survival_gear, water_oxygen_below

AIR_LIKE_BLOCKS: frozenset[str] = frozenset(
    {
        "air",
        "cave_air",
        "void_air",
        "light",
        "structure_void",
    }
)
BREATHABLE_AIR_BLOCKS = frozenset({"air", "cave_air", "void_air"})
WATER_BLOCKS = frozenset({"water", "flowing_water"})

# Blocks that do NOT block vision/arrows and do NOT block flat walking
PASSABLE_TRANSPARENT_BLOCKS: frozenset[str] = frozenset(
    {
        *AIR_LIKE_BLOCKS,
        "short_grass",
        "tall_grass",
        "fern",
        "large_fern",
        "dead_bush",
        "leaf_litter",
        "wildflowers",
        "dandelion",
        "poppy",
        "blue_orchid",
        "allium",
        "azure_bluet",
        "red_tulip",
        "orange_tulip",
        "white_tulip",
        "pink_tulip",
        "oxeye_daisy",
        "cornflower",
        "lily_of_the_valley",
        "torch",
        "wall_torch",
        "soul_torch",
        "soul_wall_torch",
        "redstone_torch",
        "redstone_wall_torch",
        "redstone_wire",
        "lever",
        "snow",
        "vine",
        "cave_vines",
        "cave_vines_plant",
        "glow_lichen",
        "sugar_cane",
        "wheat",
        "carrots",
        "potatoes",
        "beetroots",
        "oak_sapling",
        "spruce_sapling",
        "birch_sapling",
        "jungle_sapling",
        "acacia_sapling",
        "dark_oak_sapling",
        "cherry_sapling",
    }
)

HAZARD_BLOCK_TYPES: tuple[str, ...] = (
    "lava",
    "flowing_lava",
    "fire",
    "soul_fire",
    "magma_block",
    "water",
    "flowing_water",
    "cactus",
    "sweet_berry_bush",
    "powder_snow",
    "cobweb",
    "pointed_dripstone",
)

SURFACE_SHORE_PRIORITY_BLOCKS: tuple[str, ...] = (
    "grass_block",
    "dirt_path",
    "coarse_dirt",
    "podzol",
    "mycelium",
    "sandstone",
    "cobblestone",
    "oak_planks",
    "spruce_planks",
    "birch_planks",
    "oak_log",
    "spruce_log",
)

SHORE_LAND_BLOCKS: tuple[str, ...] = (
    "grass_block",
    "dirt_path",
    "dirt",
    "sand",
    "stone",
    "cobblestone",
    "gravel",
    "sandstone",
    "deepslate",
    "mud",
    "clay",
    "oak_planks",
    "spruce_planks",
    "oak_log",
    "spruce_log",
    "ice",
    "packed_ice",
    "snow_block",
    "moss_block",
)

NETHER_SHORE_BLOCKS: tuple[str, ...] = (
    "netherrack",
    "basalt",
    "blackstone",
    "polished_blackstone",
    "polished_blackstone_bricks",
    "gilded_blackstone",
    "nether_bricks",
    "cracked_nether_bricks",
    "soul_sand",
    "soul_soil",
    "crimson_nylium",
    "warped_nylium",
)

LAVA_SHORE_BLOCKS: tuple[str, ...] = SHORE_LAND_BLOCKS + NETHER_SHORE_BLOCKS

TRAP_BLOCKS: frozenset[str] = frozenset(
    {
        "cactus",
        "sweet_berry_bush",
        "powder_snow",
        "cobweb",
        "magma_block",
        "pointed_dripstone",
    }
)

SAFE_TACTICAL_FILLERS: tuple[str, ...] = (
    "cobblestone",
    "cobbled_deepslate",
    "stone",
    "deepslate",
    "andesite",
    "diorite",
    "granite",
    "tuff",
    "netherrack",
    "blackstone",
    "basalt",
    "dirt",
    "coarse_dirt",
)

MELEE_WEAPON_ORDER: tuple[str, ...] = (
    "netherite_sword",
    "diamond_sword",
    "iron_sword",
    "stone_sword",
    "netherite_axe",
    "diamond_axe",
    "iron_axe",
    "stone_axe",
    "wooden_sword",
    "golden_sword",
    "wooden_axe",
    "golden_axe",
)

LONE_COUNTERKILL_MOBS: frozenset[str] = frozenset(
    {
        "zombie",
        "zombie_villager",
        "husk",
        "drowned",
        "spider",
        "cave_spider",
        "silverfish",
        "endermite",
        "slime",
    }
)

CORNERED_PIGLIN_MAX_HEALTH = 12.0
CORNERED_PIGLIN_REACH = 2.5

# Ambush melee hostiles watched by Guardian pre_hurt / mid-tool watcher (excludes neutral pigman/enderman).
PRE_HURT_MELEE_MOBS: frozenset[str] = LONE_COUNTERKILL_MOBS | frozenset(
    {
        "piglin",
        "piglin_brute",
        "wither_skeleton",
        "hoglin",
        "vindicator",
        "ravager",
        "phantom",
        "magma_cube",
        "zoglin",
    }
)

RANGED_MOBS: frozenset[str] = frozenset(
    {
        "skeleton",
        "stray",
        "bogged",
        "pillager",
        "witch",
        "blaze",
        "breeze",
    }
)
WARDEN_EVADE_DISTANCE = 16.0

CARDINAL_VECTORS: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}


def attack_feedback_hits(attack: dict[str, Any]) -> int | float | None:
    """Prefer connected hits or target health feedback over legacy swing counts."""
    for key in ("connected_hits", "hits_connected"):
        count = attack.get(key)
        if type(count) in (int, float) and math.isfinite(count) and count >= 0:
            return count
    before, after = attack.get("target_health_before"), attack.get("target_health_after")
    if all(type(value) in (int, float) and math.isfinite(value) for value in (before, after)):
        return int(after < before)
    delta = attack.get("target_hp_delta")
    if type(delta) in (int, float) and delta == 0:
        return 0
    count = attack.get("hits")
    return count if type(count) in (int, float) and math.isfinite(count) and count >= 0 else None


def shot_release_confirmed(shot: dict[str, Any]) -> bool:
    """Use projectile or consumption evidence when the API supplies release telemetry."""
    if shot.get("fired") is not True:
        return False
    if "projectiles" not in shot and "used" not in shot:
        return True
    projectiles = shot.get("projectiles")
    if isinstance(projectiles, list) and any(isinstance(p, dict) and type(p.get("id")) is int for p in projectiles):
        return True
    used = shot.get("used")
    if isinstance(used, dict) and any(type(count) is int and count > 0 for count in used.values()):
        return True
    before, after = shot.get("target_health_before"), shot.get("target_health_after")
    return shot.get("killed") is True or (
        type(before) in (int, float) and type(after) in (int, float) and after < before
    )


def _clean_block_name(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("type") or ""
    return str(raw or "air").removeprefix("minecraft:").strip().lower() or "air"


def _is_passable(name: str) -> bool:
    if name in PASSABLE_TRANSPARENT_BLOCKS:
        return True
    if name.endswith(("_carpet", "_button", "_pressure_plate", "_sign", "_banner")):
        return True
    return False


def _is_los_transparent(name: str) -> bool:
    if _is_passable(name):
        return True
    if name in {"water", "flowing_water", "glass", "iron_bars", "cobweb", "fire", "soul_fire"} or name.endswith(("_glass", "_glass_pane")):
        return True
    return False


def _is_full_cover_block(name: str) -> bool:
    return name in SAFE_TACTICAL_FILLERS or name.endswith(("_planks", "_log"))


def _is_lava_block(name: str) -> bool:
    return name in {"lava", "flowing_lava"}


def _water_requires_surface(status: dict[str, Any], *, oxygen_bubbles: float) -> bool:
    return bool(status.get("in_water") and (status.get("submerged") or water_oxygen_below(status, oxygen_bubbles)))


def _combat_status_unavailable(status: dict[str, Any]) -> bool:
    health = status.get("health")
    return (
        status.get("ready") is False or status.get("alive") is False
        or (isinstance(health, (int, float)) and health <= 0)
    )


def _hitbox_columns(px: float, pz: float) -> list[tuple[int, int]]:
    """Return unique integer `(x, z)` columns overlapped by the player's 0.6x0.6 horizontal hitbox."""
    return list(
        dict.fromkeys(
            (math.floor(px + ox), math.floor(pz + oz))
            for ox in (-0.3, 0.3)
            for oz in (-0.3, 0.3)
        )
    )


def footing_blocks(pos: Sequence[float]) -> list[tuple[int, int, int]]:
    x, y, z = pos[:3]
    floor_y = math.floor(y + 0.05) - 1
    return [(bx, floor_y, bz) for bx, bz in _hitbox_columns(x, z)]


def _is_control_signal(error: Exception) -> bool:
    if not isinstance(error, RuntimeError):
        return False
    # Importing the harness before the skills package finishes initializing creates a cycle.
    from ...harness.control import RequestCancelled, RunTimedOut

    return isinstance(error, (RequestCancelled, RunTimedOut))


def retreat_made_progress(ok: bool, moved: Any, reason: Any) -> bool:
    """Recognize a partial retreat with movement and no reported landing hazard."""
    return ok or (reason is None and isinstance(moved, (int, float)) and math.isfinite(moved) and moved >= 0.35)


def ranged_retreat_positions(
    origin: Sequence[float], entities: Sequence[dict[str, Any]], *, max_distance: float,
    trident_attacker_id: int | None = None, include_drowned: bool = False,
) -> list[tuple[int | None, Sequence[float]]]:
    positions = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        pos = entity.get("pos")
        name = _clean_block_name(entity.get("name"))
        entity_id = entity.get("id")
        if (
            (
                name in RANGED_MOBS or (
                    name == "drowned" and (include_drowned or (trident_attacker_id is not None and entity_id == trident_attacker_id))
                )
            )
            and isinstance(pos, (list, tuple)) and len(pos) >= 3
            and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            and math.dist(origin[:3], pos[:3]) <= max_distance
        ):
            positions.append((entity_id, pos))
    return positions


def melee_clear_for_projectile_cover(entities: Sequence[dict[str, Any]], *, max_distance: float = 5.5) -> bool:
    return not any(
        isinstance(entity, dict)
        and str(entity.get("name") or "").removeprefix("minecraft:") in PRE_HURT_MELEE_MOBS | {"creeper"}
        and (
            not isinstance(entity.get("dist"), (int, float))
            or not math.isfinite(entity["dist"]) or entity["dist"] <= max_distance
        )
        for entity in entities
    )


def _is_solid_ground(name: str) -> bool:
    if _is_passable(name):
        return False
    if name in {"water", "flowing_water", "lava", "flowing_lava", "fire", "soul_fire", "powder_snow", "cobweb"}:
        return False
    return True


def aim_yaw_pitch(
    bot_pos: Sequence[float],
    target_pos: Sequence[float],
    *,
    src_eye: float = 1.62,
    dst_height: float = 0.85,
) -> tuple[float, float]:
    """Mineflayer-compatible yaw/pitch (`yaw = atan2(-dx, -dz)`)."""
    dx = float(target_pos[0]) - float(bot_pos[0])
    dy = (float(target_pos[1]) + dst_height) - (float(bot_pos[1]) + src_eye)
    dz = float(target_pos[2]) - float(bot_pos[2])
    yaw = math.atan2(-dx, -dz)
    pitch = math.atan2(dy, max(1e-6, math.hypot(dx, dz)))
    return yaw, pitch


def yaw_to_unit_xz(yaw: float) -> tuple[float, float]:
    """Inverse of `yaw = atan2(-dx, -dz)` -> `(dx, dz) = (-sin(yaw), -cos(yaw))`."""
    return -math.sin(float(yaw)), -math.cos(float(yaw))


class ThreatTrigger(TypedDict, total=False):
    source: Literal[
        "pre_hurt_creeper",
        "pre_hurt_melee",
        "pre_hurt_ranged",
        "tool_interrupted_hurt",
        "tool_water_threat",
        "boundary_threat",
        "guard_poll_hurt",
    ]
    primary_entity_id: int | None
    primary_type: str | None
    primary_pos: list[float] | None
    distance: float | None
    hurt_source: str | None
    observed_life: tuple[Any, Any]
    water_observation: dict[str, Any]
    action_released: bool
    staircase_surface_exit: bool


class CombatOutcome(TypedDict, total=False):
    ok: bool
    handled: bool
    mode: str
    summary: str
    actions: list[dict[str, Any]]
    status_after: dict[str, Any]
    requires_replan: bool
    latched_guard: bool
    release_unready_shield: bool


class CombatController(Protocol):
    def on_encounter(self, env: "CombatEnv", trigger: ThreatTrigger) -> CombatOutcome: ...


class _TerrainProbeExpired(Exception):
    """An urgent terrain probe ran out of time or could not verify a block."""


class _CombatFortificationInterrupted(Exception):
    def __init__(self, status: dict[str, Any], *, life_unavailable: bool) -> None:
        self.status = status
        self.life_unavailable = life_unavailable
