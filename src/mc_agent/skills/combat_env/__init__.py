"""Combat environment & subcortical instinct controller package facade."""

from __future__ import annotations

from mc_agent.skills.combat_env._base import (
    math,
    time,
    Any,
    Callable,
    Collection,
    Literal,
    Protocol,
    Sequence,
    TypedDict,
    httpx,
    ApiError,
    SAFE_FOOD_PRIORITY,
    auto_equip_survival_gear,
    water_oxygen_below,
    AIR_LIKE_BLOCKS,
    BREATHABLE_AIR_BLOCKS,
    WATER_BLOCKS,
    PASSABLE_TRANSPARENT_BLOCKS,
    HAZARD_BLOCK_TYPES,
    SURFACE_SHORE_PRIORITY_BLOCKS,
    SHORE_LAND_BLOCKS,
    NETHER_SHORE_BLOCKS,
    LAVA_SHORE_BLOCKS,
    TRAP_BLOCKS,
    SAFE_TACTICAL_FILLERS,
    MELEE_WEAPON_ORDER,
    LONE_COUNTERKILL_MOBS,
    CORNERED_PIGLIN_MAX_HEALTH,
    CORNERED_PIGLIN_REACH,
    PRE_HURT_MELEE_MOBS,
    RANGED_MOBS,
    WARDEN_EVADE_DISTANCE,
    CARDINAL_VECTORS,
    attack_feedback_hits,
    shot_release_confirmed,
    _clean_block_name,
    _is_passable,
    _is_los_transparent,
    _is_full_cover_block,
    _is_lava_block,
    _water_requires_surface,
    _combat_status_unavailable,
    _hitbox_columns,
    footing_blocks,
    _is_control_signal,
    retreat_made_progress,
    ranged_retreat_positions,
    melee_clear_for_projectile_cover,
    _is_solid_ground,
    aim_yaw_pitch,
    yaw_to_unit_xz,
    ThreatTrigger,
    CombatOutcome,
    CombatController,
    _TerrainProbeExpired,
    _CombatFortificationInterrupted,
)

from mc_agent.skills.combat_env.terrain_tactics import _TerrainTacticsMixin
from mc_agent.skills.combat_env.lava_cover_flee import _LavaCoverFleeMixin
from mc_agent.skills.combat_env.water_shore import _WaterShoreMixin
from mc_agent.skills.combat_env.instinct import *  # noqa: F401,F403
from mc_agent.skills.combat_env.instinct import CombatControllerSlot, SubcorticalInstinctController

class CombatEnv(
    _TerrainTacticsMixin,
    _LavaCoverFleeMixin,
    _WaterShoreMixin,
):
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
        self._stalled_water_shores: dict[tuple[float, ...], tuple[tuple[float, ...], tuple[Any, Any], float]] = {}
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

