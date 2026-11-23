"""Path-compressed MiningDFS state machine supporting A/B strategies ('fishbone' vs 'radar')."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import copy
import dataclasses
import inspect
import json
import math
import os
import pathlib
import re
import time
from typing import Any

import httpx

from ..errors import ApiError
from ..kb import assess_world_target, normalize_dimension
from .library import (
    BLOCK_MIN_PICKAXE_TIER,
    EXPECTED_BLOCK_DROPS,
    PICKAXE_TIERS,
    SHAFT_SEAL_BLOCKS,
    _best_pickaxe_in_inv,
    _compute_empty_slots,
    _expected_drops_for_blocks,
    _inventory_counts,
    _inventory_counts_from_raw,
    _inventory_snapshot_and_counts,
    _normalize_drop_name,
    _positive_inv_delta,
    _total_planks,
)

DIRECTIONS: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}

OPPOSITE_DIR: dict[str, str] = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
}

LEFT_RIGHT_DIRS: dict[str, tuple[str, str]] = {
    "north": ("west", "east"),
    "south": ("east", "west"),
    "east": ("north", "south"),
    "west": ("south", "north"),
}

AIR_BLOCKS = frozenset({"air", "cave_air", "void_air"})
FLUID_BLOCKS = frozenset({"water", "lava", "flowing_water", "flowing_lava"})
LAVA_BLOCKS = frozenset({"lava", "flowing_lava"})
FALLING_BLOCKS = frozenset({"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"})
UTILITY_BLOCKS = frozenset({"crafting_table", "furnace", "blast_furnace", "smoker"})
PASSABLE_BLOCKS = AIR_BLOCKS | frozenset(
    {
        "short_grass",
        "tall_grass",
        "fern",
        "large_fern",
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
        "sunflower",
        "lilac",
        "rose_bush",
        "peony",
        "pink_petals",
        "glow_lichen",
        "sculk_vein",
        "cave_vines",
        "cave_vines_plant",
        "hanging_roots",
        "leaf_litter",
        "wildflowers",
        "bush",
        "dead_bush",
        "torch",
        "wall_torch",
        "snow",
    }
)
FLIMSY_FLOOR_BLOCKS = frozenset(
    {
        "vine",
        "lily_pad",
        "scaffolding",
        "azalea",
        "flowering_azalea",
        "moss_carpet",
        "pale_moss_carpet",
    }
)
FLIMSY_FLOOR_SUFFIXES = ("_leaves", "_sapling", "_carpet", "_banner")
HAZARD_FLOOR_BLOCKS = frozenset(
    {"magma_block", "cactus", "campfire", "soul_campfire", "sweet_berry_bush", "powder_snow", "pointed_dripstone"}
)
ROCK_BLOCKS = frozenset(
    {
        "stone",
        "deepslate",
        "cobblestone",
        "cobbled_deepslate",
        "granite",
        "diorite",
        "andesite",
        "tuff",
        "calcite",
        "dirt",
        "coarse_dirt",
        "rooted_dirt",
        "grass_block",
        "sandstone",
        "red_sandstone",
    }
)
SURFACE_GROUND_BLOCKS: tuple[str, ...] = (
    "grass_block",
    "dirt",
    "coarse_dirt",
    "podzol",
    "rooted_dirt",
    "mud",
    "moss_block",
    "mycelium",
    "snow_block",
    "stone",
    "cobblestone",
    "andesite",
    "diorite",
    "granite",
    "tuff",
    "deepslate",
    "cobbled_deepslate",
    "sand",
    "red_sand",
    "sandstone",
    "red_sandstone",
    "gravel",
    "terracotta",
    "clay",
    "packed_ice",
    "netherrack",
    "blackstone",
    "basalt",
    "smooth_basalt",
    "soul_sand",
    "soul_soil",
    "crimson_nylium",
    "warped_nylium",
)
TRACKED_ORES: tuple[str, ...] = (
    "diamond_ore",
    "deepslate_diamond_ore",
    "iron_ore",
    "deepslate_iron_ore",
    "gold_ore",
    "deepslate_gold_ore",
    "redstone_ore",
    "deepslate_redstone_ore",
    "lapis_ore",
    "deepslate_lapis_ore",
    "emerald_ore",
    "deepslate_emerald_ore",
    "coal_ore",
    "deepslate_coal_ore",
    "ancient_debris",
    "nether_gold_ore",
    "nether_quartz_ore",
)

ORE_DFS_PROFILES: dict[str, dict[str, Any]] = {
    "ancient_debris": {
        "target_blocks": ("ancient_debris",),
        "expected_items": ("ancient_debris",),
        "default_target_y": 15,
        "min_tier": 4,
    },
    "quartz": {
        "target_blocks": ("nether_quartz_ore",),
        "expected_items": ("quartz", "nether_quartz_ore"),
        "default_target_y": 48,
        "min_tier": 1,
    },
    "nether_gold_ore": {
        "target_blocks": ("nether_gold_ore",),
        "expected_items": ("gold_nugget", "nether_gold_ore"),
        "default_target_y": 64,
        "min_tier": 1,
    },
    "iron": {
        "target_blocks": ("iron_ore", "deepslate_iron_ore"),
        "expected_items": ("raw_iron", "iron_ore", "deepslate_iron_ore"),
        "default_target_y": 16,
        "min_tier": 2,
    },
    "coal": {
        "target_blocks": ("coal_ore", "deepslate_coal_ore"),
        "expected_items": ("coal", "coal_ore", "deepslate_coal_ore"),
        "default_target_y": 44,
        "min_tier": 1,
    },
    "copper": {
        "target_blocks": ("copper_ore", "deepslate_copper_ore"),
        "expected_items": ("raw_copper", "copper_ore", "deepslate_copper_ore"),
        "default_target_y": 44,
        "min_tier": 2,
    },
    "lapis": {
        "target_blocks": ("lapis_ore", "deepslate_lapis_ore"),
        "expected_items": ("lapis_lazuli", "lapis_ore", "deepslate_lapis_ore"),
        "default_target_y": 0,
        "min_tier": 2,
    },
    "gold": {
        "target_blocks": ("gold_ore", "deepslate_gold_ore"),
        "expected_items": ("raw_gold", "gold_ore", "deepslate_gold_ore"),
        "default_target_y": -16,
        "min_tier": 3,
    },
    "redstone": {
        "target_blocks": ("redstone_ore", "deepslate_redstone_ore"),
        "expected_items": ("redstone", "redstone_ore", "deepslate_redstone_ore"),
        "default_target_y": -54,
        "min_tier": 3,
    },
    "diamond": {
        "target_blocks": ("diamond_ore", "deepslate_diamond_ore"),
        "expected_items": ("diamond", "diamond_ore", "deepslate_diamond_ore"),
        "default_target_y": -54,
        "min_tier": 3,
    },
    "emerald": {
        "target_blocks": ("emerald_ore", "deepslate_emerald_ore"),
        "expected_items": ("emerald", "emerald_ore", "deepslate_emerald_ore"),
        "default_target_y": assess_world_target("emerald_ore")["recommended_y"],
        "min_tier": 3,
    },
}

DEFAULT_MAX_DURABILITY: dict[str, int] = {
    "wooden_pickaxe": 59,
    "golden_pickaxe": 32,
    "stone_pickaxe": 131,
    "iron_pickaxe": 250,
    "diamond_pickaxe": 1561,
    "netherite_pickaxe": 2031,
}

RADAR_SCAN_RADIUS = 64
VEIN_HOOK_RADAR_RADIUS = 12
VEIN_HOOK_FISHBONE_RADIUS = 4
# fishbone_scan: fishbone skeleton + read-only widened ore reach ONLY within the target band
VEIN_HOOK_SCAN_RADIUS = 10
DEPTH_RESET_THRESHOLD = 8
SCAN_BAND = 3  # |anchor.y - target_y| <= SCAN_BAND counts as "in target band"
LAYER_SWITCH_MIN_DELTA = 12
CLIMB_HEIGHT_TOLERANCE = 2
OFFSET_RETRY_STEPS = 3
NODE_REJOIN_DISTANCE = 16.0
# fishbone skeleton never emits vertical branches; when the caller raises target depth
# mid-shaft we still must descend, otherwise the spine tunnels sideways on the stale layer.
SPINE_DESCEND_MIN_DELTA = 4
# Craft a burner stone pickaxe below this total durability so the iron pick stays for ore.
RECRAFT_PICK_DURABILITY = 100
# Main DFS loop is deadline-bounded; bail out after this many iterations with zero progress.
MAX_IDLE_ITERATIONS = 4
# Also bail when a whole stretch of wall clock passes with zero progress (slow APIs).
MAX_IDLE_SECONDS = 20.0
# Keep a stalled first dig cooling through a second failed turn and dispatch between calls.
DIG_TIMEOUT_COOLDOWN_S = 45.0
DIG_RECOVERY_RESERVE_S = 12.0
WATER_SHAFT_COOLDOWN_S = 60.0
# Three consecutive fluid/hazard aborts on one shaft means an aquifer/cave field: relocate.
FLUID_ABORT_LIMIT = 3
# Three consecutive "ok but nothing happened" calls mean the shaft is a dead end.
IDLE_CALLS_BEFORE_RELOCATE = 3
# A shaft that yields nothing for this many consecutive calls is a dead run: abandon it loudly.
# Threshold evidence (overnight runs): n0424 had ~20 dry calls in a row and then produced 3
# diamonds, so 20 was too aggressive; n0344 burned 45 dry calls for 1 hit. 30 separates them,
# and this remains a coarse heuristic — treat it as a budget guard, not a geological verdict.
DRY_CALLS_BEFORE_ABANDON = 30
# A broken ore drops inside its own cell, so we must be standing within pickup range of it.
# The MC API does NOT enforce dig reach (only 1 "out_of_reach" in thousands of live digs), so
# nothing else stops us from shattering an ore behind a wall and losing the drop forever.
DROP_PICKUP_RADIUS_MANHATTAN = 2
FLUID_ABORT_REASONS = frozenset({
    # NOTE: "cave_breach_sealed" is deliberately NOT here. Sealing a breach is a *success* — the
    # engine handled it. Counting it made cave-riddled terrain kill a healthy shaft every ~3
    # breaches (live s11: 1/37 hits, 5 resets at y≈54, never reached the target layer).
    "fluid_or_bedrock",
    "unsafe_floor_or_cave",
    "falling_material_adjacent",
    # Drowning is the same hazard from the player's side (live s12: 3 escapes, still 0 counted,
    # so the shaft was never marked as a water trap and the bot kept digging back into the pool).
    "in_water",
})
EXHAUSTED_COLLAR_MIN_SEP = 14.0
UNREACHABLE_ORE_COOLDOWN_S = 300.0


class _MiningDeadline(Exception):
    """Unwind nested mining work without being swallowed as a recoverable ApiError."""


class _CollarResupplyDeadline(_MiningDeadline):
    """A mining slice expired while following the route back for a replacement pickaxe."""


class _MiningLifeChanged(Exception):
    """End a slice when a status read confirms death, respawn or a dimension change."""


class _BudgetSession:
    """Private session copy: nested skills share a budget without changing the live bot.

    Binding session methods to the copy bounds observations as well as actions.
    No worker thread survives a timeout and continues issuing actions.
    """

    _RETRYABLE_PROBES = frozenset({"status", "inventory", "get_block", "get_entities", "find_blocks"})

    def __init__(self, bot: Any, deadline: float, *, dig_timeout_s: float = 8.0) -> None:
        self.deadline = deadline
        self.dig_timeout_s = float(dig_timeout_s)
        self.pickup_budget_remaining = 6.0
        self.pickup_stop_deadline: float | None = None
        self.confirmed_ascent_levels: set[int] = set()
        self.first_pos: list[float] | None = None
        self.last_status: dict[str, Any] = {}
        self._observed_dimension: str | None = None
        self._observed_death_time: Any = None
        self.first_inventory: dict[str, Any] | None = None
        self.last_inventory: dict[str, Any] | None = None
        self.timeout_path: str | None = None
        self.timeout_dig_pos: tuple[int, int, int] | None = None
        self.timeout_dig_read_s = 0.0
        self.dug_count = 0
        self.confirmed_stone_pickaxe_crafts = 0
        self.ore_breaks: list[dict[str, Any]] = []
        self.positions: list[tuple[int, int, int]] = []
        self.climb_routes: list[tuple[int, list[tuple[int, int, int]]]] = []
        self.session = copy.copy(bot)
        self._call = self.session.call
        self.session.call = self.call
        self.session._mining_deadline = deadline
        # A recovery reconnect owns a separate, much longer lifecycle budget. Leave that
        # to the caller, rather than letting BotSession.call start it inside mining.
        self.session._recovering_quarantine = True

    def release_timed_out_action(self) -> bool:
        try:
            stopped = self._call("stop", {}, timeout_s=1.0, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            return False
        return stopped.get("released") is True or (
            stopped.get("stopped") is True and "action" in stopped and stopped["action"] is None
        )

    def _observe_status(self, status: dict[str, Any]) -> None:
        had_status = bool(self.last_status)
        self.last_status = status
        raw_dimension = status.get("dimension")
        dimension = normalize_dimension(raw_dimension) if raw_dimension else None
        death = status.get("last_death")
        death_time = death.get("t") if isinstance(death, dict) else None
        if had_status and (
            status.get("alive") is False
            or (death_time is not None and death_time != self._observed_death_time)
            or (dimension is not None and self._observed_dimension is not None and dimension != self._observed_dimension)
        ):
            raise _MiningLifeChanged
        if dimension is not None:
            self._observed_dimension = dimension
        if death_time is not None:
            self._observed_death_time = death_time
        pos = status.get("pos")
        if self.first_pos is None and pos:
            self.first_pos = list(pos)
        if pos and status.get("on_ground", True):
            cell = _floor_pos(pos)
            if not self.positions or cell != self.positions[-1]:
                self.positions.append(cell)

    def call(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        endpoint = path.lstrip("/")
        default_limit = self.dig_timeout_s if endpoint == "dig" else 45.0
        timeout_limit = float(kwargs.get("timeout_s") or default_limit)
        first_step_probe = endpoint in self._RETRYABLE_PROBES and not self.dug_count and len(self.positions) < 2
        kwargs["allow_reauth"] = False
        for attempt in range(2):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0.2:
                raise _MiningDeadline
            # BotSession itself can retry a closed keepalive after a 150ms pause.
            attempt_budget = (remaining - 0.15) / 2
            limit = min(timeout_limit, 5.0) if first_step_probe and attempt == 0 else timeout_limit
            phase_timeout = min(0.5, limit, attempt_budget / 4)
            read_timeout = min(limit, attempt_budget - 3 * phase_timeout)
            payload = dict(body) if body is not None else None
            if payload is not None and endpoint in {"move_to", "control"}:
                # Let the server return its own deadline before the client times out.
                action_limit_ms = int((read_timeout - 0.25) * 1000)
                if endpoint == "control":
                    if action_limit_ms < 50:
                        raise _MiningDeadline
                    duration = payload.get("ms")
                    payload["ms"] = min(300 if duration is None else int(duration), action_limit_ms)
                else:
                    if action_limit_ms < 1:
                        raise _MiningDeadline
                    duration = payload.get("timeout")
                    payload["timeout"] = min(action_limit_ms if duration is None else int(duration), action_limit_ms)
            kwargs["timeout_s"] = httpx.Timeout(read_timeout, pool=phase_timeout, connect=phase_timeout, write=phase_timeout)
            try:
                result = self._call(path, payload, **kwargs)
                break
            except (httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                timed_out = isinstance(exc, httpx.TimeoutException)
                if timed_out and first_step_probe and attempt == 0 and self.deadline - time.monotonic() > 1.0:
                    continue
                dig_pos = (payload or {}).get("pos")
                if (
                    timed_out and endpoint == "dig"
                    and self.dug_count > 0
                    and self.deadline - time.monotonic() > DIG_RECOVERY_RESERVE_S
                    and isinstance(dig_pos, (list, tuple))
                    and len(dig_pos) >= 3
                    and self.release_timed_out_action()
                ):
                    raise ApiError(
                        "dig_timeout", f"dig stalled at {_floor_pos(dig_pos)}", http_status=408,
                    ) from exc
                self.timeout_path = endpoint
                if endpoint == "dig" and isinstance(dig_pos, (list, tuple)) and len(dig_pos) >= 3:
                    self.timeout_dig_pos = _floor_pos(dig_pos)
                    self.timeout_dig_read_s = read_timeout if isinstance(exc, httpx.ReadTimeout) else 0.0
                # The server may have accepted a mutation even when no reply arrived.
                raise _MiningDeadline from exc
        if endpoint == "dig":
            dug = result.get("dug")
            if dug:
                self.dug_count += 1
            if isinstance(dug, str):
                block = dug.removeprefix("minecraft:")
                pos = (payload or {}).get("pos")
                if block in TRACKED_ORES and isinstance(pos, (list, tuple)) and len(pos) == 3:
                    self.ore_breaks.append({"pos": list(pos), "block": block})
        if endpoint == "craft" and result.get("crafted") in {"stone_pickaxe", "minecraft:stone_pickaxe"}:
            quantity = result.get("gained")
            if type(quantity) is int and quantity > 0:
                self.confirmed_stone_pickaxe_crafts += quantity
        if endpoint == "inventory" and isinstance(result.get("items"), list):
            if self.first_inventory is None:
                self.first_inventory = result
            self.last_inventory = result
        if endpoint == "status":
            self._observe_status(result)
        return result


def normalize_dfs_strategy(raw: str | None = None) -> str:
    """Resolve DFS strategy (default 'radar'; also supports 'fishbone_scan', 'fishbone', 'v5')."""
    val = str(raw if raw is not None else os.environ.get("MC_DFS_STRATEGY", "radar")).strip().lower()
    if val in {"fishbone", "conservative", "classic", "strip"}:
        return "fishbone"
    if val in {"fishbone_scan", "fishbone_plus", "fbscan", "scan"}:
        return "fishbone_scan"
    if val in {"v5", "v4_fixed", "patched_v4", "moderate"}:
        return "v5"
    if val in {"radar", "aggressive", "guided", "surgical", ""}:
        return "radar"
    return "radar"


@dataclasses.dataclass
class BranchSpec:
    kind: str  # "stair_down" | "stair_up" | "spine_forward" | "branch_leaf" | "offset_retry"
    direction: str
    steps: int
    status: str = "pending"  # "pending" | "active" | "completed" | "pruned"
    prune_reason: str | None = None
    completed_waypoints: list[tuple[int, int, int]] = dataclasses.field(default_factory=list)
    offset_tried: bool = False
    sidestep_dir: str | None = None
    resume_kind: str | None = None
    branch_target_y: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "direction": self.direction,
            "steps": self.steps,
            "status": self.status,
            "prune_reason": self.prune_reason,
            "completed_waypoints": [list(p) for p in self.completed_waypoints],
            "offset_tried": self.offset_tried,
            "sidestep_dir": self.sidestep_dir,
            "resume_kind": self.resume_kind,
            "branch_target_y": self.branch_target_y,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BranchSpec:
        bty = raw.get("branch_target_y")
        return cls(
            kind=str(raw["kind"]),
            direction=str(raw["direction"]),
            steps=int(raw["steps"]),
            status=str(raw.get("status", "pending")),
            prune_reason=raw.get("prune_reason"),
            completed_waypoints=[(int(p[0]), int(p[1]), int(p[2])) for p in (raw.get("completed_waypoints") or [])],
            offset_tried=bool(raw.get("offset_tried", False)),
            sidestep_dir=raw.get("sidestep_dir"),
            resume_kind=raw.get("resume_kind"),
            branch_target_y=(int(bty) if bty is not None else None),
        )


@dataclasses.dataclass
class ShaftEdge:
    edge_kind: str  # "stair_down" | "stair_up" | "spine_forward" | "branch_leaf" | "offset_sidestep"
    direction: str
    waypoints: list[tuple[int, int, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_kind": self.edge_kind,
            "direction": self.direction,
            "waypoints": [list(p) for p in self.waypoints],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShaftEdge:
        return cls(
            edge_kind=str(raw["edge_kind"]),
            direction=str(raw["direction"]),
            waypoints=[(int(p[0]), int(p[1]), int(p[2])) for p in (raw.get("waypoints") or [])],
        )


@dataclasses.dataclass
class ShaftNode:
    node_id: int
    kind: str  # "collar" | "switchback" | "spine"
    anchor: tuple[int, int, int]
    parent_id: int | None
    edge_from_parent: ShaftEdge | None
    branches: list[BranchSpec]
    torch_placed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "anchor": list(self.anchor),
            "parent_id": self.parent_id,
            "edge_from_parent": self.edge_from_parent.to_dict() if self.edge_from_parent else None,
            "branches": [b.to_dict() for b in self.branches],
            "torch_placed": self.torch_placed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShaftNode:
        anc = raw["anchor"]
        efp = raw.get("edge_from_parent")
        return cls(
            node_id=int(raw["node_id"]),
            kind=str(raw["kind"]),
            anchor=(int(anc[0]), int(anc[1]), int(anc[2])),
            parent_id=int(raw["parent_id"]) if raw.get("parent_id") is not None else None,
            edge_from_parent=ShaftEdge.from_dict(efp) if isinstance(efp, dict) else None,
            branches=[BranchSpec.from_dict(b) for b in (raw.get("branches") or [])],
            torch_placed=bool(raw.get("torch_placed", False)),
        )


@dataclasses.dataclass(frozen=True)
class _WaterInterruptedShaft:
    collar: tuple[int, int, int]
    water_cell: tuple[int, int, int]
    observed_life: tuple[Any, Any]
    until: float


@dataclasses.dataclass
class DeferredOreRecord:
    pos: tuple[int, int, int]
    block_name: str
    required_tier: int
    nearest_node_id: int


class WorldLedger:
    """Spatial memory of excavated corridors, safe shafts, discovered ores, and utility stations."""

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        self.path: pathlib.Path | None = pathlib.Path(path) if path is not None else None
        self.excavated_positions: set[tuple[int, int, int]] = set()
        self.known_corridor_positions: set[tuple[int, int, int]] = set()
        self.exhausted_collars: list[tuple[int, int, int]] = []
        self.discovered_ores: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.utility_blocks: dict[str, list[tuple[int, int, int]]] = {
            "crafting_table": [],
            "furnace": [],
        }
        self.saved_shafts: list[dict[str, Any]] = []
        if self.path is not None:
            self.load()

    def record_excavated(self, pt: tuple[int, int, int]) -> None:
        self.excavated_positions.add((int(pt[0]), int(pt[1]), int(pt[2])))
        self.discovered_ores.pop((int(pt[0]), int(pt[1]), int(pt[2])), None)

    def record_corridor(self, pt: tuple[int, int, int]) -> None:
        bx, by, bz = int(pt[0]), int(pt[1]), int(pt[2])
        self.known_corridor_positions.add((bx, by, bz))
        self.known_corridor_positions.add((bx, by + 1, bz))

    def record_exhausted_collar(self, anchor: tuple[int, int, int]) -> None:
        pt = (int(anchor[0]), int(anchor[1]), int(anchor[2]))
        if pt not in self.exhausted_collars:
            self.exhausted_collars.append(pt)
        # Keep the saved shaft: its waypoints are the walkable route home. It is only "retired"
        # for mining, not for travel (live s12: losing the route forced a 120-block re-dig).
        for sh in self.saved_shafts:
            if tuple(sh.get("collar_anchor") or ()) == pt:
                sh["retired"] = True

    def record_ore(self, pt: tuple[int, int, int], block_name: str, required_tier: int) -> None:
        pos = (int(pt[0]), int(pt[1]), int(pt[2]))
        if pos in self.excavated_positions:
            return
        self.discovered_ores[pos] = {
            "pos": list(pos),
            "block": str(block_name),
            "required_tier": int(required_tier),
        }

    def remove_ore(self, pt: tuple[int, int, int]) -> None:
        self.discovered_ores.pop((int(pt[0]), int(pt[1]), int(pt[2])), None)

    def record_utility(self, block_name: str, pos: tuple[int, int, int] | list[int]) -> None:
        key = str(block_name).removeprefix("minecraft:")
        if len(pos) < 3:
            return
        pt = (int(pos[0]), int(pos[1]), int(pos[2]))
        bucket = self.utility_blocks.setdefault(key, [])
        if pt not in bucket:
            bucket.append(pt)
            if len(bucket) > 24:
                del bucket[:-24]

    def nearest_utility(
        self,
        block_name: str,
        origin: tuple[int, int, int],
        *,
        max_dist: float = 48.0,
    ) -> tuple[int, int, int] | None:
        key = str(block_name).removeprefix("minecraft:")
        cands = self.utility_blocks.get(key) or []
        best: tuple[int, int, int] | None = None
        best_d = max_dist
        for pt in cands:
            d = math.dist(origin, pt)
            if d <= best_d:
                best_d = d
                best = pt
        return best

    def upsert_shaft(self, shaft_data: dict[str, Any]) -> None:
        anc = tuple(int(v) for v in shaft_data["collar_anchor"])
        if anc in self.exhausted_collars and not shaft_data.get("retired"):
            return
        filtered = [
            s for s in self.saved_shafts if tuple(s.get("collar_anchor") or ()) != anc
        ]
        entry = dict(shaft_data)
        entry["collar_anchor"] = list(anc)
        entry["from_disk"] = False
        filtered.append(entry)
        self.saved_shafts = filtered[-8:]

    def load(self) -> bool:
        if self.path is None or not self.path.exists():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict):
            return False
        for p in raw.get("excavated_positions") or []:
            if isinstance(p, list) and len(p) >= 3:
                self.excavated_positions.add((int(p[0]), int(p[1]), int(p[2])))
        for p in raw.get("known_corridor_positions") or []:
            if isinstance(p, list) and len(p) >= 3:
                self.known_corridor_positions.add((int(p[0]), int(p[1]), int(p[2])))
        for p in raw.get("exhausted_collars") or []:
            if isinstance(p, list) and len(p) >= 3:
                pt = (int(p[0]), int(p[1]), int(p[2]))
                if pt not in self.exhausted_collars:
                    self.exhausted_collars.append(pt)
        for item in raw.get("discovered_ores") or []:
            if isinstance(item, dict) and isinstance(item.get("pos"), list) and len(item["pos"]) >= 3:
                pos = (int(item["pos"][0]), int(item["pos"][1]), int(item["pos"][2]))
                self.discovered_ores[pos] = {
                    "pos": list(pos),
                    "block": str(item.get("block") or ""),
                    "required_tier": int(item.get("required_tier", 2)),
                    "from_disk": True,
                }
        utils = raw.get("utility_blocks")
        if isinstance(utils, dict):
            for k, arr in utils.items():
                if isinstance(arr, list):
                    self.utility_blocks[str(k)] = [
                        (int(p[0]), int(p[1]), int(p[2]))
                        for p in arr
                        if isinstance(p, list) and len(p) >= 3
                    ]
        for sh in raw.get("saved_shafts") or []:
            if isinstance(sh, dict) and isinstance(sh.get("collar_anchor"), list) and len(sh["collar_anchor"]) >= 3:
                sh_copy = dict(sh)
                sh_copy["from_disk"] = True
                self.saved_shafts.append(sh_copy)
        return True

    def save(self) -> bool:
        if self.path is None:
            return False
        payload = {
            "version": 1,
            "updated_at": round(time.time(), 2),
            "excavated_positions": [list(p) for p in list(self.excavated_positions)[-4000:]],
            "known_corridor_positions": [list(p) for p in list(self.known_corridor_positions)[-4000:]],
            "exhausted_collars": [list(p) for p in self.exhausted_collars[-32:]],
            "discovered_ores": list(self.discovered_ores.values())[-200:],
            "utility_blocks": {
                k: [list(p) for p in v[-24:]] for k, v in self.utility_blocks.items()
            },
            "saved_shafts": self.saved_shafts[-8:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
            return True
        except OSError:
            return False


def _default_ledger_path_for_bot(bot: Any) -> pathlib.Path | None:
    client = getattr(bot, "_client", None)
    transport = getattr(client, "_transport", None)
    if transport is not None and type(transport).__name__ == "MockTransport":
        return None
    raw_user = str(getattr(bot, "bot_username", None) or "default")
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_user).strip("_") or "default"
    return pathlib.Path(".state") / f"world_ledger_{safe_user}.json"


def _should_focus_target_only(
    *,
    strategy: str,
    min_target_tier: int,
    max_tier: int,
    anchor_y: int,
    target_y: int,
) -> bool:
    """Whether the vein hook may ignore non-target ores.

    Only safe on the target layer itself (or for the radar family which steers by ore
    positions). While transiting down to a deep layer the skeleton must keep harvesting
    adjacent iron/coal: a worn pickaxe otherwise strands the shaft with no way to recraft.
    """
    if min_target_tier < 3 or max_tier < min_target_tier:
        return False
    if strategy in {"radar", "v5"}:
        return True
    return strategy == "fishbone_scan" and abs(anchor_y - target_y) <= SCAN_BAND


def _resolve_target_profile(
    target: str, target_y: int | None, *, dimension: str | None = None,
) -> dict[str, Any]:
    raw = str(target or "iron").strip().lower().removeprefix("minecraft:")
    aliases = {
        "raw_iron": "iron",
        "iron_ingot": "iron",
        "iron_ore": "iron",
        "deepslate_iron_ore": "iron",
        "diamond_ore": "diamond",
        "deepslate_diamond_ore": "diamond",
        "coal_ore": "coal",
        "deepslate_coal_ore": "coal",
        "copper_ore": "copper",
        "deepslate_copper_ore": "copper",
        "raw_copper": "copper",
        "gold_ore": "gold",
        "deepslate_gold_ore": "gold",
        "raw_gold": "gold",
        "gold_nugget": "nether_gold_ore",
        "nether_quartz_ore": "quartz",
        "lapis_lazuli": "lapis",
        "lapis_ore": "lapis",
        "deepslate_lapis_ore": "lapis",
        "redstone_ore": "redstone",
        "deepslate_redstone_ore": "redstone",
        "emerald_ore": "emerald",
        "deepslate_emerald_ore": "emerald",
    }
    key = aliases.get(raw, raw)
    if raw == "gold" and dimension == "the_nether":
        key = "nether_gold_ore"
    prof = ORE_DFS_PROFILES.get(key)
    if prof is not None:
        resolved_y = int(target_y) if target_y is not None else int(prof["default_target_y"])
        return {
            "profile_key": key,
            "target_blocks": tuple(prof["target_blocks"]),
            "expected_items": tuple(prof["expected_items"]),
            "target_y": resolved_y,
            "min_tier": int(prof["min_tier"]),
        }
    req_tier = max(1, int(BLOCK_MIN_PICKAXE_TIER.get(raw, 2)))
    expected = tuple(sorted(_expected_drops_for_blocks([raw])))
    resolved_y = int(target_y) if target_y is not None else (-54 if req_tier >= 3 else 16)
    return {
        "profile_key": raw,
        "target_blocks": (raw,),
        "expected_items": expected or (raw,),
        "target_y": resolved_y,
        "min_tier": req_tier,
    }


def _extract_item_remaining_durability(item: dict[str, Any]) -> int:
    name = str(item.get("name") or "").removeprefix("minecraft:")
    max_d = int(DEFAULT_MAX_DURABILITY.get(name, 100))
    dur = item.get("durability")
    if isinstance(dur, (list, tuple)) and len(dur) >= 1:
        try:
            return max(0, int(dur[0]))
        except (TypeError, ValueError):
            pass
    rem = item.get("durability_remaining")
    if isinstance(rem, (int, float)):
        return max(0, int(rem))
    used = item.get("durability_used", item.get("damage"))
    item_max = int(item.get("max_durability") or max_d)
    if isinstance(used, (int, float)) and item_max > 0:
        return max(0, item_max - int(used))
    if isinstance(dur, (int, float)) and item_max > 0:
        if 0.0 <= float(dur) <= 1.0 and item_max > 1:
            return max(0, int(round(float(dur) * item_max)))
        return max(0, int(dur))
    return max_d


def _pickaxe_inventory_summary(inv: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Return (usable_pickaxes, max_tier, total_remaining_durability)."""
    picks: list[dict[str, Any]] = []
    seen_slots: set[int] = set()
    rows: list[tuple[dict[str, Any], bool]] = [
        (row, False) for row in (inv.get("items") or []) if isinstance(row, dict)
    ]
    held = inv.get("held")
    if isinstance(held, dict) and held.get("name"):
        rows.append((held, False))
    offhand = inv.get("offhand")
    if isinstance(offhand, dict) and offhand.get("name"):
        rows.append((offhand, True))
    for row, from_offhand in rows:
        name = str(row.get("name") or "").removeprefix("minecraft:")
        if name not in PICKAXE_TIERS:
            continue
        raw_slot = row.get("slot")
        slot = 45 if (from_offhand and raw_slot is None) else raw_slot
        if isinstance(slot, int):
            if slot in seen_slots:
                continue
            seen_slots.add(slot)
        rem = _extract_item_remaining_durability(row)
        if rem <= 0:
            continue
        picks.append({
            "name": name,
            "tier": int(PICKAXE_TIERS[name]),
            "remaining": rem,
            "slot": slot,
            "in_offhand": bool(from_offhand or slot == 45),
        })
    max_tier = max((int(p["tier"]) for p in picks), default=0)
    total_rem = sum(int(p["remaining"]) for p in picks)
    return picks, max_tier, total_rem


def _choose_pickaxe_for_tier(
    picks: list[dict[str, Any]], required_tier: int, *, prefer_stone: bool = True,
) -> dict[str, Any] | None:
    """Choose the lowest usable tier, with stone preferred for ordinary rock."""
    eligible = [p for p in picks if int(p["tier"]) >= max(1, int(required_tier)) and int(p["remaining"]) >= 1]
    if not eligible:
        return None
    eligible.sort(key=lambda p: (
        0 if prefer_stone and p["tier"] == 2 else int(p["tier"]),
        bool(p.get("in_offhand")),
        int(p["remaining"]),
    ))
    return eligible[0]


def _seal_reserve(counts: dict[str, int]) -> int:
    return sum(int(counts.get(name, 0)) for name in SHAFT_SEAL_BLOCKS)


def _pick_seal_material(counts: dict[str, int]) -> str | None:
    for cand in SHAFT_SEAL_BLOCKS:
        if counts.get(cand, 0) >= 1:
            return cand
    return None


def _stone_pickaxe_headroom(counts: dict[str, int]) -> tuple[int, int, int]:
    """Count recipe ingredients independently of crafting-table availability.

    Returns (pickaxes, stone_blocks, sticks_available).
    """
    stone = int(counts.get("cobblestone", 0)) + int(counts.get("cobbled_deepslate", 0))
    sticks = int(counts.get("stick", 0))
    planks = _total_planks(counts)
    logs = sum(int(v) for k, v in counts.items() if k.endswith(("_log", "_stem")))
    sticks_available = sticks + 4 * (planks // 2) + 8 * logs
    return min(stone // 3, sticks_available // 2), stone, sticks_available


def _pickaxe_low_hint(counts: dict[str, int]) -> str:
    """Actionable recraft hint so the model fixes a worn pickaxe in one round."""
    n, stone, sticks = _stone_pickaxe_headroom(counts)
    if n > 0:
        return (
            f"石料和木棍材料够做 {n} 把石镐（每把 3 石料、2 木棍，当前有 {stone} 石料、木棍折算 {sticks}）。"
            "建议先 mc_skill_craft_gear(item='stone_pickaxe') 再继续，石镐当耗材、铁镐留着挖矿。"
        )
    if stone >= 3:
        return (
            f"石料够（{stone}），木棍不足（折算 {sticks}）。先到能采木的安全位置，"
            "用 mc_skill_harvest_logs(target_logs=1) 补原木或菌柄，再用 mc_skill_craft_gear(item='stone_pickaxe')。"
        )
    if not any(counts.get(name, 0) > 0 for name in PICKAXE_TIERS):
        return (
            "石料不足 3，背包里也没有镐。先用 mc_skill_craft_gear(item='wooden_pickaxe', equip_slot='hand') "
            "制作并装备木镐。缺木料时先在安全位置补原木或菌柄。再用木镐采石，补齐 3 块石料后制作石镐。"
        )
    return "石料不足 3：先挖几块石头/深板岩，或用 mc_skill_craft_gear(item='stone_pickaxe') 让它自动备料。"


def _has_collar_resupply_materials(counts: dict[str, int]) -> bool:
    _, stone, sticks = _stone_pickaxe_headroom(counts)
    has_head_mat = stone >= 3 or counts.get("iron_ingot", 0) >= 3 or counts.get("diamond", 0) >= 3
    has_smeltable = counts.get("raw_iron", 0) >= 1
    return (sticks >= 2 and has_head_mat) or has_smeltable


def _can_stack_expected_drop(inv_raw: dict[str, Any], expected_drops: set[str] | frozenset[str]) -> bool:
    for row in inv_raw.get("items") or []:
        if not isinstance(row, dict):
            continue
        nm = str(row.get("name") or "").removeprefix("minecraft:")
        cnt = int(row.get("count") or 0)
        if nm in expected_drops and 0 < cnt < 64:
            return True
    return False


def _is_two_step_stair(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return abs(a[1] - b[1]) == 2 and (abs(a[0] - b[0]), abs(a[2] - b[2])) in {(2, 0), (0, 2)}


def _is_two_block_vertical_gap(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return abs(a[1] - b[1]) == 2 and abs(a[0] - b[0]) + abs(a[2] - b[2]) <= 1


def _descent_start_beside_anchor(
    start: tuple[int, int, int], anchor: tuple[int, int, int], direction: str,
) -> bool:
    dx = start[0] - anchor[0]
    dz = start[2] - anchor[2]
    drop = anchor[1] - start[1]
    if drop == 0:
        return abs(dx) + abs(dz) == 1
    return drop in (1, 2) and (dx, dz) == DIRECTIONS[direction]


def _descending_waypoint_prefix(
    waypoints: list[tuple[int, int, int]], start_y: int,
) -> list[tuple[int, int, int]]:
    previous_y = start_y
    for index, (_, y, _) in enumerate(waypoints):
        if y > previous_y:
            return waypoints[:index]
        previous_y = y
    return waypoints


def _omitted_waypoint_index(
    waypoints: list[tuple[int, int, int]], pos: tuple[int, int, int], edge_kind: str | None,
) -> int | None:
    for index, (a, b) in enumerate(zip(waypoints, waypoints[1:]), 1):
        if edge_kind in {"stair_down", "stair_up"}:
            if abs(a[1] - pos[1]) != 1 or not (_is_two_step_stair(a, b) or _is_two_block_vertical_gap(a, b)):
                continue
        elif edge_kind in {"spine_forward", "branch_leaf", "offset_sidestep"}:
            # Level excavation saves every third step. Only expand short,
            # straight sections at the same height.
            dx, dz = abs(a[0] - b[0]), abs(a[2] - b[2])
            if a[1] != pos[1] or b[1] != pos[1] or (dx, dz) not in {(2, 0), (3, 0), (0, 2), (0, 3)}:
                continue
        else:
            return None
        if pos in _interpolate_cells(a, b)[1:-1]:
            return index
    return None


def _interpolate_cells(a: tuple[int, int, int], b: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """Cell-by-cell straight line from a to b (each hop <= 1 block on every axis)."""
    steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), abs(b[2] - a[2]))
    if steps <= 1:
        return [a, b]
    out: list[tuple[int, int, int]] = []
    for i in range(1, steps + 1):
        t = i / steps
        out.append(
            (
                int(round(a[0] + (b[0] - a[0]) * t)),
                int(round(a[1] + (b[1] - a[1]) * t)),
                int(round(a[2] + (b[2] - a[2]) * t)),
            )
        )
    return [a, *out]


def _floor_pos(raw: Any) -> tuple[int, int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        raise ValueError(f"Invalid pos: {raw!r}")
    return (math.floor(float(raw[0])), math.floor(float(raw[1])), math.floor(float(raw[2])))


class MiningDFSEngine:
    """Stateful path-compressed underground mining tree supporting 'fishbone' and 'radar' strategies."""

    def __init__(
        self,
        skills: Any,
        *,
        ledger_path: pathlib.Path | str | None = None,
        strategy: str | None = None,
    ) -> None:
        self.skills = skills
        self.bot = skills.bot
        self._explicit_strategy: str | None = normalize_dfs_strategy(strategy) if strategy is not None else None
        self.strategy: str = self._explicit_strategy or normalize_dfs_strategy(None)
        resolved_ledger = ledger_path if ledger_path is not None else _default_ledger_path_for_bot(self.bot)
        self.ledger = WorldLedger(resolved_ledger)
        self.nodes: dict[int, ShaftNode] = {}
        self.next_node_id: int = 1
        self.collar_node_id: int | None = None
        self.active_node_id: int | None = None
        self.target_y: int = 16
        self.deferred_ores: dict[tuple[int, int, int], DeferredOreRecord] = {}
        self.mined_ore_positions: set[tuple[int, int, int]] = set()
        self.unreachable_ores: dict[tuple[int, int, int], tuple[float, str]] = {}
        self.excavated_positions: set[tuple[int, int, int]] = set(self.ledger.excavated_positions)
        self.known_corridor_positions: set[tuple[int, int, int]] = set(self.ledger.known_corridor_positions)
        self.exhausted_collars: list[tuple[int, int, int]] = list(self.ledger.exhausted_collars)
        self.radar_targets: list[tuple[int, int, int]] = []
        self.ever_seen_radar_ores: set[tuple[int, int, int]] = set()
        self.recrafted_picks: int = 0
        self.fluid_aborts: int = 0
        # Set when a water/cave-trap shaft is abandoned while we are still deep underground.
        self.fluid_abandoned_deep: bool = False
        # Highest collar ever opened this run = the surface datum we must not reset away from.
        self.collar_ceiling_y: int | None = None
        # Consecutive explore calls that returned "ok" without moving or gaining anything.
        self.idle_calls: int = 0
        # Ore blocks broken whose drop could not be picked up (through-wall mining losses).
        self.lost_drops: int = 0
        # Consecutive calls on this shaft that produced no target item at all.
        self.dry_calls: int = 0
        self.locked_cluster: dict[str, Any] | None = None
        self._last_locked_rep: tuple[int, int, int] | None = None
        self._locked_rep_dry_calls: int = 0
        self._locked_rep_best_distance: float = math.inf
        self._last_radar_anchor: tuple[int, int, int] | None = None
        self.sealed_positions: set[tuple[int, int, int]] = set()
        self.catch_blocks_placed: list[list[int]] = []
        self.bridge_blocks_placed: list[list[int]] = []
        self.stone_pick_rock_equips: int = 0
        self.ore_pick_equips: int = 0
        self.dug_during_return: int = 0
        self.relocations_count: int = 0
        self.surgical_extractions: int = 0
        self.verified_ore_pickups: int = 0
        self.drop_receipts: dict[int, dict[str, Any]] = {}
        self._active_geofence: Any = None
        self._deadline: float | None = None
        self._budget_session: _BudgetSession | None = None
        self._water_interrupted_shaft: _WaterInterruptedShaft | None = None
        # Remember failed escape primitives at each cell across time slices. Otherwise
        # a slow blocked north stair consumes every call and south is never attempted.
        self._climb_failures: dict[tuple[int, int, int], set[tuple[str, str]]] = {}

    def _refresh_strategy(self, override: str | None = None) -> str:
        if override is not None:
            self._explicit_strategy = normalize_dfs_strategy(override)
            self.strategy = self._explicit_strategy
        elif self._explicit_strategy is None:
            self.strategy = normalize_dfs_strategy(None)
        return self.strategy

    @property
    def radar_enabled(self) -> bool:
        return self.strategy in {"v5", "radar"}

    @property
    def surgical_enabled(self) -> bool:
        return self.strategy == "radar"

    def _clear_active_tree_only(self) -> None:
        self.nodes.clear()
        self.next_node_id = 1
        self.collar_node_id = None
        self.active_node_id = None
        self.sealed_positions.clear()
        self._last_radar_anchor = None

    def _active_shaft_is_retired(self) -> bool:
        collar = self.nodes.get(self.collar_node_id)
        return collar is not None and (
            collar.anchor in self.exhausted_collars
            or any(
                shaft.get("retired") and tuple(shaft.get("collar_anchor") or ()) == collar.anchor
                for shaft in self.ledger.saved_shafts
            )
        )

    def _detach_retired_shaft(self) -> None:
        if self._active_shaft_is_retired():
            self._clear_active_tree_only()

    def _mark_ore_unreachable(self, pt: tuple[int, int, int], reason: str) -> None:
        self.unreachable_ores[pt] = (time.monotonic() + UNREACHABLE_ORE_COOLDOWN_S, reason)
        if pt in self.radar_targets:
            self.radar_targets = [p for p in self.radar_targets if p != pt]
        if self.locked_cluster and tuple(self.locked_cluster.get("rep") or ()) == pt:
            self.locked_cluster = None

    def _is_ore_unreachable(self, pt: tuple[int, int, int]) -> bool:
        rec = self.unreachable_ores.get(pt)
        if rec is None:
            return False
        if rec[0] <= time.monotonic():
            del self.unreachable_ores[pt]
            return False
        return True

    def _archive_current_shaft_to_ledger(self, *, mark_exhausted: bool = False) -> None:
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            return
        collar_anc = self.nodes[self.collar_node_id].anchor
        exhausted = mark_exhausted or not self._tree_has_pending_branches()
        if exhausted and collar_anc not in self.exhausted_collars:
            self.exhausted_collars.append(collar_anc)
        # Always persist the node graph + waypoints. For an exhausted shaft this is the
        # "route home" we can walk later without re-digging stairs.
        self.ledger.upsert_shaft(
            {
                "collar_anchor": list(collar_anc),
                "target_y": self.target_y,
                "collar_node_id": self.collar_node_id,
                "active_node_id": self.active_node_id,
                "next_node_id": self.next_node_id,
                "nodes": [n.to_dict() for n in self.nodes.values()],
                "retired": bool(exhausted),
            }
        )
        if exhausted:
            self.ledger.record_exhausted_collar(collar_anc)
        self.ledger.save()

    def _tree_has_pending_branches(self) -> bool:
        for n in self.nodes.values():
            if any(b.status == "pending" for b in n.branches):
                return True
            if n.kind == "spine" and any(
                b.kind == "branch_leaf"
                and b.status == "completed"
                and b.prune_reason != "promoted_to_spine"
                and len(b.completed_waypoints) >= 3
                for b in n.branches
            ):
                return True
        return False

    def _active_deep_branch_pending(self) -> bool:
        active = self.nodes.get(self.active_node_id)
        collar = self.nodes.get(self.collar_node_id)
        if active is None or collar is None:
            return False
        ceiling = collar.anchor[1]
        if self.collar_ceiling_y is not None:
            ceiling = max(ceiling, self.collar_ceiling_y)
        return (
            active.anchor[1] <= ceiling - 20
            and any(branch.status == "pending" for branch in active.branches)
        )

    def reset(self, *, geofence: Any = None) -> dict[str, Any]:
        if not self.nodes and self.collar_node_id is None:
            # Live s07/s08: an empty tree made reset a free "ok" no-op, so the model pressed it
            # 250+ times at ~3.5s/turn (it was drowning and looking for any way out).
            return {
                "ok": False,
                "requires_replan": True,
                "action": "reset",
                "strategy": self.strategy,
                "stop_reason": "nothing_to_reset",
                "message": (
                    "当前没有可重置的矿井（空间树为空），reset 不会改变任何状态。"
                    "若被困或在水里：先用 mc_skill_unstick，或 mc_skill_staircase(vertical='up') 爬到岸上，"
                    "再直接 explore 开新井。"
                ),
                "tree": self.snapshot(),
            }
        collar = self.nodes.get(self.collar_node_id)
        active = self.nodes.get(self.active_node_id)
        reset_origin = collar or active
        had_collar = reset_origin.anchor if reset_origin is not None else None

        deep_ceiling = self.collar_ceiling_y
        ref_y: int | None = None
        if had_collar is not None:
            ref_y = had_collar[1]
        if deep_ceiling is not None:
            ref_y = int(deep_ceiling) if ref_y is None else max(ref_y, int(deep_ceiling))
        if ref_y is not None:
            try:
                st_chk = self.bot.status()
                if st_chk.get("pos") and _floor_pos(st_chk["pos"])[1] <= ref_y - 20:
                    now_y = _floor_pos(st_chk["pos"])[1]
                    # Underground reset would orphan the tree with no surface anchor and no shaft
                    # to climb; live s01 burned 8 resets at y=-54 and never found ore, and s03
                    # cleared the tree at y=8 then spun on no_progress.
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "action": "reset",
                        "strategy": self.strategy,
                        "stop_reason": "reset_needs_surface",
                        "message": (
                            f"你当前在 y={now_y}，比本局最高井口 (y={ref_y}) 低 20 格以上，不能就地重置。"
                            "如果只是这条支巷挖干了，直接 action='explore' 会继续开支巷；"
                            "如果要换井：" + self._surface_climb_hint()
                        ),
                        "tree": self.snapshot(),
                    }
            except (ApiError, ValueError, KeyError):
                pass

        for node in (collar, active):
            if node is not None:
                if node.anchor not in self.exhausted_collars:
                    self.exhausted_collars.append(node.anchor)
                self.ledger.record_exhausted_collar(node.anchor)
        self._clear_active_tree_only()
        self.deferred_ores.clear()
        self.radar_targets.clear()
        self.ever_seen_radar_ores.clear()
        self.locked_cluster = None
        self.catch_blocks_placed.clear()
        self.bridge_blocks_placed.clear()
        self.stone_pick_rock_equips = 0
        self.ore_pick_equips = 0
        self.dug_during_return = 0
        # A reset starts a fresh shaft: a stale hazard streak made the very next explore
        # return shaft_exhausted instantly (live s03: reset -> explore -> shaft_exhausted x6).
        self.fluid_aborts = 0
        self.fluid_abandoned_deep = False

        relocated_from: list[int] | None = None
        relocated_to: list[int] | None = None
        if had_collar is not None:
            try:
                st = self.bot.status()
                if st.get("alive") is not False and st.get("ready") is not False and st.get("pos"):
                    cur_pos = _floor_pos(st["pos"])
                    if self._is_near_exhausted_collar(cur_pos):
                        relocated_from = list(cur_pos)
                        new_pos = self._relocate_to_fresh_collar_site(cur_pos, geofence=geofence)
                        if new_pos != cur_pos:
                            relocated_to = list(new_pos)
            except (ApiError, ValueError, KeyError):
                pass
        self.ledger.save()
        out: dict[str, Any] = {
            "ok": True,
            "action": "reset",
            "strategy": self.strategy,
            "message": (
                f"MiningDFS ({self.strategy}) 矿井空间树已重置并自动换点至 {relocated_to}。"
                if relocated_to is not None
                else f"MiningDFS ({self.strategy}) 矿井空间树已重置（如需换井请确保已离开旧井口 >=14 格）。"
            ),
        }
        if relocated_from is not None:
            out["relocated_from"] = relocated_from
        if relocated_to is not None:
            out["relocated_to"] = relocated_to
        return out

    def snapshot(self) -> dict[str, Any]:
        node_summaries = [
            {
                "node_id": n.node_id,
                "kind": n.kind,
                "anchor": list(n.anchor),
                "parent_id": n.parent_id,
                "edge_kind": n.edge_from_parent.edge_kind if n.edge_from_parent else None,
                "pending_branches": [
                    {"kind": b.kind, "dir": b.direction, "steps": b.steps}
                    for b in n.branches
                    if b.status == "pending"
                ],
                "pruned_branches": [
                    {"kind": b.kind, "dir": b.direction, "reason": b.prune_reason}
                    for b in n.branches
                    if b.status == "pruned"
                ],
            }
            for n in self.nodes.values()
        ]
        snap: dict[str, Any] = {
            "strategy": self.strategy,
            "collar_node_id": self.collar_node_id,
            "active_node_id": self.active_node_id,
            "target_y": self.target_y,
            "node_count": len(self.nodes),
            "dry_calls": self.dry_calls,
            "nodes": node_summaries[-12:],
            "deferred_ores": [
                {
                    "pos": list(rec.pos),
                    "block": rec.block_name,
                    "required_tier": rec.required_tier,
                    "nearest_node_id": rec.nearest_node_id,
                }
                for rec in self.deferred_ores.values()
            ],
            "radar_targets": [list(p) for p in self.radar_targets[:8]],
            "radar_ever_seen_count": len(self.ever_seen_radar_ores),
            "exhausted_collars": [list(p) for p in self.exhausted_collars[-6:]],
            "sealed_count": len(self.sealed_positions),
            "catch_blocks_placed": len(self.catch_blocks_placed),
            "bridge_blocks_placed": len(self.bridge_blocks_placed),
            "stone_pick_rock_equips": self.stone_pick_rock_equips,
            "ore_pick_equips": self.ore_pick_equips,
            "relocations_count": self.relocations_count,
            "surgical_extractions": self.surgical_extractions,
            "verified_ore_pickups": self.verified_ore_pickups,
        }
        if self.locked_cluster is not None:
            snap["locked_cluster"] = {
                "rep": list(self.locked_cluster["rep"]),
                "size": int(self.locked_cluster["size"]),
                "mob_nearby": bool(self.locked_cluster.get("mob_nearby", False)),
            }
        return snap

    # ---- Low-level Safe World Helpers ----

    def _record_excavated(self, pt: tuple[int, int, int]) -> None:
        self.excavated_positions.add(pt)
        self.ledger.record_excavated(pt)

    def _record_corridor(self, pt: tuple[int, int, int]) -> None:
        self.known_corridor_positions.add(pt)
        self.known_corridor_positions.add((pt[0], pt[1] + 1, pt[2]))
        self.ledger.record_corridor(pt)

    def _is_self_cavity(self, pts: list[tuple[int, int, int]] | tuple[tuple[int, int, int], ...]) -> bool:
        return any(p in self.excavated_positions or p in self.known_corridor_positions for p in pts)

    def _is_near_exhausted_collar(self, pos: tuple[int, int, int], *, min_dist: float = 12.0) -> bool:
        for ex in self.exhausted_collars:
            if math.hypot(pos[0] - ex[0], pos[2] - ex[2]) < min_dist and abs(pos[1] - ex[1]) <= 18:
                return True
        return False

    def _get_block_name(self, pt: tuple[int, int, int], cache: dict[tuple[int, int, int], str] | None = None) -> str:
        if cache is not None and pt in cache:
            return cache[pt]
        res = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
        raw = res.get("name") or res.get("block")
        if not raw:
            raise ApiError("unknown_block", f"无法确认方块 {list(pt)}")
        name = str(raw).removeprefix("minecraft:")
        if cache is not None:
            cache[pt] = name
        if name in {"crafting_table", "furnace"}:
            self.ledger.record_utility(name, pt)
        return name

    def _scan_hostile_positions(self, radius: int = 32) -> list[tuple[float, float, float]]:
        try:
            res = self.bot.call("get_entities", {"radius": radius, "types": ["hostile"]})
            ents = res.get("entities") or []
        except ApiError:
            return []
        out: list[tuple[float, float, float]] = []
        for e in ents:
            if not isinstance(e, dict):
                continue
            p = e.get("pos")
            if isinstance(p, (list, tuple)) and len(p) >= 3:
                out.append((float(p[0]), float(p[1]), float(p[2])))
        return out

    def _find_offhand_swap_item(self, inv: dict[str, Any], *, max_exclude_tier: int = 3) -> str | None:
        items = [
            r
            for r in (inv.get("items") or [])
            if isinstance(r, dict) and r.get("name") and int(r.get("count") or 1) > 0
        ]
        counts: dict[str, int] = {}
        for r in items:
            n = str(r["name"]).removeprefix("minecraft:")
            counts[n] = counts.get(n, 0) + int(r.get("count") or 1)
        seal = _pick_seal_material(counts)
        if seal:
            return seal
        non_pick = [
            str(r["name"]).removeprefix("minecraft:")
            for r in items
            if str(r["name"]).removeprefix("minecraft:") not in PICKAXE_TIERS
        ]
        if non_pick:
            return non_pick[0]
        lower_pick = [
            str(r["name"]).removeprefix("minecraft:")
            for r in items
            if PICKAXE_TIERS.get(str(r["name"]).removeprefix("minecraft:"), 99) < max_exclude_tier
        ]
        return lower_pick[0] if lower_pick else None

    def _equip_for_block_tier(
        self,
        required_tier: int,
        *,
        for_ore: bool = False,
        inv_snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, int, int]:
        """Protect 1 high-tier pickaxe in offhand while mining rock; equip required tier on demand."""
        inv = inv_snapshot if isinstance(inv_snapshot, dict) else self.bot.inventory()
        picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
        held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None

        if required_tier <= 2:
            low_main = [p for p in picks if not p.get("in_offhand") and int(p["tier"]) <= 2]
            high_main = [p for p in picks if not p.get("in_offhand") and int(p["tier"]) >= 3]
            high_off = [p for p in picks if p.get("in_offhand") and int(p["tier"]) >= 3]
            if low_main and high_main and not high_off:
                high_keep = max(high_main, key=lambda p: (int(p["tier"]), int(p["remaining"])))
                try:
                    self.bot.call("equip", {"item": high_keep["name"], "slot": "off-hand"})
                    inv = self.bot.inventory()
                    picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
                    held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None
                except ApiError:
                    pass

        chosen = _choose_pickaxe_for_tier(picks, required_tier, prefer_stone=not for_ore)
        if chosen is not None and chosen.get("in_offhand"):
            swap_item = self._find_offhand_swap_item(inv, max_exclude_tier=int(chosen["tier"]))
            if swap_item:
                try:
                    self.bot.call("equip", {"item": swap_item, "slot": "off-hand"})
                    inv = self.bot.inventory()
                    picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
                    held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None
                    chosen = _choose_pickaxe_for_tier(picks, required_tier, prefer_stone=not for_ore)
                except ApiError:
                    pass

        if chosen is not None and (chosen.get("in_offhand") or chosen["name"] != held_name):
            try:
                self.bot.call("equip", {"item": chosen["name"], "slot": "hand"})
                held_name = chosen["name"]
                if for_ore:
                    self.ore_pick_equips += 1
                elif chosen["name"] in {"stone_pickaxe", "wooden_pickaxe"}:
                    self.stone_pick_rock_equips += 1
            except ApiError:
                pass
        return chosen, held_name, max_tier, total_rem

    def _geofence_blocked(self, geofence: Any, x: float, z: float, *, action: str) -> dict[str, Any] | None:
        if geofence is None:
            return None
        return geofence.check_point(x + 0.5, z + 0.5, action=action)

    def _inspect_step_hazards(
        self,
        targets: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        *,
        forward_delta: tuple[int, int] | None = None,
    ) -> tuple[tuple[str, dict[str, Any]] | None, list[tuple[int, int, int]]]:
        """Distinguish in-path fluid/bedrock hazards from sealable lateral/overhead fluids."""
        if not targets:
            return None, []
        target_set = set(targets)
        sealable_fluids: list[tuple[int, int, int]] = []
        seen_fluids: set[tuple[int, int, int]] = set()
        top_target = targets[0]

        for p in targets:
            b = self._get_block_name(p, cache)
            if b in FLUID_BLOCKS or b == "bedrock":
                return ("fluid_or_bedrock", {"pos": list(p), "block": b, "relation": "in_path"}), []
            if b == "pointed_dripstone":
                return ("falling_material_adjacent", {"pos": list(p), "block": b, "relation": "in_path"}), []
            for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, -1)):
                nb = (p[0] + ax, p[1] + ay, p[2] + az)
                if nb in target_set:
                    continue
                nb_name = self._get_block_name(nb, cache)
                if ay == 1 and nb_name == "pointed_dripstone":
                    return (
                        "falling_material_adjacent",
                        {"pos": list(nb), "block": nb_name, "relation": "overhead", "target": list(p)},
                    ), []
                if nb_name in FLUID_BLOCKS:
                    if forward_delta is not None and (ax, az) == forward_delta and nb_name in LAVA_BLOCKS:
                        return (
                            "fluid_or_bedrock",
                            {"pos": list(nb), "block": nb_name, "relation": "forward_lava", "target": list(p)},
                        ), []
                    if nb not in seen_fluids:
                        seen_fluids.add(nb)
                        sealable_fluids.append(nb)

        above1 = (top_target[0], top_target[1] + 1, top_target[2])
        above2 = (top_target[0], top_target[1] + 2, top_target[2])
        a1_name = self._get_block_name(above1, cache)
        if a1_name in FALLING_BLOCKS and self._get_block_name(above2, cache) in FALLING_BLOCKS:
            return (
                "falling_material_adjacent",
                {
                    "pos": list(above1),
                    "block": a1_name,
                    "relation": "overhead_falling_column",
                    "target": list(top_target),
                },
            ), sealable_fluids
        return None, sealable_fluids

    def _adjacent_fluid_or_falling(
        self,
        targets: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[str, dict[str, Any]] | None:
        hazard, adj_fluids = self._inspect_step_hazards(targets, cache)
        if hazard is not None:
            return hazard
        if adj_fluids:
            nb = adj_fluids[0]
            return "fluid_or_bedrock", {"pos": list(nb), "block": cache.get(nb, "fluid"), "relation": "adjacent_fluid"}
        return None

    def _is_solid_floor(self, pt: tuple[int, int, int], cache: dict[tuple[int, int, int], str]) -> bool:
        b = self._get_block_name(pt, cache)
        if b in PASSABLE_BLOCKS | FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS | FLIMSY_FLOOR_BLOCKS:
            return False
        if b.endswith(FLIMSY_FLOOR_SUFFIXES):
            return False
        if b in FALLING_BLOCKS:
            below = self._get_block_name((pt[0], pt[1] - 1, pt[2]), cache)
            if below in PASSABLE_BLOCKS | FLUID_BLOCKS | FLIMSY_FLOOR_BLOCKS or below.endswith(FLIMSY_FLOOR_SUFFIXES):
                return False
        return True

    def _dig_cell_safely(
        self,
        dpos: tuple[int, int, int],
        *,
        collect_rock: bool,
        parent_node_id: int,
        cache: dict[tuple[int, int, int], str],
        verify_pickaxe: bool = False,
    ) -> tuple[bool, str | None, bool, int]:
        """Dig one corridor cell safely; if it is a tracked ore, verify real drop pickup."""
        deferred_added = 0
        mined_ore = False
        stale_passable = cache.get(dpos) in PASSABLE_BLOCKS | FALLING_BLOCKS
        bname = self._get_block_name(dpos, None if stale_passable else cache)
        if bname in PASSABLE_BLOCKS:
            cache[dpos] = bname
            return True, None, False, 0
        if bname == "pointed_dripstone":
            return False, "falling_material_adjacent", False, 0
        avoidance = self.skills.temporary_avoidance.get(dpos)
        if avoidance and avoidance.get("reason") == "dig_timeout" and self.skills._is_block_avoided(dpos):
            return False, "dig_cell_avoided", False, 0

        is_tracked = bname in TRACKED_ORES
        expected_ore_drops = _expected_drops_for_blocks([bname]) if is_tracked else frozenset()
        before_ore_cnt = 0
        if is_tracked and expected_ore_drops:
            inv_pre, counts_pre = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_pre) < 2 and not _can_stack_expected_drop(inv_pre, expected_ore_drops):
                inv_pre, _ = self.skills._ensure_free_slots(min_free=4, inv_snapshot=inv_pre)
                _, counts_pre = _inventory_snapshot_and_counts(self.bot)
            before_ore_cnt = sum(int(counts_pre.get(it, 0)) for it in expected_ore_drops)

        for _ in range(5):
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 1))
            if req_t > 1:
                chosen_o, _, max_t, _ = self._equip_for_block_tier(req_t, for_ore=(bname in TRACKED_ORES))
                if chosen_o is None or max_t < req_t:
                    if bname in TRACKED_ORES and dpos not in self.deferred_ores:
                        self.deferred_ores[dpos] = DeferredOreRecord(dpos, bname, req_t, parent_node_id)
                        self.ledger.record_ore(dpos, bname, req_t)
                        deferred_added += 1
                    return False, "insufficient_pickaxe_tier", mined_ore, deferred_added
            elif verify_pickaxe:
                chosen, held, _, _ = self._equip_for_block_tier(1)
                if chosen is None or held not in PICKAXE_TIERS:
                    return False, "pickaxe_low", mined_ore, deferred_added

            is_tracked = bname in TRACKED_ORES
            should_collect = bool(is_tracked or collect_rock)
            retried_tool = False
            while True:
                try:
                    self.bot.call("dig", {"pos": [dpos[0], dpos[1], dpos[2]], "collect": should_collect, "stop_on": ["hurt", "death"]})
                    cache[dpos] = "air"
                    self._record_excavated(dpos)
                    if is_tracked:
                        self.mined_ore_positions.add(dpos)
                        self.ledger.remove_ore(dpos)
                        if expected_ore_drops:
                            gained_ok = self._collect_and_verify_target_drop(
                                expected_drops=expected_ore_drops,
                                before_count=before_ore_cnt,
                                ore_pt=dpos,
                                stand_y=dpos[1],
                                cache=cache,
                                can_open_lower=False,
                            )
                            mined_ore = gained_ok
                        else:
                            mined_ore = True
                    break
                except ApiError as err:
                    if err.code == "nothing_to_dig":
                        cache[dpos] = "air"
                        self._record_excavated(dpos)
                        return True, None, mined_ore, deferred_added
                    if err.code in {"hurt", "death", "interrupted", "protected_region"}:
                        return False, ("hurt" if err.code in {"hurt", "death"} else err.code), mined_ore, deferred_added
                    if not retried_tool:
                        retried_tool = True
                        st = self.bot.status()
                        if st.get("alive") is False:
                            return False, "not_ready", mined_ore, deferred_added
                        cache.pop(dpos, None)
                        live_blk = self._get_block_name(dpos, cache)
                        if live_blk in PASSABLE_BLOCKS:
                            return True, None, mined_ore, deferred_added
                        bname = live_blk
                        self._equip_for_block_tier(int(BLOCK_MIN_PICKAXE_TIER.get(bname, 1)), for_ore=(bname in TRACKED_ORES))
                        continue
                    return False, "dig_failed", mined_ore, deferred_added

            above_pt = (dpos[0], dpos[1] + 1, dpos[2])
            if bname in FALLING_BLOCKS or self._get_block_name(above_pt, cache) in FALLING_BLOCKS:
                time.sleep(0.14)
                cache.pop(dpos, None)
                cache.pop(above_pt, None)
                refill = self._get_block_name(dpos, cache)
                if refill in PASSABLE_BLOCKS:
                    return True, None, mined_ore, deferred_added
                if refill in FLUID_BLOCKS:
                    return False, "fluid_or_bedrock", mined_ore, deferred_added
                bname = refill
                continue
            return True, None, mined_ore, deferred_added

        return False, "falling_material_adjacent", mined_ore, deferred_added

    def _seal_breach(
        self,
        seal_coords: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        *,
        allow_corridor: bool = False,
    ) -> list[list[int]]:
        """Place solid seal blocks at exposed air/fluid openings while preserving return corridors."""
        placed: list[list[int]] = []
        counts = _inventory_counts(self.bot)
        filler = _pick_seal_material(counts)
        if not filler:
            return placed
        replaceable = (PASSABLE_BLOCKS | FLUID_BLOCKS) - {"torch", "wall_torch"}
        for pt in seal_coords:
            if pt in self.sealed_positions:
                continue
            if not allow_corridor and pt in self.known_corridor_positions:
                continue
            cache.pop(pt, None)
            try:
                cur = self._get_block_name(pt, cache)
            except ApiError:
                continue
            if cur not in replaceable:
                continue
            try:
                st = self.bot.status()
                bpos = _floor_pos(st.get("pos") or [0, 0, 0])
                if pt in {bpos, (bpos[0], bpos[1] + 1, bpos[2])}:
                    continue
            except (ApiError, ValueError):
                continue
            try:
                self.bot.call("place", {"item": filler, "pos": [pt[0], pt[1], pt[2]]})
                cache[pt] = filler
                self.sealed_positions.add(pt)
                placed.append([pt[0], pt[1], pt[2]])
            except ApiError:
                counts = _inventory_counts(self.bot)
                filler = _pick_seal_material(counts)
                if not filler:
                    break
        return placed

    def _place_bridge_block(
        self,
        support_pt: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> bool:
        """Place a solid floor/parapet block across self-dug cavities, gaps, or window sills."""
        counts = _inventory_counts(self.bot)
        filler = _pick_seal_material(counts)
        if not filler:
            return False
        cache.pop(support_pt, None)
        try:
            cur = self._get_block_name(support_pt, cache)
        except ApiError:
            return False
        if self._is_solid_floor(support_pt, cache):
            return True
        replaceable = (PASSABLE_BLOCKS | FLUID_BLOCKS | FLIMSY_FLOOR_BLOCKS) - {"torch", "wall_torch"}
        if cur.endswith(FLIMSY_FLOOR_SUFFIXES) and cur not in replaceable:
            try:
                self.bot.call("dig", {"pos": [support_pt[0], support_pt[1], support_pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                cache[support_pt] = "air"
            except ApiError:
                return False
        elif cur not in replaceable:
            return False
        try:
            self.bot.call("place", {"item": filler, "pos": [support_pt[0], support_pt[1], support_pt[2]]})
            cache[support_pt] = filler
            self.bridge_blocks_placed.append([support_pt[0], support_pt[1], support_pt[2]])
            return True
        except ApiError:
            return False

    def _settled_position(self, cur: list[float] | None = None) -> tuple[list[float], bool]:
        pos = list(cur) if cur else [0.0, 64.0, 0.0]
        on_ground = True
        for _ in range(6):
            st = self.bot.status()
            if st.get("pos"):
                pos = [float(v) for v in st["pos"][:3]]
            if st.get("in_water") or st.get("in_lava") or st.get("alive") is False or st.get("ready") is False:
                return pos, False
            on_ground = bool(st.get("on_ground", True))
            if on_ground:
                break
            time.sleep(0.08)
        return pos, on_ground

    def _move_exact(self, pt: tuple[int, int, int], *, timeout_ms: int = 4500) -> bool:
        fx, fy, fz = pt[0] + 0.5, float(pt[1]), pt[2] + 0.5
        try:
            self.bot.call("move_to", {"x": fx, "y": fy, "z": fz, "range": 0.35, "timeout": timeout_ms, "stop_on": ["hurt", "death"]})
        except ApiError:
            pass
        cur, on_ground = self._settled_position()
        if not on_ground:
            return False
        if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.78:
            self._record_corridor(pt)
            return True

        if math.dist((cur[0], cur[2]), (fx, fz)) <= 2.6:
            need_jump = fy > cur[1] + 0.25
            try:
                self.bot.call("look", {"x": fx, "y": fy + 0.4, "z": fz})
                ctrl_body: dict[str, Any] = {
                    "forward": True,
                    "ms": 540 if need_jump else 420,
                    "stop_on": ["hurt", "death"],
                }
                if need_jump:
                    ctrl_body["jump"] = True
                self.bot.call("control", ctrl_body)
            except ApiError:
                pass
            time.sleep(0.08)
            cur, on_ground = self._settled_position(cur)
            if not on_ground:
                return False
            if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.85:
                self._record_corridor(pt)
                return True

        if math.dist((cur[0], cur[2]), (fx, fz)) <= 2.6:
            try:
                self.bot.call("move_to", {"x": fx, "y": fy, "z": fz, "range": 0.3, "timeout": 2500, "stop_on": ["hurt", "death"]})
            except ApiError:
                pass
            cur, on_ground = self._settled_position(cur)
            if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.85 and on_ground:
                self._record_corridor(pt)
                return True
        return False

    def _step_to(
        self,
        cell: tuple[int, int, int],
        *,
        from_cell: tuple[int, int, int] | None = None,
        repair_support: bool = False,
    ) -> bool:
        """One-cell traversal that also handles step-ups, gaps and 1-block drops.

        Live n0112: repeated `backtrack_blocked@...,(feet=air,head=air)` — nothing obstructed,
        move_exact simply cannot jump a gap or a 1-block ledge, so the whole route was abandoned.
        """
        # On a recorded ascending stair, try the local jump before asking the
        # pathfinder to solve the entire adjacent step.
        jump_first = (
            from_cell is not None and cell[1] == from_cell[1] + 1
            and abs(cell[0] - from_cell[0]) + abs(cell[2] - from_cell[2]) == 1
        )
        if jump_first and self._hop_up_to(cell, repair_support=repair_support):
            return True
        if self._move_exact(cell):
            return True
        if not jump_first and self._hop_up_to(cell):
            return True
        try:
            pos, grounded = self._settled_position()
        except (ApiError, TypeError, ValueError):
            return False
        if not grounded:
            return False
        dx = cell[0] + 0.5 - float(pos[0])
        dz = cell[2] + 0.5 - float(pos[2])
        if max(abs(dx), abs(dz)) > 1.6:
            return False
        yaw = math.atan2(-dx, -dz)
        for jump in (True, False):
            try:
                self.bot.call(
                    "control",
                    {"forward": True, "jump": jump, "yaw": yaw, "ms": 650, "stop_on": ["hurt", "death"]},
                )
            except ApiError as exc:
                if exc.code in {"hurt", "death", "dead", "not_ready", "disconnected"}:
                    return False
            try:
                position, grounded = self._settled_position(pos)
            except (ApiError, TypeError, ValueError):
                return False
            if not grounded:
                return False
            if _floor_pos(position) == cell:
                return True
        return False

    def _hop_up_to(self, cell: tuple[int, int, int], *, ms: int = 700, repair_support: bool = False) -> bool:
        """Jump-step onto an air cell one block higher (move_exact cannot jump).

        Live s19: the route block was reported as feet=air,head=air — nothing was obstructing,
        the bot simply refused a 1-block step-up, which aborted the whole walk home.
        """
        try:
            st = self.bot.status()
            if (
                not st.get("on_ground", True) or st.get("in_water") or st.get("in_lava")
                or st.get("alive") is False or st.get("ready") is False
            ):
                return False
            pos = st.get("pos") or [0.0, 64.0, 0.0]
            cur = (math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2])))
        except (ApiError, TypeError, ValueError):
            return False
        if cell[1] - cur[1] != 1 or max(abs(cell[0] - cur[0]), abs(cell[2] - cur[2])) > 1:
            return False
        x, y, z = cell
        support = (x, y - 1, z)
        cache: dict[tuple[int, int, int], str] = {}
        try:
            if (
                self._get_block_name(cell) not in PASSABLE_BLOCKS
                or self._get_block_name((x, y + 1, z)) not in PASSABLE_BLOCKS
            ):
                return False
            if not self._is_solid_floor(support, cache):
                # Repair an open, recorded stair from the neighboring step. The
                # bot can already be standing in the missing support cell.
                if (
                    not repair_support or support == cur or cache.get(support) not in AIR_BLOCKS
                    or self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                    or not self._is_solid_floor((cur[0], cur[1] - 1, cur[2]), cache)
                    or self._adjacent_fluid_or_falling([(x, y + 1, z), cell, support], cache) is not None
                ):
                    return False
                if not self._place_bridge_block(support, cache):
                    return False
                cache.pop(support, None)
                if not self._is_solid_floor(support, cache):
                    return False
        except ApiError:
            return False
        dx = cell[0] + 0.5 - float(pos[0])
        dz = cell[2] + 0.5 - float(pos[2])
        yaw = math.atan2(-dx, -dz)
        try:
            self.bot.call(
                "control",
                {"forward": True, "jump": True, "yaw": yaw, "ms": ms, "stop_on": ["hurt", "death"]},
            )
        except ApiError:
            return False
        try:
            position, grounded = self._settled_position(pos)
        except (ApiError, TypeError, ValueError):
            return False
        if grounded and _floor_pos(position) == cell:
            self._record_corridor(cell)
            return True
        return False

    def _pillar_to_recorded_step(
        self, cell: tuple[int, int, int], *, deadline: float | None, min_hp: float
    ) -> bool:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        x, y, z = cell
        support = (x, y - 1, z)
        try:
            st = self.bot.status()
            if (
                st.get("alive") is False or st.get("ready") is False
                or not st.get("on_ground", True) or st.get("health", min_hp) < min_hp
                or _floor_pos(st.get("pos")) != support
                or self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                or self.skills._is_block_avoided(support)
            ):
                return False
            cache: dict[tuple[int, int, int], str] = {}
            if any(self._get_block_name(pt, cache) not in AIR_BLOCKS for pt in (support, cell, (x, y + 1, z))):
                return False
            if not self._is_solid_floor((x, y - 2, z), cache):
                return False
            result = self.skills.dig_staircase(vertical="pillar", steps=1, stop_on_ore=False, deadline=deadline)
            self.dug_during_return += int(result.get("dug_count", 0))
            if result.get("placed_count"):
                self.bridge_blocks_placed.append(list(support))
            position, grounded = self._settled_position(st["pos"])
        except (ApiError, TypeError, ValueError):
            return False
        if grounded and _floor_pos(position) == cell:
            self._record_corridor(cell)
            return True
        return False

    def _drop_to_recorded_step(self, cell: tuple[int, int, int], *, min_hp: float) -> bool:
        x, y, z = cell
        cache: dict[tuple[int, int, int], str] = {}
        try:
            stance = self.bot.status()
            if (
                stance.get("alive") is False or stance.get("ready") is False
                or not stance.get("on_ground", True) or stance.get("health", min_hp) < min_hp
                or _floor_pos(stance["pos"]) != (x, y + 1, z)
                or self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                or self.skills._is_block_avoided(cell)
            ):
                return False
            if (
                self._get_block_name(cell, cache) in PASSABLE_BLOCKS | FLUID_BLOCKS | UTILITY_BLOCKS | {"bedrock"}
                or self._get_block_name((x, y + 1, z), cache) not in PASSABLE_BLOCKS
                or not self._is_solid_floor((x, y - 1, z), cache)
                or self._adjacent_fluid_or_falling([cell], cache) is not None
            ):
                return False
            self.bot.call("dig", {"pos": list(cell), "collect": False, "stop_on": ["hurt", "death"]})
            self.dug_during_return += 1
            pos, on_ground = self._settled_position(stance["pos"])
        except ApiError:
            return False
        if not on_ground or _floor_pos(pos) != cell:
            return False
        self._record_corridor(cell)
        return True

    def _clear_route_obstruction(self, target_wp: tuple[int, int, int]) -> int:
        """Re-open a recorded corridor cell: clear ordinary fill while preserving fluids and utility blocks.

        Returns the number of blocks dug, or -1 when the route is unrecoverable here.
        The cell belongs to our own carved corridor, so clearing it cannot open new terrain.
        """
        x, y, z = target_wp
        cells = (target_wp, (x, y + 1, z))
        try:
            names = [self._get_block_name(pt) for pt in cells]
        except ApiError:
            return -1
        if any(name in FLUID_BLOCKS | UTILITY_BLOCKS | {"bedrock"} for name in names):
            return -1
        dug = 0
        for pt, name in zip(cells, names):
            if name in PASSABLE_BLOCKS:
                continue
            try:
                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                dug += 1
            except ApiError:
                return -1
        return dug

    def _clear_incidental_obstruction_only(self, target_wp: tuple[int, int, int]) -> int:
        """During lossless backtrack, clear loose debris AND self-placed seal/catch/bridge blocks at waypoint."""
        dug = 0
        self_placed = (
            self.sealed_positions
            | {tuple(p) for p in self.catch_blocks_placed}
            | {tuple(p) for p in self.bridge_blocks_placed}
        )
        for pt in (target_wp, (target_wp[0], target_wp[1] + 1, target_wp[2])):
            if pt not in self_placed:
                continue
            try:
                if self._get_block_name(pt) in UTILITY_BLOCKS:
                    continue
                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                self.sealed_positions.discard(pt)
                dug += 1
            except ApiError:
                pass
        for dy in (1, 0):
            pt = (target_wp[0], target_wp[1] + dy, target_wp[2])
            try:
                b = self._get_block_name(pt)
            except ApiError:
                continue
            if b in FALLING_BLOCKS:
                try:
                    self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                    dug += 1
                except ApiError:
                    pass
        return dug

    def _recorded_mixed_stair(
        self, lower: tuple[int, int, int], upper: tuple[int, int, int],
        *, cache: dict[tuple[int, int, int], str] | None = None,
    ) -> list[tuple[int, int, int]] | None:
        """Recover an omitted flat step from the actual footing on a short recorded ascent."""
        dx, dy, dz = (b - a for a, b in zip(lower, upper))
        length = abs(dx) + abs(dz)
        if dx * dz or length not in (3, 4) or dy != length - 1:
            return None
        route = [lower]
        if cache is None:
            cache = {}
        try:
            for offset in range(1, length):
                x, z = lower[0] + dx // length * offset, lower[2] + dz // length * offset
                low_y = max(route[-1][1], upper[1] - (length - offset))
                high_y = min(route[-1][1] + 1, upper[1])
                if self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None:
                    return None
                for y in range(high_y, low_y - 1, -1):
                    if (
                        self._get_block_name((x, y, z), cache) in PASSABLE_BLOCKS
                        and self._get_block_name((x, y + 1, z), cache) in PASSABLE_BLOCKS
                        and self._is_solid_floor((x, y - 1, z), cache)
                    ):
                        route.append((x, y, z))
                        break
                else:
                    return None
        except ApiError:
            return None
        return [*route, upper]

    def _route_at_position(
        self,
        waypoints: list[tuple[int, int, int]],
        pos: tuple[int, int, int],
        edge_kind: str | None,
    ) -> tuple[list[tuple[int, int, int]], int] | None:
        if pos in waypoints:
            return waypoints, waypoints.index(pos)
        if (offset := _omitted_waypoint_index(waypoints, pos, edge_kind)) is not None:
            return [*waypoints[:offset], pos, *waypoints[offset:]], offset
        if edge_kind not in {"stair_down", "stair_up"}:
            return None
        for index, (start, end) in enumerate(zip(waypoints, waypoints[1:])):
            if not all(min(a, b) <= c <= max(a, b) for a, b, c in zip(start, end, pos)):
                continue
            ascending = start[1] < end[1]
            lower, upper = (start, end) if ascending else (end, start)
            route = self._recorded_mixed_stair(lower, upper)
            if route is None or pos not in route[1:-1]:
                continue
            if not ascending:
                route.reverse()
            return [*waypoints[:index], *route, *waypoints[index + 2:]], index + route.index(pos)
        return None

    def _walk_waypoints(
        self,
        waypoints: list[tuple[int, int, int]],
        *,
        reverse: bool = False,
        min_hp_ref: list[float],
        deadline: float | None = None,
        resume_edge_kind: str | None = None,
        record_climb: bool = False,
    ) -> tuple[bool, str | None]:
        if deadline is None:
            deadline = getattr(self, "_deadline", None)
        ordered = list(reversed(waypoints)) if reverse else list(waypoints)
        if resume_edge_kind is not None and ordered:
            if deadline is not None and time.monotonic() >= deadline:
                return False, "walk_time_slice"
            stance = self.bot.status()
            if stance.get("on_ground", True):
                current = _floor_pos(stance["pos"])
                resumed = self._route_at_position(ordered, current, resume_edge_kind)
                if resumed is not None:
                    route, offset = resumed
                    ordered = route[offset:]
        if record_climb and len(ordered) > 1 and (scoped := self._budget_session) is not None:
            scoped.climb_routes.append((max(0, len(scoped.positions) - 1), ordered))
        for index, wp in enumerate(ordered):
            if deadline is not None and time.monotonic() >= deadline:
                # Live n0211: a 60-waypoint walk held one tool call for 3+ minutes with the bot
                # unattended. Abort on the slice boundary and report it instead.
                return False, "walk_time_slice"
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                return False, "not_ready"
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                return False, "hurt"
            cur = _floor_pos(st["pos"])
            if abs(cur[0] - wp[0]) == 0 and abs(cur[1] - wp[1]) == 0 and abs(cur[2] - wp[2]) == 0:
                continue
            # Waypoints can be far apart (node anchors). Walk the straight line cell by cell:
            # a single move_exact over a 30-block climb always fails, which used to end the
            # route walk ("backtrack_blocked") and force a full staircase re-dig (live s18).
            recorded_segment = index > 0 and cur == ordered[index - 1]
            mixed_stair = self._recorded_mixed_stair(cur, wp) if recorded_segment else None
            hops = mixed_stair or _interpolate_cells(cur, wp)
            if len(hops) > 1:
                # Recorded stairs can omit an intermediate stair or pillar step.
                recorded_stair = recorded_segment and _is_two_step_stair(cur, wp)
                recorded_vertical_gap = recorded_segment and _is_two_block_vertical_gap(cur, wp)
                stepped_all = True
                for previous, hop in zip(hops, hops[1:]):
                    if deadline is not None and time.monotonic() >= deadline:
                        return False, "walk_time_slice"
                    if (hop == wp or recorded_vertical_gap) and previous == (hop[0], hop[1] - 1, hop[2]):
                        if self._pillar_to_recorded_step(hop, deadline=deadline, min_hp=min_hp_ref[0]):
                            continue
                    if not self._step_to(hop, from_cell=previous, repair_support=(hop == wp or recorded_stair)):
                        stepped_all = False
                        break
                if stepped_all:
                    continue
            if deadline is not None and time.monotonic() >= deadline:
                return False, "walk_time_slice"
            if self._move_exact(wp):
                continue
            dx_w = wp[0] - cur[0]
            dy_w = wp[1] - cur[1]
            dz_w = wp[2] - cur[2]
            if dy_w == 0:
                detour_deadline = min(deadline if deadline is not None else math.inf, time.monotonic() + 8.0)
                detoured, detour_error = self._walk_level_detour(wp, min_hp=min_hp_ref[0], deadline=detour_deadline)
                if not detoured and detour_error is None and index + 1 < len(ordered):
                    # The blocked cell can itself be a stored waypoint; try rejoining just past it.
                    detoured, detour_error = self._walk_level_detour(
                        ordered[index + 1], min_hp=min_hp_ref[0], deadline=detour_deadline, bypass=wp,
                    )
                if detoured:
                    continue
                if detour_error:
                    return False, detour_error
            if max(abs(dx_w), abs(dz_w)) == 2 and abs(dy_w) in (0, 2):
                mid_wp = (cur[0] + dx_w // 2, cur[1] + (dy_w // 2), cur[2] + dz_w // 2)
                if self._move_exact(mid_wp) and self._move_exact(wp):
                    continue
            # A failed approach can still land directly below the recorded step.
            if dy_w > 0 and self._pillar_to_recorded_step(wp, deadline=deadline, min_hp=min_hp_ref[0]):
                continue
            cleared = self._clear_incidental_obstruction_only(wp)
            self.dug_during_return += cleared
            if cleared > 0 and self._move_exact(wp):
                continue
            # A recorded corridor cell that got filled in (collapse / inflow debris) used to end
            # the walk here ("backtrack_blocked") and force a full staircase re-dig (live s12/s15).
            repaired = self._clear_route_obstruction(wp)
            if repaired > 0:
                self.dug_during_return += repaired
                if self._move_exact(wp):
                    continue
            try:
                feet = self._get_block_name((wp[0], wp[1], wp[2]))
                head = self._get_block_name((wp[0], wp[1] + 1, wp[2]))
            except ApiError:
                feet, head = "?", "?"
            return (
                False,
                f"backtrack_blocked@{wp[0]},{wp[1]},{wp[2]}"
                f"(feet={feet},head={head},from={cur[0]},{cur[1]},{cur[2]})",
            )
        return True, None

    def _find_node_path(self, start_id: int, goal_id: int) -> list[tuple[list[tuple[int, int, int]], bool, int]]:
        if start_id == goal_id:
            return []
        ancestors_a: list[int] = []
        cur: int | None = start_id
        while cur is not None and cur in self.nodes:
            ancestors_a.append(cur)
            cur = self.nodes[cur].parent_id
        set_a = set(ancestors_a)

        ancestors_b: list[int] = []
        cur_b: int | None = goal_id
        lca: int | None = None
        while cur_b is not None and cur_b in self.nodes:
            if cur_b in set_a:
                lca = cur_b
                break
            ancestors_b.append(cur_b)
            cur_b = self.nodes[cur_b].parent_id

        if lca is None:
            return []

        segments: list[tuple[list[tuple[int, int, int]], bool, int]] = []
        walk = start_id
        while walk != lca:
            node = self.nodes[walk]
            if node.edge_from_parent is None or node.parent_id is None:
                break
            segments.append((node.edge_from_parent.waypoints, True, node.parent_id))
            walk = node.parent_id
        for child_id in reversed(ancestors_b):
            node = self.nodes[child_id]
            if node.edge_from_parent is not None:
                segments.append((node.edge_from_parent.waypoints, False, child_id))
        return segments

    def _is_recorded_shaft_position(self, pos: tuple[int, int, int]) -> bool:
        for node in self.nodes.values():
            if pos == node.anchor:
                return True
            edge = node.edge_from_parent
            if edge is not None and self._route_at_position(edge.waypoints, pos, edge.edge_kind) is not None:
                return True
            if any(
                branch.kind == "branch_leaf" and branch.status == "completed"
                and branch.completed_waypoints and branch.completed_waypoints[0] == node.anchor
                and pos in branch.completed_waypoints
                for branch in node.branches
            ):
                return True
        return False

    def _nearby_node_without_ascent(
        self, pos: tuple[int, int, int], goal_id: int,
    ) -> ShaftNode | None:
        candidates = sorted(self.nodes.values(), key=lambda node: math.dist(pos, node.anchor))
        for candidate in candidates:
            if math.dist(pos, candidate.anchor) > NODE_REJOIN_DISTANCE:
                break
            if candidate.anchor[1] > pos[1]:
                continue
            if candidate.node_id == goal_id:
                return candidate
            route = self._find_node_path(candidate.node_id, goal_id)
            if route and all(
                points and self.nodes[dest_id].anchor[1] <= pos[1] and all(pt[1] <= pos[1] for pt in points)
                for points, _, dest_id in route
            ):
                return candidate
        return None

    def _navigate_to_node(
        self,
        goal_node_id: int,
        *,
        min_hp_ref: list[float],
        deadline: float | None = None,
        allow_lower_rejoin: bool = True,
    ) -> tuple[bool, str | None]:
        if self.active_node_id is None or self.active_node_id not in self.nodes or goal_node_id not in self.nodes:
            return False, "no_active_tree"
        if deadline is None:
            deadline = getattr(self, "_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            return False, "walk_time_slice"
        st = self.bot.status()
        if st.get("alive") is False or st.get("ready") is False:
            return False, "not_ready"
        if st.get("on_ground", True):
            cur_pos = _floor_pos(st["pos"])
        else:
            settled, grounded = self._settled_position(st["pos"])
            if not grounded:
                return False, "not_grounded"
            cur_pos = _floor_pos(settled)
        # A slice can end between nodes. Resume the recorded edge toward the
        # goal subtree, without first walking to the far end and doubling back.
        if cur_pos == self.nodes[goal_node_id].anchor:
            self.active_node_id = goal_node_id
            return True, None
        if cur_pos != self.nodes[self.active_node_id].anchor:
            for node_id, node in self.nodes.items():
                if node.anchor == cur_pos:
                    self.active_node_id = node_id
                    break
            else:
                goal_ancestors = {goal_node_id}
                parent_id = self.nodes[goal_node_id].parent_id
                while parent_id in self.nodes and parent_id not in goal_ancestors:
                    goal_ancestors.add(parent_id)
                    parent_id = self.nodes[parent_id].parent_id

                for node in self.nodes.values():
                    edge = node.edge_from_parent
                    parent_id = node.parent_id
                    if edge is None or parent_id is None or parent_id not in self.nodes:
                        continue
                    resumed = self._route_at_position(edge.waypoints, cur_pos, edge.edge_kind)
                    if resumed is None:
                        continue
                    waypoints, offset = resumed
                    if node.node_id in goal_ancestors:
                        dest = node.node_id
                        route = waypoints[offset:]
                    else:
                        dest = parent_id
                        route = list(reversed(waypoints[:offset + 1]))
                    if route[-1] != self.nodes[dest].anchor:
                        route = [*route, self.nodes[dest].anchor]
                    ok, err = self._walk_waypoints(
                        route, min_hp_ref=min_hp_ref, deadline=deadline, record_climb=not allow_lower_rejoin,
                    )
                    if not ok:
                        return False, err
                    self.active_node_id = dest
                    cur_pos = self.nodes[dest].anchor
                    break
                else:
                    # Finished side branches keep waypoints without adding a node.
                    # Follow the recorded branch back to its junction first.
                    leaf = next((
                        (node.node_id, branch.completed_waypoints)
                        for node in self.nodes.values()
                        for branch in node.branches
                        if branch.kind == "branch_leaf" and branch.status == "completed"
                        and branch.completed_waypoints and branch.completed_waypoints[0] == node.anchor
                        and cur_pos in branch.completed_waypoints
                    ), None)
                    if leaf is not None:
                        dest, waypoints = leaf
                        route = list(reversed(waypoints[:waypoints.index(cur_pos) + 1]))
                        ok, err = self._walk_waypoints(
                            route, min_hp_ref=min_hp_ref, deadline=deadline, record_climb=not allow_lower_rejoin,
                        )
                        if not ok:
                            return False, err
                        self.active_node_id = dest
                        cur_pos = self.nodes[dest].anchor
        anchor_now = self.nodes[self.active_node_id].anchor
        if cur_pos != anchor_now:
            best_near_id: int | None = None
            best_near_d = float("inf")
            min_rejoin_y = -math.inf if allow_lower_rejoin else cur_pos[1]
            for nid, nd in self.nodes.items():
                if nd.anchor[1] < min_rejoin_y:
                    continue
                d = math.dist(cur_pos, nd.anchor)
                if d < best_near_d:
                    best_near_d = d
                    best_near_id = nid
            lower_join = None
            if (
                allow_lower_rejoin and best_near_id is not None
                and self.nodes[best_near_id].anchor[1] > cur_pos[1] >= self.nodes[goal_node_id].anchor[1]
            ):
                lower_join = self._nearby_node_without_ascent(cur_pos, goal_node_id)
            if lower_join is not None and self._move_exact(lower_join.anchor):
                self.active_node_id = lower_join.node_id
            elif best_near_id is not None and best_near_d <= NODE_REJOIN_DISTANCE and (
                best_near_id == self.collar_node_id or best_near_d <= math.dist(cur_pos, anchor_now)
            ):
                nearest = self.nodes[best_near_id]
                anchor = nearest.anchor
                edge = nearest.edge_from_parent
                repair_support = edge is not None and edge.edge_kind in {"stair_down", "stair_up"}
                adjacent_ascent = anchor[1] == cur_pos[1] + 1 and best_near_d <= math.sqrt(2)
                stepped_up = adjacent_ascent and (
                    self._hop_up_to(anchor, repair_support=repair_support)
                    or (best_near_d == 1.0 and self._pillar_to_recorded_step(anchor, deadline=deadline, min_hp=min_hp_ref[0]))
                )
                if not stepped_up and not self._move_exact(anchor):
                    directly_above = cur_pos == (anchor[0], anchor[1] + 1, anchor[2])
                    if not directly_above or not self._drop_to_recorded_step(anchor, min_hp=min_hp_ref[0]):
                        return False, "cannot_reach_nearest_node"
                self.active_node_id = best_near_id
            elif (
                self.collar_node_id in self.nodes and self.nodes[self.collar_node_id].anchor[1] >= min_rejoin_y
                and math.hypot(
                    cur_pos[0] - self.nodes[self.collar_node_id].anchor[0],
                    cur_pos[2] - self.nodes[self.collar_node_id].anchor[2],
                ) <= 48.0
            ):
                collar_anc = self.nodes[self.collar_node_id].anchor
                if allow_lower_rejoin:
                    try:
                        self.bot.call(
                            "move_to",
                            {
                                "x": collar_anc[0] + 0.5,
                                "z": collar_anc[2] + 0.5,
                                "range": 1.4,
                                "timeout": 9000,
                                "stop_on": ["hurt", "death"],
                            },
                        )
                    except ApiError:
                        pass
                if not self._move_exact(collar_anc):
                    return False, "cannot_reach_collar"
                self.active_node_id = self.collar_node_id
            elif anchor_now[1] < min_rejoin_y or not self._move_exact(anchor_now):
                return False, "cannot_reach_anchor"

        if self.active_node_id == goal_node_id:
            return True, None

        segments = self._find_node_path(self.active_node_id, goal_node_id)
        for waypoints, rev, dest_id in segments:
            if not waypoints:
                return False, "no_recorded_route"
            route = list(reversed(waypoints)) if rev else list(waypoints)
            anchor = self.nodes[dest_id].anchor
            if route[-1] != anchor:
                route.append(anchor)
            ok, err = self._walk_waypoints(
                route, min_hp_ref=min_hp_ref, deadline=deadline, record_climb=not allow_lower_rejoin,
            )
            if not ok:
                return False, err
            self.active_node_id = dest_id
        if self.active_node_id != goal_node_id:
            return False, "no_recorded_route"
        return True, None

    def _walk_to_collar(
        self, collar_id: int, *, min_hp_ref: list[float], deadline: float | None = None,
        allow_lower_rejoin: bool = True,
    ) -> tuple[bool, str | None]:
        mining_node_id = self.active_node_id
        try:
            return self._navigate_to_node(
                collar_id, min_hp_ref=min_hp_ref, deadline=deadline, allow_lower_rejoin=allow_lower_rejoin,
            )
        finally:
            self.active_node_id = mining_node_id

    def _resume_shaft_tip(self, goal_node_id: int, *, min_hp_ref: list[float]) -> tuple[bool, str | None]:
        try:
            ok, reason = self._navigate_to_node(goal_node_id, min_hp_ref=min_hp_ref)
            if reason == "walk_time_slice":
                raise _MiningDeadline
            return ok, reason
        except _MiningDeadline:
            # Navigation may have updated the active node at an intermediate anchor.
            # Keep the mining destination so the next slice continues along the route.
            self.active_node_id = goal_node_id
            raise

    def _should_resume_at_depth(self, node: ShaftNode, pos: tuple[int, int, int]) -> bool:
        collar = self.nodes[self.collar_node_id]
        ceiling = collar.anchor[1]
        if self.collar_ceiling_y is not None:
            ceiling = max(ceiling, self.collar_ceiling_y)
        return (
            self.target_y - SCAN_BAND <= pos[1] <= ceiling - 20
            and node.anchor[1] > max(pos[1], self.target_y) + DEPTH_RESET_THRESHOLD
        )

    def _resume_explore_at_depth(
        self, node: ShaftNode, pos: tuple[int, int, int], *, min_hp_ref: list[float],
    ) -> bool:
        if self._is_recorded_shaft_position(pos):
            return self._resume_deepest_branch(node, min_hp_ref=min_hp_ref, max_y=pos[1])
        if self._resume_local_branch(node.node_id, min_hp_ref=min_hp_ref):
            return True
        if pos[1] <= self.target_y:
            return False

        # A cutoff can leave the bot below the last saved waypoint. Start a
        # new branch here only after verifying dry footing and an exit direction.
        geofence = self._active_geofence
        branches = self._probe_dry_descent(
            pos, directions=tuple(DIRECTIONS), geofence=geofence, min_hp_ref=min_hp_ref,
        )
        if not branches:
            return False
        self._archive_current_shaft_to_ledger(mark_exhausted=False)
        self._clear_active_tree_only()
        self._init_collar(
            pos, target_y=self.target_y, geofence=geofence,
            viable_dirs=[branch.direction for branch in branches],
        )
        return True

    def _recover_failed_shaft_rejoin(
        self, node: ShaftNode, pos: tuple[int, int, int], reason: str | None, *, min_hp_ref: list[float],
    ) -> bool:
        if (
            reason != "cannot_reach_nearest_node"
            or not self.target_y + SPINE_DESCEND_MIN_DELTA < pos[1] < node.anchor[1]
            or not self._active_deep_branch_pending()
        ):
            return False
        current = _floor_pos(self.bot.status()["pos"])
        return current[1] <= pos[1] and self._resume_explore_at_depth(node, current, min_hp_ref=min_hp_ref)

    @staticmethod
    def _ahead_of_pending_descent(node: ShaftNode, pos: tuple[int, int, int]) -> bool:
        dx, dy, dz = (p - a for p, a in zip(pos, node.anchor))
        distance = abs(dx) + abs(dz)
        return distance > 0 and abs(distance + dy) <= 1 and any(
            branch.kind == "stair_down" and branch.status == "pending"
            and (dx, dz) == tuple(distance * component for component in DIRECTIONS[branch.direction])
            for branch in node.branches
        )

    def _resume_deepest_branch(
        self, node: ShaftNode, *, min_hp_ref: list[float], descendants_only: bool = False,
        max_y: int | None = None, blocked_branches: set[int] | None = None,
    ) -> bool:
        ceiling = node.anchor[1] if max_y is None else min(max_y, node.anchor[1])
        candidates = []
        for candidate in self.nodes.values():
            if candidate.node_id == node.node_id or candidate.anchor[1] > ceiling:
                continue
            if not any(
                branch.status == "pending" and (blocked_branches is None or id(branch) not in blocked_branches)
                for branch in candidate.branches
            ):
                continue
            if descendants_only:
                route = self._find_node_path(node.node_id, candidate.node_id)
                if not route or any(reverse for _, reverse, _ in route):
                    continue
            candidates.append(candidate)
        for candidate in sorted(candidates, key=lambda candidate: candidate.anchor[1]):
            reached, reason = self._resume_shaft_tip(candidate.node_id, min_hp_ref=min_hp_ref)
            if reached:
                return True
            self.active_node_id = node.node_id
            if reason in {"hurt", "not_ready", "not_grounded"}:
                break
        return False

    def _utility_blocks_backtrack(self, reason: str) -> bool:
        return reason.startswith("backtrack_blocked@") and any(
            f"feet={block}," in reason or f"head={block}," in reason for block in UTILITY_BLOCKS
        )

    def _resume_local_branch(self, blocked_node_id: int, *, min_hp_ref: list[float]) -> bool:
        stance = self.bot.status()
        if (
            stance.get("alive") is False or stance.get("ready") is False
            or not stance.get("on_ground", True) or stance.get("in_water") or stance.get("in_lava")
            or stance.get("health", min_hp_ref[0]) < min_hp_ref[0]
        ):
            return False
        current = _floor_pos(stance["pos"])
        candidates = sorted(self.nodes.values(), key=lambda node: math.dist(current, node.anchor))
        for candidate in candidates:
            if candidate.node_id == blocked_node_id or not any(b.status == "pending" for b in candidate.branches):
                continue
            try:
                if not self._level_walk_is_clear(current, candidate.anchor):
                    continue
                if current == candidate.anchor or self._move_short_distance(
                    candidate.anchor, deadline=self._deadline or math.inf,
                ):
                    settled, on_ground = self._settled_position()
                    if on_ground and _floor_pos(settled) == candidate.anchor:
                        self.active_node_id = candidate.node_id
                        return True
            except ApiError as err:
                if err.code in {"hurt", "death", "interrupted", "dead", "disconnected"}:
                    return False
            # Try another candidate only from a newly verified stance on this level.
            stance = self.bot.status()
            if not stance.get("on_ground", True) or _floor_pos(stance["pos"]) != current:
                return False
        return False

    # ---- Long-Range 64-Block Cluster Radar & Hostile Threat Triage ('radar' strategy) ----

    def _cluster_ore_points(
        self,
        anchor: tuple[int, int, int],
        pts: list[tuple[int, int, int]],
        hostiles: list[tuple[float, float, float]],
    ) -> list[dict[str, Any]]:
        if not pts:
            return []
        remaining = set(pts)
        clusters: list[dict[str, Any]] = []
        while remaining:
            seed = next(iter(remaining))
            remaining.remove(seed)
            comp = [seed]
            q: deque[tuple[int, int, int]] = deque([seed])
            while q:
                cur = q.popleft()
                neighbors = [
                    p for p in list(remaining)
                    if abs(p[0] - cur[0]) + abs(p[1] - cur[1]) + abs(p[2] - cur[2]) <= 2
                ]
                for nb in neighbors:
                    remaining.remove(nb)
                    comp.append(nb)
                    q.append(nb)
            rep = min(comp, key=lambda p: math.dist(anchor, p))
            dist_a = math.dist(anchor, rep)
            min_h = min(
                (math.dist((rep[0] + 0.5, rep[1] + 0.5, rep[2] + 0.5), hp) for hp in hostiles),
                default=999.0,
            )
            mob_nearby = min_h <= 9.0
            # Safe mob-free clusters always outrank mob-guarded caves; rich close veins rank highest
            eff_cost = dist_a / (1.0 + 0.35 * min(len(comp), 6))
            clusters.append(
                {
                    "rep": rep,
                    "members": comp,
                    "size": len(comp),
                    "dist": round(dist_a, 2),
                    "min_hostile_dist": round(min_h, 2),
                    "mob_nearby": mob_nearby,
                    "rank_key": (1 if mob_nearby else 0, eff_cost),
                }
            )
        clusters.sort(key=lambda c: c["rank_key"])
        return clusters

    def _scan_ore_radar(
        self,
        anchor: tuple[int, int, int],
        *,
        target_blocks: tuple[str, ...],
        max_tier: int,
        geofence: Any,
        force: bool = False,
    ) -> list[tuple[int, int, int]]:
        """64-block radar ('fishbone': off; 'fishbone_scan': read-only in target band; 'v5': nearest-point; 'radar': cluster + triage)."""
        _scan_band = self.strategy == "fishbone_scan" and abs(anchor[1] - self.target_y) <= SCAN_BAND
        if not self.radar_enabled and not _scan_band:
            self.radar_targets = []
            self.locked_cluster = None
            return []

        self.radar_targets = [
            pt for pt in self.radar_targets
            if pt not in self.mined_ore_positions
            and not self._is_ore_unreachable(pt)
            and not self.skills._is_block_avoided(pt)
        ]
        recheck_dist = 8.0 if self.surgical_enabled else 10.0
        if (
            not force
            and self._last_radar_anchor is not None
            and math.dist(anchor, self._last_radar_anchor) < recheck_dist
            and self.radar_targets
        ):
            if not self.surgical_enabled:
                self.locked_cluster = None
                self.radar_targets.sort(key=lambda p: math.dist(anchor, p))
                return self.radar_targets
            hostiles_cached = self._scan_hostile_positions(radius=24)
            clusters = self._cluster_ore_points(anchor, self.radar_targets, hostiles_cached)
            self.locked_cluster = clusters[0] if clusters else None
            self.radar_targets = [c["rep"] for c in clusters] + [
                p for c in clusters for p in c["members"] if p != c["rep"]
            ]
            return self.radar_targets

        self._last_radar_anchor = anchor
        target_set = set(target_blocks)
        found_pts: list[tuple[int, int, int]] = []
        seen: set[tuple[int, int, int]] = set()
        scan_limit = 60 if self.surgical_enabled else 32

        try:
            res = self.bot.call(
                "find_blocks",
                {"radius": RADAR_SCAN_RADIUS, "types": list(target_blocks), "limit": scan_limit},
            )
            blocks = res.get("blocks") or []
        except ApiError:
            blocks = []

        nearest_nid = self.active_node_id or self.collar_node_id or 1
        for blk in blocks:
            bname = str(blk.get("name") or "").removeprefix("minecraft:")
            bpos = blk.get("pos")
            if bname not in target_set or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            if (
                pt in seen
                or pt in self.mined_ore_positions
                or self._is_ore_unreachable(pt)
                or self.skills._is_block_avoided(pt)
            ):
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="ore_radar") is not None:
                continue
            seen.add(pt)
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
            self.ledger.record_ore(pt, bname, req_t)
            if req_t > max_tier:
                if pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(pt, bname, req_t, nearest_nid)
                continue
            self.ever_seen_radar_ores.add(pt)
            found_pts.append(pt)

        for pt, meta in list(self.ledger.discovered_ores.items()):
            if (
                pt in seen
                or pt in self.mined_ore_positions
                or self._is_ore_unreachable(pt)
                or self.skills._is_block_avoided(pt)
            ):
                continue
            if meta.get("block") not in target_set or int(meta.get("required_tier", 2)) > max_tier:
                continue
            if math.dist(anchor, pt) > RADAR_SCAN_RADIUS + 4:
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="ore_radar") is not None:
                continue
            if meta.get("from_disk"):
                try:
                    live_name = self._get_block_name(pt)
                except ApiError:
                    continue
                if live_name != meta.get("block"):
                    self.ledger.remove_ore(pt)
                    continue
                meta["from_disk"] = False
            seen.add(pt)
            self.ever_seen_radar_ores.add(pt)
            found_pts.append(pt)

        if self.strategy in {"radar", "v5"} and found_pts:
            # Never lock onto bedrock-embedded ores (Overworld bedrock spans Y=-64..-60);
            # diving into Y<=-60 triggers repeated `shaft_exhausted` stalls.
            if self.target_y < 0:
                above_bedrock = [p for p in found_pts if p[1] >= -59]
                if above_bedrock:
                    found_pts = above_bedrock
            in_band = [p for p in found_pts if abs(p[1] - self.target_y) <= 8]
            if in_band:
                found_pts = in_band
            else:
                near_band = [p for p in found_pts if abs(p[1] - self.target_y) <= 10]
                if near_band:
                    found_pts = near_band

        if not self.surgical_enabled:
            # 'v5' mode: original v4 nearest single-point radar ordering (no hostile cluster triage)
            self.locked_cluster = None
            found_pts.sort(key=lambda p: (abs(p[1] - self.target_y) // 12, math.dist(anchor, p)))
            self.radar_targets = found_pts[:24]
            return self.radar_targets

        hostiles = self._scan_hostile_positions(radius=32)
        clusters = self._cluster_ore_points(anchor, found_pts, hostiles)
        self.locked_cluster = clusters[0] if clusters else None
        ordered_pts = [c["rep"] for c in clusters] + [
            p for c in clusters for p in c["members"] if p != c["rep"]
        ]
        self.radar_targets = ordered_pts[:32]
        return self.radar_targets

    def _best_radar_target_for_anchor(
        self,
        anchor: tuple[int, int, int],
        *,
        at_spine: bool = False,
    ) -> tuple[int, int, int] | None:
        if not self.radar_enabled:
            return None
        if self.surgical_enabled and self.locked_cluster is not None:
            rep = tuple(self.locked_cluster["rep"])
            if (
                rep not in self.mined_ore_positions
                and not self._is_ore_unreachable(rep)
                and not self.skills._is_block_avoided(rep)
            ):
                return rep
        valid = [
            pt for pt in self.radar_targets
            if pt not in self.mined_ore_positions
            and not self._is_ore_unreachable(pt)
            and not self.skills._is_block_avoided(pt)
        ]
        if not valid:
            return None
        band_tol = 16 if self.surgical_enabled else 10
        if at_spine:
            same_band = [pt for pt in valid if abs(pt[1] - anchor[1]) <= band_tol]
            if same_band:
                return min(same_band, key=lambda p: math.dist(anchor, p))
        return min(valid, key=lambda p: math.dist(anchor, p))

    def _radar_direction_projection(
        self,
        anchor: tuple[int, int, int],
        direction: str,
        *,
        at_spine: bool = False,
    ) -> float:
        rt = self._best_radar_target_for_anchor(anchor, at_spine=at_spine)
        if rt is None:
            return 0.0
        dx, dz = DIRECTIONS[direction]
        vx, vz = rt[0] - anchor[0], rt[2] - anchor[2]
        return float(vx * dx + vz * dz)

    # ---- Canopy Escape, Hitbox Ledge Snap & Solid Collar Site Selection ----

    def _probe_collar_dirs(
        self,
        anchor: tuple[int, int, int],
        *,
        geofence: Any,
        cache: dict[tuple[int, int, int], str] | None = None,
        counts: dict[str, int] | None = None,
        directions: tuple[str, ...] = ("north", "east", "south", "west"),
        dry_only: bool = False,
        blockers: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        blk_cache = cache if cache is not None else {}
        inv_counts = counts if counts is not None else _inventory_counts(self.bot)
        has_seal = _seal_reserve(inv_counts) >= 1
        canonical = {"north": 0, "east": 1, "south": 2, "west": 3}
        scored: list[tuple[int, float, int, str]] = []
        effective_y = self._effective_collar_target_y(anchor)
        at_target_layer = self._collar_starts_at_level(anchor, effective_y)
        step_y = anchor[1] if at_target_layer else anchor[1] - 1

        for d in directions:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_collar") is not None:
                if blockers is not None:
                    blockers[d] = {"code": "void_geofence_blocked"}
                continue
            nx, nz = anchor[0] + dx, anchor[2] + dz
            # At the target layer, the block below the face is the floor.
            # Probing it as a dig cell rejects walkable bedrock and probes a
            # descent that this collar will never actually take.
            dig_cells = [(nx, y, nz) for y in range(anchor[1] + 1, step_y - 1, -1)]
            try:
                hazard, adj_fluids = self._inspect_step_hazards(dig_cells, blk_cache, forward_delta=(dx, dz))
                if hazard is not None:
                    if blockers is not None:
                        code, details = hazard
                        blockers[d] = {"code": code, **details}
                    continue
                if adj_fluids and (dry_only or not has_seal and any(blk_cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids)):
                    if blockers is not None:
                        fluid = adj_fluids[0] if dry_only else next(nb for nb in adj_fluids if blk_cache[nb] in LAVA_BLOCKS)
                        blockers[d] = {"code": "adjacent_fluid", "pos": list(fluid), "block": blk_cache[fluid]}
                    continue
                support_pt = (nx, step_y - 1, nz)
                support_blk = self._get_block_name(support_pt, blk_cache)
                if (
                    support_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS
                    or (support_blk == "bedrock" and not at_target_layer and step_y > effective_y)
                ):
                    if blockers is not None:
                        blockers[d] = {"code": "blocked_floor", "pos": list(support_pt), "block": support_blk}
                    continue
                support_ok = self._is_solid_floor(support_pt, blk_cache)
                if not support_ok:
                    can_bridge = has_seal and (
                        self._is_self_cavity([support_pt, *dig_cells])
                        or self._is_solid_floor((nx, step_y - 2, nz), blk_cache)
                    )
                    if not can_bridge:
                        if blockers is not None:
                            blocker = {"code": "missing_support", "pos": list(support_pt), "block": support_blk}
                            level_floor = (nx, anchor[1] - 1, nz)
                            if (
                                not at_target_layer and not adj_fluids
                                and blk_cache[(nx, anchor[1], nz)] in AIR_BLOCKS
                                and blk_cache[(nx, anchor[1] + 1, nz)] in AIR_BLOCKS
                                and blk_cache[level_floor] not in FALLING_BLOCKS
                                and self._is_solid_floor(level_floor, blk_cache)
                            ):
                                # The descent can lack support one block below a walkable ledge.
                                blocker["safe_side_step"] = [nx + 0.5, anchor[1], nz + 0.5]
                            blockers[d] = blocker
                        continue
            except ApiError as exc:
                if blockers is not None:
                    blockers[d] = {"code": "collar_probe_failed", "error": exc.code}
                continue

            proj = self._radar_direction_projection(anchor, d, at_spine=False)
            fluid_penalty = 1 if adj_fluids else 0
            radar_rank = -proj if proj > 1.0 else 0.0
            scored.append((fluid_penalty, radar_rank, canonical[d], d))

        scored.sort(key=lambda r: (r[0], r[1], r[2]))
        return [r[3] for r in scored]

    def _snap_from_hitbox_ledge_if_needed(
        self,
        pos: tuple[int, int, int],
        *,
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[int, int, int]:
        """Fix underground hitbox-edge false 'no_solid_collar_ground' when standing across block boundary."""
        for dy in (0, -1):
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                cand = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                if self._geofence_blocked(geofence, cand[0], cand[2], action="collar_snap") is not None:
                    continue
                try:
                    if not self._is_solid_floor((cand[0], cand[1] - 1, cand[2]), cache):
                        continue
                    if self._get_block_name(cand, cache) not in PASSABLE_BLOCKS:
                        continue
                    if self._get_block_name((cand[0], cand[1] + 1, cand[2]), cache) not in PASSABLE_BLOCKS:
                        continue
                except ApiError:
                    continue
                if self._move_exact(cand, timeout_ms=2000):
                    return cand
        return pos

    def _descend_tree_canopy_if_needed(self, cur_pos: tuple[int, int, int]) -> tuple[int, int, int]:
        """Remove canopy leaves only when there is a confirmed landing within three blocks."""
        cache: dict[tuple[int, int, int], str] = {}
        pos = cur_pos
        for _ in range(10):
            under_pt = (pos[0], pos[1] - 1, pos[2])
            try:
                if not self._get_block_name(under_pt).endswith("_leaves"):
                    break
                for drop in range(1, 4):
                    floor = (under_pt[0], under_pt[1] - drop, under_pt[2])
                    block = self._get_block_name(floor, cache)
                    if block in PASSABLE_BLOCKS:
                        continue
                    if not (block.endswith("_leaves") or self._is_solid_floor(floor, cache)):
                        return pos
                    break
                else:
                    break
            except ApiError:
                break
            try:
                self.bot.call("dig", {"pos": list(under_pt), "collect": False, "stop_on": ["hurt", "death"]})
            except ApiError:
                break
            settled, grounded = self._settled_position()
            pos = _floor_pos(settled)
            if not grounded or pos != (floor[0], floor[1] + 1, floor[2]):
                break
        return pos

    def _clear_lateral_leaves_toward(self, stand: tuple[int, int, int]) -> None:
        """Clear blocking *_leaves laterally when walking off a dense tree branch onto nearby solid ground."""
        for _ in range(6):
            st = self.bot.status()
            cur = _floor_pos(st["pos"])
            vx, vz = stand[0] - cur[0], stand[2] - cur[2]
            if vx == 0 and vz == 0:
                return
            sx = (1 if vx > 0 else -1) if abs(vx) >= abs(vz) else 0
            sz = (1 if vz > 0 else -1) if sx == 0 else 0
            cleared_leaf = False
            for dy in (1, 0, -1):
                lp = (cur[0] + sx, cur[1] + dy, cur[2] + sz)
                try:
                    bn = self._get_block_name(lp)
                except ApiError:
                    continue
                if bn.endswith("_leaves"):
                    try:
                        self.bot.call("dig", {"pos": [lp[0], lp[1], lp[2]], "collect": False, "stop_on": ["hurt", "death"]})
                        cleared_leaf = True
                    except ApiError:
                        pass
            if not cleared_leaf:
                return
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": (cur[0] + sx) + 0.5,
                        "z": (cur[2] + sz) + 0.5,
                        "range": 1.0,
                        "timeout": 2500,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                pass

    def _find_solid_collar_candidates(
        self,
        origin: tuple[int, int, int],
        *,
        geofence: Any,
        avoid_exhausted: bool,
        radius: int = 20,
    ) -> list[tuple[int, int, int]]:
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": min(64, max(8, radius)), "types": list(SURFACE_GROUND_BLOCKS), "limit": 48},
            )
            blocks = found.get("blocks") or []
        except ApiError:
            blocks = []

        cache: dict[tuple[int, int, int], str] = {}
        counts = _inventory_counts(self.bot)
        scored: list[tuple[int, float, tuple[int, int, int]]] = []
        seen: set[tuple[int, int, int]] = set()

        raw_coords = [
            (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2]))
            for b in blocks
            if isinstance(b.get("pos"), (list, tuple)) and len(b["pos"]) >= 3
        ]
        # Underground supplement: probe local floor neighborhood directly so ceiling blocks don't starve limit=48
        if origin[1] < 55 and not avoid_exhausted:
            for dx in range(-4, 5, 2):
                for dz in range(-4, 5, 2):
                    for dy in (-2, -1, 0):
                        raw_coords.append((origin[0] + dx, origin[1] + dy, origin[2] + dz))

        for gx, gy, gz in raw_coords:
            stand = (gx, gy + 1, gz)
            if stand in seen:
                continue
            seen.add(stand)
            if self._geofence_blocked(geofence, gx, gz, action="collar_seek") is not None:
                continue
            if avoid_exhausted and self._is_near_exhausted_collar(stand, min_dist=EXHAUSTED_COLLAR_MIN_SEP):
                continue
            hdist = math.hypot(stand[0] - origin[0], stand[2] - origin[2])
            if avoid_exhausted and hdist < 10.0:
                continue
            try:
                if not self._is_solid_floor((gx, gy, gz), cache):
                    continue
                if self._get_block_name(stand, cache) not in PASSABLE_BLOCKS:
                    continue
                if self._get_block_name((gx, gy + 2, gz), cache) not in PASSABLE_BLOCKS:
                    continue
                dirs = self._probe_collar_dirs(stand, geofence=geofence, cache=cache, counts=counts)
            except ApiError:
                continue
            if not dirs:
                continue
            dir_tier = 0 if len(dirs) >= 2 else 1
            scored.append((dir_tier, math.dist(origin, stand), stand))

        scored.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in scored[:8]]

    def _skip_stalled_first_dig(self, *, deadline: float, min_hp: float) -> bool:
        scoped = self._budget_session
        if (
            scoped is None or scoped.dug_count or scoped.timeout_path != "dig"
            or scoped.timeout_dig_pos is None or scoped.timeout_dig_read_s < 1.0
            or min(deadline, scoped.deadline) - time.monotonic() <= DIG_RECOVERY_RESERVE_S
            or not scoped.release_timed_out_action()
        ):
            return False
        before = scoped.last_status
        try:
            stance = self.bot.status()
        except ApiError:
            return False
        hp = stance.get("health")
        if (
            stance.get("alive") is not True or stance.get("ready") is not True
            or stance.get("on_ground") is not True or stance.get("in_water") or stance.get("in_lava")
            or not isinstance(hp, (int, float)) or hp < min_hp
            or (stance.get("dimension"), stance.get("last_death")) != (before.get("dimension"), before.get("last_death"))
            or not before.get("pos") or not stance.get("pos") or _floor_pos(stance["pos"]) != _floor_pos(before["pos"])
        ):
            return False
        # The dig might have changed the cell before its reply was lost. A different heading
        # can be explored after release without replaying that mutation.
        self.skills._avoid_temporarily(scoped.timeout_dig_pos, "dig_timeout", DIG_TIMEOUT_COOLDOWN_S)
        scoped.timeout_path = None
        scoped.timeout_dig_pos = None
        scoped.timeout_dig_read_s = 0.0
        return True

    def _relocate_to_fresh_collar_site(
        self,
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any = None,
        deadline: float | None = None,
        min_hp_ref: list[float] | None = None,
    ) -> tuple[int, int, int]:
        cands = self._find_solid_collar_candidates(
            cur_pos,
            geofence=geofence,
            avoid_exhausted=True,
            radius=28,
        )
        for stand in cands[:5]:
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": stand[0] + 0.5,
                        "z": stand[2] + 0.5,
                        "range": 1.2,
                        "timeout": 8000,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                pass
            self.bot.status()
            self._move_exact(stand, timeout_ms=2500)
            st = self.bot.status()
            now_pos = _floor_pos(st["pos"])
            if not self._is_near_exhausted_collar(now_pos, min_dist=10.0):
                if self._probe_collar_dirs(now_pos, geofence=geofence):
                    self.relocations_count += 1
                    return now_pos

        ex_ref = self.exhausted_collars[-1] if self.exhausted_collars else cur_pos
        dir_order = sorted(
            ("north", "east", "south", "west"),
            key=lambda d: -(
                (cur_pos[0] + DIRECTIONS[d][0] * 16 - ex_ref[0]) ** 2
                + (cur_pos[2] + DIRECTIONS[d][1] * 16 - ex_ref[2]) ** 2
            ),
        )
        hp_ref = min_hp_ref if min_hp_ref is not None else [20.0]
        dl = deadline if deadline is not None else (time.monotonic() + 22.0)
        turned_after_timeout = False
        for d in dir_order:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, cur_pos[0] + dx * 16, cur_pos[2] + dz * 16, action="collar_relocate_drift") is not None:
                continue
            try:
                drift = self._dig_level_edge(
                    direction=d,
                    steps=16,
                    target_blocks=(),
                    parent_node_id=0,
                    geofence=geofence,
                    deadline=dl,
                    min_hp_ref=hp_ref,
                    min_target_tier=1,
                )
            except _MiningDeadline:
                if turned_after_timeout or not self._skip_stalled_first_dig(deadline=dl, min_hp=hp_ref[0]):
                    raise
                turned_after_timeout = True
                continue
            if int(drift["steps_done"]) >= 4:
                self.relocations_count += 1
                return tuple(drift["end_anchor"])

        return cur_pos

    def _ensure_solid_collar_anchor(
        self,
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
    ) -> tuple[tuple[int, int, int], list[str], dict[str, Any] | None]:
        cache: dict[tuple[int, int, int], str] = {}
        pos = cur_pos
        near_ex = self._is_near_exhausted_collar(pos)
        under_pt = (pos[0], pos[1] - 1, pos[2])
        try:
            on_solid = self._is_solid_floor(under_pt, cache)
        except ApiError:
            on_solid = False

        if not on_solid and not near_ex:
            snapped = self._snap_from_hitbox_ledge_if_needed(pos, geofence=geofence, cache=cache)
            if snapped != pos:
                pos = snapped
                on_solid = True

        if on_solid and not near_ex:
            dirs0 = self._probe_collar_dirs(pos, geofence=geofence, cache=cache)
            if dirs0:
                return pos, dirs0, None

        pos = self._descend_tree_canopy_if_needed(pos)
        cache.clear()
        near_ex = self._is_near_exhausted_collar(pos)
        try:
            on_solid = self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), cache)
        except ApiError:
            on_solid = False
        if on_solid and not near_ex:
            dirs1 = self._probe_collar_dirs(pos, geofence=geofence, cache=cache)
            if dirs1:
                return pos, dirs1, None

        if near_ex:
            pos = self._relocate_to_fresh_collar_site(
                pos,
                geofence=geofence,
                deadline=deadline,
                min_hp_ref=min_hp_ref,
            )
            dirs_rel = self._probe_collar_dirs(pos, geofence=geofence)
            if dirs_rel and self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {}):
                return pos, dirs_rel, None

        cands = self._find_solid_collar_candidates(
            pos,
            geofence=geofence,
            avoid_exhausted=near_ex,
            radius=20,
        )
        for stand in cands[:5]:
            if time.monotonic() >= deadline:
                break
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": stand[0] + 0.5,
                        "z": stand[2] + 0.5,
                        "range": 1.2,
                        "timeout": 7000,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                self._clear_lateral_leaves_toward(stand)
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": stand[0] + 0.5,
                            "z": stand[2] + 0.5,
                            "range": 1.2,
                            "timeout": 5000,
                            "stop_on": ["hurt", "death"],
                        },
                    )
                except ApiError:
                    pass
            self.bot.status()
            self._move_exact(stand, timeout_ms=2200)
            st = self.bot.status()
            pos = _floor_pos(st["pos"])
            if self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {}):
                dirs_c = self._probe_collar_dirs(pos, geofence=geofence)
                if dirs_c:
                    return pos, dirs_c, None

        try:
            still_solid = self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {})
        except ApiError:
            still_solid = False
        if not still_solid:
            return pos, [], {"stop_reason": "no_solid_collar_ground"}

        blockers: dict[str, dict[str, Any]] = {}
        dirs_final = self._probe_collar_dirs(pos, geofence=geofence, blockers=blockers)
        if dirs_final:
            return pos, dirs_final, None
        return pos, [], {"stop_reason": "no_viable_collar_direction", "collar_blockers": blockers}

    def _try_restore_saved_shaft(
        self,
        cur_pos: tuple[int, int, int],
        *,
        min_hp_ref: list[float],
        allow_retired: bool = False,
        skip_collar: tuple[int, int, int] | None = None,
    ) -> bool:
        """Restore a nearby shaft. A blocked deep rejoin leaves its graph loaded."""
        best_sh: dict[str, Any] | None = None
        best_d = 36.0
        for sh in self.ledger.saved_shafts:
            anc_raw = sh.get("collar_anchor")
            if not isinstance(anc_raw, list) or len(anc_raw) < 3:
                continue
            anc = (int(anc_raw[0]), int(anc_raw[1]), int(anc_raw[2]))
            wet_shaft = self._water_interrupted_shaft
            if (
                anc == skip_collar
                or (not allow_retired and (
                    sh.get("retired") or anc in self.exhausted_collars
                    or (wet_shaft is not None and anc == wet_shaft.collar)
                ))
            ):
                continue
            if allow_retired and anc[1] <= cur_pos[1]:
                continue
            # Pending stairs still carry their original branch target layer.
            saved_target_y = sh.get("target_y")
            if not allow_retired and saved_target_y is not None and abs(int(saved_target_y) - self.target_y) > SCAN_BAND:
                continue
            hd = math.hypot(cur_pos[0] - anc[0], cur_pos[2] - anc[2])
            vertical_near = abs(cur_pos[1] - anc[1]) <= 20
            if allow_retired or not vertical_near or hd > 36.0:
                nodes = sh.get("nodes", [])
                edges = [
                    (edge.get("edge_kind"), [tuple(wp) for wp in edge.get("waypoints", [])])
                    for n in nodes if (edge := n.get("edge_from_parent"))
                ]
                points = [tuple(n["anchor"]) for n in nodes if n.get("anchor")]
                points.extend(wp for _, waypoints in edges for wp in waypoints)
                for n in nodes:
                    for branch in n.get("branches", []):
                        waypoints = branch.get("completed_waypoints") or []
                        if (branch.get("kind") == "branch_leaf" and branch.get("status") == "completed"
                                and waypoints and waypoints[0] == n.get("anchor")):
                            points.extend(tuple(wp) for wp in waypoints)
                if allow_retired and points:
                    hd = min(math.dist(cur_pos, pt) for pt in points)
                    vertical_near = hd <= 6.0
                elif cur_pos in points or any(
                    self._route_at_position(waypoints, cur_pos, kind) is not None for kind, waypoints in edges
                ):
                    hd = 0.0
                    vertical_near = True
            if hd <= best_d and vertical_near:
                best_d = hd
                best_sh = sh

        if best_sh is None:
            return False

        raw_nodes = best_sh.get("nodes") or []
        if not raw_nodes:
            return False
        restored: dict[int, ShaftNode] = {}
        for rn in raw_nodes:
            if isinstance(rn, dict):
                nd = ShaftNode.from_dict(rn)
                restored[nd.node_id] = nd
        collar_id = int(best_sh.get("collar_node_id", 1))
        if collar_id not in restored:
            return False

        if best_sh.get("from_disk"):
            collar_anc = restored[collar_id].anchor
            sample_wp: tuple[int, int, int] | None = None
            for nd in restored.values():
                if nd.edge_from_parent:
                    for wp in nd.edge_from_parent.waypoints:
                        if wp != collar_anc:
                            sample_wp = wp
                            break
                if sample_wp is not None:
                    break
            if sample_wp is not None:
                try:
                    if self._get_block_name(sample_wp) not in PASSABLE_BLOCKS:
                        self.ledger.saved_shafts = [s for s in self.ledger.saved_shafts if s is not best_sh]
                        return False
                except ApiError:
                    return False

        self.nodes = restored
        self.collar_node_id = collar_id
        # Restoring a saved shaft is a fresh attempt: don't inherit a hazard streak that would
        # make the next explore bail out as shaft_exhausted on its first branch.
        self.fluid_aborts = 0
        self.fluid_abandoned_deep = False
        if self.collar_ceiling_y is None or restored[collar_id].anchor[1] > self.collar_ceiling_y:
            self.collar_ceiling_y = int(restored[collar_id].anchor[1])
        self.next_node_id = max(restored.keys(), default=1) + 1
        active_id = int(best_sh.get("active_node_id") or collar_id)
        self.active_node_id = active_id if active_id in restored else collar_id
        if not allow_retired and not self._tree_has_pending_branches():
            self._clear_active_tree_only()
            return False
        if allow_retired:
            # A return trip only needs the route. Leave saved ores for explore
            # to verify when it restores a mining shaft.
            return True

        for o_pt, meta in list(self.ledger.discovered_ores.items()):
            if o_pt in self.mined_ore_positions or o_pt in self.deferred_ores:
                continue
            bname = str(meta.get("block") or "")
            if not bname:
                continue
            if meta.get("from_disk"):
                try:
                    if self._get_block_name(o_pt) != bname:
                        self.ledger.remove_ore(o_pt)
                        continue
                    meta["from_disk"] = False
                except ApiError:
                    continue
            near_nid = min(self.nodes.keys(), key=lambda nid: math.dist(o_pt, self.nodes[nid].anchor))
            self.deferred_ores[o_pt] = DeferredOreRecord(
                pos=o_pt,
                block_name=bname,
                required_tier=int(meta.get("required_tier", 2)),
                nearest_node_id=near_nid,
            )

        active = self.nodes[self.active_node_id]
        if self._has_stale_descent(self.target_y, observed_layers={point[1] for point in self.radar_targets}):
            return self._replan_shallower_layer(
                self.bot.status(), cur_pos, target_y=self.target_y, geofence=self._active_geofence,
            )
        if self._should_resume_at_depth(active, cur_pos):
            return self._resume_explore_at_depth(active, cur_pos, min_hp_ref=min_hp_ref)
        ok_nav, _ = self._resume_shaft_tip(active.node_id, min_hp_ref=min_hp_ref)
        if not ok_nav:
            self._clear_active_tree_only()
            return False
        return True

    # ---- Verified Ore Drop Collection & Backside 1x1 Surgical Extraction ----

    def _scan_target_drops(self, expected_drops: set[str] | frozenset[str]) -> list[dict[str, Any]]:
        entities = self.bot.call("get_entities", {"radius": 6, "types": ["item"]}).get("entities") or []
        drops = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            label = entity.get("item")
            item = _normalize_drop_name(label)
            if item not in expected_drops or not isinstance(entity.get("id"), int):
                continue
            try:
                position = [float(value) for value in entity["pos"][:3]]
                _floor_pos(position)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            quantity = str(label).rpartition("x")[-1]
            count = max(1, int(quantity)) if quantity.isdecimal() and len(quantity) <= 4 else 1
            drops.append({"id": entity["id"], "item": item, "item_pos": position, "stack_count": count})
        return drops

    def _verify_drop_receipts(
        self,
        drops: list[dict[str, Any]],
        expected_drops: set[str] | frozenset[str],
        item: str,
        gain: int,
    ) -> None:
        remaining_ids = {drop["id"] for drop in self._scan_target_drops(expected_drops)}
        vanished = [drop for drop in drops if drop["item"] == item and drop["id"] not in remaining_ids]
        if not vanished:
            return
        position = self.bot.status()["pos"]
        nearby = [drop for drop in vanished if math.dist(position[:3], drop["item_pos"]) <= 1.5]
        for drop in vanished:
            self.drop_receipts[drop["id"]]["reason"] = "pickup_unconfirmed"
        if sum(drop["stack_count"] for drop in nearby) <= gain:
            for drop in nearby:
                self.drop_receipts[drop["id"]].update(reason="picked_up", picked_count=drop["stack_count"])

    def _level_walk_is_clear(self, start: tuple[int, int, int], end: tuple[int, int, int]) -> bool:
        if start[1] != end[1] or math.dist(start, end) > 4.0:
            return False
        route = _interpolate_cells(start, end)
        cache: dict[tuple[int, int, int], str] = {}

        def standable(cell: tuple[int, int, int]) -> bool:
            x, y, z = cell
            return (
                self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_local_walk") is None
                and self._get_block_name(cell, cache) in PASSABLE_BLOCKS
                and self._get_block_name((x, y + 1, z), cache) in PASSABLE_BLOCKS
                and self._is_solid_floor((x, y - 1, z), cache)
            )

        if not all(standable(cell) for cell in route):
            return False
        for prior, cell in zip(route, route[1:]):
            if prior[0] != cell[0] and prior[2] != cell[2] and not (
                standable((prior[0], cell[1], cell[2])) or standable((cell[0], cell[1], prior[2]))
            ):
                return False
        return True

    def _walk_level_detour(
        self, destination: tuple[int, int, int], *, min_hp: float, deadline: float,
        bypass: tuple[int, int, int] | None = None,
    ) -> tuple[bool, str | None]:
        """Try an open parallel passage one block beside a blocked level route."""
        if deadline - time.monotonic() < 3.0:
            return False, None

        def stance_error(status: dict[str, Any]) -> str | None:
            if not status.get("pos") or status.get("alive") is False or status.get("ready") is False:
                return "not_ready"
            hp = status.get("health")
            if isinstance(hp, (int, float)) and hp < min_hp:
                return "hurt"
            if status.get("on_ground") is not True or status.get("in_water") or status.get("in_lava"):
                return "not_grounded"
            return None

        try:
            stance = self.bot.status()
            if (error := stance_error(stance)) is not None:
                return False, error
            start = _floor_pos(stance["pos"])
            dx, dy, dz = (end - begin for begin, end in zip(start, destination))
            if dy or dx * dz or not 2 <= abs(dx) + abs(dz) <= 4:
                return False, None
            if bypass is not None and bypass not in _interpolate_cells(start, destination)[1:-1]:
                return False, None

            for side in (1, -1):
                ox, oz = (0, side) if dx else (side, 0)
                side_start = (start[0] + ox, start[1], start[2] + oz)
                side_end = (destination[0] + ox, destination[1], destination[2] + oz)
                corners = [start, side_start, side_end, destination]
                if all(self._level_walk_is_clear(a, b) for a, b in zip(corners, corners[1:])):
                    break
            else:
                return False, None
        except ApiError:
            return False, None

        for cell in [*_interpolate_cells(side_start, side_end), destination]:
            try:
                if not self._move_short_distance(cell, deadline=deadline):
                    return False, "movement_not_confirmed"
                stance = self.bot.status()
            except ApiError as exc:
                if exc.code in {"hurt", "death", "interrupted"}:
                    return False, "hurt" if exc.code == "hurt" else "not_ready"
                return False, "movement_not_confirmed"
            if (error := stance_error(stance)) is not None:
                return False, error
            if _floor_pos(stance["pos"]) != cell:
                return False, "movement_not_confirmed"
        return True, None

    def _move_short_distance(self, cell: tuple[int, int, int], *, deadline: float) -> bool:
        timeout = int(min(1.1, deadline - time.monotonic()) * 1000)
        if timeout < 200:
            return False
        x, y, z = cell
        self.bot.call("move_to", {
            "x": x + 0.5, "y": y, "z": z + 0.5, "range": 0.3,
            "timeout": timeout, "stop_on": ["hurt", "death"],
        })
        return True

    def _open_drop_pocket_base(
        self,
        base: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> None:
        x, y, z = base
        if (
            self._get_block_name(base, cache) not in PASSABLE_BLOCKS | FLUID_BLOCKS | UTILITY_BLOCKS | {"bedrock"}
            and self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_pickup") is None
            and self._is_solid_floor((x, y - 1, z), cache)
            and self._adjacent_fluid_or_falling([base], cache) is None
        ):
            self._dig_cell_safely(base, collect_rock=False, parent_node_id=0, cache=cache)

    def _collect_and_verify_target_drop(
        self,
        *,
        expected_drops: set[str] | frozenset[str],
        before_count: int,
        ore_pt: tuple[int, int, int],
        stand_y: int,
        cache: dict[tuple[int, int, int], str],
        can_open_lower: bool = False,
        allow_ore_step: bool = False,
    ) -> bool:
        """Verify inventory gain and check for reachable split stacks before leaving the ore."""
        scoped = self._budget_session
        started = time.monotonic()
        outer_deadline = scoped.deadline if scoped is not None else self._deadline or math.inf
        allowance = min(3.0, scoped.pickup_budget_remaining) if scoped is not None else 3.0
        pickup_deadline = min(outer_deadline, started + allowance)
        collect_until = pickup_deadline - min(0.5, allowance / 5)
        if scoped is not None:
            scoped.deadline = collect_until
        try:
            counts = self._check_target_drop_positions(
                expected_drops=expected_drops, before_count=before_count, ore_pt=ore_pt,
                stand_y=stand_y, cache=cache, can_open_lower=can_open_lower, collect_until=collect_until,
                allow_ore_step=allow_ore_step,
            )
        except _MiningDeadline:
            # A timed-out observation can be skipped. An action may still hold the
            # server lease, in which case the outer slice must stop and preserve its progress.
            if scoped is None:
                raise
            if scoped.timeout_path is not None and scoped.timeout_path not in scoped._RETRYABLE_PROBES:
                scoped.pickup_stop_deadline = pickup_deadline
                raise
            if pickup_deadline >= outer_deadline:
                raise
            counts = _inventory_counts_from_raw(scoped.last_inventory or {})
            scoped.timeout_path = None
        finally:
            if scoped is not None:
                scoped.deadline = outer_deadline
                scoped.pickup_budget_remaining = max(0.0, scoped.pickup_budget_remaining - (time.monotonic() - started))
        gained = sum(counts.get(it, 0) for it in expected_drops) > before_count
        if gained:
            self.verified_ore_pickups += 1
        return gained

    def _check_target_drop_positions(
        self,
        *,
        expected_drops: set[str] | frozenset[str],
        before_count: int,
        ore_pt: tuple[int, int, int],
        stand_y: int,
        cache: dict[tuple[int, int, int], str],
        can_open_lower: bool,
        collect_until: float,
        allow_ore_step: bool,
    ) -> dict[str, int]:
        def verified_ore_step(upper: tuple[int, int, int]) -> bool:
            x, y, z = ore_pt
            return (
                allow_ore_step and upper[1] == y + 1
                and abs(upper[0] - x) + abs(upper[2] - z) == 1
                and self._level_walk_is_clear(upper, upper)
                and self._level_walk_is_clear(ore_pt, ore_pt)
                and self._get_block_name((x, y + 2, z)) in PASSABLE_BLOCKS
            )

        inv, counts = _inventory_snapshot_and_counts(self.bot)
        origin = None
        moved = False
        interrupted = False
        attempted = {
            entity_id: (row["item_pos"], row["reason"]) for entity_id, row in self.drop_receipts.items()
            if row["reason"] in {"move_failed", "pickup_unconfirmed"}
        }

        for attempt in range(3):
            if time.monotonic() + 0.2 >= collect_until:
                break
            try:
                drops = self._scan_target_drops(expected_drops)
                if not drops:
                    if attempt == 0 and sum(counts.get(it, 0) for it in expected_drops) <= before_count:
                        time.sleep(0.16)
                        counts = _inventory_counts(self.bot)
                        continue
                    break

                stance = self.bot.status()
                pos = stance["pos"]
                current = _floor_pos(pos)
                if origin is None:
                    origin = current
                if not stance.get("on_ground", True) or stance.get("in_water") or stance.get("in_lava"):
                    break
                for row in drops:
                    self.drop_receipts[row["id"]] = {
                        **row, "reason": "pickup_budget", "trigger_ore_pos": list(ore_pt),
                    }

                for row in sorted(drops, key=lambda row: math.dist(pos, row["item_pos"])):
                    entity_id, item, drop_pos = row["id"], row["item"], row["item_pos"]
                    self.drop_receipts[entity_id]["reason"] = "height_difference"
                    if abs(drop_pos[1] - current[1]) > 1.3:
                        continue
                    drop_cell = _floor_pos(drop_pos)
                    x, _, z = drop_cell
                    goal = (x, current[1], z)
                    if drop_cell == ore_pt and current == origin and verified_ore_step(current):
                        goal = ore_pt
                    elif not self._level_walk_is_clear(current, goal):
                        self.drop_receipts[entity_id]["reason"] = "unsafe_route"
                        if (
                            attempt == 1 and can_open_lower and ore_pt[1] >= stand_y + 1
                            and goal == (ore_pt[0], stand_y, ore_pt[2])
                        ):
                            self._open_drop_pocket_base(goal, cache)
                        continue
                    previous = attempted.get(entity_id)
                    if previous is not None and previous[0] == drop_pos:
                        self.drop_receipts[entity_id]["reason"] = previous[1]
                        continue
                    if _compute_empty_slots(inv) == 0 and not _can_stack_expected_drop(inv, expected_drops):
                        inv, _ = self.skills._ensure_free_slots(min_free=3, inv_snapshot=inv)
                    reason = "pickup_unconfirmed"
                    try:
                        if not self._move_short_distance(goal, deadline=collect_until - 0.5):
                            self.drop_receipts[entity_id]["reason"] = "pickup_budget"
                            break
                    except ApiError as err:
                        reason = "move_failed"
                        interrupted = err.code in {"hurt", "death", "interrupted", "dead", "disconnected"}
                    moved = True
                    self.drop_receipts[entity_id]["reason"] = reason
                    attempted[entity_id] = (drop_pos, reason)
                    if interrupted:
                        break
                    time.sleep(0.12)
                    inv, after = _inventory_snapshot_and_counts(self.bot)
                    gain = after.get(item, 0) - counts.get(item, 0)
                    counts = after
                    if gain > 0:
                        self._verify_drop_receipts(drops, expected_drops, item, gain)
                    break  # Refresh moving items and the bot's stance before the next approach.
                else:
                    if attempt < 2:
                        time.sleep(0.12)
                if interrupted:
                    break
            except ApiError:
                break

        if moved and not interrupted and origin is not None and time.monotonic() + 0.2 < collect_until:
            try:
                stance = self.bot.status()
                current = _floor_pos(stance["pos"])
                if current != origin and stance.get("on_ground", True) and (
                    (current == ore_pt and verified_ore_step(origin)) or self._level_walk_is_clear(current, origin)
                ):
                    self._move_short_distance(origin, deadline=collect_until)
            except ApiError:
                pass
        if (
            not interrupted and time.monotonic() + 0.2 < collect_until
            and sum(counts.get(it, 0) for it in expected_drops) <= before_count
        ):
            counts = _inventory_counts(self.bot)
        return counts

    def _find_peel_blockers(
        self,
        anchor: tuple[int, int, int],
        ore_pt: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
        *,
        max_blocks: int = 2,
    ) -> list[tuple[int, int, int]]:
        eye = (anchor[0], anchor[1] + 1, anchor[2])
        cands: list[tuple[float, tuple[int, int, int]]] = []
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nb = (ore_pt[0] + ax, ore_pt[1] + ay, ore_pt[2] + az)
            if nb in {anchor, eye, ore_pt}:
                continue
            if abs(nb[1] - anchor[1]) > 3 or (abs(nb[0] - anchor[0]) + abs(nb[2] - anchor[2])) > 4:
                continue
            try:
                b = self._get_block_name(nb, cache)
            except ApiError:
                continue
            if b in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"}:
                continue
            if self._adjacent_fluid_or_falling([nb], cache) is not None:
                continue
            cands.append((math.dist(eye, nb), nb))
        cands.sort(key=lambda r: r[0])
        if not cands:
            return []
        first = cands[0][1]
        if max_blocks <= 1 or abs(first[0] - anchor[0]) + abs(first[2] - anchor[2]) <= 2:
            return [first]
        step_x = first[0] + (1 if anchor[0] > first[0] else (-1 if anchor[0] < first[0] else 0))
        step_z = first[2] + (1 if anchor[2] > first[2] else (-1 if anchor[2] < first[2] else 0))
        mid = (step_x, first[1], step_z)
        if mid not in {anchor, eye, ore_pt, first}:
            try:
                mb = self._get_block_name(mid, cache)
                if mb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([mid], cache) is None:
                    return [mid, first]
            except ApiError:
                pass
        return [first]

    def _drop_cell_reachable(
        self,
        pt: tuple[int, int, int],
        stand: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> bool:
        """True when the drop of `pt` would land within pickup range of our feet position."""
        if (
            max(abs(pt[0] - stand[0]), abs(pt[2] - stand[2])) <= 1
            and abs(pt[1] - stand[1]) <= 1
        ):
            return True
        eye = (stand[0], stand[1] + 1, stand[2])
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
            if nb in {stand, eye}:
                return True
            if abs(nb[0] - stand[0]) + abs(nb[1] - stand[1]) + abs(nb[2] - stand[2]) <= 1:
                try:
                    if self._get_block_name(nb, cache) in PASSABLE_BLOCKS:
                        return True
                except ApiError:
                    continue
        return False

    def _escape_primitive(self, name: str, *, deadline: float, **kwargs: Any) -> dict[str, Any]:
        skill = getattr(self.skills, name)
        params = inspect.signature(skill).parameters
        if "deadline" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["deadline"] = deadline
        return skill(**kwargs)

    def _climb_construction_steps(self, pos: list[float] | None, direction: str, limit: int) -> int:
        fence = self._active_geofence
        if fence is None or not fence.enabled:
            return limit
        if not pos or len(pos) < 3:
            return 0
        distance = fence.distance_from_center(pos[0], pos[2])
        outside = distance > fence.safe_radius
        if direction == "pillar":
            return 0 if outside else limit
        x, _, z = _floor_pos(pos)
        dx, dz = DIRECTIONS[direction]
        if outside:
            # Recheck the actual landing before extending an inward path from outside the fence.
            limit = 1
        for step in range(1, limit + 1):
            landing = fence.distance_from_center(x + dx * step + 0.5, z + dz * step + 0.5)
            if landing > fence.safe_radius and (not outside or landing >= distance):
                return step - 1
        return limit

    def _climb_outside_fence(self, pos: list[float] | None) -> bool:
        fence = self._active_geofence
        return bool(
            pos and fence is not None and fence.enabled
            and fence.distance_from_center(pos[0], pos[2]) > fence.safe_radius
        )

    def _climb_target_reached(self, status: dict[str, Any], minimum_y: int) -> bool:
        pos = status.get("pos")
        return (
            bool(pos) and bool(status.get("on_ground", True))
            and status.get("alive") is not False and status.get("ready") is not False
            and not (status.get("in_water") or status.get("in_lava"))
            and _floor_pos(pos)[1] >= minimum_y
            and not self._climb_outside_fence(pos)
        )

    def _climb_fence_progress(self, climb: dict[str, Any]) -> bool:
        scoped, fence = self._budget_session, self._active_geofence
        if scoped is None or fence is None or not fence.enabled:
            return False
        status = scoped.last_status
        start, end = scoped.first_pos, status.get("pos")
        if not start or not end:
            return False
        start_distance = fence.distance_from_center(start[0], start[2])
        end_distance = fence.distance_from_center(end[0], end[2])
        if max(start_distance, end_distance) <= fence.safe_radius:
            return False
        climb["geofence"] = {
            "start_distance": round(start_distance, 2),
            "end_distance": round(end_distance, 2),
            "safe_radius": fence.safe_radius,
        }
        return (
            start_distance > fence.safe_radius and end_distance < start_distance
            and (math.floor(start[0]), math.floor(start[2])) != (math.floor(end[0]), math.floor(end[2]))
            and status.get("on_ground") is True and status.get("alive") is not False and status.get("ready") is not False
            and not (status.get("in_water") or status.get("in_lava"))
        )

    def _climb_recorded_route_progress(self, climb: dict[str, Any]) -> bool:
        scoped = self._budget_session
        if scoped is None or not scoped.first_pos or not scoped.climb_routes or climb["start_y"] != climb["end_y"]:
            return False
        status = scoped.last_status
        if status.get("on_ground") is not True or not self._climb_target_reached(status, climb["start_y"]):
            return False
        end = _floor_pos(status["pos"])
        if end == _floor_pos(scoped.first_pos):
            return False
        for start_index, route in scoped.climb_routes:
            if end in route and set(route[:route.index(end)]).intersection(scoped.positions[start_index:-1]):
                climb["recorded_route_progress"] = True
                return True
        return False

    def _confirm_climb_slice_progress(self, result: dict[str, Any], scoped: _BudgetSession) -> None:
        if result.get("stop_reason") != "climb_progress":
            return
        status = scoped.last_status
        before, after = scoped.first_pos, status.get("pos")
        if (
            not before or not after or status.get("on_ground") is not True
            or status.get("alive") is False or status.get("ready") is False
            or status.get("in_water") or status.get("in_lava")
        ):
            return
        climb = result["climb"]
        end_y = _floor_pos(after)[1]
        if end_y == climb["end_y"] and (
            end_y > _floor_pos(before)[1] or climb.get("recorded_route_progress") or self._climb_fence_progress(climb)
        ):
            # The harness must retain partial progress without allowing the next queued action.
            result.update(ok=True, pos_before=before, pos_after=after)

    def _climb_out(self, *, deadline: float, max_attempts: int = 40) -> dict[str, Any]:
        """Climb to the surface datum in ONE call: probe directions and keep going.

        Live s09/s12: one `staircase(up, steps=6)` per model turn (10-20s each) plus one wasted
        turn per blocked direction made a 120-block climb cost 10-15 minutes. Doing the probing
        and the repetition in-call turns that into 1-3 turns.
        """
        start_y = _floor_pos(self.bot.status().get("pos") or [0, 64, 0])[1]
        # explore refuses at y <= ceiling-20, so climbing must overshoot that boundary by a few
        # blocks. With target_y == ceiling-20 the two thresholds coincide and live n0332 looped
        # forever: climb_out "reached" at 42, explore rejected 42, climb again, ...
        target_y = (int(self.collar_ceiling_y) - 16) if self.collar_ceiling_y is not None else start_y
        minimum_y = max(start_y, target_y - CLIMB_HEIGHT_TOLERANCE)
        method = "stair"
        try:
            hp_now = float(self.bot.status().get("health") or 20.0)
        except (ApiError, TypeError, ValueError):
            hp_now = 20.0
        hp_ref = [hp_now]

        # 1) Preferred: walk our own corridor to a higher collar.
        walked = False
        collar = self.nodes.get(self.collar_node_id)
        attempted_collar = collar.anchor if collar is not None else None
        if collar is not None and collar.anchor[1] > start_y:
            previous_y = start_y
            while True:
                walked, walk_error = self._walk_to_collar(
                    collar.node_id, min_hp_ref=hp_ref, deadline=deadline, allow_lower_rejoin=False,
                )
                if walked:
                    break
                partial_status = self.bot.status()
                y_part = _floor_pos(partial_status.get("pos") or [0, start_y, 0])[1]
                reached = self._climb_target_reached(partial_status, minimum_y)
                eligible_to_retry = (
                    walk_error not in {"walk_time_slice", "hurt", "not_ready", "not_grounded"}
                    and deadline - time.monotonic() >= 10.0
                    and partial_status.get("on_ground", True)
                    and partial_status.get("alive") is not False and partial_status.get("ready") is not False
                    and not (partial_status.get("in_water") or partial_status.get("in_lava"))
                )
                if reached or (y_part > start_y and not eligible_to_retry):
                    if reached:
                        self._detach_retired_shaft()
                    return {
                        "start_y": start_y,
                        "end_y": y_part,
                        "climbed": y_part - start_y,
                        "target_y": target_y,
                        "minimum_y": minimum_y,
                        "reached": reached,
                        "attempts": 0,
                        "method": "walk",
                    }
                # A recorded walk can stop at an obstruction after making progress.
                # Rejoin once from the new height before trying an alternate escape.
                if not eligible_to_retry or y_part <= previous_y:
                    break
                previous_y = y_part
        # 2) Look for a different nearby saved route before cutting new stairs.
        if not walked:
            cur_now = _floor_pos(self.bot.status().get("pos") or [0, start_y, 0])
            if self._try_restore_saved_shaft(
                cur_now, min_hp_ref=hp_ref, allow_retired=True, skip_collar=attempted_collar,
            ):
                ok_walk, _err_walk = self._navigate_to_node(
                    self.collar_node_id, min_hp_ref=hp_ref, deadline=deadline, allow_lower_rejoin=False,
                )
                if ok_walk:
                    walked = True
                    # Travel only: do not keep this retired tree as the mining tree.
                    self._clear_active_tree_only()
        if walked:
            y_walk = _floor_pos(self.bot.status().get("pos") or [0, start_y, 0])[1]
            # _navigate_to_node can report success while we are still deep (e.g. the active node
            # already equals the collar id). Only trust it when it actually moved us up.
            if y_walk <= start_y and y_walk < minimum_y:
                walked = False
        if walked:
            method = "walk"
            walk_status = self.bot.status()
            y_walk = _floor_pos(walk_status.get("pos") or [0, start_y, 0])[1]
            if self._climb_target_reached(walk_status, minimum_y):
                self._detach_retired_shaft()
                return {
                    "start_y": start_y,
                    "end_y": y_walk,
                    "climbed": y_walk - start_y,
                    "target_y": target_y,
                    "minimum_y": minimum_y,
                    "reached": True,
                    "attempts": 0,
                    "method": method,
                }

        dirs = ["north", "south", "east", "west"]
        if getattr(self, "_climb_dir", None) in dirs:
            dirs.remove(self._climb_dir)
            dirs.insert(0, self._climb_dir)
        attempts = 0
        while time.monotonic() < deadline and attempts < max_attempts:
            stance = self.bot.status()
            cur_cell = _floor_pos(stance.get("pos") or [0, 64, 0])
            cur_y = cur_cell[1]
            at_height = cur_y >= minimum_y
            if at_height and not self._climb_outside_fence(stance.get("pos")):
                break
            if len(self._climb_failures) > 128:
                self._climb_failures.pop(next(iter(self._climb_failures)))
            failed = self._climb_failures.setdefault(cur_cell, set())
            moved = 0
            stair_dirs = [] if at_height else [d for d in dirs if ("stair", d) not in failed]
            for d in stair_dirs:
                if time.monotonic() >= deadline or attempts >= max_attempts:
                    break
                if not self._climb_construction_steps(stance.get("pos"), d, 1):
                    continue
                attempts += 1
                failed.add(("stair", d))
                try:
                    self._escape_primitive("dig_staircase", deadline=deadline,
                                           direction=d, steps=1, vertical="up", stop_on_ore=False)
                except (ApiError, ValueError, KeyError, TypeError):
                    if self._active_geofence is not None and self._active_geofence.enabled:
                        stance = self.bot.status()
                    continue
                stance = self.bot.status()
                gained = _floor_pos(stance.get("pos") or [0, cur_y, 0])[1] - int(cur_y)
                if gained > 0:
                    self._climb_failures.pop(cur_cell, None)
                    self._climb_dir = d
                    moved += gained
                    cur_y += gained
                    break          # keep using the direction that just worked
            if moved > 0:
                continue
            if time.monotonic() >= deadline or attempts >= max_attempts:
                break
            if (
                not at_height and ("pillar", "pillar") not in failed
                and self._climb_construction_steps(stance.get("pos"), "pillar", 1)
            ):
                failed.add(("pillar", "pillar"))
                attempts += 1
                try:
                    self._escape_primitive("dig_staircase", deadline=deadline,
                                           direction="pillar", steps=1, vertical="pillar", stop_on_ore=False)
                except (ApiError, ValueError, KeyError, TypeError):
                    pass
                continue
            # Tunnel sideways to clear a blocked ascent, or toward the fence interior
            # without climbing higher once the height target is met.
            escaped = False
            for d in dirs:
                if time.monotonic() >= deadline or attempts >= max_attempts:
                    break
                if ("tunnel", d) in failed:
                    continue
                pos = self.bot.status().get("pos")
                steps = self._climb_construction_steps(pos, d, 3)
                if not steps:
                    continue
                before = _floor_pos(pos or [0, cur_y, 0])
                failed.add(("tunnel", d))
                attempts += 1
                try:
                    tunnel = self._escape_primitive("dig_level_tunnel", deadline=deadline,
                                                    direction=d, steps=steps, stop_on_ore=False)
                except _MiningDeadline:
                    failed.discard(("tunnel", d))
                    raise
                except (ApiError, ValueError, KeyError, TypeError):
                    continue
                if tunnel.get("stop_reason") == "time_slice":
                    failed.discard(("tunnel", d))
                    break
                after = _floor_pos(self.bot.status().get("pos") or [0, cur_y, 0])
                if after != before:
                    self._climb_dir = d
                    escaped = True
                    break
            if escaped:
                continue
            break
        end_status = self.bot.status()
        end_y = _floor_pos(end_status.get("pos") or [0, 64, 0])[1]
        reached = self._climb_target_reached(end_status, minimum_y)
        if reached:
            self._detach_retired_shaft()
        return {
            "start_y": start_y,
            "end_y": end_y,
            "climbed": end_y - start_y,
            "target_y": target_y,
            "minimum_y": minimum_y,
            "reached": reached,
            "attempts": attempts,
            "method": method if not walked else f"{method}+stair",
        }

    @staticmethod
    def _climb_arrival_message(climb: dict[str, Any]) -> str:
        message = (
            f"已达到本轮回撤高度，当前 y={climb['end_y']}"
            f"（最低 y={climb['minimum_y']}，计划目标 y={climb['target_y']}）"
        )
        if climb["recorded_collar_y"] is not None:
            message += f"，最高记录井口 y={climb['recorded_collar_y']}"
        return (
            message + "。若任务要求到地表，请确认已出洞。仍在洞内时可用 mc_skill_staircase(vertical='up') 继续上行。"
            "若只需换井，可以在安全站位继续 explore。"
        )

    @staticmethod
    def _climb_progress_message(climb: dict[str, Any]) -> str:
        fence = climb.get("geofence")
        if fence and fence["end_distance"] > fence["safe_radius"]:
            return (
                f"本轮回撤到 y={climb['end_y']}，距围栏中心 {fence['end_distance']} 格"
                f"（安全半径 {fence['safe_radius']} 格）。可从当前站位继续 climb_out 向内回撤。"
            )
        if climb.get("recorded_route_progress"):
            return f"本轮已沿记录矿道回撤，当前 y={climb['end_y']}（目标 y={climb['target_y']}）。可从当前站位继续 climb_out。"
        return f"本轮爬到 y={climb['end_y']}（目标 y={climb['target_y']}），可继续 climb_out。"

    def _surface_climb_hint(self) -> str:
        """Tell the model exactly how high to climb before calling explore again."""
        if self.collar_ceiling_y is None:
            return (
                "请调用 action='climb_out'，优先走自建巷道，再挖台阶。"
                "若返回 climb_progress，可从当前站位继续 climb_out。"
            )
        need_y = int(self.collar_ceiling_y) - 20
        return (
            f"请调用 action='climb_out'，优先沿自建巷道向上（目标 y>={need_y}，"
            f"地表基准 y={self.collar_ceiling_y}）。若返回 climb_progress，可从当前站位继续 climb_out。"
            "到达地表后再 explore 开新井。"
        )

    def _mine_vein_ore_cell(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        anchor: tuple[int, int, int],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[bool, int, int]:
        if pt in self.mined_ore_positions or self._is_ore_unreachable(pt) or self.skills._is_block_avoided(pt):
            return False, 0, 0
        try:
            if self._get_block_name(pt, cache) != bname:
                return False, 0, 0
        except ApiError:
            return False, 0, 0
        try:
            stance = self.bot.status()
        except ApiError:
            stance = {}
        stand = _floor_pos(stance["pos"]) if stance.get("pos") else anchor
        distance = sum(abs(ore_coord - foot_coord) for ore_coord, foot_coord in zip(pt, stand))
        replaces_footing = pt == (stand[0], stand[1] - 1, stand[2])
        if replaces_footing or distance > DROP_PICKUP_RADIUS_MANHATTAN + 2:
            return self._mine_vein_cell_from_side(
                pt, bname, stance=stance, target_set=target_set, cache=cache, replaces_footing=replaces_footing,
            )
        return self._dig_vein_cell(pt, bname, anchor=anchor, target_set=target_set, cache=cache)

    def _mine_vein_cell_from_side(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        stance: dict[str, Any],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
        replaces_footing: bool,
    ) -> tuple[bool, int, int]:
        def dry_footing(status: dict[str, Any]) -> bool:
            return (
                bool(status.get("pos")) and status.get("on_ground", True)
                and status.get("alive") is not False and status.get("ready") is not False
                and not (status.get("in_water") or status.get("in_lava"))
            )

        if not dry_footing(stance):
            return False, 0, 0
        origin = _floor_pos(stance["pos"])
        x, _, z = pt
        candidates = [(x + dx, origin[1], z + dz) for dx, dz in DIRECTIONS.values()]
        candidates.sort(key=lambda cell: math.dist(origin, cell))
        deadline = self._deadline or math.inf
        try:
            goal = next((
                cell for cell in candidates
                if self._drop_cell_reachable(pt, cell, cache) and self._level_walk_is_clear(origin, cell)
            ), None)
        except ApiError:
            return False, 0, 0
        if goal is None:
            return False, 0, 0

        can_dig = False
        try:
            if not self._move_short_distance(goal, deadline=deadline):
                return False, 0, 0
            landed = self.bot.status()
            can_dig = (
                dry_footing(landed) and _floor_pos(landed["pos"]) == goal
                and self._level_walk_is_clear(goal, goal)
                and self._get_block_name(pt) == bname
            )
        except ApiError as err:
            if err.code != "path_not_found":
                return False, 0, 0

        result = (
            self._dig_vein_cell(
                pt, bname, anchor=goal, target_set=target_set, cache=cache, replaces_footing=replaces_footing,
            )
            if can_dig else (False, 0, 0)
        )

        try:
            current = self.bot.status()
            if dry_footing(current) and (cell := _floor_pos(current["pos"])) != origin:
                if (
                    replaces_footing and result[0] and cell == goal
                    and self._get_block_name(pt) in AIR_BLOCKS
                    and self._level_walk_is_clear(cell, cell)
                ):
                    self._place_bridge_block(pt, cache)
                if self._level_walk_is_clear(cell, origin):
                    self._move_short_distance(origin, deadline=deadline)
        except ApiError:
            pass
        return result

    def _dig_vein_cell(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        anchor: tuple[int, int, int],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
        replaces_footing: bool = False,
    ) -> tuple[bool, int, int]:
        """Break the ore with fluid and cave protection, then verify the drop."""
        catch_placed = 0
        breaches_sealed = 0
        hazard, adj_fluids = self._inspect_step_hazards([pt], cache)
        if hazard is not None:
            self._mark_ore_unreachable(pt, hazard[0])
            return False, 0, 0
        if adj_fluids:
            sealed_f = self._seal_breach(adj_fluids, cache)
            breaches_sealed += len(sealed_f)
            if len(sealed_f) < len(adj_fluids) and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                self._mark_ore_unreachable(pt, "adjacent_lava_unsealable")
                return False, catch_placed, breaches_sealed

        # Inspect outer cave-air faces behind pt (away from anchor) BEFORE breaking pt
        outer_cave_faces: list[tuple[int, int, int]] = []
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
            nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
            if abs(nb[0] - anchor[0]) <= 1 and abs(nb[2] - anchor[2]) <= 1:
                continue
            if self._is_self_cavity([nb]):
                continue
            try:
                if self._get_block_name(nb, cache) in AIR_BLOCKS:
                    outer_cave_faces.append(nb)
            except ApiError:
                pass

        below_pt = (pt[0], pt[1] - 1, pt[2])
        try:
            below_name = self._get_block_name(below_pt, cache)
        except ApiError:
            below_name = "stone"
        if below_name in PASSABLE_BLOCKS and below_pt != anchor and below_pt != (anchor[0], anchor[1] + 1, anchor[2]):
            counts = _inventory_counts(self.bot)
            filler = _pick_seal_material(counts)
            if filler:
                try:
                    self.bot.call("place", {"item": filler, "pos": [below_pt[0], below_pt[1], below_pt[2]]})
                    cache[below_pt] = filler
                    self.catch_blocks_placed.append([below_pt[0], below_pt[1], below_pt[2]])
                    catch_placed += 1
                except ApiError:
                    pass

        expected_drops = _expected_drops_for_blocks([bname])
        inv_pre, counts_pre = _inventory_snapshot_and_counts(self.bot)
        if expected_drops and _compute_empty_slots(inv_pre) < 2 and not _can_stack_expected_drop(inv_pre, expected_drops):
            inv_pre, _ = self.skills._ensure_free_slots(min_free=4, inv_snapshot=inv_pre)
            inv_pre, counts_pre = _inventory_snapshot_and_counts(self.bot)
        before_target_cnt = sum(int(counts_pre.get(it, 0)) for it in expected_drops)

        req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
        chosen, _, _, total_rem = self._equip_for_block_tier(req_t, for_ore=True, inv_snapshot=inv_pre)
        if chosen is None or total_rem < 1:
            return False, catch_placed, breaches_sealed

        # Prevent Mineflayer collectDrops -> goNear from pulling the bot into a monster cave!
        hostile_near = False
        if outer_cave_faces:
            try:
                st_h = self.bot.status()
                nh = st_h.get("nearest_hostile")
                hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 12.0
            except ApiError:
                hostile_near = False
        safe_auto_collect = not (replaces_footing or (outer_cave_faces and hostile_near))

        dug = False
        _scan_band_cell = self.strategy == "fishbone_scan" and abs(anchor[1] - self.target_y) <= SCAN_BAND
        max_peel = 2 if (self.surgical_enabled or _scan_band_cell) else 1
        try:
            self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": safe_auto_collect, "stop_on": ["hurt", "death"]})
            dug = True
        except ApiError as err:
            if (
                err.code == "out_of_reach"
                and bname in target_set
                and abs(pt[1] - anchor[1]) <= (3 if (self.radar_enabled or _scan_band_cell) else 2)
                and (abs(pt[0] - anchor[0]) + abs(pt[2] - anchor[2])) <= (5 if (self.surgical_enabled or _scan_band_cell) else (4 if self.radar_enabled else 3))
            ):
                peel_pts = self._find_peel_blockers(anchor, pt, cache, max_blocks=max_peel)
                if peel_pts:
                    peeled_ok = True
                    for pp in peel_pts:
                        ok_p, _, _, _ = self._dig_cell_safely(pp, collect_rock=False, parent_node_id=0, cache=cache)
                        if not ok_p:
                            peeled_ok = False
                            break
                    if peeled_ok:
                        chosen2, _, _, rem2 = self._equip_for_block_tier(req_t, for_ore=True)
                        if chosen2 is not None and rem2 >= 1:
                            try:
                                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": safe_auto_collect, "stop_on": ["hurt", "death"]})
                                dug = True
                            except ApiError:
                                pass
        if not dug:
            return False, catch_placed, breaches_sealed

        cache[pt] = "air"
        self._record_excavated(pt)
        self.mined_ore_positions.add(pt)
        self.deferred_ores.pop(pt, None)
        self.ledger.remove_ore(pt)

        if adj_fluids:
            breaches_sealed += len(self._seal_breach(adj_fluids, cache))

        # Seal the OUTER cave opening behind pt FIRST (so monsters are walled off while pt stays open to collect drop!)
        outer_all_sealed = True
        if outer_cave_faces:
            sealed_out = self._seal_breach(outer_cave_faces, cache)
            breaches_sealed += len(sealed_out)
            outer_all_sealed = len(sealed_out) >= len(outer_cave_faces)

        collected_ok = True
        if expected_drops:
            collected_ok = self._collect_and_verify_target_drop(
                expected_drops=expected_drops,
                before_count=before_target_cnt,
                ore_pt=pt,
                stand_y=anchor[1],
                cache=cache,
                can_open_lower=outer_all_sealed,
                allow_ore_step=replaces_footing,
            )

        if expected_drops and not collected_ok:
            self.lost_drops += 1

        # Only AFTER drop pickup is verified/finished, plug pt itself if outer cave could not be fully sealed
        if outer_cave_faces and not outer_all_sealed:
            breaches_sealed += len(self._seal_breach([pt], cache))

        return collected_ok, catch_placed, breaches_sealed

    def _surgical_breach_to_ore(
        self,
        anchor: tuple[int, int, int],
        pt: tuple[int, int, int],
        bname: str,
        *,
        target_set: set[str],
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
        min_hp_ref: list[float],
    ) -> tuple[bool, int, int]:
        """Backside pure-rock 1-3 step pocket approach + waist parapet + 1x1 window extraction ('radar' only)."""
        catch_total = 0
        seal_total = 0
        # Pre-check pt for unsealable lava before investing pocket steps
        hazard0, adj_fl0 = self._inspect_step_hazards([pt], cache)
        if hazard0 is not None or any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fl0):
            self._mark_ore_unreachable(pt, "lava_adjacent_target")
            return False, 0, 0

        pocket_wps: list[tuple[int, int, int]] = [anchor]
        cur = anchor

        for _ in range(3):
            h_manhattan = abs(pt[0] - cur[0]) + abs(pt[2] - cur[2])
            v_diff = pt[1] - cur[1]
            if h_manhattan <= 1 and abs(v_diff) <= 2:
                break
            vx, vz = pt[0] - cur[0], pt[2] - cur[2]
            if abs(vx) >= abs(vz) and vx != 0:
                sdx, sdz = (1 if vx > 0 else -1), 0
            elif vz != 0:
                sdx, sdz = 0, (1 if vz > 0 else -1)
            else:
                break
            nx, nz = cur[0] + sdx, cur[2] + sdz
            if self._geofence_blocked(geofence, nx, nz, action="surgical_pocket") is not None:
                break

            step_dy = -1 if v_diff <= -2 else (1 if v_diff >= 2 else 0)
            ny = cur[1] + step_dy
            if (nx, ny, nz) == pt or (nx, ny + 1, nz) == pt:
                break

            # STRICT RULE: Never step into natural cave_air! Stop inside solid rock backside!
            check_cells = [(nx, ny, nz), (nx, ny + 1, nz)]
            if step_dy == -1:
                check_cells.append((nx, cur[1] + 1, nz))
            elif step_dy == 1:
                check_cells.append((cur[0], cur[1] + 2, cur[2]))
            try:
                enters_natural_cave = any(
                    self._get_block_name(c, cache) in AIR_BLOCKS and not self._is_self_cavity([c])
                    for c in ((nx, ny, nz), (nx, ny + 1, nz))
                )
            except ApiError:
                break
            if enters_natural_cave:
                break

            haz_s, adj_fl_s = self._inspect_step_hazards(check_cells, cache, forward_delta=(sdx, sdz))
            if haz_s is not None:
                break
            floor_cell = (nx, ny - 1, nz)
            try:
                fl_blk = self._get_block_name(floor_cell, cache)
                fl_ok = self._is_solid_floor(floor_cell, cache)
            except ApiError:
                break
            if fl_blk in FLUID_BLOCKS | {"bedrock"} | HAZARD_FLOOR_BLOCKS:
                break
            if adj_fl_s:
                seal_total += len(self._seal_breach(adj_fl_s, cache))
            ok_d, _, _, _ = self._dig_step_cells(check_cells, collect_rock=True, parent_node_id=0, cache=cache)
            if not ok_d:
                break
            if adj_fl_s:
                seal_total += len(self._seal_breach(adj_fl_s, cache))
            if not fl_ok and not self._place_bridge_block(floor_cell, cache):
                break
            if not self._move_exact((nx, ny, nz), timeout_ms=2800):
                break
            cur = (nx, ny, nz)
            pocket_wps.append(cur)

        # Enforce Waist-High Parapet + 1x1 Eye Window between cur and pt!
        vx_f, vz_f = pt[0] - cur[0], pt[2] - cur[2]
        if abs(vx_f) >= abs(vz_f) and vx_f != 0:
            wdx, wdz = (1 if vx_f > 0 else -1), 0
        elif vz_f != 0:
            wdx, wdz = 0, (1 if vz_f > 0 else -1)
        else:
            wdx, wdz = 0, 0

        need_reseal_parapet: tuple[int, int, int] | None = None
        if (wdx, wdz) != (0, 0):
            parapet_pt = (cur[0] + wdx, cur[1], cur[2] + wdz)
            window_pt = (cur[0] + wdx, cur[1] + 1, cur[2] + wdz)
            sunk_under_parapet = parapet_pt == (pt[0], pt[1] + 1, pt[2])
            if parapet_pt != pt and not sunk_under_parapet:
                try:
                    if not self._is_solid_floor(parapet_pt, cache):
                        # Place waist-high cobblestone parapet so 1.8m mobs & pathfinder CANNOT cross!
                        self._place_bridge_block(parapet_pt, cache)
                except ApiError:
                    pass
            if window_pt != pt:
                try:
                    wb = self._get_block_name(window_pt, cache)
                    if wb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([window_pt], cache) is None:
                        self._dig_cell_safely(window_pt, collect_rock=False, parent_node_id=0, cache=cache)
                except ApiError:
                    pass
            if sunk_under_parapet:
                # Target ore is directly beneath parapet_pt (e.g. LocC-v4 sunken ore):
                # uncover its top cap to expose the ore from our elevated step, then re-plug parapet after pickup!
                try:
                    pb = self._get_block_name(parapet_pt, cache)
                    if pb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([parapet_pt], cache) is None:
                        self._dig_cell_safely(parapet_pt, collect_rock=False, parent_node_id=0, cache=cache)
                        need_reseal_parapet = parapet_pt
                except ApiError:
                    pass

        ok_m, c_delta, s_delta = self._mine_vein_ore_cell(
            pt,
            bname,
            anchor=cur,
            target_set=target_set,
            cache=cache,
        )
        catch_total += c_delta
        seal_total += s_delta
        if need_reseal_parapet is not None:
            try:
                for ax, az in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nb_c = (need_reseal_parapet[0] + ax, need_reseal_parapet[1], need_reseal_parapet[2] + az)
                    if nb_c != cur and not self._is_self_cavity([nb_c]) and self._get_block_name(nb_c, cache) in AIR_BLOCKS:
                        seal_total += len(self._seal_breach([need_reseal_parapet], cache, allow_corridor=True))
                        break
            except ApiError:
                pass
        if ok_m:
            self.surgical_extractions += 1
        elif abs(pt[0] - cur[0]) + abs(pt[2] - cur[2]) <= 2:
            self._mark_ore_unreachable(pt, "surgical_out_of_reach")

        if len(pocket_wps) > 1:
            self._walk_waypoints(pocket_wps, reverse=True, min_hp_ref=min_hp_ref)
        if pt != (anchor[0], anchor[1] - 1, anchor[2]):
            self._move_exact(anchor, timeout_ms=2500)
        return ok_m, catch_total, seal_total

    def _vein_hook_reach(self, *, target_ore: bool, scan_band: bool) -> tuple[int, int]:
        """Return the vertical and horizontal search limits from a corridor anchor."""
        if self.surgical_enabled:
            return (4, 6) if target_ore else (3, 4)
        if self.radar_enabled:
            return (4, 7) if target_ore else (3, 5)
        if scan_band:
            return (6, 8) if target_ore else (3, 4)
        return 2, 3

    def _run_vein_hook(
        self,
        anchor: tuple[int, int, int],
        *,
        target_blocks: tuple[str, ...],
        current_node_id: int,
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
        min_target_tier: int = 1,
        min_hp_ref: list[float] | None = None,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Harvest exposed ores ('fishbone': strict corridor-adjacent; 'radar': 12m scan + 1x1 surgical breach)."""
        target_set = set(target_blocks)
        inv_raw, _inv_counts = _inventory_snapshot_and_counts(self.bot)
        _, max_tier, _ = _pickaxe_inventory_summary(inv_raw)
        focus_target_only = _should_focus_target_only(
            strategy=self.strategy,
            min_target_tier=min_target_tier,
            max_tier=max_tier,
            anchor_y=anchor[1],
            target_y=self.target_y,
        )
        # BUG-092: when iron stock < 12, include iron ores in query_types so close exposed iron (<=2.5m) can be grabbed on the way.
        if focus_target_only:
            if (_inv_counts.get("iron_ingot", 0) + _inv_counts.get("raw_iron", 0)) < 12:
                query_types = list(dict.fromkeys([*target_blocks, "iron_ore", "deepslate_iron_ore"]))
            else:
                query_types = list(target_blocks)
        else:
            query_types = list(TRACKED_ORES)
        # fishbone_scan: widen ore reach ONLY when standing in the target band (target layer),
        # keep the safe narrow corridor-adjacent behaviour on the transit layers.
        _scan_band = self.strategy == "fishbone_scan" and abs(anchor[1] - self.target_y) <= SCAN_BAND
        if self.radar_enabled:
            hook_radius = VEIN_HOOK_RADAR_RADIUS
            hook_limit = 24
        elif _scan_band:
            hook_radius = VEIN_HOOK_SCAN_RADIUS
            hook_limit = 20
        else:
            hook_radius = VEIN_HOOK_FISHBONE_RADIUS
            hook_limit = 12

        try:
            found = self.bot.call("find_blocks", {"radius": hook_radius, "types": query_types, "limit": hook_limit})
        except ApiError:
            return {"mined_coords": [], "deferred_added": 0, "catch_placed": 0, "breaches_sealed": 0}

        raw_blocks = found.get("blocks") or []
        mined_coords: list[list[int]] = []
        deferred_added = 0
        catch_placed = 0
        breaches_sealed = 0
        hp_ref = min_hp_ref if min_hp_ref is not None else [20.0]

        candidates: list[tuple[int, float, tuple[int, int, int], str]] = []
        for blk in raw_blocks:
            bname = str(blk.get("name") or "").removeprefix("minecraft:")
            bpos = blk.get("pos")
            if not bname or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            if pt in self.mined_ore_positions or self._is_ore_unreachable(pt) or self.skills._is_block_avoided(pt):
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="vein_hook") is not None:
                continue
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
            if bname in target_set:
                self.ledger.record_ore(pt, bname, req_t)

            max_y_diff, max_xz_diff = self._vein_hook_reach(target_ore=bname in target_set, scan_band=_scan_band)

            if abs(pt[1] - anchor[1]) > max_y_diff or (abs(pt[0] - anchor[0]) + abs(pt[2] - anchor[2])) > max_xz_diff:
                if (self.radar_enabled or _scan_band) and bname in target_set and req_t <= max_tier:
                    self.ever_seen_radar_ores.add(pt)
                    if pt not in self.radar_targets:
                        self.radar_targets.append(pt)
                elif req_t > max_tier and pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(pt, bname, req_t, current_node_id)
                    deferred_added += 1
                continue
            if req_t > max_tier:
                if pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(
                        pos=pt,
                        block_name=bname,
                        required_tier=req_t,
                        nearest_node_id=current_node_id,
                    )
                    deferred_added += 1
                continue
            if bname not in target_set:
                if focus_target_only:
                    # BUG-092: zero-detour opportunistic iron grab (<=2.5m) when iron stock < 12
                    if (
                        bname in {"iron_ore", "deepslate_iron_ore"}
                        and math.dist(anchor, pt) <= 2.5
                        and (_inv_counts.get("iron_ingot", 0) + _inv_counts.get("raw_iron", 0)) < 12
                    ):
                        pass
                    else:
                        continue
                elif bname not in {"coal_ore", "deepslate_coal_ore", "iron_ore", "deepslate_iron_ore", "diamond_ore", "deepslate_diamond_ore"}:
                    continue
            prio = 0 if bname in target_set else 1
            dist = math.dist(anchor, pt)
            candidates.append((prio, dist, pt, bname))

        candidates.sort(key=lambda r: (r[0], r[1]))
        vein_queue: deque[tuple[tuple[int, int, int], str]] = deque()
        queued_pts: set[tuple[int, int, int]] = set()
        for _, _, pt, bname in candidates[:6]:
            if pt not in queued_pts:
                vein_queue.append((pt, bname))
                queued_pts.add(pt)

        vein_limit = 16
        chain_limit = 7 if self.radar_enabled else 4
        while vein_queue and len(mined_coords) < vein_limit:
            pt, bname = vein_queue.popleft()
            try:
                ok_ore, catch_delta, seal_delta = self._mine_vein_ore_cell(
                    pt,
                    bname,
                    anchor=anchor,
                    target_set=target_set,
                    cache=cache,
                )
                catch_placed += catch_delta
                breaches_sealed += seal_delta
                if (
                    not ok_ore
                    and (self.surgical_enabled or _scan_band)
                    and bname in target_set
                    and pt not in self.mined_ore_positions
                    and min(abs(pt[0] - anchor[0]), abs(pt[2] - anchor[2])) <= 1
                    and max(abs(pt[0] - anchor[0]), abs(pt[2] - anchor[2])) <= 4
                ):
                    ok_ore, c_surg, s_surg = self._surgical_breach_to_ore(
                        anchor,
                        pt,
                        bname,
                        target_set=target_set,
                        geofence=geofence,
                        cache=cache,
                        min_hp_ref=hp_ref,
                    )
                    catch_placed += c_surg
                    breaches_sealed += s_surg
                elif not ok_ore and self.radar_enabled and bname in target_set and pt not in self.mined_ore_positions:
                    self.ever_seen_radar_ores.add(pt)
                    if pt not in self.radar_targets:
                        self.radar_targets.append(pt)
            except _MiningDeadline:
                scoped = self._budget_session
                if (
                    scoped is not None and not scoped.dug_count
                    and scoped.timeout_dig_read_s >= 1.0 and scoped.timeout_dig_pos != pt
                ):
                    # The next approach to this ore might dig a different blocker.
                    self.skills._avoid_temporarily(pt, "vein_dig_timeout", DIG_TIMEOUT_COOLDOWN_S)
                raise
            if not ok_ore:
                continue
            mined_coords.append([pt[0], pt[1], pt[2]])
            if bname in target_set:
                if quota_met is not None and quota_met():
                    break
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
                    if nb in queued_pts or nb in self.mined_ore_positions or self.skills._is_block_avoided(nb):
                        continue
                    if geofence is not None and geofence.check_point(nb[0] + 0.5, nb[2] + 0.5, action="vein_hook") is not None:
                        continue
                    if abs(nb[0] - anchor[0]) + abs(nb[1] - anchor[1]) + abs(nb[2] - anchor[2]) > chain_limit:
                        continue
                    try:
                        nb_name = self._get_block_name(nb, cache)
                    except ApiError:
                        continue
                    if nb_name in target_set:
                        vein_queue.append((nb, nb_name))
                        queued_pts.add(nb)

        if mined_coords:
            self._move_exact(anchor)
            self._equip_for_block_tier(1, for_ore=False)

        return {
            "mined_coords": mined_coords,
            "deferred_added": deferred_added,
            "catch_placed": catch_placed,
            "breaches_sealed": breaches_sealed,
        }

    # ---- Edge Excavators (Down, Up, Level) ----

    def _dig_step_cells(
        self,
        cells: list[tuple[int, int, int]],
        *,
        collect_rock: bool,
        parent_node_id: int,
        cache: dict[tuple[int, int, int], str],
        verify_pickaxe: bool = False,
    ) -> tuple[bool, str | None, list[list[int]], int]:
        mined_ores: list[list[int]] = []
        deferred_added = 0
        for dpos in cells:
            ok_d, err_d, was_ore, def_delta = self._dig_cell_safely(
                dpos,
                collect_rock=collect_rock,
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=verify_pickaxe,
            )
            deferred_added += def_delta
            if was_ore:
                mined_ores.append([dpos[0], dpos[1], dpos[2]])
            if not ok_d:
                return False, err_d or "dig_failed", mined_ores, deferred_added
        return True, None, mined_ores, deferred_added

    def _move_excavated_step(
        self,
        step_pt: tuple[int, int, int],
        *,
        from_cell: tuple[int, int, int],
        head_cells: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        deadline: float,
        min_hp: float,
    ) -> str | None:
        floor = (step_pt[0], step_pt[1] - 1, step_pt[2])
        # Excavation or a failed bridge placement can invalidate the earlier support check.
        try:
            if not self._is_solid_floor(floor, {}):
                return "unsafe_floor_or_cave"
        except ApiError:
            return "unknown_block"
        if self._move_exact(step_pt):
            return None
        blocked = "movement_not_confirmed"
        if time.monotonic() + 2.5 >= deadline:
            return blocked
        try:
            stance = self.bot.status()
            if stance.get("alive") is False or stance.get("ready") is False:
                return "not_ready"
            hp = stance.get("health")
            if isinstance(hp, (int, float)) and hp < min_hp:
                return "hurt"
            if (
                not stance.get("on_ground", True)
                or stance.get("in_water") or stance.get("in_lava")
            ):
                return blocked
            if _floor_pos(stance["pos"]) != from_cell:
                if step_pt[1] == from_cell[1] - 1 and stance.get("on_ground") is True:
                    return self._rejoin_excavated_descent(step_pt, from_cell, stance, deadline=deadline, min_hp=min_hp)
                return blocked

            clearance = tuple(dict.fromkeys((*head_cells, step_pt)))
            for cell in (*clearance, floor):
                cache.pop(cell, None)
            names = [self._get_block_name(cell, cache) for cell in clearance]
            if (
                any(name not in PASSABLE_BLOCKS | FALLING_BLOCKS for name in names)
                or not self._is_solid_floor(floor, cache)
            ):
                return blocked
            for cell, name in zip(clearance, names):
                if name in FALLING_BLOCKS:
                    ok, reason, _, _ = self._dig_cell_safely(cell, collect_rock=False, parent_node_id=0, cache=cache)
                    if not ok:
                        return reason if reason in {"hurt", "not_ready", "interrupted"} else blocked
        except ApiError:
            return blocked

        if time.monotonic() + 2.5 < deadline and self._move_exact(step_pt, timeout_ms=1500):
            return None
        return blocked

    def _rejoin_excavated_descent(
        self,
        step: tuple[int, int, int],
        start: tuple[int, int, int],
        stance: dict[str, Any],
        *,
        deadline: float,
        min_hp: float,
    ) -> str | None:
        """Return to the excavated step after overshooting onto a short dry stair."""
        blocked = "movement_not_confirmed"
        current = _floor_pos(stance["pos"])
        dx, dz = step[0] - start[0], step[2] - start[2]
        overshoot = (current[0] - step[0], current[2] - step[2])
        rise = step[1] - current[1]
        if overshoot not in {(dx, dz), (2 * dx, 2 * dz)} or rise not in {0, 1}:
            return blocked
        cache: dict[tuple[int, int, int], str] = {}
        if rise == 1 and overshoot != (dx, dz):
            route = self._recorded_mixed_stair(current, start, cache=cache)
            if route is None or step not in route[1:-1]:
                return blocked
            recovery = route[:route.index(step) + 1]
        else:
            recovery = _interpolate_cells(current, step)
        for x, y, z in recovery:
            feet, head = (x, y, z), (x, y + 1, z)
            if (
                self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                or self.skills._is_block_avoided(feet)
                or any(self._get_block_name(cell, cache) not in PASSABLE_BLOCKS for cell in (feet, head))
                or not self._is_solid_floor((x, y - 1, z), cache)
                or self._adjacent_fluid_or_falling([head, feet], cache) is not None
            ):
                return blocked

        for x, y, z in recovery[1:]:
            if time.monotonic() + 2.5 >= deadline:
                return blocked
            self.bot.call("move_to", {
                "x": x + 0.5, "y": y, "z": z + 0.5, "range": 0.3, "timeout": 1500,
                "stop_on": ["hurt", "death"],
            }, timeout_s=2.5)
            actual = self.bot.status()
            if (
                actual.get("alive") is False or actual.get("ready") is False
                or actual.get("last_death") != stance.get("last_death")
                or actual.get("dimension") != stance.get("dimension")
            ):
                return "not_ready"
            hp = actual.get("health")
            if isinstance(hp, (int, float)) and hp < min_hp:
                return "hurt"
            if (
                actual.get("on_ground") is not True or actual.get("in_water") or actual.get("in_lava")
                or not actual.get("pos") or _floor_pos(actual["pos"]) != (x, y, z)
            ):
                return blocked
        self._record_corridor(step)
        return None

    def _should_recraft_burner_pickaxe(
        self,
        chosen_rock: dict[str, Any] | None,
        total_rem: int,
        *,
        in_deepslate: bool = False,
    ) -> bool:
        """Recraft a stone burner pickaxe when durability is low, only wooden remains (BUG-091), or we would burn iron/diamond on rock."""
        if total_rem <= RECRAFT_PICK_DURABILITY:
            return True
        if in_deepslate and chosen_rock is not None and int(chosen_rock.get("tier", 1)) >= 3 and total_rem > 160:
            return False
        if chosen_rock is not None and int(chosen_rock.get("tier", 1)) != 2:
            return True
        return False

    def _try_recraft_burner_pickaxe(
        self, counts_now: dict[str, int], *, at_durability_redline: bool = False,
    ) -> bool:
        """Craft one stone pickaxe from collected stone/wood so rock digging never eats the iron pick.

        Returns True when a fresh pickaxe is now in the inventory.
        """
        # Allow one replacement if the two worn picks together cannot sustain digging.
        stone_pickaxe_limit = 3 if at_durability_redline else 2
        if int(counts_now.get("stone_pickaxe", 0)) >= stone_pickaxe_limit:
            return False
        affordable, stone, _ = _stone_pickaxe_headroom(counts_now)
        if affordable <= 0 or stone < 3:
            return False
        has_table = int(counts_now.get("crafting_table", 0)) >= 1
        if not has_table:
            planks_equiv = _total_planks(counts_now) + 4 * sum(
                int(v) for k, v in counts_now.items() if k.endswith(("_log", "_stem"))
            )
            sticks = int(counts_now.get("stick", 0))
            need_planks = 4 + (0 if sticks >= 2 else 2)
            if planks_equiv < need_planks:
                return False
        scoped = self._budget_session
        confirmed_before = scoped.confirmed_stone_pickaxe_crafts if scoped is not None else 0
        try:
            res = self.skills.craft_gear(item="stone_pickaxe", count=1)
            if not isinstance(res, dict) or not res.get("ok") or res.get("already_owned") or res.get("gained") == 0:
                return False
            owned = _inventory_counts(self.bot).get("stone_pickaxe", 0)
        except _MiningDeadline:
            if scoped is not None:
                # The craft reply may arrive before the final inventory can be sampled.
                self.recrafted_picks += scoped.confirmed_stone_pickaxe_crafts - confirmed_before
            raise
        except (ApiError, ValueError, KeyError, TypeError):
            return False
        if owned <= counts_now.get("stone_pickaxe", 0):
            return False
        self.recrafted_picks += 1
        return True

    def _seal_toward_threat(
        self,
        pos: tuple[int, int, int],
        threat_pos: tuple[float, float, float],
    ) -> int:
        """Wall off the corridor cell facing the threat so melee mobs lose line of sight."""
        dx = float(threat_pos[0]) - float(pos[0])
        dz = float(threat_pos[2]) - float(pos[2])
        step = (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dz) else (0, 1 if dz > 0 else -1)
        if abs(dx) < 0.5 and abs(dz) < 0.5:
            return 0
        cols: list[tuple[int, int, int]] = []
        for dy in (-1, 0, 1):
            base = (pos[0] + step[0], pos[1] + dy, pos[2] + step[1])
            cols.extend([(base[0], base[1] + k, base[2]) for k in (0, 1, 2)])
        return len(self._seal_breach(cols, {}, allow_corridor=True))

    def is_inside_shaft(self, pos: tuple[int, int, int] | None = None) -> bool:
        """True when the bot stands in its own carved works (corridor, vein pocket or node)."""
        if pos is None:
            st = self.bot.status()
            pos = _floor_pos(st.get("pos") or [0, 64, 0])
        for group in (self.known_corridor_positions, self.excavated_positions):
            for c in group:
                if max(abs(pos[0] - c[0]), abs(pos[1] - c[1]), abs(pos[2] - c[2])) <= 1:
                    return True
        for n in self.nodes.values():
            if max(abs(pos[0] - n.anchor[0]), abs(pos[1] - n.anchor[1]), abs(pos[2] - n.anchor[2])) <= 2:
                return True
        return False

    def retreat_into_shaft(
        self,
        *,
        min_hp_ref: list[float] | None = None,
        max_hops: int = 3,
        seal_behind: bool = True,
        threat_pos: tuple[float, float, float] | None = None,
    ) -> dict[str, Any]:
        """Threat response that never leaves our own tunnel: back up the tree and seal the choke point.

        Skeleton mining only stays safe inside the corridor it carved. Free pathfinding flee at
        depth tends to run into open cave air, so prefer retreating along recorded waypoints.
        Returns ok=False when the bot is not inside its own shaft (caller should flee normally).
        """
        if not self.nodes or self.collar_node_id is None:
            return {"ok": False, "mode": "retreat_shaft", "reason": "no_shaft"}
        st = self.bot.status()
        pos = _floor_pos(st.get("pos") or [0, 64, 0])
        inside = self.is_inside_shaft(pos)
        if not inside:
            return {"ok": False, "mode": "retreat_shaft", "reason": "not_in_shaft", "pos": list(pos)}

        hp_ref = min_hp_ref if min_hp_ref is not None else [float(st.get("health") or 20.0)]
        hops = 0
        sealed = 0
        # Break line of sight FIRST: against an adjacent mob, walking backwards while being hit
        # just trades hits until death (observed live). Wall off the threat side, then retreat.
        sealed_first = 0
        if threat_pos is not None:
            sealed_first = self._seal_toward_threat(pos, threat_pos)
            sealed += sealed_first
        cur = self.nodes.get(self.active_node_id) if self.active_node_id is not None else None
        while hops < max(1, int(max_hops)) and cur is not None and cur.node_id != self.collar_node_id:
            parent = self.nodes.get(cur.parent_id) if cur.parent_id is not None else None
            if parent is None:
                break
            ok_walk, err = self._walk_waypoints(
                cur.edge_from_parent.waypoints, reverse=True, min_hp_ref=hp_ref
            )
            if not ok_walk:
                if hops == 0:
                    return {"ok": False, "mode": "retreat_shaft", "reason": err or "retreat_blocked", "pos": list(pos)}
                break
            self.active_node_id = parent.node_id
            hops += 1
            if seal_behind:
                choke = [(cur.anchor[0], cur.anchor[1] + dy, cur.anchor[2]) for dy in (0, 1, 2)]
                sealed += len(self._seal_breach(choke, {}, allow_corridor=True))
            cur = parent

        st_end = self.bot.status()
        return {
            "ok": hops > 0,
            "mode": "retreat_shaft",
            "structure": "carved_shaft",
            "hops": hops,
            "sealed_first": sealed_first,
            "sealed": sealed,
            "pos_before": [round(float(v), 1) for v in pos],
            "pos_after": st_end.get("pos"),
            "requires_replan": hops > 0,
            "note": (
                "沿自建巷道后退并封堵来路：始终留在鱼骨巷道内，不进入未知洞穴。"
                if hops > 0
                else "已在井口节点，无需继续后退。"
            ),
        }

    def _remember_water_interruption(self, trigger: Any, status: dict[str, Any]) -> bool:
        if not isinstance(trigger, dict) or trigger.get("source") != "tool_water_threat":
            return False
        collar = self.nodes.get(self.collar_node_id)
        observation = trigger.get("water_observation")
        life = (status.get("dimension"), status.get("last_death"))
        if (
            collar is None or not isinstance(observation, dict)
            or observation.get("in_water") is not True or trigger.get("observed_life") != life
        ):
            return False
        try:
            water_cell = _floor_pos(observation["pos"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        self._water_interrupted_shaft = _WaterInterruptedShaft(
            collar.anchor, water_cell, life, time.monotonic() + WATER_SHAFT_COOLDOWN_S,
        )
        return True

    def _switch_to_dry_collar_after_preemption(
        self,
        stance: dict[str, Any],
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any,
    ) -> bool:
        wet_shaft = self._water_interrupted_shaft
        if wet_shaft is None:
            return True
        if (
            wet_shaft.until <= time.monotonic()
            or wet_shaft.observed_life != (stance.get("dimension"), stance.get("last_death"))
        ):
            self._water_interrupted_shaft = None
            return True
        collar = self.nodes.get(self.collar_node_id)
        if collar is None or collar.anchor != wet_shaft.collar:
            return True
        x, y, z = cur_pos
        wet_x, wet_y, wet_z = wet_shaft.water_cell
        surface_y = self.collar_ceiling_y if self.collar_ceiling_y is not None else collar.anchor[1]
        if (
            stance.get("on_ground") is not True or stance.get("in_water") or stance.get("in_lava")
            or self._geofence_blocked(geofence, x, z, action="mine_dfs_collar") is not None
        ):
            return False
        offset_x, offset_z = x - wet_x, z - wet_z
        horizontal_distance = math.hypot(offset_x, offset_z)
        local_at_depth = y <= surface_y - 20
        probe_directions = tuple(DIRECTIONS)
        if local_at_depth:
            # A short dry relocation near the working layer should not restart the descent.
            if (
                horizontal_distance < 6.0 or abs(y - wet_y) > 2
                or abs(y - self.target_y) > DEPTH_RESET_THRESHOLD
            ):
                return False
            probe_directions = tuple(d for d, (dx, dz) in DIRECTIONS.items() if dx * offset_x + dz * offset_z >= 0)
        elif horizontal_distance < NODE_REJOIN_DISTANCE:
            return False

        cache: dict[tuple[int, int, int], str] = {}
        try:
            if (
                not self._is_solid_floor((x, y - 1, z), cache)
                or any(self._get_block_name((x, y + dy, z), cache) not in PASSABLE_BLOCKS for dy in (0, 1))
            ):
                return False
            directions = self._probe_collar_dirs(
                cur_pos, geofence=geofence, cache=cache, directions=probe_directions, dry_only=True,
            )
        except ApiError:
            return False
        if not directions:
            return False

        self._archive_current_shaft_to_ledger(mark_exhausted=False)
        self._clear_active_tree_only()
        new_collar = self._init_collar(cur_pos, target_y=self.target_y, geofence=geofence, viable_dirs=directions)
        if local_at_depth:
            new_collar.branches = [branch for branch in new_collar.branches if branch.direction in directions]
        return True

    def _note_fluid_abort(self, reason: str) -> bool:
        """Count aquifer/hazard aborts; True once this shaft looks like a water/cave trap."""
        if reason in FLUID_ABORT_REASONS:
            self.fluid_aborts += 1
        elif reason not in {"time_slice", "completed", "reached_target_y"}:
            self.fluid_aborts = 0
        return self.fluid_aborts >= FLUID_ABORT_LIMIT

    def _continue_descent_around_hazard(
        self,
        node: ShaftNode,
        branch: BranchSpec,
        waypoints: list[tuple[int, int, int]],
        *,
        geofence: Any,
        min_hp_ref: list[float],
    ) -> bool:
        """Keep the verified descent and queue dry side branches when available."""
        end = waypoints[-1]
        if waypoints[0] != node.anchor or end[1] >= node.anchor[1]:
            return False
        branches = self._probe_dry_descent(
            end, directions=LEFT_RIGHT_DIRS[branch.direction], geofence=geofence, min_hp_ref=min_hp_ref,
        )
        if branches is None:
            return False
        self._create_node(
            kind="switchback", anchor=end, parent_id=node.node_id,
            edge_from_parent=ShaftEdge(branch.kind, branch.direction, waypoints), branches=branches,
        )
        return bool(branches)

    def _probe_dry_descent(
        self,
        anchor: tuple[int, int, int],
        *,
        directions: tuple[str, ...],
        geofence: Any,
        min_hp_ref: list[float],
    ) -> list[BranchSpec] | None:
        """Return dry branch candidates, or None if the current stance is unconfirmed."""
        x, y, z = anchor
        status = self.bot.status()
        health = status.get("health")
        if (
            status.get("alive") is False or status.get("ready") is False
            or not status.get("on_ground") or status.get("in_water") or status.get("in_lava")
            or not status.get("pos") or _floor_pos(status["pos"]) != anchor
            or (isinstance(health, (int, float)) and health < min_hp_ref[0])
            or self._geofence_blocked(geofence, x, z, action="mine_dfs_switchback") is not None
        ):
            return None

        cache: dict[tuple[int, int, int], str] = {}
        try:
            if not self._is_solid_floor((x, y - 1, z), cache):
                return None
            if any(self._get_block_name(pt, cache) not in PASSABLE_BLOCKS for pt in (anchor, (x, y + 1, z))):
                return None
        except ApiError:
            return None
        if not directions:
            return []
        viable = self._probe_collar_dirs(
            anchor, geofence=geofence, cache=cache, directions=directions, dry_only=True,
        )
        return [BranchSpec("stair_down", d, y - self.target_y, branch_target_y=self.target_y) for d in viable]

    def _force_relocate_on_fluid(
        self,
        *,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        auto_relocations: int,
    ) -> tuple[bool, int]:
        """Archive a water-logged shaft and open a fresh collar elsewhere.

        Only works at surface level: from depth -50 the candidate scan finds no solid, dry
        surface stand, so the caller is told to climb out first and reopen there.
        """
        cur_now = _floor_pos(self.bot.status()["pos"])
        collar_y: int | None = None
        if self.collar_node_id in self.nodes:
            collar_y = self.nodes[self.collar_node_id].anchor[1]
        if collar_y is not None and cur_now[1] <= collar_y - 6:
            # Live s03: 3 cave breeches at y=8 -> relocation failed -> model reset the tree
            # underground -> explore had no collar and spun on no_progress.
            self.fluid_abandoned_deep = True
            self._archive_current_shaft_to_ledger(mark_exhausted=True)
            self._clear_active_tree_only()
            return False, auto_relocations
        self._archive_current_shaft_to_ledger(mark_exhausted=True)
        if auto_relocations < 1 and time.monotonic() + 8.0 < deadline:
            self._clear_active_tree_only()
            new_site = self._relocate_to_fresh_collar_site(
                cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
            )
            new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
            if new_site != cur_now and new_dirs:
                self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                return True, auto_relocations + 1
        return False, auto_relocations

    def _retry_dry_junction(
        self,
        node: ShaftNode,
        *,
        geofence: Any,
        min_hp_ref: list[float],
    ) -> bool:
        collar = self.nodes.get(self.collar_node_id)
        if collar is None or node.anchor[1] > collar.anchor[1] - 6:
            return False
        if not any(branch.status == "pending" for branch in node.branches):
            return False
        if self._probe_dry_descent(node.anchor, directions=(), geofence=geofence, min_hp_ref=min_hp_ref) is None:
            return False
        self.fluid_aborts = 0
        return True

    def _retry_dry_parent_after_level_abort(
        self, node: ShaftNode, waypoints: list[tuple[int, int, int]],
        *, geofence: Any, min_hp_ref: list[float],
    ) -> bool:
        parent = self.nodes.get(node.parent_id)
        collar = self.nodes.get(self.collar_node_id)
        edge = node.edge_from_parent
        if (
            parent is None or collar is None or edge is None
            or parent.anchor[1] != node.anchor[1] or parent.anchor[1] > collar.anchor[1] - 6
            or not any(branch.status == "pending" for branch in parent.branches)
            or not waypoints or waypoints[0] != node.anchor
            or not edge.waypoints or edge.waypoints[0] != parent.anchor or edge.waypoints[-1] != node.anchor
        ):
            return False

        retreat = [*reversed(waypoints), *reversed(edge.waypoints[:-1])]
        if any(cell[1] != parent.anchor[1] for cell in retreat):
            return False
        if self._probe_dry_descent(retreat[0], directions=(), geofence=geofence, min_hp_ref=min_hp_ref) is None:
            return False
        walked, _ = self._walk_waypoints(retreat, min_hp_ref=min_hp_ref)
        if not walked or not self._retry_dry_junction(parent, geofence=geofence, min_hp_ref=min_hp_ref):
            return False
        self.active_node_id = parent.node_id
        return True

    def _pickaxe_redline_threshold(self, cur_y: int, counts_now: dict[str, int]) -> int:
        if self.collar_node_id in self.nodes:
            collar_y = self.nodes[self.collar_node_id].anchor[1]
            if collar_y - cur_y >= 10 and _has_collar_resupply_materials(counts_now):
                return 12
        return 6

    def _dig_stair_down_edge(
        self,
        *,
        direction: str,
        max_steps: int,
        target_y: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dx, dz = DIRECTIONS[direction]
        waypoints: list[tuple[int, int, int]] = []
        cache: dict[tuple[int, int, int], str] = {}
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints.append(start_anchor)

        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            cur = _floor_pos(st["pos"])
            bx, by, bz = cur
            if by <= target_y:
                stop_reason = "reached_target_y"
                break

            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if self._should_recraft_burner_pickaxe(chosen_rock, total_rem, in_deepslate=(by < 0)) and self._try_recraft_burner_pickaxe(
                counts_now, at_durability_redline=total_rem <= redline,
            ):
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
                chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_stair") is not None:
                stop_reason = "geofence_blocked"
                break

            cur_head = (bx, by + 1, bz)
            head_ok, head_err, head_ores, head_def = self._dig_step_cells(
                [cur_head],
                collect_rock=collect_rock,
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= 1,
            )
            deferred_added += head_def
            mined_ores.extend(head_ores)
            if not head_ok:
                stop_reason = head_err or "dig_failed"
                break

            support_pt = (nx, by - 2, nz)
            try:
                support_blk = self._get_block_name(support_pt, cache)
                support_ok = self._is_solid_floor(support_pt, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            down_targets = [(nx, by + 1, nz), (nx, by, nz), (nx, by - 1, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(down_targets, cache, forward_delta=(dx, dz))
            if hazard is not None or (support_blk == "bedrock" and by - 1 > target_y) or support_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                breach_pts = [(nx, by, nz), (nx, by + 1, nz)]
                sealed = self._seal_breach(breach_pts, cache)
                breaches_sealed += len(sealed)
                stop_reason = hazard[0] if hazard is not None else ("fluid_or_bedrock" if support_blk in FLUID_BLOCKS | {"bedrock"} else "cave_or_drop_ahead")
                break

            nh = st.get("nearest_hostile")
            hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 9.0
            need_bridge = False
            if not support_ok:
                is_self_cav = self._is_self_cavity([support_pt, *down_targets])
                try:
                    below_sup_solid = self._is_solid_floor((nx, by - 3, nz), cache)
                except ApiError:
                    below_sup_solid = False
                can_bridge = is_self_cav or (below_sup_solid and not hostile_near)
                if not can_bridge:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "cave_or_drop_ahead"
                    break
                need_bridge = True

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "fluid_or_bedrock"
                    break
                pre_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(pre_sealed)

            try:
                pre_names = [self._get_block_name(p, cache) for p in down_targets]
            except ApiError:
                stop_reason = "unknown_block"
                break
            if (
                pre_names[1] in PASSABLE_BLOCKS
                and pre_names[2] in PASSABLE_BLOCKS
                and hostile_near
                and not self._is_self_cavity(down_targets[1:])
            ):
                sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                breaches_sealed += len(sealed)
                stop_reason = "hostile_in_cave_ahead"
                break

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                down_targets,
                collect_rock=(collect_rock or need_bridge or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= 1 + len(down_targets),
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break

            if adj_fluids:
                post_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(post_sealed)
                flooded = False
                for dp in down_targets:
                    cache.pop(dp, None)
                    if self._get_block_name(dp, cache) in FLUID_BLOCKS:
                        plug = self._seal_breach([dp], cache)
                        breaches_sealed += len(plug)
                        flooded = True
                if flooded:
                    stop_reason = "fluid_or_bedrock"
                    break

            if need_bridge:
                if not self._place_bridge_block(support_pt, cache) or not self._is_solid_floor(support_pt, cache):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "cave_or_drop_ahead"
                    break

            step_pt = (nx, by - 1, nz)
            move_failure = self._move_excavated_step(
                step_pt, from_cell=cur, head_cells=down_targets[:2], cache=cache,
                deadline=deadline, min_hp=min_hp_ref[0],
            )
            if move_failure is not None:
                stop_reason = move_failure
                break
            steps_done += 1
            if steps_done % 2 == 0 or step_pt[1] <= target_y:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if steps_done % 2 == 0 or step_pt[1] <= target_y:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                    min_hp_ref=min_hp_ref,
                    quota_met=quota_met,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }

    def _dig_stair_up_edge(
        self,
        *,
        direction: str,
        max_steps: int,
        target_y: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Dig safe rock-enclosed ascending staircase toward higher radar ore clusters ('radar' mode)."""
        dx, dz = DIRECTIONS[direction]
        cache: dict[tuple[int, int, int], str] = {}
        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints: list[tuple[int, int, int]] = [start_anchor]
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            bx, by, bz = _floor_pos(st["pos"])
            if by >= target_y:
                stop_reason = "reached_target_y"
                break

            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if self._should_recraft_burner_pickaxe(chosen_rock, total_rem, in_deepslate=(by < 0)) and self._try_recraft_burner_pickaxe(
                counts_now, at_durability_redline=total_rem <= redline,
            ):
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
                chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_stair_up") is not None:
                stop_reason = "geofence_blocked"
                break

            up_cells = [(bx, by + 2, bz), (nx, by + 2, nz), (nx, by + 1, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(up_cells, cache, forward_delta=(dx, dz))
            step_floor = (nx, by, nz)
            try:
                fl_blk = self._get_block_name(step_floor, cache)
                fl_ok = self._is_solid_floor(step_floor, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            if hazard is not None or fl_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                breaches_sealed += len(self._seal_breach([(nx, by + 1, nz), (nx, by + 2, nz)], cache))
                stop_reason = hazard[0] if hazard is not None else "fluid_or_bedrock"
                break

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    stop_reason = "fluid_or_bedrock"
                    break
                breaches_sealed += len(self._seal_breach(adj_fluids, cache))

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                up_cells,
                collect_rock=(collect_rock or not fl_ok or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= len(up_cells),
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break
            if adj_fluids:
                breaches_sealed += len(self._seal_breach(adj_fluids, cache))
            if not fl_ok and (not self._place_bridge_block(step_floor, cache) or not self._is_solid_floor(step_floor, cache)):
                stop_reason = "cave_or_drop_ahead"
                break

            step_pt = (nx, by + 1, nz)
            move_failure = self._move_excavated_step(
                step_pt, from_cell=(bx, by, bz), head_cells=[(nx, by + 2, nz), (nx, by + 1, nz)], cache=cache,
                deadline=deadline, min_hp=min_hp_ref[0],
            )
            if move_failure is not None:
                stop_reason = move_failure
                break
            steps_done += 1
            if self._budget_session is not None:
                self._budget_session.confirmed_ascent_levels.add(step_pt[1])
            if steps_done % 2 == 0 or step_pt[1] >= target_y:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if steps_done % 2 == 0 or step_pt[1] >= target_y:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                    min_hp_ref=min_hp_ref,
                    quota_met=quota_met,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }

    def _dig_level_edge(
        self,
        *,
        direction: str,
        steps: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dx, dz = DIRECTIONS[direction]
        cache: dict[tuple[int, int, int], str] = {}
        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints: list[tuple[int, int, int]] = [start_anchor]
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        for _ in range(steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            cur = _floor_pos(st["pos"])
            bx, by, bz = cur
            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if self._should_recraft_burner_pickaxe(chosen_rock, total_rem, in_deepslate=(by < 0)) and self._try_recraft_burner_pickaxe(
                counts_now, at_durability_redline=total_rem <= redline,
            ):
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
                chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_tunnel") is not None:
                stop_reason = "geofence_blocked"
                break

            floor_pt = (nx, by - 1, nz)
            try:
                floor_blk = self._get_block_name(floor_pt, cache)
                floor_ok = self._is_solid_floor(floor_pt, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            faces = [(nx, by + 1, nz), (nx, by, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(faces, cache, forward_delta=(dx, dz))
            if hazard is not None or floor_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                breaches_sealed += len(sealed)
                stop_reason = hazard[0] if hazard is not None else "fluid_or_bedrock"
                break

            nh = st.get("nearest_hostile")
            hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 9.0
            need_bridge = False
            if not floor_ok:
                is_self_cav = self._is_self_cavity([floor_pt, *faces])
                try:
                    below_fl_solid = self._is_solid_floor((nx, by - 2, nz), cache)
                except ApiError:
                    below_fl_solid = False
                can_bridge = is_self_cav or (below_fl_solid and not hostile_near)
                if not can_bridge:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "unsafe_floor_or_cave"
                    break
                need_bridge = True

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "fluid_or_bedrock"
                    break
                pre_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(pre_sealed)

            try:
                face_names = [self._get_block_name(p, cache) for p in faces]
            except ApiError:
                stop_reason = "unknown_block"
                break
            if face_names[0] in PASSABLE_BLOCKS and face_names[1] in PASSABLE_BLOCKS:
                far_floor = (nx + dx, by - 1, nz + dz)
                is_known_intersection = self._is_self_cavity([*faces, far_floor])
                if hostile_near and not is_known_intersection:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "hostile_in_cave_ahead"
                    break
                if not is_known_intersection:
                    try:
                        if not self._is_solid_floor(far_floor, cache):
                            sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                            breaches_sealed += len(sealed)
                            stop_reason = "cave_breach_sealed"
                            break
                    except ApiError:
                        pass

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                faces,
                collect_rock=(collect_rock or need_bridge or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= len(faces),
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break

            if adj_fluids:
                post_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(post_sealed)
                flooded = False
                for fp in faces:
                    cache.pop(fp, None)
                    if self._get_block_name(fp, cache) in FLUID_BLOCKS:
                        plug = self._seal_breach([fp], cache)
                        breaches_sealed += len(plug)
                        flooded = True
                if flooded:
                    stop_reason = "fluid_or_bedrock"
                    break

            if need_bridge:
                if not self._place_bridge_block(floor_pt, cache) or not self._is_solid_floor(floor_pt, cache):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "unsafe_floor_or_cave"
                    break

            step_pt = (nx, by, nz)
            move_failure = self._move_excavated_step(
                step_pt, from_cell=cur, head_cells=faces, cache=cache,
                deadline=deadline, min_hp=min_hp_ref[0],
            )
            if move_failure is not None:
                stop_reason = move_failure
                break
            steps_done += 1
            if steps_done % 3 == 0:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if target_blocks:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                    min_hp_ref=min_hp_ref,
                    quota_met=quota_met,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }

    # ---- Tree Construction, Radar Pinpoint Homing & Offset Retry ----

    def _create_node(
        self,
        *,
        kind: str,
        anchor: tuple[int, int, int],
        parent_id: int | None,
        edge_from_parent: ShaftEdge | None,
        branches: list[BranchSpec],
    ) -> ShaftNode:
        nid = self.next_node_id
        self.next_node_id += 1
        node = ShaftNode(
            node_id=nid,
            kind=kind,
            anchor=anchor,
            parent_id=parent_id,
            edge_from_parent=edge_from_parent,
            branches=branches,
        )
        self.nodes[nid] = node
        self.active_node_id = nid
        self._record_corridor(anchor)
        for rec in self.deferred_ores.values():
            old_node = self.nodes.get(rec.nearest_node_id)
            if old_node is None or math.dist(rec.pos, anchor) < math.dist(rec.pos, old_node.anchor):
                rec.nearest_node_id = nid
        return node

    def _effective_collar_target_y(self, anchor: tuple[int, int, int]) -> int:
        if self.radar_enabled:
            rt = self._best_radar_target_for_anchor(anchor, at_spine=False)
            if rt is not None and rt[1] < anchor[1] - 2:
                return int(rt[1])
        return self.target_y

    def _collar_starts_at_level(self, anchor: tuple[int, int, int], effective_y: int) -> bool:
        radar_target = self._best_radar_target_for_anchor(anchor, at_spine=True) if self.radar_enabled else None
        return anchor[1] <= effective_y and (radar_target is None or radar_target[1] >= anchor[1] - 2)

    def _collar_branch_plan(
        self,
        anchor: tuple[int, int, int],
        cands: list[str],
        *,
        geofence: Any,
    ) -> tuple[str, list[BranchSpec]]:
        eff_y = self._effective_collar_target_y(anchor)

        # If Collar is initialized directly AT or BELOW target depth (unless radar sees an overhead cluster),
        # attach Spine branches directly so we never execute 0-step stair_down transitions!
        if self._collar_starts_at_level(anchor, eff_y):
            sp_branches = [
                branch for branch in self._spine_branches(cands[0], anchor, geofence)
                if branch.kind == "stair_up" or branch.direction in cands
            ]
            existing_dirs = {b.direction for b in sp_branches if b.kind in {"spine_forward", "branch_leaf"}}
            for extra_d in cands:
                if extra_d not in existing_dirs:
                    sp_branches.append(BranchSpec(kind="branch_leaf", direction=extra_d, steps=8))
            return "spine", sp_branches

        branches: list[BranchSpec] = []
        depth = max(4, anchor[1] - eff_y)
        for d in cands:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_collar") is not None:
                continue
            branches.append(BranchSpec(kind="stair_down", direction=d, steps=depth, branch_target_y=eff_y))
        if not branches:
            branches.append(BranchSpec(kind="stair_down", direction="north", steps=depth, branch_target_y=eff_y))
        return "collar", branches

    def _init_collar(
        self,
        anchor: tuple[int, int, int],
        *,
        target_y: int,
        geofence: Any,
        viable_dirs: list[str] | None = None,
    ) -> ShaftNode:
        cands = viable_dirs or self._probe_collar_dirs(anchor, geofence=geofence)
        kind, branches = self._collar_branch_plan(anchor, cands or ["north", "east", "south", "west"], geofence=geofence)
        node = self._create_node(kind=kind, anchor=anchor, parent_id=None, edge_from_parent=None, branches=branches)
        self.collar_node_id = node.node_id
        self.fluid_aborts = 0
        if kind == "collar":
            self.dry_calls = 0
        self.fluid_abandoned_deep = False
        if self.collar_ceiling_y is None or anchor[1] > self.collar_ceiling_y:
            self.collar_ceiling_y = int(anchor[1])
        return node

    def _has_stale_descent(self, target_y: int, *, observed_layers: set[int] | None = None) -> bool:
        return any(
            branch.status in {"pending", "active"}
            and (branch.kind == "stair_down" or branch.kind == "offset_retry" and branch.resume_kind == "stair_down")
            and branch.branch_target_y is not None
            and target_y - branch.branch_target_y >= LAYER_SWITCH_MIN_DELTA
            and (observed_layers is None or branch.branch_target_y not in observed_layers)
            for node in self.nodes.values()
            for branch in node.branches
        )

    def _replan_shallower_layer(
        self, stance: dict[str, Any], pos: tuple[int, int, int], *,
        target_y: int, geofence: Any, target_blocks: tuple[str, ...] = (), max_tier: int = 0,
    ) -> bool:
        if (
            pos[1] < target_y - SCAN_BAND or stance.get("on_ground") is not True
            or not stance.get("pos") or stance.get("in_water") or stance.get("in_lava")
            or stance.get("alive") is False or stance.get("ready") is False
        ):
            return False
        try:
            if not self._level_walk_is_clear(pos, pos):
                return False
        except ApiError:
            return False

        node = next((node for node in self.nodes.values() if node.anchor == pos), None)
        parent_id = None
        incoming_edge = node.edge_from_parent if node is not None else None
        if node is None:
            for child in self.nodes.values():
                edge, parent = child.edge_from_parent, self.nodes.get(child.parent_id)
                if edge is None or parent is None:
                    continue
                route = self._route_at_position(edge.waypoints, pos, edge.edge_kind)
                if route is not None and route[0][0] == parent.anchor:
                    waypoints, index = route
                    parent_id = parent.node_id
                    incoming_edge = ShaftEdge(edge.edge_kind, edge.direction, waypoints[:index + 1])
                    break

        previous_y = self.target_y
        try:
            self.target_y = target_y
            if target_blocks:
                self._scan_ore_radar(pos, target_blocks=target_blocks, max_tier=max_tier, geofence=geofence, force=True)
            candidates = tuple(d for d in DIRECTIONS if not self._cuts_incoming_stair(pos, incoming_edge, d))
            directions = self._probe_collar_dirs(pos, geofence=geofence, dry_only=True, directions=candidates)
            if not directions:
                return False
            kind, branches = self._collar_branch_plan(pos, directions, geofence=geofence)
        finally:
            self.target_y = previous_y

        if node is None and incoming_edge is not None:
            node = self._create_node(
                kind=kind, anchor=pos, parent_id=parent_id, edge_from_parent=incoming_edge, branches=[],
            )
        if node is None:
            self._archive_current_shaft_to_ledger(mark_exhausted=False)
            self._clear_active_tree_only()
            self.target_y = target_y
            self._init_collar(pos, target_y=target_y, geofence=geofence, viable_dirs=directions)
        else:
            # The checked waypoints remain available for return. Only unfinished work belongs to the old target.
            for saved_node in self.nodes.values():
                saved_node.branches = [branch for branch in saved_node.branches if branch.status == "completed"]
            node.kind = "switchback" if kind == "collar" and node.parent_id is not None else kind
            node.branches.extend(branches)
            self.active_node_id = node.node_id
            self.target_y = target_y
            self.dry_calls = 0
        return True

    def _spine_branches(self, forward_dir: str, anchor: tuple[int, int, int], geofence: Any) -> list[BranchSpec]:
        left_d, right_d = LEFT_RIGHT_DIRS[forward_dir]
        raw_specs: list[tuple[str, str, int]] = [
            ("branch_leaf", left_d, 8),
            ("branch_leaf", right_d, 8),
            ("spine_forward", forward_dir, 4),
        ]

        if not self.radar_enabled:
            out_fb: list[BranchSpec] = []
            # Fishbone has no vertical branch by design (one shaft = one layer). If the
            # caller switched to a deeper target_y mid-shaft, schedule a stair_down first
            # so the tree keeps digging down instead of tunneling sideways forever.
            drop = anchor[1] - self.target_y
            if drop > SPINE_DESCEND_MIN_DELTA:
                for d in (forward_dir, left_d, right_d):
                    dx, dz = DIRECTIONS[d]
                    if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_spine_drop") is not None:
                        continue
                    out_fb.append(
                        BranchSpec(
                            kind="stair_down",
                            direction=d,
                            steps=min(16, max(3, drop)),
                            branch_target_y=self.target_y,
                        )
                    )
                    break
            else:
                # Symmetric case: the shaft overshot below target_y (e.g. diamond layer -> iron layer).
                # Without a stair_up the skeleton would stay buried and never reach the requested layer.
                rise = self.target_y - anchor[1]
                if rise > SPINE_DESCEND_MIN_DELTA:
                    for d in (forward_dir, left_d, right_d):
                        dx, dz = DIRECTIONS[d]
                        if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_spine_climb") is not None:
                            continue
                        out_fb.append(
                            BranchSpec(
                                kind="stair_up",
                                direction=d,
                                steps=min(16, max(3, rise)),
                                branch_target_y=self.target_y,
                            )
                        )
                        break
            for kind, d, ln in raw_specs:
                dx, dz = DIRECTIONS[d]
                if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                    continue
                out_fb.append(BranchSpec(kind=kind, direction=d, steps=ln))
            return out_fb

        rt = self._best_radar_target_for_anchor(anchor, at_spine=True)
        if not self.surgical_enabled:
            # 'v5' mode: bug-fixed original v4 Spine steering (fixed 8/4 step lengths, stair_down only, no stair_up)
            extra_descent: BranchSpec | None = None
            if rt is not None:
                projs_v5 = {d: self._radar_direction_projection(anchor, d, at_spine=True) for d in (left_d, right_d, forward_dir)}
                best_d_v5, best_proj_v5 = max(projs_v5.items(), key=lambda kv: kv[1])
                if best_proj_v5 > 2.0:
                    if best_d_v5 != forward_dir and best_proj_v5 > 7.0:
                        other_lat_v5 = right_d if best_d_v5 == left_d else left_d
                        steer_steps_v5 = min(8, max(4, int(best_proj_v5)))
                        raw_specs = [
                            ("spine_forward", best_d_v5, steer_steps_v5),
                            ("branch_leaf", forward_dir, 6),
                            ("branch_leaf", other_lat_v5, 8),
                        ]
                    else:
                        raw_specs.sort(key=lambda item: (-projs_v5.get(item[1], 0.0)))
                    if rt[1] < anchor[1] - 4:
                        dx_v5, dz_v5 = DIRECTIONS[best_d_v5]
                        if self._geofence_blocked(geofence, anchor[0] + dx_v5 * 3, anchor[2] + dz_v5 * 3, action="mine_dfs_spine_drop") is None:
                            extra_descent = BranchSpec(
                                kind="stair_down",
                                direction=best_d_v5,
                                steps=min(16, max(3, anchor[1] - rt[1])),
                                branch_target_y=int(rt[1]),
                            )
            out_v5: list[BranchSpec] = []
            if extra_descent is not None:
                out_v5.append(extra_descent)
            for kind, d, ln in raw_specs:
                dx, dz = DIRECTIONS[d]
                if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                    continue
                out_v5.append(BranchSpec(kind=kind, direction=d, steps=ln))
            return out_v5

        # 'radar' mode: 3D pinpoint alignment (exact horizontal step clamping + vertical stair_down / stair_up)
        vertical_branch: BranchSpec | None = None
        defer_ascent = False
        if rt is not None:
            projs = {d: self._radar_direction_projection(anchor, d, at_spine=True) for d in (left_d, right_d, forward_dir)}
            best_d, best_proj = max(projs.items(), key=lambda kv: kv[1])
            vy = rt[1] - anchor[1]

            if best_proj >= 1.0:
                align_steps = min(8, max(2, int(round(best_proj))))
                if best_d != forward_dir and best_proj >= 3.0:
                    other_lat = right_d if best_d == left_d else left_d
                    raw_specs = [
                        ("spine_forward", best_d, align_steps),
                        ("branch_leaf", forward_dir, 6),
                        ("branch_leaf", other_lat, 8),
                    ]
                else:
                    adjusted: list[tuple[str, str, int]] = []
                    for kind, d, ln in raw_specs:
                        if d == best_d:
                            adjusted.append(("spine_forward" if kind == "spine_forward" else kind, d, align_steps))
                        else:
                            adjusted.append((kind, d, ln))
                    adjusted.sort(key=lambda item: (-projs.get(item[1], 0.0)))
                    raw_specs = adjusted

            stair_dir = best_d if best_proj > 0 else forward_dir
            dx_r, dz_r = DIRECTIONS[stair_dir]
            if vy < -3 and self._geofence_blocked(geofence, anchor[0] + dx_r * 3, anchor[2] + dz_r * 3, action="mine_dfs_spine_drop") is None:
                vertical_branch = BranchSpec(
                    kind="stair_down",
                    direction=stair_dir,
                    steps=min(16, max(2, anchor[1] - rt[1])),
                    branch_target_y=int(rt[1]),
                )
            elif vy > 3 and self._geofence_blocked(geofence, anchor[0] + dx_r * 3, anchor[2] + dz_r * 3, action="mine_dfs_spine_climb") is None:
                # Stay at the requested depth while closing the horizontal gap to a high, distant cluster.
                defer_ascent = (
                    abs(anchor[1] - self.target_y) <= SCAN_BAND
                    and rt[1] > self.target_y + DEPTH_RESET_THRESHOLD
                    and vy > DEPTH_RESET_THRESHOLD
                    and math.hypot(rt[0] - anchor[0], rt[2] - anchor[2]) > VEIN_HOOK_RADAR_RADIUS
                )
                vertical_branch = BranchSpec(
                    kind="stair_up",
                    direction=stair_dir,
                    steps=min(14, max(2, rt[1] - anchor[1])),
                    branch_target_y=int(rt[1]),
                )

        out: list[BranchSpec] = []
        for kind, d, ln in raw_specs:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                continue
            out.append(BranchSpec(kind=kind, direction=d, steps=ln))
        if vertical_branch is not None:
            out.insert(len(out) if defer_ascent else 0, vertical_branch)
        return out

    def _switchback_branches(self, prev_dir: str, cur_y: int, target_y: int, anchor: tuple[int, int, int], geofence: Any) -> list[BranchSpec]:
        left_d, right_d = LEFT_RIGHT_DIRS[prev_dir]
        cand_dirs = [left_d, right_d]
        if self.radar_enabled:
            cand_dirs.sort(key=lambda d: -self._radar_direction_projection(anchor, d, at_spine=False))
        depth = max(3, cur_y - target_y)
        out: list[BranchSpec] = []
        for d in cand_dirs:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_switchback") is not None:
                continue
            out.append(BranchSpec(kind="stair_down", direction=d, steps=depth, branch_target_y=target_y))
        return out

    @staticmethod
    def _cuts_incoming_stair(anchor: tuple[int, int, int], edge: ShaftEdge | None, direction: str) -> bool:
        if edge is None or edge.edge_kind != "stair_down":
            return False
        x, y, z = anchor
        dx, dz = DIRECTIONS[direction]
        # The offset cuts two blocks high. Either block can be the floor of
        # the return stair, including a step omitted from the waypoint list.
        for step in range(1, OFFSET_RETRY_STEPS + 1):
            for rise in (1, 2):
                stair_cell = (x + dx * step, y + rise, z + dz * step)
                if stair_cell in edge.waypoints or _omitted_waypoint_index(
                    edge.waypoints, stair_cell, edge.edge_kind,
                ) is not None:
                    return True
        return False

    def _enqueue_offset_retry_if_eligible(
        self,
        node: ShaftNode,
        failed_branch: BranchSpec,
        prune_reason: str,
        geofence: Any,
        *,
        after_exhaustion: bool = False,
    ) -> bool:
        if failed_branch.offset_tried or failed_branch.kind != "stair_down" or node.kind == "spine":
            return False
        if prune_reason not in {
            "cave_or_drop_ahead",
            "unsafe_floor_or_cave",
            "cave_breach_sealed",
            "fluid_or_bedrock",
            "falling_material_adjacent",
        } and not (after_exhaustion and prune_reason == "movement_not_confirmed"):
            return False

        left_d, right_d = LEFT_RIGHT_DIRS[failed_branch.direction]
        for lat_d in (left_d, right_d):
            if self._cuts_incoming_stair(node.anchor, node.edge_from_parent, lat_d):
                continue
            lx, lz = DIRECTIONS[lat_d]
            if self._geofence_blocked(
                geofence, node.anchor[0] + lx * OFFSET_RETRY_STEPS,
                node.anchor[2] + lz * OFFSET_RETRY_STEPS, action="mine_dfs_offset",
            ) is not None:
                continue
            already = any(
                b.kind == "offset_retry" and b.direction == failed_branch.direction and b.sidestep_dir == lat_d
                for b in node.branches
            )
            if already:
                continue
            node.branches.append(
                BranchSpec(
                    kind="offset_retry",
                    direction=failed_branch.direction,
                    steps=failed_branch.steps,
                    offset_tried=True,
                    sidestep_dir=lat_d,
                    resume_kind=failed_branch.kind,
                    branch_target_y=failed_branch.branch_target_y,
                )
            )
            return True
        return False

    def _maybe_place_torch_at_node(self, node: ShaftNode) -> bool:
        if node.torch_placed:
            return False
        counts = _inventory_counts(self.bot)
        if counts.get("torch", 0) <= 0:
            return False
        tpos = [node.anchor[0], node.anchor[1] + 1, node.anchor[2]]
        try:
            self.bot.call("place", {"item": "torch", "pos": tpos})
            node.torch_placed = True
            return True
        except ApiError:
            return False

    def _maybe_return_collar_for_resupply(
        self,
        counts_now: dict[str, int],
        *,
        min_hp_ref: list[float],
    ) -> bool:
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            return False
        _, _, sticks = _stone_pickaxe_headroom(counts_now)
        if sticks < 2 or counts_now.get("crafting_table", 0) >= 1:
            return False
        total_planks = sum(v for k, v in counts_now.items() if k.endswith("_planks"))
        total_logs = sum(v for k, v in counts_now.items() if k.endswith(("_log", "_stem")))
        if total_planks >= 4 or total_logs >= 1:
            return False
        if not _has_collar_resupply_materials(counts_now) and self.ledger.nearest_utility("crafting_table", self.nodes[self.collar_node_id].anchor, max_dist=16.0) is None:
            return False
        try:
            ok_ret, reason = self._walk_to_collar(self.collar_node_id, min_hp_ref=min_hp_ref)
        except _MiningDeadline as exc:
            raise _CollarResupplyDeadline from exc
        if reason == "walk_time_slice":
            raise _CollarResupplyDeadline
        return ok_ret

    def _pickaxe_low_recovery(self, counts: dict[str, int], pos: list[float] | None) -> dict[str, Any]:
        _, _, sticks = _stone_pickaxe_headroom(counts)
        if (
            sticks < 2 and pos and self.collar_ceiling_y is not None
            and _floor_pos(pos)[1] <= min(19, self.collar_ceiling_y - 20)
        ):
            climb_target_y = self.collar_ceiling_y - 16
            return {
                "next_action": "climb_out",
                "message": (
                    "当前井下缺少合成新镐的木料。先调用 action='climb_out' 沿已有巷道回撤，"
                    f"本次爬升目标为 y>={climb_target_y}。若收到 climb_progress，可从当前位置继续回撤。"
                    "回到能采木的位置后用 mc_skill_harvest_logs(target_logs=1) 补原木或菌柄，合成可用镐后继续。"
                ),
            }
        return {"message": _pickaxe_low_hint(counts)}

    def _confirmed_straight_descent(
        self, anchor: tuple[int, int, int], direction: str, observations: list[tuple[int, int, int]],
    ) -> list[tuple[int, int, int]]:
        dx, dz = DIRECTIONS[direction]
        route = [anchor]
        for point in observations:
            x, y, z = route[-1]
            if point == (x + dx, y - 1, z + dz) and point in self.known_corridor_positions:
                route.append(point)
        return route

    def _preserve_interrupted_branch(
        self, scoped: _BudgetSession, *, deadline_failure: _MiningDeadline | None,
    ) -> bool:
        node = self.nodes.get(self.active_node_id)
        if node is None:
            return False
        for index, branch in enumerate(node.branches):
            if branch.status == "active":
                break
        else:
            return False

        branch.status = "pending"
        if isinstance(deadline_failure, _CollarResupplyDeadline):
            return True
        if scoped.timeout_path == "dig" and not scoped.dug_count:
            # Give the other branches a first step before retrying an unconfirmed dig.
            node.branches.append(node.branches.pop(index))
        route = scoped.positions
        if node.anchor in route:
            route = route[len(route) - 1 - list(reversed(route)).index(node.anchor):]
            steps_taken = len(route) - 1
        elif (
            branch.kind == "stair_down" and len(route) > 1
            and route[0] in self.known_corridor_positions
            and _descent_start_beside_anchor(route[0], node.anchor, branch.direction)
            and route[-1][1] < node.anchor[1]
            and not (scoped.last_status.get("in_water") or scoped.last_status.get("in_lava"))
            and all(math.dist(a, b) <= math.sqrt(2) and b[1] <= a[1] for a, b in zip(route, route[1:]))
        ):
            # Connect a descent that began one cell away from the saved node.
            steps_taken = node.anchor[1] - route[-1][1]
            route = [node.anchor, *route]
        elif branch.kind == "stair_down" and len(route) > 1:
            # The previous slice can finish two confirmed steps below its saved node.
            dx, dz = DIRECTIONS[branch.direction]
            x, y, z = node.anchor
            skipped_step = (x + dx, y - 1, z + dz)
            skipped_head = (x + dx, y, z + dz)
            status = scoped.last_status
            if (
                route[0] != (x + 2 * dx, y - 2, z + 2 * dz)
                or skipped_head not in self.known_corridor_positions
                or status.get("on_ground") is not True
                or status.get("in_water") or status.get("in_lava")
                or status.get("alive") is False or status.get("ready") is False
                or not status.get("pos") or _floor_pos(status["pos"]) != route[-1]
            ):
                return True
            connected = self._confirmed_straight_descent(node.anchor, branch.direction, [skipped_step, *route])
            if connected[-1] != route[-1]:
                return True
            route = connected
            steps_taken = node.anchor[1] - route[-1][1]
        else:
            return True
        if branch.kind == "stair_down":
            verified_route = _descending_waypoint_prefix(route, node.anchor[1])
            if len(verified_route) < len(route):
                if not verified_route:
                    return True
                status = scoped.last_status
                if deadline_failure is not None and status.get("on_ground") and not (
                    status.get("in_water") or status.get("in_lava")
                    or status.get("alive") is False or status.get("ready") is False
                ):
                    # A pickup or a jump can briefly take the bot off its new stair.
                    # Only skip those observations if every step to the final stance is known.
                    connected = self._confirmed_straight_descent(node.anchor, branch.direction, route[1:])
                    if connected[-1] == route[-1] and connected[-1][1] < verified_route[-1][1]:
                        verified_route = connected
                route = verified_route
                steps_taken = node.anchor[1] - route[-1][1]
        elif branch.kind != "stair_up" and any(pt[1] != node.anchor[1] for pt in route):
            return True
        if steps_taken <= 0 or route[-1] == node.anchor:
            return True
        if deadline_failure is None:
            # External interruption can occur during movement or resupply.
            # Only promote the contiguous, confirmed part of a descending branch.
            status = scoped.last_status
            if (
                branch.kind != "stair_down" or route[-1][1] >= node.anchor[1]
                or status.get("in_water") or status.get("in_lava")
                or status.get("alive") is False or status.get("ready") is False
                or any(pt not in self.known_corridor_positions for pt in route[1:])
                or any(math.dist(a, b) > math.sqrt(2) or b[1] > a[1] for a, b in zip(route, route[1:]))
            ):
                return True
            steps_taken = node.anchor[1] - route[-1][1]
        self._record_partial_branch(node, branch, route, steps_taken)
        return True

    def _record_partial_branch(
        self, node: ShaftNode, branch: BranchSpec, route: list[tuple[int, int, int]], steps_taken: int,
    ) -> None:
        if branch.kind == "offset_retry":
            direction = branch.sidestep_dir or LEFT_RIGHT_DIRS[branch.direction][0]
            dx, dz = DIRECTIONS[direction]
            x, y, z = node.anchor
            if 2 <= steps_taken <= OFFSET_RETRY_STEPS and all(
                pt == (x + step * dx, y, z + step * dz) for step, pt in enumerate(route)
            ):
                self._record_offset_branch(node, branch, route, direction)
            return
        branch.status = "completed"
        self._create_node(
            kind="switchback" if branch.kind.startswith("stair") else "spine",
            anchor=route[-1], parent_id=node.node_id,
            edge_from_parent=ShaftEdge(branch.kind, branch.direction, list(route)),
            branches=[dataclasses.replace(
                branch, status="pending", completed_waypoints=[],
                steps=max(1, branch.steps - steps_taken),
            )],
        )

    def _record_offset_branch(
        self, node: ShaftNode, branch: BranchSpec, route: list[tuple[int, int, int]], direction: str,
    ) -> ShaftNode:
        resume_kind = branch.resume_kind or "stair_down"
        branch.status = "completed"
        return self._create_node(
            kind="spine" if resume_kind == "spine_forward" else "switchback",
            anchor=route[-1], parent_id=node.node_id,
            edge_from_parent=ShaftEdge("offset_sidestep", direction, list(route)),
            branches=[BranchSpec(
                kind=resume_kind, direction=branch.direction, steps=branch.steps,
                offset_tried=True, branch_target_y=branch.branch_target_y,
            )],
        )

    # ---- Public Entrypoint ----

    def run(
        self,
        *,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        max_duration_s: float = 32.0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        budget = max(0.0, min(90.0, float(max_duration_s)))
        deadline = time.monotonic() + budget
        act = str(action or "explore").strip().lower()
        original_bot, original_skills = self.bot, self.skills
        previous_deadline = self._deadline
        previous_budget_session = self._budget_session
        previous_active_geofence = self._active_geofence
        self._active_geofence = geofence
        self.drop_receipts.clear()
        return_dug_before = self.dug_during_return
        recrafted_before = self.recrafted_picks
        _norm_tgt = str(target or "").strip().lower().removeprefix("minecraft:")
        _dig_cap = 16.0 if _norm_tgt in {"obsidian", "crying_obsidian"} else 8.0
        scoped = _BudgetSession(original_bot, deadline - min(0.5, budget / 10), dig_timeout_s=_dig_cap)
        self._deadline = scoped.deadline
        self._budget_session = scoped
        self.bot = scoped.session
        self.skills = copy.copy(original_skills)
        self.skills.bot = self.bot
        try:
            result = self._run(target=target, goal_count=goal_count, target_y=target_y,
                               action=action, geofence=geofence,
                               strategy=strategy, deadline=scoped.deadline)
            if act in {"climb_out", "climb"} and result.get("stop_reason") == "surface_reached":
                result.update(pos_before=scoped.first_pos, pos_after=scoped.last_status.get("pos"))
            self._confirm_climb_slice_progress(result, scoped)
            if act == "explore":
                requested = max(1, min(64, int(goal_count)))
                t_gained = int(result.get("target_gained", 0))
                result["goal_met"] = t_gained >= requested
                result["dug_count"] = scoped.dug_count
                self._track_locked_cluster_yield(result, attempted="target_gained" in result)
                if scoped.last_inventory is not None:
                    profile = _resolve_target_profile(
                        target, target_y, dimension=normalize_dimension(scoped.last_status.get("dimension")),
                    )
                    self._report_target_inventory(
                        result, scoped.last_inventory, profile["expected_items"], requested,
                        target=profile["profile_key"],
                    )
                self._report_explore_depth(
                    result, target_y=self.target_y, budget=budget, ascent_levels=scoped.confirmed_ascent_levels,
                )
            if self.drop_receipts:
                result["target_drop_receipts"] = list(self.drop_receipts.values())
            if scoped.ore_breaks:
                result["ore_breaks"] = scoped.ore_breaks
            return result
        except _MiningLifeChanged:
            result = {
                "ok": False,
                "requires_replan": True,
                "action": act,
                "strategy": self.strategy,
                "stop_reason": "life_changed",
                "pos_before": scoped.first_pos,
                "pos_after": scoped.last_status.get("pos"),
                "message": "本轮因生命状态或维度变化结束。请先确认当前位置和背包，再决定如何继续。",
                "tree": self.snapshot(),
            }
            if scoped.ore_breaks:
                result["ore_breaks"] = scoped.ore_breaks
            return result
        except _MiningDeadline as exc:
            resupply_interrupted = isinstance(exc, _CollarResupplyDeadline)
            moved = len(scoped.positions) >= 2 and scoped.positions[-1] != scoped.positions[0]
            if scoped.timeout_dig_pos is not None and scoped.timeout_dig_read_s >= 1.0:
                last_pos = scoped.last_status.get("pos")
                stalled_at_feet = bool(last_pos and _floor_pos(last_pos) == scoped.timeout_dig_pos)
                # A stall at the current feet can repeat after productive excavation.
                if not scoped.dug_count or stalled_at_feet:
                    self.skills._avoid_temporarily(scoped.timeout_dig_pos, "dig_timeout", DIG_TIMEOUT_COOLDOWN_S)
            self._preserve_interrupted_branch(scoped, deadline_failure=exc)
            self._archive_current_shaft_to_ledger(mark_exhausted=False)
            result: dict[str, Any] = {
                "ok": False, "requires_replan": True, "action": act,
                "strategy": self.strategy, "stop_reason": "time_slice",
                "pos_before": scoped.first_pos, "pos_after": scoped.last_status.get("pos"),
                "tree": self.snapshot(),
            }
            if scoped.timeout_path is not None:
                result["timeout_path"] = scoped.timeout_path
            if scoped.timeout_dig_pos is not None:
                result["dig_target"] = list(scoped.timeout_dig_pos)
            if act == "explore":
                gained = {}
                if scoped.first_inventory is not None and scoped.last_inventory is not None:
                    gained = _positive_inv_delta(
                        _inventory_counts_from_raw(scoped.first_inventory),
                        _inventory_counts_from_raw(scoped.last_inventory),
                    )
                profile = _resolve_target_profile(
                    target, target_y, dimension=normalize_dimension(scoped.last_status.get("dimension")),
                )
                target_gained = sum(gained.get(item, 0) for item in profile["expected_items"])
                requested = max(1, min(64, int(goal_count)))
                recrafted = self.recrafted_picks - recrafted_before
                result.update(
                    goal_met=target_gained >= requested,
                    goal_count=requested,
                    target_gained=target_gained,
                    gained=gained,
                    dug_count=scoped.dug_count,
                    recrafted_picks=recrafted,
                )
                self._track_locked_cluster_yield(
                    result, attempted=bool(scoped.dug_count or scoped.timeout_dig_read_s >= 1.0),
                )
                self._report_target_inventory(
                    result, scoped.last_inventory, profile["expected_items"], requested,
                    target=profile["profile_key"],
                )
                if resupply_interrupted:
                    result.update(
                        stop_reason="pickaxe_low", next_action="return_collar", budget_effective_s=budget,
                        message="镐耐久不足，回井口补给途中达到时间上限。请继续 action='return_collar'，补好镐后再 explore。",
                    )
                else:
                    made_progress = bool(moved or scoped.dug_count or gained or recrafted)
                    if (
                        scoped.timeout_dig_pos is not None and scoped.timeout_dig_read_s >= 1.0
                        and not (scoped.dug_count or gained or recrafted)
                        and len(self.nodes) == 1 and self.collar_node_id in self.nodes
                        and scoped.first_pos and result["pos_after"]
                        and _floor_pos(scoped.first_pos) == _floor_pos(result["pos_after"])
                    ):
                        # A timed-out first dig bypasses the normal end-of-slice idle check.
                        self.idle_calls += 1
                    elif made_progress:
                        self.idle_calls = 0
                    if made_progress:
                        result.update(
                            ok=True,
                            requires_replan=False,
                            stop_reason="quota_reached" if result["goal_met"] else "time_slice_progress",
                            message="本轮已保留确认的采掘进展和回撤路径，可以继续 explore。",
                        )
                    self._report_explore_depth(
                        result, target_y=int(profile["target_y"]), budget=budget,
                        ascent_levels=scoped.confirmed_ascent_levels,
                    )
            if act in {"climb_out", "climb"} and scoped.positions:
                start_y, end_y = scoped.positions[0][1], scoped.positions[-1][1]
                target_height = int(self.collar_ceiling_y) - 16 if self.collar_ceiling_y is not None else start_y
                minimum_y = max(start_y, target_height - CLIMB_HEIGHT_TOLERANCE)
                status = scoped.last_status
                in_fluid = status.get("in_water") or status.get("in_lava")
                reached = self._climb_target_reached(status, minimum_y)
                result["climb"] = {
                    "start_y": start_y, "end_y": end_y, "climbed": end_y - start_y,
                    "target_y": target_height, "reached": reached,
                    "minimum_y": minimum_y,
                    "recorded_collar_y": self.collar_ceiling_y,
                    "method": "interrupted",
                }
                progressed_inward = self._climb_fence_progress(result["climb"])
                progressed_on_route = self._climb_recorded_route_progress(result["climb"])
                if reached:
                    self._detach_retired_shaft()
                    result.update(
                        ok=True, requires_replan=False, stop_reason="surface_reached",
                        message=self._climb_arrival_message(result["climb"]),
                        tree=self.snapshot(),
                    )
                elif (end_y > start_y or progressed_inward or progressed_on_route) and not in_fluid:
                    result.update(
                        stop_reason="climb_progress", next_action="climb_out",
                        message=self._climb_progress_message(result["climb"]),
                    )
                    remaining_height = result["climb"]["minimum_y"] - end_y
                    if 20 <= budget < 55 and remaining_height > end_y - start_y:
                        result["suggested_next"] = {"retry_with": {"max_duration_s": 55}}
                        result["message"] += "若本集时间允许，可保留其余参数，将 max_duration_s 调到 55 秒。"
                    self._confirm_climb_slice_progress(result, scoped)
            if act == "return_collar" and self.collar_node_id in self.nodes:
                collar = self.nodes[self.collar_node_id].anchor
                result["collar_anchor"] = list(collar)
                result["dug_during_return"] = self.dug_during_return - return_dug_before
                status = scoped.last_status
                if (
                    moved and not (status.get("in_water") or status.get("in_lava"))
                    and status.get("alive") is not False and status.get("ready") is not False
                    and math.dist(scoped.positions[-1], collar) < math.dist(scoped.positions[0], collar)
                ):
                    result.update(
                        requires_replan=False, stop_reason="return_collar_progress", next_action="return_collar",
                        message="本轮已沿矿道向井口回撤。请从当前站位继续 action='return_collar'。",
                    )
            if result["stop_reason"] == "time_slice":
                next_steps: dict[str, Any] = {
                    "if_blocked": "处理前方障碍，或换到安全站位后重新规划。",
                }
                result["message"] = "本轮因时间限制中止。请先检查站位及障碍，避免原地用相同参数重试。"
                if budget < 90:
                    next_steps["retry_with"] = {"max_duration_s": max(20, min(90, math.ceil(budget + 15)))}
                    result["message"] += "若路线畅通，可按 retry_with 调大预算并保留其余参数。"
                result["suggested_next"] = next_steps
            if self.drop_receipts:
                result["target_drop_receipts"] = list(self.drop_receipts.values())
            if scoped.ore_breaks:
                result["ore_breaks"] = scoped.ore_breaks
            return result
        except BaseException as exc:
            if act == "explore":
                preserved = self._preserve_interrupted_branch(scoped, deadline_failure=None)
                wet_shaft = self._remember_water_interruption(getattr(exc, "trigger", None), scoped.last_status)
                if preserved or wet_shaft:
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
            raise
        finally:
            self.bot, self.skills = original_bot, original_skills
            self._deadline = previous_deadline
            self._budget_session = previous_budget_session
            self._active_geofence = previous_active_geofence
            # Stop a request that may still be running server-side, within the reserved
            # portion of the caller's budget. Never reconnect or reauthenticate here.
            stop_deadline = min(deadline, scoped.pickup_stop_deadline or deadline)
            remaining = stop_deadline - time.monotonic()
            if remaining > 0.2 and (scoped.timeout_path is not None or time.monotonic() >= scoped.deadline - 0.2):
                try:
                    scoped.deadline = stop_deadline
                    scoped.call("stop", {}, timeout_s=0.25)
                except (_MiningDeadline, ApiError, httpx.HTTPError):
                    pass

    def _track_locked_cluster_yield(self, result: dict[str, Any], *, attempted: bool) -> None:
        if result.get("target_gained", 0) > 0:
            self._last_locked_rep = None
            self._locked_rep_dry_calls = 0
            return
        if not attempted:
            return
        raw_rep = self.locked_cluster.get("rep") if self.locked_cluster else None
        if not raw_rep:
            self._last_locked_rep = None
            self._locked_rep_dry_calls = 0
            return
        rep = _floor_pos(raw_rep)
        if rep != self._last_locked_rep:
            self._last_locked_rep = rep
            self._locked_rep_dry_calls = 0
            self._locked_rep_best_distance = math.inf

        before, after = result.get("pos_before"), result.get("pos_after")
        if isinstance(after, (list, tuple)) and len(after) >= 3:
            distance = math.dist(after[:3], rep)
            previous_best = self._locked_rep_best_distance
            self._locked_rep_best_distance = min(previous_best, distance)
            if isinstance(before, (list, tuple)) and len(before) >= 3:
                # Rewalking the same approach after being moved away does not find a new mining stance.
                if distance < min(previous_best, math.dist(before[:3], rep)) - 2.0:
                    self._locked_rep_dry_calls = 0
                    return
        self._locked_rep_dry_calls += 1
        if self._locked_rep_dry_calls >= 2:
            self._mark_ore_unreachable(rep, "stalled_locked_cluster")
            self._last_locked_rep = None
            self._locked_rep_dry_calls = 0
            result["radar_evicted_cluster"] = list(rep)
            result["tree"] = self.snapshot()
            if isinstance(result.get("radar_summary"), dict):
                result["radar_summary"]["locked_cluster"] = None
                result["radar_summary"]["cluster_size"] = 0

    @staticmethod
    def _report_target_inventory(
        result: dict[str, Any],
        inventory: dict[str, Any] | None,
        expected_items: tuple[str, ...],
        requested: int,
        *,
        target: str,
    ) -> None:
        if inventory is None:
            return
        counts = _inventory_counts_from_raw(inventory)
        target_items = {item: counts[item] for item in expected_items if counts.get(item, 0) > 0}
        total = sum(target_items.values())
        result["target_inventory_items"] = target_items
        result["target_inventory_count"] = total
        result["target_inventory_meets_goal"] = total >= requested
        if target == "iron":
            ingots, raw_iron = counts.get("iron_ingot", 0), counts.get("raw_iron", 0)
            result["iron_pickaxe_materials"] = {
                "iron_ingot": ingots,
                "raw_iron": raw_iron,
                "additional_iron_needed": max(0, 3 - ingots - raw_iron),
            }

    @staticmethod
    def _report_explore_depth(
        result: dict[str, Any], *, target_y: int, budget: float, ascent_levels: set[int],
    ) -> None:
        result["budget_effective_s"] = budget
        before, after = result.get("pos_before"), result.get("pos_after")
        if not before or not after:
            return
        start_y, end_y = _floor_pos(before)[1], _floor_pos(after)[1]
        depth_reference = max(start_y, target_y)
        planned_ascent = sum(depth_reference < y <= end_y for y in ascent_levels)
        if planned_ascent:
            result["planned_ascent_blocks"] = planned_ascent
        if result["goal_met"]:
            return
        if end_y > depth_reference + planned_ascent + DEPTH_RESET_THRESHOLD:
            result["progress_lost"] = True
            result["depth_lost"] = end_y - start_y
            if result.get("stop_reason") in {"time_slice_progress", "movement_not_confirmed"}:
                result.update(
                    ok=False, requires_replan=True, stop_reason="depth_reset",
                    message=f"本轮从 y={start_y} 回升到了 y={end_y}，目标层是 y={target_y}。请先检查下行路线再继续。",
                )
        elif result.get("stop_reason") == "time_slice_progress" and end_y < start_y and end_y - target_y >= 32:
            next_budget = min(90, max(60, math.ceil(30 + 0.4 * (end_y - target_y))))
            if next_budget > budget:
                result["suggested_next"] = {"retry_with": {"max_duration_s": next_budget}}
                result["message"] = (
                    f"本轮已下挖到 y={end_y}，距离目标层还有 {end_y - target_y} 格。"
                    f"下一轮可保留其余参数，将 max_duration_s 调到 {next_budget} 秒。"
                )

    def _collar_failure(
        self, failure: dict[str, Any] | None, pos: tuple[int, int, int], geofence: Any,
    ) -> dict[str, Any]:
        details = dict(failure or {})
        reason = details.pop("stop_reason", "no_viable_collar_direction")
        fence_error = self._geofence_blocked(geofence, pos[0], pos[2], action="mine_dfs_collar")
        if fence_error is not None:
            reason = "void_geofence_blocked"
            message = "当前站位超出安全范围，请核对坐标和岛中心后再进入安全区。"
            details.clear()
        elif reason == "no_viable_collar_direction":
            offset = next(
                (blocker["safe_side_step"] for blocker in details.get("collar_blockers", {}).values()
                 if "safe_side_step" in blocker),
                None,
            )
            if offset is not None:
                details["suggested_recovery_pos"] = offset
                message = f"当前站位无法安全下挖。可先尝试向同层有支撑的 {offset} 短移，确认落脚后再探查井口。"
            else:
                message = "当前站位附近没有可安全开挖的方向，请按方向受阻信息探查另一处实心站位再 explore。"
        else:
            message = "当前站位无法确认有实心支撑，请先移到安全的实心地面再 explore。"
        result: dict[str, Any] = {
            "ok": False,
            "requires_replan": True,
            "action": "explore",
            "strategy": self.strategy,
            "stop_reason": reason,
            "message": message,
            "pos_after": list(pos),
            "tree": self.snapshot(),
            **details,
        }
        if fence_error is not None:
            result["error"] = fence_error["error"]
        return result

    def _layer_switch_refusal(self, status: dict[str, Any], *, message: str | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "requires_replan": True,
            "action": "explore",
            "strategy": self.strategy,
            "stop_reason": "layer_switch_upward_refused",
            "pos_before": status.get("pos"),
            "pos_after": status.get("pos"),
            "message": message or "当前站位无法确认新层的安全下行或同层路线。请先沿旧井回撤到有支撑的干燥站位，再切换目标层。",
            "tree": self.snapshot(),
        }

    def _deep_shaft_rejoin_failure(self) -> dict[str, Any]:
        scoped = self._budget_session
        if self._has_stale_descent(self.target_y, observed_layers={point[1] for point in self.radar_targets}):
            return self._layer_switch_refusal(scoped.last_status if scoped is not None else {})
        return {
            "ok": False,
            "requires_replan": True,
            "action": "explore",
            "strategy": self.strategy,
            "stop_reason": "needs_surface_climb",
            "pos_before": scoped.first_pos if scoped is not None else None,
            "pos_after": scoped.last_status.get("pos") if scoped is not None else None,
            "message": (
                "当前深度附近的待挖节点暂时无法接上。请先在同层调整站位，再继续 explore。"
                "如果无法安全继续，可调用 climb_out 沿已记矿道回撤。"
            ),
            "tree": self.snapshot(),
        }

    def _run(
        self,
        *,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        strategy: str | None = None,
        deadline: float,
    ) -> dict[str, Any]:
        self._refresh_strategy(strategy)
        act = str(action or "explore").strip().lower()
        if act == "reset":
            return self.reset(geofence=geofence)
        if act in {"climb_out", "climb"}:
            climb = self._climb_out(deadline=deadline)
            climb["recorded_collar_y"] = self.collar_ceiling_y
            progressed_inward = self._climb_fence_progress(climb)
            progressed_on_route = self._climb_recorded_route_progress(climb)
            made_progress = climb["climbed"] > 0 or progressed_inward or progressed_on_route
            ok_climb = bool(climb["reached"])
            # Never hand control back while the bot is floating: live n0152 drowned during the
            # model's next (empty) turns after unstick left it in_water.
            wet_after = False
            try:
                if bool(self.bot.status().get("in_water")):
                    esc = self.skills.escape_water()
                    wet_after = bool(esc.get("in_water"))
                    if wet_after:
                        self.skills.unstick()
                        wet_after = bool(self.bot.status().get("in_water"))
            except (ApiError, ValueError, KeyError):
                wet_after = False
            if wet_after:
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": "climb_out",
                    "strategy": self.strategy,
                    "stop_reason": "in_water",
                    "climb": climb,
                    "message": "爬升途中仍在水中，请先用 staircase(vertical='up') 或 unstick 上岸/离开水域，再继续。",
                    "tree": self.snapshot(),
                }
            result = {
                "ok": ok_climb,
                "requires_replan": not ok_climb,
                "action": "climb_out",
                "strategy": self.strategy,
                "stop_reason": "surface_reached" if ok_climb else (
                    "climb_progress" if made_progress else "climb_blocked"
                ),
                "climb": climb,
                "message": (
                    self._climb_arrival_message(climb)
                    if ok_climb
                    else (self._climb_progress_message(climb)
                          if made_progress else "所有爬升方向均无法推进，请先处理障碍或换位置，避免原地重复 climb_out。")
                ),
                "tree": self.snapshot(),
            }
            if not ok_climb and made_progress:
                result["next_action"] = "climb_out"
            return result
        if act == "status":
            return {"ok": True, "action": "status", "strategy": self.strategy, "tree": self.snapshot()}

        st0 = self.bot.status()
        if st0.get("alive") is False:
            return {"ok": False, "requires_replan": True, "strategy": self.strategy, "stop_reason": "dead", "tree": self.snapshot()}
        raw_dimension = st0.get("dimension")
        dimension = normalize_dimension(raw_dimension)
        profile = _resolve_target_profile(target, target_y, dimension=dimension)
        if act == "explore" and raw_dimension:
            ecology = assess_world_target(profile["target_blocks"], dimension=dimension, mode="find")
            if ecology["verdict"] == "wrong_dimension":
                available_targets = {}
                if dimension == "the_nether":
                    available_targets = {
                        target_name: ORE_DFS_PROFILES[profile_key]["default_target_y"]
                        for target_name, profile_key in (
                            ("ancient_debris", "ancient_debris"),
                            ("quartz", "quartz"),
                            ("gold", "nether_gold_ore"),
                        )
                    }
                hints = [f"{name} (Y={y})" for name, y in available_targets.items()]
                message = ecology["hint"]
                if hints:
                    message += " DFS 可改采 " + "、".join(hints) + "。"
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "bad_target_for_dimension",
                    "target": profile["profile_key"],
                    "dimension": dimension,
                    "allowed_dimensions": ecology["allowed_dimensions"],
                    "available_targets": available_targets,
                    "message": message,
                    "pos_after": st0.get("pos"),
                    "tree": self.snapshot(),
                }
            if dimension == "the_nether" and target_y is not None and profile["target_y"] < 0:
                suggested_y = ORE_DFS_PROFILES.get(profile["profile_key"], {}).get("default_target_y")
                message = "下界 Y=0 以下没有可采掘的地形，请改用非负的 target_y。"
                if suggested_y is not None:
                    message += f"当前目标可先试 Y={suggested_y}。"
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "bad_target_y_for_dimension",
                    "target": profile["profile_key"],
                    "dimension": dimension,
                    "target_y": profile["target_y"],
                    "minimum_y": 0,
                    "suggested_target_y": suggested_y,
                    "message": message,
                    "pos_before": st0.get("pos"),
                    "pos_after": st0.get("pos"),
                    "tree": self.snapshot(),
                }
        if act == "explore" and self._active_shaft_is_retired():
            ceiling = self.collar_ceiling_y
            if ceiling is None:
                ceiling = self.nodes[self.collar_node_id].anchor[1]
            if not self._climb_target_reached(st0, ceiling - 16 - CLIMB_HEIGHT_TOLERANCE):
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "needs_surface_climb",
                    "next_action": "climb_out",
                    "pos_before": st0.get("pos"),
                    "pos_after": st0.get("pos"),
                    "message": "当前矿井已枯竭，请沿已记路线继续回撤。" + self._surface_climb_hint(),
                    "tree": self.snapshot(),
                }
            self._clear_active_tree_only()
        requested_y = int(profile["target_y"])
        shallower_layer = (
            act == "explore"
            and self.collar_node_id in self.nodes
            and requested_y - self.target_y >= LAYER_SWITCH_MIN_DELTA
        )
        if shallower_layer:
            try:
                cur_y = _floor_pos(st0.get("pos") or [0, 64, 0])[1]
            except (ApiError, TypeError, ValueError):
                cur_y = self.target_y
            # A paused descent still has its original target, even before it reaches that layer.
            if cur_y < requested_y - SCAN_BAND:
                return self._layer_switch_refusal(
                    st0,
                    message=(
                        f"当前位置 y={cur_y} 已低于新目标层 y={requested_y}，旧井仍保存 y={self.target_y} 的路线。"
                        "要换到浅层，请先用 action='return_collar' 或 'climb_out' 主动回撤，再以新目标开井。"
                        "要沿当前井继续下探，请使用原目标层。"
                    ),
                )
        active = self.nodes.get(self.active_node_id)
        replan_layer = shallower_layer and (
            self._has_stale_descent(requested_y)
            or active is not None and active.anchor[1] < requested_y - SCAN_BAND
        )
        if act == "explore" and not replan_layer:
            self.target_y = requested_y
        target_blocks = profile["target_blocks"]
        expected_items = profile["expected_items"]
        goal_count = max(1, min(64, int(goal_count)))

        recrafted_before = self.recrafted_picks
        if st0.get("in_water"):
            esc = self.skills.escape_water()
            if esc.get("in_water"):
                # Second attempt: unstick nudges the hitbox/jumps out of shallow water.
                try:
                    self.skills.unstick()
                except (ApiError, ValueError, KeyError):
                    pass
                st_w = self.bot.status()
                if st_w.get("in_water"):
                    # Only a flooded MINING layer condemns the shaft. A puddle/pond on the way
                    # down is a detour, not a dead shaft (live s11 churned 3 shafts at y=54).
                    try:
                        wet_y = _floor_pos(st_w["pos"])[1]
                    except (KeyError, TypeError, ValueError):
                        wet_y = self.target_y
                    if wet_y > int(self.target_y) + 12:
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "strategy": self.strategy,
                            "stop_reason": "in_water",
                            "water_escape": esc,
                            "message": (
                                "当前仍在水中，挖矿无法开始（reset 无效）。请先用 "
                                "mc_skill_staircase(vertical='up') 或 mc_skill_flee 离开水域/爬上岸，"
                                "然后直接 explore。"
                            ),
                        }
                    if self._note_fluid_abort("in_water"):
                        self._archive_current_shaft_to_ledger(mark_exhausted=True)
                        self._clear_active_tree_only()
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "action": act,
                            "strategy": self.strategy,
                            "stop_reason": "shaft_exhausted",
                            "water_escape": esc,
                            "message": (
                                "这口井已连续 3 次因溺水/水患中止，判定为水淹矿井并归档。"
                                + self._surface_climb_hint()
                            ),
                            "tree": self.snapshot(),
                        }
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "strategy": self.strategy,
                        "stop_reason": "in_water",
                        "water_escape": esc,
                        "message": (
                            "当前仍在水中，挖矿无法开始（reset 无效）。请先用 "
                            "mc_skill_staircase(vertical='up') 或 mc_skill_flee 离开水域/爬上岸，"
                            "然后直接 explore。"
                        ),
                    }
                st0 = st_w
            else:
                st0 = self.bot.status()

        cur_pos = _floor_pos(st0.get("pos") or [0, 64, 0])
        min_hp_ref = [float(st0.get("health") if isinstance(st0.get("health"), (int, float)) else 20.0)]

        if act == "return_collar":
            if self.collar_node_id is None or self.collar_node_id not in self.nodes:
                return {"ok": False, "requires_replan": True, "strategy": self.strategy, "stop_reason": "no_collar_recorded", "tree": self.snapshot()}
            dug_before = self.dug_during_return
            ok_ret, err_ret = self._walk_to_collar(
                self.collar_node_id,
                min_hp_ref=min_hp_ref,
                deadline=deadline,
            )
            if err_ret == "walk_time_slice":
                raise _MiningDeadline
            st_after = self.bot.status()
            hp_after = st_after.get("health")
            collar_anchor = self.nodes[self.collar_node_id].anchor
            result = {
                "ok": ok_ret,
                "requires_replan": not ok_ret,
                "action": "return_collar",
                "strategy": self.strategy,
                "stop_reason": "returned_to_collar" if ok_ret else (err_ret or "return_failed"),
                "collar_reached": ok_ret,
                "pos_before": st0.get("pos"),
                "pos_after": st_after.get("pos"),
                "collar_anchor": list(collar_anchor),
                "dug_during_return": self.dug_during_return - dug_before,
                "tree": self.snapshot(),
            }
            if (
                not ok_ret and (err_ret or "").startswith("backtrack_blocked@")
                and st_after.get("on_ground") is True
                and self._climb_target_reached(st_after, cur_pos[1])
                and (not isinstance(hp_after, (int, float)) or hp_after >= min_hp_ref[0])
            ):
                end = _floor_pos(st_after["pos"])
                if math.dist(end, collar_anchor) < math.dist(cur_pos, collar_anchor):
                    result.update(ok=True, stop_reason="return_collar_progress", rejoin_error=err_ret)
                if end[1] < collar_anchor[1]:
                    result["message"] = (
                        "已确认当前站位，记录的下一段回程受阻。若只需出井，可调用 action='climb_out' 查找安全上行路线。"
                        "若需要回原井口补给，请先按回执中的阻塞坐标核对踏点与绕路。"
                    )
            return result

        if act != "explore":
            return {"ok": False, "error": "bad_args", "message": f"未知 action={action!r}，支持 explore / return_collar / reset / status。"}

        inv0, counts0 = _inventory_snapshot_and_counts(self.bot)
        picks0, max_tier0, total_rem0 = _pickaxe_inventory_summary(inv0)
        if total_rem0 <= 6 and self._try_recraft_burner_pickaxe(counts0, at_durability_redline=True):
            # Entry gate used to bail out before any recraft attempt, which made the model walk back
            # to the collar, find no tools there either, and loop (live: 3x returned_to_collar).
            inv0, counts0 = _inventory_snapshot_and_counts(self.bot)
            picks0, max_tier0, total_rem0 = _pickaxe_inventory_summary(inv0)
        if not picks0 or total_rem0 <= 6:
            recovery = self._pickaxe_low_recovery(counts0, st0.get("pos"))
            recovery["message"] = "可用镐缺失或总剩余耐久过低。" + recovery["message"]
            return {
                "ok": False,
                "requires_replan": True,
                "strategy": self.strategy,
                "stop_reason": "pickaxe_low",
                "pos_after": st0.get("pos"),
                "tree": self.snapshot(),
                **recovery,
            }
        if max_tier0 < int(profile["min_tier"]):
            return {
                "ok": False,
                "requires_replan": True,
                "strategy": self.strategy,
                "stop_reason": "insufficient_pickaxe_tier",
                "required_tier": int(profile["min_tier"]),
                "current_max_tier": max_tier0,
                "message": f"MiningDFS 目标 {profile['profile_key']} 需要至少等级 {profile['min_tier']} 的镐，当前背包最高镐等级为 {max_tier0}，请先升镐或改采低阶矿。",
                "tree": self.snapshot(),
            }

        if not replan_layer:
            self._scan_ore_radar(cur_pos, target_blocks=target_blocks, max_tier=max_tier0, geofence=geofence, force=True)

        if not self._switch_to_dry_collar_after_preemption(st0, cur_pos, geofence=geofence):
            return {
                "ok": False,
                "requires_replan": True,
                "action": act,
                "strategy": self.strategy,
                "stop_reason": "water_reentry_avoided",
                "pos_before": st0.get("pos"),
                "pos_after": st0.get("pos"),
                "message": "旧矿道刚被积水中断。请沿干地离开进水点，选一个有支撑的干燥入口再 explore。",
                "tree": self.snapshot(),
            }

        # A failed restore leaves its return route loaded. Recheck the queued descent on the next explore too.
        needs_layer_plan = replan_layer or self._has_stale_descent(
            requested_y, observed_layers={point[1] for point in self.radar_targets},
        )
        if needs_layer_plan and not self._replan_shallower_layer(
            st0, cur_pos, target_y=requested_y, target_blocks=target_blocks if replan_layer else (),
            max_tier=max_tier0, geofence=geofence,
        ):
            return self._layer_switch_refusal(st0)

        # Tree initialization, WorldLedger shaft restoration, or resume from Collar / current node
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            # Restore exactly once: this call mutates state, so it must not sit inside a boolean.
            restored = self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref)
            if (
                not restored
                and self.collar_node_id not in self.nodes
                and self.collar_ceiling_y is not None
                and cur_pos[1] <= int(self.collar_ceiling_y) - 20
            ):
                # Live s01/s03: after a deep reset this path opened a "collar" at y=-54,
                # so every later reset looked legitimate and the run churned underground.
                # An explore at or above the target layer must not spend its
                # slice digging away from it when no shaft can be restored.
                if cur_pos[1] >= self.target_y - SCAN_BAND or time.monotonic() + 6.0 >= deadline:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "action": act,
                        "strategy": self.strategy,
                        "stop_reason": "needs_surface_climb",
                        "next_action": "climb_out",
                        "pos_after": st0.get("pos"),
                        "message": self._surface_climb_hint(),
                        "tree": self.snapshot(),
                    }
                climb = self._climb_out(deadline=deadline)
                if not climb["reached"]:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "action": act,
                        "strategy": self.strategy,
                        "stop_reason": "needs_surface_climb",
                        "climb": climb,
                        "message": (
                            f"已尝试自动爬升，y {climb['start_y']}→{climb['end_y']}"
                            f"（目标 y={climb['target_y']}）。" + self._surface_climb_hint()
                        ),
                        "tree": self.snapshot(),
                    }
                cur_pos = _floor_pos(self.bot.status().get("pos") or cur_pos)
                restored = self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref)
            if not restored:
                if self.collar_node_id in self.nodes:
                    return self._deep_shaft_rejoin_failure()
                solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                    cur_pos,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                )
                if not viable_dirs:
                    return self._collar_failure(anchor_err, solid_pos, geofence)
                self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
        else:
            collar = self.nodes[self.collar_node_id]
            active = self.nodes.get(self.active_node_id or -1)
            if not self._tree_has_pending_branches():
                self._archive_current_shaft_to_ledger(mark_exhausted=True)
                self._clear_active_tree_only()
                solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                    cur_pos,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                )
                if anchor_err is not None and not viable_dirs:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "strategy": self.strategy,
                        "stop_reason": "shaft_exhausted",
                        "message": "当前矿井已枯竭且附近无安全新井落点，请先用 mc_move_to 移动 20 格以上再调用 explore。",
                        "tree": self.snapshot(),
                    }
                self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
            elif active is None:
                self.active_node_id = collar.node_id
            elif (
                math.dist(cur_pos, active.anchor) > 6.0
                and math.hypot(cur_pos[0] - collar.anchor[0], cur_pos[2] - collar.anchor[2]) > 48.0
                and not self._is_recorded_shaft_position(cur_pos)
                # Combat can leave us just outside the mine near a deep tip,
                # far from the collar. Give local navigation a chance to rejoin.
                and (
                    cur_pos[1] > collar.anchor[1] - 20
                    or math.dist(cur_pos, active.anchor) > NODE_REJOIN_DISTANCE
                )
            ):
                self._archive_current_shaft_to_ledger(mark_exhausted=False)
                self._clear_active_tree_only()
                if not self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref):
                    if self.collar_node_id in self.nodes:
                        return self._deep_shaft_rejoin_failure()
                    solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                        cur_pos,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                    )
                    if not viable_dirs:
                        return self._collar_failure(anchor_err, solid_pos, geofence)
                    self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
            # Rejoin the saved anchor when the current stance cannot connect to the descent.
            elif math.dist(cur_pos, active.anchor) > 3.5 or (
                cur_pos[1] >= active.anchor[1] and cur_pos != active.anchor
                and any(
                    branch.kind == "stair_down" and branch.status == "pending"
                    and not _descent_start_beside_anchor(cur_pos, active.anchor, branch.direction)
                    for branch in active.branches
                )
            ):
                resume_at_depth = self._should_resume_at_depth(active, cur_pos)
                err_nav = None
                local_obstruction = False
                if resume_at_depth:
                    ok_nav = self._resume_explore_at_depth(active, cur_pos, min_hp_ref=min_hp_ref)
                else:
                    ok_nav, err_nav = self._resume_shaft_tip(active.node_id, min_hp_ref=min_hp_ref)
                    local_obstruction = (
                        (err_nav or "").startswith("backtrack_blocked@")
                        and abs(cur_pos[1] - self.target_y) <= SCAN_BAND
                    )
                    if local_obstruction:
                        ok_nav = self._resume_local_branch(active.node_id, min_hp_ref=min_hp_ref)
                    elif (
                        not ok_nav
                        and (
                            self._ahead_of_pending_descent(active, cur_pos)
                            or (
                                active.kind == "spine"
                                and 0 < active.anchor[1] - cur_pos[1] <= SCAN_BAND
                                and math.dist(cur_pos, active.anchor) <= NODE_REJOIN_DISTANCE
                            )
                        )
                        and self._recover_failed_shaft_rejoin(active, cur_pos, err_nav, min_hp_ref=min_hp_ref)
                    ):
                        ok_nav = True
                    elif (
                        not ok_nav and active.kind in {"switchback", "spine"}
                        and err_nav not in {"hurt", "not_ready", "not_grounded"}
                    ):
                        ok_nav = self._resume_deepest_branch(active, min_hp_ref=min_hp_ref)
                if not ok_nav:
                    if local_obstruction:
                        self.active_node_id = active.node_id
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "action": act,
                            "strategy": self.strategy,
                            "stop_reason": "shaft_rejoin_blocked",
                            "rejoin_error": err_nav,
                            "pos_after": self.bot.status()["pos"],
                            "message": "目标层的矿道受阻。请根据阻挡坐标从同层绕行或清理可安全移除的障碍，再继续 explore。",
                            "tree": self.snapshot(),
                        }
                    if resume_at_depth:
                        return self._deep_shaft_rejoin_failure()
                    if (
                        self.collar_ceiling_y is not None
                        and cur_pos[1] <= int(self.collar_ceiling_y) - 20
                        and abs(cur_pos[1] - self.target_y) > 12
                    ):
                        # Same trap as the reset path: re-initialising far above the target layer
                        # plants a fake underground "collar". However, if we are ALREADY at the
                        # target depth band (e.g. Y=-54 for diamond, BUG-093), re-anchor locally
                        # instead of forcing an 80-block climb_out!
                        last_status = self._budget_session.last_status if self._budget_session is not None else {}
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "action": act,
                            "strategy": self.strategy,
                            "stop_reason": "needs_surface_climb",
                            "rejoin_error": err_nav,
                            "pos_before": st0.get("pos"),
                            "pos_after": last_status.get("pos"),
                            "message": (
                                f"回撤到当前节点失败，且你已在地下 y={cur_pos[1]}"
                                f"（最高井口 y={self.collar_ceiling_y}）。" + self._surface_climb_hint()
                            ),
                            "tree": self.snapshot(),
                        }
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
                    self._clear_active_tree_only()
                    solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                        cur_pos, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                    )
                    if not viable_dirs:
                        return self._collar_failure(anchor_err, solid_pos, geofence)
                    self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)

        if self.dry_calls >= DRY_CALLS_BEFORE_ABANDON and self.collar_node_id in self.nodes:
            # Dead shaft: 40 minutes of zero target yield. Archive it and tell the model to move.
            self._archive_current_shaft_to_ledger(mark_exhausted=True)
            self._clear_active_tree_only()
            self.dry_calls = 0
            return {
                "ok": False,
                "requires_replan": True,
                "action": act,
                "strategy": self.strategy,
                "stop_reason": "shaft_dry_abandoned",
                "message": (
                    f"本井连续 {DRY_CALLS_BEFORE_ABANDON} 次调用没有产出任何 {profile['profile_key']}，"
                    "判定为死井并归档。请换到 16 格以外的新点位："
                    "mc_move_to 平移后 explore；若在地下先 action='climb_out' 回地表。"
                ),
                "tree": self.snapshot(),
            }

        if self.idle_calls >= IDLE_CALLS_BEFORE_RELOCATE:
            # Live s05: 9 consecutive explore calls returned ok=true with gained={} and an
            # unchanged position; the model had no signal and kept retrying the same dead node.
            self.idle_calls = 0
            # Do NOT clear the tree first: the helper needs the collar y to decide whether a
            # surface relocation is even physically possible from here.
            moved, _ = self._force_relocate_on_fluid(
                geofence=geofence, deadline=deadline,
                min_hp_ref=min_hp_ref, auto_relocations=0,
            )
            if not moved:
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "shaft_exhausted",
                    "message": (
                        "连续 3 次调用都没有挖到东西也没移动，这条矿井已判定空转并归档。"
                        "请先 action='return_collar' 返回地表再 explore 开新井。"
                    ),
                    "tree": self.snapshot(),
                }

        mined_ores: list[list[int]] = []
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        stop_reason = "time_slice_progress"
        auto_relocations_this_slice = 0

        def _gained_target_count(after_counts: dict[str, int]) -> int:
            delta = _positive_inv_delta(counts0, after_counts)
            return sum(int(delta.get(item_n, 0)) for item_n in expected_items)

        min_target_tier = int(profile["min_tier"])

        def _quota_met() -> bool:
            return _gained_target_count(_inventory_counts(self.bot)) >= goal_count

        # Priority 1: harvest unlocked deferred ores whose required tier is now satisfied!
        unlockable = [
            rec for rec in sorted(self.deferred_ores.values(), key=lambda r: (r.required_tier, r.nearest_node_id))
            if rec.block_name in target_blocks and rec.required_tier <= max_tier0
        ]
        working_node = self.nodes.get(self.active_node_id)
        preserve_depth = working_node is not None and any(branch.status == "pending" for branch in working_node.branches)
        deferred_ceiling = max(cur_pos[1], self.target_y) + DEPTH_RESET_THRESHOLD
        for rec in unlockable:
            if time.monotonic() >= deadline:
                break
            near_node = self.nodes.get(rec.nearest_node_id)
            if near_node is None:
                continue
            near_anc = near_node.anchor
            if preserve_depth and near_anc[1] > deferred_ceiling:
                continue
            # A wide scan can associate ore with a node outside both retrieval ranges.
            vertical = abs(rec.pos[1] - near_anc[1])
            horizontal = abs(rec.pos[0] - near_anc[0]) + abs(rec.pos[2] - near_anc[2])
            scan_band = self.strategy == "fishbone_scan" and abs(near_anc[1] - self.target_y) <= SCAN_BAND
            hook_y, hook_xz = self._vein_hook_reach(target_ore=True, scan_band=scan_band)
            if vertical + horizontal > DROP_PICKUP_RADIUS_MANHATTAN + 2 and (vertical > hook_y or horizontal > hook_xz):
                continue
            ok_nav, err_nav = self._navigate_to_node(rec.nearest_node_id, min_hp_ref=min_hp_ref)
            if not ok_nav:
                stop_reason = err_nav or "deferred_nav_failed"
                break
            hook = self._run_vein_hook(
                near_anc,
                target_blocks=target_blocks,
                current_node_id=rec.nearest_node_id,
                geofence=geofence,
                cache={},
                min_target_tier=min_target_tier,
                min_hp_ref=min_hp_ref,
                quota_met=_quota_met,
            )
            mined_ores.extend(hook["mined_coords"])
            catch_placed += hook["catch_placed"]
            breaches_sealed += hook["breaches_sealed"]
            if _quota_met():
                stop_reason = "quota_reached"
                break
            if rec.pos in self.deferred_ores:
                ok_direct, c_dir, s_dir = self._mine_vein_ore_cell(
                    rec.pos,
                    rec.block_name,
                    anchor=near_anc,
                    target_set=set(target_blocks),
                    cache={},
                )
                catch_placed += c_dir
                breaches_sealed += s_dir
                if ok_direct:
                    mined_ores.append(list(rec.pos))
                    if rec.pos != (near_anc[0], near_anc[1] - 1, near_anc[2]):
                        self._move_exact(near_anc)
                    self._equip_for_block_tier(1, for_ore=False)
                if _quota_met():
                    stop_reason = "quota_reached"
                    break

        # Priority 2: main DFS loop
        blocked_branches: set[int] = set()
        turned_after_first_dig_timeout = False
        idle_streak = 0
        idle_since = time.monotonic()
        last_excavated = len(self.excavated_positions)
        last_node_count = len(self.nodes)
        last_active_node = self.active_node_id
        while stop_reason != "quota_reached" and time.monotonic() < deadline:
            # Spin guard: this loop is deadline-bounded only, so an iteration that neither
            # excavates, nor grows the tree, nor switches nodes would burn the whole slice
            # doing nothing (observed live as a 32s no-op call).
            if (
                len(self.excavated_positions) == last_excavated
                and len(self.nodes) == last_node_count
                and self.active_node_id == last_active_node
            ):
                idle_streak += 1
                # Wall clock matters too: with a slow/rate-limited API one iteration can take
                # 10s, so a 4-iteration threshold may never fit inside a 32s slice (live: five
                # consecutive no-op calls each burned the full slice).
                if idle_streak >= MAX_IDLE_ITERATIONS or (time.monotonic() - idle_since) >= MAX_IDLE_SECONDS:
                    if self._active_deep_branch_pending():
                        stop_reason = "no_progress"
                        break
                    idle_streak = 0
                    idle_since = time.monotonic()
                    # Stuck tree: try one in-slice relocation to a fresh collar before giving up,
                    # otherwise the next LLM round would hit the same dead end again.
                    if auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        self._archive_current_shaft_to_ledger(mark_exhausted=True)
                        self._clear_active_tree_only()
                        cur_now = _floor_pos(self.bot.status().get("pos") or [0, 64, 0])
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = "no_progress"
                    break
            else:
                idle_streak = 0
                idle_since = time.monotonic()
            last_excavated = len(self.excavated_positions)
            last_node_count = len(self.nodes)
            last_active_node = self.active_node_id

            if self.active_node_id is None or self.active_node_id not in self.nodes:
                stop_reason = "shaft_exhausted"
                break
            node = self.nodes[self.active_node_id]

            self._scan_ore_radar(node.anchor, target_blocks=target_blocks, max_tier=max_tier0, geofence=geofence)

            hook0 = self._run_vein_hook(
                node.anchor,
                target_blocks=target_blocks,
                current_node_id=node.node_id,
                geofence=geofence,
                cache={},
                min_target_tier=min_target_tier,
                min_hp_ref=min_hp_ref,
                quota_met=_quota_met,
            )
            mined_ores.extend(hook0["mined_coords"])
            deferred_added += hook0["deferred_added"]
            catch_placed += hook0["catch_placed"]
            breaches_sealed += hook0["breaches_sealed"]
            if _quota_met():
                stop_reason = "quota_reached"
                break

            edge = node.edge_from_parent
            if (
                node.kind == "spine" and edge is not None and not self.radar_enabled
                and node.anchor[1] - self.target_y > SPINE_DESCEND_MIN_DELTA
                and not any(b.kind == "stair_down" and b.branch_target_y == self.target_y for b in node.branches)
            ):
                # An existing spine can still have the branch plan from its old target layer.
                plan = self._spine_branches(edge.direction, node.anchor, geofence)
                if plan and plan[0].kind == "stair_down":
                    node.branches.insert(0, plan[0])

            next_branch = next((b for b in node.branches if b.status == "pending" and id(b) not in blocked_branches), None)
            if next_branch is None:
                descending = (
                    edge is not None and node.kind == "switchback" and node.anchor[1] > self.target_y + SCAN_BAND
                )
                alternatives = None
                if descending:
                    # Check untried dry turns even if the existing descent is cooling down.
                    tried = {b.direction for b in node.branches} | {OPPOSITE_DIR.get(edge.direction)}
                    directions = tuple(d for d in DIRECTIONS if d not in tried)
                    alternatives = self._probe_dry_descent(
                        node.anchor, directions=directions, geofence=geofence, min_hp_ref=min_hp_ref,
                    )
                    if alternatives:
                        node.branches.extend(alternatives)
                        continue
                if any(b.status == "pending" for b in node.branches):
                    if node.kind == "spine" and self._resume_deepest_branch(
                        node, min_hp_ref=min_hp_ref, blocked_branches=blocked_branches,
                    ):
                        continue
                    stop_reason = "dig_cell_avoided"
                    break
                spine_blocked = any(b.kind == "spine_forward" and b.status == "pruned" for b in node.branches)
                if node.kind == "spine" and spine_blocked:
                    promo = max(
                        (
                            b
                            for b in node.branches
                            if b.kind == "branch_leaf"
                            and b.status == "completed"
                            and b.prune_reason != "promoted_to_spine"
                            and len(b.completed_waypoints) >= 3
                        ),
                        key=lambda b: (
                            self._radar_direction_projection(node.anchor, b.direction, at_spine=True),
                            len(b.completed_waypoints),
                        ),
                        default=None,
                    )
                    if promo is not None:
                        promo.prune_reason = "promoted_to_spine"
                        ok_pr, err_pr = self._walk_waypoints(
                            promo.completed_waypoints,
                            reverse=False,
                            min_hp_ref=min_hp_ref,
                        )
                        if not ok_pr:
                            stop_reason = err_pr or "promote_walk_failed"
                            break
                        end_tip = promo.completed_waypoints[-1]
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_tip,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("branch_leaf", promo.direction, list(promo.completed_waypoints)),
                            branches=self._spine_branches(promo.direction, end_tip, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                        continue

                if node.parent_id is None or edge is None:
                    self._archive_current_shaft_to_ledger(mark_exhausted=True)
                    if auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        cur_now = _floor_pos(self.bot.status()["pos"])
                        self._clear_active_tree_only()
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now,
                            geofence=geofence,
                            deadline=deadline,
                            min_hp_ref=min_hp_ref,
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = "shaft_exhausted"
                    break
                at_final_stair = (
                    node.kind == "switchback" and edge.edge_kind == "stair_down"
                    and abs(node.anchor[1] - self.target_y) <= SCAN_BAND
                )
                if (descending and alternatives is not None) or at_final_stair:
                    # Avoid giving up the final stair when a same-level offset can bypass its blocked landing.
                    if any(
                        self._enqueue_offset_retry_if_eligible(
                            node, branch, branch.prune_reason or "", geofence, after_exhaustion=True,
                        )
                        for branch in tuple(node.branches)
                        if branch.status == "pruned"
                        and (descending or branch.prune_reason == "movement_not_confirmed")
                    ):
                        continue
                parent = self.nodes.get(node.parent_id)
                if parent is not None and parent.anchor[1] > node.anchor[1]:
                    # Resuming at a cleared transit node should lead back down
                    # to unfinished work below it before walking up the shaft.
                    if self._resume_deepest_branch(node, min_hp_ref=min_hp_ref, descendants_only=True):
                        continue
                reached_y = min(cur_pos[1], node.anchor[1])
                ascent_ceiling = max(reached_y, self.target_y) + DEPTH_RESET_THRESHOLD
                if reached_y <= self.target_y + SCAN_BAND and (
                    (parent is not None and parent.anchor[1] > ascent_ceiling)
                    or any(waypoint[1] > ascent_ceiling for waypoint in edge.waypoints)
                ):
                    if self._resume_local_branch(node.node_id, min_hp_ref=min_hp_ref):
                        continue
                    stop_reason = "shaft_rejoin_blocked"
                    break
                ok_bt, err_bt = self._walk_waypoints(
                    edge.waypoints, reverse=True, min_hp_ref=min_hp_ref, resume_edge_kind=edge.edge_kind,
                )
                if not ok_bt:
                    if abs(node.anchor[1] - self.target_y) <= SCAN_BAND and self._utility_blocks_backtrack(err_bt or ""):
                        if self._resume_local_branch(node.node_id, min_hp_ref=min_hp_ref):
                            continue
                        stop_reason = err_bt or "backtrack_failed"
                        break
                    if (err_bt or "").startswith("backtrack_blocked"):
                        if self._resume_deepest_branch(node, min_hp_ref=min_hp_ref):
                            continue
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
                    if not descending and auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        cur_now = _floor_pos(self.bot.status()["pos"])
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            # A deadline during relocation must leave the old shaft available.
                            self._clear_active_tree_only()
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = err_bt or "backtrack_failed"
                    break
                self.active_node_id = node.parent_id
                continue

            branch_kind = next_branch.kind
            is_level = branch_kind not in {"stair_down", "stair_up"}
            if branch_kind == "offset_retry":
                lat_dir = next_branch.sidestep_dir or LEFT_RIGHT_DIRS[next_branch.direction][0]
                if self._cuts_incoming_stair(node.anchor, node.edge_from_parent, lat_dir):
                    next_branch.status = "pruned"
                    next_branch.prune_reason = "stair_support"
                    continue
            if is_level or branch_kind == "stair_down":
                stance = _floor_pos(self.bot.status()["pos"])
                if is_level:
                    needs_rejoin = stance[1] != node.anchor[1] or (branch_kind == "offset_retry" and stance != node.anchor)
                else:
                    x, y, z = node.anchor
                    dx, dz = DIRECTIONS[next_branch.direction]
                    # A displaced start must still connect to this branch's saved anchor.
                    needs_rejoin = (
                        stance not in (node.anchor, (x + 2 * dx, y - 2, z + 2 * dz))
                        and not _descent_start_beside_anchor(stance, node.anchor, next_branch.direction)
                    )
                    if needs_rejoin and stance[1] < y - DEPTH_RESET_THRESHOLD:
                        stop_reason = "movement_not_confirmed"
                        break
                if needs_rejoin:
                    reached, reason = self._resume_shaft_tip(node.node_id, min_hp_ref=min_hp_ref)
                    if not reached:
                        if auto_relocations_this_slice < 1 and self._recover_failed_shaft_rejoin(
                            node, stance, reason, min_hp_ref=min_hp_ref,
                        ):
                            auto_relocations_this_slice += 1
                            continue
                        stop_reason = reason or "movement_not_confirmed"
                        break
            next_branch.status = "active"
            try:
                if branch_kind in {"stair_down", "stair_up"}:
                    is_up = branch_kind == "stair_up"
                    eff_y = next_branch.branch_target_y if next_branch.branch_target_y is not None else self.target_y
                    stair_fn = self._dig_stair_up_edge if is_up else self._dig_stair_down_edge
                    edge_res = stair_fn(
                        direction=next_branch.direction,
                        max_steps=next_branch.steps,
                        target_y=eff_y,
                        target_blocks=target_blocks,
                        parent_node_id=node.node_id,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                        min_target_tier=min_target_tier,
                        quota_met=_quota_met,
                    )
                else:
                    edge_res = self._dig_level_edge(
                        direction=lat_dir if branch_kind == "offset_retry" else next_branch.direction,
                        steps=OFFSET_RETRY_STEPS if branch_kind == "offset_retry" else next_branch.steps,
                        target_blocks=target_blocks,
                        parent_node_id=node.node_id,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                        min_target_tier=min_target_tier,
                        quota_met=_quota_met,
                    )
            except _MiningDeadline:
                if (
                    turned_after_first_dig_timeout
                    or not any(b.status == "pending" and id(b) not in blocked_branches for b in node.branches)
                    or not self._skip_stalled_first_dig(deadline=deadline, min_hp=min_hp_ref[0])
                ):
                    raise
                next_branch.status = "pending"
                blocked_branches.add(id(next_branch))
                turned_after_first_dig_timeout = True
                idle_streak = 0
                idle_since = time.monotonic()
                continue
            steps_done = int(edge_res["steps_done"])
            er = str(edge_res["stop_reason"])
            breaches_sealed += int(edge_res["breaches_sealed"])
            catch_placed += int(edge_res["catch_placed"])
            deferred_added += int(edge_res["deferred_added"])
            mined_ores.extend(edge_res["mined_ores"])
            if is_level and any(pt[1] != node.anchor[1] for pt in edge_res["waypoints"]):
                # A sidestep or level corridor cannot connect nodes on different floors.
                next_branch.status = "pending"
                stop_reason = er if er in {"hurt", "not_ready", "pickaxe_low"} else "movement_not_confirmed"
                break
            if er == "dig_cell_avoided":
                if steps_done == 0:
                    next_branch.status = "pending"
                    blocked_branches.add(id(next_branch))
                    idle_streak = 0
                    idle_since = time.monotonic()
                    continue
                # Keep the traversed edge and resume after the cooldown.
                er = "time_slice"

            if branch_kind == "offset_retry":
                if er == "quota_reached":
                    next_branch.status = "completed"
                    if steps_done >= 1:
                        self._record_offset_branch(node, next_branch, edge_res["waypoints"], lat_dir)
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    stop_reason = er
                    break
                if er == "time_slice" and steps_done == 0:
                    next_branch.status = "pending"
                    stop_reason = "time_slice_progress"
                    break
                if steps_done >= 2:
                    sh_node = self._record_offset_branch(node, next_branch, edge_res["waypoints"], lat_dir)
                    self._maybe_place_torch_at_node(sh_node)
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if steps_done > 0:
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)

            elif branch_kind in {"stair_down", "stair_up"}:
                end_anchor = tuple(edge_res["end_anchor"])
                if not is_up:
                    waypoints = edge_res["waypoints"]
                    verified_route = _descending_waypoint_prefix(waypoints, node.anchor[1])
                    if len(verified_route) < len(waypoints):
                        next_branch.status = "pending"
                        if verified_route and verified_route[0] == node.anchor:
                            descent = node.anchor[1] - verified_route[-1][1]
                            if descent > 0:
                                self._record_partial_branch(node, next_branch, verified_route, descent)
                        stop_reason = er if er in {"hurt", "not_ready", "pickaxe_low"} else "movement_not_confirmed"
                        break
                reached_band = (end_anchor[1] >= eff_y - 1) if is_up else (end_anchor[1] <= eff_y + 2)

                if self._note_fluid_abort(er):
                    detoured = (
                        not is_up and steps_done > 0 and end_anchor[1] > self.target_y + 12
                        and self._continue_descent_around_hazard(
                            node, next_branch, edge_res["waypoints"], geofence=geofence, min_hp_ref=min_hp_ref,
                        )
                    )
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if detoured:
                        self.fluid_aborts = 0
                        continue
                    if not steps_done and self._retry_dry_junction(node, geofence=geofence, min_hp_ref=min_hp_ref):
                        continue
                    moved, auto_relocations_this_slice = self._force_relocate_on_fluid(
                        geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref,
                        auto_relocations=auto_relocations_this_slice,
                    )
                    if moved:
                        continue
                    stop_reason = "shaft_exhausted"
                    break

                if er == "quota_reached":
                    next_branch.status = "completed"
                    if steps_done > 0 and end_anchor != node.anchor:
                        rem_dq = max(1, abs(end_anchor[1] - eff_y))
                        q_node = self._create_node(
                            kind="spine" if reached_band else "switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=(
                                self._spine_branches(next_branch.direction, end_anchor, geofence)
                                if reached_band
                                else [BranchSpec(next_branch.kind, next_branch.direction, rem_dq, branch_target_y=eff_y)]
                                + self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                            ),
                        )
                        self._maybe_place_torch_at_node(q_node)
                    stop_reason = "quota_reached"
                    break

                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    elif steps_done > 0 and end_anchor != node.anchor:
                        rem_d = max(1, abs(end_anchor[1] - eff_y))
                        next_branch.status = "completed"
                        self._create_node(
                            kind="spine" if reached_band else "switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=(
                                self._spine_branches(next_branch.direction, end_anchor, geofence)
                                if reached_band
                                else [BranchSpec(next_branch.kind, next_branch.direction, rem_d, branch_target_y=eff_y)]
                                + self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                            ),
                        )
                    stop_reason = er
                    break

                # HARD SPIN GUARD: never spawn a 0-displacement child node at the exact same coordinate!
                if steps_done == 0 or end_anchor == node.anchor:
                    if er == "time_slice":
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    next_branch.status = "completed" if er == "reached_target_y" else "pruned"
                    if er != "reached_target_y":
                        next_branch.prune_reason = er
                        self._move_exact(node.anchor)
                        self._enqueue_offset_retry_if_eligible(node, next_branch, er, geofence)
                    elif not is_up and node.kind == "switchback" and end_anchor == node.anchor:
                        # A previous slice can stop on the final stair before opening the spine.
                        node.kind = "spine"
                        node.branches.extend(self._spine_branches(next_branch.direction, node.anchor, geofence))
                    continue

                if er in {"reached_target_y", "completed"} and reached_band:
                    next_branch.status = "completed"
                    spine_node = self._create_node(
                        kind="spine",
                        anchor=end_anchor,
                        parent_id=node.node_id,
                        edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                        branches=self._spine_branches(next_branch.direction, end_anchor, geofence),
                    )
                    self._maybe_place_torch_at_node(spine_node)
                elif er in {"time_slice", "completed"} and steps_done > 0:
                    next_branch.status = "completed"
                    if not reached_band:
                        rem_depth = max(1, abs(end_anchor[1] - eff_y))
                        cont_branches = [BranchSpec(next_branch.kind, next_branch.direction, rem_depth, branch_target_y=eff_y)]
                        if not is_up:
                            cont_branches.extend(
                                self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                            )
                        self._create_node(
                            kind="switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=cont_branches,
                        )
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if steps_done >= 2 and reached_band:
                        left_d, _ = LEFT_RIGHT_DIRS[next_branch.direction]
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(left_d, end_anchor, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                    elif steps_done >= 2 and not is_up:
                        sw_branches = self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                        if sw_branches:
                            sw_node = self._create_node(
                                kind="switchback",
                                anchor=end_anchor,
                                parent_id=node.node_id,
                                edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                                branches=sw_branches,
                            )
                            self._maybe_place_torch_at_node(sw_node)
                        else:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    else:
                        if steps_done > 0:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                        else:
                            self._move_exact(node.anchor)
                        self._enqueue_offset_retry_if_eligible(node, next_branch, er, geofence)

            else:
                # "branch_leaf" or "spine_forward"
                if self._note_fluid_abort(er):
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if not steps_done and self._retry_dry_junction(node, geofence=geofence, min_hp_ref=min_hp_ref):
                        continue
                    if self._retry_dry_parent_after_level_abort(
                        node, edge_res["waypoints"], geofence=geofence, min_hp_ref=min_hp_ref,
                    ):
                        continue
                    moved, auto_relocations_this_slice = self._force_relocate_on_fluid(
                        geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref,
                        auto_relocations=auto_relocations_this_slice,
                    )
                    if moved:
                        continue
                    stop_reason = "shaft_exhausted"
                    break

                if er == "quota_reached":
                    next_branch.status = "completed"
                    end_anchor_q = tuple(edge_res["end_anchor"])
                    if next_branch.kind == "spine_forward" and steps_done >= 1 and end_anchor_q != node.anchor:
                        q_sp = self._create_node(
                            kind="spine",
                            anchor=end_anchor_q,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor_q, geofence),
                        )
                        self._maybe_place_torch_at_node(q_sp)
                    elif next_branch.kind == "branch_leaf" and steps_done > 0:
                        if steps_done >= 2:
                            next_branch.completed_waypoints = list(edge_res["waypoints"])
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    end_anchor_s = tuple(edge_res["end_anchor"])
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    elif steps_done >= 2 and next_branch.kind == "spine_forward" and end_anchor_s != node.anchor:
                        next_branch.status = "completed"
                        self._create_node(
                            kind="spine",
                            anchor=end_anchor_s,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor_s, geofence),
                        )
                    elif steps_done > 0 and next_branch.kind == "branch_leaf":
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    stop_reason = er
                    break

                if next_branch.kind == "branch_leaf":
                    if er == "time_slice" and steps_done == 0:
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    next_branch.status = "completed" if er in {"completed", "time_slice"} else "pruned"
                    if er in {"completed", "time_slice"} and steps_done >= 2:
                        next_branch.completed_waypoints = list(edge_res["waypoints"])
                    if er not in {"completed", "time_slice"}:
                        next_branch.prune_reason = er
                    if steps_done > 0:
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    end_anchor = tuple(edge_res["end_anchor"])
                    if er == "time_slice" and steps_done == 0:
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    if (
                        er in {"completed", "time_slice"}
                        and end_anchor != node.anchor
                        and steps_done >= 1
                    ):
                        next_branch.status = "completed"
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                        if er == "time_slice":
                            stop_reason = "time_slice_progress"
                            break
                    else:
                        next_branch.status = "pruned"
                        next_branch.prune_reason = er
                        if steps_done > 0:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)

            if _quota_met():
                stop_reason = "quota_reached"
                break

        inv_after, counts_after = _inventory_snapshot_and_counts(self.bot)
        gained_delta = _positive_inv_delta(counts0, counts_after)
        target_gained = _gained_target_count(counts_after)
        if target_gained >= goal_count:
            stop_reason = "quota_reached"

        returned_collar_on_low = False
        if stop_reason == "pickaxe_low":
            returned_collar_on_low = self._maybe_return_collar_for_resupply(counts_after, min_hp_ref=min_hp_ref)

        self._archive_current_shaft_to_ledger(mark_exhausted=(stop_reason == "shaft_exhausted"))
        self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_after)
        st_end = self.bot.status()

        ok = stop_reason in {"quota_reached", "time_slice_progress"}
        if stop_reason == "time_slice_progress" and not gained_delta and st0.get("pos") == st_end.get("pos"):
            self.idle_calls += 1
        else:
            self.idle_calls = 0
        if target_gained <= 0:
            self.dry_calls += 1
        else:
            self.dry_calls = 0
        out_res: dict[str, Any] = {
            "ok": ok,
            "requires_replan": not ok or stop_reason == "hurt",
            "action": act,
            "strategy": self.strategy,
            "target": profile["profile_key"],
            "target_y": self.target_y,
            "goal_count": goal_count,
            "target_gained": target_gained,
            "gained": gained_delta,
            "stop_reason": stop_reason,
            "pos_before": st0.get("pos"),
            "pos_after": st_end.get("pos"),
            "mined_ores": mined_ores,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "recrafted_picks": self.recrafted_picks - recrafted_before,
            "tree": self.snapshot(),
        }
        if self.radar_enabled:
            out_res["radar_summary"] = {
                "exposed_count": len(self.radar_targets),
                "ever_seen_count": len(self.ever_seen_radar_ores),
                "locked_cluster": list(self.locked_cluster["rep"]) if self.locked_cluster else None,
                "cluster_size": int(self.locked_cluster["size"]) if self.locked_cluster else 0,
                "mob_nearby": bool(self.locked_cluster.get("mob_nearby", False)) if self.locked_cluster else False,
            }
        if returned_collar_on_low:
            out_res["returned_to_collar_for_resupply"] = True
        if stop_reason == "shaft_exhausted" and self.fluid_abandoned_deep:
            out_res["message"] = (
                "本井连续遇到水或洞穴，已归档回撤路线。请调用 action='climb_out' 优先沿已保存的巷道上爬。"
                "若返回 climb_progress，可从当前站位继续 climb_out。到达地表后再 explore。"
            )
        if stop_reason == "pickaxe_low":
            out_res.update(self._pickaxe_low_recovery(counts_after, st_end.get("pos")))
        if stop_reason == "dig_cell_avoided":
            out_res["message"] = "当前可走分支的挖掘格处于超时冷却。请先处理障碍，或改从同层的安全站位进矿。"
        if stop_reason == "shaft_rejoin_blocked":
            out_res["message"] = "目标层没有可直接接续的已知分支。请从同层安全位置继续探矿，或调用 climb_out 回撤。"
        if stop_reason == "no_progress":
            if self._active_deep_branch_pending():
                out_res["message"] = "已保留当前深度的矿道和待挖分支。请在同层确认安全站位，再继续 explore。"
            else:
                out_res["message"] = (
                    "在原地连续多次既没挖到方块也没推进矿井节点（可能卡在死胡同反复重试），已提前结束本轮而不是空耗时间。"
                    "请先调整站位；有井口记录时用 return_collar 回撤，空间树为空时直接 explore 开新井。"
                )
        if stop_reason == "shaft_exhausted" and not self.fluid_abandoned_deep:
            out_res["message"] = (
                "当前矿井分支与偏移重试均已穷尽；请先移到 16 格以外的安全新点位，再直接 explore。"
                "若仍在地下，有井口记录时用 return_collar，空间树为空时用 climb_out 回撤。"
            )
        if self._utility_blocks_backtrack(stop_reason):
            out_res["message"] = "目标层的矿道受阻。请根据阻挡坐标从同层绕行或清理可安全移除的障碍，再继续 explore。"
        return out_res
