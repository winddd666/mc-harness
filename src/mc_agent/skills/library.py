"""High-level Minecraft macro skills facade (implementation split under mc_agent.skills.macro)."""

from __future__ import annotations

from mc_agent.skills.macro._base import (
    copy,
    partial,
    inspect,
    math,
    re,
    time,
    SimpleNamespace,
    Any,
    Callable,
    httpx,
    BotSession,
    ApiError,
    AuthError,
    DeathLoopCooldownError,
    RECIPES_1_21,
    assess_world_target,
    normalize_dimension,
    resolve_item_alias,
    check_and_stabilize,
    material_shortage,
    explain_craft_error,
    MAX_LOCAL_RECOVERY_ATTEMPTS,
    LOG_TYPES,
    NETHER_STEM_TYPES,
    BED_BLOCK_TYPES,
    VILLAGE_LANDMARK_BLOCKS,
    VILLAGE_SURVEY_CLUTTER,
    SURFACE_SCOUT_DIRECTIONS,
    SURFACE_SCOUT_ENTITIES,
    SURFACE_SCOUT_LANDMARKS,
    NETHER_SCOUT_TARGET_BLOCKS,
    NETHER_SCOUT_TARGET_ENTITIES,
    NETHER_TUNNEL_UNSUPPORTED_FLOORS,
    SURFACE_SCOUT_PATH_ERRORS,
    rejected_find_block,
    AXE_PRIORITY,
    PICKAXE_TIERS,
    STAIR_FILLER_BLOCKS,
    BLOCK_MIN_PICKAXE_TIER,
    ORE_COUNTERPARTS,
    EXPECTED_BLOCK_DROPS,
    _expected_drops_for_blocks,
    TABLE_RECIPES,
    _WORKBENCH_OPEN_ERRORS,
    DEFAULT_EQUIP_SLOTS,
    _REUSABLE_CRAFT_ITEMS,
    TOOL_WOOD_NEEDS,
    _inventory_counts_from_raw,
    _inventory_snapshot_and_counts,
    _inventory_counts,
    _window_item_entries,
    _log_to_planks_name,
    _PLANK_LOG_SOURCES,
    _PLANK_SOURCE_LOGS,
    _canonical_plank_recipe,
    _total_planks,
    _plank_equivalent,
    _stick_craft_batches,
    _wood_budget_shortage,
    _best_pickaxe_in_inv,
    MELEE_WEAPONS_PRIORITY,
    SAFE_MELEE_MOBS,
    best_melee_weapon_in_inv,
    PASSABLE_SIGHT_BLOCKS,
    PASSABLE_WALK_BLOCKS,
    NON_WALL_SIGHT_SUFFIXES,
    is_passable_sight_block,
    SHAFT_SEAL_BLOCKS,
    cardinal_bearing,
    _cached_block_name,
    ray_wall_blocker,
    inspect_local_enclosure,
    assess_hostile_occlusion,
    _is_protected_err,
    _TRANSIENT_BLOCK_ERRORS,
    _positive_inv_delta,
    _TRASH_TIER1_ZERO_KEEP,
    _TRASH_TIER2_DECOR_STONES,
    _TRASH_TIER2_LOOSE_BLOCKS,
    _TRASH_TIER3_DIRT,
    _TRASH_TIER4_COBBLE,
    _TRASH_TIER_NETHER,
    _COMMON_GATHER_DROPS,
    _MINING_RETENTION_CAPS,
    _inventory_retention_plan,
    _UNSTACKABLE_ITEMS,
    _CRAFT_SLOT_INPUTS,
    craft_material_shortage,
    _reserved_craft_materials,
    explain_material_craft_error,
    _compute_empty_slots,
    _is_stackable_item,
    _can_fit_item_in_inventory,
    _yaw_toward,
    _CONTAINER_TYPES,
    SMELT_OUTPUTS,
    _validate_smelt_input,
    _merge_counts,
    _clean_block,
    _HIGH_VALUE_DROPS,
    _PICKUP_INTERRUPTION_CODES,
    _MID_VALUE_DROPS,
    _normalize_drop_name,
    _summarize_uncollected_drops,
    _StairDeadline,
    _StairSession,
    _SkillDeadline,
    _SkillBudgetSession,
)

from mc_agent.skills.macro.scout_nav import _ScoutNavMixin
from mc_agent.skills.macro.inventory_station import _InventoryStationMixin
from mc_agent.skills.macro.gather import _GatherMixin
from mc_agent.skills.macro.crafting import _CraftingMixin
from mc_agent.skills.macro.mining_container import _MiningContainerMixin
from mc_agent.skills.macro.smelting import _SmeltingMixin
from mc_agent.skills.macro.merchant_brew import _MerchantBrewMixin
from mc_agent.skills.macro.escape_unstick import _EscapeUnstickMixin
from mc_agent.skills.macro.staircase import _StaircaseMixin
from mc_agent.skills.macro.combat_portal import _CombatPortalMixin

class MacroSkills(
    _ScoutNavMixin,
    _InventoryStationMixin,
    _GatherMixin,
    _CraftingMixin,
    _MiningContainerMixin,
    _SmeltingMixin,
    _MerchantBrewMixin,
    _EscapeUnstickMixin,
    _StaircaseMixin,
    _CombatPortalMixin,
):
    """Stateful skills with permanent protection and separate expiring failure records."""
    def __init__(self, bot: BotSession, *, geofence: Any | None = None) -> None:
        self.bot = bot
        self.geofence = geofence
        self.protected_positions: set[tuple[int, int, int]] = set()
        self.temporary_avoidance: dict[tuple[int, int, int], dict[str, Any]] = {}
        self.failed_stair_dirs: dict[tuple[int, int, int, str], dict[str, tuple[float, str]]] = {}
        self._pillar_fallbacks: dict[tuple[str, int, int], tuple[int, float]] = {}
        self._failed_scout_routes: dict[tuple[str, int], tuple[tuple[float, float, float], float, float]] = {}
        self.failed_tunnel_dirs: dict[tuple[int, int, int], dict[str, tuple[float, str]]] = {}
        self._drop_retry_after: dict[tuple[str, int, tuple[int, ...], tuple[int, ...]], float] = {}
        self._temporary_furnaces: set[tuple[str, tuple[int, ...]]] = set()
        self._unfinished_portal_frames: dict[str, tuple[int, int, int]] = {}
        self._pending_furnace_takes: dict[tuple[str, tuple[int, ...], str], tuple[int, int]] = {}
        self._smelt_inventory_reserve: dict[str, int] = {}
        self._placed_storage: dict[tuple[str, tuple[int, ...]], str] = {}
        self._dfs_storage_reports: list[dict[str, Any]] | None = None
        self._mining_dfs: Any = None
        self.world_memory: Any = None
        self._terrain_nav: Any = None
        self.inventory_manager: Any = None


    def _get_terrain_nav(self) -> Any:
        """Lazily build the L2 local-terrain navigator (breadcrumbs + 5 primitives)."""
        if self._terrain_nav is None:
            from .movement import TerrainNavigator

            self._terrain_nav = TerrainNavigator(self.bot, skills=self)
        return self._terrain_nav


    def _get_world_memory(self) -> Any:
        if self.world_memory is None:
            from ..harness.world_memory import WorldMemory

            self.world_memory = WorldMemory()
        return self.world_memory

