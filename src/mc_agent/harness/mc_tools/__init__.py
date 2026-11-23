"""McToolset package facade (split from legacy mc_tools.py)."""

from __future__ import annotations

from mc_agent.harness.mc_tools._base import (
    copy,
    dataclass,
    replace,
    json,
    math,
    re,
    threading,
    time,
    Any,
    Callable,
    httpx,
    BotSession,
    ApiError,
    AuthError,
    BotNotReadyError,
    DeathLoopCooldownError,
    assess_world_target,
    build_jit_error_hint,
    lookup_kb,
    normalize_dimension,
    resolve_item_alias,
    MacroSkills,
    SafetyEmergency,
    check_and_stabilize,
    compute_advancement_frontier,
    run_combat_skill,
    BED_BLOCK_TYPES,
    BLOCK_MIN_PICKAXE_TIER,
    LOG_TYPES,
    NETHER_STEM_TYPES,
    SURFACE_SCOUT_DIRECTIONS,
    TABLE_RECIPES,
    VILLAGE_LANDMARK_BLOCKS,
    VILLAGE_SURVEY_CLUTTER,
    _PICKUP_INTERRUPTION_CODES,
    _SkillBudgetSession,
    _SkillDeadline,
    _StairDeadline,
    _StairSession,
    _best_pickaxe_in_inv,
    _can_fit_item_in_inventory,
    _compute_empty_slots,
    _expected_drops_for_blocks,
    _inventory_counts,
    _merge_counts,
    _normalize_drop_name,
    best_melee_weapon_in_inv,
    explain_material_craft_error,
    is_passable_sight_block,
    rejected_find_block,
    SAFE_FOOD_PRIORITY,
    VoidGeofence,
    Parameter,
    Tool,
    ToolRegistry,
    WorldMemory,
    default_memory_path_for_bot,
    _ScoutNumberParameter,
    _BLOCK_COLORS,
    _BLOCK_WOODS,
    _FIND_BLOCK_ALIASES,
    _NON_BLOCK_FIND_TYPES,
    _invalid_find_block_query,
    _normalize_find_block_types,
    COMPACT_CORE_TOOLS,
    _PASSABLE_SIGHT_BLOCKS,
    _FailedMoveWaypoint,
    _COMPACT_TOOL_DESC,
    _SELF_EVIDENT_PARAMS,
    _COMPACT_PARAM_DESC,
    _compactify_tool,
    _build_window_target,
)

from mc_agent.harness.mc_tools.sensing import _SensingMixin
from mc_agent.harness.mc_tools.actions import _ActionsMixin
from mc_agent.harness.mc_tools.skills_bridge import _SkillsBridgeMixin
from mc_agent.harness.mc_tools.registry import _RegistryMixin

class McToolset(
    _SensingMixin,
    _ActionsMixin,
    _SkillsBridgeMixin,
    _RegistryMixin,
):
    def __init__(
        self,
        bot: BotSession,
        *,
        geofence: VoidGeofence | None = None,
        world_memory: WorldMemory | None = None,
        wait_for_cooldown: bool = False,
        stop_check: Callable[[], bool] | None = None,
        on_cooldown_wait: Callable[[str, float], None] | None = None,
        episode_deadline_mono: float | None = None,
    ) -> None:
        self.bot = bot
        self._state_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._wait_for_cooldown = bool(wait_for_cooldown)
        self._stop_check = stop_check
        self._on_cooldown_wait = on_cooldown_wait
        self._episode_deadline_mono: float | None = episode_deadline_mono
        self._last_event_seq: int = int(getattr(bot, "_last_event_seq", 0) or 0)
        self._in_water: bool = False
        self._last_block_pos: list[int] | None = None
        self._last_dimension: str = "overworld"
        self._last_biome: str = ""
        self._entity_observations: dict[int, dict[str, Any]] = {}
        self._entity_observed_mono: float | None = None
        self._entity_observed_dimension: str = "overworld"
        self._nearby_drop_observation: tuple[str, frozenset[str]] | None = None
        self._status_hostile_id: int | None = None
        self._geofence_blocked_count: int = 0
        self._path_not_found_targets: list[tuple[float, float]] = []
        self._last_failed_waypoint: _FailedMoveWaypoint | None = None
        self._empty_max_scans: dict[tuple[str, tuple[str, ...]], tuple[int, tuple[float, float] | None]] = {}
        self.geofence = geofence if geofence is not None else VoidGeofence.from_env()
        self.world_memory = (
            world_memory
            if world_memory is not None
            else WorldMemory(default_memory_path_for_bot(bot))
        )
        self.skills = MacroSkills(bot, geofence=self.geofence)
        self.skills.world_memory = self.world_memory
        self._ender_eye_throws: list[tuple[float, float, float, float]] = []
        self._triangulated_stronghold_xz: list[int] | None = None
