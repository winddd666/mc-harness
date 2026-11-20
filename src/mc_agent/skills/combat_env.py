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
from typing import Any, Callable, Literal, Protocol, Sequence, TypedDict

import httpx

from ..errors import ApiError
from ..harness.control import RequestCancelled, RunTimedOut
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
    if name in {"water", "flowing_water", "glass", "iron_bars", "cobweb"} or name.endswith(("_glass", "_glass_pane")):
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


class CombatEnv:
    """Two-tier spatial combat environment (`CombatEnv` spec v1)."""

    def __init__(
        self,
        bot: Any,
        *,
        skills: Any | None = None,
        geofence: Any | None = None,
        water_escape: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        block_cache_ttl: float = 3.0,
        hazard_cache_ttl: float = 3.0,
    ) -> None:
        self.bot = bot
        self.skills = skills
        self.geofence = geofence
        self._water_escape = water_escape
        self.block_cache_ttl = max(0.2, float(block_cache_ttl))
        self.hazard_cache_ttl = max(0.2, float(hazard_cache_ttl))
        self._block_cache: dict[tuple[int, int, int], tuple[str | None, float]] = {}
        self._hazard_cache: tuple[tuple[int, int, int], int, float, list[dict[str, Any]]] | None = None

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

    def _get_block_name(self, x: int, y: int, z: int, *, strict: bool = False) -> str:
        key = (int(x), int(y), int(z))
        now = time.monotonic()
        cached = self._block_cache.get(key)
        if cached is not None and (now - cached[1]) <= self.block_cache_ttl:
            name = cached[0]
        else:
            try:
                res = self.bot.call("get_block", {"pos": [key[0], key[1], key[2]]})
                name = _clean_block_name(res) if res.get("name") or res.get("type") else ""
            except (RunTimedOut, RequestCancelled):
                raise
            except Exception:
                name = None
            self._block_cache[key] = (name, now)
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

        for i in range(1, steps):
            frac = i / steps
            px, py, pz = sx + dx * frac, sy + dy * frac, sz + dz * frac
            voxel = (math.floor(px), math.floor(py), math.floor(pz))
            if voxel in seen_voxels:
                continue
            seen_voxels.add(voxel)
            bname = self._get_block_name(*voxel)
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
        while sample_d <= max_r + 1e-6:
            nx = ox + ux * sample_d
            nz = oz + uz * sample_d
            if self.geofence is not None and hasattr(self.geofence, "check_point"):
                if self.geofence.check_point(nx, nz, action="probe_ray") is not None:
                    hazard = "geofence"
                    break

            bx, bz = math.floor(nx), math.floor(nz)
            foot_b = self._get_block_name(bx, cur_y, bz)
            head_b = self._get_block_name(bx, cur_y + 1, bz)

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
                step_head2 = self._get_block_name(bx, cur_y + 2, bz)
                orig_above = self._get_block_name(math.floor(ox), cur_y + 2, math.floor(oz))
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
                below_b = self._get_block_name(bx, cur_y - dy_down, bz)
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
    ) -> dict[str, Any]:
        """Fan-probe around `preferred_yaw`, ranking safe directions with minimal angular deviation first."""
        candidates: list[dict[str, Any]] = []
        for deg in angles_deg:
            cand_yaw = float(preferred_yaw) + math.radians(float(deg))
            ray = self.probe_ray(origin, cand_yaw, dist=dist)
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

    def probe_retreat(self, origin: Sequence[float], yaw: float, dist: float) -> tuple[int, bool] | None:
        """Check support and clearance under the entire hitbox along a short retreat."""
        ox, oy, oz = map(float, origin[:3])
        ux, uz = yaw_to_unit_xz(yaw)
        start_y = math.floor(oy + 0.05)
        walk_y = start_y
        needs_jump = False
        samples = max(1, math.ceil(dist / 0.45))
        for step in range(1, samples + 1):
            travel = dist * step / samples
            nx, nz = ox + ux * travel, oz + uz * travel
            if not self._point_allowed(nx, nz, action="flee"):
                return None
            bx, bz = math.floor(nx), math.floor(nz)
            foot = self._get_block_name(bx, walk_y, bz, strict=True)
            if not foot or foot in HAZARD_BLOCK_TYPES:
                return None
            if _is_solid_ground(foot):
                walk_y += 1
                needs_jump = True
            elif _is_passable(self._get_block_name(bx, walk_y - 1, bz, strict=True)):
                walk_y -= 1
            if abs(walk_y - start_y) > 1:
                return None
            for hx, hz in _hitbox_columns(nx, nz):
                foot, head, floor = (
                    self._get_block_name(hx, walk_y + dy, hz, strict=True) for dy in (0, 1, -1)
                )
                if not _is_passable(foot) or not _is_passable(head) or not floor or floor in HAZARD_BLOCK_TYPES:
                    return None
                if (hx, hz) != (bx, bz) and _is_passable(floor):
                    floor = self._get_block_name(hx, walk_y - 2, hz, strict=True)
                if not floor or floor in HAZARD_BLOCK_TYPES or not _is_solid_ground(floor):
                    return None
        return walk_y, needs_jump

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
                res = self.skills.dig_cover_pocket()
                self.invalidate_block_cache()
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
            return {
                "ok": placed > 0,
                "mode": norm_mode,
                "direction": chosen_dir,
                "placed": placed,
                "pos_after": self.bot.status().get("pos"),
                "reason": None if placed > 0 else "place_failed",
            }

        return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "unknown_mode"}

    def _best_effort_stop(self, *, stop_control: bool = True) -> None:
        """Stop residual movement/pathfinder controls after a timeout or preemption."""
        try:
            self.bot.call("stop", {})
        except (ApiError, httpx.HTTPError):
            pass
        if stop_control:
            try:
                self.bot.call("control", {"wait": False})
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
                return {
                    "ok": True,
                    "in_lava": False,
                    "already_dry": True,
                    "pos_after": st.get("pos"),
                    "actions": [],
                }

        actions: list[str] = []
        lava_deadline = time.monotonic() + 5.0
        try:
            counts, _, _ = self._inventory_counts_and_shield()
        except (ApiError, httpx.HTTPError):
            counts = {}
        dim = str(st.get("dimension") or "").lower()
        if counts.get("water_bucket", 0) > 0 and "nether" not in dim:
            try:
                douse = self.build_tactical("place_water_feet")
                actions.append("place_water_feet" if douse.get("ok") else "place_water_feet_failed")
            except (ApiError, httpx.HTTPError):
                actions.append("place_water_feet_failed")
            st = self._safe_status(st)
            if not bool(st.get("in_lava")):
                return {"ok": True, "in_lava": False, "pos_after": st.get("pos"), "actions": actions}
            try:
                self.bot.call("use_item", {"item": "water_bucket"})
                actions.append("use_water_bucket")
            except (ApiError, httpx.HTTPError):
                pass
            st = self._safe_status(st)
            if not bool(st.get("in_lava")):
                return {"ok": True, "in_lava": False, "pos_after": st.get("pos"), "actions": actions}

        # 1. If submerged deep in lava, swim up first so surface bank scan can find solid footing
        self.invalidate_block_cache()
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]
        dim0 = str(st.get("dimension") or "").lower()
        last_death0 = st.get("last_death")
        hp0 = float(st.get("health")) if isinstance(st.get("health"), (int, float)) else None

        def _did_respawn_during_lava(st_now: dict[str, Any]) -> bool:
            pos_now = st_now.get("pos") or pos0
            moved = math.hypot(float(pos_now[0]) - float(pos0[0]), float(pos_now[2]) - float(pos0[2]))
            dim_now = str(st_now.get("dimension") or "").lower()
            ld_now = st_now.get("last_death")
            hp_now = st_now.get("health")
            died_heal = (
                hp0 is not None
                and isinstance(hp_now, (int, float))
                and hp0 < 18.0
                and float(hp_now) >= 19.5
                and (float(hp_now) - hp0) >= 2.0
            )
            return bool(
                st_now.get("alive") is False
                or st_now.get("ready") is False
                or moved > 64.0
                or (dim0 and dim_now and dim_now != dim0)
                or (ld_now is not None and ld_now != last_death0)
                or died_heal
            )

        def _should_exit_lava(st_now: dict[str, Any]) -> bool:
            if _did_respawn_during_lava(st_now):
                return True
            if bool(st_now.get("in_lava")) or self._is_airborne_above_lava(st_now, start_pos=pos0):
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
            airborne_over_lava = (
                (not respawned)
                and (not still_in_lava)
                and self._is_airborne_above_lava(st_now, start_pos=pos0)
            )
            return {
                "ok": (not respawned) and (not still_in_lava) and (not airborne_over_lava),
                "in_lava": still_in_lava,
                "pos_after": pos_now,
                "actions": actions,
                **({"respawned": True} if respawned else {}),
                **({"airborne_above_lava": True} if airborne_over_lava else {}),
            }

        head_bx, head_by, head_bz = math.floor(float(pos0[0])), math.floor(float(pos0[1])) + 1, math.floor(float(pos0[2]))
        if self._get_block_name(head_bx, head_by, head_bz) in {"lava", "flowing_lava"}:
            try:
                # The current yaw can still face the attacker that knocked us in.
                # Preserve the horizontal position until the shore probe picks a heading.
                self.bot.call("control", {"forward": False, "jump": True, "sprint": False, "ms": 950, "stop_on": ["death"]})
                actions.append("lava_surface_swim")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    actions.append("lava_surface_swim_timeout")
                    self._best_effort_stop()
            self.invalidate_block_cache()
            st = self._safe_status(st)
            if _should_exit_lava(st):
                return _lava_result(st)
            pos0 = st.get("pos") or pos0

        # 2. Try scanning safe non-lava exits (including 1-block step-ups, plus wide shore search) and jump-sprinting out
        scan = self.scan_safe_yaws(
            pos0,
            float(st.get("yaw") or 0.0),
            dist=2.8,
            angles_deg=(0, 45, -45, 90, -90, 135, -135, 180),
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
        if not tried_yaws and time.monotonic() < lava_deadline:
            # Wide lava lake: filter shore types by dimension to keep find_blocks fast (<1s).
            shore_blocks = NETHER_SHORE_BLOCKS if "nether" in dim else SHORE_LAND_BLOCKS
            lava_shore_target = self._find_walkable_shore_target(
                pos0,
                block_types=shore_blocks,
                max_radius=18,
                min_y=py - 1,
                max_y=py + 2,
                max_candidates=6,
            )
            if lava_shore_target is not None:
                tried_yaws = [aim_yaw_pitch(pos0, lava_shore_target)[0]]
            else:
                base_yaw = float(st.get("yaw") or 0.0)
                best_shore_yaw: float | None = None
                for probe_dist in (4.5, 9.0):
                    if time.monotonic() >= lava_deadline:
                        break
                    for deg in (0, 90, -90, 180, 45, -45, 135, -135):
                        cand_yaw = base_yaw + math.radians(deg)
                        ux, uz = yaw_to_unit_xz(cand_yaw)
                        px = math.floor(float(pos0[0]) + ux * probe_dist)
                        pz = math.floor(float(pos0[2]) + uz * probe_dist)
                        for y_chk in (py, py + 1):
                            if _is_solid_ground(self._get_block_name(px, y_chk, pz)) and _is_passable(self._get_block_name(px, y_chk + 1, pz)):
                                best_shore_yaw = cand_yaw
                                break
                        if best_shore_yaw is not None:
                            break
                    if best_shore_yaw is not None:
                        break
                tried_yaws = [best_shore_yaw] if best_shore_yaw is not None else [base_yaw]

        swim_ms = 1400 if (lava_shore_target is not None or len(tried_yaws) == 1) else 950

        def _run_jump_sprint(cyaw: float) -> bool:
            nonlocal st
            try:
                self.bot.call("look", {"yaw": cyaw, "pitch": 0.0})
                self.bot.call(
                    "control",
                    {"forward": True, "jump": True, "sprint": True, "ms": swim_ms, "stop_on": ["death"]},
                )
                actions.append("lava_jump_sprint")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    actions.append("lava_jump_sprint_timeout")
                    self._best_effort_stop()
            self.invalidate_block_cache()
            st = self._safe_status(st)
            return _should_exit_lava(st)

        if tried_yaws and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[0]):
                return _lava_result(st)

        # 2b. Short pathfinder step onto close shore (`<=6.5m`) guarded against pathfinder ReadTimeout stalls.
        if lava_shore_target is not None and time.monotonic() + 1.5 < lava_deadline:
            shore_d = math.hypot(lava_shore_target[0] - float(pos0[0]), lava_shore_target[2] - float(pos0[2]))
            if shore_d <= 6.5:
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": lava_shore_target[0],
                            "y": lava_shore_target[1],
                            "z": lava_shore_target[2],
                            "range": 1.2,
                            "timeout": 1200,
                            "stop_on": ["death"],
                        },
                    )
                    actions.append("lava_move_to_shore")
                except (ApiError, httpx.HTTPError) as err:
                    actions.append("lava_move_to_shore_timeout" if isinstance(err, httpx.HTTPError) else "lava_move_to_shore_err")
                    self._best_effort_stop()
                self.invalidate_block_cache()
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)

        # 3. Boxed-in pit or deep lava: try bridging beneath feet or against adjacent solid bank/floor
        filler = self._pick_filler_block(counts)
        if filler is not None and time.monotonic() < lava_deadline + 0.8:
            p_cur = st.get("pos") or pos0
            bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
            place_targets = [[bx, by, bz], [bx, by - 1, bz]]
            if st.get("on_ground") is False and _is_passable(self._get_block_name(bx, by - 1, bz)):
                place_targets.append([bx, by - 2, bz])
            for dx, dz in CARDINAL_VECTORS.values():
                for cy in (by, by - 1):
                    tx, tz = bx + dx, bz + dz
                    if not self._point_allowed(tx + 0.5, tz + 0.5, action="escape_lava"):
                        continue
                    cur_bn = self._get_block_name(tx, cy, tz)
                    if cur_bn not in {"lava", "flowing_lava"} and not _is_passable(cur_bn):
                        continue
                    has_support = any(
                        _is_solid_ground(self._get_block_name(tx + ox, cy + oy, tz + oz))
                        for ox, oy, oz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, -1, 0), (0, 1, 0))
                    )
                    if has_support and [tx, cy, tz] not in place_targets:
                        place_targets.append([tx, cy, tz])
                        if len(place_targets) >= 5:
                            break
                if len(place_targets) >= 5:
                    break
            for place_pos in place_targets:
                if time.monotonic() >= lava_deadline + 0.8:
                    break
                try:
                    self.bot.call("control", {"jump": True, "ms": 220, "wait": False})
                    time.sleep(0.18)
                    self.bot.call("place", {"item": filler, "pos": place_pos})
                    actions.append(f"lava_place_{filler}")
                    self.invalidate_block_cache()
                    if place_pos[0] != bx or place_pos[2] != bz:
                        try:
                            byaw, _ = aim_yaw_pitch(p_cur, [place_pos[0] + 0.5, place_pos[1] + 1.0, place_pos[2] + 0.5])
                            self.bot.call("look", {"yaw": byaw, "pitch": 0.0})
                            self.bot.call("control", {"forward": True, "jump": True, "sprint": True, "ms": 650, "stop_on": ["death"]})
                            actions.append("lava_step_bridge")
                        except (ApiError, httpx.HTTPError) as err_step:
                            if isinstance(err_step, httpx.HTTPError):
                                self._best_effort_stop()
                    st = self._safe_status(st)
                    if _should_exit_lava(st):
                        return _lava_result(st)
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop()
                    continue

        if len(tried_yaws) > 1 and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[1]):
                return _lava_result(st)

        # 4. Last resort: carve a 2-high notch out of an adjacent solid bank and leap onto it
        p_cur = st.get("pos") or pos0
        bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
        for dname, (dx, dz) in CARDINAL_VECTORS.items():
            if time.monotonic() >= lava_deadline:
                break
            ex, ez = bx + dx, bz + dz
            if not self._point_allowed(ex + 0.5, ez + 0.5, action="escape_lava"):
                continue
            b_foot = self._get_block_name(ex, by, ez)
            b_head = self._get_block_name(ex, by + 1, ez)
            b_top = self._get_block_name(ex, by + 2, ez)
            if b_foot in {"lava", "flowing_lava", "bedrock"} or b_head in {"lava", "flowing_lava", "bedrock"}:
                continue
            dug_any = False
            for cy in (by + 1, by + 2):
                bn = self._get_block_name(ex, cy, ez)
                if not _is_passable(bn) and bn not in {"lava", "flowing_lava", "water", "flowing_water", "bedrock"}:
                    try:
                        self.bot.call("dig", {"pos": [ex, cy, ez], "collect": False, "stop_on": ["death"]})
                        dug_any = True
                    except (ApiError, httpx.HTTPError) as err:
                        if isinstance(err, httpx.HTTPError):
                            self._best_effort_stop()
                        break
            if dug_any or (_is_passable(b_head) and _is_passable(b_top)):
                yaw_out = math.atan2(-(ex + 0.5 - float(p_cur[0])), -(ez + 0.5 - float(p_cur[2])))
                try:
                    self.bot.call("look", {"yaw": yaw_out, "pitch": 0.0})
                    self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "sprint": True, "ms": 1000, "stop_on": ["death"]},
                    )
                    actions.append(f"lava_notch_{dname}")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop()
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

    def flee(
        self,
        threat_pos: Sequence[float] | None = None,
        *,
        distance: float = 14.0,
        urgent: bool = True,
        allow_shaft_seal: bool = True,
    ) -> dict[str, Any]:
        """Disengage from `threat_pos` along verified ground or seal a carved shaft."""
        st0 = self.bot.status()
        pos0 = st0.get("pos")
        if not pos0 or st0.get("alive") is False or st0.get("ready") is False:
            return {"ok": False, "mode": "flee", "reason": "not_ready", "moved_dist": 0.0, "pos_after": pos0}
        if st0.get("in_lava") or st0.get("on_ground") is False:
            return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0, "pos_after": pos0}
        if threat_pos is None:
            nh = st0.get("nearest_hostile")
            if isinstance(nh, dict) and isinstance(nh.get("pos"), list):
                threat_pos = nh["pos"]

        if allow_shaft_seal:
            snap = self.arena_snapshot(pos0)
            if snap["enclosure"] == "carved_shaft":
                seal = self.build_tactical("full_seal", threat_pos=threat_pos)
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
        for angle in (0, 35, -35, 70, -70, 110, -110, 150, -150, 180):
            chosen_yaw = pref_yaw + math.radians(angle)
            route = self.probe_retreat(pos0, chosen_yaw, 3.5)
            if route is not None:
                break
        else:
            return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0, "pos_after": pos0}

        _, needs_jump = route
        try:
            self.bot.call("look", {"yaw": chosen_yaw, "pitch": 0.0})
            self.bot.call(
                "control",
                {
                    "forward": True,
                    "sprint": True,
                    "jump": needs_jump or bool(st0.get("in_water")),
                    "ms": 450 if urgent else 350,
                    "stop_on": ["death"],
                },
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop()

        def retreat_result(st: dict[str, Any]) -> dict[str, Any]:
            pos = st.get("pos") or pos0
            moved = math.hypot(float(pos[0]) - float(pos0[0]), float(pos[2]) - float(pos0[2]))
            respawned = (
                st.get("alive") is False
                or moved > 64.0
                or (st.get("dimension") and st0.get("dimension") and st["dimension"] != st0["dimension"])
                or (st.get("last_death") is not None and st["last_death"] != st0.get("last_death"))
            )
            unsafe = (
                st.get("ready") is False or st.get("in_lava") or st.get("in_water") or st.get("on_ground") is False
            )
            return {
                "ok": not respawned and not unsafe and moved >= 1.5,
                "mode": "respawned" if respawned else "flee",
                "moved_dist": round(moved, 2),
                "pos_after": pos,
                **({"respawned": True} if respawned else {}),
                **({"reason": "unsafe_landing"} if unsafe else {}),
            }

        st_mid = self._safe_status({**st0, "ready": False})
        result = retreat_result(st_mid)
        if result.get("respawned") or result.get("reason") == "unsafe_landing":
            return result

        mid_pos = result["pos_after"]
        threat_dist_mid = 0.0
        if threat_pos is not None and len(threat_pos) >= 3:
            threat_dist_mid = math.hypot(float(mid_pos[0]) - float(threat_pos[0]), float(mid_pos[2]) - float(threat_pos[2]))

        ux, uz = yaw_to_unit_xz(chosen_yaw)
        if (
            threat_pos is not None
            and threat_dist_mid >= min(float(distance), 8.5)
            and result["ok"]
        ):
            return result

        remaining = min(6.0, float(distance) - result["moved_dist"])
        if remaining < 1.5:
            return result
        self.invalidate_block_cache()
        route = self.probe_retreat(mid_pos, chosen_yaw, remaining)
        if route is None:
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
        return retreat_result(self._safe_status({**st_mid, "ready": False}))

    def _point_allowed(self, x: float, z: float, *, action: str = "combat_env") -> bool:
        """Return True unless an enabled VoidGeofence rejects `(x, z)` via `check_point`."""
        if self.geofence is None or not hasattr(self.geofence, "check_point"):
            return True
        return self.geofence.check_point(float(x), float(z), action=action) is None

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
    ) -> list[float] | None:
        """Query `find_blocks` for nearby solid shoreline with 2-high passable headroom."""
        start_x, start_z = float(pos0[0]), float(pos0[2])
        has_threat = isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3
        tx_th = float(threat_pos[0]) if has_threat else 0.0
        tz_th = float(threat_pos[2]) if has_threat else 0.0
        threat_dist0 = math.hypot(start_x - tx_th, start_z - tz_th) if has_threat else 0.0

        def _pick_from_query(q_types: Sequence[str], *, q_limit: int) -> list[float] | None:
            try:
                found = self.bot.call(
                    "find_blocks",
                    {
                        "radius": max(8, min(28, int(max_radius))),
                        "types": list(q_types),
                        "limit": q_limit,
                    },
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
                if not _is_solid_ground(_clean_block_name(row.get("name"))):
                    continue
                if not self._point_allowed(cx + 0.5, cz + 0.5, action="lava_shore"):
                    continue
                hdist = math.hypot(cx + 0.5 - start_x, cz + 0.5 - start_z)
                away_penalty = 1.5 if (has_threat and math.hypot(cx + 0.5 - tx_th, cz + 0.5 - tz_th) < threat_dist0) else 0.0
                candidates.append((hdist + away_penalty, cx, cy, cz))

            candidates.sort(key=lambda item: item[0])
            for _, cx, cy, cz in candidates[:max(1, int(max_candidates))]:
                foot_b = self._get_block_name(cx, cy + 1, cz)
                head_b = self._get_block_name(cx, cy + 2, cz)
                if _is_passable(foot_b) and _is_passable(head_b):
                    return [cx + 0.5, float(cy + 1), cz + 0.5]
            return None

        if tuple(block_types) == SHORE_LAND_BLOCKS:
            surf_hit = _pick_from_query(SURFACE_SHORE_PRIORITY_BLOCKS, q_limit=48)
            if surf_hit is not None:
                return surf_hit
        return _pick_from_query(block_types, q_limit=120)

    def escape_deep_water_to_shore(
        self,
        threat_pos: Sequence[float] | None = None,
        *,
        max_radius: int = 24,
        keep_shield: bool = False,
        surface_first: bool = False,
    ) -> dict[str, Any]:
        """Swim up and move toward the nearest walkable dry shoreline block (or retreat at surface)."""
        st0 = self.bot.status()
        if st0.get("alive") is False or st0.get("ready") is False:
            return {
                "ok": False,
                "in_water": bool(st0.get("in_water", True)),
                "mode": "not_ready",
                "pos_after": st0.get("pos"),
            }
        if not bool(st0.get("in_water")):
            return {
                "ok": True,
                "in_water": False,
                "mode": "already_dry",
                "pos_after": st0.get("pos"),
            }

        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        start_x, start_y, start_z = float(pos0[0]), float(pos0[1]), float(pos0[2])
        bx, by, bz = math.floor(start_x), math.floor(start_y + 0.05), math.floor(start_z)
        actions: list[str] = []
        shore_target: list[float] | None = None

        def interrupted_water_escape(st: dict[str, Any]) -> dict[str, Any] | None:
            pos = st.get("pos") or pos0
            respawned = (
                math.hypot(float(pos[0]) - start_x, float(pos[2]) - start_z) > 64.0
                or (st.get("last_death") is not None and st["last_death"] != st0.get("last_death"))
            )
            if not respawned and st.get("alive") is not False and st.get("ready") is not False:
                return None
            return {
                "ok": False,
                "in_water": bool(st.get("in_water", True)),
                "mode": "respawned" if respawned else "not_ready",
                "shore_target": shore_target,
                "actions": actions,
                "pos_after": pos,
            }

        def swim_step_allowed(pos: Sequence[float], yaw: float) -> bool:
            ux, uz = yaw_to_unit_xz(yaw)
            return self._point_allowed(float(pos[0]) + 5.0 * ux, float(pos[2]) + 5.0 * uz, action="water_retreat")

        # Hold buoyancy + shield while locating shoreline or breaking surface.
        try:
            self.bot.call(
                "posture",
                {"shield": bool(keep_shield), "sneak": False, "jump": True},
            )
        except ApiError:
            pass

        oxy0 = st0.get("oxygen")
        if surface_first or bool(st0.get("submerged")) or (isinstance(oxy0, (int, float)) and oxy0 < 260):
            swim_forward = not surface_first and swim_step_allowed(pos0, float(st0.get("yaw") or 0.0))
            try:
                self.bot.call(
                    "control",
                    {"forward": swim_forward, "jump": True, "sprint": swim_forward, "ms": 750, "stop_on": ["death"]},
                )
                actions.append("water_surface_surge")
            except ApiError:
                pass
            if surface_first:
                st_surface = self.bot.status()
                interruption = interrupted_water_escape(st_surface)
                if interruption is not None:
                    return interruption
                if not st_surface.get("in_water") and st_surface.get("on_ground") is not False:
                    return {
                        "ok": True, "in_water": False, "mode": "surfaced",
                        "actions": actions, "pos_after": st_surface.get("pos"),
                    }

        # 1. Search nearby solid dry shoreline via find_blocks (filtering out ocean floor below waterline).
        has_threat = isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3
        shore_target = self._find_walkable_shore_target(
            pos0,
            block_types=SHORE_LAND_BLOCKS,
            max_radius=max_radius,
            min_y=max(61, by - 1),
            threat_pos=threat_pos,
        )

        # 2. Radial fallback if find_blocks yielded no dry surface spot.
        if shore_target is None:
            base_yaw = (
                aim_yaw_pitch(threat_pos, pos0)[0]
                if has_threat
                else float(st0.get("yaw") or 0.0)
            )
            y_probe = max(62, by)
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                cyaw = base_yaw + math.radians(deg)
                ux, uz = yaw_to_unit_xz(cyaw)
                for dist in (4, 8, 14):
                    px = math.floor(start_x + ux * dist)
                    pz = math.floor(start_z + uz * dist)
                    if not self._point_allowed(px + 0.5, pz + 0.5, action="water_shore"):
                        continue
                    g_name = self._get_block_name(px, y_probe, pz)
                    if _is_solid_ground(g_name):
                        foot_p = self._get_block_name(px, y_probe + 1, pz)
                        head_p = self._get_block_name(px, y_probe + 2, pz)
                        if _is_passable(foot_p) and _is_passable(head_p):
                            shore_target = [px + 0.5, float(y_probe + 1), pz + 0.5]
                            break
                if shore_target is not None:
                    break

        # 3. Move to shore target (plus shoreline lip jump) or swim up and away from threat.
        if shore_target is not None:
            try:
                syaw, _ = aim_yaw_pitch(pos0, shore_target)
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
                        "timeout": 4200,
                        "stop_on": ["death"],
                    },
                )
                actions.append("move_to_shore")
            except (ApiError, httpx.HTTPError) as err:
                actions.append("move_to_shore_failed")
                if isinstance(err, httpx.HTTPError):
                    self._best_effort_stop()

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
                lip_ms = 1150 if float(mid_pos[1]) < float(shore_target[1]) - 0.05 else 850
                try:
                    syaw_m, _ = aim_yaw_pitch(mid_pos, shore_target)
                    self.bot.call("look", {"yaw": syaw_m, "pitch": -10.0})
                    self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "sprint": True, "ms": lip_ms, "stop_on": ["death"]},
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

            if away_yaw is None:
                actions.append("surface_retreat_geofence_blocked")
            elif st_mid.get("in_water"):
                ux, uz = yaw_to_unit_xz(away_yaw)
                px, py, pz = map(float, swim_pos[:3])
                step_dist = 12.0 if self._point_allowed(px + ux * 12.0, pz + uz * 12.0, action="water_retreat") else 5.0
                try:
                    self.bot.call("look", {"yaw": away_yaw, "pitch": -15.0})
                except (ApiError, httpx.HTTPError):
                    pass
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": px + ux * step_dist,
                            "y": max(63.0, py + 1.0),
                            "z": pz + uz * step_dist,
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
                if st_mid.get("in_water") and swim_step_allowed(st_mid.get("pos") or swim_pos, away_yaw):
                    try:
                        self.bot.call(
                            "control",
                            {"forward": True, "jump": True, "sprint": True, "yaw": away_yaw, "ms": 950, "stop_on": ["death"]},
                        )
                        actions.append("surface_retreat_jump")
                    except (ApiError, httpx.HTTPError) as err:
                        if isinstance(err, httpx.HTTPError):
                            self._best_effort_stop()

        st_end = self._safe_status(st0)
        interruption = interrupted_water_escape(st_end)
        if interruption is not None:
            return interruption
        pos_end = st_end.get("pos") or pos0

        still_wet = bool(st_end.get("in_water"))
        try:
            self.bot.call(
                "posture",
                {"shield": bool(keep_shield), "sneak": False, "jump": still_wet},
            )
        except ApiError:
            pass
        return {
            "ok": not still_wet,
            "in_water": still_wet,
            "mode": "move_to_shore" if shore_target is not None else "surface_retreat",
            "shore_target": shore_target,
            "actions": actions,
            "pos_after": pos_end,
        }

    def guard_lava_edge(
        self,
        pos: Sequence[float] | None = None,
        *,
        counts: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Force sneak posture and seal exposed adjacent lava edges before falling in."""
        st0 = self.bot.status()
        if bool(st0.get("in_lava")) or st0.get("on_ground") is not True or st0.get("ready") is False or st0.get("alive") is False:
            return {"ok": False, "near_lava": False, "sneak": bool(st0.get("sneaking")), "sealed": 0}

        cur_pos = pos or st0.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(cur_pos[0])), math.floor(float(cur_pos[1]) + 0.05), math.floor(float(cur_pos[2]))
        exposed_lava: list[list[int]] = []
        for dx, dz in CARDINAL_VECTORS.values():
            nx, nz = bx + dx, bz + dz
            for check_y in (by - 1, by):
                if self._get_block_name(nx, check_y, nz) in {"lava", "flowing_lava"}:
                    exposed_lava.append([nx, check_y, nz])
                    break

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
            for lpos in exposed_lava[:2]:
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

    def _find_live_hostiles(self, env: CombatEnv, radius: int = 16) -> list[dict[str, Any]]:
        try:
            res = env.bot.call("get_entities", {"radius": radius, "types": ["hostile"]})
            rows = [e for e in (res or {}).get("entities") or [] if isinstance(e, dict)]
            rows.sort(key=lambda e: float(e.get("dist", 999.0)))
            return rows
        except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
            raise
        except Exception:
            return []

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
            except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                raise
            except Exception:
                pass
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

    def on_encounter(self, env: CombatEnv, trigger: ThreatTrigger) -> CombatOutcome:
        actions: list[dict[str, Any]] = []
        deadline = time.monotonic() + 7.5
        st = env.bot.status()
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
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        hp = float(st.get("health") or 20.0)
        try:
            inv0 = env.bot.call("inventory", method="GET")
        except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
            raise
        except Exception:
            inv0 = {}
        counts, has_shield, held_name = env._inventory_counts_and_shield(inv0)
        if isinstance(inv0, dict) and (isinstance(inv0.get("armor"), dict) or "offhand" in inv0):
            auto_equip_survival_gear(env.bot, inv0, allow_armor=True, allow_shield=has_shield)

        hostiles = self._find_live_hostiles(env, radius=16)
        nh = st.get("nearest_hostile") if isinstance(st.get("nearest_hostile"), dict) else None
        primary: dict[str, Any] | None = None
        trig_id = trigger.get("primary_entity_id")
        if trig_id is not None:
            primary = next((e for e in hostiles if e.get("id") == trig_id), None)
        if primary is None and hostiles:
            primary = hostiles[0]
        if primary is None and nh and nh.get("name"):
            primary = nh

        mob_name = _clean_block_name((primary or {}).get("name") or trigger.get("primary_type") or "")
        mob_id = (primary or {}).get("id") if isinstance(primary, dict) else trig_id
        mob_pos = (primary or {}).get("pos") or trigger.get("primary_pos")
        try:
            mob_dist = float((primary or {}).get("dist") or trigger.get("distance") or 99.0)
        except (TypeError, ValueError):
            mob_dist = 99.0

        any_creeper_close = mob_name == "creeper" or any(
            _clean_block_name(e.get("name")) == "creeper" and float(e.get("dist", 99.0)) <= 5.5
            for e in hostiles
        )
        # Count close-range engaged hostiles (<=8.5m) for swarm/solo gates so mobs outside enclosed
        # walls (`10..16m`) never trick a sword+shield bot into pure circle-fleeing inside a room.
        nearby_hostiles = [e for e in hostiles if float(e.get("dist", 99.0)) <= 8.5]
        mode = "stabilize"
        latched_guard = False

        in_water = bool(st.get("in_water"))
        submerged = bool(st.get("submerged"))
        oxy_raw = st.get("oxygen")
        low_oxygen = isinstance(oxy_raw, (int, float)) and oxy_raw < 180

        if (
            not in_water
            and has_shield
            and not st.get("blocking")
            and trigger.get("source") in {"pre_hurt_ranged", "pre_hurt_melee", "pre_hurt_creeper"}
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
                shore = env.escape_deep_water_to_shore(mob_pos, keep_shield=has_shield)
                actions.append({"action": "water_surface_shore", "ok": bool(shore.get("ok"))})

        # 1) Creeper emergency: NEVER melee in blast radius — shield & retreat / seal shaft
        elif any_creeper_close or trigger.get("source") == "pre_hurt_creeper":
            mode = "creeper_evade"
            self._equip_best_weapon_and_shield(env, counts, has_shield, held_name)
            creeper_ent = next(
                (e for e in hostiles if _clean_block_name(e.get("name")) == "creeper"),
                primary,
            )
            c_pos = (creeper_ent or {}).get("pos") or mob_pos
            snap = env.arena_snapshot(pos)
            sealed_ok = False
            if snap["enclosure"] == "carved_shaft" and env._pick_filler_block(counts) is not None:
                seal = env.build_tactical("full_seal", threat_pos=c_pos)
                sealed_ok = bool(seal.get("ok"))
                actions.append({"action": "seal_creeper_shaft", "ok": sealed_ok})
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
                if has_shield:
                    if isinstance(creeper_ent, dict) and creeper_ent.get("id") is not None:
                        try:
                            env.bot.call("look", {"entity_id": creeper_ent["id"]})
                        except (ApiError, httpx.HTTPError):
                            pass
                    try:
                        env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                        actions.append({"action": "raise_shield_creeper", "ok": True})
                    except (ApiError, httpx.HTTPError):
                        pass
                flee_res = env.flee(c_pos, distance=11.5, urgent=True, allow_shaft_seal=False)
                actions.append({"action": "retreat_from_creeper", "ok": bool(flee_res.get("ok")), "moved": flee_res.get("moved_dist")})
                st_chk = env._safe_status(st)
                post_h = self._find_live_hostiles(env, radius=8)
                close_creeper = next(
                    (
                        e
                        for e in post_h
                        if _clean_block_name(e.get("name")) == "creeper"
                        and self._entity_dist(st_chk.get("pos"), e) <= 5.5
                    ),
                    None,
                )
                if close_creeper is None:
                    nh_chk = st_chk.get("nearest_hostile")
                    if (
                        isinstance(nh_chk, dict)
                        and _clean_block_name(nh_chk.get("name")) == "creeper"
                        and self._entity_dist(st_chk.get("pos"), nh_chk) <= 5.5
                    ):
                        close_creeper = nh_chk
                creeper_still_close = close_creeper is not None
                if (
                    (not flee_res.get("ok") or creeper_still_close)
                    and not flee_res.get("respawned")
                    and has_shield
                    and st_chk.get("alive") is not False
                ):
                    cid = (close_creeper or creeper_ent or {}).get("id")
                    latched_guard = self._latch_shield_at_threat(env, int(cid) if cid is not None else None)

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
            for _ in range(5):
                if time.monotonic() >= deadline:
                    break
                st_cur = env.bot.status()
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
                        shore_w = env.escape_deep_water_to_shore(mob_pos, keep_shield=has_shield)
                        actions.append({"action": "water_surface_shore", "ok": bool(shore_w.get("ok"))})
                        st_w = env._safe_status(st_w)
                    nh_w = st_w.get("nearest_hostile")
                    nh_w_dist = float(nh_w.get("dist", 99.0)) if isinstance(nh_w, dict) and nh_w.get("name") else 99.0
                    if has_shield and st_w.get("alive") is not False and not st_w.get("in_water") and (nh_w_dist <= 8.0 or not esc_cur.get("ok")):
                        tid_w = nh_w.get("id") if isinstance(nh_w, dict) and nh_w.get("id") is not None else mob_id
                        latched_guard = self._latch_shield_at_threat(env, int(tid_w) if tid_w is not None else None)
                    mode = "water_surface_abort"
                    break
                if hp_cur <= 8.0 or st_cur.get("alive") is False:
                    flee_low = env.flee(mob_pos, distance=12.0, urgent=True)
                    actions.append({"action": "low_hp_abort_flee", "ok": bool(flee_low.get("ok"))})
                    if not flee_low.get("ok") and not flee_low.get("respawned") and has_shield:
                        st_lw = env._safe_status(st_cur)
                        if st_lw.get("alive") is not False and not st_lw.get("in_water"):
                            latched_guard = self._latch_shield_at_threat(env, int(mob_id) if mob_id is not None else None)
                    mode = "low_hp_disengage"
                    break
                cur_hostiles = self._find_live_hostiles(env, radius=10)
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
                eff_id = int(target_ent["id"]) if isinstance(target_ent, dict) and target_ent.get("id") is not None else mob_id
                if eff_id is None:
                    break
                try:
                    atk = env.bot.call("attack", {"entity_id": eff_id, "timeout": 2600, "stop_on": ["death"]})
                    hits = atk.get("hits")
                    actions.append({"action": "melee_attack", "weapon": weapon, "entity_id": eff_id, "ok": True, "killed": bool(atk.get("killed"))})
                    if atk.get("killed"):
                        killed = True
                        break
                    if isinstance(hits, (int, float)) and hits == 0:
                        flee_uh = env.flee(mob_pos, distance=10.0, urgent=True)
                        actions.append({"action": "unreachable_flee", "ok": bool(flee_uh.get("ok"))})
                        mode = "cover_or_flee"
                        break
                except ApiError as err:
                    actions.append({"action": "melee_attack", "ok": False, "code": err.code})
                    if err.code in {"entity_not_found", "invalid_entity", "target_dead"}:
                        killed = True
                        break
                    if err.code in {"timeout", "attack_zero_hits", "path_not_found", "no_path"}:
                        flee_to = env.flee(mob_pos, distance=10.0, urgent=True)
                        actions.append({"action": "attack_timeout_flee", "ok": bool(flee_to.get("ok"))})
                        mode = "cover_or_flee"
                    break
                if not cur_hostiles:
                    killed = True
                    break
            if killed:
                mode = "counter_killed"
                self._recent_flee_streak = 0

        # 3) Ranged shooter / swarm / low-HP / unarmed -> seal shaft, close-rush with shield, bunker, or cover/flee
        else:
            self._equip_best_weapon_and_shield(env, counts, has_shield, held_name)
            snap = env.arena_snapshot(pos)
            has_weapon = any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
            if snap["enclosure"] == "carved_shaft" and env._pick_filler_block(counts) is not None:
                seal = env.build_tactical("full_seal", threat_pos=mob_pos)
                actions.append({"action": "seal_shaft", "ok": bool(seal.get("ok"))})
                mode = "sealed_shaft" if seal.get("ok") else "flee"
                if not seal.get("ok"):
                    fl = env.flee(mob_pos, distance=12.0, urgent=True, allow_shaft_seal=False)
                    actions.append({"action": "flee", "ok": bool(fl.get("ok"))})
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
                            if time.monotonic() >= deadline:
                                break
                            atk_r = env.bot.call("attack", {"entity_id": int(mob_id), "timeout": 2400, "stop_on": ["death"]})
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
                    if err.code not in {"entity_not_found", "invalid_entity", "target_dead"}:
                        fl = env.flee(mob_pos, distance=12.0, urgent=True)
                        actions.append({"action": "fallback_flee", "ok": bool(fl.get("ok"))})
            else:
                mode = "cover_or_flee"
                covered = False
                if (
                    snap["enclosure"] in {"open_cave", "carved_shaft"}
                    and env.skills is not None
                    and mob_name not in {"blaze", "ghast"}
                    and trigger.get("hurt_source") not in {"fireball", "small_fireball", "wither_skull"}
                ):
                    cov = env.build_tactical("cover_pocket")
                    if cov.get("ok"):
                        covered = True
                        mode = "cover_pocket"
                        actions.append({"action": "cover_pocket", "ok": True})
                now_flee = time.monotonic()
                if now_flee - self._last_flee_at > 25.0:
                    self._recent_flee_streak = 0
                should_bunker = (
                    not covered
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
                    bunk = env.skills.dig_safety_bunker(urgent=True)
                    if bunk.get("ok"):
                        covered = True
                        mode = "bunker_sealed"
                        self._recent_flee_streak = 0
                        actions.append({"action": "emergency_bunker", "ok": True})
                if not covered:
                    if has_shield and mob_id is not None:
                        try:
                            env.bot.call("look", {"entity_id": int(mob_id)})
                            env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                        except (ApiError, httpx.HTTPError):
                            pass
                    fl = env.flee(mob_pos, distance=14.0, urgent=True)
                    actions.append({
                        "action": "flee", "ok": bool(fl.get("ok")), "moved": fl.get("moved_dist"),
                        **({"reason": fl["reason"]} if fl.get("reason") else {}),
                    })
                    self._recent_flee_streak += 1
                    self._last_flee_at = now_flee
                    flee_ok = bool(fl.get("ok"))
                    check_ranged_guard = flee_ok and fl.get("mode") == "flee" and mob_name in RANGED_MOBS
                    if (not flee_ok or check_ranged_guard) and not fl.get("respawned") and has_shield:
                        st_fl = env._safe_status(st)
                        nearest = st_fl.get("nearest_hostile")
                        still_exposed = (
                            check_ranged_guard
                            and isinstance(nearest, dict)
                            and mob_id is not None and nearest.get("id") == mob_id
                            and isinstance(nearest.get("dist"), (int, float)) and nearest["dist"] <= 12.0
                            and isinstance(st_fl.get("health"), (int, float)) and st_fl["health"] <= 10.0
                        )
                        if (
                            (not flee_ok or still_exposed)
                            and st_fl.get("ready") is not False
                            and st_fl.get("alive") is not False
                            and not st_fl.get("in_water")
                            and not st_fl.get("in_lava")
                        ):
                            latched_guard = self._latch_shield_at_threat(
                                env,
                                int(mob_id) if mob_id is not None else None,
                            )

        # 4) Post-combat food recovery when safe and ready
        st_post = env._safe_status(st)
        hp_post = float(st_post.get("health") or 20.0)
        food_post = int(st_post.get("food") or 20)
        nh_post = st_post.get("nearest_hostile")
        nh_post_name = _clean_block_name(nh_post.get("name")) if isinstance(nh_post, dict) and nh_post.get("name") else ""
        nh_post_d = float(nh_post.get("dist", 99.0)) if nh_post_name else 99.0
        if (
            not latched_guard
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
            if best_food:
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
            except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                raise
            except Exception:
                pass

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
        finally:
            # 2. Exit rule: NEVER leave shield/sneak/jump stuck at 30% speed unless explicitly latched
            if not outcome.get("latched_guard", False):
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
                    except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                        raise
                    except Exception:
                        pass
