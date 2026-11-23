"""GenerationGuard reflex/preemption package facade (split from legacy guardian.py)."""

from __future__ import annotations

from mc_agent.harness.guardian._base import (
    contextmanager,
    ContextVar,
    copy,
    deepcopy,
    dataclass,
    replace,
    json,
    math,
    threading,
    time,
    Any,
    Callable,
    Sequence,
    httpx,
    BotSession,
    ApiError,
    RequestCancelled,
    RunTimedOut,
    CARDINAL_VECTORS,
    CORNERED_PIGLIN_MAX_HEALTH,
    CORNERED_PIGLIN_REACH,
    LONE_COUNTERKILL_MOBS,
    MELEE_WEAPON_ORDER,
    PRE_HURT_MELEE_MOBS,
    RANGED_MOBS,
    SUBCORTICAL_COMBAT_BUDGET_S,
    WARDEN_EVADE_DISTANCE,
    CombatController,
    CombatControllerSlot,
    CombatEnv,
    SubcorticalInstinctController,
    ThreatTrigger,
    _CombatFortificationInterrupted,
    _TerrainProbeExpired,
    aim_yaw_pitch,
    footing_blocks,
    shot_release_confirmed,
    melee_clear_for_projectile_cover,
    ranged_retreat_positions,
    retreat_made_progress,
    yaw_to_unit_xz,
    MacroSkills,
    _inventory_counts_from_raw,
    best_melee_weapon_in_inv,
    ray_wall_blocker,
    SAFE_FOOD_PRIORITY,
    auto_equip_survival_gear,
    water_oxygen_below,
    IN_WALL_HURT_SOURCES,
    LOW_HP_BUNKER_MAX,
    RANGED_HURT_SOURCES,
    MELEE_HURT_SOURCES,
    BLAST_HURT_SOURCES,
    LAVA_HURT_SOURCES,
    FIRE_HURT_SOURCES,
    NO_DIG_COVER_REASONS,
    NON_COMBAT_HURT_SOURCES,
    MINING_WATER_WATCH_TOOLS,
    GATHERING_TOOLS,
    NON_PREEMPTABLE_TOOLS,
    ACTION_LOCK_ENDPOINTS,
    READ_ONLY_POLL_ENDPOINTS,
    HOT_FLOOR_WATCH_TOOLS,
    ReflexPreempted,
    _RecoveryBudgetExceeded,
    _UnstickTransportInterrupted,
    _WaterRecoveryEnteredLava,
    _UnsafeUnstickExit,
    _UnsafeFleeExit,
    _UnsafeCombatStep,
    _UnsafeCreeperAttack,
    _UnsafeCreeperMove,
    _UnsafeDig,
    _MiningLandingDenied,
    _UnsafeFoodUse,
    _StationaryWarning,
    _MiningMeleeSite,
    _GuardSession,
)

from mc_agent.harness.guardian.recovery import _RecoveryMixin
from mc_agent.harness.guardian.sensing import _SensingMixin
from mc_agent.harness.guardian.poll_loop import _PollLoopMixin
from mc_agent.harness.guardian.retreat_guard import _RetreatGuardMixin
from mc_agent.harness.guardian.intercept import _InterceptMixin
from mc_agent.harness.guardian.preemption import _PreemptionMixin

class GenerationGuard(
    _RecoveryMixin,
    _SensingMixin,
    _PollLoopMixin,
    _RetreatGuardMixin,
    _InterceptMixin,
    _PreemptionMixin,
):
    LAVA_ESCAPE_BUDGET_S = 6.5


    WATER_ESCAPE_BUDGET_S = 20.0


    SURFACE_WATER_ESCAPE_BUDGET_S = 4.0


    COMBAT_WATER_ESCAPE_BUDGET_S = 4.0


    ATTACK_WATER_THREAT_RANGE = 16.0


    MOVE_DROWNED_THREAT_RANGE = 10.0


    TOOL_LEGACY_OXYGEN_HINT_MAX = 140.0


    STATUS_FULL_OXYGEN = 20.0


    TOOL_WATER_ESCAPE_BUBBLES = 10.0


    TOOL_WATER_SUBMERSION_GRACE_S = 2.0


    MINING_UNSAFE_FOOTING_RECHECK_S = 2.0


    WATER_SHORE_MAX_HP = 6.0


    WATER_ESCAPE_RETRY_WINDOW_S = 30.0


    IN_WALL_RECOVERY_BUDGET_S = 6.0


    COMBAT_RECOVERY_BUDGET_S = 8.0


    COMBAT_COVER_BUDGET_S = 3.5


    ARROW_COVER_PLACEMENT_BUDGET_S = 2.1


    INTERRUPTED_BUNKER_RETRY_S = 4.0


    SPAWN_HURT_RESUME_S = 4.0


    UNSTICK_BUDGET_S = 30.0


    FLEE_BUDGET_S = 35.0


    HOT_FLOOR_RETRY_S = 5.0


    MELEE_EAT_DEFER_S = 10.0


    RANGED_EAT_DEFER_S = 8.0


    TRIDENT_EAT_THREAT_S = 30.0


    NETHER_BURN_RANGED_WINDOW_S = 12.0


    PRE_HURT_TARGET_COOLDOWN_S = 3.0


    PRE_HURT_RANGED_COOLDOWN_S = 30.0


    PRE_HURT_MELEE_COOLDOWN_S = 12.0


    MELEE_DIG_TIMEOUT_RETRY_S = 24.0


    MELEE_DIG_TIMEOUT_RESCAN_S = 2.0


    RANGED_COMBAT_STANCE_S = 35.0


    MINING_MELEE_REENTRY_S = 60.0


    def __init__(
        self,
        bot: Any,
        events: Any,
        *,
        checkpoint: Callable[[], None],
        interval_s: float = 0.25,
        geofence: Any | None = None,
        skills: Any | None = None,
        combat_controller: CombatController | None = None,
    ) -> None:
        self.raw_bot = bot
        self.bot = _GuardSession(bot, checkpoint)
        self.events = events
        self.interval_s = interval_s
        self.geofence = geofence
        # Sharing the live MacroSkills instance lets reflexes see the MiningDFS shaft state
        self.skills = skills
        self.combat_env = CombatEnv(
            self.bot, skills=self.skills, geofence=self.geofence, water_escape=self._escape_water_for_combat,
            run_lava_escape=self._run_lava_escape, run_fortification=self._run_combat_fortification,
            post_combat_food=False,
        )
        self.combat_slot = CombatControllerSlot(combat_controller)
        self._custom_combat_registered = combat_controller is not None
        self.last_poll = float("-inf")
        self.last_reaction = float("-inf")
        self._last_water_escape_at = float("-inf")
        self._last_water_escape_failed = False
        self._last_underground_water_preemption: tuple[tuple[Any, Any], float] | None = None
        self._last_surface_mining_water: tuple[
            tuple[Any, Any], float, tuple[float, ...], tuple[tuple[float, float, float], ...],
        ] | None = None
        self._mining_water_reentry_site: tuple[tuple[int, int, int], tuple[str, Any]] | None = None
        self._mining_melee_site: _MiningMeleeSite | None = None
        self._water_hurt_streak = 0
        self._no_water_until = float("-inf")
        self._last_pre_hurt_melee_at = float("-inf")
        self._last_fire_douse_at = float("-inf")
        self._last_fire_clear_at = float("-inf")
        self._last_non_combat_alert_at = float("-inf")
        self._last_gear_check_at = float("-inf")
        self._last_torch_place_at = float("-inf")
        self._hot_floor_retry_at = float("-inf")
        self._last_hot_floor_block: tuple[str, int, int, int] | None = None
        self._last_hot_floor_health: float | None = None
        self.previous_health: float | None = None
        self.previous_ready: bool | None = None
        self.cursor = int(getattr(bot, "_last_event_seq", 0) or 0)
        self._eat_history_checked = self.cursor <= 0
        self.failures = 0
        self.disconnected = False
        self.retry_after_ready = False
        self._pending_spawn_hurts: tuple[tuple[Any, Any], float, list[dict[str, Any]]] | None = None
        self._in_reflex = False
        self._consecutive_flees = 0
        self._shield_only_streak = 0
        self._last_shield_only_at = float("-inf")
        self._pre_hurt_suppress_block: tuple[int, int, int] | None = None
        self._pre_hurt_suppress_until: float = float("-inf")
        self._pre_hurt_target_cooldown: dict[str, float] = {}
        self._stationary_warnings: dict[str, _StationaryWarning] = {}
        self._stationary_melee_dig_timeout: tuple[float, dict[str, Any], list[float], float | None] | None = None
        self._last_stalled_lava: tuple[tuple[float, ...], tuple[Any, ...], float] | None = None
        self._ranged_combat_stance_until: float = float("-inf")
        self._ranged_cover_active_until: float = float("-inf")
        self._last_ranged_hurt_at = float("-inf")
        self._last_attack_read_timeout_at = float("-inf")
        self._last_trident_hurt_at = float("-inf")
        self._last_melee_hurt_at = float("-inf")
        self._nether_melee_eat_defer_until = float("-inf")
        self._last_lava_contact_at = float("-inf")
        self._last_fire_contact_at = float("-inf")
        self._last_burn_hurt_at = float("-inf")
        self._last_dig_cover_fail_at: float = float("-inf")
        self._blocked_bunker: tuple[tuple[Any, ...], list[int], str] | None = None
        self._last_interrupted_bunker: tuple[tuple[Any, ...], float] | None = None
        self._blocked_flee_site: tuple[tuple[float, ...], tuple[Any, Any], float] | None = None
        self._last_dig_cover_block: tuple[int, int, int] | None = None
        self._last_dig_cover_at: float = float("-inf")
        self.stats = {
            "polls": 0,
            "incidents": 0,
            "actions": 0,
            "cancelled_generations": 0,
            "tool_preemptions": 0,
            "boundary_stabilizations": 0,
        }

