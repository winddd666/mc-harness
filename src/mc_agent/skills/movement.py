"""L2 local terrain primitives: breadcrumb trail, soft-trap clearing, sneak bridging,
cliff climbing/descending, boat crossing.

This module is intentionally strategy-free: every entry point takes an explicit local
goal (a cell direction or target coordinate) and deterministically performs the physical
action, returning a structured receipt.  Global task switching / curriculum priority
belongs to L3.
"""

from __future__ import annotations

import math
import time
from typing import Any, Iterable

from ..errors import ApiError

# --- Block classification -------------------------------------------------

WATER_BLOCKS: frozenset[str] = frozenset({"water", "flowing_water", "bubble_column"})
LAVA_BLOCKS: frozenset[str] = frozenset({"lava", "flowing_lava"})
AIR_BLOCKS: frozenset[str] = frozenset({"air", "cave_air", "void_air"})
# Passable plants: a bot can stand in these cells, so shore scans must not treat them as walls.
PASSABLE_PLANTS: frozenset[str] = frozenset(
    {
        "short_grass",
        "tall_grass",
        "fern",
        "large_fern",
        "firefly_bush",
        "dandelion",
        "poppy",
        "blue_orchid",
        "allium",
        "azure_bluet",
        "oxeye_daisy",
        "cornflower",
        "lily_of_the_valley",
        "torchflower",
        "pink_petals",
        "wildflowers",
        "leaf_litter",
        "dead_bush",
        "sweet_berry_bush",
        "wheat",
        "snow",
    }
)

SOFT_TRAP_BLOCKS: frozenset[str] = frozenset(
    {
        "cobweb",
        "sweet_berry_bush",
        "bamboo",
        "bamboo_sapling",
        "powder_snow",
        "magma_block",
    }
)
GRAVITY_BLOCKS: frozenset[str] = frozenset(
    {"sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel"}
)
FRAGILE_SNOW_BLOCKS: frozenset[str] = frozenset({"ice", "packed_ice", "blue_ice", "frosted_ice"})

FILLER_PRIORITY: tuple[str, ...] = (
    "cobblestone",
    "cobbled_deepslate",
    "deepslate",
    "stone",
    "andesite",
    "diorite",
    "granite",
    "tuff",
    "dirt",
    "netherrack",
    "blackstone",
    "basalt",
    "end_stone",
    "oak_planks",
    "spruce_planks",
    "birch_planks",
)
# Fillers that must never be consumed for bridging (utility / valuable / gravity).
UNSAFE_FILLERS: frozenset[str] = frozenset(
    {
        "sand",
        "red_sand",
        "gravel",
        "suspicious_sand",
        "suspicious_gravel",
        "crafting_table",
        "furnace",
        "chest",
        "trapped_chest",
        "barrel",
        "iron_block",
        "gold_block",
        "diamond_block",
        "emerald_block",
        "netherite_block",
        "ancient_debris",
        "obsidian",
        "crying_obsidian",
    }
)
BOAT_ITEMS: tuple[str, ...] = (
    "oak_boat",
    "spruce_boat",
    "birch_boat",
    "jungle_boat",
    "acacia_boat",
    "dark_oak_boat",
    "mangrove_boat",
    "cherry_boat",
    "pale_oak_boat",
    "oak_chest_boat",
)

TRAP_CLEAR_STOP_ON: tuple[str, ...] = ("death",)
MAX_RECOVERY_ATTEMPTS = 2


def _clean(name: Any) -> str:
    return str(name or "").removeprefix("minecraft:").strip().lower()


def is_trap_block(name: str) -> bool:
    clean = _clean(name)
    return clean in SOFT_TRAP_BLOCKS or clean.endswith("_leaves") or clean.endswith("_vine")


def is_door_block(name: str) -> bool:
    clean = _clean(name)
    return clean.endswith("_door") or clean.endswith("_fence_gate") or clean.endswith("_trapdoor")


def is_fence_block(name: str) -> bool:
    clean = _clean(name)
    return clean.endswith("_fence") or clean.endswith("_wall")


def is_gravity_block(name: str) -> bool:
    return _clean(name) in GRAVITY_BLOCKS


def is_waterlike(name: str) -> bool:
    return _clean(name) in WATER_BLOCKS


def _floor_pos(st: dict[str, Any]) -> tuple[int, int, int]:
    p = st.get("pos") or [0.0, 64.0, 0.0]
    return (math.floor(float(p[0])), math.floor(float(p[1])), math.floor(float(p[2])))


def _dominant_axis(dx: float, dz: float) -> tuple[int, int]:
    if abs(dx) >= abs(dz):
        return (1 if dx > 0 else -1 if dx < 0 else 0, 0)
    return (0, 1 if dz > 0 else -1)


class BreadcrumbTrail:
    """Per-dimension ring stack of physically connected waypoints + reverse retreat."""

    def __init__(
        self,
        bot: Any,
        *,
        capacity: int = 48,
        min_step: float = 6.0,
        max_step: float = 20.0,
        teleport_jump: float = 48.0,
    ) -> None:
        self.bot = bot
        self.capacity = max(4, int(capacity))
        self.min_step = float(min_step)
        self.max_step = float(max_step)
        self.teleport_jump = float(teleport_jump)
        self.stacks: dict[str, list[list[float]]] = {}
        self.segments: dict[str, int] = {}
        self.last_note: dict[str, Any] = {}

    # -- probing ---------------------------------------------------------
    def _block(self, pos: tuple[int, int, int]) -> str:
        try:
            res = self.bot.call("get_block", {"pos": [pos[0], pos[1], pos[2]]})
        except ApiError:
            return "unknown"
        return _clean(res.get("name") or res.get("block") or "air")

    def _stack(self, dim: str) -> list[list[float]]:
        return self.stacks.setdefault(dim, [])

    # -- sampling --------------------------------------------------------
    def note_status(self, st: dict[str, Any] | None = None) -> bool:
        """Push a waypoint when standing on stable non-trap ground, 6m <= d <= 20m from top."""
        if st is None:
            try:
                st = self.bot.status()
            except ApiError:
                return False
        if not isinstance(st, dict):
            return False
        if not st.get("on_ground") or st.get("in_water") or st.get("in_lava") or st.get("in_vehicle"):
            return False
        dim = str(st.get("dimension") or "overworld")
        bx, by, bz = _floor_pos(st)
        under = self._block((bx, by - 1, bz))
        self.last_note = {"dim": dim, "under": under, "pos": [bx, by, bz]}
        if under in AIR_BLOCKS or under in WATER_BLOCKS or under in LAVA_BLOCKS or under == "unknown":
            return False
        if is_trap_block(under) or is_gravity_block(under) or under in {"magma_block", "slime_block"}:
            return False
        stack = self._stack(dim)
        cur = [float(st["pos"][0]), float(st["pos"][1]), float(st["pos"][2])]
        if not stack:
            stack.append([bx, by, bz])
            self.segments[dim] = self.segments.get(dim, 0)
            return True
        last = stack[-1]
        dist = math.dist(cur, [float(last[0]) + 0.5, float(last[1]), float(last[2]) + 0.5])
        if dist > self.teleport_jump:
            # teleport / death respawn / dimension hop: start a new segment
            stack.clear()
            stack.append([bx, by, bz])
            self.segments[dim] = self.segments.get(dim, 0) + 1
            return True
        if self.min_step <= dist <= self.max_step:
            stack.append([bx, by, bz])
            while len(stack) > self.capacity:
                stack.pop(0)
            return True
        return False

    def points(self, dim: str | None = None) -> list[list[int]]:
        if dim is not None:
            return [list(map(int, p)) for p in self.stacks.get(str(dim), [])]
        out: list[list[int]] = []
        for pts in self.stacks.values():
            out.extend(list(map(int, p)) for p in pts)
        return out

    def backtrack(
        self,
        *,
        steps: int = 3,
        max_dist: float = 24.0,
        dim: str | None = None,
        move_fn: Any = None,
    ) -> dict[str, Any]:
        """Walk back along the most recent waypoints in reverse order."""
        st = self.bot.status()
        dim = str(dim or st.get("dimension") or "overworld")
        stack = self.stacks.get(dim) or []
        cur = [float(v) for v in (st.get("pos") or [0.0, 64.0, 0.0])]
        visited: list[list[int]] = []
        travelled = 0.0
        while stack and len(visited) < max(1, int(steps)):
            pt = stack[-1]
            d = math.dist(cur, [pt[0] + 0.5, pt[1], pt[2] + 0.5])
            if d > max_dist - travelled:
                break
            try:
                if move_fn is not None:
                    move_fn(pt)
                else:
                    self.bot.call(
                        "move_to",
                        {"x": pt[0] + 0.5, "z": pt[2] + 0.5, "y": float(pt[1]), "range": 1.2},
                    )
            except ApiError as err:
                return {
                    "ok": False,
                    "error": err.code,
                    "message": err.message,
                    "visited": visited,
                    "remaining": len(stack),
                    "dim": dim,
                }
            stack.pop()
            visited.append([int(pt[0]), int(pt[1]), int(pt[2])])
            travelled += d
            cur = [pt[0] + 0.5, pt[1], pt[2] + 0.5]
        return {
            "ok": bool(visited),
            "visited": visited,
            "distance": round(travelled, 2),
            "remaining": len(stack),
            "dim": dim,
            "reason": None if visited else "no_breadcrumbs",
        }

    def clear(self, dim: str | None = None) -> None:
        if dim is None:
            self.stacks.clear()
        else:
            self.stacks.pop(str(dim), None)


class TerrainNavigator:
    """Deterministic local terrain-crossing primitives for ``navigate_segmented``."""

    def __init__(self, bot: Any, *, skills: Any = None, trail: BreadcrumbTrail | None = None) -> None:
        self.bot = bot
        self.skills = skills
        self.trail = trail if trail is not None else BreadcrumbTrail(bot)

    # -- small helpers ---------------------------------------------------
    def note_status(self, st: dict[str, Any] | None = None) -> bool:
        return self.trail.note_status(st)

    def _block(self, pos: Iterable[int]) -> str:
        p = list(pos)
        try:
            res = self.bot.call("get_block", {"pos": [int(p[0]), int(p[1]), int(p[2])]})
        except ApiError:
            return "unknown"
        return _clean(res.get("name") or res.get("block") or "air")

    def _properties(self, pos: Iterable[int]) -> dict[str, Any]:
        p = list(pos)
        try:
            res = self.bot.call("get_block", {"pos": [int(p[0]), int(p[1]), int(p[2])]})
        except ApiError:
            return {}
        props = res.get("properties")
        return props if isinstance(props, dict) else {}

    def _counts(self) -> dict[str, int]:
        try:
            inv = self.bot.inventory()
        except ApiError:
            return {}
        out: dict[str, int] = {}
        for row in (inv.get("items") or []):
            if not isinstance(row, dict):
                continue
            name = _clean(row.get("name"))
            if name:
                out[name] = out.get(name, 0) + int(row.get("count") or 0)
        return out

    def _pick_filler(self, counts: dict[str, int] | None = None) -> str | None:
        counts = counts if counts is not None else self._counts()
        for name in FILLER_PRIORITY:
            if name in UNSAFE_FILLERS:
                continue
            if int(counts.get(name, 0)) > 0:
                return name
        for name, cnt in sorted(counts.items()):
            if cnt <= 0 or name in UNSAFE_FILLERS:
                continue
            if name.endswith(("_planks", "_log", "_stem", "_hyphae")):
                continue
            if name.endswith(("_slab", "_stairs", "_wall", "_fence")):
                continue
            if any(k in name for k in ("shulker", "bed", "boat", "bucket", "sword", "pickaxe", "axe", "hoe", "shovel")):
                continue
            return name
        return None

    def _face(self, x: float, z: float, y: float | None = None) -> None:
        payload: dict[str, Any] = {"x": float(x), "z": float(z)}
        if y is not None:
            payload["y"] = float(y)
        try:
            self.bot.call("look", payload)
        except ApiError:
            pass

    def _step_forward(self, ms: int = 700, *, jump: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"forward": True, "ms": max(50, min(3000, int(ms))), "stop_on": ["death"]}
        if jump:
            payload["jump"] = True
        try:
            return self.bot.call("control", payload)
        except ApiError as err:
            return {"ok": False, "error": err.code, "message": err.message}

    def _probe_around(self, st: dict[str, Any] | None = None) -> dict[str, Any]:
        st = st if st is not None else self.bot.status()
        bx, by, bz = _floor_pos(st)
        cells: dict[str, str] = {
            "feet": self._block((bx, by, bz)),
            "head": self._block((bx, by + 1, bz)),
            "below": self._block((bx, by - 1, bz)),
        }
        for name, (dx, dz) in (
            ("north", (0, -1)),
            ("south", (0, 1)),
            ("east", (1, 0)),
            ("west", (-1, 0)),
        ):
            cells[f"{name}_feet"] = self._block((bx + dx, by, bz + dz))
            cells[f"{name}_head"] = self._block((bx + dx, by + 1, bz + dz))
            cells[f"{name}_below"] = self._block((bx + dx, by - 1, bz + dz))
        return {"pos": [bx, by, bz], "cells": cells, "status": st}

    # -- primitive 1: soft traps + doors ---------------------------------
    def clear_traps_and_doors(
        self,
        *,
        dx: int | None = None,
        dz: int | None = None,
        max_ops: int = 8,
        clear_radius: int = 1,
        door_radius: int = 5,
    ) -> dict[str, Any]:
        """Break berry bushes / cobwebs / bamboo / leaves / powder snow and open wooden doors."""
        st = self.bot.status()
        bx, by, bz = _floor_pos(st)
        counts = self._counts()
        cleared: list[dict[str, Any]] = []
        opened: list[dict[str, Any]] = []
        scooped: list[dict[str, Any]] = []
        errors: list[str] = []

        dirs: list[tuple[int, int]] = []
        if dx or dz:
            dirs.append((int(dx or 0), int(dz or 0)))
        for d in ((0, -1), (0, 1), (1, 0), (-1, 0)):
            if d not in dirs:
                dirs.append(d)

        ops = 0
        # 1) own collision box first (berry bush / cobweb / leaves can be inside feet/head)
        for cell in ((bx, by, bz), (bx, by + 1, bz)):
            if ops >= max_ops:
                break
            name = self._block(cell)
            if not is_trap_block(name):
                continue
            if _clean(name) == "powder_snow" and counts.get("bucket", 0) > 0:
                try:
                    self.bot.call("interact", {"pos": list(cell), "item": "bucket"})
                    counts["bucket"] = counts.get("bucket", 0) - 1
                    counts["powder_snow_bucket"] = counts.get("powder_snow_bucket", 0) + 1
                    scooped.append({"pos": list(cell), "block": "powder_snow"})
                    ops += 1
                    continue
                except ApiError as err:
                    errors.append(f"bucket_scoop:{err.code}")
            try:
                res = self.bot.call(
                    "dig",
                    {"pos": list(cell), "collect": True, "stop_on": list(TRAP_CLEAR_STOP_ON)},
                )
                cleared.append({"pos": list(cell), "block": _clean(name), "dug": _clean(res.get("dug") or name)})
                ops += 1
            except ApiError as err:
                errors.append(f"dig:{err.code}@{cell}")

        # 2) forward cells within `clear_radius`
        for r in range(1, max(1, clear_radius) + 1):
            for ddx, ddz in dirs:
                if ops >= max_ops:
                    break
                cx, cz = bx + ddx * r, bz + ddz * r
                for cy in (by, by + 1):
                    if ops >= max_ops:
                        break
                    name = self._block((cx, cy, cz))
                    if not name or name == "unknown":
                        continue
                    if is_door_block(name):
                        props = self._properties((cx, cy, cz))
                        if props.get("open") is True:
                            continue
                        try:
                            self.bot.call("interact", {"pos": [cx, cy, cz]})
                            opened.append({"pos": [cx, cy, cz], "block": name})
                            ops += 1
                            continue
                        except ApiError as err:
                            errors.append(f"interact_door:{err.code}@{cx},{cy},{cz}")
                    if is_fence_block(name):
                        gate = self._find_gate_near(bx, by, bz, radius=6)
                        if gate is not None:
                            # Gate opening is handled once by the door sweep below (avoids double-toggle).
                            try:
                                self.bot.call(
                                    "move_to", {"x": gate[0] + 0.5, "z": gate[2] + 0.5, "range": 2.2}
                                )
                            except ApiError:
                                pass
                            continue
                        # No gate: dig the fence block (never blind-jump a 1.5-high fence).
                        try:
                            res = self.bot.call(
                                "dig",
                                {"pos": [cx, cy, cz], "collect": True, "stop_on": list(TRAP_CLEAR_STOP_ON)},
                            )
                            cleared.append(
                                {"pos": [cx, cy, cz], "block": _clean(name), "dug": _clean(res.get("dug") or name)}
                            )
                            ops += 1
                        except ApiError as err:
                            filler = self._pick_filler(counts)
                            if filler:
                                try:
                                    self.bot.call("place", {"item": filler, "pos": [cx, cy, cz]})
                                    counts[filler] = max(0, counts.get(filler, 0) - 1)
                                    ops += 1
                                except ApiError as place_err:
                                    errors.append(f"place_fence:{place_err.code}")
                            else:
                                errors.append(f"dig_fence:{err.code}")
                        continue
                    if not is_trap_block(name):
                        continue
                    if _clean(name) == "powder_snow" and counts.get("bucket", 0) > 0:
                        try:
                            self.bot.call("interact", {"pos": [cx, cy, cz], "item": "bucket"})
                            counts["bucket"] = counts.get("bucket", 0) - 1
                            scooped.append({"pos": [cx, cy, cz], "block": "powder_snow"})
                            ops += 1
                            continue
                        except ApiError as err:
                            errors.append(f"bucket_scoop:{err.code}")
                    try:
                        res = self.bot.call(
                            "dig",
                            {"pos": [cx, cy, cz], "collect": True, "stop_on": list(TRAP_CLEAR_STOP_ON)},
                        )
                        cleared.append(
                            {"pos": [cx, cy, cz], "block": _clean(name), "dug": _clean(res.get("dug") or name)}
                        )
                        ops += 1
                    except ApiError as err:
                        errors.append(f"dig:{err.code}@{cx},{cy},{cz}")

        # 3) nearby closed doors / fence gates (sweep within `door_radius`) -----------
        if door_radius and door_radius > 0:
            gate_names = [
                f"{wood}_{suffix}"
                for wood in ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak", "crimson", "warped")
                for suffix in ("door", "fence_gate")
            ]
            try:
                found = self.bot.call(
                    "find_blocks", {"radius": int(door_radius), "types": gate_names, "limit": 8}
                )
                cands = []
                for b in found.get("blocks") or []:
                    if not isinstance(b, dict) or not isinstance(b.get("pos"), list):
                        continue
                    pt = [int(v) for v in b["pos"][:3]]
                    if pt not in cands:  # de-dupe: double interact would re-close the door
                        cands.append(pt)
            except ApiError:
                cands = []
            cands.sort(key=lambda p: (p[1] != by) + math.dist(p, [bx, by, bz]))
            for pos in cands:
                if ops >= max_ops:
                    break
                props = self._properties(pos)
                if props.get("open") is True or props.get("powered") is True:
                    continue
                name = self._block(pos)
                if not is_door_block(name):
                    continue
                try:
                    self.bot.call("move_to", {"x": pos[0] + 0.5, "z": pos[2] + 0.5, "range": 2.0})
                except ApiError:
                    pass
                try:
                    self.bot.call("interact", {"pos": pos})
                    opened.append({"pos": pos, "block": name, "sweep": True})
                    ops += 1
                except ApiError as err:
                    errors.append(f"interact_sweep:{err.code}@{pos}")

        # 4) submerged under ice: break the ceiling for air
        ice_broken: list[list[int]] = []
        if st.get("submerged"):
            for dy in (1, 2, 3):
                head = (bx, by + dy, bz)
                name = self._block(head)
                if _clean(name) in FRAGILE_SNOW_BLOCKS:
                    try:
                        self.bot.call("dig", {"pos": list(head), "collect": True, "stop_on": ["death"]})
                        ice_broken.append(list(head))
                        ops += 1
                    except ApiError as err:
                        errors.append(f"ice:{err.code}")
                    break

        return {
            "ok": bool(cleared or opened or scooped or ice_broken),
            "cleared": cleared,
            "opened": opened,
            "scooped": scooped,
            "ice_broken": ice_broken,
            "errors": errors[:6],
            "ops": ops,
        }

    def _find_gate_near(self, bx: int, by: int, bz: int, *, radius: int = 6) -> list[int] | None:
        gate_names = [
            f"{wood}_fence_gate"
            for wood in ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "pale_oak", "crimson", "warped")
        ]
        try:
            found = self.bot.call(
                "find_blocks", {"radius": int(radius), "types": gate_names, "limit": 5}
            )
        except ApiError:
            return None
        blocks = found.get("blocks") or []
        cands = [b for b in blocks if isinstance(b, dict) and isinstance(b.get("pos"), list)]
        if not cands:
            return None
        cands.sort(key=lambda b: abs(int(b["pos"][1]) - by))
        return [int(v) for v in cands[0]["pos"][:3]]

    def _advance_to_cell(self, tx: float, tz: float, *, tol: float = 0.32, max_bursts: int = 8) -> float:
        """Walk to a cell centre with short proportional bursts (deterministic, no overshoot)."""
        total = 0.0
        last = None
        for _ in range(max_bursts):
            st = self.bot.status()
            p = st.get("pos") or [0.0, 64.0, 0.0]
            dist = math.hypot(float(tx) - float(p[0]), float(tz) - float(p[2]))
            if dist <= tol:
                return total
            self._face(tx, tz, float(p[1]))
            # sneaking speed is ~1.3 m/s; small bursts keep us on the 1-wide bridge
            ms = int(max(90, min(520, (dist / 1.25) * 1000)))
            before = [float(p[0]), float(p[2])]
            self._step_forward(ms=ms, jump=False)
            st2 = self.bot.status()
            p2 = st2.get("pos") or p
            step = math.hypot(float(p2[0]) - before[0], float(p2[2]) - before[1])
            total += step
            if step < 0.03:
                if last is not None and last < 0.03:
                    break
            last = step
        return total

    # -- primitive 2: sneak bridge ---------------------------------------
    def sneak_bridge(
        self,
        x: float,
        z: float,
        *,
        max_steps: int = 20,
        filler: str | None = None,
        reinforce_gravity: bool = True,
    ) -> dict[str, Any]:
        """Sneak-lock and place one filler block per step until reaching solid ground."""
        st = self.bot.status()
        bx, by, bz = _floor_pos(st)
        counts = self._counts()
        filler = filler or self._pick_filler(counts)
        dx, dz = _dominant_axis(float(x) - (bx + 0.5), float(z) - (bz + 0.5))
        if dx == 0 and dz == 0:
            return {"ok": False, "error": "bad_args", "message": "目标与当前水平坐标重合，无需搭桥。"}

        below = self._block((bx, by - 1, bz))
        below2 = self._block((bx, by - 2, bz))
        if is_gravity_block(below) and (below2 in AIR_BLOCKS or below2 in WATER_BLOCKS):
            # step back onto stable ground first
            self._face(bx - dx * 4 + 0.5, bz - dz * 4 + 0.5)
            self._step_forward(ms=700, jump=False)
            st = self.bot.status()
            bx, by, bz = _floor_pos(st)
            below = self._block((bx, by - 1, bz))
            below2 = self._block((bx, by - 2, bz))
            if is_gravity_block(below) and (below2 in AIR_BLOCKS or below2 in WATER_BLOCKS):
                return {
                    "ok": False,
                    "error": "unsafe_gravity_liftoff",
                    "message": f"起桥点脚下是悬空 {below}（下方 {below2}），先退回实心地面或垫实底层。",
                    "pos": [bx, by, bz],
                }

        if not filler:
            return {
                "ok": False,
                "error": "missing_item",
                "message": "背包没有可用的非重力实心垫脚方块（圆石/深板岩/泥土等）。",
            }
        if int(counts.get(filler, 0)) < 4:
            return {
                "ok": False,
                "error": "missing_item",
                "message": f"垫脚方块 {filler} 仅剩 {counts.get(filler, 0)}，不足以搭桥。",
            }

        placed: list[dict[str, Any]] = []
        reinforced: list[dict[str, Any]] = []
        steps = 0
        sneaking = False
        landed = False
        error: str | None = None
        try:
            self.bot.call("posture", {"sneak": True})
            sneaking = True
            while steps < max_steps:
                st = self.bot.status()
                cx, cy, cz = _floor_pos(st)
                if math.hypot(float(x) - (cx + 0.5), float(z) - (cz + 0.5)) <= 1.6:
                    break
                nxt = (cx + dx, cy, cz + dz)
                support = (nxt[0], nxt[1] - 1, nxt[2])
                support_name = self._block(support)
                support_below = self._block((support[0], support[1] - 1, support[2]))
                # floating sand/gravel between shore and chasm: reinforce instead of stepping on it
                if (
                    reinforce_gravity
                    and is_gravity_block(support_name)
                    and (support_below in AIR_BLOCKS or support_below in WATER_BLOCKS)
                ):
                    try:
                        self.bot.call(
                            "dig", {"pos": list(support), "collect": True, "stop_on": list(TRAP_CLEAR_STOP_ON)}
                        )
                        self.bot.call("place", {"item": filler, "pos": list(support)})
                        counts[filler] = max(0, counts.get(filler, 0) - 1)
                        reinforced.append({"pos": list(support), "replaced": support_name})
                    except ApiError as err:
                        error = f"reinforce:{err.code}"
                        break
                elif support_name in AIR_BLOCKS or support_name in WATER_BLOCKS or support_name in LAVA_BLOCKS:
                    if int(counts.get(filler, 0)) <= 0:
                        error = "missing_item"
                        break
                    try:
                        res = self.bot.call("place", {"item": filler, "pos": list(support)})
                        counts[filler] = max(0, counts.get(filler, 0) - 1)
                        placed.append({"pos": list(support), "item": filler, "placed": _clean(res.get("placed") or filler)})
                    except ApiError as err:
                        error = f"place:{err.code}"
                        break
                elif is_trap_block(support_name):
                    try:
                        self.bot.call(
                            "dig", {"pos": list(support), "collect": True, "stop_on": list(TRAP_CLEAR_STOP_ON)}
                        )
                    except ApiError:
                        pass
                # advance exactly one block while sneak-locked (proportional short bursts,
                # never overshoot the 1-wide bridge: overshoot is what makes `/place` report no_support)
                moved = self._advance_to_cell(nxt[0] + 0.5, nxt[2] + 0.5, tol=0.32)
                if moved < 0.3:
                    error = "stalled_on_bridge"
                    break
                steps += 1
            # landed?
            st = self.bot.status()
            fx, fy, fz = _floor_pos(st)
            landed = False
            if math.hypot(float(x) - (fx + 0.5), float(z) - (fz + 0.5)) <= 2.0:
                ahead = self._block((fx + dx, fy - 1, fz + dz))
                landed = ahead not in AIR_BLOCKS and ahead not in WATER_BLOCKS and ahead not in LAVA_BLOCKS
        finally:
            if sneaking:
                try:
                    self.bot.call("posture", {})
                except ApiError:
                    pass

        st = self.bot.status()
        return {
            "ok": error is None and steps > 0,
            "op": "sneak_bridge",
            "error": error,
            "steps": steps,
            "placed": placed,
            "reinforced_gravity": reinforced,
            "pos": st.get("pos"),
            "sneaking": bool(st.get("sneaking")),
            "filler": filler,
            "landed": landed,
        }

    # -- primitive 3: cliffs ---------------------------------------------
    def climb_or_descend_cliff(
        self,
        target_y: float | None = None,
        *,
        dx: int | None = None,
        dz: int | None = None,
        max_steps: int = 14,
        allow_dig: bool = True,
        allow_water_bucket: bool = True,
    ) -> dict[str, Any]:
        """Ascend/descend a >=4 block cliff without bare jumping or frame-perfect MLG."""
        st = self.bot.status()
        bx, by, bz = _floor_pos(st)
        ty = float(target_y) if target_y is not None else float(by)
        dy = ty - by
        if abs(dy) < 2:
            return {"ok": True, "op": "none", "dy": dy, "reason": "vertical_gap_too_small"}
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        if dx is None or dz is None:
            p = st.get("pos") or [0, 64, 0]
            yaw = float(st.get("yaw") or 0.0)
            fdx, fdz = -math.sin(yaw), -math.cos(yaw)
            ddx, ddz = _dominant_axis(fdx, fdz)
        else:
            ddx, ddz = _dominant_axis(float(dx), float(dz))
        direction = next((k for k, v in dirs.items() if v == (ddx, ddz)), "north")

        if dy > 0:
            if allow_dig and self.skills is not None and hasattr(self.skills, "dig_staircase"):
                wall_solid = all(
                    self._block((bx + ddx * 1, by + i, bz + ddz * 1)) not in AIR_BLOCKS
                    for i in (0, 1, 2)
                )
                if wall_solid:
                    try:
                        res = self.skills.dig_staircase(
                            direction=direction, steps=int(min(max_steps, dy + 2)), vertical="up"
                        )
                        st_after = self.bot.status()
                        gained = _floor_pos(st_after)[1] - by
                        return {
                            "ok": gained >= max(1, int(dy) - 1),
                            "op": "dig_staircase_up",
                            "gained": gained,
                            "result": res if isinstance(res, dict) else str(res)[:400],
                        }
                    except (ApiError, TypeError, ValueError) as err:
                        if isinstance(err, ApiError) and err.code in ("hurt", "death", "interrupted"):
                            raise
                        # fall through to step-climb
            return self._step_climb(by=by, ddx=ddx, ddz=ddz, dy=int(dy), max_steps=max_steps)

        # dy < 0: descend
        if allow_dig and self.skills is not None and hasattr(self.skills, "dig_staircase"):
            diggable = all(
                self._block((bx + ddx * 1, by - i, bz + ddz * 1)) not in WATER_BLOCKS
                for i in (1, 2)
            )
            if diggable:
                try:
                    res = self.skills.dig_staircase(
                        direction=direction, steps=int(min(max_steps, abs(dy) + 2)), vertical="down"
                    )
                    st_after = self.bot.status()
                    dropped = by - _floor_pos(st_after)[1]
                    if dropped >= 1:
                        return {
                            "ok": dropped >= max(1, int(abs(dy)) - 2),
                            "op": "dig_staircase_down",
                            "dropped": dropped,
                            "result": res if isinstance(res, dict) else str(res)[:400],
                        }
                except (ApiError, TypeError, ValueError) as err:
                    if isinstance(err, ApiError) and err.code in ("hurt", "death", "interrupted"):
                        raise
        if allow_water_bucket and self._counts().get("water_bucket", 0) > 0:
            return self._water_descend(by=by, ddx=ddx, ddz=ddz, dirs=dirs)
        return self._ledge_descend(by=by, drop=int(abs(dy)), ddx=ddx, ddz=ddz)

    def _step_climb(self, *, by: int, ddx: int, ddz: int, dy: int, max_steps: int) -> dict[str, Any]:
        """Walk/jump up 1-block steps; only place filler when a step is genuinely unreachable."""
        counts = self._counts()
        filler = self._pick_filler(counts)
        placed: list[dict[str, Any]] = []
        rises = 0
        try:
            for _ in range(min(max_steps, dy + 2)):
                st = self.bot.status()
                cx, cy, cz = _floor_pos(st)
                if cy >= by + dy:
                    break
                tx, tz = cx + ddx + 0.5, cz + ddz + 0.5
                # 1) natural step: pathfinder walks up one block
                try:
                    self.bot.call(
                        "move_to",
                        {"x": tx, "z": tz, "range": 0.4, "stop_on": ["death"], "timeout": 4000},
                    )
                except ApiError:
                    pass
                st2 = self.bot.status()
                cy2 = _floor_pos(st2)[1]
                if cy2 > cy:
                    rises += cy2 - cy
                    continue
                # 2) blocked: place filler on top of the ground in front (supported by that ground)
                front_solid = self._block((cx + ddx, cy, cz + ddz)) not in AIR_BLOCKS
                if not front_solid and filler and int(counts.get(filler, 0)) > 0:
                    below_front = self._block((cx + ddx, cy - 1, cz + ddz))
                    if below_front not in AIR_BLOCKS:
                        try:
                            self.bot.call("place", {"item": filler, "pos": [cx + ddx, cy, cz + ddz]})
                            counts[filler] = counts.get(filler, 0) - 1
                            placed.append({"pos": [cx + ddx, cy, cz + ddz], "item": filler})
                        except ApiError as err:
                            if err.code in ("death", "hurt", "interrupted"):
                                raise
                # 3) jump onto / over the step
                self._face(tx, tz, float(cy + 1))
                self._step_forward(ms=520, jump=True)
                st3 = self.bot.status()
                cy3 = _floor_pos(st3)[1]
                if cy3 > cy:
                    rises += cy3 - cy
                else:
                    self._step_forward(ms=420, jump=True)
                    cy4 = _floor_pos(self.bot.status())[1]
                    if cy4 > cy:
                        rises += cy4 - cy
                    else:
                        break
        finally:
            try:
                self.bot.call("posture", {})
            except ApiError:
                pass
        st = self.bot.status()
        return {
            "ok": rises > 0 and _floor_pos(st)[1] > by,
            "op": "step_climb",
            "rises": rises,
            "placed": placed,
            "pos": st.get("pos"),
        }

    def _water_descend(self, *, by: int, ddx: int, ddz: int, dirs: dict[str, tuple[int, int]]) -> dict[str, Any]:
        st = self.bot.status()
        bx, by, bz = _floor_pos(st)
        edge = (bx + ddx * 1, by - 1, bz + ddz * 1)
        try:
            self.bot.call("interact", {"pos": list(edge), "item": "water_bucket"})
        except ApiError as err:
            return {"ok": False, "error": f"water:{err.code}", "op": "water_descend"}
        self._face(bx + ddx * 3 + 0.5, bz + ddz * 3 + 0.5, float(by - 2))
        self._step_forward(ms=900)
        time.sleep(0.8)
        try:
            self.bot.call("interact", {"pos": [bx + ddx, by - 2, bz + ddz], "item": "bucket"})
        except ApiError:
            pass
        st2 = self.bot.status()
        return {
            "ok": _floor_pos(st2)[1] < by,
            "op": "water_descend",
            "from_y": by,
            "to_y": _floor_pos(st2)[1],
            "pos": st2.get("pos"),
        }

    def _ledge_descend(self, *, by: int, drop: int, ddx: int = 0, ddz: int = 0) -> dict[str, Any]:
        """Hop down ledges <=3 blocks (fall damage starts above 3 blocks)."""
        hops: list[dict[str, Any]] = []
        remaining = drop
        while remaining > 0:
            # walk to the current ledge's edge first: scanning from the middle cannot see the drop
            if ddx or ddz:
                for _ in range(20):
                    st0 = self.bot.status()
                    bx0, cy0, bz0 = _floor_pos(st0)
                    ahead_ground = self._block((bx0 + ddx, cy0 - 1, bz0 + ddz))
                    if ahead_ground in AIR_BLOCKS or ahead_ground in WATER_BLOCKS or ahead_ground in LAVA_BLOCKS:
                        break
                    self._face(bx0 + ddx + 0.5, bz0 + ddz + 0.5, float(cy0))
                    self._step_forward(ms=260, jump=False)
            st = self.bot.status()
            bx, cy, bz = _floor_pos(st)
            landed = False
            for dx, dz in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                target_y = cy - min(3, remaining)
                ground = self._block((bx + dx, target_y - 1, bz + dz))
                clear = self._block((bx + dx, target_y, bz + dz))
                if ground not in AIR_BLOCKS and ground not in WATER_BLOCKS and clear in AIR_BLOCKS:
                    self._face(bx + dx + 0.5, bz + dz + 0.5, float(target_y))
                    self._step_forward(ms=600)
                    st2 = self.bot.status()
                    new_y = _floor_pos(st2)[1]
                    if new_y < cy:
                        # wait for the fall to settle before reading the landing level
                        settle_deadline = time.monotonic() + 1.6
                        while time.monotonic() < settle_deadline:
                            st_settle = self.bot.status()
                            if st_settle.get("on_ground"):
                                new_y = _floor_pos(st_settle)[1]
                                break
                            time.sleep(0.1)
                        hops.append({"from": cy, "to": new_y})
                        remaining -= max(1, cy - new_y)
                        landed = True
                        break
            if not landed:
                break
        st = self.bot.status()
        return {
            "ok": remaining <= 0 or _floor_pos(st)[1] < by,
            "op": "ledge_descend",
            "hops": hops,
            "pos": st.get("pos"),
        }

    # -- primitive 4: boat crossing --------------------------------------
    def cross_water_with_boat(
        self,
        x: float,
        z: float,
        *,
        segment_m: float = 12.0,
        shore_margin: float = 1.2,
        max_segments: int = 8,
    ) -> dict[str, Any]:
        """Place a boat, drive a 10-14 m polyline, dismount <=1.2 m from shore, recover the boat."""
        st = self.bot.status()
        counts = self._counts()
        boat = next((b for b in BOAT_ITEMS if counts.get(b, 0) > 0), None)
        crafted = False
        if boat is None:
            for log_name in ("oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log"):
                if counts.get(log_name, 0) >= 3:
                    try:
                        self.bot.call("craft", {"item": "oak_boat", "count": 1})
                        boat = "oak_boat"
                        crafted = True
                        break
                    except ApiError:
                        continue
        if boat is None:
            return {
                "ok": False,
                "error": "missing_item",
                "op": "cross_water_with_boat",
                "message": "背包没有船，木板/原木也不足以合成。",
            }

        bx, by, bz = _floor_pos(st)
        dx, dz = _dominant_axis(float(x) - (bx + 0.5), float(z) - (bz + 0.5))
        # water cell in front of the feet (boats float on the surface block)
        launch_candidates: list[list[int]] = []

        def _push_launch(cell: tuple[int, int, int]) -> None:
            pt = [cell[0], cell[1], cell[2]]
            if pt not in launch_candidates:
                launch_candidates.append(pt)

        if st.get("in_water"):
            # Float to the surface first: placing a boat from 2+ blocks underwater hangs use_item.
            try:
                self.bot.call("posture", {"jump": True})
                time.sleep(1.0)
            except ApiError:
                pass
            st = self.bot.status()
            bx, by, bz = _floor_pos(st)
            # Already swimming: place the boat 3-5 blocks away (own/1-2 block cells hang the
            # server-side use_item). This works from water and avoids a long swim to a far bank.
            surf = self._water_surface_y(bx, by, bz)
            if surf is not None:
                for r in (3, 4, 5):
                    for ddx, ddz in ((dx, dz), (1, 0), (-1, 0), (0, 1), (0, -1)):
                        cand = (bx + ddx * r, surf, bz + ddz * r)
                        if self._block(cand) in WATER_BLOCKS and self._block((cand[0], cand[1] + 1, cand[2])) in AIR_BLOCKS:
                            _push_launch(cand)
        # scan outward in all 4 axes, preferring >=3 blocks: a boat 1-2 blocks from the player's
        # hitbox makes the server wait for a placement that never resolves (>20s timeout).
        dirs = ((dx, dz), (dz, -dx), (-dz, dx), (-dx, -dz))
        for radius_set in ((3, 4, 5), (2, 1)):
            for ddx, ddz in dirs:
                for r in radius_set:
                    for oy in (0, -1, -2, -3, -4):
                        cell = (bx + ddx * r, by + oy, bz + ddz * r)
                        name = self._block(cell)
                        if name in WATER_BLOCKS:
                            above = self._block((cell[0], cell[1] + 1, cell[2]))
                            if above in AIR_BLOCKS:
                                _push_launch(cell)
                                break
        if not launch_candidates and st.get("in_water"):
            # no open water nearby: swim to a bank, then rescan once
            shore = self._nearest_solid_shore(float(bx), float(bz), float(by)) or self._find_far_shore()
            self._swim_to_bank(shore, max_bursts=30)
            st = self.bot.status()
            bx, by, bz = _floor_pos(st)
            if not st.get("in_water"):
                for radius_set in ((3, 4, 5), (2, 1)):
                    for ddx, ddz in dirs:
                        for r in radius_set:
                            for oy in (0, -1, -2, -3, -4):
                                cell = (bx + ddx * r, by + oy, bz + ddz * r)
                                name = self._block(cell)
                                if name in WATER_BLOCKS and self._block((cell[0], cell[1] + 1, cell[2])) in AIR_BLOCKS:
                                    _push_launch(cell)
                                    break
                        if launch_candidates:
                            break
                    if launch_candidates:
                        break
        if not launch_candidates:
            return {
                "ok": False,
                "error": "no_water",
                "op": "cross_water_with_boat",
                "message": "周围 5 格内没有可放船的水面。",
            }

        boat_id: int | None = None
        used_launch: list[int] | None = None
        last_error: str | None = None
        for _pass in range(2):
            if boat_id is not None:
                break
            for launch in launch_candidates[:4]:
                try:
                    used = self.bot.call("use_item", {"item": boat, "target": {"pos": launch}}, timeout_s=15.0)
                    ent = used.get("entity") if isinstance(used, dict) else None
                    if isinstance(ent, dict):
                        boat_id = int(ent.get("id") or 0) or None
                    if boat_id is not None:
                        used_launch = launch
                        break
                    last_error = "no_entity"
                except ApiError as err:
                    last_error = f"use_item:{err.code}"
                    if err.code == "busy":
                        self.release_stuck_action()
                        time.sleep(1.5)
                    continue
                except Exception as err:  # noqa: BLE001 - hung placement: /stop releases the action lock
                    last_error = f"use_item_timeout:{type(err).__name__}"
                    self.release_stuck_action()
                    time.sleep(1.5)
                    continue
        if boat_id is None:
            return {
                "ok": False,
                "error": last_error or "use_item_failed",
                "op": "cross_water_with_boat",
                "launch": launch_candidates[0],
                "attempted": launch_candidates[:4],
            }
        launch = used_launch
        if boat_id is None:
            try:
                ents = self.bot.call("get_entities", {"radius": 6, "types": ["other"]})
                cands = [
                    e
                    for e in (ents.get("entities") or [])
                    if isinstance(e, dict) and "boat" in str(e.get("name") or "")
                ]
                if cands:
                    cands.sort(key=lambda e: float(e.get("dist") or 99))
                    boat_id = int(cands[0]["id"])
            except ApiError:
                pass
        if boat_id is None:
            return {"ok": False, "error": "no_such_entity", "op": "cross_water_with_boat", "message": "船放下后没找到实体。"}

        try:
            self.bot.call("interact", {"entity_id": boat_id})
        except ApiError as err:
            return {
                "ok": False,
                "error": f"mount:{err.code}",
                "op": "cross_water_with_boat",
                "boat": boat_id,
                "message": err.message,
            }

        hops: list[dict[str, Any]] = []
        dismount_ok = False
        on_boat = False
        recovered = False
        try:
            remaining = math.hypot(float(x) - (bx + 0.5), float(z) - (bz + 0.5))
            seg_count = 0
            while remaining > shore_margin and seg_count < max_segments:
                st = self.bot.status()
                cp = st.get("pos") or [bx, by, bz]
                rem = math.hypot(float(x) - float(cp[0]), float(z) - float(cp[2]))
                if rem <= shore_margin:
                    break
                frac = min(1.0, segment_m / max(rem, 0.01))
                hx = float(cp[0]) + (float(x) - float(cp[0])) * frac
                hz = float(cp[2]) + (float(z) - float(cp[2])) * frac
                try:
                    self.bot.call("move_to", {"x": hx, "z": hz, "range": 1.0, "stop_on": ["death"]})
                    hops.append({"x": round(hx, 1), "z": round(hz, 1)})
                except ApiError as err:
                    if err.code in ("death", "hurt"):
                        raise
                    hops.append({"x": round(hx, 1), "z": round(hz, 1), "error": err.code})
                    break
                st2 = self.bot.status()
                cp2 = st2.get("pos") or cp
                if math.hypot(float(cp2[0]) - float(cp[0]), float(cp2[2]) - float(cp[2])) < 0.4:
                    break
                remaining = math.hypot(float(x) - float(cp2[0]), float(z) - float(cp2[2]))
                seg_count += 1

            # approach the shore <=1.2m, then dismount
            st = self.bot.status()
            cp = st.get("pos") or [bx, by, bz]
            shore = self._nearest_solid_shore(
                float(cp[0]), float(cp[2]), float(cp[1]), target=(float(x), float(z))
            )
            if shore is not None:
                try:
                    self.bot.call(
                        "move_to",
                        {"x": shore[0] + 0.5, "z": shore[2] + 0.5, "range": 0.9, "stop_on": ["death"]},
                    )
                except ApiError:
                    pass
            dismount_ok = False
            try:
                self.bot.call("dismount", {})
                dismount_ok = True
            except ApiError as err:
                if err.code != "not_in_vehicle":
                    raise
            on_boat = False
            st = self.bot.status()
            if not st.get("on_ground") and not st.get("in_water"):
                on_boat = True
            if (not st.get("on_ground")) or st.get("in_water"):
                # standing on the boat hull or already swimming: climb/swim ashore
                if shore is not None:
                    tx, tz = shore[0] + 0.5, shore[2] + 0.5
                else:
                    tx, tz = float(x), float(z)
                if shore is not None:
                    self._build_steps_to_shore(shore, cp)
                try:
                    self.bot.call("posture", {"jump": True})
                except ApiError:
                    pass
                for _ in range(10):
                    st = self.bot.status()
                    if not st.get("in_water") and (
                        st.get("on_ground") or float((st.get("pos") or [0, 0, 0])[1]) >= (shore[1] if shore else 0)
                    ):
                        break
                    self._face(tx, tz, float(cp[1]) + 1.0)
                    self._step_forward(ms=420, jump=True)
                st = self.bot.status()
                if (not st.get("on_ground") or st.get("in_water")) and shore is not None:
                    try:
                        self.bot.call(
                            "move_to",
                            {"x": shore[0] + 0.5, "z": shore[2] + 0.5, "range": 0.7, "stop_on": ["death"]},
                        )
                    except ApiError:
                        for _ in range(4):
                            self._face(shore[0] + 0.5, shore[2] + 0.5, float(shore[1]) + 1.0)
                            self._step_forward(ms=450, jump=True)
                            st_chk = self.bot.status()
                            if st_chk.get("on_ground") and not st_chk.get("in_water"):
                                break
                st = self.bot.status()
                if st.get("in_water") or not st.get("on_ground"):
                    self._get_ashore(float(x), float(z))
                st = self.bot.status()
                try:
                    self.bot.call("posture", {})
                except ApiError:
                    pass
                st = self.bot.status()
            recovered = False
            if boat_id is not None:
                try:
                    self.bot.call("attack", {"entity_id": boat_id, "max_hits": 3, "interval_ms": 300})
                    recovered = True
                except ApiError:
                    recovered = False
            # Attacking the boat auto-paths to it and can drag the bot back into the water:
            # do one final ashore pass after the recovery.
            st = self.bot.status()
            if st.get("in_water"):
                self._get_ashore(float(x), float(z))
            st = self.bot.status()
        finally:
            try:
                self.bot.call("posture", {})
            except ApiError:
                pass

        # final stabilization: the boat attack / a late wave can push us back into the shallows
        for _ in range(2):
            st = self.bot.status()
            if not st.get("in_water"):
                break
            self._get_ashore(float(x), float(z))
            time.sleep(0.4)
        st = self.bot.status()
        return {
            "ok": bool(dismount_ok) and not st.get("in_vehicle") and not st.get("in_water"),
            "op": "cross_water_with_boat",
            "boat": boat,
            "crafted": crafted,
            "boat_id": boat_id,
            "hops": hops,
            "dismounted": dismount_ok,
            "stood_on_boat_after_dismount": on_boat,
            "boat_recovered": recovered,
            "pos": st.get("pos"),
            "on_ground": st.get("on_ground"),
            "in_water": st.get("in_water"),
        }

    def release_stuck_action(self) -> bool:
        """Free a stuck action lock: /stop first, then a full disconnect/reconnect fallback."""
        for _ in range(2):
            try:
                self.bot.call("stop", {}, timeout_s=8.0)
                return True
            except ApiError as err:
                if err.code != "busy":
                    return False
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.8)
        try:
            self.bot.disconnect()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)
        try:
            self.bot.connect(respect_reconnect_interval=False)
            st = self.bot.wait_until_ready(timeout_s=45.0)
            return bool(st.get("ready"))
        except Exception:  # noqa: BLE001
            return False

    def _get_ashore(self, tx: float, tz: float) -> bool:
        """Deterministic multi-strategy shore exit: escape_water -> bank swim -> jump bursts."""
        st = self.bot.status()
        if not st.get("in_water"):
            return True
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]
        close = self._nearest_solid_shore(float(pos0[0]), float(pos0[2]), float(pos0[1]))
        if close is not None and math.dist(close, [float(v) for v in pos0[:3]]) <= 4.0:
            # a bank is right next to us: do NOT run the general escape_water (it may swim away)
            self._swim_to_bank(close, max_bursts=10)
            if not self.bot.status().get("in_water"):
                return True
        if self.skills is not None and hasattr(self.skills, "escape_water"):
            try:
                self.skills.escape_water()
            except ApiError:
                pass
            except Exception:  # noqa: BLE001
                pass
        st = self.bot.status()
        if not st.get("in_water"):
            return True
        shore = self._nearest_solid_shore(
            float((st.get("pos") or [0, 0, 0])[0]),
            float((st.get("pos") or [0, 0, 0])[2]),
            float((st.get("pos") or [0, 0, 0])[1]),
        ) or self._shore_along(tx, tz) or self._find_far_shore()
        if shore is not None:
            self._swim_to_bank(shore, max_bursts=20)
        st = self.bot.status()
        if not st.get("in_water"):
            return True
        try:
            self.bot.call("posture", {"jump": True})
        except ApiError:
            pass
        try:
            for _ in range(14):
                st = self.bot.status()
                if not st.get("in_water"):
                    return True
                pos = st.get("pos") or [0.0, 64.0, 0.0]
                near = self._nearest_solid_shore(float(pos[0]), float(pos[2]), float(pos[1]))
                tgt = near or shore or [int(tx), int(pos[1]), int(tz)]
                self._face(tgt[0] + 0.5, tgt[2] + 0.5, float(tgt[1]) + 1.0)
                self._step_forward(ms=650, jump=True)
        finally:
            try:
                self.bot.call("posture", {})
            except ApiError:
                pass
        return not self.bot.status().get("in_water")

    def _find_far_shore(self, *, max_r: int = 36) -> list[int] | None:
        """Coarse 8-direction scan for a standable bank when swimming in a wide water body."""
        st = self.bot.status()
        p = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(p[0])), math.floor(float(p[1])), math.floor(float(p[2]))
        dirs = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
        for r in range(4, max_r + 1, 4):
            for ax, az in dirs:
                x, z = bx + ax * r, bz + az * r
                for dy in (0, 1, 2):
                    feet = self._block((x, by + dy, z))
                    if feet not in AIR_BLOCKS and feet not in PASSABLE_PLANTS:
                        continue
                    below = self._block((x, by + dy - 1, z))
                    if below in AIR_BLOCKS or below in WATER_BLOCKS or below in LAVA_BLOCKS:
                        continue
                    return [x, by + dy, z]
        return None

    def _swim_to_bank(self, shore: list[int] | None, *, max_bursts: int = 14) -> bool:
        if not shore:
            return False
        try:
            self.bot.call("posture", {"jump": True})
        except ApiError:
            pass
        try:
            # pathfinder swims better than raw key presses for long distances
            try:
                self.bot.call(
                    "move_to",
                    {"x": shore[0] + 0.5, "z": shore[2] + 0.5, "range": 1.0, "stop_on": ["death"], "timeout": 12000},
                    timeout_s=20.0,
                )
            except ApiError:
                pass
            except Exception:  # noqa: BLE001
                pass
            for _ in range(max_bursts):
                st = self.bot.status()
                if not st.get("in_water"):
                    return True
                pos = st.get("pos") or [0.0, 64.0, 0.0]
                near = self._nearest_solid_shore(float(pos[0]), float(pos[2]), float(pos[1]))
                tgt = near or shore
                self._face(tgt[0] + 0.5, tgt[2] + 0.5, float(tgt[1]) + 1.0)
                self._step_forward(ms=600, jump=True)
        finally:
            try:
                self.bot.call("posture", {})
            except ApiError:
                pass
        st = self.bot.status()
        return not st.get("in_water")

    def _shore_along(self, tx: float, tz: float, *, max_r: int = 14) -> list[int] | None:
        """Standable cell along the straight line to (tx, tz) — used when the ring scan misses."""
        st = self.bot.status()
        p = st.get("pos") or [0.0, 64.0, 0.0]
        fx, fy, fz = float(p[0]), float(p[1]), float(p[2])
        dx, dz = tx - fx, tz - fz
        dist = math.hypot(dx, dz)
        if dist < 0.5:
            return None
        ux, uz = dx / dist, dz / dist
        for i in range(1, min(max_r, int(dist) + 3) + 1):
            x, z = int(math.floor(fx + ux * i)), int(math.floor(fz + uz * i))
            for y in (int(math.floor(fy)) + 1, int(math.floor(fy)), int(math.floor(fy)) + 2):
                feet = self._block((x, y, z))
                if feet not in AIR_BLOCKS and feet not in PASSABLE_PLANTS:
                    continue
                below = self._block((x, y - 1, z))
                if below in AIR_BLOCKS or below in WATER_BLOCKS or below in LAVA_BLOCKS:
                    continue
                return [x, y, z]
        return None

    def _water_surface_y(self, bx: int, by: int, bz: int) -> int | None:
        for y in range(by, by + 5):
            if self._block((bx, y, bz)) not in WATER_BLOCKS:
                return y - 1 if y > by else None
        return None

    def _build_steps_to_shore(self, shore: list[int], cp: list[float]) -> list[list[int]]:
        """If the bank is >=2 above the water surface, place a filler step in the water first."""
        bx, by, bz = math.floor(float(cp[0])), math.floor(float(cp[1])), math.floor(float(cp[2]))
        sdx = 1 if shore[0] > bx else -1 if shore[0] < bx else 0
        sdz = 1 if shore[2] > bz else -1 if shore[2] < bz else 0
        water_top = self._water_surface_y(bx, max(by, 1), bz)
        if water_top is None:
            return []
        stand_y = water_top + 1
        if shore[1] - stand_y < 2:
            return []
        filler = self._pick_filler()
        if not filler:
            return []
        placed: list[list[int]] = []
        for step in range(1, 3):
            cell = [bx + sdx * step, water_top, bz + sdz * step]
            if self._block(cell) not in WATER_BLOCKS:
                continue
            try:
                self.bot.call("place", {"item": filler, "pos": cell})
                placed.append(cell)
            except ApiError:
                break
            if step >= 1:
                break
        return placed

    def _nearest_solid_shore(
        self, cx: float, cz: float, cy: float, *, target: tuple[float, float] | None = None
    ) -> list[int] | None:
        """Find a standable bank cell (solid below, air at feet) near the water level."""
        bx, bz, by = math.floor(cx), math.floor(cz), math.floor(cy)
        best: tuple[float, list[int]] | None = None
        ring = ((0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1))
        for r in range(1, 6):
            for dx, dz in ring:
                x, z = bx + dx * r, bz + dz * r
                for dy in (0, 1, -1, 2, -2):
                    y = by + dy
                    feet = self._block((x, y, z))
                    if feet not in AIR_BLOCKS and feet not in PASSABLE_PLANTS:
                        continue
                    below = self._block((x, y - 1, z))
                    if below in AIR_BLOCKS or below in WATER_BLOCKS or below in LAVA_BLOCKS:
                        continue
                    d_bot = math.hypot(x + 0.5 - cx, z + 0.5 - cz)
                    score = d_bot
                    if target is not None:
                        score = math.hypot(x + 0.5 - target[0], z + 0.5 - target[1]) + 0.15 * d_bot
                    if best is None or score < best[0]:
                        best = (score, [x, y, z])
                    break
            if best is not None and r >= 2:
                break
        return best[1] if best else None

    # -- dispatcher ------------------------------------------------------
    def recover(
        self,
        *,
        x: float,
        z: float,
        y: float | None = None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        """Diagnose the 1-8 blocks ahead and fire exactly one local primitive."""
        st = self.bot.status()
        bx, by, bz = _floor_pos(st)
        probe = self._probe_around(st)
        cells = probe["cells"]
        dx, dz = _dominant_axis(float(x) - (bx + 0.5), float(z) - (bz + 0.5))
        ahead_water = sum(
            1 for i in range(1, 9) if self._block((bx + dx * i, by, bz + dz * i)) in WATER_BLOCKS
        )
        front_cells = [
            cells.get(f"{d}_feet", "")
            for d in ("north", "south", "east", "west")
        ] + [
            cells.get(f"{d}_head", "")
            for d in ("north", "south", "east", "west")
        ]
        if st.get("in_water") and ahead_water >= 6:
            res = self.cross_water_with_boat(x, z)
            return {**res, "diagnosis": "wide_water"}
        if any(is_trap_block(c) or is_door_block(c) or is_fence_block(c) for c in front_cells + [cells["feet"], cells["head"]]):
            res = self.clear_traps_and_doors(dx=dx, dz=dz)
            return {**res, "diagnosis": "traps_or_doors", "op": "clear_traps_and_doors"}
        if y is not None and abs(float(y) - by) >= 4:
            res = self.climb_or_descend_cliff(y, dx=dx, dz=dz)
            return {**res, "diagnosis": "cliff"}
        gap_start, gap_width = self._scan_ahead_gap(bx, by, bz, dx, dz)
        if gap_width >= 2 and gap_width <= 14:
            if gap_start > 1:
                # walk up to the lip first: path exists up to the edge
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": bx + dx * (gap_start - 1) + 0.5,
                            "z": bz + dz * (gap_start - 1) + 0.5,
                            "range": 0.8,
                            "stop_on": ["death"],
                        },
                    )
                except ApiError:
                    pass
            res = self.sneak_bridge(x, z)
            return {**res, "diagnosis": f"gap_{gap_width}_at_{gap_start}"}
        if by < 50 and self.trail.points(st.get("dimension")):
            res = self.trail.backtrack(steps=3, max_dist=24.0)
            return {**res, "diagnosis": "underground_dead_end", "op": "backtrack_breadcrumbs"}
        if any(is_trap_block(c) for c in [cells["feet"], cells["head"]]):
            res = self.clear_traps_and_doors(dx=dx, dz=dz)
            return {**res, "diagnosis": "standing_in_trap", "op": "clear_traps_and_doors"}
        # Last-resort generic recovery: dig through a solid wall ahead, otherwise unstick.
        ahead_feet = self._block((bx + dx, by, bz + dz))
        ahead_head = self._block((bx + dx, by + 1, bz + dz))
        if (
            ahead_feet not in AIR_BLOCKS
            and ahead_head not in AIR_BLOCKS
            and self.skills is not None
            and hasattr(self.skills, "dig_level_tunnel")
        ):
            direction = {
                (0, -1): "north",
                (0, 1): "south",
                (1, 0): "east",
                (-1, 0): "west",
            }.get((dx, dz), "north")
            try:
                res = self.skills.dig_level_tunnel(direction=direction, steps=2, stop_on_ore=True)
                return {
                    "ok": isinstance(res, dict) and bool(res.get("ok", True)),
                    "op": "dig_level_tunnel",
                    "diagnosis": "solid_wall_ahead",
                    "attempt": attempt,
                    "result": res if isinstance(res, dict) else str(res)[:300],
                }
            except ApiError as err:
                if err.code in ("hurt", "death", "interrupted"):
                    raise
        if self.skills is not None and hasattr(self.skills, "unstick"):
            try:
                res = self.skills.unstick()
                return {
                    "ok": isinstance(res, dict) and bool(res.get("ok")),
                    "op": "unstick",
                    "diagnosis": "unclassified",
                    "attempt": attempt,
                    "result": res if isinstance(res, dict) else str(res)[:300],
                }
            except ApiError as err:
                if err.code in ("hurt", "death", "interrupted"):
                    raise
        return {
            "ok": False,
            "error": "no_local_recovery",
            "diagnosis": "unclassified",
            "op": None,
            "attempt": attempt,
            "pos": [bx, by, bz],
            "cells": cells,
        }

    def _scan_ahead_gap(
        self, bx: int, by: int, bz: int, dx: int, dz: int, *, max_scan: int = 16
    ) -> tuple[int, int]:
        """Return (distance_to_first_gap, consecutive_gap_width) along the dominant axis."""
        start = 0
        width = 0
        for i in range(1, max_scan + 1):
            name = self._block((bx + dx * i, by - 1, bz + dz * i))
            if name in AIR_BLOCKS or name in WATER_BLOCKS or name in LAVA_BLOCKS:
                if start == 0:
                    start = i
                width += 1
            elif start:
                break
        return start, width


__all__ = [
    "BreadcrumbTrail",
    "TerrainNavigator",
    "is_trap_block",
    "is_door_block",
    "is_fence_block",
    "is_gravity_block",
    "is_waterlike",
    "MAX_RECOVERY_ATTEMPTS",
]
