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
from typing import Any, Literal, Protocol, Sequence, TypedDict

from ..errors import ApiError
from ..harness.control import RequestCancelled, RunTimedOut
from .safety import SAFE_FOOD_PRIORITY

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
        block_cache_ttl: float = 3.0,
        hazard_cache_ttl: float = 3.0,
    ) -> None:
        self.bot = bot
        self.skills = skills
        self.geofence = geofence
        self.block_cache_ttl = max(0.2, float(block_cache_ttl))
        self.hazard_cache_ttl = max(0.2, float(hazard_cache_ttl))
        self._block_cache: dict[tuple[int, int, int], tuple[str, float]] = {}
        self._hazard_cache: tuple[tuple[int, int, int], int, float, list[dict[str, Any]]] | None = None

    def invalidate_block_cache(self) -> None:
        self._block_cache.clear()
        self._hazard_cache = None

    def _get_block_name(self, x: int, y: int, z: int) -> str:
        key = (int(x), int(y), int(z))
        now = time.monotonic()
        cached = self._block_cache.get(key)
        if cached is not None and (now - cached[1]) <= self.block_cache_ttl:
            return cached[0]
        try:
            res = self.bot.call("get_block", {"pos": [key[0], key[1], key[2]]})
            name = _clean_block_name(res)
        except Exception:
            name = "stone"
        self._block_cache[key] = (name, now)
        return name

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

    def escape_lava(self) -> dict[str, Any]:
        """Deterministically escape lava using water douse (overworld), safe-yaw jump sprint, wall-notch/pillar climb, and live `in_lava` verification."""
        st = self.bot.status()
        if not bool(st.get("in_lava")):
            return {
                "ok": True,
                "in_lava": False,
                "already_dry": True,
                "pos_after": st.get("pos"),
                "actions": [],
            }

        actions: list[str] = []
        counts, _, _ = self._inventory_counts_and_shield()
        dim = str(st.get("dimension") or "").lower()
        if counts.get("water_bucket", 0) > 0 and "nether" not in dim:
            douse = self.build_tactical("place_water_feet")
            actions.append("place_water_feet" if douse.get("ok") else "place_water_feet_failed")
            st = self.bot.status()
            if not bool(st.get("in_lava")):
                return {"ok": True, "in_lava": False, "pos_after": st.get("pos"), "actions": actions}
            try:
                self.bot.call("use_item", {"item": "water_bucket"})
                actions.append("use_water_bucket")
                st = self.bot.status()
                if not bool(st.get("in_lava")):
                    return {"ok": True, "in_lava": False, "pos_after": st.get("pos"), "actions": actions}
            except ApiError:
                pass

        # 1. If submerged deep in lava, swim up first so surface bank scan can find solid footing
        self.invalidate_block_cache()
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]

        def _lava_result(st_now: dict[str, Any]) -> dict[str, Any]:
            still_in_lava = bool(st_now.get("in_lava"))
            pos_now = st_now.get("pos") or pos0
            moved = math.hypot(float(pos_now[0]) - float(pos0[0]), float(pos_now[2]) - float(pos0[2]))
            respawned = st_now.get("alive") is False or st_now.get("ready") is False or moved > 64.0
            return {
                "ok": (not respawned) and (not still_in_lava),
                "in_lava": still_in_lava,
                "pos_after": pos_now,
                "actions": actions,
                **({"respawned": True} if respawned else {}),
            }

        head_bx, head_by, head_bz = math.floor(float(pos0[0])), math.floor(float(pos0[1])) + 1, math.floor(float(pos0[2]))
        if self._get_block_name(head_bx, head_by, head_bz) in {"lava", "flowing_lava"}:
            try:
                self.bot.call("control", {"forward": True, "jump": True, "sprint": True, "ms": 950, "stop_on": ["death"]})
                actions.append("lava_surface_swim")
            except ApiError:
                pass
            self.invalidate_block_cache()
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False or not bool(st.get("in_lava")):
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
        if not tried_yaws:
            # Wide Nether lava lake (>2.8m from shore): probe 4.5m..7.5m to steer straight toward solid dry land
            base_yaw = float(st.get("yaw") or 0.0)
            best_shore_yaw: float | None = None
            py = math.floor(float(pos0[1]) + 0.05)
            for probe_dist in (4.5, 7.5):
                for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                    cand_yaw = base_yaw + math.radians(deg)
                    ux, uz = yaw_to_unit_xz(cand_yaw)
                    px = math.floor(float(pos0[0]) + ux * probe_dist)
                    pz = math.floor(float(pos0[2]) + uz * probe_dist)
                    if _is_solid_ground(self._get_block_name(px, py, pz)) and _is_passable(self._get_block_name(px, py + 1, pz)):
                        best_shore_yaw = cand_yaw
                        break
                if best_shore_yaw is not None:
                    break
            tried_yaws = [best_shore_yaw] if best_shore_yaw is not None else [base_yaw]

        for cyaw in tried_yaws[:2]:
            try:
                self.bot.call("look", {"yaw": cyaw, "pitch": 0.0})
                self.bot.call(
                    "control",
                    {"forward": True, "jump": True, "sprint": True, "ms": 950, "stop_on": ["death"]},
                )
                actions.append("lava_jump_sprint")
            except ApiError:
                pass
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False or not bool(st.get("in_lava")):
                return _lava_result(st)

        # 3. Boxed-in pit or deep lava: try replacing lava at feet / pillaring up with a filler block
        filler = self._pick_filler_block(counts)
        if filler is not None:
            p_cur = st.get("pos") or pos0
            bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
            for place_pos in ([bx, by, bz], [bx, by - 1, bz]):
                try:
                    self.bot.call("control", {"jump": True, "ms": 220, "wait": False})
                    time.sleep(0.18)
                    self.bot.call("place", {"item": filler, "pos": place_pos})
                    actions.append(f"lava_place_{filler}")
                    self.invalidate_block_cache()
                    st = self.bot.status()
                    if st.get("alive") is False or st.get("ready") is False or not bool(st.get("in_lava")):
                        return _lava_result(st)
                except ApiError:
                    continue

        # 4. Last resort: carve a 2-high notch out of an adjacent solid bank and leap onto it
        p_cur = st.get("pos") or pos0
        bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
        for dname, (dx, dz) in CARDINAL_VECTORS.items():
            ex, ez = bx + dx, bz + dz
            if self.geofence is not None and hasattr(self.geofence, "check_point"):
                if self.geofence.check_point(ex + 0.5, ez + 0.5, action="escape_lava") is not None:
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
                    except ApiError:
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
                except ApiError:
                    pass
                st = self.bot.status()
                if st.get("alive") is False or st.get("ready") is False or not bool(st.get("in_lava")):
                    break

        st_end = self.bot.status()
        still_in_lava = bool(st_end.get("in_lava"))
        if still_in_lava and st_end.get("alive") is not False and st_end.get("ready") is not False:
            try:
                self.bot.call("posture", {"jump": True})
            except ApiError:
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
        """Disengage from `threat_pos`: seal shaft if inside corridor, else sprint safe ray + pathfind away."""
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
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

        scan = self.scan_safe_yaws(pos0, pref_yaw, dist=min(4.0, max(2.5, float(distance) * 0.35)))
        chosen_yaw = scan["best_yaw"] if scan["best_yaw"] is not None else pref_yaw
        needs_jump = bool(scan.get("best_needs_jump"))

        try:
            self.bot.call("look", {"yaw": chosen_yaw, "pitch": 0.0})
            self.bot.call(
                "control",
                {
                    "forward": True,
                    "sprint": True,
                    "jump": needs_jump or bool(st0.get("in_water")),
                    "ms": 950 if urgent else 650,
                },
            )
        except ApiError:
            pass

        st_mid = self.bot.status()
        if st_mid.get("alive") is False:
            return {"ok": False, "mode": "respawned", "moved_dist": 0.0, "pos_after": st_mid.get("pos"), "respawned": True}
        mid_pos = st_mid.get("pos") or pos0
        mid_moved = math.hypot(float(mid_pos[0]) - float(pos0[0]), float(mid_pos[2]) - float(pos0[2]))
        if mid_moved > 64.0:
            return {
                "ok": False,
                "mode": "respawned",
                "moved_dist": round(mid_moved, 2),
                "pos_after": mid_pos,
                "respawned": True,
            }

        threat_dist_mid = 0.0
        if threat_pos is not None and len(threat_pos) >= 3:
            threat_dist_mid = math.hypot(float(mid_pos[0]) - float(threat_pos[0]), float(mid_pos[2]) - float(threat_pos[2]))

        ux, uz = yaw_to_unit_xz(chosen_yaw)
        goal_x = float(pos0[0]) + ux * float(distance)
        goal_z = float(pos0[2]) + uz * float(distance)
        should_pathfind = not (
            threat_pos is not None
            and threat_dist_mid >= min(float(distance), 8.5)
            and mid_moved >= 4.0
        )
        if should_pathfind and (
            self.geofence is None or not hasattr(self.geofence, "check_point") or self.geofence.check_point(goal_x, goal_z) is None
        ):
            try:
                self.bot.call(
                    "move_to",
                    {"x": round(goal_x, 1), "z": round(goal_z, 1), "range": 2.0, "timeout": 3500, "stop_on": ["death"]},
                )
            except ApiError:
                pass

        st_end = self.bot.status()
        nh_end = st_end.get("nearest_hostile")
        if (
            urgent
            and st_end.get("alive") is not False
            and isinstance(nh_end, dict)
            and nh_end.get("name")
            and float(nh_end.get("dist", 99.0)) <= 5.5
        ):
            try:
                self.bot.call("look", {"yaw": chosen_yaw, "pitch": 0.0})
                self.bot.call("control", {"forward": True, "sprint": True, "jump": True, "ms": 750})
                st_end = self.bot.status()
            except ApiError:
                pass
        pos_end = st_end.get("pos") or pos0
        moved = round(math.hypot(float(pos_end[0]) - float(pos0[0]), float(pos_end[2]) - float(pos0[2])), 2)
        respawned = st_end.get("alive") is False or moved > 64.0
        return {
            "ok": False if respawned else moved >= 1.5,
            "mode": "respawned" if respawned else "flee",
            "moved_dist": moved,
            "pos_after": pos_end,
            **({"respawned": True} if respawned else {}),
        }


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
        counts, has_shield, held_name = env._inventory_counts_and_shield()

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
        mode = "stabilize"
        latched_guard = False

        in_water = bool(st.get("in_water"))
        submerged = bool(st.get("submerged"))
        oxy_raw = st.get("oxygen")
        low_oxygen = isinstance(oxy_raw, (int, float)) and oxy_raw < 180

        # 0) Underwater drowning emergency: surface immediately instead of burning oxygen in combat!
        if in_water and (submerged or low_oxygen):
            mode = "water_surface"
            if env.skills is not None and hasattr(env.skills, "escape_water"):
                esc = env.skills.escape_water()
                actions.append({"action": "water_surface", "ok": bool(esc.get("ok"))})
            else:
                try:
                    env.bot.call("control", {"jump": True, "forward": True, "ms": 950, "stop_on": ["death"]})
                    actions.append({"action": "water_surface", "ok": True})
                except ApiError as err:
                    actions.append({"action": "water_surface", "ok": False, "code": err.code})

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
                        except ApiError:
                            pass
                    try:
                        env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                        actions.append({"action": "raise_shield_creeper", "ok": True})
                    except ApiError:
                        pass
                flee_res = env.flee(c_pos, distance=11.5, urgent=True, allow_shaft_seal=False)
                actions.append({"action": "retreat_from_creeper", "ok": bool(flee_res.get("ok")), "moved": flee_res.get("moved_dist")})
                st_chk = env.bot.status()
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
                    if cid is not None:
                        try:
                            env.bot.call("look", {"entity_id": int(cid)})
                        except ApiError:
                            pass
                    try:
                        env.bot.call("posture", {"shield": True, "sneak": False, "jump": False})
                        latched_guard = True
                    except ApiError:
                        pass

        # 2) Lone/pair melee hostile + healthy + weapon -> 0-LLM counter-kill loop!
        elif (
            mob_name in LONE_COUNTERKILL_MOBS
            and hp > 10.0
            and len(hostiles) <= 2
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
                    if env.skills is not None and hasattr(env.skills, "escape_water"):
                        esc_cur = env.skills.escape_water()
                        actions.append({"action": "water_surface_abort", "ok": bool(esc_cur.get("ok"))})
                    else:
                        try:
                            env.bot.call("control", {"jump": True, "forward": True, "ms": 950, "stop_on": ["death"]})
                            actions.append({"action": "water_surface_abort", "ok": True})
                        except ApiError:
                            pass
                    mode = "water_surface_abort"
                    break
                if hp_cur <= 8.0 or st_cur.get("alive") is False:
                    flee_low = env.flee(mob_pos, distance=12.0, urgent=True)
                    actions.append({"action": "low_hp_abort_flee", "ok": bool(flee_low.get("ok"))})
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
                and has_shield
                and has_weapon
                and hp >= 14.0
                and len(hostiles) <= 1
                and mob_dist <= 9.5
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
                        atk_r = env.bot.call("attack", {"entity_id": int(mob_id), "timeout": 2800, "stop_on": ["death"]})
                        actions.append({"action": "shield_rush_attack", "ok": True, "killed": bool(atk_r.get("killed"))})
                except ApiError as err:
                    actions.append({"action": "shield_rush_attack", "ok": False, "code": err.code})
                    fl = env.flee(mob_pos, distance=12.0, urgent=True)
                    actions.append({"action": "fallback_flee", "ok": bool(fl.get("ok"))})
            else:
                mode = "cover_or_flee"
                covered = False
                if snap["enclosure"] in {"open_cave", "carved_shaft"} and env.skills is not None:
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
                        except ApiError:
                            pass
                    fl = env.flee(mob_pos, distance=14.0, urgent=True)
                    actions.append({"action": "flee", "ok": bool(fl.get("ok")), "moved": fl.get("moved_dist")})
                    self._recent_flee_streak += 1
                    self._last_flee_at = now_flee

        # 4) Post-combat food recovery when safe and ready
        st_post = env.bot.status()
        hp_post = float(st_post.get("health") or 20.0)
        food_post = int(st_post.get("food") or 20)
        nh_post = st_post.get("nearest_hostile")
        nh_post_d = float(nh_post.get("dist", 99.0)) if isinstance(nh_post, dict) and nh_post.get("name") else 99.0
        if (
            st_post.get("ready", True)
            and st_post.get("alive") is not False
            and not st_post.get("in_water")
            and not st_post.get("in_lava")
            and nh_post_d > 6.0
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
