"""Path-compressed MiningDFS engine facade (implementation split under mc_agent.skills.mining)."""

from __future__ import annotations

from mc_agent.skills.mining._base import (
    deque,
    Callable,
    copy,
    dataclasses,
    inspect,
    json,
    math,
    os,
    pathlib,
    re,
    time,
    Any,
    httpx,
    ApiError,
    assess_world_target,
    normalize_dimension,
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
    DIRECTIONS,
    OPPOSITE_DIR,
    LEFT_RIGHT_DIRS,
    AIR_BLOCKS,
    FLUID_BLOCKS,
    LAVA_BLOCKS,
    FALLING_BLOCKS,
    UTILITY_BLOCKS,
    PASSABLE_BLOCKS,
    FLIMSY_FLOOR_BLOCKS,
    FLIMSY_FLOOR_SUFFIXES,
    HAZARD_FLOOR_BLOCKS,
    ROCK_BLOCKS,
    SURFACE_GROUND_BLOCKS,
    TRACKED_ORES,
    ORE_DFS_PROFILES,
    DEFAULT_MAX_DURABILITY,
    RADAR_SCAN_RADIUS,
    VEIN_HOOK_RADAR_RADIUS,
    VEIN_HOOK_FISHBONE_RADIUS,
    VEIN_HOOK_SCAN_RADIUS,
    DEPTH_RESET_THRESHOLD,
    SCAN_BAND,
    LAYER_SWITCH_MIN_DELTA,
    CLIMB_HEIGHT_TOLERANCE,
    OFFSET_RETRY_STEPS,
    NODE_REJOIN_DISTANCE,
    SPINE_DESCEND_MIN_DELTA,
    RECRAFT_PICK_DURABILITY,
    MAX_IDLE_ITERATIONS,
    MAX_IDLE_SECONDS,
    DIG_TIMEOUT_COOLDOWN_S,
    DIG_RECOVERY_RESERVE_S,
    WATER_SHAFT_COOLDOWN_S,
    FLUID_ABORT_LIMIT,
    IDLE_CALLS_BEFORE_RELOCATE,
    DRY_CALLS_BEFORE_ABANDON,
    DROP_PICKUP_RADIUS_MANHATTAN,
    FLUID_ABORT_REASONS,
    EXHAUSTED_COLLAR_MIN_SEP,
    UNREACHABLE_ORE_COOLDOWN_S,
    _MiningDeadline,
    _CollarResupplyDeadline,
    _MiningLifeChanged,
    _BudgetSession,
    normalize_dfs_strategy,
    BranchSpec,
    ShaftEdge,
    ShaftNode,
    _WaterInterruptedShaft,
    DeferredOreRecord,
    WorldLedger,
    _default_ledger_path_for_bot,
    _should_focus_target_only,
    _resolve_target_profile,
    _extract_item_remaining_durability,
    _pickaxe_inventory_summary,
    _choose_pickaxe_for_tier,
    _seal_reserve,
    _pick_seal_material,
    _stone_pickaxe_headroom,
    _pickaxe_low_hint,
    _has_collar_resupply_materials,
    _can_stack_expected_drop,
    _is_two_step_stair,
    _is_two_block_vertical_gap,
    _descent_start_beside_anchor,
    _descending_waypoint_prefix,
    _omitted_waypoint_index,
    _interpolate_cells,
    _floor_pos,
)

from mc_agent.skills.mining.navigation import _NavigationMixin
from mc_agent.skills.mining.collar_radar import _CollarRadarMixin
from mc_agent.skills.mining.climb_drops import _ClimbDropsMixin
from mc_agent.skills.mining.vein_extract import _VeinExtractMixin
from mc_agent.skills.mining.edges_branches import _EdgesBranchesMixin
from mc_agent.skills.mining.runner import _RunnerMixin

class MiningDFSEngine(
    _NavigationMixin,
    _CollarRadarMixin,
    _ClimbDropsMixin,
    _VeinExtractMixin,
    _EdgesBranchesMixin,
    _RunnerMixin,
):
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

