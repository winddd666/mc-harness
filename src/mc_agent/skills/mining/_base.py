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

from ...errors import ApiError
from ...kb import assess_world_target, normalize_dimension
from ..library import (
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
