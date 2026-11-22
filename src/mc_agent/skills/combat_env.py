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

from ..errors import ApiError
from .safety import SAFE_FOOD_PRIORITY, auto_equip_survival_gear

AIR_LIKE_BLOCKS: frozenset[str] = frozenset(
    {
        "air",
        "cave_air",
        "void_air",
        "light",
        "structure_void",
    }
)

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


def _is_lava_block(name: str) -> bool:
    return name in {"lava", "flowing_lava"}


def _hitbox_columns(px: float, pz: float) -> list[tuple[int, int]]:
    """Return unique integer `(x, z)` columns overlapped by the player's 0.6x0.6 horizontal hitbox."""
    return list(
        dict.fromkeys(
            (math.floor(px + ox), math.floor(pz + oz))
            for ox in (-0.3, 0.3)
            for oz in (-0.3, 0.3)
        )
    )


def _is_control_signal(error: Exception) -> bool:
    if not isinstance(error, RuntimeError):
        return False
    # Importing the harness before the skills package finishes initializing creates a cycle.
    from ..harness.control import RequestCancelled, RunTimedOut

    return isinstance(error, (RequestCancelled, RunTimedOut))


def retreat_made_progress(ok: bool, moved: Any, reason: Any) -> bool:
    """Recognize a partial retreat with movement and no reported landing hazard."""
    return ok or (reason is None and isinstance(moved, (int, float)) and math.isfinite(moved) and moved >= 0.35)


def ranged_retreat_positions(
    origin: Sequence[float], entities: Sequence[dict[str, Any]], *, max_distance: float,
    trident_attacker_id: int | None = None,
) -> list[tuple[int | None, Sequence[float]]]:
    positions = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        pos = entity.get("pos")
        name = _clean_block_name(entity.get("name"))
        entity_id = entity.get("id")
        if (
            (name in RANGED_MOBS or (name == "drowned" and trident_attacker_id is not None and entity_id == trident_attacker_id))
            and isinstance(pos, (list, tuple)) and len(pos) >= 3
            and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            and math.dist(origin[:3], pos[:3]) <= max_distance
        ):
            positions.append((entity_id, pos))
    return positions


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
        "boundary_threat",
        "guard_poll_hurt",
    ]
    primary_entity_id: int | None
    primary_type: str | None
    primary_pos: list[float] | None
    distance: float | None
    hurt_source: str | None
    observed_life: tuple[Any, Any]


class CombatOutcome(TypedDict, total=False):
    handled: bool
    mode: str
    summary: str
    actions: list[dict[str, Any]]
    status_after: dict[str, Any]
    requires_replan: bool
    latched_guard: bool


class CombatController(Protocol):
    def on_encounter(self, env: "CombatEnv", trigger: ThreatTrigger) -> CombatOutcome: ...


class _TerrainProbeExpired(Exception):
    """An urgent terrain probe ran out of time or could not verify a block."""


class _CombatFortificationInterrupted(Exception):
    def __init__(self, status: dict[str, Any], *, life_unavailable: bool) -> None:
        self.status = status
        self.life_unavailable = life_unavailable


class CombatEnv:
    """Two-tier spatial combat environment (`CombatEnv` spec v1)."""

    def __init__(
        self,
        bot: Any,
        *,
        skills: Any | None = None,
        geofence: Any | None = None,
        water_escape: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        run_lava_escape: Callable[[Callable[[], dict[str, Any]]], dict[str, Any]] | None = None,
        run_fortification: Callable[[dict[str, Any], Callable[[], dict[str, Any]]], dict[str, Any]] | None = None,
        post_combat_food: bool = True,
        block_cache_ttl: float = 3.0,
        hazard_cache_ttl: float = 3.0,
    ) -> None:
        self.bot = bot
        self.skills = skills
        self.geofence = geofence
        self._water_escape = water_escape
        self._run_lava_escape = run_lava_escape
        self._run_fortification = run_fortification
        self.post_combat_food = post_combat_food
        self.block_cache_ttl = max(0.2, float(block_cache_ttl))
        self.hazard_cache_ttl = max(0.2, float(hazard_cache_ttl))
        self._block_cache: dict[tuple[int, int, int], tuple[str | None, float]] = {}
        self._hazard_cache: tuple[tuple[int, int, int], int, float, list[dict[str, Any]]] | None = None
        self._stalled_lava_shores: dict[tuple[float, ...], tuple[tuple[float, ...], float]] = {}
        self._stalled_lava_swim: tuple[tuple[float, ...], float, str, float] | None = None
        self._stalled_water_shore: tuple[tuple[float, ...], tuple[float, ...], tuple[Any, Any], float] | None = None
        self._nearby_water_bank: tuple[tuple[float, ...], tuple[Any, Any], float] | None = None
        self._last_dry_bank: tuple[tuple[float, ...], tuple[Any, Any], float] | None = None
        self._stalled_lava_notch: dict[tuple[str, tuple[int, int, int], str, str | None], float] = {}

    def fortify(self, status: dict[str, Any], operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if self._run_fortification is None:
            return operation()
        return self._run_fortification(status, operation)

    def surface_from_water(self, status: dict[str, Any]) -> dict[str, Any]:
        if self._water_escape is not None:
            return self._water_escape(status)
        if self.skills is not None and hasattr(self.skills, "escape_water"):
            return self.skills.escape_water()
        try:
            self.bot.call("control", {"jump": True, "forward": True, "ms": 950, "stop_on": ["death"]})
        except ApiError as err:
            return {"ok": False, "code": err.code}
        return {"ok": True}

    def invalidate_block_cache(self) -> None:
        self._block_cache.clear()
        self._hazard_cache = None

    def _get_block_name(
        self, x: int, y: int, z: int, *, strict: bool = False, deadline: float | None = None,
    ) -> str:
        key = (int(x), int(y), int(z))
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise _TerrainProbeExpired
        cached = self._block_cache.get(key)
        if cached is not None and (now - cached[1]) <= self.block_cache_ttl:
            name = cached[0]
        else:
            try:
                transport = {"timeout_s": min(0.35, deadline - now), "allow_reauth": False} if deadline is not None else {}
                res = self.bot.call("get_block", {"pos": [key[0], key[1], key[2]]}, **transport)
                name = _clean_block_name(res) if res.get("name") or res.get("type") else ""
            except Exception as error:
                if _is_control_signal(error):
                    raise
                name = None
            self._block_cache[key] = (name, now)
        if deadline is not None and (not name or time.monotonic() >= deadline):
            raise _TerrainProbeExpired
        if name or strict:
            return name or ""
        return "stone" if name is None else "air"

    def _inventory_counts_and_shield(self, inv: dict[str, Any] | None = None) -> tuple[dict[str, int], bool, str | None]:
        raw = inv if isinstance(inv, dict) else self.bot.call("inventory", method="GET")
        counts: dict[str, int] = {}
        for row in raw.get("items") or []:
            if isinstance(row, dict) and row.get("name"):
                nm = _clean_block_name(row["name"])
                cnt = int(row.get("count") or 0)
                if cnt > 0:
                    counts[nm] = counts.get(nm, 0) + cnt
        offhand = raw.get("offhand") if isinstance(raw.get("offhand"), dict) else {}
        off_name = _clean_block_name(offhand.get("name")) if offhand.get("name") else ""
        has_shield = off_name == "shield" or counts.get("shield", 0) > 0
        held = raw.get("held") if isinstance(raw.get("held"), dict) else {}
        held_name = _clean_block_name(held.get("name")) if held.get("name") else None
        return counts, has_shield, held_name

    # ---- Layer 1: 4 Read-Only Geometric Probes (0 Action Lock) ----

    def has_los(
        self,
        src_pos: Sequence[float] | dict[str, Any],
        dst_pos: Sequence[float] | dict[str, Any] | None = None,
        *,
        src_eye: float = 1.62,
        dst_height: float = 1.0,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Check voxel line-of-sight between `src_pos` and `dst_pos`.

        Also accepts single-arg `has_los(target_pos_or_entity)` by reading `bot.status()['pos']`.
        """
        if dst_pos is None:
            target = src_pos
            st = self.bot.status()
            s_pos = st.get("pos") or [0.0, 64.0, 0.0]
        else:
            s_pos = src_pos
            target = dst_pos

        if isinstance(s_pos, dict):
            s_pos = s_pos.get("pos") or [0.0, 64.0, 0.0]
        if isinstance(target, dict):
            target = target.get("pos") or [0.0, 64.0, 0.0]

        sx, sy, sz = float(s_pos[0]), float(s_pos[1]) + float(src_eye), float(s_pos[2])
        tx, ty, tz = float(target[0]), float(target[1]) + float(dst_height), float(target[2])
        dx, dy, dz = tx - sx, ty - sy, tz - sz
        total_dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if total_dist < 0.35:
            return {"clear": True, "blocker": None, "dist": round(total_dist, 2)}

        steps = min(28, max(2, int(math.ceil(total_dist / 0.45))))
        start_voxel = (math.floor(sx), math.floor(sy), math.floor(sz))
        end_voxel = (math.floor(tx), math.floor(ty), math.floor(tz))
        seen_voxels: set[tuple[int, int, int]] = {start_voxel, end_voxel}
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}

        for i in range(1, steps):
            frac = i / steps
            px, py, pz = sx + dx * frac, sy + dy * frac, sz + dz * frac
            voxel = (math.floor(px), math.floor(py), math.floor(pz))
            if voxel in seen_voxels:
                continue
            seen_voxels.add(voxel)
            bname = self._get_block_name(*voxel, **probe_kwargs)
            if not _is_los_transparent(bname):
                bdist = round(total_dist * frac, 2)
                return {
                    "clear": False,
                    "blocker": {"name": bname, "pos": [voxel[0], voxel[1], voxel[2]], "dist": bdist},
                    "dist": round(total_dist, 2),
                }
        return {"clear": True, "blocker": None, "dist": round(total_dist, 2)}

    def probe_ray(
        self,
        origin: Sequence[float],
        yaw: float,
        *,
        dist: float = 3.0,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Probe walkability, 1-block step-ups, cliffs, traps, fluids, and geofence along `yaw`."""
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
        base_y = math.floor(oy + 0.05)
        ux, uz = yaw_to_unit_xz(yaw)
        max_r = max(0.8, float(dist))

        needs_jump = False
        blocked_by_wall = False
        max_drop = 0
        hazard: str | None = None
        safe_dist = 0.0

        sample_d = 0.9
        cur_y = base_y
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        while sample_d <= max_r + 1e-6:
            nx = ox + ux * sample_d
            nz = oz + uz * sample_d
            if self.geofence is not None and hasattr(self.geofence, "check_point"):
                if self.geofence.check_point(nx, nz, action="probe_ray") is not None:
                    hazard = "geofence"
                    break

            bx, bz = math.floor(nx), math.floor(nz)
            foot_b = self._get_block_name(bx, cur_y, bz, **probe_kwargs)
            head_b = self._get_block_name(bx, cur_y + 1, bz, **probe_kwargs)

            if foot_b in {"lava", "flowing_lava"} or head_b in {"lava", "flowing_lava"}:
                hazard = "lava"
                break
            if foot_b in {"fire", "soul_fire"} or head_b in {"fire", "soul_fire"}:
                hazard = "fire"
                break
            if foot_b in TRAP_BLOCKS or head_b in TRAP_BLOCKS:
                hazard = "trap"
                break
            if foot_b in {"water", "flowing_water"}:
                hazard = "water"
                break

            if not _is_passable(head_b):
                blocked_by_wall = True
                hazard = "wall"
                break

            if not _is_passable(foot_b):
                # Check if this is a clean 1-block step-up (`cur_y + 1` and `cur_y + 2` + overhead at origin clear)
                step_head2 = self._get_block_name(bx, cur_y + 2, bz, **probe_kwargs)
                orig_above = self._get_block_name(math.floor(ox), cur_y + 2, math.floor(oz), **probe_kwargs)
                if _is_solid_ground(foot_b) and _is_passable(step_head2) and _is_passable(orig_above):
                    needs_jump = True
                    cur_y += 1
                    safe_dist = round(sample_d, 2)
                    sample_d = round(sample_d + 0.85, 4)
                    continue
                blocked_by_wall = True
                hazard = "wall"
                break

            # Foot and head at `cur_y` are passable -> measure drop below `(bx, cur_y - 1, bz)`
            drop = 0
            landed_hazard: str | None = None
            for dy_down in range(1, 6):
                below_b = self._get_block_name(bx, cur_y - dy_down, bz, **probe_kwargs)
                if below_b in {"lava", "flowing_lava"}:
                    landed_hazard = "lava"
                    break
                if below_b in {"fire", "soul_fire"}:
                    landed_hazard = "fire"
                    break
                if below_b in TRAP_BLOCKS:
                    landed_hazard = "trap"
                    break
                if below_b in {"water", "flowing_water"}:
                    landed_hazard = "water"
                    break
                if _is_solid_ground(below_b):
                    break
                drop = dy_down

            if landed_hazard is not None:
                hazard = landed_hazard
                break
            max_drop = max(max_drop, drop)
            if drop >= 4:
                hazard = "cliff"
                break
            cur_y -= min(drop, 1)
            safe_dist = round(sample_d, 2)
            sample_d = round(sample_d + 0.85, 4)

        if safe_dist >= max_r - 0.86 and hazard is None and not blocked_by_wall:
            safe_dist = round(max_r, 2)

        is_safe = (hazard is None) and (not blocked_by_wall) and (max_drop < 4) and (safe_dist >= min(1.5, max_r))
        return {
            "safe": bool(is_safe),
            "walkable_flat": bool(is_safe and not needs_jump and max_drop <= 1),
            "needs_jump": bool(needs_jump),
            "blocked_by_wall": bool(blocked_by_wall),
            "max_drop": int(max_drop),
            "hazard": hazard,
            "safe_dist": float(safe_dist),
        }

    def scan_safe_yaws(
        self,
        origin: Sequence[float],
        preferred_yaw: float,
        *,
        dist: float = 3.0,
        angles_deg: Sequence[float] = (0, 35, -35, 70, -70, 110, -110, 150, -150),
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Fan-probe around `preferred_yaw`, ranking safe directions with minimal angular deviation first."""
        candidates: list[dict[str, Any]] = []
        for deg in angles_deg:
            cand_yaw = float(preferred_yaw) + math.radians(float(deg))
            try:
                ray = self.probe_ray(origin, cand_yaw, dist=dist, **({"deadline": deadline} if deadline is not None else {}))
            except _TerrainProbeExpired:
                break
            candidates.append(
                {
                    "yaw": round(cand_yaw, 4),
                    "offset_deg": float(deg),
                    "safe": ray["safe"],
                    "needs_jump": ray["needs_jump"],
                    "safe_dist": ray["safe_dist"],
                    "hazard": ray["hazard"],
                }
            )

        safe_cands = [c for c in candidates if c["safe"]]
        safe_cands.sort(key=lambda c: (abs(c["offset_deg"]), 1 if c["needs_jump"] else 0, -c["safe_dist"]))
        if safe_cands:
            best = safe_cands[0]
            return {
                "best_yaw": best["yaw"],
                "best_needs_jump": best["needs_jump"],
                "cornered": False,
                "candidates": candidates,
            }
        return {
            "best_yaw": None,
            "best_needs_jump": False,
            "cornered": True,
            "candidates": candidates,
        }

    def fluid_above_headroom(self, pos: Sequence[float], target: Sequence[int]) -> str | None:
        """Identify water or lava held back by a block above the bot."""
        x, y, z = pos[:3]
        tx, ty, tz = target[:3]
        feet_y = math.floor(y + 0.05)
        if ty not in {feet_y + 1, feet_y + 2} or (tx, tz) not in _hitbox_columns(x, z):
            return None
        block = self._get_block_name(tx, ty + 1, tz, strict=True)
        if _is_lava_block(block):
            return "lava"
        if block in {"water", "flowing_water"}:
            return "water"
        return None

    def can_remove_footing(self, pos: Sequence[float], target: Sequence[int]) -> bool:
        """Allow ordinary digs and check the landing for a dig through the current floor."""
        x, y, z = pos[:3]
        tx, ty, tz = target[:3]
        if ty != math.floor(y + 0.05) - 1 or (tx, tz) not in _hitbox_columns(x, z):
            return True

        # A sliver of support at the edge of the hitbox can disappear with a small displacement.
        center_x, center_z = math.floor(x), math.floor(z)
        if (center_x, center_z) != (tx, tz):
            floor = self._get_block_name(center_x, ty, center_z, strict=True)
            if floor and floor not in HAZARD_BLOCK_TYPES and _is_solid_ground(floor):
                return True

        for depth in range(1, 4):
            floor = self._get_block_name(tx, ty - depth, tz, strict=True)
            if not floor or floor in HAZARD_BLOCK_TYPES:
                return False
            if _is_solid_ground(floor):
                return True
            if not _is_passable(floor):
                return False
        return False

    def has_safe_footing(self, pos: Sequence[float]) -> bool:
        x, y, z = pos[:3]
        floor_y = math.floor(y + 0.05) - 1
        for bx, bz in _hitbox_columns(x, z):
            floor = self._get_block_name(bx, floor_y, bz, strict=True)
            if not floor or floor in HAZARD_BLOCK_TYPES or not _is_solid_ground(floor):
                return False
        return True

    def probe_retreat(
        self, origin: Sequence[float], yaw: float, dist: float, *, cross_magma: bool = False,
        deadline: float | None = None, allow_step_down: bool = False, short_climb: bool = False,
    ) -> tuple[int, bool] | None:
        """Check support and clearance under the entire hitbox along a short retreat."""
        ox, oy, oz = map(float, origin[:3])
        ux, uz = yaw_to_unit_xz(yaw)
        start_y = math.floor(oy + 0.05)
        walk_y = start_y
        needs_jump = False
        samples = max(1, math.ceil(dist / 0.45))
        crossing_hazards = (
            tuple(name for name in HAZARD_BLOCK_TYPES if name != "magma_block") if cross_magma else HAZARD_BLOCK_TYPES
        )
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        for step in range(1, samples + 1):
            travel = dist * step / samples
            nx, nz = ox + ux * travel, oz + uz * travel
            if not self._point_allowed(nx, nz, action="flee"):
                return None
            bx, bz = math.floor(nx), math.floor(nz)
            foot = self._get_block_name(bx, walk_y, bz, strict=True, **probe_kwargs)
            if not foot or foot in HAZARD_BLOCK_TYPES:
                return None
            if _is_solid_ground(foot):
                walk_y += 1
                needs_jump = True
            elif _is_passable(self._get_block_name(bx, walk_y - 1, bz, strict=True, **probe_kwargs)):
                if short_climb:
                    return None
                walk_y -= 1
            if abs(walk_y - start_y) > 1:
                return None
            floor_hazards = crossing_hazards if step < samples else HAZARD_BLOCK_TYPES
            for hx, hz in _hitbox_columns(nx, nz):
                foot, head, floor = (
                    self._get_block_name(hx, walk_y + dy, hz, strict=True, **probe_kwargs) for dy in (0, 1, -1)
                )
                # Feet can remain on a trailing upper block until the hitbox clears its edge.
                if (
                    allow_step_down and walk_y == start_y - 1 and step < samples
                    and (hx, hz) != (bx, bz) and (hx + 0.5 - nx) * ux + (hz + 0.5 - nz) * uz < 0
                    and foot and foot not in HAZARD_BLOCK_TYPES and _is_solid_ground(foot) and _is_passable(head)
                    and _is_passable(self._get_block_name(hx, walk_y + 2, hz, strict=True, **probe_kwargs))
                ):
                    continue
                if not _is_passable(foot) or not _is_passable(head) or not floor or floor in floor_hazards:
                    return None
                if (hx, hz) != (bx, bz) and _is_passable(floor):
                    if allow_step_down and walk_y < start_y:
                        return None
                    floor = self._get_block_name(hx, walk_y - 2, hz, strict=True, **probe_kwargs)
                if not floor or floor in floor_hazards or not _is_solid_ground(floor):
                    return None
        if short_climb and needs_jump:
            # The jump can reach above the standing headroom before landing on the step.
            for step in range(samples + 1):
                travel = dist * step / samples
                for hx, hz in _hitbox_columns(ox + ux * travel, oz + uz * travel):
                    for height in (start_y + 2, start_y + 3):
                        head = self._get_block_name(hx, height, hz, strict=True, **probe_kwargs)
                        if not _is_passable(head) or head in HAZARD_BLOCK_TYPES:
                            return None
        return walk_y, needs_jump

    def is_on_hot_floor(self, pos: Sequence[float]) -> bool:
        x, y, z = map(float, pos[:3])
        ground_y = math.floor(y + 0.05) - 1
        return any(self._get_block_name(bx, ground_y, bz, strict=True) == "magma_block" for bx, bz in _hitbox_columns(x, z))

    def extinguish_contact_fire(self, pos: Sequence[float], *, sneaking: bool = False) -> dict[str, Any]:
        """Clear a verified flame intersecting the bot without moving or breaking support."""
        x, y, z = pos[:3]
        height = 1.5 if sneaking else 1.8
        columns = _hitbox_columns(x, z)
        flames = {"fire", "soul_fire"}
        self.invalidate_block_cache()
        for by in range(math.floor(y), math.ceil(y + height)):
            for bx, bz in columns:
                if self._get_block_name(bx, by, bz, strict=True) not in flames:
                    continue
                target = [bx, by, bz]
                self.bot.call("dig", {"pos": target, "collect": False, "stop_on": ["death"]},
                              timeout_s=0.6, allow_reauth=False)
                self.invalidate_block_cache()
                after = self._get_block_name(*target, strict=True)
                return {"ok": bool(after) and after not in flames, "block_pos": target, "block_after": after}
        return {"ok": False, "reason": "no_contact_fire"}

    def douse_in_adjacent_water(self, before: dict[str, Any]) -> dict[str, Any]:
        """Reach adjacent shallow water only across verified, supported terrain."""
        ox, oy, oz = before["pos"][:3]
        bx, by, bz = math.floor(ox), math.floor(oy + 0.05), math.floor(oz)
        self.invalidate_block_cache()

        def safe_floor(x: int, y: int, z: int) -> bool:
            block = self._get_block_name(x, y, z, strict=True)
            return bool(block) and block not in HAZARD_BLOCK_TYPES and _is_solid_ground(block)

        for dx, dz in CARDINAL_VECTORS.values():
            wx, wz = bx + dx, bz + dz
            water_y = next((y for y in (by, by - 1) if self._get_block_name(wx, y, wz, strict=True) == "water"), None)
            if water_y is None or not safe_floor(wx, water_y - 1, wz):
                continue
            if not all(_is_passable(self._get_block_name(wx, y, wz, strict=True)) for y in range(water_y + 1, by + 2)):
                continue
            x, z = wx + 0.5, wz + 0.5
            steps = max(1, math.ceil(math.hypot(x - ox, z - oz) / 0.3))
            path = [(ox + (x - ox) * i / steps, oz + (z - oz) * i / steps) for i in range(steps + 1)]
            if not all(self._point_allowed(px, pz, action="fire_douse") for px, pz in path):
                continue
            columns = {(cx, cz) for px, pz in path for cx, cz in _hitbox_columns(px, pz)}
            columns.discard((wx, wz))
            if not all(
                safe_floor(cx, by - 1, cz)
                and all(_is_passable(self._get_block_name(cx, y, cz, strict=True)) for y in (by, by + 1))
                for cx, cz in columns
            ):
                continue
            current = self._safe_status()
            current_pos = current.get("pos")
            if (
                not current_pos or math.dist(current_pos[:3], before["pos"][:3]) > 0.2
                or current.get("ready") is not True or current.get("alive") is False
                or current.get("on_ground") is not True or current.get("in_lava") or not current.get("on_fire")
                or current.get("dimension") != before.get("dimension") or current.get("last_death") != before.get("last_death")
            ):
                return {"ok": False, "reason": "position_or_hazard_changed"}
            try:
                self.bot.call(
                    "move_to", {"x": x, "y": water_y, "z": z, "range": 0.2, "timeout": 650, "stop_on": ["death"]},
                    timeout_s=0.95, allow_reauth=False,
                )
            except (ApiError, httpx.HTTPError) as error:
                if isinstance(error, httpx.HTTPError):
                    self._best_effort_stop(timeout_s=0.25)
            after = self._safe_status()
            ok = bool(
                after.get("ready") is True and after.get("alive") is not False
                and after.get("in_water") and not after.get("in_lava") and after.get("on_fire") is False
                and after.get("last_death") == before.get("last_death") and after.get("dimension") == before.get("dimension")
            )
            return {"ok": ok, "pos_after": after.get("pos"), **({"reason": "douse_unconfirmed"} if not ok else {})}
        return {"ok": False, "reason": "no_safe_shallow_water"}

    def step_off_hot_floor(self, status: dict[str, Any]) -> dict[str, Any]:
        pos = status.get("pos")
        if not pos or status.get("on_ground") is not True:
            return {"ok": False, "reason": "unsafe_landing"}
        ox, oy, oz = map(float, pos[:3])
        self.invalidate_block_cache()
        for steps in (1, 2):
            for dx, dz in CARDINAL_VECTORS.values():
                x = math.floor(ox) + 0.5 + steps * dx
                z = math.floor(oz) + 0.5 + steps * dz
                yaw, _ = aim_yaw_pitch(pos, [x, oy, z])
                route = self.probe_retreat(pos, yaw, math.hypot(x - ox, z - oz), cross_magma=True)
                if route is not None:
                    y, _ = route
                    return self._move_to_hot_floor_exit(status, [x, y, z])
        return {"ok": False, "reason": "no_safe_hot_floor_exit"}

    def _move_to_hot_floor_exit(self, before: dict[str, Any], target: list[float]) -> dict[str, Any]:
        x, y, z = target
        try:
            self.bot.call("move_to", {"x": x, "y": y, "z": z, "range": 0.15, "timeout": 1100, "stop_on": ["death"]})
        except (ApiError, httpx.HTTPError) as error:
            if isinstance(error, httpx.HTTPError):
                self._best_effort_stop()
        after = self._safe_status({"ready": False})
        pos_after = after.get("pos")
        if (
            not pos_after or after.get("ready") is False or after.get("alive") is False
            or after.get("on_ground") is not True or after.get("in_lava") or after.get("in_water")
            or (after.get("last_death") is not None and after["last_death"] != before.get("last_death"))
            or math.hypot(float(pos_after[0]) - x, float(pos_after[2]) - z) > 0.35
        ):
            return {"ok": False, "reason": "unsafe_landing", "pos_after": pos_after}
        self.invalidate_block_cache()
        ground_y = math.floor(float(pos_after[1]) + 0.05) - 1
        floors = [self._get_block_name(bx, ground_y, bz, strict=True) for bx, bz in _hitbox_columns(pos_after[0], pos_after[2])]
        safe = all(name and name not in HAZARD_BLOCK_TYPES and _is_solid_ground(name) for name in floors)
        if not safe:
            return {"ok": False, "reason": "unsafe_landing", "pos_after": pos_after}
        return {"ok": True, "mode": "stepped_off", "pos_after": pos_after}

    def _query_hazards(self, bx: int, by: int, bz: int, radius: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._hazard_cache is not None:
            c_origin, c_rad, c_ts, c_rows = self._hazard_cache
            if (
                c_rad == radius
                and (now - c_ts) <= self.hazard_cache_ttl
                and abs(c_origin[0] - bx) + abs(c_origin[1] - by) + abs(c_origin[2] - bz) <= 2
            ):
                return list(c_rows)
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": max(2, int(radius)), "types": list(HAZARD_BLOCK_TYPES), "limit": 24},
            )
            rows = []
            for b in (found or {}).get("blocks") or []:
                if isinstance(b, dict) and isinstance(b.get("pos"), list):
                    rows.append(
                        {
                            "name": _clean_block_name(b.get("name")),
                            "pos": [int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2])],
                            "dist": round(float(b.get("dist", 0.0)), 2),
                        }
                    )
        except Exception:
            rows = []
        self._hazard_cache = ((bx, by, bz), radius, now, rows)
        return list(rows)

    def arena_snapshot(
        self,
        pos: Sequence[float] | None = None,
        *,
        target_pos: Sequence[float] | None = None,
        hazard_radius: int = 10,
    ) -> dict[str, Any]:
        """Summarize local enclosure, overhead pillar clearance, nearby hazards, and target drop hazard."""
        if pos is None:
            st = self.bot.status()
            pos = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1]) + 0.05), math.floor(float(pos[2]))

        open_dirs: list[str] = []
        solid_wall_dirs: list[str] = []
        for dname, (dx, dz) in CARDINAL_VECTORS.items():
            foot_n = self._get_block_name(bx + dx, by, bz + dz)
            head_n = self._get_block_name(bx + dx, by + 1, bz + dz)
            if _is_passable(foot_n) and _is_passable(head_n):
                open_dirs.append(dname)
            else:
                solid_wall_dirs.append(dname)

        max_pillar = 0
        for h in range(1, 4):
            if _is_passable(self._get_block_name(bx, by + 1 + h, bz)):
                max_pillar = h
            else:
                break

        roof_solid = not _is_passable(self._get_block_name(bx, by + 2, bz))
        opposite_walls = (
            ("north" in solid_wall_dirs and "south" in solid_wall_dirs)
            or ("east" in solid_wall_dirs and "west" in solid_wall_dirs)
        )
        if len(solid_wall_dirs) == 4 and roof_solid:
            enclosure = "sealed_bunker"
        elif roof_solid and (len(solid_wall_dirs) >= 3 or opposite_walls):
            enclosure = "carved_shaft"
        elif roof_solid or max_pillar < 3:
            enclosure = "open_cave"
        else:
            enclosure = "surface"

        foot_here = self._get_block_name(bx, by, bz)
        in_water_or_lava = foot_here in {"water", "flowing_water", "lava", "flowing_lava"}
        hazards = self._query_hazards(bx, by, bz, int(hazard_radius))

        target_over_hazard = False
        if target_pos is not None and len(target_pos) >= 3:
            tx, ty, tz = math.floor(float(target_pos[0])), math.floor(float(target_pos[1])), math.floor(float(target_pos[2]))
            air_streak = 0
            for dy_down in range(1, 6):
                bn = self._get_block_name(tx, ty - dy_down, tz)
                if bn in {"lava", "flowing_lava"}:
                    target_over_hazard = True
                    break
                if _is_solid_ground(bn) or bn in {"water", "flowing_water"}:
                    break
                air_streak += 1
            if air_streak >= 5:
                target_over_hazard = True

        return {
            "enclosure": enclosure,
            "open_dirs": open_dirs,
            "solid_wall_dirs": solid_wall_dirs,
            "max_pillar_height": max_pillar,
            "in_water_or_lava": in_water_or_lava,
            "hazards": hazards,
            "target_over_hazard": target_over_hazard,
        }

    # ---- Layer 2: 3 High-Level Delegated Actions (Acquire Action Lock) ----

    def chase_to_range(
        self,
        target: int | Sequence[float],
        *,
        range_m: float = 2.5,
        timeout_ms: int = 1200,
        stop_on: Sequence[str] = ("death",),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "range": max(0.5, float(range_m)),
            "timeout": max(300, int(timeout_ms)),
            "stop_on": list(stop_on),
        }
        target_coords: list[float] | None = None
        if isinstance(target, int):
            payload["entity_id"] = int(target)
        else:
            tx, tz = float(target[0]), float(target[2])
            if self.geofence is not None and hasattr(self.geofence, "check_point"):
                if self.geofence.check_point(tx, tz, action="chase_to_range") is not None:
                    st = self.bot.status()
                    return {
                        "ok": False,
                        "dist_after": 999.0,
                        "pos_after": st.get("pos"),
                        "reason": "geofence_blocked",
                    }
            payload["x"] = tx
            payload["z"] = tz
            if len(target) >= 3:
                payload["y"] = float(target[1])
                target_coords = [tx, float(target[1]), tz]

        reason = "reached"
        ok = True
        try:
            self.bot.call("move_to", payload)
        except ApiError as err:
            ok = False
            if err.code in {"path_not_found", "no_path"}:
                reason = "path_not_found"
            elif err.code in {"timeout", "action_timeout"}:
                reason = "timeout"
            else:
                reason = err.code

        st_after = self.bot.status()
        pos_after = st_after.get("pos") or [0.0, 64.0, 0.0]
        dist_after = 0.0
        if target_coords is not None:
            dist_after = round(
                math.sqrt(sum((float(pos_after[i]) - target_coords[i]) ** 2 for i in range(3))),
                2,
            )
        return {
            "ok": ok,
            "dist_after": dist_after,
            "pos_after": pos_after,
            "reason": reason,
        }

    def _pick_filler_block(self, counts: dict[str, int]) -> str | None:
        for cand in SAFE_TACTICAL_FILLERS:
            if counts.get(cand, 0) > 0:
                return cand
        for name, cnt in counts.items():
            if cnt > 0 and name.endswith("_planks"):
                return name
        return None

    def dig_cover_pocket(self, skills: Any) -> dict[str, Any]:
        result = skills.dig_cover_pocket()
        self.invalidate_block_cache()
        pocket = result.get("pocket")
        if not result.get("ok") or not pocket:
            return result
        pos = result.get("pos_after")
        if pos and [math.floor(pos[0]), math.floor(pos[1] + 0.05), math.floor(pos[2])] == pocket[0]:
            return result
        return {**result, "ok": False, "requires_replan": True, "reason": "cover_not_entered"}

    def build_tactical(
        self,
        mode: str,
        *,
        direction: str | None = None,
        height: int = 2,
        threat_pos: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """Execute tactical block micro-fortifications (`pillar`, `half_seal`, `full_seal`, `cover_pocket`, `place_water_feet`)."""
        norm_mode = str(mode or "").strip().lower()
        if norm_mode == "pillar_up":
            norm_mode = "pillar"
        elif norm_mode == "seal_tunnel":
            norm_mode = "full_seal"

        st = self.bot.status()
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos0[0])), math.floor(float(pos0[1]) + 0.05), math.floor(float(pos0[2]))
        counts, _, _ = self._inventory_counts_and_shield()

        if norm_mode == "place_water_feet":
            if counts.get("water_bucket", 0) < 1:
                return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "no_water_bucket"}
            try:
                self.bot.call("interact", {"pos": [bx, by - 1, bz], "item": "water_bucket"})
                self.invalidate_block_cache()
                return {"ok": True, "mode": norm_mode, "placed": 1, "pos_after": self.bot.status().get("pos"), "reason": None}
            except ApiError as err:
                return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": err.code}

        if norm_mode == "cover_pocket":
            if self.skills is not None and hasattr(self.skills, "dig_cover_pocket"):
                res = self.dig_cover_pocket(self.skills)
                return {
                    "ok": bool(res.get("ok")),
                    "mode": "cover_pocket",
                    "placed": len([x for x in (res.get("sealed") or []) if x]),
                    "pos_after": self.bot.status().get("pos"),
                    "reason": None if res.get("ok") else str(res.get("reason") or "cover_failed"),
                }
            return {"ok": False, "mode": "cover_pocket", "placed": 0, "pos_after": pos0, "reason": "no_skills"}

        filler = self._pick_filler_block(counts)
        if filler is None:
            return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "no_blocks"}

        if norm_mode == "pillar":
            want_h = max(1, min(3, int(height)))
            snap = self.arena_snapshot(pos0)
            if snap["max_pillar_height"] < want_h:
                return {
                    "ok": False,
                    "mode": "pillar",
                    "placed": 0,
                    "pos_after": pos0,
                    "reason": "low_ceiling",
                }
            if counts.get(filler, 0) < want_h:
                return {"ok": False, "mode": "pillar", "placed": 0, "pos_after": pos0, "reason": "insufficient_blocks"}
            placed = 0
            cur_y = by
            for _ in range(want_h):
                try:
                    self.bot.call("control", {"jump": True, "ms": 220, "wait": False})
                    time.sleep(0.21)
                    self.bot.call("place", {"item": filler, "pos": [bx, cur_y, bz]})
                    placed += 1
                    cur_y += 1
                    time.sleep(0.12)
                except ApiError:
                    break
            self.invalidate_block_cache()
            st_end = self.bot.status()
            return {
                "ok": placed > 0,
                "mode": "pillar",
                "placed": placed,
                "pos_after": st_end.get("pos"),
                "reason": None if placed == want_h else "partial_pillar",
            }

        if norm_mode in {"half_seal", "full_seal"}:
            # A wall in an adjacent column cannot separate entities already sharing this column.
            if (
                norm_mode == "full_seal" and threat_pos is not None and len(threat_pos) >= 3
                and bx <= float(threat_pos[0]) < bx + 1 and bz <= float(threat_pos[2]) < bz + 1
                and abs(float(threat_pos[1]) - float(pos0[1])) < 1.0
            ):
                return {
                    "ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0,
                    "reason": "threat_inside_shaft",
                }
            chosen_dir = str(direction or "").strip().lower()
            if chosen_dir not in CARDINAL_VECTORS:
                if threat_pos is not None and len(threat_pos) >= 3:
                    tdx = float(threat_pos[0]) - float(pos0[0])
                    tdz = float(threat_pos[2]) - float(pos0[2])
                    if abs(tdx) >= abs(tdz):
                        chosen_dir = "east" if tdx >= 0 else "west"
                    else:
                        chosen_dir = "south" if tdz >= 0 else "north"
                else:
                    snap = self.arena_snapshot(pos0)
                    chosen_dir = snap["open_dirs"][0] if snap["open_dirs"] else "north"
            dx, dz = CARDINAL_VECTORS[chosen_dir]
            tx, tz = bx + dx, bz + dz
            cells = [[tx, by + 1, tz]] if norm_mode == "half_seal" else [[tx, by, tz], [tx, by + 1, tz]]
            placed = 0
            for cpos in cells:
                try:
                    if _is_passable(self._get_block_name(cpos[0], cpos[1], cpos[2])):
                        self.bot.call("place", {"item": filler, "pos": cpos})
                        placed += 1
                except ApiError:
                    continue
            self.invalidate_block_cache()
            ok = placed > 0
            reason = None if ok else "place_failed"
            verified = [self._get_block_name(*cell, strict=True) for cell in cells] if norm_mode == "full_seal" else []
            st_end = self.bot.status()
            if norm_mode == "full_seal":
                wall_complete = all(block and _is_solid_ground(block) for block in verified)
                if placed:
                    ok = wall_complete
                    reason = None if ok else "seal_unconfirmed"
                elif wall_complete and threat_pos is not None and len(threat_pos) >= 3:
                    end = st_end.get("pos")
                    if (
                        end and math.dist(end[:3], pos0[:3]) <= 0.2 and st_end.get("on_ground") is True
                        and st_end.get("ready") is not False and st_end.get("alive") is not False
                        and not any(st_end.get(key) for key in ("in_water", "in_lava"))
                        and all(st_end.get(key) == st.get(key) for key in ("dimension", "last_death"))
                    ):
                        from .library import assess_hostile_occlusion, ray_wall_blocker

                        block_cache = {tuple(cell): block for cell, block in zip(cells, verified)}
                        try:
                            blocker = ray_wall_blocker(self.bot, tuple(end[:3]), tuple(threat_pos[:3]), block_cache)
                            if blocker is not None and blocker.get("pos") in cells:
                                threats = assess_hostile_occlusion(
                                    self.bot, end, radius=6, fallback_nearest=st_end.get("nearest_hostile"),
                                    block_cache=block_cache,
                                )
                                ok = threats["complete"] and not threats["unoccluded"]
                        except (ApiError, httpx.HTTPError):
                            pass
                        if ok:
                            reason = "already_sealed"
            return {
                "ok": ok,
                "mode": norm_mode,
                "direction": chosen_dir,
                "placed": placed,
                "pos_after": st_end.get("pos"),
                "reason": reason,
            }

        return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "unknown_mode"}

    def _best_effort_stop(self, *, stop_control: bool = True, timeout_s: float | None = None) -> None:
        """Stop residual movement/pathfinder controls after a timeout or preemption."""
        transport = {"timeout_s": timeout_s, "allow_reauth": False} if timeout_s is not None else {}
        try:
            self.bot.call("stop", {}, **transport)
        except (ApiError, httpx.HTTPError):
            pass
        if stop_control:
            try:
                self.bot.call("control", {"wait": False}, **transport)
            except (ApiError, httpx.HTTPError):
                pass

    def _safe_status(self, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch live bot status safely without letting transient ReadTimeout abort active reflexes."""
        try:
            if callable(getattr(self.bot, "status", None)):
                res = self.bot.status()
            else:
                res = self.bot.call("status", method="GET")
            return res if isinstance(res, dict) else dict(fallback or {})
        except (ApiError, httpx.HTTPError):
            return dict(fallback or {})

    def _is_near_or_over_lava(self, st_now: dict[str, Any]) -> bool:
        """Return True when the bot's hitbox or immediate 1-block rim touches lava."""
        if bool(st_now.get("in_lava")):
            return True
        pos = st_now.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return False
        try:
            px, py_f, pz = float(pos[0]), float(pos[1]), float(pos[2])
            bx, by, bz = math.floor(px), math.floor(py_f + 0.05), math.floor(pz)
        except (TypeError, ValueError):
            return False
        for hx, hz in _hitbox_columns(px, pz):
            for cy in (by, by - 1):
                if _is_lava_block(self._get_block_name(hx, cy, hz)):
                    return True
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for cy in (by, by - 1):
                    if _is_lava_block(self._get_block_name(bx + dx, cy, bz + dz)):
                        return True
        return False

    def _is_airborne_above_lava(self, st_now: dict[str, Any], *, start_pos: Sequence[float]) -> bool:
        """Return True when in_lava=False only because a jump lifted the bot mid-air or grazing lava."""
        if bool(st_now.get("in_lava")) or st_now.get("alive") is False or st_now.get("ready") is False:
            return False
        if st_now.get("on_ground") is not False:
            return False
        p_now = st_now.get("pos") or start_pos
        try:
            px, py_f, pz = float(p_now[0]), float(p_now[1]), float(p_now[2])
            if math.hypot(px - float(start_pos[0]), pz - float(start_pos[2])) > 64.0:
                return False
            by = math.floor(py_f + 0.05)
        except (TypeError, ValueError, IndexError):
            return False
        for hx, hz in _hitbox_columns(px, pz):
            if _is_lava_block(self._get_block_name(hx, by, hz)):
                return True
            b1 = self._get_block_name(hx, by - 1, hz)
            if _is_lava_block(b1):
                return True
            if _is_passable(b1) and _is_lava_block(self._get_block_name(hx, by - 2, hz)):
                return True
        return False

    def escape_lava(self, *, force_rim_escape: bool = False) -> dict[str, Any]:
        """Deterministically escape lava using water douse (overworld), safe-yaw jump sprint, wall-notch/pillar climb, and live `in_lava` verification."""
        if self._run_lava_escape is not None:
            return self._run_lava_escape(lambda: self._escape_lava(force_rim_escape=force_rim_escape))
        return self._escape_lava(force_rim_escape=force_rim_escape)

    def _escape_lava(self, *, force_rim_escape: bool) -> dict[str, Any]:
        def _landing_unconfirmed(status: dict[str, Any]) -> bool:
            return status.get("on_ground") is False and not status.get("in_water")

        try:
            st = self.bot.status()
        except (ApiError, httpx.HTTPError):
            self._best_effort_stop()
            return {
                "ok": False,
                "in_lava": True,
                "timeout": True,
                "pos_after": None,
                "actions": ["lava_status_timeout"],
            }
        rim_lava_active = False
        if not bool(st.get("in_lava")):
            if st.get("alive") is not False and st.get("ready") is not False:
                self.invalidate_block_cache()
                pos_chk = st.get("pos") or [0.0, 64.0, 0.0]
                rim_lava_active = self._is_airborne_above_lava(st, start_pos=pos_chk) or (
                    force_rim_escape and self._is_near_or_over_lava(st)
                )
            if not rim_lava_active:
                if not _landing_unconfirmed(st):
                    self._stalled_lava_shores.clear()
                    self._stalled_lava_swim = None
                unconfirmed = _landing_unconfirmed(st)
                return {
                    "ok": not unconfirmed,
                    "in_lava": False,
                    "already_dry": True,
                    "pos_after": st.get("pos"),
                    "actions": [],
                    **({"reason": "lava_landing_unconfirmed"} if unconfirmed else {}),
                }

        actions: list[str] = []
        notch_dig_failures: list[dict[str, Any]] = []
        lava_deadline = time.monotonic() + 5.0
        # 1. Surface before spending time on a bucket or scanning for a shore.
        self.invalidate_block_cache()
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]
        start_x, start_z = float(pos0[0]), float(pos0[2])
        dim0 = str(st.get("dimension") or "").lower()
        last_death0 = st.get("last_death")

        def _did_respawn_during_lava(st_now: dict[str, Any]) -> bool:
            pos_now = st_now.get("pos") or pos0
            moved = math.hypot(float(pos_now[0]) - start_x, float(pos_now[2]) - start_z)
            dim_now = str(st_now.get("dimension") or "").lower()
            ld_now = st_now.get("last_death")
            return bool(
                st_now.get("alive") is False
                or st_now.get("ready") is False
                or moved > 64.0
                or (dim0 and dim_now and dim_now != dim0)
                or (ld_now is not None and ld_now != last_death0)
            )

        def _should_exit_lava(st_now: dict[str, Any]) -> bool:
            if _did_respawn_during_lava(st_now):
                return True
            if (
                st_now.get("in_lava") or _landing_unconfirmed(st_now)
                or self._is_airborne_above_lava(st_now, start_pos=pos0)
            ):
                return False
            if rim_lava_active:
                p_chk = st_now.get("pos") or pos0
                moved_r = math.hypot(float(p_chk[0]) - float(pos0[0]), float(p_chk[2]) - float(pos0[2]))
                if moved_r < 0.45 and self._is_near_or_over_lava(st_now):
                    return False
            return True

        def _lava_result(st_now: dict[str, Any]) -> dict[str, Any]:
            still_in_lava = bool(st_now.get("in_lava"))
            pos_now = st_now.get("pos") or pos0
            respawned = _did_respawn_during_lava(st_now)
            if respawned or (not still_in_lava and not _landing_unconfirmed(st_now)):
                self._stalled_lava_shores.clear()
                self._stalled_lava_swim = None
            airborne_over_lava = (
                (not respawned)
                and (not still_in_lava)
                and self._is_airborne_above_lava(st_now, start_pos=pos0)
            )
            unconfirmed = not respawned and not still_in_lava and _landing_unconfirmed(st_now)
            return {
                "ok": not (respawned or still_in_lava or unconfirmed or airborne_over_lava),
                "in_lava": still_in_lava,
                "pos_after": pos_now,
                "actions": actions,
                **({"notch_dig_failures": notch_dig_failures} if notch_dig_failures else {}),
                **({"respawned": True} if respawned else {}),
                **({"airborne_above_lava": True} if airborne_over_lava else {}),
                **({"reason": "lava_landing_unconfirmed"} if unconfirmed else {}),
            }

        def jump_in_lava(ms: int, *, advance: bool) -> None:
            self.bot.call(
                "control", {"forward": advance, "jump": True, "sprint": advance, "ms": ms, "stop_on": ["death"]},
                timeout_s=ms / 1000.0 + 0.6, allow_reauth=False,
            )

        tried_recent_bank = False
        for _ in range(3):
            px, py, pz = map(float, pos0[:3])
            head = self._get_block_name(math.floor(px), math.floor(py) + 1, math.floor(pz))
            if not _is_lava_block(head) or time.monotonic() >= lava_deadline:
                break
            hp_now = st.get("health")
            if (
                not tried_recent_bank and self._last_dry_bank is not None
                and st.get("in_lava") and isinstance(hp_now, (int, float)) and 0 < hp_now <= 12.0
            ):
                tried_recent_bank = True
                bank, bank_life, bank_at = self._last_dry_bank
                distance = math.hypot(bank[0] - px, bank[2] - pz)
                if (
                    bank_life == (st.get("dimension"), st.get("last_death")) and time.monotonic() - bank_at < 30.0
                    and 0.6 <= distance <= 3.0 and 0 <= bank[1] - py <= 1.5
                    and self._point_allowed(bank[0], bank[2], action="escape_lava")
                    and self._dry_bank_is_walkable(bank) and time.monotonic() + 1.25 < lava_deadline
                ):
                    try:
                        yaw, _ = aim_yaw_pitch(pos0, bank)
                        self.bot.call("look", {"yaw": yaw, "pitch": 0.0}, timeout_s=0.35, allow_reauth=False)
                        jump_in_lava(950, advance=True)
                        actions.append("lava_swim_to_recent_dry_bank")
                    except (ApiError, httpx.HTTPError) as err:
                        if isinstance(err, httpx.HTTPError):
                            self._best_effort_stop(timeout_s=0.5)
                    self.invalidate_block_cache()
                    st = self._safe_status(st)
                    if _should_exit_lava(st):
                        return _lava_result(st)
                    pos0 = st.get("pos") or pos0
                    continue
            try:
                # The current yaw can still face the attacker that knocked us in.
                # Preserve the horizontal position until the shore probe picks a heading.
                jump_in_lava(950, advance=False)
                actions.append("lava_surface_swim")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    actions.append("lava_surface_swim_timeout")
                    self._best_effort_stop(timeout_s=0.5)
            self.invalidate_block_cache()
            st = self._safe_status(st)
            if _should_exit_lava(st):
                return _lava_result(st)
            pos0 = st.get("pos") or pos0
            if float(pos0[1]) - py < 0.1:
                break

        try:
            counts, _, held_name = self._inventory_counts_and_shield()
        except (ApiError, httpx.HTTPError):
            counts, held_name = {}, None
        if counts.get("water_bucket", 0) > 0 and "nether" not in dim0 and time.monotonic() < lava_deadline:
            px, py, pz = map(float, pos0[:3])
            head = self._get_block_name(math.floor(px), math.floor(py) + 1, math.floor(pz), strict=True)
            if head and not _is_lava_block(head):
                try:
                    douse = self.build_tactical("place_water_feet")
                    actions.append("place_water_feet" if douse.get("ok") else "place_water_feet_failed")
                except (ApiError, httpx.HTTPError):
                    actions.append("place_water_feet_failed")
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                try:
                    self.bot.call("use_item", {"item": "water_bucket"})
                    actions.append("use_water_bucket")
                except (ApiError, httpx.HTTPError):
                    pass
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                pos0 = st.get("pos") or pos0
                self.invalidate_block_cache()

        # 2. Try scanning safe non-lava exits (including 1-block step-ups, plus wide shore search) and jump-sprinting out
        terrain_deadline = min(lava_deadline - 2.0, time.monotonic() + 1.5)
        scan = self.scan_safe_yaws(
            pos0,
            float(st.get("yaw") or 0.0),
            dist=2.8,
            angles_deg=(0, 45, -45, 90, -90, 135, -135, 180),
            deadline=terrain_deadline,
        )
        tried_yaws: list[float] = []
        if scan.get("best_yaw") is not None:
            tried_yaws.append(float(scan["best_yaw"]))
        for c in scan.get("candidates") or []:
            if c.get("hazard") not in {"lava", "geofence"} and float(c.get("yaw", 0.0)) not in tried_yaws:
                if c.get("safe") or c.get("hazard") in {None, "water"}:
                    tried_yaws.append(float(c["yaw"]))

        py = math.floor(float(pos0[1]) + 0.05)
        lava_shore_target: list[float] | None = None
        filler = self._pick_filler_block(counts)
        blind_swim = False
        defer_blind_swim = False
        now = time.monotonic()
        self._stalled_lava_shores = {
            bank: (rim, at) for bank, (rim, at) in self._stalled_lava_shores.items() if now - at < 15.0
        }

        def stalled_shores_near(pos: Sequence[float]) -> set[tuple[float, ...]]:
            return {
                bank for bank, (rim, at) in self._stalled_lava_shores.items()
                if time.monotonic() - at < 15.0 and abs(pos[1] - rim[1]) <= 2.0
                and math.hypot(pos[0] - rim[0], pos[2] - rim[2]) <= 1.25
            }

        if not tried_yaws and time.monotonic() < lava_deadline:
            # Wide lava lake: filter shore types by dimension to keep find_blocks fast (<1s).
            shore_blocks = NETHER_SHORE_BLOCKS if "nether" in dim0 else SHORE_LAND_BLOCKS
            excluded_shores = stalled_shores_near(pos0)
            try:
                lava_shore_target = self._find_walkable_shore_target(
                    pos0,
                    block_types=shore_blocks,
                    max_radius=18,
                    min_y=py - 1,
                    max_y=py + 2,
                    max_candidates=6,
                    deadline=terrain_deadline,
                    excluded_shores=excluded_shores,
                )
            except _TerrainProbeExpired:
                pass
            if lava_shore_target is not None:
                tried_yaws = [aim_yaw_pitch(pos0, lava_shore_target)[0]]
            else:
                if excluded_shores:
                    actions.append("lava_shore_path_stalled")
                base_yaw = float(st.get("yaw") or 0.0)
                best_shore_yaw: float | None = None
                for probe_dist in (4.5, 9.0):
                    if time.monotonic() >= terrain_deadline:
                        break
                    for deg in (0, 90, -90, 180, 45, -45, 135, -135):
                        if time.monotonic() >= terrain_deadline:
                            break
                        cand_yaw = base_yaw + math.radians(deg)
                        ux, uz = yaw_to_unit_xz(cand_yaw)
                        px = math.floor(float(pos0[0]) + ux * probe_dist)
                        pz = math.floor(float(pos0[2]) + uz * probe_dist)
                        for y_chk in (py, py + 1):
                            try:
                                floor = self._get_block_name(px, y_chk, pz, deadline=terrain_deadline)
                                if _is_solid_ground(floor) and _is_passable(self._get_block_name(px, y_chk + 1, pz, deadline=terrain_deadline)):
                                    best_shore_yaw = cand_yaw
                                    break
                            except _TerrainProbeExpired:
                                break
                        if best_shore_yaw is not None:
                            break
                    if best_shore_yaw is not None:
                        break
                blind_swim = best_shore_yaw is None
                tried_yaws = [base_yaw if blind_swim else best_shore_yaw]
                stalled_swim = self._stalled_lava_swim
                if blind_swim and stalled_swim is not None:
                    stalled_pos, stalled_yaw, stalled_dim, stalled_at = stalled_swim
                    if (
                        stalled_dim == dim0 and time.monotonic() - stalled_at < 15.0
                        and abs(pos0[1] - stalled_pos[1]) <= 2.0
                        and math.hypot(pos0[0] - stalled_pos[0], pos0[2] - stalled_pos[2]) <= 1.25
                        and abs(math.remainder(base_yaw - stalled_yaw, math.tau)) <= math.radians(15)
                    ):
                        tried_yaws = []
                        for offset in (math.pi / 2, -math.pi / 2, math.pi):
                            candidate = base_yaw + offset
                            ux, uz = yaw_to_unit_xz(candidate)
                            if self._point_allowed(pos0[0] + ux * 2.8, pos0[2] + uz * 2.8, action="escape_lava"):
                                tried_yaws.append(candidate)
                                break
                        actions.append("lava_swim_heading_stalled")
                health_after_ascent = st.get("health")
                # At critical health, reserve time for usable blocks before following an unverified yaw.
                defer_blind_swim = (
                    blind_swim and bool(tried_yaws) and filler is not None
                    and isinstance(health_after_ascent, (int, float)) and health_after_ascent < 10.0
                )

        swim_ms = 1400 if (lava_shore_target is not None or len(tried_yaws) == 1) else 950

        def _run_jump_sprint(cyaw: float) -> bool:
            nonlocal st
            swim_start = st.get("pos") or pos0
            completed = False
            try:
                self.bot.call("look", {"yaw": cyaw, "pitch": 0.0}, timeout_s=0.6, allow_reauth=False)
                jump_in_lava(swim_ms, advance=True)
                completed = True
                actions.append("lava_jump_sprint")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    actions.append("lava_jump_sprint_timeout")
                    self._best_effort_stop(timeout_s=0.5)
            self.invalidate_block_cache()
            observed = self._safe_status()
            if observed:
                st = observed
                if blind_swim and completed and st.get("in_lava") and st.get("pos") and not _did_respawn_during_lava(st):
                    end = st["pos"]
                    advanced = math.hypot(end[0] - swim_start[0], end[2] - swim_start[2]) >= 0.35
                    self._stalled_lava_swim = None if advanced else (tuple(end[:3]), cyaw, dim0, time.monotonic())
            return _should_exit_lava(st)

        if tried_yaws and not defer_blind_swim and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[0]):
                return _lava_result(st)
            landing = st.get("pos")
            if (
                lava_shore_target is not None and landing and not st.get("in_lava") and st.get("on_ground") is False
                and math.hypot(landing[0] - lava_shore_target[0], landing[2] - lava_shore_target[2]) <= 0.35
                and -0.05 <= landing[1] - lava_shore_target[1] <= 0.9
                and lava_deadline - time.monotonic() >= 0.35
            ):
                # The swim pulse can end above the bank just before the next physics tick lands.
                time.sleep(0.1)
                try:
                    st = self.bot.call("status", method="GET", timeout_s=0.25, allow_reauth=False)
                    actions.append("lava_shore_landing_checked")
                except (ApiError, httpx.HTTPError):
                    pass
                if _should_exit_lava(st):
                    return _lava_result(st)

        # 2b. Short pathfinder step onto close shore (`<=6.5m`) guarded against pathfinder ReadTimeout stalls.
        if lava_shore_target is not None and time.monotonic() + 1.5 < lava_deadline:
            swim_pos = st.get("pos") or pos0
            shore_d = math.hypot(lava_shore_target[0] - float(swim_pos[0]), lava_shore_target[2] - float(swim_pos[2]))
            bank = tuple(lava_shore_target)
            if bank in stalled_shores_near(swim_pos):
                actions.append("lava_shore_path_stalled")
            elif shore_d <= 6.5:
                # Keep the attempted bank if the recovery deadline interrupts the status recheck.
                self._stalled_lava_shores[bank] = (tuple(swim_pos[:3]), time.monotonic())
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": lava_shore_target[0],
                            "y": lava_shore_target[1],
                            "z": lava_shore_target[2],
                            # Keep the entire hitbox on the confirmed bank tile.
                            "range": 0.15,
                            "timeout": 1200,
                            "stop_on": ["death"],
                        },
                        timeout_s=2.0,
                        allow_reauth=False,
                    )
                    actions.append("lava_move_to_shore")
                except (ApiError, httpx.HTTPError) as err:
                    actions.append("lava_move_to_shore_timeout" if isinstance(err, httpx.HTTPError) else "lava_move_to_shore_err")
                    self._best_effort_stop(timeout_s=0.5)
                self.invalidate_block_cache()
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                end = st.get("pos") or swim_pos
                remaining = math.hypot(bank[0] - end[0], bank[2] - end[2])
                if remaining >= shore_d - 0.75:
                    self._stalled_lava_shores[bank] = (tuple(end[:3]), time.monotonic())
                else:
                    self._stalled_lava_shores.pop(bank, None)

        # 3. Boxed-in pit or deep lava: try bridging beneath feet or against adjacent solid bank/floor
        if filler is not None and time.monotonic() < lava_deadline + 0.8:
            bridge_deadline = lava_deadline + 0.8
            checked_bridge_landing = False
            p_cur = st.get("pos") or pos0
            bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
            place_targets = [[bx, by, bz], [bx, by - 1, bz]]

            def bridge_call(
                action: str, body: dict[str, Any] | None, *, timeout_s: float, **kwargs: Any,
            ) -> dict[str, Any]:
                remaining = bridge_deadline - time.monotonic()
                if remaining <= 0:
                    raise _TerrainProbeExpired
                return self.bot.call(action, body, timeout_s=min(timeout_s, remaining), allow_reauth=False, **kwargs)

            probe_until = min(bridge_deadline - 0.8, time.monotonic() + 0.6)
            try:
                if st.get("on_ground") is False and _is_passable(self._get_block_name(bx, by - 1, bz, deadline=probe_until)):
                    place_targets.append([bx, by - 2, bz])
                for dx, dz in CARDINAL_VECTORS.values():
                    for cy in (by, by - 1):
                        tx, tz = bx + dx, bz + dz
                        if not self._point_allowed(tx + 0.5, tz + 0.5, action="escape_lava"):
                            continue
                        cur_bn = self._get_block_name(tx, cy, tz, deadline=probe_until)
                        if cur_bn not in {"lava", "flowing_lava"} and not _is_passable(cur_bn):
                            continue
                        has_support = any(
                            _is_solid_ground(self._get_block_name(tx + ox, cy + oy, tz + oz, deadline=probe_until))
                            for ox, oy, oz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, -1, 0), (0, 1, 0))
                        )
                        if has_support and [tx, cy, tz] not in place_targets:
                            place_targets.append([tx, cy, tz])
                            if len(place_targets) >= 5:
                                break
                    if len(place_targets) >= 5:
                        break
            except _TerrainProbeExpired:
                actions.append("lava_bridge_probe_incomplete")
                try:
                    st = bridge_call("status", None, timeout_s=0.25, method="GET")
                except (_TerrainProbeExpired, ApiError, httpx.HTTPError):
                    pass
                if _should_exit_lava(st):
                    return _lava_result(st)

            for place_pos in place_targets:
                if time.monotonic() >= bridge_deadline:
                    break
                try:
                    bridge_call("control", {"jump": True, "ms": 220, "wait": False}, timeout_s=0.4)
                    time.sleep(0.18)
                    bridge_call("place", {"item": filler, "pos": place_pos}, timeout_s=0.7)
                    actions.append(f"lava_place_{filler}")
                    self.invalidate_block_cache()
                    side_step = place_pos[0] != bx or place_pos[2] != bz
                    if side_step:
                        try:
                            byaw, _ = aim_yaw_pitch(p_cur, [place_pos[0] + 0.5, place_pos[1] + 1.0, place_pos[2] + 0.5])
                            bridge_call("look", {"yaw": byaw, "pitch": 0.0}, timeout_s=0.35)
                            bridge_call(
                                "control", {"forward": True, "jump": True, "sprint": True, "ms": 650, "stop_on": ["death"]},
                                timeout_s=1.0,
                            )
                            actions.append("lava_step_bridge")
                        except (ApiError, httpx.HTTPError) as err_step:
                            if isinstance(err_step, httpx.HTTPError):
                                self._best_effort_stop(timeout_s=0.5)
                    st = self._safe_status(st)
                    if (
                        not checked_bridge_landing and not st.get("in_lava") and _landing_unconfirmed(st)
                        and not _did_respawn_during_lava(st) and bridge_deadline - time.monotonic() >= 0.2
                    ):
                        # Allow a jump that cleared the lava to settle before trying another placement.
                        checked_bridge_landing = True
                        landing_deadline = min(bridge_deadline, time.monotonic() + 0.35)
                        for sample in range(2):
                            if sample:
                                # The bot can land with its center over the bridge and its hitbox straddling the edge.
                                p_landing = st.get("pos")
                                if (
                                    not side_step or st.get("in_lava") or not _landing_unconfirmed(st)
                                    or _did_respawn_during_lava(st) or not p_landing
                                    or p_landing[1] < place_pos[1] + 0.95
                                    or (math.floor(p_landing[0]), math.floor(p_landing[2])) != (place_pos[0], place_pos[2])
                                ):
                                    break
                            if landing_deadline - time.monotonic() <= 0.1:
                                break
                            time.sleep(0.1)
                            remaining = landing_deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            try:
                                st = self.bot.call("status", method="GET", timeout_s=min(0.25, remaining), allow_reauth=False)
                            except (ApiError, httpx.HTTPError):
                                break
                    if _should_exit_lava(st):
                        if st.get("on_ground") is True and not st.get("in_water") and not _did_respawn_during_lava(st):
                            # Residual movement can carry the bot off either kind of one-block landing.
                            try:
                                bridge_call("posture", {"sneak": True, "jump": False, "shield": bool(st.get("blocking"))}, timeout_s=0.35)
                                actions.append("lava_bridge_sneak")
                                st = bridge_call("status", None, timeout_s=0.35, method="GET")
                            except (_TerrainProbeExpired, ApiError, httpx.HTTPError):
                                return {**_lava_result(st), "ok": False, "reason": "lava_bridge_landing_unconfirmed"}
                            if not _did_respawn_during_lava(st) and st.get("on_ground") is not True:
                                return {**_lava_result(st), "ok": False, "reason": "lava_landing_unconfirmed"}
                        return _lava_result(st)
                except _TerrainProbeExpired:
                    break
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.5)
                    continue

        if defer_blind_swim and st.get("in_lava") and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[0]):
                return _lava_result(st)
        if len(tried_yaws) > 1 and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[1]):
                return _lava_result(st)

        # 4. Last resort: carve a 2-high notch out of an adjacent solid bank and leap onto it
        notch_deadline = lava_deadline + 0.8
        now = time.monotonic()
        self._stalled_lava_notch = {key: until for key, until in self._stalled_lava_notch.items() if now < until}
        for dname, (dx, dz) in CARDINAL_VECTORS.items():
            if time.monotonic() >= lava_deadline:
                break
            p_cur = st.get("pos") or pos0
            bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
            ex, ez = bx + dx, bz + dz
            if not self._point_allowed(ex + 0.5, ez + 0.5, action="escape_lava"):
                continue
            foot = self._get_block_name(ex, by, ez, strict=True)
            if not foot or foot in HAZARD_BLOCK_TYPES:
                continue
            floor_y = by if _is_solid_ground(foot) else by - 1
            floor = self._get_block_name(ex, floor_y, ez, strict=True)
            if not floor or not _is_solid_ground(floor) or floor in HAZARD_BLOCK_TYPES:
                continue
            clearance = (floor_y + 1, floor_y + 2)
            blocks = [self._get_block_name(ex, cy, ez, strict=True) for cy in clearance]
            if any(not block or block in HAZARD_BLOCK_TYPES or block == "bedrock" for block in blocks):
                continue
            if any(block in {"obsidian", "crying_obsidian"} for block in blocks):
                # Neither block can clear within the one-second emergency dig budget.
                actions.append("lava_notch_hard_block")
                continue
            dig_keys = [
                (dim0, (ex, cy, ez), block, held_name)
                for cy, block in zip(clearance, blocks)
                if not _is_passable(block)
            ]
            now = time.monotonic()
            if any(self._stalled_lava_notch.get(key, now) > now for key in dig_keys):
                actions.append("lava_notch_dig_deferred")
                continue
            for key in dig_keys:
                # Reserve time to reach the notch after opening it.
                dig_budget = min(1.0, notch_deadline - time.monotonic() - 1.5)
                if dig_budget < 0.25:
                    break
                try:
                    self.bot.call(
                        "dig", {"pos": list(key[1]), "collect": False, "stop_on": ["death"]},
                        timeout_s=dig_budget, allow_reauth=False,
                    )
                except (ApiError, httpx.HTTPError) as err:
                    error = err.code if isinstance(err, ApiError) else type(err).__name__
                    notch_dig_failures.append({
                        "pos": list(key[1]), "block": key[2], "held": key[3], "error": error,
                    })
                    timed_out = isinstance(err, httpx.TimeoutException) or error in {"timeout", "action_timeout"}
                    if timed_out:
                        self._stalled_lava_notch[key] = time.monotonic() + 15.0
                        actions.append("lava_notch_dig_timeout")
                    if timed_out or isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.25)
                    break
            if time.monotonic() + 1.25 >= notch_deadline:
                break
            self.invalidate_block_cache()
            floor = self._get_block_name(ex, floor_y, ez, strict=True)
            if (
                floor and _is_solid_ground(floor) and floor not in HAZARD_BLOCK_TYPES
                and all(_is_passable(self._get_block_name(ex, cy, ez, strict=True)) for cy in clearance)
            ):
                yaw_out = math.atan2(-(ex + 0.5 - float(p_cur[0])), -(ez + 0.5 - float(p_cur[2])))
                try:
                    self.bot.call("look", {"yaw": yaw_out, "pitch": 0.0}, timeout_s=0.3, allow_reauth=False)
                    self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "sprint": True, "ms": 1000, "stop_on": ["death"]},
                        timeout_s=1.3, allow_reauth=False,
                    )
                    actions.append(f"lava_notch_{dname}")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.25)
                self.invalidate_block_cache()
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    break

        st_end = self._safe_status(st)
        still_in_lava = bool(st_end.get("in_lava"))
        if still_in_lava and st_end.get("alive") is not False and st_end.get("ready") is not False:
            try:
                self.bot.call("posture", {"jump": True})
            except (ApiError, httpx.HTTPError):
                pass
        return _lava_result(st_end)

    def _blocks_shot(
        self, pos: Sequence[float], threat_pos: Sequence[float], *, deadline: float | None = None,
    ) -> bool:
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        for height in (0.75, 1.6):
            blocker = self.has_los(threat_pos, pos, dst_height=height, **probe_kwargs).get("blocker")
            if blocker is None:
                return False
            name = self._get_block_name(*blocker["pos"], strict=True, **probe_kwargs)
            if not name or _is_los_transparent(name):
                return False
        return True

    def _blocks_shot_across_hitbox(
        self, pos: Sequence[float], threat_pos: Sequence[float], *, deadline: float | None = None,
    ) -> bool:
        dx, dz = threat_pos[0] - pos[0], threat_pos[2] - pos[2]
        horizontal = math.hypot(dx, dz)
        samples = [pos]
        if horizontal > 0.05:
            # Check both sides of the player's hitbox where it can extend around a corner.
            side_x, side_z = -0.3 * dz / horizontal, 0.3 * dx / horizontal
            samples.extend((
                [pos[0] + side_x, pos[1], pos[2] + side_z],
                [pos[0] - side_x, pos[1], pos[2] - side_z],
            ))
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        return all(self._blocks_shot(sample, threat_pos, **probe_kwargs) for sample in samples)

    def arrow_cover_material(self, counts: dict[str, int]) -> str | None:
        # Prefer a stack that can finish a two-block wall before relying on an existing partial column.
        for minimum in (2, 1):
            available = {name: count for name, count in counts.items() if count >= minimum}
            material = self._pick_filler_block(available) or next((name for name in available if name.endswith("_log")), None)
            if material is not None:
                return material
        return None

    def place_arrow_cover(
        self, before: dict[str, Any], shooter: Sequence[float], counts: dict[str, int],
    ) -> dict[str, Any]:
        """Build close cover on a supported column without leaving the current footing."""
        origin = before["pos"]
        filler = self.arrow_cover_material(counts)
        failed = {"ok": False, "mode": "placed_cover", "placed": 0}
        if filler is None or math.hypot(shooter[0] - origin[0], shooter[2] - origin[2]) <= 3.5:
            return failed

        def on_same_footing(status: dict[str, Any]) -> bool:
            return bool(
                status.get("pos") and math.dist(status["pos"][:3], origin[:3]) <= 0.2
                and status.get("ready") is True and status.get("alive") is True and status.get("on_ground") is True
                and not any(status.get(key) for key in ("busy", "in_water", "in_lava", "on_fire"))
                and all(status.get(key) == before.get(key) for key in ("last_death", "dimension"))
            )

        yaw, _ = aim_yaw_pitch(origin, shooter)
        dx, dz = yaw_to_unit_xz(yaw)
        y = math.floor(origin[1] + 0.05)
        occupied = _hitbox_columns(origin[0], origin[2])
        self.invalidate_block_cache()
        columns = dict.fromkeys(
            (math.floor(origin[0] + dx * distance), math.floor(origin[2] + dz * distance))
            for distance in (0.65, 1.1, 1.55, 2.0)
        )
        for x, z in columns:
            if (x, z) in occupied or not self._point_allowed(x + 0.5, z + 0.5, action="place_arrow_cover"):
                continue
            # A supported column within reach keeps the place endpoint from pathfinding.
            floor = self._get_block_name(x, y - 1, z, strict=True)
            if not floor or floor in HAZARD_BLOCK_TYPES or not _is_solid_ground(floor):
                continue
            cells = [(x, y, z), (x, y + 1, z)]
            blocks = [self._get_block_name(*cell, strict=True) for cell in cells]
            if any(not block or block in HAZARD_BLOCK_TYPES or (
                block not in AIR_LIKE_BLOCKS and _is_los_transparent(block)
            ) for block in blocks):
                continue
            to_place = [cell for cell, block in zip(cells, blocks) if block in AIR_LIKE_BLOCKS]
            if to_place and len(to_place) <= counts[filler]:
                break
        else:
            return failed

        placed = 0
        for cell in to_place:
            current = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
            if not on_same_footing(current):
                break
            try:
                self.bot.call("place", {"item": filler, "pos": list(cell)}, timeout_s=0.65, allow_reauth=False)
            except ApiError:
                break
            self.invalidate_block_cache()
            block = self._get_block_name(*cell, strict=True)
            if block != filler:
                break
            placed += 1

        after = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
        confirmed = placed > 0 and on_same_footing(after) and self._blocks_shot(after["pos"], shooter)
        return {"ok": confirmed, "mode": "placed_cover", "placed": placed, "pos_after": after.get("pos")}

    def seek_natural_cover(
        self, threat_pos: Sequence[float], *, other_shooters: Sequence[Sequence[float]] = (), allow_stationary: bool = True,
        verify_live_shooters: bool = False,
    ) -> dict[str, Any] | None:
        """Find a close, supported position behind existing terrain and verify the landing."""
        before = self.bot.status()
        origin = before.get("pos")
        if (
            not origin or before.get("on_ground") is not True
            or before.get("alive") is False or before.get("ready") is False
            or before.get("in_water") or before.get("in_lava")
        ):
            return None
        self.invalidate_block_cache()
        shooters = (threat_pos, *other_shooters)
        blocks_shot = self._blocks_shot_across_hitbox if verify_live_shooters else self._blocks_shot
        # A fresh blast at this location overrides otherwise favorable sight lines.
        already_covered = allow_stationary and all(blocks_shot(origin, shooter) for shooter in shooters)
        if not already_covered:
            away, _ = aim_yaw_pitch(threat_pos, origin)
            for offset in (90, -90, 45, -45):
                yaw = away + math.radians(offset)
                route = self.probe_retreat(origin, yaw, 2.2)
                if route is None:
                    continue
                ux, uz = yaw_to_unit_xz(yaw)
                target = [origin[0] + ux * 2.2, route[0], origin[2] + uz * 2.2]
                if all(blocks_shot(target, shooter) for shooter in shooters):
                    break
            else:
                return None

        current = self._safe_status({"ready": False})
        current_pos = current.get("pos")
        if (
            not current_pos or math.dist(current_pos[:3], origin[:3]) > 0.18
            or current.get("on_ground") is not True
            or current.get("alive") is False or current.get("ready") is False
            or current.get("in_water") or current.get("in_lava")
            or current.get("last_death") != before.get("last_death")
            or current.get("dimension") != before.get("dimension")
        ):
            return None
        if already_covered:
            confirmed = not verify_live_shooters or self._blocks_live_shots(current_pos)
            return {
                "ok": confirmed, "mode": "natural_cover", "pos_after": current_pos, "moved_dist": 0.0,
                **({"reason": "shooter_lanes_unconfirmed"} if not confirmed else {}),
            }
        try:
            self.bot.call(
                "move_to",
                {"x": target[0], "y": target[1], "z": target[2], "range": 0.25,
                 "timeout": 1200, "stop_on": ["death"]},
                timeout_s=1.5, allow_reauth=False,
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop()

        after = self._safe_status({"ready": False})
        final_pos = after.get("pos")
        self.invalidate_block_cache()
        respawned = after.get("alive") is False or bool(
            final_pos and (after.get("last_death") != before.get("last_death")
                           or after.get("dimension") != before.get("dimension"))
        )
        confirmed = bool(
            final_pos and math.dist(final_pos[:3], target) <= 0.6 and after.get("on_ground") is True
            and after.get("ready") is not False and not respawned
            and not after.get("in_water") and not after.get("in_lava")
            and all(blocks_shot(final_pos, shooter) for shooter in shooters)
        )
        reason = "cover_unconfirmed" if not confirmed else None
        if confirmed and verify_live_shooters and not self._blocks_live_shots(final_pos):
            confirmed = False
            reason = "shooter_lanes_unconfirmed"
        return {
            "ok": confirmed, "mode": "natural_cover", "pos_after": final_pos,
            **({"reason": reason} if reason else {}),
            **({"respawned": True} if respawned else {}),
        }

    def _blocks_live_shots(self, pos: Sequence[float]) -> bool:
        try:
            sighting = self.bot.call("get_entities", {"radius": 16, "types": ["hostile"]}, timeout_s=0.4, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            return False
        entities = sighting.get("entities")
        if not isinstance(entities, list):
            return False
        shooters = ranged_retreat_positions(pos, entities, max_distance=16.0)
        # A wall checked before the entity scan may have changed while the scan was in flight.
        self.invalidate_block_cache()
        return bool(shooters) and all(self._blocks_shot_across_hitbox(pos, shooter) for _, shooter in shooters)

    def flee(
        self,
        threat_pos: Sequence[float] | None = None,
        *,
        distance: float = 14.0,
        urgent: bool = True,
        allow_shaft_seal: bool = True,
        wait_for_ground: bool = False,
        initial_landing_checks: int = 3,
        other_shooters: Sequence[Sequence[float]] = (),
        occluded_shooters: Sequence[Sequence[float]] = (),
        other_melee: Sequence[Sequence[float]] = (),
        primary_melee: bool = False,
        other_creepers: Sequence[Sequence[float]] = (),
        avoid_blast: bool = False,
        keep_blast_clearance: bool = False,
        use_nearest_hostile: bool = True,
        min_threat_distance: float | None = None,
        allow_second_leg: bool = True,
    ) -> dict[str, Any]:
        """Disengage from `threat_pos` along verified ground or seal a carved shaft."""
        def respawned_since(before: dict[str, Any], after: dict[str, Any]) -> bool:
            start, end = before.get("pos"), after.get("pos")
            return bool(
                after.get("alive") is False
                or (after.get("dimension") and before.get("dimension") and after["dimension"] != before["dimension"])
                or (after.get("last_death") is not None and after["last_death"] != before.get("last_death"))
                or (start and end and math.hypot(end[0] - start[0], end[2] - start[2]) > 64.0)
            )

        def settle_airborne(status: dict[str, Any], life: dict[str, Any], checks: int = 3) -> dict[str, Any]:
            # Each sample allows one physics interval, with a shared allowance for status reads.
            deadline = time.monotonic() + (checks * 100 + 150) / 1000
            sample = status
            for _ in range(checks):
                if (
                    sample.get("on_ground") is not False or sample.get("ready") is False
                    or sample.get("in_water") or sample.get("in_lava") or respawned_since(life, sample)
                ):
                    break
                if deadline - time.monotonic() <= 0.1:
                    break
                time.sleep(0.1)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    observed = self.bot.call("status", method="GET", timeout_s=remaining, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    break
                sample = {**observed, "on_ground": observed.get("on_ground") is True}
            return sample

        st0 = self.bot.status()
        entered_water = False
        if (
            wait_for_ground and st0.get("pos") and st0.get("on_ground") is False
            and st0.get("alive") is not False and st0.get("ready") is not False
        ):
            before = st0
            st0 = settle_airborne(before, before, checks=initial_landing_checks)
            if respawned_since(before, st0):
                return {
                    "ok": False, "mode": "respawned", "respawned": True, "moved_dist": 0.0,
                    "pos_after": st0.get("pos") or before["pos"],
                }
            entered_water = bool(st0.get("in_water") and not before.get("in_water"))
        pos0 = st0.get("pos")
        if not pos0 or st0.get("alive") is False or st0.get("ready") is False:
            return {"ok": False, "mode": "flee", "reason": "not_ready", "moved_dist": 0.0, "pos_after": pos0}
        if st0.get("in_lava") or entered_water or st0.get("on_ground") is False:
            return {
                "ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0, "pos_after": pos0,
                **{key: st0.get(key) for key in ("on_ground", "in_water", "in_lava")},
            }
        if threat_pos is None and use_nearest_hostile:
            nh = st0.get("nearest_hostile")
            if isinstance(nh, dict) and isinstance(nh.get("pos"), list):
                threat_pos = nh["pos"]

        if allow_shaft_seal:
            snap = self.arena_snapshot(pos0)
            if snap["enclosure"] == "carved_shaft":
                seal = self.fortify(st0, lambda: self.build_tactical("full_seal", threat_pos=threat_pos))
                if seal.get("ok"):
                    pos_s = seal.get("pos_after") or pos0
                    return {"ok": True, "mode": "sealed", "moved_dist": 0.0, "pos_after": pos_s}

        if threat_pos is not None and len(threat_pos) >= 3:
            away_dx = float(pos0[0]) - float(threat_pos[0])
            away_dz = float(pos0[2]) - float(threat_pos[2])
            pref_yaw = math.atan2(-away_dx, -away_dz) if math.hypot(away_dx, away_dz) > 0.05 else float(st0.get("yaw") or 0.0)
        else:
            pref_yaw = float(st0.get("yaw") or 0.0) + math.pi

        self.invalidate_block_cache()
        short_step = False
        disengage_range = min(float(distance), 8.5) if min_threat_distance is None else min_threat_distance
        yaws = [pref_yaw + math.radians(angle) for angle in (0, 35, -35, 70, -70, 110, -110, 150, -150, 180)]
        covered_shooters = [(shooter, min(6.0, math.dist(pos0[:3], shooter[:3]))) for shooter in occluded_shooters]

        def avoids_uncovering_close_fire(point: Sequence[float], deadline: float) -> bool:
            try:
                return all(
                    math.dist(point[:3], shooter[:3]) >= clearance
                    or self._blocks_shot_across_hitbox(point, shooter, deadline=deadline)
                    for shooter, clearance in covered_shooters
                )
            except _TerrainProbeExpired:
                return False

        def probe_route(start: Sequence[float], yaw: float, length: float, **options: bool) -> tuple[int, bool] | None:
            route = self.probe_retreat(start, yaw, length, **options)
            if route is None or not covered_shooters:
                return route
            goal_y, _ = route
            ux, uz = yaw_to_unit_xz(yaw)
            steps = math.ceil(length)
            for step in range(1, steps + 1):
                travel = length * step / steps
                point = [start[0] + ux * travel, goal_y, start[2] + uz * travel]
                if not avoids_uncovering_close_fire(point, visibility_deadline):
                    return None
            return route

        require_blast_clearance = keep_blast_clearance or avoid_blast
        blast_offsets = [
            tuple(float(threat[axis]) - float(pos0[axis]) for axis in range(3))
            for threat in (threat_pos, *other_creepers)
        ] if (other_creepers or require_blast_clearance) and threat_pos is not None and len(threat_pos) >= 3 else []

        def blast_clearance(yaw: float, travel: float) -> tuple[float, float]:
            ux, uz = yaw_to_unit_xz(yaw)
            loss = 0.0
            endpoint = math.inf
            step = min(travel, 3.0)
            for dx, dy, dz in blast_offsets:
                nearest_step = max(0.0, min(travel, dx * ux + dz * uz))
                nearest = math.hypot(dx - ux * nearest_step, dy, dz - uz * nearest_step)
                loss = max(loss, min(6.0, math.hypot(dx, dy, dz)) - nearest)
                endpoint = min(endpoint, math.hypot(dx - ux * step, dy, dz - uz * step))
            return -loss, endpoint

        blast_route_length = min(float(distance), 9.5)
        if other_creepers and blast_offsets:
            yaws.extend((pref_yaw + math.pi / 2, pref_yaw - math.pi / 2))
            # The optional second leg can add six blocks in the same direction.
            yaws.sort(key=lambda yaw: blast_clearance(yaw, blast_route_length), reverse=True)
        elif (other_shooters or other_melee) and threat_pos is not None and len(threat_pos) >= 3:
            # A follow-up move keeps the selected heading. Account for both legs before probing terrain.
            yaws.extend((pref_yaw + math.pi / 2, pref_yaw - math.pi / 2))
            threat_offsets = [
                tuple(float(threat[axis]) - float(pos0[axis]) for axis in range(3))
                for threat in (threat_pos, *other_shooters, *other_melee)
            ]

            def threat_clearance(yaw: float) -> float:
                ux, uz = yaw_to_unit_xz(yaw)
                if require_blast_clearance:
                    dx, dy, dz = threat_offsets[0]
                    # Keep the original order unless the heading quickly clears the blast radius.
                    if dx * ux + dz * uz > 1e-6 or math.hypot(dx - ux * 3, dy, dz - uz * 3) < 5.5:
                        return -1.0
                nearest = math.inf
                for index, (dx, dy, dz) in enumerate(threat_offsets):
                    if require_blast_clearance and index == 0:
                        continue
                    # The primary melee attacker is already in reach. Compare clearance
                    # after the initial step away, and check the whole route when it approaches an attacker.
                    projection = dx * ux + dz * uz
                    stepping_away = index == 0 and (primary_melee or other_melee) and (not primary_melee or projection <= 0)
                    start = min(1.65, disengage_range) if stepping_away else 0.0
                    along_route = max(start, min(disengage_range, projection))
                    nearest = min(nearest, math.hypot(dx - ux * along_route, dy, dz - uz * along_route))
                return nearest

            yaws.sort(key=threat_clearance, reverse=True)
        visibility_deadline = time.monotonic() + 0.75
        for chosen_yaw in yaws:
            if require_blast_clearance and blast_clearance(chosen_yaw, blast_route_length)[0] < -1e-6:
                continue
            route = probe_route(pos0, chosen_yaw, 3.5)
            if route is not None:
                break
        else:
            if st0.get("on_ground") is True and not st0.get("in_water"):
                foot_y = math.floor(float(pos0[1]) + 0.05)
                short_yaws = [pref_yaw + math.pi / 2, pref_yaw - math.pi / 2]
                if blast_offsets:
                    short_yaws = yaws if other_melee and not other_creepers else sorted(
                        yaws, key=lambda yaw: blast_clearance(yaw, 1.65), reverse=True,
                    )
                elif primary_melee and other_shooters:
                    short_yaws = yaws
                elif wait_for_ground:
                    short_yaws.extend(yaws[:3])
                    if threat_pos is not None and not (other_shooters or other_melee):
                        short_yaws.extend(pref_yaw + math.radians(angle) for angle in (70, -70))
                for chosen_yaw in short_yaws:
                    if require_blast_clearance and blast_clearance(chosen_yaw, 1.65)[0] < -1e-6:
                        continue
                    route = probe_route(pos0, chosen_yaw, 1.65)
                    if route == (foot_y, False):
                        short_step = True
                        break
                if not short_step and wait_for_ground and str(st0.get("dimension") or "").removeprefix("minecraft:") == "overworld":
                    for chosen_yaw in short_yaws:
                        if require_blast_clearance and blast_clearance(chosen_yaw, 1.65)[0] < -1e-6:
                            continue
                        route = probe_route(pos0, chosen_yaw, 1.65, allow_step_down=True)
                        if route == (foot_y - 1, False):
                            short_step = True
                            break
                    if (
                        not short_step and threat_pos is not None and not require_blast_clearance
                        and not (other_melee or other_shooters)
                    ):
                        # A nearby step up can lead out of a depression even when no full retreat fits.
                        for angle in (0, 35, -35, 70, -70):
                            chosen_yaw = pref_yaw + math.radians(angle)
                            route = probe_route(pos0, chosen_yaw, 1.65, short_climb=True)
                            if route == (foot_y + 1, True):
                                short_step = True
                                break
            if not short_step:
                return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0, "pos_after": pos0}

        landing_y, needs_jump = route
        short_climb = short_step and needs_jump
        pulse_ms = 450 if urgent else 350
        if short_step:
            pulse_ms = 300 if short_climb else 220
        try:
            self.bot.call("look", {"yaw": chosen_yaw, "pitch": 0.0})
            self.bot.call(
                "control",
                {
                    "forward": True,
                    "sprint": not short_step,
                    "jump": needs_jump or bool(st0.get("in_water")),
                    "ms": pulse_ms,
                    "stop_on": ["death"],
                },
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop()

        def retreat_result(st: dict[str, Any]) -> dict[str, Any]:
            pos = st.get("pos") or pos0
            moved = math.hypot(float(pos[0]) - float(pos0[0]), float(pos[2]) - float(pos0[2]))
            respawned = respawned_since(st0, st)
            unsafe = (
                st.get("ready") is False or st.get("in_lava") or st.get("in_water") or st.get("on_ground") is False
            )
            if covered_shooters and not respawned and not unsafe:
                self.invalidate_block_cache()
                if not avoids_uncovering_close_fire(pos, time.monotonic() + 0.45):
                    return {
                        "ok": False, "mode": "flee", "pos_after": pos, "moved_dist": round(moved, 2),
                        "reason": "projectile_lane_unconfirmed",
                    }
            return {
                "ok": (
                    not respawned and not unsafe and moved >= (0.35 if short_step else 1.5)
                    and (not short_climb or math.floor(float(pos[1]) + 0.05) == landing_y)
                ),
                "mode": "respawned" if respawned else "flee",
                "moved_dist": 0.0 if respawned else round(moved, 2),
                "pos_after": pos,
                **({"respawned": True} if respawned else {}),
                **({
                    "reason": "unsafe_landing",
                    **{key: st.get(key) for key in ("on_ground", "in_water", "in_lava")},
                } if unsafe else {}),
            }

        st_mid = self._safe_status({**st0, "ready": False})
        if wait_for_ground:
            st_mid = settle_airborne(st_mid, st0)
        result = retreat_result(st_mid)
        if (
            not allow_second_leg or short_step or result.get("respawned")
            or result.get("reason") in {"unsafe_landing", "projectile_lane_unconfirmed"}
        ):
            return result

        mid_pos = result["pos_after"]
        threat_dist_mid = 0.0
        if threat_pos is not None and len(threat_pos) >= 3:
            threat_dist_mid = math.hypot(float(mid_pos[0]) - float(threat_pos[0]), float(mid_pos[2]) - float(threat_pos[2]))

        ux, uz = yaw_to_unit_xz(chosen_yaw)
        if (
            threat_pos is not None
            and threat_dist_mid >= disengage_range
            and result["ok"]
        ):
            return result

        leg_limit = 6.0 if min_threat_distance is None else 9.0
        remaining = min(leg_limit, float(distance) - result["moved_dist"])
        if remaining < 1.5:
            return result
        self.invalidate_block_cache()
        visibility_deadline = time.monotonic() + 0.75
        for leg in dict.fromkeys((remaining, min(remaining, 6.0), min(remaining, 3.5))):
            route = probe_route(mid_pos, chosen_yaw, leg)
            if route is not None:
                remaining = leg
                break
        else:
            return result
        goal_y, _ = route
        try:
            self.bot.call(
                "move_to",
                {
                    "x": float(mid_pos[0]) + ux * remaining,
                    "y": goal_y,
                    "z": float(mid_pos[2]) + uz * remaining,
                    "range": 1.0,
                    "timeout": 2500,
                    "stop_on": ["death"],
                },
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop()
        st_final = self._safe_status({**st_mid, "ready": False})
        if wait_for_ground:
            st_final = settle_airborne(st_final, st0)
        return retreat_result(st_final)

    def step_away_from_melee(self, status: dict[str, Any], threat_pos: Sequence[float], *, deadline: float) -> bool:
        """Take one short, supported step away from a still-close attacker."""
        origin = status["pos"]
        away, _ = aim_yaw_pitch(threat_pos, origin)
        foot_y = math.floor(float(origin[1]) + 0.05)
        self.invalidate_block_cache()
        try:
            for offset in (0, 45, -45):
                yaw = away + math.radians(offset)
                if self.probe_retreat(origin, yaw, 1.65, deadline=deadline - 0.85) == (foot_y, False):
                    break
            else:
                return False
            current = self.bot.call("status", method="GET", timeout_s=0.2, allow_reauth=False)
            pos = current.get("pos")
            if (
                not pos or math.dist(pos[:3], origin[:3]) > 0.2
                or current.get("on_ground") is not True or current.get("in_water") or current.get("in_lava")
                or current.get("ready") is False or current.get("alive") is False
                or current.get("last_death") != status.get("last_death") or current.get("dimension") != status.get("dimension")
                or time.monotonic() + 0.65 >= deadline
            ):
                return False
            self.bot.call("look", {"yaw": yaw, "pitch": 0.0}, timeout_s=0.15, allow_reauth=False)
            result = self.bot.call(
                "control", {"forward": True, "ms": 220, "stop_on": ["death"]},
                timeout_s=0.3, allow_reauth=False,
            )
            remaining = deadline - time.monotonic()
            if not result.get("ok", True) or remaining <= 0:
                return False
            landed = self.bot.call("status", method="GET", timeout_s=min(0.2, remaining), allow_reauth=False)
        except (ApiError, httpx.HTTPError, _TerrainProbeExpired):
            return False
        end = landed.get("pos")
        if (
            not end or landed.get("on_ground") is not True or landed.get("ready") is False
            or landed.get("alive") is False or landed.get("in_water") or landed.get("in_lava")
            or landed.get("last_death") != status.get("last_death") or landed.get("dimension") != status.get("dimension")
            or math.floor(float(end[1]) + 0.05) != foot_y
        ):
            return False
        return (
            math.hypot(end[0] - pos[0], end[2] - pos[2]) >= 0.25
            and math.hypot(end[0] - threat_pos[0], end[2] - threat_pos[2])
            > math.hypot(pos[0] - threat_pos[0], pos[2] - threat_pos[2])
        )

    def _point_allowed(self, x: float, z: float, *, action: str = "combat_env") -> bool:
        """Return True unless an enabled VoidGeofence rejects `(x, z)` via `check_point`."""
        if self.geofence is None or not hasattr(self.geofence, "check_point"):
            return True
        return self.geofence.check_point(float(x), float(z), action=action) is None

    @staticmethod
    def _crosses_near_water_threat(pos: Sequence[float], x: float, z: float, threat: Sequence[float]) -> bool:
        dx, dz = x - pos[0], z - pos[2]
        length_sq = dx * dx + dz * dz
        if length_sq == 0:
            return False
        tx, ty, tz = threat[0] - pos[0], threat[1] - pos[1], threat[2] - pos[2]
        fraction = max(0.0, min(1.0, (tx * dx + tz * dz) / length_sq))
        closest = math.hypot(tx - fraction * dx, ty, tz - fraction * dz)
        return closest < min(4.5, math.hypot(tx, ty, tz)) - 1e-6

    def _find_walkable_shore_target(
        self,
        pos0: Sequence[float],
        *,
        block_types: Sequence[str],
        max_radius: int = 24,
        min_y: int = -64,
        max_y: int | None = None,
        threat_pos: Sequence[float] | None = None,
        max_candidates: int = 10,
        avoid_close_threat: bool = False,
        deadline: float | None = None,
        excluded_shores: Collection[tuple[float, ...]] = (),
    ) -> list[float] | None:
        """Query `find_blocks` for nearby solid shoreline with 2-high passable headroom."""
        start_x, start_z = float(pos0[0]), float(pos0[2])
        has_threat = isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3
        tx_th = float(threat_pos[0]) if has_threat else 0.0
        tz_th = float(threat_pos[2]) if has_threat else 0.0
        threat_dist0 = math.hypot(start_x - tx_th, start_z - tz_th) if has_threat else 0.0

        def _pick_from_query(q_types: Sequence[str], *, q_limit: int) -> list[float] | None:
            transport = {}
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                transport = {"timeout_s": remaining, "allow_reauth": False}
            try:
                found = self.bot.call(
                    "find_blocks",
                    {
                        "radius": max(8, min(28, int(max_radius))),
                        "types": list(q_types),
                        "limit": q_limit,
                    },
                    **transport,
                )
            except Exception:
                found = None

            candidates: list[tuple[float, int, int, int]] = []
            for row in (found or {}).get("blocks") or []:
                if not isinstance(row, dict):
                    continue
                rpos = row.get("pos")
                if not isinstance(rpos, (list, tuple)) or len(rpos) < 3:
                    continue
                cx, cy, cz = int(rpos[0]), int(rpos[1]), int(rpos[2])
                if cy < min_y or (max_y is not None and cy > max_y):
                    continue
                if (cx + 0.5, float(cy + 1), cz + 0.5) in excluded_shores:
                    continue
                if not _is_solid_ground(_clean_block_name(row.get("name"))):
                    continue
                if not self._point_allowed(cx + 0.5, cz + 0.5, action="lava_shore"):
                    continue
                if avoid_close_threat and has_threat and self._crosses_near_water_threat(pos0, cx + 0.5, cz + 0.5, threat_pos):
                    continue
                hdist = math.hypot(cx + 0.5 - start_x, cz + 0.5 - start_z)
                away_penalty = 1.5 if (has_threat and math.hypot(cx + 0.5 - tx_th, cz + 0.5 - tz_th) < threat_dist0) else 0.0
                candidates.append((hdist + away_penalty, cx, cy, cz))

            candidates.sort(key=lambda item: item[0])
            for _, cx, cy, cz in candidates[:max(1, int(max_candidates))]:
                probe_kwargs = {"deadline": deadline} if deadline is not None else {}
                foot_b = self._get_block_name(cx, cy + 1, cz, **probe_kwargs)
                head_b = self._get_block_name(cx, cy + 2, cz, **probe_kwargs)
                if _is_passable(foot_b) and _is_passable(head_b):
                    return [cx + 0.5, float(cy + 1), cz + 0.5]
            return None

        if tuple(block_types) == SHORE_LAND_BLOCKS:
            surf_hit = _pick_from_query(SURFACE_SHORE_PRIORITY_BLOCKS, q_limit=48)
            if surf_hit is not None:
                return surf_hit
        return _pick_from_query(block_types, q_limit=120)

    def remember_dry_bank(self, status: dict[str, Any]) -> None:
        pos = status.get("pos")
        if (
            pos and status.get("on_ground") is True and status.get("ready") is True and status.get("alive") is True
            and not status.get("in_water") and not status.get("submerged") and not status.get("in_lava")
        ):
            x, y, z = map(math.floor, pos[:3])
            bank = (x + 0.5, float(y), z + 0.5)
            life = (status.get("dimension"), status.get("last_death"))
            self._last_dry_bank = (bank, life, time.monotonic())
            if self._stalled_water_shore is not None and (bank, life) == (
                self._stalled_water_shore[0], self._stalled_water_shore[2],
            ):
                self._stalled_water_shore = None

    def _dry_bank_is_walkable(self, bank: Sequence[float]) -> bool:
        self.invalidate_block_cache()
        bx, by, bz = map(math.floor, bank)
        deadline = time.monotonic() + 0.6
        try:
            floor, feet, head = (
                self._get_block_name(bx, by + dy, bz, strict=True, deadline=deadline) for dy in (-1, 0, 1)
            )
        except _TerrainProbeExpired:
            return False
        return (
            floor not in HAZARD_BLOCK_TYPES and _is_solid_ground(floor)
            and _is_passable(feet) and _is_passable(head)
        )

    def escape_deep_water_to_shore(
        self,
        threat_pos: Sequence[float] | None = None,
        *,
        max_radius: int = 24,
        keep_shield: bool = False,
        shield_target_id: int | None = None,
        surface_first: bool = False,
        avoid_close_threat: bool = False,
        sneak_on_magma: bool = False,
        prefer_recent_dry_bank: bool = False,
    ) -> dict[str, Any]:
        """Swim up and move toward the nearest walkable dry shoreline block (or retreat at surface)."""
        st0 = self.bot.status()
        previous_bank = self._nearby_water_bank
        self._nearby_water_bank = None
        if st0.get("alive") is False or st0.get("ready") is False:
            return {
                "ok": False,
                "in_water": bool(st0.get("in_water", True)),
                "mode": "not_ready",
                "pos_after": st0.get("pos"),
            }
        if st0.get("in_lava"):
            return {
                "ok": False, "in_water": bool(st0.get("in_water")), "in_lava": True,
                "mode": "entered_lava", "pos_after": st0.get("pos"),
            }
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        start_x, start_z = float(pos0[0]), float(pos0[2])
        actions: list[str] = []
        shore_target: list[float] | None = None
        has_threat = isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3

        def interrupted_water_escape(st: dict[str, Any]) -> dict[str, Any] | None:
            pos = st.get("pos") or pos0
            respawned = (
                math.hypot(float(pos[0]) - start_x, float(pos[2]) - start_z) > 64.0
                or (st.get("last_death") is not None and st["last_death"] != st0.get("last_death"))
                or (st.get("dimension") and st0.get("dimension") and st["dimension"] != st0["dimension"])
            )
            if respawned:
                mode = "respawned"
            elif st.get("alive") is False or st.get("ready") is False:
                mode = "not_ready"
            elif st.get("in_lava"):
                mode = "entered_lava"
            else:
                return None
            return {
                "ok": False,
                "in_water": bool(st.get("in_water", True)),
                "mode": mode,
                **({"in_lava": True} if mode == "entered_lava" else {}),
                "shore_target": shore_target,
                "actions": actions,
                "pos_after": pos,
            }

        def settle_landing(st: dict[str, Any], bank: Sequence[float] | None = None) -> dict[str, Any]:
            deadline = time.monotonic() + 0.45
            for _ in range(3):
                pos = st.get("pos")
                if (
                    st.get("in_water") or st.get("on_ground") is not False or not pos
                    or deadline - time.monotonic() <= 0.1
                    or (bank is not None and (
                        math.hypot(pos[0] - bank[0], pos[2] - bank[2]) > 0.6 or not -0.6 <= pos[1] - bank[1] <= 1.25
                    ))
                ):
                    break
                time.sleep(0.1)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    sample = self.bot.call("status", method="GET", timeout_s=min(0.25, remaining), allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    break
                if not isinstance(sample, dict):
                    break
                if interrupted_water_escape(sample) is not None:
                    return sample
                if not sample.get("pos"):
                    break
                st = sample
            return st

        if not st0.get("in_water"):
            refreshed = settle_landing(st0)
            interruption = interrupted_water_escape(refreshed)
            if interruption is not None:
                return interruption
            if not refreshed.get("in_water"):
                landed = refreshed.get("on_ground") is not False
                if landed:
                    self.remember_dry_bank(refreshed)
                return {
                    "ok": landed,
                    "in_water": False,
                    "mode": "already_dry" if landed else "shore_landing_unconfirmed",
                    "pos_after": refreshed.get("pos"),
                }
            st0 = refreshed
            pos0 = st0["pos"]
            start_x, start_z = float(pos0[0]), float(pos0[2])
        search_status = st0

        def swim_step_allowed(
            pos: Sequence[float], yaw: float, distance: float = 5.0,
            *, inward_target: tuple[float, float] | None = None,
        ) -> bool:
            ux, uz = yaw_to_unit_xz(yaw)
            x, z = float(pos[0]) + distance * ux, float(pos[2]) + distance * uz
            can_step_inward = (
                inward_target is not None
                and not self._point_allowed(pos[0], pos[2], action="water_retreat")
                and math.hypot(inward_target[0] - pos[0], inward_target[1] - pos[2]) >= distance
                and math.hypot(inward_target[0] - x, inward_target[1] - z)
                < math.hypot(inward_target[0] - pos[0], inward_target[1] - pos[2])
            )
            return (can_step_inward or self._point_allowed(x, z, action="water_retreat")) and not (
                avoid_close_threat and has_threat and self._crosses_near_water_threat(pos, x, z, threat_pos)
            )

        def shore_bank_in_reach(
            pos: Sequence[float], bank: Sequence[float], *, max_dist: float = 3.5, max_height: float = 1.25,
        ) -> bool:
            return (
                math.hypot(bank[0] - pos[0], bank[2] - pos[2]) <= max_dist
                and abs(bank[1] - pos[1]) <= max_height
                and self._point_allowed(bank[0], bank[2], action="water_shore")
                and not (
                    avoid_close_threat and has_threat
                    and self._crosses_near_water_threat(pos, bank[0], bank[2], threat_pos)
                )
            )

        life = (st0.get("dimension"), st0.get("last_death"))
        retry_bank = None
        if previous_bank is not None:
            bank, bank_life, bank_at = previous_bank
            if (
                life == bank_life and time.monotonic() - bank_at < 12.0
                and shore_bank_in_reach(pos0, bank) and self._dry_bank_is_walkable(bank)
            ):
                retry_bank = list(bank)

        # Hold buoyancy + shield while locating shoreline or breaking surface.
        try:
            self.bot.call(
                "posture",
                {"shield": bool(keep_shield), "sneak": sneak_on_magma, "jump": True},
            )
        except ApiError:
            pass

        oxy0 = st0.get("oxygen")
        if surface_first or bool(st0.get("submerged")) or (isinstance(oxy0, (int, float)) and oxy0 < 260):
            swim_forward = (
                not (surface_first or has_threat or keep_shield)
                and retry_bank is None
                and swim_step_allowed(pos0, float(st0.get("yaw") or 0.0))
            )
            for _ in range(2):
                try:
                    self.bot.call(
                        "control",
                        {"forward": swim_forward, "jump": True, "sprint": swim_forward, "ms": 750, "stop_on": ["death"]},
                    )
                    actions.append("water_surface_surge")
                except ApiError:
                    pass
                search_status = self.bot.status()
                interruption = interrupted_water_escape(search_status)
                if interruption is not None:
                    return interruption
                oxygen = search_status.get("oxygen")
                raised_pos = search_status.get("pos")
                # Continue a successful ascent while the bot has no air.
                if not (
                    surface_first and search_status.get("in_water") and search_status.get("submerged")
                    and isinstance(oxygen, (int, float)) and oxygen <= 0
                    and raised_pos and raised_pos[1] - pos0[1] >= 0.5
                ):
                    break
            if surface_first and not search_status.get("in_water") and search_status.get("on_ground") is not False:
                if sneak_on_magma:
                    try:
                        self.bot.call("posture", {"shield": bool(keep_shield), "sneak": False, "jump": False})
                    except (ApiError, httpx.HTTPError):
                        pass
                return {
                    "ok": True, "in_water": False, "mode": "surfaced",
                    "actions": actions, "pos_after": search_status.get("pos"),
                }

        # The surge can change both the shoreline height and the bot's horizontal position.
        search_pos = search_status.get("pos") or pos0
        shore_x, shore_y, shore_z = map(float, search_pos[:3])
        by = math.floor(shore_y + 0.05)
        excluded_shore = None
        if self._stalled_water_shore is not None:
            stalled_shore, stalled_pos, stalled_life, stalled_at = self._stalled_water_shore
            if life == stalled_life and time.monotonic() - stalled_at < 15.0 and math.dist(search_pos[:3], stalled_pos) <= 2.5:
                excluded_shore = stalled_shore
                actions.append("shore_lip_stalled")

        def return_to_dry_bank(*, max_dist: float, max_height: float) -> list[float] | None:
            if self._last_dry_bank is None:
                return None
            bank, bank_life, bank_at = self._last_dry_bank
            if (
                life == bank_life and time.monotonic() - bank_at < 60.0 and bank != excluded_shore
                and shore_bank_in_reach(search_pos, bank, max_dist=max_dist, max_height=max_height)
                and self._dry_bank_is_walkable(bank)
            ):
                self._last_dry_bank = None
                actions.append("return_to_dry_bank")
                return list(bank)
            return None

        retrying_bank = (
            retry_bank is not None and tuple(retry_bank) != excluded_shore and shore_bank_in_reach(search_pos, retry_bank)
        )
        if retrying_bank:
            shore_target = retry_bank
            actions.append("shore_bank_retry")
        elif prefer_recent_dry_bank:
            shore_target = return_to_dry_bank(max_dist=3.5, max_height=1.25)
        if shore_target is None:
            shore_target = self._find_walkable_shore_target(
                search_pos,
                block_types=SHORE_LAND_BLOCKS,
                max_radius=max_radius,
                min_y=by - 1,
                max_y=by + 2,
                threat_pos=threat_pos,
                avoid_close_threat=avoid_close_threat,
                excluded_shores=(excluded_shore,) if excluded_shore is not None else (),
            )

        # 2. Radial fallback if find_blocks yielded no dry surface spot.
        if shore_target is None:
            base_yaw = (
                aim_yaw_pitch(threat_pos, search_pos)[0]
                if has_threat
                else float(search_status.get("yaw") or 0.0)
            )
            y_probe = by
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                cyaw = base_yaw + math.radians(deg)
                ux, uz = yaw_to_unit_xz(cyaw)
                for dist in (4, 8, 14):
                    px = math.floor(shore_x + ux * dist)
                    pz = math.floor(shore_z + uz * dist)
                    candidate = (px + 0.5, float(y_probe + 1), pz + 0.5)
                    if candidate == excluded_shore or not self._point_allowed(px + 0.5, pz + 0.5, action="water_shore"):
                        continue
                    if avoid_close_threat and has_threat and self._crosses_near_water_threat(search_pos, px + 0.5, pz + 0.5, threat_pos):
                        continue
                    g_name = self._get_block_name(px, y_probe, pz, strict=True)
                    if g_name and g_name not in HAZARD_BLOCK_TYPES and _is_solid_ground(g_name):
                        foot_p = self._get_block_name(px, y_probe + 1, pz, strict=True)
                        head_p = self._get_block_name(px, y_probe + 2, pz, strict=True)
                        if _is_passable(foot_p) and _is_passable(head_p):
                            shore_target = list(candidate)
                            break
                if shore_target is not None:
                    break

        if shore_target is None:
            shore_target = return_to_dry_bank(max_dist=36.0, max_height=2.0)

        # 3. Move to shore target (plus shoreline lip jump) or swim up and away from threat.
        shore_path_unreachable = False
        if shore_target is not None:
            shore_dist = math.hypot(shore_target[0] - shore_x, shore_target[2] - shore_z)
            contested_short_crossing = (has_threat or shield_target_id is not None) and shore_dist <= 5.0
            try:
                syaw, _ = aim_yaw_pitch(search_pos, shore_target)
                self.bot.call("look", {"yaw": syaw, "pitch": -10.0})
            except (ApiError, httpx.HTTPError):
                pass
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": shore_target[0],
                        "y": shore_target[1],
                        "z": shore_target[2],
                        "range": 0.6,
                        "timeout": 1800 if contested_short_crossing else 4200,
                        "stop_on": ["death"],
                    },
                    **({"timeout_s": 2.4, "allow_reauth": False} if contested_short_crossing else {}),
                )
                actions.append("move_to_shore")
            except (ApiError, httpx.HTTPError) as err:
                actions.append("move_to_shore_failed")
                shore_path_unreachable = isinstance(err, ApiError) and err.code in {"path_not_found", "no_path"}
                if isinstance(err, httpx.HTTPError):
                    self._best_effort_stop(timeout_s=0.5 if contested_short_crossing else None)

            st_mid = self._safe_status(st0)
            interruption = interrupted_water_escape(st_mid)
            if interruption is not None:
                return interruption
            if (
                bool(st_mid.get("in_water"))
                and st_mid.get("alive") is not False
                and st_mid.get("ready", True)
            ):
                mid_pos = st_mid.get("pos") or pos0
                lip_height = float(shore_target[1]) - float(mid_pos[1])
                lip_distance = math.hypot(shore_target[0] - mid_pos[0], shore_target[2] - mid_pos[2])
                lip_ms = 1150 if lip_height > 0.05 else 850
                shallow_lip = 0.6 < lip_distance <= 1.8 and 0.0 <= lip_height <= 1.25
                # A failed short hop can leave the bot pressed against the same low bank.
                sustained_climb = (
                    shallow_lip and retrying_bank and not (has_threat or shield_target_id is not None or sneak_on_magma)
                )
                if shallow_lip and not sustained_climb:
                    # A full swim pulse can carry the bot across a shallow one-block bank.
                    lip_ms = 450
                try:
                    syaw_m, _ = aim_yaw_pitch(mid_pos, shore_target)
                    self.bot.call("look", {"yaw": syaw_m, "pitch": -10.0})
                    self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "sprint": not sustained_climb, "ms": lip_ms, "stop_on": ["death"]},
                    )
                    actions.append("shore_lip_jump")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop()
        else:
            st_mid = self._safe_status(st0)
            interruption = interrupted_water_escape(st_mid)
            if interruption is not None:
                return interruption
            swim_pos = st_mid.get("pos") or pos0
            pref_yaw = aim_yaw_pitch(threat_pos, swim_pos)[0] if has_threat else float(st0.get("yaw") or 0.0)
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                away_yaw = pref_yaw + math.radians(deg)
                if swim_step_allowed(swim_pos, away_yaw):
                    break
            else:
                away_yaw = None

            inward_target = None
            clamp = getattr(self.geofence, "clamped_move_target", None)
            if away_yaw is None and callable(clamp):
                candidate = clamp(swim_pos[0], swim_pos[2])
                if candidate and self._point_allowed(*candidate, action="water_retreat") and not (
                    avoid_close_threat and has_threat
                    and self._crosses_near_water_threat(swim_pos, *candidate, threat_pos)
                ):
                    inward_target = candidate
                    away_yaw = aim_yaw_pitch(swim_pos, [candidate[0], swim_pos[1], candidate[1]])[0]
                    actions.append("surface_retreat_inward")

            if away_yaw is None:
                actions.append("surface_retreat_geofence_blocked")
            elif st_mid.get("in_water"):
                ux, uz = yaw_to_unit_xz(away_yaw)
                px, py, pz = map(float, swim_pos[:3])
                step_dist = 12.0 if swim_step_allowed(swim_pos, away_yaw, 12.0) else 5.0
                target_x, target_z = inward_target or (px + ux * step_dist, pz + uz * step_dist)
                try:
                    self.bot.call("look", {"yaw": away_yaw, "pitch": -15.0})
                except (ApiError, httpx.HTTPError):
                    pass
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": target_x,
                            "y": max(63.0, py + 1.0) if py >= 61.0 else py + 1.0,
                            "z": target_z,
                            "range": 1.5,
                            "timeout": 2800,
                            "stop_on": ["death"],
                        },
                    )
                    actions.append("surface_retreat_move")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop()
                st_mid = self._safe_status(st_mid)
                interruption = interrupted_water_escape(st_mid)
                if interruption is not None:
                    return interruption
                current = st_mid.get("pos") or swim_pos
                if inward_target is not None and not self._point_allowed(current[0], current[2], action="water_retreat"):
                    away_yaw = aim_yaw_pitch(current, [target_x, current[1], target_z])[0]
                if st_mid.get("in_water") and swim_step_allowed(current, away_yaw, inward_target=inward_target):
                    try:
                        self.bot.call(
                            "control",
                            {"forward": True, "jump": True, "sprint": True, "yaw": away_yaw, "ms": 950, "stop_on": ["death"]},
                        )
                        actions.append("surface_retreat_jump")
                    except (ApiError, httpx.HTTPError) as err:
                        if isinstance(err, httpx.HTTPError):
                            self._best_effort_stop()

        st_end = self._safe_status()
        observed_end_pos = bool(st_end.get("pos"))
        st_end = st_end or st0
        interruption = interrupted_water_escape(st_end)
        if interruption is not None:
            return interruption
        pos_end = st_end.get("pos") or pos0

        still_wet = bool(st_end.get("in_water"))
        try:
            self.bot.call(
                "posture",
                {"shield": bool(keep_shield), "sneak": sneak_on_magma and still_wet, "jump": still_wet},
            )
        except ApiError:
            pass
        # A jump can clear the water flag before the bot has reached the bank.
        if (
            shore_target is not None and not still_wet and st_end.get("on_ground") is False
            and not st_end.get("in_lava") and math.dist(pos_end[:3], shore_target) <= 2.0
        ):
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": shore_target[0], "y": shore_target[1], "z": shore_target[2],
                        "range": 0.25, "timeout": 1600, "stop_on": ["death"],
                    },
                    timeout_s=2.5, allow_reauth=False,
                )
                actions.append("shore_landing")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    self._best_effort_stop()
            st_end = self._safe_status({**st_end, "ready": False})
            interruption = interrupted_water_escape(st_end)
            if interruption is not None:
                return interruption
            pos_end = st_end.get("pos") or pos_end
            still_wet = bool(st_end.get("in_water"))
            if still_wet:
                try:
                    self.bot.call("posture", {"shield": bool(keep_shield), "sneak": sneak_on_magma, "jump": True})
                except ApiError:
                    pass
        if keep_shield and isinstance(shield_target_id, int):
            try:
                self.bot.call("look", {"entity_id": shield_target_id}, timeout_s=0.9, allow_reauth=False)
                actions.append("face_water_attacker")
            except (ApiError, httpx.HTTPError):
                pass
        # A timed-out crossing can also find dry footing before the selected bank.
        if observed_end_pos and not still_wet:
            landing_bank = shore_target
            if landing_bank is not None and math.dist(pos_end[:3], landing_bank) > 2.0:
                landing_bank = None
            st_end = settle_landing(st_end, bank=landing_bank)
            interruption = interrupted_water_escape(st_end)
            if interruption is not None:
                return interruption
            pos_end = st_end.get("pos") or pos_end
            still_wet = bool(st_end.get("in_water"))
            if still_wet:
                try:
                    self.bot.call("posture", {"shield": bool(keep_shield), "sneak": sneak_on_magma, "jump": True})
                except ApiError:
                    pass
        landed = st_end.get("on_ground") is not False and not st_end.get("in_lava")
        if not still_wet and landed:
            self._stalled_water_shore = None
            self.remember_dry_bank(st_end)
        elif observed_end_pos and shore_target is not None and life == (st_end.get("dimension"), st_end.get("last_death")):
            tried_lip = "shore_lip_jump" in actions
            lip_height = shore_target[1] - pos_end[1]
            near_lip = math.hypot(shore_target[0] - pos_end[0], shore_target[2] - pos_end[2]) <= 2.5
            stalled_below_lip = (
                tried_lip and near_lip and lip_height >= (1.75 if still_wet else 0.5)
                and (still_wet or "shore_landing" in actions)
            )
            no_approach = still_wet and (tried_lip or shore_path_unreachable) and (
                math.dist(search_pos[:3], shore_target) - math.dist(pos_end[:3], shore_target) < 0.5
            )
            if stalled_below_lip or no_approach:
                self._stalled_water_shore = (tuple(shore_target), tuple(pos_end[:3]), life, time.monotonic())
        if (
            still_wet and not retrying_bank and shore_target is not None and "shore_lip_jump" in actions
            and life == (st_end.get("dimension"), st_end.get("last_death"))
            and shore_bank_in_reach(pos_end, shore_target)
        ):
            self._nearby_water_bank = (tuple(shore_target), life, time.monotonic())
        return {
            "ok": not still_wet and landed,
            "in_water": still_wet,
            "mode": "move_to_shore" if shore_target is not None else "surface_retreat",
            "shore_target": shore_target,
            "actions": actions,
            "pos_after": pos_end,
            **({"reason": "shore_landing_unconfirmed"} if not still_wet and not landed else {}),
        }

    def guard_lava_edge(
        self,
        pos: Sequence[float] | None = None,
        *,
        counts: dict[str, int] | None = None,
        flowing_lava_target: list[int] | None = None,
    ) -> dict[str, Any]:
        """Force sneak posture and seal exposed lava around the current foothold."""
        st0 = self.bot.status()
        if bool(st0.get("in_lava")) or st0.get("on_ground") is not True or st0.get("ready") is False or st0.get("alive") is False:
            return {"ok": False, "near_lava": False, "sneak": bool(st0.get("sneaking")), "sealed": 0}

        cur_pos = st0.get("pos") or pos or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(cur_pos[0])), math.floor(float(cur_pos[1]) + 0.05), math.floor(float(cur_pos[2]))
        exposed_lava: list[list[int]] = []
        if flowing_lava_target is not None:
            fx, fy, fz = flowing_lava_target
            # A flow above an adjacent column can overrun the ledge even after plugging lower cells.
            if 1 <= fy - by <= 2 and max(abs(fx - bx), abs(fz - bz)) == 1:
                self.invalidate_block_cache()
                if self._point_allowed(fx + 0.5, fz + 0.5, action="guard_lava_edge") and _is_lava_block(
                    self._get_block_name(fx, fy, fz, strict=True)
                ):
                    exposed_lava.append(flowing_lava_target)
        for dx, dz in CARDINAL_VECTORS.values():
            nx, nz = bx + dx, bz + dz
            below_feet = self._get_block_name(nx, by - 1, nz, strict=True)
            if _is_lava_block(below_feet):
                exposed_lava.append([nx, by - 1, nz])
                continue
            at_feet = self._get_block_name(nx, by, nz, strict=True)
            if _is_lava_block(at_feet):
                exposed_lava.append([nx, by, nz])
            elif (
                _is_passable(at_feet) and _is_passable(below_feet)
                and _is_lava_block(self._get_block_name(nx, by - 2, nz, strict=True))
            ):
                exposed_lava.append([nx, by - 2, nz])

        if not exposed_lava:
            return {"ok": True, "near_lava": False, "sneak": bool(st0.get("sneaking")), "sealed": 0}

        sneaking = bool(st0.get("sneaking"))
        if not sneaking:
            try:
                self.bot.call(
                    "posture",
                    {"sneak": True, "shield": bool(st0.get("blocking")), "jump": False},
                )
                sneaking = True
            except ApiError:
                pass

        if counts is None:
            try:
                counts, _, _ = self._inventory_counts_and_shield()
            except Exception:
                counts = {}
        filler = self._pick_filler_block(counts)
        sealed = 0
        if filler is not None:
            for index, lpos in enumerate(exposed_lava[:2]):
                if index:
                    current = self._safe_status()
                    if (
                        current.get("in_lava") or current.get("on_ground") is not True
                        or current.get("ready") is False or current.get("alive") is False
                        or any(current.get(key) != st0.get(key) for key in ("last_death", "dimension"))
                    ):
                        break
                if not self._point_allowed(lpos[0] + 0.5, lpos[2] + 0.5, action="guard_lava_edge"):
                    continue
                try:
                    self.bot.call("place", {"item": filler, "pos": lpos})
                    sealed += 1
                except ApiError:
                    continue
            if sealed:
                self.invalidate_block_cache()
        return {"ok": True, "near_lava": True, "sneak": sneaking, "sealed": sealed}


SUBCORTICAL_COMBAT_BUDGET_S = 7.5


class SubcorticalInstinctController:
    """Built-in 0-LLM aggressive instinct controller prior to teammate's full `combat.py` plug-in.

    Implements the `/grill-me` locked-in rules:
    1. Creeper `<= 4.5m` (or `pre_hurt_creeper`): equip/latch shield facing creeper and retreat `> 6.0m`
       along `scan_safe_yaws` (or `full_seal` if inside a `1x2` shaft).
    2. Lone melee mob (`zombie`/`spider`/etc., `hostile_count <= 2`) + `HP > 10` + sword/axe available:
       equip best melee weapon and aggressively counter-kill (`0-LLM` loop until dead or low HP).
    3. Ranged shooter (`skeleton`/etc.), swarm (`>= 3`), or low HP (`<= 10`):
       - Inside `carved_shaft` with blocks -> `full_seal` (2 blocks break LOS in 0.5s);
       - Close isolated skeleton (`<= 9m`) on open ground with shield + melee weapon + `HP >= 14` -> shield-close & kill;
       - Otherwise -> `cover_pocket`, night/low-HP `dig_safety_bunker`, or `flee` along safe yaws.
    4. Post-engagement stabilization: if safe and `HP < 16` or `food <= 15`, eat 1 safe food item.
    """

    def __init__(self) -> None:
        self._recent_flee_streak = 0
        self._last_flee_at = float("-inf")

    @staticmethod
    def _entity_dist(bot_pos: Sequence[float] | None, ent: dict[str, Any]) -> float:
        epos = ent.get("pos")
        if isinstance(bot_pos, (list, tuple)) and len(bot_pos) >= 3 and isinstance(epos, (list, tuple)) and len(epos) >= 3:
            try:
                return math.sqrt(sum((float(bot_pos[i]) - float(epos[i])) ** 2 for i in range(3)))
            except (TypeError, ValueError):
                pass
        try:
            return float(ent.get("dist", 99.0))
        except (TypeError, ValueError):
            return 99.0

    @staticmethod
    def _is_falling_fast(status: dict[str, Any], origin: Sequence[float]) -> bool:
        if status.get("on_ground") is not False or status.get("in_water") or status.get("in_lava"):
            return False
        velocity = status.get("velocity")
        if (
            isinstance(velocity, (list, tuple)) and len(velocity) >= 2
            and isinstance(velocity[1], (int, float)) and velocity[1] <= -10.0
        ):
            return True
        pos = status.get("pos")
        return (
            isinstance(pos, (list, tuple)) and len(pos) >= 3
            and isinstance(pos[1], (int, float)) and pos[1] <= origin[1] - 2.5
        )

    def _find_live_hostiles(
        self, env: CombatEnv, radius: int = 16, *, timeout_s: float | None = None,
    ) -> list[dict[str, Any]]:
        return self._scan_live_hostiles(env, radius, timeout_s=timeout_s) or []

    def _scan_live_hostiles(
        self, env: CombatEnv, radius: int, *, timeout_s: float | None = None,
    ) -> list[dict[str, Any]] | None:
        try:
            transport = {"timeout_s": timeout_s, "allow_reauth": False} if timeout_s is not None else {}
            res = env.bot.call("get_entities", {"radius": radius, "types": ["hostile"]}, **transport)
            entities = res.get("entities") if isinstance(res, dict) else None
            if not isinstance(entities, list):
                return None
            rows = [e for e in entities if isinstance(e, dict)]
            rows.sort(key=lambda e: float(e.get("dist", 999.0)))
            return rows
        except Exception as error:
            if _is_control_signal(error):
                raise
            return None

    @staticmethod
    def _other_creeper_positions(hostiles: Sequence[dict[str, Any]], target_id: int | None) -> list[Sequence[float]]:
        positions = []
        for hostile in hostiles:
            point = hostile.get("pos")
            if (
                hostile.get("id") != target_id and _clean_block_name(hostile.get("name")) == "creeper"
                and not hostile.get("blocked_by_wall")
                and isinstance(point, (list, tuple)) and len(point) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3])
            ):
                positions.append(point)
        return positions

    @staticmethod
    def _other_melee_positions(hostiles: Sequence[dict[str, Any]], target_id: int | None) -> list[Sequence[float]]:
        positions = []
        for hostile in hostiles:
            pos = hostile.get("pos")
            if (
                hostile.get("id") != target_id and _clean_block_name(hostile.get("name")) in PRE_HURT_MELEE_MOBS
                and not hostile.get("blocked_by_wall") and isinstance(pos, (list, tuple)) and len(pos) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            ):
                positions.append(pos)
        return positions

    def _equip_best_weapon_and_shield(
        self,
        env: CombatEnv,
        counts: dict[str, int],
        has_shield: bool,
        held_name: str | None,
    ) -> str | None:
        best_weapon = next((w for w in MELEE_WEAPON_ORDER if counts.get(w, 0) > 0 or held_name == w), None)
        if best_weapon and held_name != best_weapon:
            try:
                env.bot.call("equip", {"item": best_weapon, "slot": "hand"})
            except ApiError:
                pass
        if has_shield:
            try:
                inv = env.bot.call("inventory", method="GET")
                off_nm = _clean_block_name((inv.get("offhand") or {}).get("name"))
                if off_nm != "shield" and counts.get("shield", 0) > 0:
                    env.bot.call("equip", {"item": "shield", "slot": "off-hand"})
            except Exception as error:
                if _is_control_signal(error):
                    raise
        return best_weapon

    @staticmethod
    def _latch_shield_at_threat(env: CombatEnv, target_id: int | None) -> bool:
        """Face `target_id` when known and keep the shield raised."""
        if target_id is not None:
            try:
                env.bot.call("look", {"entity_id": int(target_id)})
            except (ApiError, httpx.HTTPError):
                pass
        try:
            env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
            return True
        except (ApiError, httpx.HTTPError):
            return False

    def strike_cornered_hostile(
        self, env: CombatEnv, target_id: int, counts: dict[str, int], held_name: str | None,
        *, status: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if status is None:
            status = env._safe_status({"ready": False})
        health = status.get("health")
        nearest = status.get("nearest_hostile")
        if (
            not isinstance(health, (int, float)) or not 0 < health <= 6.0
            or status.get("ready") is False or status.get("alive") is False
            or status.get("on_ground") is not True or status.get("in_water") or status.get("in_lava")
            or not isinstance(nearest, dict) or nearest.get("id") != target_id
            or self._entity_dist(status.get("pos"), nearest) > 1.25
        ):
            return None
        weapon = self._equip_best_weapon_and_shield(env, counts, False, held_name)
        try:
            result = env.bot.call(
                "attack",
                {
                    "entity_id": target_id,
                    "max_hits": 1,
                    "timeout": 300,
                    "interval_ms": 100,
                    "stop_on": ["death"],
                    "chase": False,
                    "feedback": True,
                },
                timeout_s=0.9,
                allow_reauth=False,
            )
        except ApiError as err:
            return {"action": "cornered_melee_strike", "ok": False, "code": err.code}
        return {
            "action": "cornered_melee_strike", "entity_id": target_id, "weapon": weapon,
            "ok": bool(result.get("hits")), "killed": bool(result.get("killed")),
            **({"hit_log": result["hit_log"]} if isinstance(result.get("hit_log"), list) else {}),
        }

    def _verify_warden_clearance(
        self, env: CombatEnv, before: dict[str, Any], after: dict[str, Any],
    ) -> dict[str, Any]:
        pos = after.get("pos")
        unconfirmed = {"ok": False, "reason": "warden_clearance_unconfirmed"}
        if not pos or after.get("ready") is not True or after.get("on_ground") is not True:
            return unconfirmed
        if after.get("alive") is False or any(after.get(key) != before.get(key) for key in ("dimension", "last_death")):
            return {"ok": False, "respawned": True}
        if after.get("in_lava") or after.get("in_water"):
            return unconfirmed

        hostiles = self._scan_live_hostiles(env, radius=24, timeout_s=0.4)
        if hostiles is None:
            return unconfirmed
        wardens = [entity for entity in hostiles if _clean_block_name(entity.get("name")) == "warden"]
        nearest = after.get("nearest_hostile")
        if isinstance(nearest, dict) and _clean_block_name(nearest.get("name")) == "warden":
            nearest_id = nearest.get("id")
            if nearest_id is None or not any(entity.get("id") == nearest_id for entity in wardens):
                return unconfirmed
        positions = [entity.get("pos") for entity in wardens]
        if any(
            not isinstance(point, (list, tuple)) or len(point) < 3
            or not all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3])
            for point in positions
        ):
            return unconfirmed
        distance = min((math.hypot(pos[0] - point[0], pos[2] - point[2]) for point in positions), default=math.inf)
        if distance < WARDEN_EVADE_DISTANCE:
            return {"ok": False, "reason": "warden_still_in_range", "warden_distance": round(distance, 2)}
        return {"ok": True}

    def on_encounter(self, env: CombatEnv, trigger: ThreatTrigger) -> CombatOutcome:
        actions: list[dict[str, Any]] = []
        deadline = time.monotonic() + SUBCORTICAL_COMBAT_BUDGET_S
        active_deadline = getattr(env.bot, "active_deadline", None)
        if isinstance(active_deadline, (int, float)):
            deadline = min(deadline, active_deadline)
        st = env.bot.status()
        pos = st.get("pos")
        health = st.get("health")
        if (
            st.get("ready") is False or st.get("alive") is False
            or not isinstance(pos, (list, tuple)) or len(pos) < 3
            or (isinstance(health, (int, float)) and health <= 0)
        ):
            return {
                "handled": False,
                "mode": "life_unavailable",
                "summary": "角色已死亡或状态尚未就绪，等待刷新后重新规划",
                "actions": [],
                "status_after": st,
                "requires_replan": True,
                "latched_guard": False,
            }
        if bool(st.get("in_lava")):
            esc_lava = env.escape_lava()
            st_lava_after = env.bot.status()
            return {
                "handled": bool(esc_lava.get("ok")),
                "mode": "lava_escape",
                "summary": f"处于岩浆中，优先执行脱困（ok={esc_lava.get('ok')} in_lava={esc_lava.get('in_lava')}）",
                "actions": [{"action": "escape_lava", **esc_lava}],
                "status_after": st_lava_after,
                "requires_replan": True,
                "latched_guard": False,
            }
        if not st.get("in_water") and (
            _clean_block_name(trigger.get("primary_type")) == "warden" or trigger.get("hurt_source") == "sonic_boom"
        ):
            nearby = self._find_live_hostiles(env, radius=16, timeout_s=0.6)
            warden = next((entity for entity in nearby if _clean_block_name(entity.get("name")) == "warden"), {})
            warden_pos = warden.get("pos")
            if warden_pos is None and _clean_block_name(trigger.get("primary_type")) == "warden":
                warden_pos = trigger.get("primary_pos")
            if st.get("blocking"):
                try:
                    env.bot.call("posture", {"shield": False}, timeout_s=0.35, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
            retreat = env.flee(
                warden_pos, distance=WARDEN_EVADE_DISTANCE, urgent=True, allow_shaft_seal=False,
                wait_for_ground=True, use_nearest_hostile=False, min_threat_distance=WARDEN_EVADE_DISTANCE,
            )
            after = env._safe_status({"ready": False})
            if retreat.get("ok"):
                retreat = {**retreat, **self._verify_warden_clearance(env, st, after)}
            return {
                "handled": bool(retreat.get("ok")), "mode": "warden_evade",
                "summary": "监守者威胁，已尝试沿安全落脚点撤离，先远离当前位置再恢复行动",
                "actions": [{"action": "retreat_from_warden", **retreat}], "status_after": after,
                "requires_replan": True, "latched_guard": False,
            }
        hp = float(health or 20.0)
        try:
            inv0 = env.bot.call("inventory", method="GET")
        except Exception as error:
            if _is_control_signal(error):
                raise
            inv0 = {}
        counts, has_shield, held_name = env._inventory_counts_and_shield(inv0)
        equipped_gear = []
        if isinstance(inv0, dict) and (isinstance(inv0.get("armor"), dict) or "offhand" in inv0):
            equipped_gear = auto_equip_survival_gear(env.bot, inv0, allow_armor=True, allow_shield=has_shield)

        hostiles = self._find_live_hostiles(env, radius=16)
        nh = st.get("nearest_hostile") if isinstance(st.get("nearest_hostile"), dict) else None
        primary: dict[str, Any] | None = None
        trig_id = trigger.get("primary_entity_id")
        creeper_warning = trigger.get("source") == "pre_hurt_creeper"
        confirmed_melee_hurt = (
            trigger.get("source") == "tool_interrupted_hurt"
            and trigger.get("hurt_source") in {"mob_attack", "player_attack", "spear"}
        )

        def eligible_attacker(entity: dict[str, Any]) -> bool:
            return not (
                confirmed_melee_hurt and _clean_block_name(entity.get("name")) == "creeper"
                and self._entity_dist(pos, entity) > 8.5
            )

        attackers = [entity for entity in hostiles if eligible_attacker(entity)]
        trigger_attacker = {
            "id": trig_id, "name": trigger.get("primary_type"), "pos": trigger.get("primary_pos"),
            "dist": trigger.get("distance"),
        }
        trigger_is_eligible = eligible_attacker(trigger_attacker)
        nearby_creeper = next((
            e for e in hostiles
            if _clean_block_name(e.get("name")) == "creeper" and self._entity_dist(pos, e) <= 5.5
        ), None)
        if trig_id is not None:
            primary = next((e for e in attackers if e.get("id") == trig_id), None)
        if creeper_warning and (primary is None or _clean_block_name(primary.get("name")) != "creeper"):
            # Keep the blast direction if the triggering creeper drops out of the scan.
            primary = nearby_creeper
            if (
                primary is None and nh and _clean_block_name(nh.get("name")) == "creeper"
                and (nh.get("id") == trig_id or self._entity_dist(pos, nh) <= 5.5)
            ):
                primary = nh
            if primary is None:
                primary = {
                    "id": trig_id, "name": "creeper", "pos": trigger.get("primary_pos"), "dist": trigger.get("distance"),
                }
        elif not creeper_warning and nearby_creeper is not None:
            primary = nearby_creeper
        if primary is None and confirmed_melee_hurt and trigger_is_eligible and trig_id is not None:
            primary = trigger_attacker
        if primary is None and attackers:
            primary = attackers[0]
        if primary is None and nh and nh.get("name") and eligible_attacker(nh):
            primary = nh

        fallback_attacker = trigger_attacker if trigger_is_eligible else {}
        mob_name = _clean_block_name((primary or {}).get("name") or fallback_attacker.get("name") or "")
        mob_id = primary.get("id") if primary is not None else fallback_attacker.get("id")
        mob_pos = (primary or {}).get("pos")
        if not mob_pos and (mob_name != "creeper" or mob_id == trig_id):
            mob_pos = fallback_attacker.get("pos")
        try:
            mob_dist = float((primary or {}).get("dist") or trigger.get("distance") or 99.0)
        except (TypeError, ValueError):
            mob_dist = 99.0

        any_creeper_close = mob_name == "creeper"
        # Count close-range engaged hostiles (<=8.5m) for swarm/solo gates so mobs outside enclosed
        # walls (`10..16m`) never trick a sword+shield bot into pure circle-fleeing inside a room.
        nearby_hostiles = [e for e in hostiles if float(e.get("dist", 99.0)) <= 8.5]
        other_melee = (
            self._other_melee_positions(nearby_hostiles, mob_id)
            if any_creeper_close or mob_name in PRE_HURT_MELEE_MOBS else []
        )
        mode = "stabilize"
        latched_guard = False
        enclosure = None

        in_water = bool(st.get("in_water"))
        submerged = bool(st.get("submerged"))
        oxy_raw = st.get("oxygen")
        low_oxygen = isinstance(oxy_raw, (int, float)) and oxy_raw < 180

        if (
            not in_water
            and has_shield
            and not st.get("blocking")
            and not (any_creeper_close or creeper_warning)
            and trigger.get("source") in {"pre_hurt_ranged", "pre_hurt_melee"}
        ):
            if mob_id is not None:
                try:
                    env.bot.call("look", {"entity_id": int(mob_id)})
                except ApiError:
                    pass
            try:
                env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                actions.append({"action": "raise_shield_entry", "ok": True})
            except ApiError:
                pass

        # 0) Underwater drowning emergency: surface immediately instead of burning oxygen in combat!
        if in_water and (submerged or low_oxygen):
            mode = "water_surface"
            esc = env.surface_from_water(st)
            actions.append({"action": "water_surface", **esc})
            if not esc.get("respawned") and (not esc.get("ok") or esc.get("in_water")):
                shore = env.escape_deep_water_to_shore(mob_pos, keep_shield=has_shield, shield_target_id=mob_id)
                actions.append({"action": "water_surface_shore", "ok": bool(shore.get("ok"))})

        # 1) Creeper emergency: NEVER melee in blast radius — shield & retreat / seal shaft
        elif any_creeper_close or creeper_warning:
            mode = "creeper_evade"
            creeper_ent = primary
            c_pos = mob_pos
            if has_shield:
                offhand = inv0.get("offhand") if isinstance(inv0, dict) else None
                offhand_name = _clean_block_name(offhand.get("name")) if isinstance(offhand, dict) else ""
                if offhand_name != "shield" and counts.get("shield", 0) > 0 and "equip:shield" not in equipped_gear:
                    try:
                        env.bot.call("equip", {"item": "shield", "slot": "off-hand"})
                    except (ApiError, httpx.HTTPError):
                        pass
                raised = self._latch_shield_at_threat(env, mob_id)
                actions.append({"action": "raise_shield_creeper", "ok": raised})
            snap = env.arena_snapshot(pos)
            sealed_ok = False
            if snap["enclosure"] == "carved_shaft" and env._pick_filler_block(counts) is not None:
                seal = env.fortify(st, lambda: env.build_tactical("full_seal", threat_pos=c_pos))
                sealed_ok = bool(seal.get("ok"))
                actions.append({
                    "action": "seal_creeper_shaft", "ok": sealed_ok,
                    **({"reason": seal["reason"]} if seal.get("reason") else {}),
                })
                if sealed_ok:
                    st_chk_seal = env.bot.status()
                    post_creepers = [
                        e
                        for e in self._find_live_hostiles(env, radius=8)
                        if _clean_block_name(e.get("name")) == "creeper"
                        and self._entity_dist(st_chk_seal.get("pos"), e) <= 4.5
                    ]
                    if post_creepers:
                        sealed_ok = False
            if not sealed_ok:
                other_creepers = self._other_creeper_positions(hostiles, mob_id)
                rank_melee_clearance = bool(other_melee) and not any(
                    hostile is not primary and _clean_block_name(hostile.get("name")) == "creeper"
                    for hostile in nearby_hostiles
                )
                flee_res = env.flee(
                    c_pos, distance=11.5, urgent=True, allow_shaft_seal=False, wait_for_ground=True,
                    **({"other_creepers": other_creepers} if other_creepers and c_pos else {}),
                    **({"other_melee": other_melee, "avoid_blast": True} if rank_melee_clearance else {}),
                    **({"initial_landing_checks": 5} if not has_shield and st.get("on_ground") is False else {}),
                    **({"use_nearest_hostile": False} if not c_pos else {}),
                )
                st_chk = env._safe_status({**st, "ready": False})
                changed_life = (
                    bool(flee_res.get("respawned")) or st_chk.get("alive") is False
                    or st_chk.get("last_death") != st.get("last_death")
                    or st_chk.get("dimension") != st.get("dimension")
                )
                can_continue = not changed_life and st_chk.get("ready") is not False and bool(st_chk.get("pos"))
                post_scan = self._scan_live_hostiles(env, radius=8, timeout_s=0.6) if can_continue else None
                post_h = post_scan or []
                close_creeper = next(
                    (
                        e
                        for e in post_h
                        if _clean_block_name(e.get("name")) == "creeper"
                        and self._entity_dist(st_chk.get("pos"), e) <= 5.5
                    ),
                    None,
                )
                if close_creeper is None and can_continue:
                    nh_chk = st_chk.get("nearest_hostile")
                    if (
                        isinstance(nh_chk, dict)
                        and _clean_block_name(nh_chk.get("name")) == "creeper"
                        and self._entity_dist(st_chk.get("pos"), nh_chk) <= 5.5
                    ):
                        close_creeper = nh_chk
                retreat_ok = bool(flee_res.get("ok"))
                clearance_confirmed = post_scan is not None and close_creeper is None
                retreat_reason = flee_res.get("reason")
                if retreat_ok and can_continue and post_scan is None and close_creeper is None:
                    retreat_reason = "blast_clearance_unconfirmed"
                actions.append({
                    "action": "retreat_from_creeper", "ok": retreat_ok and can_continue and clearance_confirmed,
                    "moved": flee_res.get("moved_dist"),
                    **({"reason": retreat_reason} if retreat_reason else {}),
                    **({"creeper_distance": self._entity_dist(st_chk.get("pos"), close_creeper)} if close_creeper else {}),
                    **({"respawned": True} if changed_life else {}),
                })
                close_pos = close_creeper.get("pos") if close_creeper else None
                if (
                    st_chk.get("in_water") and not st_chk.get("in_lava")
                    and can_continue
                    and time.monotonic() < deadline
                ):
                    shore = env.escape_deep_water_to_shore(
                        close_pos or c_pos, keep_shield=has_shield,
                        shield_target_id=(close_creeper or creeper_ent or {}).get("id"),
                        avoid_close_threat=True,
                    )
                    actions.append({"action": "creeper_water_retreat", **shore})
                    st_chk = env._safe_status({"ready": False})
                    latched_guard = bool(
                        has_shield and st_chk.get("blocking") and not shore.get("respawned")
                        and st_chk.get("ready") is not False and st_chk.get("alive") is not False
                        and st_chk.get("last_death") == st.get("last_death")
                        and st_chk.get("dimension") == st.get("dimension")
                    )
                elif (
                    not has_shield and isinstance(close_pos, (list, tuple)) and len(close_pos) >= 3
                    and can_continue and retreat_made_progress(retreat_ok, flee_res.get("moved_dist"), retreat_reason)
                    and st_chk.get("on_ground") is True and not st_chk.get("in_water") and not st_chk.get("in_lava")
                    and time.monotonic() < deadline
                ):
                    other_close_creepers = self._other_creeper_positions(post_h, close_creeper.get("id"))
                    pursuers = self._other_melee_positions(post_h, close_creeper.get("id"))
                    followup = env.flee(
                        close_pos, distance=6.0, urgent=True, allow_shaft_seal=False, wait_for_ground=True,
                        other_creepers=other_close_creepers, keep_blast_clearance=True,
                        **({"other_melee": pursuers} if pursuers else {}),
                    )
                    if followup.get("ok"):
                        after = env._safe_status({"ready": False})
                        if after.get("alive") is False or after.get("last_death") != st.get("last_death"):
                            followup.update(ok=False, respawned=True)
                        elif after.get("ready") is False or after.get("dimension") != st.get("dimension"):
                            followup.update(ok=False, reason="blast_clearance_unconfirmed")
                        else:
                            scan = self._scan_live_hostiles(env, radius=8, timeout_s=0.6)
                            threats = list(scan or [])
                            nearest = after.get("nearest_hostile")
                            if isinstance(nearest, dict):
                                threats.append(nearest)
                            creeper_distance = min((
                                self._entity_dist(after.get("pos"), threat) for threat in threats
                                if _clean_block_name(threat.get("name")) == "creeper"
                            ), default=math.inf)
                            if creeper_distance <= 5.5:
                                followup.update(ok=False, reason="creeper_still_close", creeper_distance=creeper_distance)
                            elif scan is None:
                                followup.update(ok=False, reason="blast_clearance_unconfirmed")
                    actions.append({"action": "creeper_followup_retreat", **followup})
                if (
                    (not retreat_ok or not clearance_confirmed)
                    and can_continue
                    and has_shield
                    and not st_chk.get("in_water")
                ):
                    cid = (close_creeper or creeper_ent or {}).get("id")
                    latched_guard = self._latch_shield_at_threat(env, int(cid) if cid is not None else None)
                elif retreat_ok and can_continue and has_shield and st_chk.get("blocking"):
                    nearest = st_chk.get("nearest_hostile")
                    nearest = nearest if isinstance(nearest, dict) else {}
                    health_after = st_chk.get("health")
                    name = _clean_block_name(nearest.get("name"))
                    distance = self._entity_dist(st_chk.get("pos"), nearest)
                    guard_melee = name in PRE_HURT_MELEE_MOBS and distance <= 5.5
                    guard_ranged = name in RANGED_MOBS and distance <= 16.0 and not nearest.get("blocked_by_wall")
                    if (
                        isinstance(health_after, (int, float)) and 0 < health_after <= 8.0
                        and st_chk.get("on_ground") is True and not (st_chk.get("in_water") or st_chk.get("in_lava"))
                        and st_chk.get("ready") is not False and st_chk.get("alive") is not False and st_chk.get("pos")
                        and all(st_chk.get(key) == st.get(key) for key in ("last_death", "dimension"))
                        and isinstance(nearest.get("id"), int) and (guard_melee or guard_ranged)
                    ):
                        latched_guard = self._latch_shield_at_threat(env, nearest["id"])
                        action = "guard_ranged_after_creeper_retreat" if guard_ranged else "guard_melee_after_creeper_retreat"
                        actions.append({"action": action, "ok": latched_guard, "entity_id": nearest["id"]})

        # 2) Lone/pair melee hostile + healthy + weapon -> 0-LLM counter-kill loop!
        elif (
            mob_name in LONE_COUNTERKILL_MOBS
            and hp > 10.0
            and len(nearby_hostiles) <= 2
            and any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
        ):
            weapon = self._equip_best_weapon_and_shield(env, counts, has_shield, held_name)
            mode = "counter_kill"
            killed = False
            retreat_action = None
            for _ in range(5):
                if time.monotonic() >= deadline:
                    break
                st_cur = env.bot.status()
                if (
                    st_cur.get("ready") is False or st_cur.get("alive") is False
                    or any(st_cur.get(key) != st.get(key) for key in ("dimension", "last_death"))
                ):
                    mode = "life_unavailable"
                    break
                if self._is_falling_fast(st_cur, pos):
                    mode = "falling"
                    break
                hp_cur = float(st_cur.get("health") or 20.0)
                oxy_cur = st_cur.get("oxygen")
                if st_cur.get("in_water") and (
                    st_cur.get("submerged") or (isinstance(oxy_cur, (int, float)) and oxy_cur < 150)
                ):
                    esc_cur = env.surface_from_water(st_cur)
                    actions.append({"action": "water_surface_abort", **esc_cur})
                    st_w = env._safe_status(st_cur)
                    if not esc_cur.get("respawned") and (
                        not esc_cur.get("ok") or esc_cur.get("in_water") or st_w.get("in_water")
                    ):
                        shore_w = env.escape_deep_water_to_shore(mob_pos, keep_shield=has_shield, shield_target_id=mob_id)
                        actions.append({"action": "water_surface_shore", "ok": bool(shore_w.get("ok"))})
                        st_w = env._safe_status(st_w)
                    nh_w = st_w.get("nearest_hostile")
                    nh_w_dist = float(nh_w.get("dist", 99.0)) if isinstance(nh_w, dict) and nh_w.get("name") else 99.0
                    if has_shield and st_w.get("alive") is not False and not st_w.get("in_water") and (nh_w_dist <= 8.0 or not esc_cur.get("ok")):
                        tid_w = nh_w.get("id") if isinstance(nh_w, dict) and nh_w.get("id") is not None else mob_id
                        latched_guard = self._latch_shield_at_threat(env, int(tid_w) if tid_w is not None else None)
                    mode = "water_surface_abort"
                    break
                if hp_cur <= 8.0:
                    flee_low = env.flee(mob_pos, distance=12.0, urgent=True)
                    actions.append({"action": "low_hp_abort_flee", "ok": bool(flee_low.get("ok"))})
                    if not flee_low.get("ok") and not flee_low.get("respawned") and has_shield:
                        st_lw = env._safe_status(st_cur)
                        if st_lw.get("alive") is not False and not st_lw.get("in_water"):
                            latched_guard = self._latch_shield_at_threat(env, int(mob_id) if mob_id is not None else None)
                    mode = "low_hp_disengage"
                    break
                sighting = self._scan_live_hostiles(env, radius=10)
                cur_hostiles = sighting or []
                if any(_clean_block_name(e.get("name")) == "creeper" and float(e.get("dist", 99.0)) <= 5.0 for e in cur_hostiles):
                    flee_cr = env.flee(mob_pos, distance=11.0, urgent=True)
                    actions.append({"action": "creeper_intruded_flee", "ok": bool(flee_cr.get("ok"))})
                    mode = "creeper_evade"
                    break
                target_ent = (
                    next((e for e in cur_hostiles if e.get("id") == mob_id), None)
                    if mob_id is not None
                    else (cur_hostiles[0] if cur_hostiles else None)
                )
                if target_ent is None and mob_id is None:
                    killed = True
                    break
                if target_ent is None and sighting is not None:
                    actions.append({"action": "melee_attack", "entity_id": mob_id, "ok": False, "code": "target_not_visible"})
                    break
                eff_id = int(target_ent["id"]) if isinstance(target_ent, dict) and target_ent.get("id") is not None else mob_id
                if eff_id is None:
                    break
                if target_ent is not None and target_ent.get("blocked_by_wall") is True:
                    actions.append({"action": "melee_attack", "entity_id": eff_id, "ok": False, "code": "blocked_by_wall"})
                    break
                # Keep part of the slot budget for the reply, final status and control release.
                remaining = deadline - time.monotonic()
                if remaining < 1.5:
                    break
                attack_ms = min(2600, int((remaining - 0.9) * 1000))
                vulnerable_counter = not has_shield and hp_cur <= 12.0
                stop_on = ["hurt", "death"] if vulnerable_counter else ["death"]
                attack_body: dict[str, Any] = {
                    "entity_id": eff_id, "timeout": attack_ms, "stop_on": stop_on, "feedback": True,
                }
                target_distance = self._entity_dist(st_cur.get("pos"), target_ent) if target_ent is not None else math.inf
                if target_distance <= 3.2:
                    attack_body["chase"] = False
                if target_distance <= 2.5:
                    # A close counter can return after two swings. Reserve the longest sword/axe
                    # interval (1250 ms) for a final server-side sleep if the target slips out of reach.
                    attack_body.update(timeout=max(300, attack_ms - 1250), max_hits=2)
                try:
                    atk = env.bot.call(
                        "attack", attack_body,
                        timeout_s=attack_ms / 1000 + 0.35, allow_reauth=False,
                    )
                    hits = atk.get("hits")
                    actions.append({
                        "action": "melee_attack", "weapon": weapon, "entity_id": eff_id,
                        "ok": True, "killed": bool(atk.get("killed")),
                        **({"hits": hits} if isinstance(hits, (int, float)) and math.isfinite(hits) else {}),
                        **({"chase": False} if attack_body.get("chase") is False else {}),
                        **({"hit_log": atk["hit_log"]} if isinstance(atk.get("hit_log"), list) else {}),
                    })
                    if atk.get("killed"):
                        killed = True
                        break
                    if vulnerable_counter and atk.get("interrupted_by") == "hurt":
                        retreat_action = "low_hp_abort_flee"
                        break
                    if isinstance(hits, (int, float)) and hits == 0:
                        retreat_action = "unreachable_flee"
                        break
                except ApiError as err:
                    actions.append({"action": "melee_attack", "ok": False, "code": err.code})
                    if vulnerable_counter and err.code in {"hurt", "interrupted"}:
                        retreat_action = "low_hp_abort_flee"
                        break
                    if err.code in {"entity_not_found", "invalid_entity", "target_dead"}:
                        killed = True
                        break
                    if err.code in {"timeout", "attack_zero_hits", "path_not_found", "no_path"}:
                        retreat_action = "attack_timeout_flee"
                    break
                if not cur_hostiles:
                    killed = True
                    break
            if retreat_action is not None:
                after_attack = env._safe_status({})
                interrupted_by_hurt = retreat_action == "low_hp_abort_flee"
                mode = "low_hp_disengage" if interrupted_by_hurt else "cover_or_flee"
                if (
                    not after_attack.get("pos") or after_attack.get("ready") is False or after_attack.get("alive") is False
                    or any(after_attack.get(key) != st.get(key) for key in ("dimension", "last_death"))
                ):
                    mode = "life_unavailable"
                elif self._is_falling_fast(after_attack, pos):
                    mode = "falling"
                elif not interrupted_by_hurt or not (after_attack.get("in_water") or after_attack.get("in_lava")):
                    retreat = env.flee(
                        mob_pos, distance=12.0 if interrupted_by_hurt else 10.0, urgent=True,
                        **({"wait_for_ground": True} if interrupted_by_hurt else {}),
                    )
                    actions.append({"action": retreat_action, "ok": bool(retreat.get("ok"))})
            elif killed:
                mode = "counter_killed"
                self._recent_flee_streak = 0

        # 3) Ranged shooter / swarm / low-HP / unarmed -> seal shaft, close-rush with shield, bunker, or cover/flee
        else:
            self._equip_best_weapon_and_shield(env, counts, has_shield, held_name)
            snap = env.arena_snapshot(pos)
            enclosure = snap["enclosure"]
            has_weapon = any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
            if snap["enclosure"] == "carved_shaft" and env._pick_filler_block(counts) is not None:
                seal = env.fortify(st, lambda: env.build_tactical("full_seal", threat_pos=mob_pos))
                actions.append({
                    "action": "seal_shaft", "ok": bool(seal.get("ok")),
                    **({"reason": seal["reason"]} if seal.get("reason") else {}),
                })
                mode = "sealed_shaft" if seal.get("ok") else "flee"
                if not seal.get("ok"):
                    fl = env.flee(mob_pos, distance=12.0, urgent=True, allow_shaft_seal=False)
                    actions.append({
                        "action": "flee", "ok": bool(fl.get("ok")), "moved": fl.get("moved_dist"),
                        **({"reason": fl["reason"]} if fl.get("reason") else {}),
                        **({"respawned": True} if fl.get("respawned") else {}),
                    })
                    if has_shield and not fl.get("respawned"):
                        landing = env._safe_status({"ready": False})
                        nearest = landing.get("nearest_hostile")
                        nearest = nearest if isinstance(nearest, dict) else {}
                        name = _clean_block_name(nearest.get("name"))
                        distance = self._entity_dist(landing.get("pos"), nearest)
                        close_melee = name in PRE_HURT_MELEE_MOBS and distance <= 3.5
                        health = landing.get("health")
                        exposed_to_shooter = (
                            not fl.get("ok") and landing.get("blocking") is False
                            and mob_name in RANGED_MOBS and name in RANGED_MOBS and distance <= 8.0
                            and isinstance(health, (int, float)) and 0 < health <= 10.0
                        )
                        if (
                            (close_melee or exposed_to_shooter) and isinstance(nearest.get("id"), int)
                            and landing.get("ready") is not False and landing.get("alive") is not False
                            and not landing.get("in_water") and not landing.get("in_lava")
                            and landing.get("last_death") == st.get("last_death")
                            and landing.get("dimension") == st.get("dimension")
                        ):
                            latched_guard = self._latch_shield_at_threat(env, nearest["id"])
                            action = "guard_melee_after_retreat" if close_melee else "guard_ranged_after_failed_seal"
                            actions.append({"action": action, "ok": latched_guard, "entity_id": nearest["id"]})
            elif (
                mob_name in RANGED_MOBS
                and mob_name != "blaze"
                and trigger.get("source") != "guard_poll_hurt"
                and trigger.get("hurt_source") not in {"arrow", "trident", "fireball", "small_fireball", "wither_skull"}
                and has_shield
                and has_weapon
                and hp >= 14.0
                and len(nearby_hostiles) <= 1
                and mob_dist <= 5.0
                and mob_id is not None
            ):
                mode = "shield_rush_ranged"
                try:
                    env.bot.call("look", {"entity_id": int(mob_id)})
                    env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                    env.chase_to_range(int(mob_id), range_m=2.6, timeout_ms=1400)
                    st_mid_rush = env.bot.status()
                    hp_mid_rush = float(st_mid_rush.get("health") or 20.0)
                    if st_mid_rush.get("alive") is False or hp_mid_rush <= 10.0 or st_mid_rush.get("in_water"):
                        fl_abort = env.flee(mob_pos, distance=12.0, urgent=True)
                        actions.append({"action": "rush_low_hp_flee", "ok": bool(fl_abort.get("ok"))})
                        mode = "cover_or_flee"
                    else:
                        env.bot.call("posture", {"shield": False, "sneak": False, "jump": False})
                        for _ in range(3):
                            remaining = deadline - time.monotonic()
                            if remaining < 1.5:
                                break
                            attack_ms = min(2400, int((remaining - 0.9) * 1000))
                            atk_r = env.bot.call(
                                "attack", {"entity_id": int(mob_id), "timeout": attack_ms, "stop_on": ["death"]},
                                timeout_s=attack_ms / 1000 + 0.35, allow_reauth=False,
                            )
                            hits_r = atk_r.get("hits")
                            killed_r = bool(atk_r.get("killed"))
                            actions.append({"action": "shield_rush_attack", "ok": True, "killed": killed_r})
                            if killed_r or (isinstance(hits_r, (int, float)) and hits_r == 0):
                                break
                            st_rk = env.bot.status()
                            if st_rk.get("alive") is False or float(st_rk.get("health") or 20.0) <= 10.0:
                                break
                except ApiError as err:
                    actions.append({"action": "shield_rush_attack", "ok": False, "code": err.code})
                    if err.code not in {"no_such_entity", "entity_not_found", "invalid_entity", "target_dead"}:
                        fl = env.flee(mob_pos, distance=12.0, urgent=True)
                        actions.append({"action": "fallback_flee", "ok": bool(fl.get("ok"))})
            else:
                mode = "cover_or_flee"
                covered = False
                exposed_melee = (
                    mob_name in PRE_HURT_MELEE_MOBS and mob_dist <= 3.5
                    and (trigger.get("source") == "pre_hurt_melee" or confirmed_melee_hurt)
                    and not (primary or {}).get("blocked_by_wall")
                )
                if (
                    snap["enclosure"] in {"open_cave", "carved_shaft"}
                    and env.skills is not None
                    and not exposed_melee
                    and mob_name not in {"blaze", "ghast"}
                    and trigger.get("hurt_source") not in {"fireball", "small_fireball", "wither_skull"}
                ):
                    cov = env.fortify(st, lambda: env.build_tactical("cover_pocket"))
                    if cov.get("ok"):
                        covered = True
                        mode = "cover_pocket"
                        actions.append({"action": "cover_pocket", "ok": True})
                    elif cov.get("reason") in {"cover_not_entered", "fortification_budget"}:
                        actions.append({"action": "cover_pocket", "ok": False, "reason": cov["reason"]})
                now_flee = time.monotonic()
                if now_flee - self._last_flee_at > 25.0:
                    self._recent_flee_streak = 0
                # A vertical bunker takes several digs while a surface attacker can close in.
                exposed_to_lethal_melee = (
                    snap["enclosure"] == "surface"
                    and hp <= 4.0
                    and not has_shield
                    and exposed_melee
                )
                should_bunker = (
                    not covered
                    and not exposed_to_lethal_melee
                    and env.skills is not None
                    and hasattr(env.skills, "dig_safety_bunker")
                    and not any_creeper_close
                    and st.get("on_ground") is True
                    and (
                        hp <= 10.0
                        or (
                            not has_weapon
                            and st.get("is_day") is False
                            and (hp <= 14.0 or len(hostiles) >= 2 or self._recent_flee_streak >= 1)
                        )
                    )
                )
                if should_bunker:
                    try:
                        bunk = env.fortify(st, lambda: env.skills.dig_safety_bunker(urgent=True))
                    except ApiError as err:
                        if err.code != "missing_item":
                            raise
                        actions.append({"action": "emergency_bunker", "ok": False, "code": err.code})
                    else:
                        if bunk.get("ok"):
                            covered = True
                            mode = "bunker_sealed"
                            self._recent_flee_streak = 0
                            actions.append({"action": "emergency_bunker", "ok": True})
                        elif bunk.get("reason") == "fortification_budget":
                            actions.append({"action": "emergency_bunker", "ok": False, "reason": bunk["reason"]})
                if not covered:
                    if has_shield and mob_id is not None:
                        try:
                            env.bot.call("look", {"entity_id": int(mob_id)})
                            env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                        except (ApiError, httpx.HTTPError):
                            pass
                    other_shooters = []
                    if not has_shield and (mob_name in RANGED_MOBS or exposed_melee):
                        shooters = [
                            hostile for hostile in hostiles if hostile is not primary
                            and (not exposed_melee or not hostile.get("blocked_by_wall"))
                        ]
                        other_shooters = [
                            shooter_pos for _, shooter_pos in ranged_retreat_positions(
                                pos, shooters, max_distance=12.0,
                            )
                        ]
                    unsettled_under_attack = st.get("on_ground") is False and (
                        exposed_melee or trigger.get("source") == "tool_interrupted_hurt"
                    )
                    fl = env.flee(
                        mob_pos, distance=14.0, urgent=True,
                        wait_for_ground=mob_name in RANGED_MOBS or hp <= 10.0 or not has_weapon or unsettled_under_attack,
                        **({"use_nearest_hostile": False} if not trigger_is_eligible else {}),
                        **({"other_shooters": other_shooters} if other_shooters else {}),
                        **({"primary_melee": True} if exposed_melee and other_shooters else {}),
                        **({"other_melee": other_melee} if other_melee else {}),
                    )
                    flee_ok = bool(fl.get("ok"))
                    sealed = flee_ok and fl.get("mode") == "sealed" and not fl.get("respawned")
                    if sealed:
                        mode = "sealed_shaft"
                        actions.append({"action": "seal_shaft", "ok": True})
                        self._recent_flee_streak = 0
                    else:
                        actions.append({
                            "action": "flee", "ok": flee_ok, "moved": fl.get("moved_dist"),
                            **({"reason": fl["reason"]} if fl.get("reason") else {}),
                            **({"respawned": True} if fl.get("respawned") else {}),
                        })
                        self._recent_flee_streak += 1
                        self._last_flee_at = now_flee
                    stalled_under_arrows = (
                        not flee_ok and fl.get("reason") == "no_safe_retreat" and hp <= 4.0
                        and trigger.get("hurt_source") == "arrow"
                    )
                    moved = fl.get("moved_dist")
                    short_ranged_retreat = (
                        flee_ok and not sealed and trigger.get("source") == "pre_hurt_ranged"
                        and isinstance(moved, (int, float)) and 0 < moved < 2.0
                    )
                    if (
                        (stalled_under_arrows or short_ranged_retreat) and not fl.get("respawned")
                        and not has_shield and mob_name in RANGED_MOBS
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                        and isinstance(mob_pos, (list, tuple)) and len(mob_pos) >= 3
                        and time.monotonic() + 2.0 < deadline
                        and not any(
                            _clean_block_name(hostile.get("name")) in PRE_HURT_MELEE_MOBS
                            and self._entity_dist(pos, hostile) <= 3.5
                            for hostile in nearby_hostiles
                        )
                    ):
                        cover_after_retreat = stalled_under_arrows
                        if short_ranged_retreat:
                            landing = env._safe_status({"ready": False})
                            landing_hp = landing.get("health")
                            nearest = landing.get("nearest_hostile")
                            close_melee = (
                                isinstance(nearest, dict) and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                                and self._entity_dist(landing.get("pos"), nearest) <= 3.5
                            )
                            cover_after_retreat = (
                                isinstance(landing_hp, (int, float)) and 0 < landing_hp <= 7.0
                                and landing.get("on_ground") is True and landing.get("ready") is not False
                                and landing.get("alive") is not False and not (landing.get("in_water") or landing.get("in_lava"))
                                and landing.get("last_death") == st.get("last_death") and landing.get("dimension") == st.get("dimension")
                                and not close_melee and time.monotonic() + 2.0 < deadline
                            )
                        natural_cover = None
                        if cover_after_retreat:
                            natural_cover = env.seek_natural_cover(
                                mob_pos, other_shooters=other_shooters, allow_stationary=short_ranged_retreat,
                            )
                        if natural_cover is not None:
                            actions.append({"action": "natural_cover", **natural_cover})
                            if natural_cover.get("ok"):
                                mode = "natural_cover"
                                self._recent_flee_streak = 0
                    check_ranged_guard = flee_ok and fl.get("mode") == "flee" and mob_name in RANGED_MOBS
                    check_melee_guard = flee_ok and (sealed or fl.get("mode") == "flee") and mob_name in PRE_HURT_MELEE_MOBS
                    if (not flee_ok or check_ranged_guard or check_melee_guard) and not fl.get("respawned") and has_shield:
                        st_fl = env._safe_status({"ready": False})
                        if (
                            not flee_ok and fl.get("reason") == "no_safe_retreat"
                            and st_fl.get("blocking") and st_fl.get("dimension") == st.get("dimension") == "overworld"
                            and st_fl.get("last_death") == st.get("last_death")
                            and held_name in MELEE_WEAPON_ORDER and mob_name in LONE_COUNTERKILL_MOBS
                            and isinstance(mob_id, int) and len(nearby_hostiles) == 1
                            and nearby_hostiles[0].get("id") == mob_id and not nearby_hostiles[0].get("blocked_by_wall")
                            and time.monotonic() + 1.25 < deadline
                        ):
                            strike = self.strike_cornered_hostile(env, mob_id, counts, held_name, status=st_fl)
                            if strike is not None:
                                actions.append(strike)
                                st_fl = env._safe_status({"ready": False})
                        nearest_raw = st_fl.get("nearest_hostile")
                        nearest = nearest_raw if isinstance(nearest_raw, dict) else {}
                        nearest_id = nearest.get("id")
                        nearest_dist = nearest.get("dist")
                        low_health_after = isinstance(st_fl.get("health"), (int, float)) and st_fl["health"] <= 10.0
                        ranged_still_exposed = (
                            isinstance(nearest_id, int)
                            and isinstance(nearest_dist, (int, float))
                            and (
                                (check_ranged_guard and low_health_after and nearest_id == mob_id and nearest_dist <= 12.0)
                                or (
                                    (check_ranged_guard or (check_melee_guard and low_health_after))
                                    and st_fl.get("blocking") and nearest_dist <= 3.5
                                    and _clean_block_name(nearest.get("name")) in RANGED_MOBS
                                )
                            )
                        )
                        same_close_pursuit = (
                            trigger.get("source") == "pre_hurt_melee" and 0 < mob_dist <= 1.25
                            and nearest_id == mob_id and nearest.get("blocked_by_wall") is not True
                        )
                        melee_still_close = (
                            check_melee_guard and (low_health_after or sealed or same_close_pursuit) and st_fl.get("blocking")
                            and isinstance(nearest_id, int) and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                            and isinstance(nearest_dist, (int, float)) and nearest_dist <= 3.5
                        )
                        if (
                            (not flee_ok or ranged_still_exposed or melee_still_close)
                            and st_fl.get("ready") is not False
                            and st_fl.get("alive") is not False
                            and not st_fl.get("in_water")
                            and not st_fl.get("in_lava")
                            and st_fl.get("last_death") == st.get("last_death")
                            and st_fl.get("dimension") == st.get("dimension")
                        ):
                            guard_target = nearest_id if ranged_still_exposed or melee_still_close else mob_id
                            latched_guard = self._latch_shield_at_threat(
                                env,
                                int(guard_target) if guard_target is not None else None,
                            )
                            if melee_still_close:
                                actions.append({"action": "guard_melee_after_retreat", "ok": latched_guard, "entity_id": guard_target})
                            elif check_melee_guard and ranged_still_exposed:
                                actions.append({"action": "guard_ranged_after_melee_retreat", "ok": latched_guard, "entity_id": guard_target})
                    elif (
                        not flee_ok and fl.get("reason") in {"no_safe_retreat", "unsafe_landing"}
                        and not fl.get("respawned") and not has_shield and has_weapon
                        and mob_name in LONE_COUNTERKILL_MOBS and isinstance(mob_id, int)
                        and len(nearby_hostiles) == 1 and time.monotonic() < deadline
                    ):
                        strike = self.strike_cornered_hostile(env, mob_id, counts, held_name)
                        if strike is not None:
                            actions.append(strike)

        # 4) Check for a surviving attacker before handing back control or eating.
        has_weapon = any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
        st_post = env._safe_status(st)
        if mode == "creeper_evade" and (
            st_post.get("alive") is False
            or st_post.get("last_death") != st.get("last_death")
            or st_post.get("dimension") != st.get("dimension")
        ):
            for action in actions:
                if action["action"] in {"retreat_from_creeper", "creeper_followup_retreat", "creeper_intruded_flee"}:
                    action.update(ok=False, respawned=True)
        nh_post = st_post.get("nearest_hostile")
        retreat_hp = st_post.get("health")
        retreats = [a for a in actions if a["action"] == "flee" and not a.get("respawned")]
        night_pursuit = (
            enclosure == "surface" and st_post.get("is_day") is False
            and trigger.get("source") == "pre_hurt_melee" and mob_name in PRE_HURT_MELEE_MOBS
            and 0 < mob_dist <= 3.5
            and isinstance(retreat_hp, (int, float)) and retreat_hp > 12.0
            and any(a["ok"] and isinstance(a.get("moved"), (int, float)) and a["moved"] >= 4.0 for a in retreats)
        )
        followup_range = 0.0
        if night_pursuit:
            followup_range = 8.0
        elif isinstance(retreat_hp, (int, float)) and 0.0 < retreat_hp <= 12.0:
            followup_range = 3.5 if retreat_hp <= 10.0 else 1.5
        elif (
            isinstance(retreat_hp, (int, float)) and retreat_hp > 12.0
            and trigger.get("source") == "pre_hurt_melee" and mob_name in PRE_HURT_MELEE_MOBS
            and 0 < mob_dist <= 3.5
            and isinstance(nh_post, dict) and nh_post.get("id") == mob_id
            and any(a["ok"] and isinstance(a.get("moved"), (int, float)) and 0 < a["moved"] <= 2.0 for a in retreats)
        ):
            # A short step does not disengage when the same attacker stays in melee reach.
            followup_range = min(mob_dist, 2.0)
        if (
            mode in {"cover_or_flee", "flee"} and not has_shield and not has_weapon
            and followup_range > 0
            and any(retreat_made_progress(a["ok"], a.get("moved"), a.get("reason")) for a in retreats)
            and isinstance(nh_post, dict) and _clean_block_name(nh_post.get("name")) in PRE_HURT_MELEE_MOBS
            and isinstance(nh_post.get("id"), int) and self._entity_dist(st_post.get("pos"), nh_post) <= followup_range
            and st_post.get("on_ground") is True and st_post.get("ready") is not False and st_post.get("alive") is not False
            and not st_post.get("in_water") and not st_post.get("in_lava")
            and st_post.get("last_death") == st.get("last_death") and st_post.get("dimension") == st.get("dimension")
            and time.monotonic() + (4.1 if night_pursuit else 1.5) < deadline
        ):
            target_id = nh_post["id"]
            live_hostiles = self._find_live_hostiles(env, radius=12 if night_pursuit else 5, timeout_s=0.6)
            target = next((entity for entity in live_hostiles if entity.get("id") == target_id), {})
            target_pos = target.get("pos")
            if (
                st_post.get("pos")
                and time.monotonic() + (3.5 if night_pursuit else 0.8) < deadline
                and isinstance(target_pos, (list, tuple)) and len(target_pos) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in target_pos[:3])
                and target.get("blocked_by_wall") is not True
                and self._entity_dist(st_post.get("pos"), target) <= followup_range
            ):
                if night_pursuit:
                    exposed_hostiles = [entity for entity in live_hostiles if not entity.get("blocked_by_wall")]
                    occluded_hostiles = [entity for entity in live_hostiles if entity.get("blocked_by_wall")]
                    shooters = [
                        shooter_pos for _, shooter_pos in ranged_retreat_positions(
                            st_post["pos"], exposed_hostiles, max_distance=12.0,
                        )
                    ]
                    retreat = env.flee(
                        target_pos, distance=8.5, urgent=True, wait_for_ground=True, allow_shaft_seal=False,
                        other_shooters=shooters, primary_melee=bool(shooters),
                        occluded_shooters=[
                            point for _, point in ranged_retreat_positions(
                                st_post["pos"], occluded_hostiles, max_distance=12.0,
                            )
                        ],
                        other_melee=self._other_melee_positions(live_hostiles, target_id),
                    )
                    actions.append({"action": "followup_melee_retreat", "entity_id": target_id, **retreat})
                else:
                    stepped = env.step_away_from_melee(st_post, target_pos, deadline=min(deadline, time.monotonic() + 1.5))
                    actions.append({"action": "followup_melee_retreat", "ok": stepped, "entity_id": target_id})
                st_post = env._safe_status(st_post)
                nh_post = st_post.get("nearest_hostile")
        if (
            mode == "counter_killed" and float(st_post.get("health") or 20.0) <= 8.0
            and not self._is_falling_fast(st_post, pos)
            and isinstance(nh_post, dict) and nh_post.get("name") and nh_post.get("id") != mob_id
            and self._entity_dist(st_post.get("pos"), nh_post) <= 5.0
            and st_post.get("ready") is not False and st_post.get("alive") is not False
            and not st_post.get("in_water") and not st_post.get("in_lava")
            and all(st_post.get(key) == st.get(key) for key in ("last_death", "dimension"))
        ):
            survivor_id = nh_post.get("id")
            if has_shield and st_post.get("on_ground") is False and time.monotonic() + 0.35 < deadline:
                latched_guard = self._latch_shield_at_threat(env, survivor_id)
            survivor_pos = nh_post.get("pos")
            if survivor_pos is None and survivor_id is not None and time.monotonic() + 1.15 < deadline:
                survivors = self._find_live_hostiles(env, radius=6, timeout_s=0.5)
                survivor = next((hostile for hostile in survivors if hostile.get("id") == survivor_id), {})
                survivor_pos = survivor.get("pos")
            retreat = {"ok": False, "reason": "time_budget"}
            if time.monotonic() + 1.15 < deadline:
                retreat = env.flee(
                    survivor_pos, distance=12.0, urgent=True, wait_for_ground=True,
                    allow_second_leg=time.monotonic() + 3.5 < deadline,
                )
            actions.append({"action": "low_hp_abort_flee", "entity_id": survivor_id, **retreat})
            mode = "low_hp_disengage"
            st_post = env._safe_status(st_post)
            nearest = st_post.get("nearest_hostile")
            pursuing = (
                isinstance(nearest, dict) and isinstance(nearest.get("id"), int)
                and _clean_block_name(nearest.get("name")) in PRE_HURT_MELEE_MOBS
                and not nearest.get("blocked_by_wall") and self._entity_dist(st_post.get("pos"), nearest) <= 5.5
            )
            life_unavailable = (
                retreat.get("respawned") or st_post.get("ready") is False or st_post.get("alive") is False
                or any(st_post.get(key) != st.get(key) for key in ("last_death", "dimension"))
            )
            if life_unavailable:
                mode = "life_unavailable"
            same_dry_life = not life_unavailable and not st_post.get("in_water") and not st_post.get("in_lava")
            if not same_dry_life or (retreat.get("ok") and not pursuing):
                latched_guard = False
            elif has_shield and time.monotonic() + 0.35 < deadline:
                target_id = nearest["id"] if pursuing else survivor_id
                latched_guard = self._latch_shield_at_threat(env, target_id)
                actions.append({"action": "guard_surviving_melee", "ok": latched_guard, "entity_id": target_id})
        hp_post = float(st_post.get("health") or 20.0)
        food_post = int(st_post.get("food") or 20)
        nh_post = st_post.get("nearest_hostile")
        nh_post_name = _clean_block_name(nh_post.get("name")) if isinstance(nh_post, dict) and nh_post.get("name") else ""
        nh_post_d = float(nh_post.get("dist", 99.0)) if nh_post_name else 99.0
        surviving_shooter = nh_post_name in RANGED_MOBS and nh_post_d <= 12.0
        surviving_melee = nh_post_name in PRE_HURT_MELEE_MOBS and nh_post_d <= 3.5
        if (
            mode == "counter_killed" and has_shield and st_post.get("blocking")
            and (surviving_shooter or surviving_melee)
            and isinstance(nh_post.get("id"), int) and nh_post["id"] != mob_id
            and st_post.get("ready") is not False and st_post.get("alive") is not False
            and not st_post.get("in_water") and not st_post.get("in_lava")
            and st_post.get("last_death") == st.get("last_death")
            and st_post.get("dimension") == st.get("dimension")
        ):
            survivor_id = nh_post["id"]
            latched_guard = self._latch_shield_at_threat(env, survivor_id)
            action = "guard_surviving_shooter" if surviving_shooter else "guard_surviving_melee"
            actions.append({"action": action, "ok": latched_guard, "entity_id": survivor_id})
        # Eating takes about two seconds. Leave room to confirm status and release controls.
        food_start_deadline = deadline - 2.5
        if (
            env.post_combat_food
            and mode != "falling"
            and not latched_guard
            and time.monotonic() < food_start_deadline
            and st_post.get("ready", True)
            and st_post.get("alive") is not False
            and st_post.get("on_ground") is not False
            and not st_post.get("in_water")
            and not st_post.get("in_lava")
            and nh_post_d > 6.0
            and not ((nh_post_name in RANGED_MOBS or nh_post_name == "ghast") and nh_post_d <= 16.0)
            and food_post < 20
            and (hp_post < 16.0 or food_post <= 15)
        ):
            counts_now, _, _ = env._inventory_counts_and_shield()
            best_food = next((f for f in SAFE_FOOD_PRIORITY if counts_now.get(f, 0) > 0), None)
            if best_food and time.monotonic() < food_start_deadline:
                try:
                    env.bot.call("use_item", {"item": best_food})
                    actions.append({"action": "post_combat_eat", "item": best_food, "ok": True})
                    st_post = env.bot.status()
                except ApiError:
                    pass

        summary = (
            f"遭遇 {mob_name or 'hostile'}(d={round(mob_dist, 1)}m, trigger={trigger.get('source')})，"
            f"已执行 0-LLM 小脑接管[{mode}]，当前 HP={st_post.get('health')} pos={st_post.get('pos')}"
        )
        if mode == "falling":
            summary = f"快速下坠期间已暂停反击，当前 HP={st_post.get('health')} pos={st_post.get('pos')}"
        return {
            "handled": bool(actions),
            "mode": mode,
            "summary": summary,
            "actions": actions,
            "status_after": st_post,
            "requires_replan": True,
            "latched_guard": latched_guard,
        }


class CombatControllerSlot:
    """Pluggable L4 slot enforcing entry `/stop` cleanup and exit `/posture {}` reset."""

    def __init__(self, controller: CombatController | None = None) -> None:
        self.controller: CombatController = controller or SubcorticalInstinctController()

    def register(self, controller: CombatController) -> None:
        self.controller = controller

    def handle(self, env: CombatEnv, trigger: ThreatTrigger) -> CombatOutcome:
        # 1. Entry rule: clear residual pathfinder/mining lock & stuck keys
        for cleanup_fn in (
            lambda: env.bot.call("stop", {}),
            lambda: env.bot.call("control", {"wait": False}),
        ):
            try:
                cleanup_fn()
            except Exception as error:
                if _is_control_signal(error):
                    raise

        outcome: CombatOutcome = {
            "handled": False,
            "mode": "none",
            "summary": "",
            "actions": [],
            "status_after": {},
            "requires_replan": True,
            "latched_guard": False,
        }
        try:
            outcome = self.controller.on_encounter(env, trigger)
            return outcome
        except _CombatFortificationInterrupted as interrupted:
            outcome = {
                "handled": False,
                "mode": "life_unavailable" if interrupted.life_unavailable else "combat_timeout",
                "summary": "战斗工事超时，已尝试停止原动作，请按最新状态重新规划",
                "actions": [],
                "status_after": interrupted.status,
                "requires_replan": True,
                "latched_guard": False,
            }
            return outcome
        finally:
            # 2. Exit rule: NEVER leave shield/sneak/jump stuck at 30% speed unless explicitly latched
            if outcome.get("mode") != "life_unavailable" and not outcome.get("latched_guard", False):
                st_end = outcome.get("status_after") if isinstance(outcome, dict) else None
                wet_submerged = isinstance(st_end, dict) and bool(st_end.get("in_water")) and (
                    bool(st_end.get("submerged"))
                    or st_end.get("on_ground") is False
                    or (isinstance(st_end.get("oxygen"), (int, float)) and st_end["oxygen"] < 240)
                )
                posture_payload: dict[str, Any] = {"shield": False, "sneak": False, "jump": True} if wet_submerged else {}
                for reset_fn in (
                    lambda: env.bot.call("control", {"wait": False}),
                    lambda: env.bot.call("posture", posture_payload),
                ):
                    try:
                        reset_fn()
                    except Exception as error:
                        if _is_control_signal(error):
                            raise
