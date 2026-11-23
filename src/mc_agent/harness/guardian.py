"""Full-lifecycle deterministic spinal & subcortical reflexes (`L3/L4`).

Covers all four previously vulnerable windows without competing for the server action lock:
1. Active macro-tool / `pre_ensure` execution (`run_with_preemption`): intercepts `interrupted_by=='hurt'`
   between internal steps and runs a `0.30s` delayed read-only creeper/lava watchdog that sends `/stop`
   to unwind the active tool onto the main thread for 0-LLM combat/evasion takeover.
2. Post-tool / pre-`rate_wait` boundary (`stabilize_after_tools`): escapes water/lava, clears `in_wall`,
   engages close hostiles via `CombatControllerSlot`, and eats food BEFORE entering RPM rate-limit wait.
3. `RpmLimiter.acquire` & `HTTP 429` backoff sleeps (`poll` @ `0.25s`).
4. LLM streaming inference (`poll` @ `0.25s`, including pre-hurt creeper/melee/ranged LOS reflexes).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from dataclasses import dataclass, replace
import json
import math
import threading
import time
from typing import Any, Callable, Sequence

import httpx

from ..bot import BotSession
from ..errors import ApiError
from .control import RequestCancelled, RunTimedOut
from ..skills.combat_env import (
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
)
from ..skills.library import MacroSkills, best_melee_weapon_in_inv, ray_wall_blocker
from ..skills.safety import SAFE_FOOD_PRIORITY, auto_equip_survival_gear, water_oxygen_below

# Hurt sources that mean "my body is embedded in a block", not "something is attacking me".
IN_WALL_HURT_SOURCES = frozenset({"in_wall", "suffocation", "cramming", "inside_block"})

# At or below this health, stop trying to walk away while being hit: seal up in place instead.
LOW_HP_BUNKER_MAX = 10.0

# Ranged damage: sprinting away in open cave air just makes the firing line longer.
RANGED_HURT_SOURCES = frozenset({"arrow", "projectile", "trident", "fireball", "small_fireball", "wither_skull", "skeleton"})
MELEE_HURT_SOURCES = frozenset({"mob_attack", "player_attack", "spear"})
BLAST_HURT_SOURCES = frozenset({"player_explosion", "entity_explosion", "fireball", "small_fireball", "wither_skull"})

# Environmental / tick damage sources that a shield cannot block and must not trigger fake combat reflexes.
LAVA_HURT_SOURCES = frozenset({"lava"})
FIRE_HURT_SOURCES = frozenset({"on_fire", "in_fire", "fire"})
NO_DIG_COVER_REASONS = frozenset({"no_filler_block", "no_solid_wall"})
NON_COMBAT_HURT_SOURCES = frozenset(
    {
        "on_fire",
        "in_fire",
        "fire",
        "hot_floor",
        "fall",
        "drown",
        "starve",
        "wither",
        "magic",
        "poison",
        "cactus",
        "sweet_berry_bush",
        "freeze",
        "dry_out",
        "out_of_world",
        "falling_stalactite",
        "stalagmite",
    }
)

MINING_WATER_WATCH_TOOLS = frozenset({"mc_skill_mine_dfs", "mc_skill_mine_blocks"})
GATHERING_TOOLS = frozenset({"mc_skill_mine_blocks", "mc_skill_harvest_logs"})

# Tools that already manage combat/stabilization directly and must not be recursively preempted.
NON_PREEMPTABLE_TOOLS = frozenset(
    {
        "mc_attack",
        "mc_shoot",
        "mc_posture",
        "mc_look",
        "mc_stop",
        "mc_skill_flee",
        "mc_skill_stabilize",
        "mc_skill_bunker",
        "mc_status",
        "mc_inventory",
        "mc_events",
        "mc_find_blocks",
        "mc_get_entities",
        "mc_get_block",
        "mc_advancements",
        "mc_advancement_frontier",
        "mc_kb_lookup",
        "read_tool_result",
    }
)

ACTION_LOCK_ENDPOINTS = frozenset(
    {
        "move_to",
        "dig",
        "place",
        "craft",
        "interact",
        "use_item",
        "window/act",
        "control",
    }
)

READ_ONLY_POLL_ENDPOINTS = frozenset({"status", "inventory", "events"})
HOT_FLOOR_WATCH_TOOLS = frozenset({"mc_move_to", "mc_skill_mine_dfs", "mc_skill_staircase", "mc_skill_combat"})


class ReflexPreempted(BaseException):
    """Internal signal raised to unwind an active macro skill into 0-LLM subcortical takeover."""

    def __init__(self, trigger: ThreatTrigger) -> None:
        super().__init__(f"Reflex preempted by {trigger.get('source')}: {trigger.get('primary_type')}")
        self.trigger = trigger


class _RecoveryBudgetExceeded(BaseException):
    """Unwind a recovery macro through its transport and retry handlers."""


class _UnstickTransportInterrupted(Exception):
    """Carry the actual failed request past the tool's generic network wrapper."""

    def __init__(self, error: httpx.TransportError, endpoint: str, action_pending: bool) -> None:
        super().__init__(endpoint)
        self.transport_error = error
        self.endpoint = endpoint
        self.action_pending = action_pending


class _WaterRecoveryEnteredLava(BaseException):
    """Unwind the water macro when status confirms lava in the same life."""


class _UnsafeUnstickExit(BaseException):
    """Unwind an unstick whose final movement has no verified footing."""


class _UnsafeFleeExit(BaseException):
    """Unwind an explicit retreat before an unsupported ground movement."""


class _UnsafeCombatStep(BaseException):
    """Unwind a chase before an unsupported ground movement."""


class _UnsafeCreeperAttack(BaseException):
    """Prevent an explicit melee chase toward a confirmed creeper."""


class _UnsafeDig(BaseException):
    """Unwind a dig that would expose the bot to an unsafe drop or fluid."""


class _MiningLandingDenied(ApiError):
    """Reject a single DFS approach so the miner can choose another route."""


class _UnsafeFoodUse(BaseException):
    """Unwind ordinary food use while the bot is exposed to a hazard."""


@dataclass(frozen=True)
class _StationaryWarning:
    entity_id: int
    pos: tuple[float, ...]
    distance: float
    observed_at: float
    life: tuple[Any, Any]
    close_melee_observation: tuple[tuple[float, ...], tuple[Any, ...]] | None = None


@dataclass(frozen=True)
class _MiningMeleeSite:
    target: tuple[int, ...]
    contact_pos: tuple[float, ...]
    life: tuple[str, Any]
    entity_id: int
    expires_at: float


class _GuardSession:
    def __init__(
        self,
        bot: Any,
        checkpoint: Callable[[], None],
        *,
        deadline: float | None = None,
        call_override: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.bot = bot
        self.checkpoint = checkpoint
        self._deadline = ContextVar("guard_deadline", default=deadline)
        self._call_override = call_override

    @property
    def active_deadline(self) -> float | None:
        return self._deadline.get()

    @contextmanager
    def limit_until(self, deadline: float):
        previous = self._deadline.get()
        token = self._deadline.set(deadline if previous is None else min(previous, deadline))
        try:
            yield
        finally:
            self._deadline.reset(token)

    @contextmanager
    def pause_deadline(self, action: str):
        """Exclude time spent in a separately budgeted recovery."""
        deadline = self._deadline.get()
        if deadline is None:
            yield
            return
        self._time_remaining(action)
        started = time.monotonic()
        token = self._deadline.set(None)
        try:
            yield
        finally:
            self._deadline.reset(token)
            self._deadline.set(deadline + time.monotonic() - started)

    def _time_remaining(self, action: str) -> float:
        deadline = self._deadline.get()
        remaining = float("inf") if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise _RecoveryBudgetExceeded(action)
        return remaining

    def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        self.checkpoint()
        readonly = action in {"status", "events", "inventory", "get_entities", "get_block", "find_blocks"}
        timeout = kwargs.get("timeout_s")
        if timeout is None:
            timeout = 2.0 if readonly else 8.0
        if self._deadline.get() is not None:
            remaining = self._time_remaining(action)
            if action == "status" and isinstance(self.bot, BotSession) and not isinstance(timeout, httpx.Timeout):
                timeout = httpx.Timeout(timeout, connect=min(timeout, 0.25), write=min(timeout, 0.1), pool=min(timeout, 0.1))
            if isinstance(timeout, httpx.Timeout):
                # BotSession can retry a closed keepalive after a 150ms delay.
                attempt_budget = (remaining - 0.15) / 2
                if attempt_budget <= 0:
                    raise _RecoveryBudgetExceeded(action)
                phase_budget = attempt_budget / 4
                overhead = {
                    phase: min(seconds, phase_budget) if seconds is not None else phase_budget
                    for phase, seconds in timeout.as_dict().items() if phase != "read"
                }
                read_budget = attempt_budget - sum(overhead.values())
                read_timeout = min(timeout.read, read_budget) if timeout.read is not None else read_budget
                timeout = httpx.Timeout(read_timeout, **overhead)
            else:
                timeout = min(timeout, remaining)
        kwargs["timeout_s"] = timeout
        read_limit = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
        if isinstance(body, dict) and isinstance(body.get("timeout"), (int, float)) and read_limit is not None:
            max_body_ms = max(200, int(max(0.2, float(read_limit) - 0.5) * 1000))
            if body["timeout"] >= float(read_limit) * 1000:
                body = {**body, "timeout": min(int(body["timeout"]), max_body_ms)}
        kwargs["allow_reauth"] = False
        try:
            call = self._call_override if self._call_override is not None else self.bot.call
            result = call(action, body, **kwargs)
        except httpx.TimeoutException:
            self._time_remaining(action)
            raise
        self.checkpoint()
        self._time_remaining(action)
        return result

    def status(self) -> dict[str, Any]:
        return self.call("status", method="GET")

    def inventory(self) -> dict[str, Any]:
        return self.call("inventory", method="GET")


class GenerationGuard:
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

    @property
    def no_water_until(self) -> float:
        """Monotonic deadline before which re-entering water triggers immediate shore return."""
        return self._no_water_until

    def register_combat_controller(self, controller: CombatController) -> None:
        self.combat_slot.register(controller)
        self._custom_combat_registered = True

    def _equip_and_raise_shield_for_water(
        self,
        status: dict[str, Any],
        actions: list[dict[str, Any]],
        *,
        inv: dict[str, Any],
        attacker: dict[str, Any] | None,
        action_prefix: str = "",
        raise_on_http_error: bool = True,
    ) -> tuple[bool, bool]:
        """Equip the off-hand shield, face the attacker, and float when in water."""
        counts, has_shield, _ = self._parse_inventory_summary(inv)
        if not has_shield:
            return False, False
        if status.get("blocking"):
            return True, False

        offhand = inv.get("offhand") if isinstance(inv, dict) else None
        in_offhand = str((offhand or {}).get("name") or "").removeprefix("minecraft:") == "shield"
        if isinstance(inv, dict) and "offhand" in inv and not in_offhand and counts.get("shield", 0) > 0:
            eq_act = self._action(
                f"{action_prefix}equip_shield",
                lambda: self.bot.call("equip", {"item": "shield", "slot": "off-hand"}),
                raise_on_http_error=raise_on_http_error,
            )
            actions.append(eq_act)
            err = eq_act.get("error") if isinstance(eq_act, dict) else None
            if isinstance(err, dict) and err.get("code") in {"not_connected", "not_ready", "wrong_server", "network_uncertain"}:
                return True, True

        if isinstance(attacker, dict) and attacker.get("id") is not None:
            actions.append(
                self._action(
                    f"{action_prefix}face_attacker",
                    lambda: self.bot.call("look", {"entity_id": attacker["id"]}),
                    raise_on_http_error=raise_on_http_error,
                )
            )
        actions.append(
            self._action(
                f"{action_prefix}raise_shield",
                lambda: self.bot.call(
                    "posture",
                    {
                        "shield": True,
                        "jump": bool(status.get("in_water")),
                        "sneak": bool(status.get("sneaking")),
                    },
                ),
                raise_on_http_error=raise_on_http_error,
            )
        )
        return True, False

    def prime(self) -> None:
        """Drain historical `/events` cursor, record baseline health, and auto-equip ready armor/shield."""
        try:
            st = self.bot.status()
            self.previous_health = float(st["health"]) if isinstance(st.get("health"), (int, float)) else None
            self.previous_ready = bool(st.get("ready", st.get("alive", True)))
            self._drain_events_pages(observe_combat_hurt=False)
            setattr(self.raw_bot, "_last_event_seq", self.cursor)
            if st.get("ready", True) and st.get("alive") is not False:
                inv = self.bot.inventory()
                if isinstance(inv, dict) and (isinstance(inv.get("armor"), dict) or "offhand" in inv):
                    auto_equip_survival_gear(self.bot, inv, allow_armor=True, allow_shield=True)
                    self._last_gear_check_at = time.monotonic()
        except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
            raise
        except (ApiError, httpx.HTTPError):
            pass

    def _skills(self) -> Any:
        """Live MacroSkills (with MiningDFS shaft state) when wired, else a throwaway binding."""
        return self.skills if self.skills is not None else MacroSkills(self.bot)

    @staticmethod
    def _reported_action_release(result: Any) -> bool | None:
        if isinstance(result, dict):
            released = result.get("released")
            if isinstance(released, bool):
                return released
            if result.get("stopped") is True and "action" in result and result["action"] is None:
                return True
        return None

    def _stop_after_recovery_timeout(self) -> bool | None:
        try:
            result = self.raw_bot.call("stop", {}, timeout_s=1.5, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            return None
        return self._reported_action_release(result)

    def _run_lava_escape(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            with self.bot.limit_until(time.monotonic() + self.LAVA_ESCAPE_BUDGET_S):
                return operation()
        except _RecoveryBudgetExceeded as error:
            self._stop_after_recovery_timeout()
            self.events.emit(
                "guardian.lava_budget_exceeded",
                endpoint=error.args[0] if error.args else None,
                budget_s=self.LAVA_ESCAPE_BUDGET_S,
            )
            return {"ok": False, "in_lava": True, "reason": "time_budget", "partial": True}

    def _run_combat_fortification(
        self, before: dict[str, Any], operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        combat_deadline = self.bot.active_deadline
        if combat_deadline is None:
            return operation()
        now = time.monotonic()
        # Leave time to stop a pending write, refresh status, and prepare a retreat.
        deadline = min(now + 3.0, combat_deadline - 3.5)
        failure = {"ok": False, "reason": "fortification_budget"}
        if deadline - now < 0.3:
            return failure
        try:
            with self.bot.limit_until(deadline):
                return operation()
        except (_RecoveryBudgetExceeded, httpx.TimeoutException) as error:
            self.combat_env.invalidate_block_cache()
            endpoint = error.args[0] if isinstance(error, _RecoveryBudgetExceeded) and error.args else None
            self.events.emit("guardian.fortification_budget_exceeded", endpoint=endpoint)

        stopped = False
        latest = {}
        try:
            stopped = self.bot.call("stop", {}, timeout_s=0.5).get("ok") is not False
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
            pass
        try:
            latest = self.bot.call("status", method="GET", timeout_s=0.5)
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
            pass
        life_unavailable = (
            latest.get("ready") is False or latest.get("alive") is False
            or bool(latest and any(latest.get(key) != before.get(key) for key in ("dimension", "last_death")))
        )
        if life_unavailable or not stopped or not latest.get("pos") or any(
            latest.get(key) for key in ("busy", "in_water", "in_lava")
        ):
            raise _CombatFortificationInterrupted(latest, life_unavailable=life_unavailable)
        return failure

    def _run_subcortical_combat(self, trigger: ThreatTrigger) -> dict[str, Any]:
        skills = self.combat_env.skills
        try:
            with self.bot.limit_until(time.monotonic() + SUBCORTICAL_COMBAT_BUDGET_S):
                if isinstance(skills, MacroSkills):
                    # Keep shared terrain records while binding the macro's calls to this deadline.
                    scoped_skills = copy(skills)
                    scoped_skills.bot = self.bot
                    self.combat_env.skills = scoped_skills
                return self.combat_slot.handle(self.combat_env, trigger)
        except _RecoveryBudgetExceeded as error:
            # The combat deadline can interrupt the slot's own exit cleanup.
            while isinstance(error.__context__, _RecoveryBudgetExceeded):
                error = error.__context__
            cleanup_started = time.monotonic()
            timeout = httpx.Timeout(0.35, connect=0.1, write=0.1, pool=0.1)
            status = {}
            try:
                with self.bot.limit_until(cleanup_started + 1.5):
                    for action, body in (("stop", {}), ("control", {"wait": False})):
                        try:
                            self.bot.call(action, body, timeout_s=timeout)
                        except (ApiError, httpx.HTTPError):
                            pass
                    try:
                        status = self.bot.call("status", method="GET", timeout_s=timeout)
                    except (ApiError, httpx.HTTPError):
                        pass
                    posture = {}
                    if status.get("in_water") or status.get("in_lava"):
                        posture = {"jump": True, "shield": bool(status.get("blocking"))}
                    try:
                        self.bot.call("posture", posture, timeout_s=timeout)
                    except (ApiError, httpx.HTTPError):
                        pass
            except _RecoveryBudgetExceeded:
                pass
            endpoint = error.args[0] if error.args else None
            self.events.emit(
                "guardian.combat_budget_exceeded",
                endpoint=endpoint,
                budget_s=SUBCORTICAL_COMBAT_BUDGET_S,
                cleanup_s=round(time.monotonic() - cleanup_started, 3),
            )
            return {
                "ok": False, "handled": False, "mode": "combat_timeout",
                "reason": "time_budget", "requires_replan": True,
                **({"timeout_endpoint": endpoint} if endpoint is not None else {}),
                "summary": "战斗反射达到时限，已尝试停止挂起动作",
            }
        finally:
            self.combat_env.skills = skills

    def _clear_contact_fire(self, pos: list[float] | tuple[float, ...], *, sneaking: bool = False) -> dict[str, Any]:
        try:
            with self.bot.limit_until(time.monotonic() + 1.25):
                return self.combat_env.extinguish_contact_fire(pos, sneaking=sneaking)
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return {"ok": False, "reason": "time_budget"}

    def _douse_after_lava(
        self, before: dict[str, Any], *, after: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if after is None:
            try:
                after = self.bot.call("status", method="GET", timeout_s=0.5)
            except (ApiError, httpx.HTTPError):
                return None
        health = after.get("health")
        _, hostile_dist, _, _ = self._extract_hostile_info(after)
        if (
            after.get("ready") is True and after.get("alive") is not False and after.get("pos")
            and after.get("on_ground") is True and after.get("on_fire") and not after.get("in_water") and not after.get("in_lava")
            and "nether" not in str(after.get("dimension") or "").lower()
            and isinstance(health, (int, float)) and 0 < health <= 10.0 and hostile_dist > 8.0
            and after.get("last_death") == before.get("last_death") and after.get("dimension") == before.get("dimension")
        ):
            try:
                with self.bot.limit_until(time.monotonic() + 1.5):
                    return self._action("douse_in_water", lambda: self.combat_env.douse_in_adjacent_water(after))
            except _RecoveryBudgetExceeded:
                self._stop_after_recovery_timeout()
        return None

    def _settle_dry_landing(
        self, before: dict[str, Any], after: dict[str, Any], bot: _GuardSession, *, deadline: float,
        checks: int = 2, can_wait: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        for _ in range(checks):
            if (
                after.get("on_ground") is not False
                or any(after.get(key) for key in ("in_water", "in_lava", "submerged"))
                or after.get("alive") is False or after.get("ready") is False
                or any(after.get(key) != before.get(key) for key in ("last_death", "dimension"))
                or deadline - time.monotonic() < 0.35
                or (can_wait is not None and not can_wait(after))
            ):
                break
            time.sleep(0.1)
            try:
                after = bot.call("status", method="GET", timeout_s=0.25, allow_reauth=False)
            except (ApiError, httpx.HTTPError):
                break
        return after

    def _settle_mining_shore_jump(
        self, before: dict[str, Any], after: dict[str, Any], shore: dict[str, Any],
    ) -> dict[str, Any]:
        bank = shore.get("pos_after")
        actions = shore.get("actions")
        if not (
            shore.get("ok") is True and shore.get("in_water") is False
            and isinstance(actions, list) and "shore_lip_jump" in actions
            and after.get("in_water") is False and after.get("on_ground") is False
            and isinstance(bank, (list, tuple)) and len(bank) >= 3
            and all(type(coord) in (int, float) and math.isfinite(coord) for coord in bank[:3])
        ):
            return after

        starting_health = after.get("health")

        def can_wait(status: dict[str, Any]) -> bool:
            name, distance, _, _ = self._extract_hostile_info(status)
            health, pos = status.get("health"), status.get("pos")
            return (
                status.get("ready") is True and isinstance(health, (int, float))
                and health > self.WATER_SHORE_MAX_HP and health == starting_health
                and not (status.get("busy") or status.get("on_fire"))
                and (not name or distance > 8.0)
                and isinstance(pos, (list, tuple)) and len(pos) >= 3
                and all(type(coord) in (int, float) and math.isfinite(coord) for coord in pos[:3])
                and math.dist(bank[:3], pos[:3]) <= 1.5
            )

        return self._settle_dry_landing(
            before, after, self.bot, deadline=time.monotonic() + 0.95, checks=6, can_wait=can_wait,
        )

    @staticmethod
    def _nether_fast_fall(status: dict[str, Any]) -> bool:
        velocity = status.get("velocity")
        return bool(
            str(status.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
            and status.get("on_ground") is False and status.get("pos")
            and status.get("ready") is True and status.get("alive") is not False
            and not any(status.get(key) for key in ("in_water", "in_lava", "submerged"))
            and isinstance(velocity, (list, tuple)) and len(velocity) >= 2
            and isinstance(velocity[1], (int, float)) and math.isfinite(velocity[1]) and velocity[1] <= -10.0
        )

    @staticmethod
    def _same_life(before: dict[str, Any], current: dict[str, Any]) -> bool:
        return (
            current.get("ready") is True and current.get("alive") is not False
            and all(current.get(key) == before.get(key) for key in ("last_death", "dimension"))
        )

    def _observe_nether_fall(self, status: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Recheck a fast fall for contact or lava immediately below the hitbox."""
        deadline = time.monotonic() + 0.7
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        try:
            current = self._settle_dry_landing(status, status, bot, deadline=deadline)
        except _RecoveryBudgetExceeded:
            return status, False
        if current is status or not self._same_life(status, current):
            return current, False
        above_lava = False
        if current.get("on_ground") is False and current.get("pos") and not any(
            current.get(key) for key in ("in_lava", "in_water", "submerged")
        ):
            try:
                above_lava = CombatEnv(bot)._is_airborne_above_lava(current, start_pos=status["pos"])
            except _RecoveryBudgetExceeded:
                pass
        if current.get("in_lava") or above_lava:
            self.events.emit(
                "guardian.nether_fall_lava", pos_before=status["pos"], velocity=status.get("velocity"),
                pos=current.get("pos"), in_lava=bool(current.get("in_lava")), above_lava=above_lava,
            )
        return current, above_lava

    def _run_recovery_macro(
        self,
        before: dict[str, Any],
        run: Callable[[Any], dict[str, Any]],
        *,
        budget_s: float,
        timeout_details: dict[str, Any] | None = None,
        confirm_shore: bool = False,
        confirm_failed_life: bool = False,
    ) -> dict[str, Any]:
        skills = self._skills()
        deadline = time.monotonic() + budget_s

        def watch_water_status(action: str, body: dict[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
            result = self.raw_bot.call(action, body, **kwargs)
            if (
                action == "status" and result.get("in_lava")
                and result.get("alive") is not False and result.get("ready") is not False
                and result.get("last_death") == before.get("last_death")
                and result.get("dimension") == before.get("dimension")
            ):
                raise _WaterRecoveryEnteredLava
            return result

        recovery_bot = _GuardSession(
            self.raw_bot, self.bot.checkpoint, deadline=deadline,
            call_override=watch_water_status if confirm_shore else None,
        )
        if isinstance(skills, MacroSkills):
            # Keep the shared avoidance records while leaving the tool runner's session binding intact.
            skills = copy(skills)
            skills.bot = recovery_bot
        try:
            result = run(skills)
            if not result.get("ok") and (
                result.get("respawned") or (result.get("mode") != "flee" and not confirm_failed_life)
            ):
                return result
            if confirm_failed_life and not result.get("ok"):
                try:
                    after = recovery_bot.call("status", method="GET", timeout_s=0.6, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    return result
            else:
                after = recovery_bot.status()
            if confirm_shore:
                after = self._settle_dry_landing(before, after, recovery_bot, deadline=deadline)
        except httpx.ReadTimeout:
            if not confirm_shore or before.get("in_water") is not True:
                raise
            # Movement can finish even when its reply is lost. Require a fresh, idle landing.
            try:
                after = recovery_bot.call("status", method="GET", timeout_s=0.6, allow_reauth=False)
            except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
                after = {}
            start, end = before.get("pos"), after.get("pos")
            if not (
                start and end and math.hypot(end[0] - start[0], end[2] - start[2]) <= 64.0
                and after.get("on_ground") is True and after.get("in_water") is False
                and after.get("ready") is True and after.get("alive") is True
                and not any(after.get(key) for key in ("busy", "submerged", "in_lava", "held_controls"))
                and all(after.get(key) == before.get(key) for key in ("last_death", "dimension"))
            ):
                raise
            result = {"ok": True, "in_water": False, "pos_after": end, "confirmed_after_read_timeout": True}
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return {"ok": False, "reason": "time_budget", "partial": True, **(timeout_details or {})}
        start, end = before.get("pos"), after.get("pos") or result.get("pos_after")
        if (
            (start and end and math.hypot(end[0] - start[0], end[2] - start[2]) > 64.0)
            or after.get("alive") is False
            or (after.get("last_death") is not None and after["last_death"] != before.get("last_death"))
        ):
            return {**result, "ok": False, "respawned": True}
        if after.get("ready") is False:
            return {**result, "ok": False, "reason": "not_ready"}
        if result.get("mode") == "seal_shaft":
            seal_pos = result.get("pos_after")
            live_pos = after.get("pos")
            result = {**result, "pos_after": live_pos}
            # Placement can finish while knockback is moving the bot off the sealed step.
            if (
                after.get("on_ground") is not True or after.get("in_water") or after.get("in_lava")
                or not live_pos or not seal_pos
                or tuple(map(math.floor, live_pos[:3])) != tuple(map(math.floor, seal_pos[:3]))
            ):
                return {**result, "ok": False, "reason": "shaft_seal_landing_unconfirmed"}
        if confirm_shore and (after.get("on_ground") is False or after.get("in_water") or after.get("in_lava")):
            return {
                **result, "ok": False, "reason": "shore_landing_unconfirmed", "pos_after": end,
                "in_water": bool(after.get("in_water")), "in_lava": bool(after.get("in_lava")),
            }
        if confirm_shore:
            self.combat_env.remember_dry_bank(after)
        if (
            confirm_shore and before.get("blocking") and after.get("on_ground") is True
            and self._restore_shield_near_threat(before, after, recovery_bot)
        ):
            result = {**result, "shield_restored": True}
        if result.get("mode") == "flee" and after.get("in_lava"):
            return {**result, "ok": False, "reason": "retreat_into_lava", "pos_after": after.get("pos") or end}
        return result

    def _restore_shield_near_threat(
        self, before: dict[str, Any], after: dict[str, Any], bot: _GuardSession,
    ) -> bool:
        offhand = after.get("offhand")
        mob, distance, target_id, _ = self._extract_hostile_info(after)
        if not (
            after.get("blocking") is False and after.get("ready") is True and after.get("alive") is True
            and not after.get("in_water") and not after.get("in_lava")
            and after.get("dimension") == before.get("dimension")
            and after.get("last_death") == before.get("last_death")
            and isinstance(offhand, dict) and str(offhand.get("name") or "").removeprefix("minecraft:") == "shield"
            and target_id is not None
            and ((mob in PRE_HURT_MELEE_MOBS and distance <= 3.5) or (mob in RANGED_MOBS and distance <= 12.0))
        ):
            return False
        try:
            bot.call("look", {"entity_id": target_id}, timeout_s=0.35, allow_reauth=False)
            posture = bot.call(
                "posture", {"shield": True, "jump": False, "sneak": bool(after.get("sneaking"))},
                timeout_s=0.35, allow_reauth=False,
            )
            return posture.get("blocking") is True
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
            return False

    def _escape_water_for_combat(self, before: dict[str, Any]) -> dict[str, Any]:
        health = before.get("health")
        deadline = self.bot.active_deadline
        if (
            deadline is not None and before.get("in_water") is True
            and isinstance(health, (int, float)) and 0 < health <= 10.0
        ):
            # Leave time for a directed shore attempt if a short ascent does not reach dry ground.
            budget_s = min(self.COMBAT_WATER_ESCAPE_BUDGET_S, deadline - time.monotonic() - 2.0)
            if budget_s <= 0.25:
                return {"ok": False, "reason": "time_budget", "in_water": True}
            return self._escape_water(before, budget_s=budget_s)
        with self.bot.pause_deadline("escape_water"):
            return self._escape_water(before)

    def _escape_water(self, before: dict[str, Any], *, budget_s: float | None = None) -> dict[str, Any]:
        try:
            return self._run_recovery_macro(
                before, lambda skills: skills.escape_water(),
                budget_s=self.WATER_ESCAPE_BUDGET_S if budget_s is None else budget_s,
                timeout_details={"in_water": True},
                confirm_shore=True,
            )
        except _WaterRecoveryEnteredLava:
            escape = self._action("escape_lava", self.combat_env.escape_lava)
            return {
                **(escape.get("result") or {"in_lava": True}),
                "ok": bool(escape.get("ok")), "mode": "lava_escape",
            }

    def _swimming_at_low_health(self, status: dict[str, Any]) -> bool:
        health = status.get("health")
        return (
            isinstance(health, (int, float)) and health <= self.WATER_SHORE_MAX_HP
            and (status.get("on_ground") is False or bool(status.get("submerged")))
        )

    def _has_close_melee_in_water(self, status: dict[str, Any], *, include_wading: bool = False) -> bool:
        if not status.get("in_water") or not (
            include_wading or status.get("on_ground") is False or status.get("submerged")
        ):
            return False
        name, distance, _, _ = self._extract_hostile_info(status)
        return (
            name in PRE_HURT_MELEE_MOBS and 0 <= distance <= 4.5
            and not status["nearest_hostile"].get("blocked_by_wall")
        )

    @staticmethod
    def _is_grounded_wading(status: dict[str, Any]) -> bool:
        return bool(status.get("in_water")) and status.get("submerged") is False and status.get("on_ground") is True

    def _has_close_water_threat(self, status: dict[str, Any]) -> bool:
        if self._has_close_melee_in_water(status):
            return True
        if not self._is_grounded_wading(status):
            return False
        name, distance, _, _ = self._extract_hostile_info(status)
        if name == "drowned" and 0 <= distance <= 6.0:
            return status["nearest_hostile"].get("blocked_by_wall") is not True
        return name == "creeper" and 0 <= distance <= 4.5 and (
            distance <= 2.3 or status["nearest_hostile"].get("blocked_by_wall") is not True
        )

    def _urgent_water_melee(
        self, status: dict[str, Any], hits: Sequence[dict[str, Any]] = (), *, interrupted_source: str | None = None,
    ) -> bool:
        if not status.get("in_water"):
            return False
        melee_hits = [hit for hit in hits if str(hit.get("source") or "").strip().lower() in MELEE_HURT_SOURCES]
        if len(melee_hits) >= 2:
            return True
        if not melee_hits and str(interrupted_source or "").strip().lower() not in MELEE_HURT_SOURCES:
            return False
        # Ground contact in shallow water still leaves us exposed to the nearby attacker.
        if status.get("on_ground") is True and self._has_close_melee_in_water(status, include_wading=True):
            return True
        health_samples = [status.get("health"), *(hit.get("health") for hit in melee_hits)]
        return any(isinstance(health, (int, float)) and 0 < health <= 10.0 for health in health_samples)

    @staticmethod
    def _unattributed_fall_only(hurts: list[dict[str, Any]]) -> bool:
        return bool(hurts) and all(
            str(hit.get("source") or "").strip().lower() == "fall"
            and not (hit.get("source_entity") or hit.get("likely_attacker"))
            for hit in hurts
        )

    @staticmethod
    def _short_water_escape_after_hit(status: dict[str, Any], source: Any) -> bool:
        source = str(source or "").strip().lower()
        health = status.get("health")
        swimming = status.get("on_ground") is False or bool(status.get("submerged"))
        return source == "trident" or (
            isinstance(health, (int, float)) and health > 0
            and (source in RANGED_HURT_SOURCES or source in MELEE_HURT_SOURCES)
            and (swimming or health <= 14.0)
        )

    def _unstick_in_wall(self, before: dict[str, Any]) -> dict[str, Any]:
        return self._run_recovery_macro(
            before, lambda skills: skills.unstick(),
            budget_s=self.IN_WALL_RECOVERY_BUDGET_S, confirm_failed_life=True,
        )

    def _low_health_burning_after_ranged_hit(self, status: dict[str, Any], now: float) -> bool:
        health = status.get("health")
        return (
            bool(status.get("on_fire"))
            and isinstance(health, (int, float)) and 0 < health <= LOW_HP_BUNKER_MAX
            and now - self._last_ranged_hurt_at < self.NETHER_BURN_RANGED_WINDOW_S
        )

    def _defend_or_flee(
        self, status: dict[str, Any], attacker: dict[str, Any] | None, counts: dict[str, int],
        *, ranged: bool = False, prefer_cover: bool = False, allow_stationary_cover: bool = True, cover_only: bool = False,
        trident_attacker_id: int | None = None, verify_live_shooters: bool = False, require_safe_footing: bool = False,
        confirmed_blaze_cover: bool = False, confirm_trident_cover: bool = False,
    ) -> dict[str, Any]:
        skills = self._skills()
        health = status.get("health")
        dimension = str(status.get("dimension") or "").lower().removeprefix("minecraft:")
        # The legacy macro cannot counter below 11 HP and falls through to an unchecked sprint-jump.
        low_health = isinstance(health, (int, float)) and health < 11.0
        retreat_pos = status.get("pos")
        if (
            "nether" in dimension and low_health and status.get("on_ground") is False
            and not status.get("in_water") and isinstance(retreat_pos, (list, tuple)) and len(retreat_pos) >= 3
        ):
            probe_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 0.5)
            try:
                over_lava = CombatEnv(probe_bot)._is_airborne_above_lava(status, start_pos=retreat_pos)
            except _RecoveryBudgetExceeded:
                over_lava = False
            if over_lava:
                recovery = self._action("escape_lava", self.combat_env.escape_lava)
                result = recovery.get("result") or {}
                if not result.get("already_dry") or result.get("respawned"):
                    return {**result, "ok": bool(recovery.get("ok")), "mode": "lava_escape"}
        retreat_only = ranged or low_health or (
            dimension == "overworld" and isinstance(skills, MacroSkills)
            and best_melee_weapon_in_inv(counts) is None and not self._inside_own_shaft()
        )
        if cover_only or require_safe_footing or "nether" in dimension or (retreat_only and isinstance(skills, MacroSkills)):
            nh_name, nh_distance, nh_id, nh_pos = self._extract_hostile_info(status)
            now = time.monotonic()
            deadline = now + self.COMBAT_RECOVERY_BUDGET_S
            recent_melee = now - self._last_melee_hurt_at < 4.0
            mixed_hurt = low_health and not ranged and recent_melee and now - self._last_ranged_hurt_at < 8.0
            burning_blaze = "nether" in dimension and ranged and status.get("on_fire") and nh_name == "blaze"
            offhand = status.get("offhand")
            unshielded = (
                not status.get("blocking") and not counts.get("shield")
                and (not isinstance(offhand, dict) or str(offhand.get("name") or "").removeprefix("minecraft:") != "shield")
            )
            scan_surface_shooters = (
                dimension == "overworld" and ranged and status.get("on_ground") is True
                and (prefer_cover or (low_health and unshielded))
            )
            cover_requires_melee_clearance = (
                scan_surface_shooters and (
                    trident_attacker_id is not None or confirm_trident_cover or (
                        status.get("blocking")
                        and (cover_only or (isinstance(health, (int, float)) and 4.0 < health <= 6.0))
                    )
                )
            )
            attacker_id = attacker.get("id") if isinstance(attacker, dict) else None
            primary_id = attacker_id if isinstance(attacker_id, int) else nh_id
            if trident_attacker_id is not None:
                primary_id = trident_attacker_id
            scan_blaze_cover = (burning_blaze or confirmed_blaze_cover) and primary_id is not None
            surface_unarmed_retreat = (
                dimension == "overworld" and unshielded and best_melee_weapon_in_inv(counts) is None
            )
            melee_id = attacker_id if isinstance(attacker_id, int) else None
            if (
                melee_id is None and surface_unarmed_retreat and nh_name in PRE_HURT_MELEE_MOBS
                and (low_health or nh_distance <= 3.5)
            ):
                melee_id = nh_id
            resolve_melee = (
                (surface_unarmed_retreat or ("nether" in dimension and low_health))
                and recent_melee and not ranged and not mixed_hurt and melee_id is not None
            )
            pos = attacker.get("pos") if isinstance(attacker, dict) else None
            if not pos and (not resolve_melee or nh_id == melee_id):
                pos = nh_pos
            if not pos and resolve_melee:
                pos = self._find_hostile_position(melee_id, "", timeout_s=0.6)
            attacker_name = str(attacker.get("name") or "").removeprefix("minecraft:") if isinstance(attacker, dict) else ""
            shooter_unknown = scan_surface_shooters and attacker_name not in RANGED_MOBS and nh_name not in RANGED_MOBS
            fallback_pos = pos if shooter_unknown else None
            if shooter_unknown:
                pos = None
            other_shooters = []
            check_blaze_cover = False
            melee_clearance_confirmed = False
            if (
                (mixed_hurt or scan_surface_shooters or scan_blaze_cover)
                and isinstance(retreat_pos, (tuple, list)) and len(retreat_pos) >= 3
            ):
                try:
                    sighting = self.bot.call("get_entities", {"radius": 16, "types": ["hostile"]}, timeout_s=0.6)
                except (ApiError, httpx.HTTPError):
                    sighting = {}
                entities = sighting.get("entities") or []
                if confirm_trident_cover:
                    drowned_ids = {
                        entity["id"] for entity in entities if isinstance(entity, dict)
                        and type(entity.get("id")) is int
                        and str(entity.get("name") or "").removeprefix("minecraft:") == "drowned"
                    }
                    if trident_attacker_id is None and len(drowned_ids) == 1:
                        trident_attacker_id = next(iter(drowned_ids))
                    if trident_attacker_id not in drowned_ids:
                        return {"ok": False, "mode": "natural_cover", "reason": "trident_shooter_unconfirmed"}
                    primary_id = trident_attacker_id
                if mixed_hurt and not pos and primary_id is not None:
                    for entity in entities:
                        if not isinstance(entity, dict) or entity.get("id") != primary_id:
                            continue
                        live_pos = entity.get("pos")
                        if (
                            isinstance(live_pos, (tuple, list)) and len(live_pos) >= 3
                            and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in live_pos[:3])
                        ):
                            pos = live_pos
                            break
                shooters = ranged_retreat_positions(
                    retreat_pos, entities, max_distance=16.0, trident_attacker_id=trident_attacker_id,
                    include_drowned=confirm_trident_cover,
                )
                for shooter_id, shooter_pos in shooters:
                    if shooter_id is None:
                        continue
                    if shooter_id == primary_id:
                        pos = shooter_pos
                        check_blaze_cover = scan_blaze_cover
                    else:
                        other_shooters.append(shooter_pos)
                if confirmed_blaze_cover:
                    names = [
                        (entity.get("id"), str(entity.get("name") or "").removeprefix("minecraft:"))
                        for entity in entities if isinstance(entity, dict)
                    ]
                    check_blaze_cover = (
                        check_blaze_cover and (primary_id, "blaze") in names
                        and all(name != "ghast" for _, name in names)
                        and melee_clear_for_projectile_cover(entities)
                    )
                if (mixed_hurt or scan_surface_shooters) and not pos and other_shooters:
                    pos = other_shooters.pop(0)
                melee_clearance_confirmed = (
                    cover_requires_melee_clearance
                    and any(
                        shooter_id == trident_attacker_id if trident_attacker_id is not None else isinstance(shooter_id, int)
                        for shooter_id, _ in shooters
                    )
                    and melee_clear_for_projectile_cover(entities)
                )
            close_contact = scan_surface_shooters and (
                (nh_name == "creeper" and nh_distance <= 5.5)
                or (nh_name in PRE_HURT_MELEE_MOBS and nh_distance <= 3.5)
            )
            pos = pos or fallback_pos
            other_melee = [nh_pos] if (
                close_contact and pos and nh_pos and pos != nh_pos
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in nh_pos[:3])
            ) else []
            # A wall that blocks projectiles cannot protect us from a melee attacker already in reach.
            can_seek_cover = melee_clearance_confirmed if cover_requires_melee_clearance else not recent_melee
            if pos and (prefer_cover or check_blaze_cover) and not close_contact and can_seek_cover:
                cover_budget = 1.25 if check_blaze_cover else 2.25
                allow_stationary = allow_stationary_cover and not (
                    "nether" in dimension and self._low_health_burning_after_ranged_hit(status, now)
                )
                cover_bot = _GuardSession(
                    self.raw_bot, self.bot.checkpoint, deadline=min(deadline, time.monotonic() + cover_budget),
                )
                try:
                    cover = CombatEnv(cover_bot, geofence=self.geofence).seek_natural_cover(
                        pos, other_shooters=other_shooters, allow_stationary=allow_stationary,
                        **({"verify_live_shooters": True} if verify_live_shooters else {}),
                        **({"expected_life": (status.get("dimension"), status.get("last_death"))}
                           if confirmed_blaze_cover or confirm_trident_cover else {}),
                        **({"trident_attacker_id": trident_attacker_id} if confirm_trident_cover else {}),
                    )
                except _RecoveryBudgetExceeded:
                    self._stop_after_recovery_timeout()
                    cover = None
                if cover is not None and (cover_only or cover.get("ok") or cover.get("respawned")):
                    return cover
            if cover_only:
                return {"ok": False, "mode": "natural_cover", "reason": "no_safe_natural_cover"}
            critical_knockback = (
                dimension == "overworld" and unshielded
                and status.get("on_ground") is False and not status.get("on_fire")
                and isinstance(health, (int, float)) and 0 < health <= 6.0
                and (
                    (ranged and prefer_cover)
                    or (
                        not ranged and recent_melee and health <= 4.0
                        and nh_name in PRE_HURT_MELEE_MOBS and nh_distance <= 3.5
                    )
                )
            )
            try:
                with self.bot.limit_until(deadline):
                    return self.combat_env.flee(
                        pos, wait_for_ground=True,
                        **({"initial_landing_checks": 5} if critical_knockback else {}),
                        **({"other_shooters": other_shooters} if other_shooters else {}),
                        **({"other_melee": other_melee} if other_melee else {}),
                        **({"use_nearest_hostile": False} if resolve_melee and pos is None else {}),
                    )
            except _RecoveryBudgetExceeded:
                self._stop_after_recovery_timeout()
                return {"ok": False, "reason": "time_budget", "partial": True}
        result = self._run_recovery_macro(
            status,
            lambda skills: skills.defend_or_flee(
                attacker=attacker, counts=counts, health=health, urgent=True, geofence=self.geofence,
            ),
            budget_s=self.COMBAT_RECOVERY_BUDGET_S,
        )
        if result.get("reason") == "retreat_into_lava":
            recovery = self._action("escape_lava", self.combat_env.escape_lava)
            return {**result, "lava_recovery": recovery}
        return result

    def _recover_water_landing_after_retreat(
        self, before: dict[str, Any], landing: dict[str, Any], *,
        threat_pos: list[float] | None, threat_id: int | None, threat_name: str,
        has_shield: bool, avoid_close_threat: bool = False,
    ) -> dict[str, Any] | None:
        if (
            not landing.get("in_water") or landing.get("in_lava")
            or landing.get("ready") is False or landing.get("alive") is False
            or any(landing.get(key) != before.get(key) for key in ("last_death", "dimension"))
        ):
            return None
        if threat_pos is None and isinstance(threat_id, int):
            threat_pos = self._find_hostile_position(threat_id, threat_name, timeout_s=0.6)
        oxygen = landing.get("oxygen")
        shore = self._action(
            "escape_to_shore",
            lambda: self.combat_env.escape_deep_water_to_shore(
                threat_pos, keep_shield=has_shield or bool(landing.get("blocking")),
                shield_target_id=threat_id, surface_first=isinstance(oxygen, (int, float)) and oxygen <= 0,
                **({"avoid_close_threat": True} if avoid_close_threat else {}),
            ),
        )
        self._last_water_escape_at = time.monotonic()
        self._last_water_escape_failed = not (shore.get("ok") and (shore.get("result") or {}).get("in_water") is not True)
        return shore

    def _try_cornered_melee_strike(
        self, before: dict[str, Any], attacker: dict[str, Any] | None, counts: dict[str, int], *, hurt_source: str,
    ) -> dict[str, Any] | None:
        controller = self.combat_slot.controller
        if not isinstance(controller, SubcorticalInstinctController):
            return None
        try:
            landing = self.bot.call("status", method="GET", timeout_s=0.35)
        except (ApiError, httpx.HTTPError):
            return None
        name, distance, target_id, _ = self._extract_hostile_info(landing)
        attacker_id = (attacker or {}).get("id")
        hp = landing.get("health")
        nether_piglin = (
            name == "piglin" and hurt_source == "mob_attack"
            and str(landing.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
            and target_id == self._extract_hostile_info(before)[2]
        )
        max_health, reach = (CORNERED_PIGLIN_MAX_HEALTH, CORNERED_PIGLIN_REACH) if nether_piglin else (6.0, 1.25)
        if (
            (not nether_piglin and name not in LONE_COUNTERKILL_MOBS) or target_id is None or not 0 <= distance <= reach
            or (isinstance(attacker_id, int) and target_id != attacker_id)
            or not isinstance(hp, (int, float)) or not 0 < hp <= max_health
            or landing.get("ready") is not True or landing.get("alive") is not True or landing.get("on_ground") is not True
            or any(landing.get(key) for key in ("busy", "in_water", "in_lava", "on_fire"))
            or any(landing.get(key) != before.get(key) for key in ("last_death", "dimension"))
        ):
            return None
        try:
            scan = self.bot.call("get_entities", {"types": ["hostile"], "radius": 6}, timeout_s=0.35)
        except (ApiError, httpx.HTTPError):
            return None
        hostiles = scan.get("entities")
        if not isinstance(hostiles, list) or len(hostiles) != 1 or not isinstance(hostiles[0], dict):
            return None
        target = hostiles[0]
        if target.get("id") != target_id or target.get("blocked_by_wall") is not False or target.get("alive") is False:
            return None

        def strike() -> dict[str, Any]:
            try:
                with self.bot.limit_until(time.monotonic() + 1.25):
                    current = self.bot.call("status", method="GET", timeout_s=0.25)
                    if any(current.get(key) != landing.get(key) for key in ("last_death", "dimension")):
                        return {"ok": False, "reason": "life_changed"}
                    held = current.get("held")
                    held_name = str(held.get("name") or "").removeprefix("minecraft:") if isinstance(held, dict) else None
                    return controller.strike_cornered_hostile(
                        self.combat_env, target_id, counts, held_name, status=current,
                        visible_piglin=target if nether_piglin else None,
                    ) or {"ok": False, "reason": "opening_unconfirmed"}
            except _RecoveryBudgetExceeded:
                self._stop_after_recovery_timeout()
                return {"ok": False, "reason": "time_budget"}

        return self._action("cornered_melee_strike", strike)

    def _cornered_wither_skeleton_response(
        self, before: dict[str, Any], counts: dict[str, int], target_id: int,
    ) -> list[dict[str, Any]]:
        controller = self.combat_slot.controller
        weapon = best_melee_weapon_in_inv(counts)
        if not isinstance(controller, SubcorticalInstinctController) or weapon is None:
            return []

        def has_opening(current: dict[str, Any]) -> bool:
            name, distance, entity_id, _ = self._extract_hostile_info(current)
            health = current.get("health")
            effects = current.get("effects")
            offhand = current.get("offhand")
            nearest = current.get("nearest_hostile")
            return (
                self._same_life(before, current) and current.get("alive") is True and current.get("on_ground") is True
                and current.get("blocking") is True
                and isinstance(offhand, dict) and offhand.get("name") in {"shield", "minecraft:shield"}
                and not any(current.get(key) for key in ("busy", "in_water", "in_lava", "on_fire"))
                and isinstance(health, (int, float)) and 0 < health <= 6.0
                and name == "wither_skeleton" and entity_id == target_id and 0 < distance <= 1.75
                and isinstance(nearest, dict) and nearest.get("blocked_by_wall") is not True
                and isinstance(effects, list)
                and any(isinstance(effect, dict) and effect.get("name") in {"wither", "minecraft:wither"} for effect in effects)
            )

        try:
            landing = self.bot.call("status", method="GET", timeout_s=0.35)
            if not landing.get("pos") or not has_opening(landing):
                return []
            sighting = self.bot.call("get_entities", {"radius": 6, "types": ["hostile"]}, timeout_s=0.35)
        except (ApiError, httpx.HTTPError):
            return []
        entities = sighting.get("entities")
        if not isinstance(entities, list) or len(entities) != 1 or not isinstance(entities[0], dict):
            return []
        enemy = entities[0]
        if (
            enemy.get("id") != target_id or enemy.get("blocked_by_wall") is not False or enemy.get("alive") is False
            or self._extract_hostile_info({"nearest_hostile": enemy})[0] != "wither_skeleton"
            or not 0 < controller._entity_dist(landing["pos"], enemy) <= 1.75
        ):
            return []

        def has_weapon_in_hand(current: dict[str, Any]) -> bool:
            held = current.get("held")
            return isinstance(held, dict) and str(held.get("name") or "").removeprefix("minecraft:") == weapon

        def strike() -> dict[str, Any]:
            try:
                with self.bot.limit_until(time.monotonic() + 2.1):
                    current = self.bot.call("status", method="GET", timeout_s=0.25)
                    if not has_opening(current) or not self.combat_env._on_same_footing(landing, current):
                        return {"ok": False, "reason": "opening_unconfirmed"}
                    if not has_weapon_in_hand(current):
                        self.bot.call("equip", {"item": weapon, "slot": "hand"}, timeout_s=0.35)
                        current = self.bot.call("status", method="GET", timeout_s=0.25)
                    if not self.combat_env._on_same_footing(landing, current):
                        return {"ok": False, "reason": "opening_unconfirmed"}
                    if (
                        has_weapon_in_hand(current) and current.get("blocking") is False
                        and self._restore_shield_near_threat(landing, current, self.bot)
                    ):
                        current = self.bot.call("status", method="GET", timeout_s=0.25)
                    if (
                        not has_opening(current) or not self.combat_env._on_same_footing(landing, current)
                        or not has_weapon_in_hand(current)
                    ):
                        return {"ok": False, "reason": "opening_unconfirmed"}
                    return controller._attack_once_in_place(self.combat_env, target_id, weapon)
            except _RecoveryBudgetExceeded:
                self._stop_after_recovery_timeout()
                return {"ok": False, "reason": "time_budget"}

        steps = [self._action("cornered_melee_strike", strike)]
        try:
            after = self.bot.call("status", method="GET", timeout_s=0.35)
        except (ApiError, httpx.HTTPError):
            return steps
        if after.get("on_ground") is True and after.get("blocking") is False and after.get("nearest_hostile"):
            steps.append(self._action(
                "guard_shield_after_cornered_strike",
                lambda: {"ok": self._restore_shield_near_threat(landing, after, self.bot)},
            ))
        return steps

    @staticmethod
    def _bunker_site(status: dict[str, Any]) -> tuple[Any, Any, tuple[int, ...] | None]:
        pos = status.get("pos")
        tile = tuple(math.floor(coord) for coord in pos[:3]) if pos else None
        return status.get("dimension"), status.get("last_death"), tile

    @staticmethod
    def _bunker_life_unavailable(bunker: dict[str, Any] | None) -> bool:
        result = (bunker or {}).get("result") or {}
        error = ((bunker or {}).get("error") or {}).get("code")
        return bool(result.get("respawned") or result.get("life_unavailable") or error in {
            "dead", "not_ready", "not_connected", "wrong_server", "not_loaded",
            "chunk_not_loaded", "world_not_ready", "network_uncertain",
        })

    def _try_emergency_bunker(self, status: dict[str, Any], *, budget_s: float | None = None) -> dict[str, Any] | None:
        site = self._bunker_site(status)
        now = time.monotonic()
        if self._last_interrupted_bunker is not None:
            interrupted_site, interrupted_at = self._last_interrupted_bunker
            if (
                interrupted_site == site and now - interrupted_at < self.INTERRUPTED_BUNKER_RETRY_S
                and now - self._last_melee_hurt_at < self.INTERRUPTED_BUNKER_RETRY_S
            ):
                self.events.emit("guardian.bunker_retry_deferred", pos=status.get("pos"))
                return None
            self._last_interrupted_bunker = None
        if self._blocked_bunker is not None and self._blocked_bunker[0] == site:
            _, block_pos, obstacle = self._blocked_bunker
            try:
                live = self.bot.call("get_block", {"pos": block_pos}, timeout_s=0.35)
            except (ApiError, httpx.HTTPError):
                return None
            name = str(live.get("name") or live.get("block") or "").removeprefix("minecraft:")
            if name == obstacle:
                return None

        self._blocked_bunker = None

        def checked_bunker() -> dict[str, Any]:
            if budget_s is None:
                result = self._skills().dig_safety_bunker(urgent=True)
            else:
                result = self._run_recovery_macro(
                    status, lambda skills: skills.dig_safety_bunker(urgent=True), budget_s=budget_s,
                )
            if result.get("respawned") or result.get("error") == "dead":
                return {**result, "ok": False, "respawned": True}
            after = self.bot.call("status", method="GET", timeout_s=0.45)
            if after.get("alive") is False or any(after.get(key) != status.get(key) for key in ("last_death", "dimension")):
                return {**result, "ok": False, "respawned": True}
            if after.get("ready") is False:
                return {**result, "ok": False, "life_unavailable": True}
            return result

        action = self._action("emergency_bunker", checked_bunker)
        result = action.get("result") or {}
        if self._bunker_life_unavailable(action) or action.get("ok"):
            self._consecutive_flees = 0
            if action.get("ok") and result.get("inside_shaft") and result.get("roof_placed"):
                self._last_melee_hurt_at = float("-inf")
                self._nether_melee_eat_defer_until = float("-inf")
            return action
        reason = result.get("reason")
        tile = site[2]
        if (
            tile and reason == "dig_failed" and result.get("error") == "interrupted"
            and "hurt" in str(result.get("message") or "").lower() and result.get("dug") == []
            and result.get("failed_pos") == [tile[0], tile[1] - 1, tile[2]]
        ):
            self._last_interrupted_bunker = (site, time.monotonic())
        if reason == "unsafe_floor":
            block_pos, obstacle = result.get("floor_pos"), result.get("floor_block")
        else:
            block_pos, obstacle = result.get("block_pos"), result.get("block")
        if (
            tile and isinstance(obstacle, str)
            and isinstance(block_pos, list) and len(block_pos) == 3 and all(isinstance(coord, int) for coord in block_pos)
        ):
            unsafe_floor = reason == "unsafe_floor" and block_pos == [tile[0], tile[1] - 4, tile[2]]
            unsafe_side = (
                reason == "unsafe_open_side" and 1 <= tile[1] - block_pos[1] <= 3
                and abs(tile[0] - block_pos[0]) + abs(tile[2] - block_pos[2]) == 1
            )
            if unsafe_floor or unsafe_side:
                self._blocked_bunker = (site, block_pos, obstacle)
        return action

    def _inside_own_shaft(self) -> bool:
        """Only bunker-in-place when we are inside our own carved tunnel (never in open cave air)."""
        eng = getattr(self.skills, "mining_dfs", None) if self.skills is not None else None
        if eng is None or not getattr(eng, "nodes", None):
            return False
        try:
            return bool(eng.is_inside_shaft())
        except Exception:
            return False

    def _action(
        self,
        name: str,
        operation: Callable[[], dict[str, Any]],
        *,
        raise_on_http_error: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        self.events.emit("guardian.action.started", action=name)
        event_details = {}
        try:
            result = operation()
            outcome = {"action": name, "result": result, "ok": result.get("ok", True) if isinstance(result, dict) else True}
        except ApiError as error:
            outcome = {
                "action": name,
                "ok": False,
                "error": {"code": error.code, "message": error.message, **error.context},
            }
        except httpx.TimeoutException as error:
            if name in {"subcortical_combat", "boundary_subcortical_combat", "cornered_melee_strike"}:
                try:
                    endpoint = error.request.url.path.rsplit("/", 1)[-1]
                except RuntimeError:
                    endpoint = ""
                if endpoint in {
                    "attack", "status", "inventory", "get_entities", "get_block", "find_blocks",
                    "stop", "control", "posture", "look", "equip", "move_to", "dig", "place",
                }:
                    event_details["endpoint"] = endpoint
                if endpoint == "attack":
                    if isinstance(error, httpx.ReadTimeout):
                        self._last_attack_read_timeout_at = time.monotonic()
                    try:
                        attack_request = json.loads(error.request.content)
                    except (httpx.RequestNotRead, ValueError):
                        attack_request = None
                    target_id = attack_request.get("entity_id") if isinstance(attack_request, dict) else None
                    if type(target_id) is int:
                        event_details["target_id"] = target_id
            # Always attempt to cancel a lingering server action (e.g. `attack` / `move_to`) so the bot
            # does not remain stuck in `busy='attack'` after a client-side ReadTimeout.
            try:
                self.raw_bot.call("stop", {}, allow_reauth=False, timeout_s=1.5)
            except Exception:
                pass
            outcome = {
                "action": name,
                "ok": False,
                "error": {"code": "network_uncertain", "message": type(error).__name__, "timeout": True},
            }
        except httpx.HTTPError as error:
            # Never rewrap transport errors as RuntimeError: episode-level handling recovers from network_uncertain.
            _ = raise_on_http_error  # preserved parameter compatibility
            outcome = {
                "action": name,
                "ok": False,
                "error": {"code": "network_uncertain", "message": type(error).__name__},
            }
        self.stats["actions"] += 1
        self.events.emit("guardian.action.completed", duration=time.monotonic() - started, **outcome, **event_details)
        return outcome

    def _drain_events_pages(self, *, observe_combat_hurt: bool = True) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for _ in range(5):
            page = self.bot.call("events", method="GET", params={"since": self.cursor, "limit": 100})
            rows = [e for e in page.get("events", []) if int(e.get("seq", 0)) > self.cursor]
            rec_fn = getattr(self.bot.bot, "record_death_event", None)
            if callable(rec_fn):
                now_wall = time.time()
                now_mono = time.monotonic()
                for ev in rows:
                    if str(ev.get("type") or "").lower() != "death":
                        continue
                    timestamp_ms = ev.get("t")
                    death_key = ev.get("seq") if ev.get("seq") is not None else timestamp_ms
                    if isinstance(timestamp_ms, (int, float)) and 0 < timestamp_ms <= (now_wall + 5.0) * 1000:
                        age = max(0.0, now_wall - timestamp_ms / 1000.0)
                        if age <= 60.0:
                            rec_fn(death_key, mono_ts=now_mono - age)
                    elif observe_combat_hurt:
                        # Only live events may fall back to arrival time when the timestamp is absent.
                        rec_fn(death_key)
            fresh.extend(rows)
            self.cursor = max([self.cursor, int(page.get("last_seq", 0))] + [int(e.get("seq", 0)) for e in rows])
            if not page.get("has_more") or not rows:
                break
        if observe_combat_hurt and fresh:
            now = time.monotonic()
            for event in fresh:
                if event.get("type") in {"death", "death_loop", "disconnected", "spawn"}:
                    self._pre_hurt_suppress_block = None
                    self._pre_hurt_suppress_until = float("-inf")
                    self._pre_hurt_target_cooldown.clear()
                    self._ranged_combat_stance_until = float("-inf")
                    self._ranged_cover_active_until = float("-inf")
                    self._last_ranged_hurt_at = float("-inf")
                    self._last_attack_read_timeout_at = float("-inf")
                    self._last_trident_hurt_at = float("-inf")
                    self._last_melee_hurt_at = float("-inf")
                    self._nether_melee_eat_defer_until = float("-inf")
                    self._last_lava_contact_at = float("-inf")
                    self._last_fire_contact_at = float("-inf")
                    self._last_burn_hurt_at = float("-inf")
                    self._last_dig_cover_fail_at = float("-inf")
                    self._stationary_warnings.clear()
                    self._stationary_melee_dig_timeout = None
                    self._last_stalled_lava = None
                    self._water_hurt_streak = 0
                    self._last_water_escape_failed = False
                    self._blocked_bunker = None
                    self._last_interrupted_bunker = None
                    self._pending_spawn_hurts = None
                    self._blocked_flee_site = None
                    self._mining_melee_site = None
                    self._last_hot_floor_health = None
                elif event.get("type") == "hurt":
                    self._stationary_warnings.clear()
                    self._stationary_melee_dig_timeout = None
                    self._last_stalled_lava = None
                    source = str(event.get("source") or "").strip().lower()
                    if source in RANGED_HURT_SOURCES:
                        self._last_ranged_hurt_at = now
                        if source == "trident":
                            self._last_trident_hurt_at = now
                        self._last_melee_hurt_at = float("-inf")
                    elif source in MELEE_HURT_SOURCES:
                        self._last_melee_hurt_at = now
                        self._nether_melee_eat_defer_until = now + self.MELEE_EAT_DEFER_S
                    elif source in LAVA_HURT_SOURCES:
                        self._last_lava_contact_at = now
                    elif source in FIRE_HURT_SOURCES:
                        self._last_burn_hurt_at = now
                        if source == "in_fire":
                            self._last_fire_contact_at = now
        return fresh

    @staticmethod
    def _extract_hostile_info(status: dict[str, Any]) -> tuple[str, float, int | None, list[float] | None]:
        nh = status.get("nearest_hostile")
        if not isinstance(nh, dict) or not nh.get("name"):
            return "", 999.0, None, None
        name = str(nh.get("name") or "").removeprefix("minecraft:").strip().lower()
        try:
            dist = float(nh.get("dist", 999.0))
        except (TypeError, ValueError):
            dist = 999.0
        eid = int(nh["id"]) if isinstance(nh.get("id"), int) else None
        pos = list(nh["pos"]) if isinstance(nh.get("pos"), (list, tuple)) and len(nh["pos"]) >= 3 else None
        return name, dist, eid, pos

    def _find_visible_witch(self, entity_id: int | None = None) -> dict[str, Any] | None:
        try:
            sighting = self.bot.call("get_entities", {"radius": 12, "types": ["hostile"]}, timeout_s=0.6)
        except (ApiError, httpx.HTTPError):
            return None
        for entity in sighting.get("entities") or []:
            if not isinstance(entity, dict) or entity.get("blocked_by_wall") is not False or entity.get("alive") is False:
                continue
            name, _, observed_id, pos = self._extract_hostile_info({"nearest_hostile": entity})
            if (
                name == "witch" and observed_id is not None and (entity_id is None or observed_id == entity_id)
                and pos is not None and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            ):
                return entity
        return None

    def _find_hostile(
        self, entity_id: int | None, entity_name: str, *, session: _GuardSession | None = None,
        timeout_s: float | None = None, require_position: bool = False,
    ) -> dict[str, Any] | None:
        # Live status identifies the nearest hostile without always including coordinates.
        try:
            response = (session or self.bot).call("get_entities", {"radius": 16}, timeout_s=timeout_s)
        except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
            raise
        except Exception:
            return None
        if not isinstance(response, dict):
            return None
        for entity in response.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").removeprefix("minecraft:").strip().lower()
            if (entity_id is not None and entity.get("id") == entity_id) or (entity_id is None and name == entity_name):
                pos = entity.get("pos")
                if not require_position or (isinstance(pos, (list, tuple)) and len(pos) >= 3):
                    return entity
        return None

    def _find_hostile_position(
        self, entity_id: int | None, entity_name: str, *, session: _GuardSession | None = None, timeout_s: float | None = None,
    ) -> list[float] | None:
        hostile = self._find_hostile(entity_id, entity_name, session=session, timeout_s=timeout_s, require_position=True)
        return list(hostile["pos"]) if hostile is not None else None

    @staticmethod
    def _parse_inventory_summary(inv: dict[str, Any]) -> tuple[dict[str, int], bool, bool]:
        counts: dict[str, int] = {}
        for item in inv.get("items", []) or []:
            if isinstance(item, dict) and item.get("name"):
                nm = str(item["name"]).removeprefix("minecraft:")
                counts[nm] = counts.get(nm, 0) + int(item.get("count", 0))
        offhand = inv.get("offhand") or {}
        held = inv.get("held") or {}
        off_name = str(offhand.get("name") or "").removeprefix("minecraft:")
        held_name = str(held.get("name") or "").removeprefix("minecraft:")
        has_shield = counts.get("shield", 0) > 0 or off_name == "shield"
        has_melee_weapon = any(counts.get(w, 0) > 0 or held_name == w for w in MELEE_WEAPON_ORDER)
        return counts, has_shield, has_melee_weapon

    @staticmethod
    def _pre_hurt_target_key(entity_id: int | None, entity_name: str) -> str:
        return f"id:{entity_id}" if isinstance(entity_id, int) else f"name:{entity_name}"

    def _is_pre_hurt_debounced(self, entity_id: int | None, entity_name: str, *, now: float | None = None) -> bool:
        if not entity_name and not isinstance(entity_id, int):
            return False
        cur = time.monotonic() if now is None else now
        until = self._pre_hurt_target_cooldown.get(self._pre_hurt_target_key(entity_id, entity_name), float("-inf"))
        return cur < until

    def _mark_ranged_engagement(
        self,
        now: float | None = None,
        *,
        trigger: ThreatTrigger | None = None,
        attacker: dict[str, Any] | None = None,
        in_cover: bool = False,
    ) -> None:
        """Enter or extend ranged combat stance, optional cover window, and 30s shooter cooldown."""
        cur = time.monotonic() if now is None else now
        self._ranged_combat_stance_until = max(
            self._ranged_combat_stance_until,
            cur + self.RANGED_COMBAT_STANCE_S,
        )
        if in_cover:
            self._ranged_cover_active_until = max(
                self._ranged_cover_active_until,
                cur + self.RANGED_COMBAT_STANCE_S,
            )
        eid = (
            trigger.get("primary_entity_id")
            if isinstance(trigger, dict)
            else (attacker.get("id") if isinstance(attacker, dict) else None)
        )
        ename_raw = (
            trigger.get("primary_type")
            if isinstance(trigger, dict)
            else (attacker.get("name") if isinstance(attacker, dict) else "")
        )
        ename = str(ename_raw or "").removeprefix("minecraft:").strip().lower()
        if not ename and not isinstance(eid, int):
            return
        key = self._pre_hurt_target_key(eid if isinstance(eid, int) else None, ename)
        self._pre_hurt_target_cooldown[key] = max(
            self._pre_hurt_target_cooldown.get(key, float("-inf")),
            cur + self.PRE_HURT_RANGED_COOLDOWN_S,
        )

    def _mark_pre_hurt_cooldown(self, trigger: ThreatTrigger | None, now: float | None = None) -> None:
        if not isinstance(trigger, dict):
            return
        source = str(trigger.get("source") or "")
        ename = str(trigger.get("primary_type") or "").removeprefix("minecraft:").strip().lower()
        hurt_src = str(trigger.get("hurt_source") or "").strip().lower()
        if source == "pre_hurt_ranged" or ename in RANGED_MOBS or hurt_src in RANGED_HURT_SOURCES:
            self._mark_ranged_engagement(now, trigger=trigger)
            return
        if not source.startswith("pre_hurt_"):
            return
        eid = trigger.get("primary_entity_id")
        if not ename and not isinstance(eid, int):
            return
        cur = time.monotonic() if now is None else now
        cd = self.PRE_HURT_MELEE_COOLDOWN_S if source == "pre_hurt_melee" else self.PRE_HURT_TARGET_COOLDOWN_S
        key = self._pre_hurt_target_key(eid if isinstance(eid, int) else None, ename)
        self._pre_hurt_target_cooldown[key] = max(
            self._pre_hurt_target_cooldown.get(key, float("-inf")),
            cur + cd,
        )

    def _recover_recent_ranged_hit_for_eating(self, ranged_window_s: float) -> None:
        self._eat_history_checked = True
        # The actor can consume a final hurt event before the next episode's guard is created.
        cursor = self.cursor
        try:
            page = self.bot.call(
                "events", method="GET", params={"since": max(0, cursor - 32), "limit": 32},
                timeout_s=0.5, allow_reauth=False,
            )
        except (ApiError, httpx.HTTPError):
            return
        now_wall, now = time.time(), time.monotonic()
        for event in reversed(page.get("events") or []):
            if int(event.get("seq", 0)) > cursor:
                continue
            if event.get("type") in {"death", "death_loop", "disconnected", "spawn"}:
                break
            source = str(event.get("source") or "").strip().lower()
            if event.get("type") != "hurt" or source not in RANGED_HURT_SOURCES:
                continue
            timestamp_ms = event.get("t")
            if isinstance(timestamp_ms, (int, float)):
                age = now_wall - timestamp_ms / 1000.0
                if 0 <= age < ranged_window_s:
                    self._last_ranged_hurt_at = max(self._last_ranged_hurt_at, now - age)
                if source == "trident" and 0 <= age < self.TRIDENT_EAT_THREAT_S:
                    self._last_trident_hurt_at = max(self._last_trident_hurt_at, now - age)

    def _can_auto_eat_safely(
        self,
        status: dict[str, Any],
        *,
        min_dist: float = 5.5,
        ranged_min_dist: float = 16.0,
    ) -> bool:
        """Return False when mid-air, in fluid, or exposed within melee or ranged threat range."""
        if status.get("in_lava") or status.get("in_water") or status.get("on_ground") is False:
            return False
        now = time.monotonic()
        if status.get("on_fire") and now - self._last_fire_contact_at < 1.25:
            return False
        nh_name, nh_dist, _, _ = self._extract_hostile_info(status)
        if nh_name and nh_dist <= (WARDEN_EVADE_DISTANCE if nh_name == "warden" else min_dist):
            return False
        health = status.get("health")
        low_health = isinstance(health, (int, float)) and 0 < health <= LOW_HP_BUNKER_MAX
        if isinstance(health, (int, float)) and health <= 4.0:
            # In-flight projectiles can still reach a critical-health bot just beyond that range.
            ranged_min_dist = max(ranged_min_dist, 20.0)
        ranged_in_range = (nh_name in RANGED_MOBS or nh_name == "ghast") and nh_dist <= ranged_min_dist
        in_nether = str(status.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
        # Keep an active shield through the same Nether projectile window used while burning.
        ranged_shield_window = self.NETHER_BURN_RANGED_WINDOW_S if in_nether else self.RANGED_EAT_DEFER_S
        if not self._eat_history_checked and status.get("blocking") and status.get("ready") is True:
            self._recover_recent_ranged_hit_for_eating(ranged_shield_window)
        ranged_hit_age = now - self._last_ranged_hurt_at
        recent_ranged_hit = ranged_hit_age < self.RANGED_EAT_DEFER_S
        # A melee attack can time out while arrows still arrive from outside the nearest-hostile snapshot.
        exposed_after_attack_timeout = (
            low_health and recent_ranged_hit and now - self._last_attack_read_timeout_at < self.RANGED_EAT_DEFER_S
        )
        recent_melee_hit = now - self._last_melee_hurt_at < self.MELEE_EAT_DEFER_S
        # The melee deadline survives a mixed batch even when its last event was a projectile.
        recent_nether_ranged_only = (
            in_nether and ranged_hit_age < ranged_shield_window
            and self._last_ranged_hurt_at + self.MELEE_EAT_DEFER_S > self._nether_melee_eat_defer_until
        )
        protecting_against_recent_hit = status.get("blocking") and (
            recent_ranged_hit or recent_nether_ranged_only or recent_melee_hit
        )
        # A Nether shooter can keep firing while missing from the nearest-hostile snapshot.
        recent_nether_threat = in_nether and (
            now < self._nether_melee_eat_defer_until
            or (status.get("on_fire") and ranged_hit_age < self.NETHER_BURN_RANGED_WINDOW_S)
        )
        trident_age = now - self._last_trident_hurt_at
        trident_threat = trident_age < self.RANGED_EAT_DEFER_S
        if not trident_threat and trident_age < self.TRIDENT_EAT_THREAT_S and low_health:
            trident_threat = nh_name == "drowned" and nh_dist <= ranged_min_dist
            if not trident_threat and not ranged_in_range:
                try:
                    sighting = self.bot.call(
                        "get_entities", {"radius": ranged_min_dist, "types": ["hostile"]},
                        timeout_s=0.5, allow_reauth=False,
                    )
                except (ApiError, httpx.HTTPError):
                    trident_threat = True
                else:
                    for entity in sighting.get("entities") or []:
                        name, distance, _, _ = self._extract_hostile_info({"nearest_hostile": entity})
                        if name == "drowned" and distance <= ranged_min_dist:
                            trident_threat = True
                            break
        pos = status.get("pos")
        recent_failed_cover = (
            recent_ranged_hit and now - self._last_dig_cover_fail_at < 8.0
            and low_health
            and isinstance(pos, (list, tuple)) and len(pos) >= 3
            and (math.floor(pos[0]), math.floor(pos[1] + 0.05), math.floor(pos[2])) == self._last_dig_cover_block
        )
        if (
            (
                ranged_in_range or exposed_after_attack_timeout or protecting_against_recent_hit
                or recent_nether_threat or trident_threat or recent_failed_cover
            )
            and not self._inside_own_shaft()
        ):
            return False
        return True

    @staticmethod
    def _hot_floor_block(status: dict[str, Any]) -> tuple[str, int, int, int] | None:
        pos = status.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return None
        x, y, z = pos[:3]
        return str(status.get("dimension") or ""), math.floor(x), math.floor(y + 0.05), math.floor(z)

    def _needs_hot_floor_stance(self, status: dict[str, Any], now: float) -> bool:
        return status.get("on_ground") is True and (
            not status.get("sneaking") or self._hot_floor_damage_continued(status)
        ) and (
            now >= self._hot_floor_retry_at or self._hot_floor_block(status) != self._last_hot_floor_block
        )

    def _hot_floor_damage_continued(self, status: dict[str, Any]) -> bool:
        health = status.get("health")
        return (
            self._last_hot_floor_block is not None and self._hot_floor_block(status) == self._last_hot_floor_block
            and self._last_hot_floor_health is not None and isinstance(health, (int, float))
            and health < self._last_hot_floor_health
        )

    def _sneak_on_hot_floor(self, status: dict[str, Any]) -> dict[str, Any]:
        must_leave_tile = self._hot_floor_damage_continued(status)
        self._hot_floor_retry_at = time.monotonic() + self.HOT_FLOOR_RETRY_S
        self._last_hot_floor_block = self._hot_floor_block(status)
        health = status.get("health")
        self._last_hot_floor_health = float(health) if isinstance(health, (int, float)) else None
        posture = {"sneak": True, "shield": bool(status.get("blocking")), "jump": False}
        result = self.bot.call("posture", posture, timeout_s=1.0)
        if result.get("sneaking") is False:
            # The saved posture can still say sneak=true after the backend clears the actual key.
            self.bot.call("posture", {**posture, "sneak": False}, timeout_s=1.0)
            result = self.bot.call("posture", posture, timeout_s=1.0)
        ok = result.get("sneaking") is not False
        if ok and not must_leave_tile:
            self._hot_floor_retry_at = float("-inf")
            return {**result, "ok": True}

        step = self._step_off_hot_floor(status)
        self._hot_floor_retry_at = float("-inf") if step.get("ok") else time.monotonic() + self.HOT_FLOOR_RETRY_S
        return {**result, **step}

    def _step_off_hot_floor(self, status: dict[str, Any]) -> dict[str, Any]:
        step_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 3.0)
        step_env = CombatEnv(step_bot, geofence=self.geofence)
        try:
            return step_env.step_off_hot_floor(status)
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return {"ok": False, "reason": "time_budget"}

    def _protect_action_on_hot_floor(self, *, is_attack: bool) -> str | None:
        if self._last_hot_floor_block is None:
            return None
        try:
            status = self.bot.status()
        except (ApiError, httpx.HTTPError):
            return None
        if (
            status.get("ready") is False or status.get("alive") is False or status.get("on_ground") is not True
            or status.get("in_water") or status.get("in_lava")
            or self._hot_floor_block(status) != self._last_hot_floor_block
        ):
            return None
        health = status.get("health")
        if not is_attack and not (isinstance(health, (int, float)) and 0 < health <= 4.0):
            return None
        self.combat_env.invalidate_block_cache()
        if not self.combat_env.is_on_hot_floor(status["pos"]):
            return None
        action = "hot_floor_exit_before_attack" if is_attack else "hot_floor_exit_before_move"
        message = (
            "脚下是岩浆块，请确认安全落脚后重新选择攻击目标。"
            if is_attack else "脚下是岩浆块，请从有支撑的安全落脚点继续移动。"
        )
        step = self._action(action, lambda: self._step_off_hot_floor(status))
        return json.dumps({
            "ok": False,
            "requires_replan": True,
            "error": {"code": "hot_floor", "message": message},
            "recovery": step,
        }, ensure_ascii=False)

    def _try_eat_food(self, food_name: str, eat_st: dict[str, Any], *, action_name: str = "eat") -> dict[str, Any] | None:
        """Eat via `use_item`; treat transient `not_ready`/`wrong_server` as deferred retry, not failure."""
        if eat_st.get("blocking"):
            try:
                self.bot.call("posture", {"shield": False})
            except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                raise
            except ApiError as error:
                if error.code in {"not_ready", "wrong_server"}:
                    self.retry_after_ready = True
                    self.events.emit("guardian.eat_deferred", reason=error.code, retry_after_ready=True)
                    return None
            except Exception:
                pass
        started = time.monotonic()
        self.events.emit("guardian.action.started", action=action_name)
        try:
            result = self.bot.call("use_item", {"item": food_name})
            outcome: dict[str, Any] = {
                "action": action_name,
                "result": result,
                "ok": result.get("ok", True) if isinstance(result, dict) else True,
            }
        except ApiError as error:
            if error.code in {"not_ready", "wrong_server"}:
                self.retry_after_ready = True
                self.events.emit("guardian.eat_deferred", reason=error.code, retry_after_ready=True)
                return None
            outcome = {
                "action": action_name,
                "ok": False,
                "error": {"code": error.code, "message": error.message, **error.context},
            }
        except httpx.TimeoutException as error:
            outcome = {
                "action": action_name,
                "ok": False,
                "error": {"code": "network_uncertain", "message": type(error).__name__, "timeout": True},
            }
        except httpx.HTTPError as error:
            outcome = {
                "action": action_name,
                "ok": False,
                "error": {"code": "network_uncertain", "message": type(error).__name__},
            }
        result = outcome.get("result")
        if eat_st.get("blocking") and isinstance(result, dict) and result.get("interrupted_by") == "hurt":
            recovery_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 1.2)
            try:
                after = recovery_bot.call("status", None, method="GET", timeout_s=0.5, allow_reauth=False)
                if self._restore_shield_near_threat(eat_st, after, recovery_bot):
                    outcome["result"] = {**result, "shield_restored": True}
            except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
                pass
        self.stats["actions"] += 1
        self.events.emit("guardian.action.completed", duration=time.monotonic() - started, **outcome)
        return outcome

    def _detect_pre_hurt_trigger(self, status: dict[str, Any]) -> ThreatTrigger | None:
        nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)
        if not nh_name:
            return None
        now = time.monotonic()
        is_creeper = nh_name == "creeper" and nh_dist <= 4.5
        is_melee = nh_name in PRE_HURT_MELEE_MOBS and nh_dist <= 3.5
        is_ranged = nh_name in RANGED_MOBS and nh_dist <= 13.5
        if not (is_creeper or is_melee or is_ranged):
            return None
        if not is_creeper and status["nearest_hostile"].get("blocked_by_wall") is True:
            return None
        if (
            is_ranged
            and not is_creeper
            and not is_melee
            and (
                now < self._ranged_combat_stance_until
                or now < self._ranged_cover_active_until
                or bool(status.get("blocking"))
            )
        ):
            return None
        if self._is_pre_hurt_debounced(nh_id, nh_name, now=now):
            return None

        pos = status.get("pos")
        cur_blk: tuple[int, int, int] | None = None
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            cur_blk = (int(math.floor(float(pos[0]))), int(math.floor(float(pos[1]))), int(math.floor(float(pos[2]))))
            if (
                self._pre_hurt_suppress_block == cur_blk
                and now < self._pre_hurt_suppress_until
                and not (is_creeper and nh_dist <= 2.3)
            ):
                return None

        if nh_pos is None:
            # A name-only match may refer to another mob. Only use its position as before.
            if nh_id is None or is_creeper:
                nh_pos = self._find_hostile_position(nh_id, nh_name, timeout_s=0.6)
            else:
                hostile = self._find_hostile(nh_id, nh_name, timeout_s=0.6)
                if hostile is not None:
                    if hostile.get("blocked_by_wall") is True:
                        return None
                    candidate_pos = hostile.get("pos")
                    if isinstance(candidate_pos, (list, tuple)) and len(candidate_pos) >= 3:
                        nh_pos = list(candidate_pos)

        # Never fire `pre_hurt_*` through solid bunker walls! Real damage still triggers `guard_poll_hurt` unconditionally.
        if isinstance(pos, (list, tuple)) and len(pos) >= 3 and isinstance(nh_pos, (list, tuple)) and len(nh_pos) >= 3:
            self.combat_env.invalidate_block_cache()
            try:
                los = self.combat_env.has_los(pos, nh_pos)
            except (ApiError, httpx.HTTPError):
                return None
            if not los.get("clear"):
                return None
        elif is_ranged:
            return None

        if is_creeper:
            return {
                "source": "pre_hurt_creeper",
                "primary_entity_id": nh_id,
                "primary_type": "creeper",
                "primary_pos": nh_pos,
                "distance": nh_dist,
            }
        if is_melee:
            return {
                "source": "pre_hurt_melee",
                "primary_entity_id": nh_id,
                "primary_type": nh_name,
                "primary_pos": nh_pos,
                "distance": nh_dist,
            }
        return {
            "source": "pre_hurt_ranged",
            "primary_entity_id": nh_id,
            "primary_type": nh_name,
            "primary_pos": nh_pos,
            "distance": nh_dist,
        }

    def poll(self) -> dict[str, Any] | None:
        if self._in_reflex:
            return None
        now = time.monotonic()
        if now - self.last_poll < self.interval_s:
            return None
        self.last_poll = now
        self.stats["polls"] += 1
        previous_ranged_hurt_at = self._last_ranged_hurt_at
        try:
            status = self.bot.status()
            fresh = self._drain_events_pages()
            self.failures = 0
        except (ApiError, httpx.HTTPError) as error:
            self._stationary_melee_dig_timeout = None
            if isinstance(error, ApiError) and error.code in {"not_ready", "wrong_server"}:
                self.events.emit("guardian.observation_not_ready", code=error.code)
                return None
            self.failures += 1
            if self.failures == 1:
                self.events.emit("guardian.observation_failed", error=type(error).__name__, code=getattr(error, "code", None))
            if self.failures >= 3:
                self.disconnected = True
            return None

        hurts = [e for e in fresh if e.get("type") == "hurt"]
        deaths = [e for e in fresh if e.get("type") in {"death", "death_loop", "disconnected", "spawn"}]
        life_transition_pending = bool(deaths)
        spawn_warning = None
        if deaths and deaths[-1]["type"] == "spawn":
            # The server can deliver the last death and the new life's first hits together.
            spawn_seq = int(deaths[-1]["seq"])
            post_spawn_hurts = [e for e in hurts if int(e["seq"]) > spawn_seq]
            spawn_name, spawn_dist, _, _ = self._extract_hostile_info(status)
            spawn_health = status.get("health")
            exposed_spawn = (
                status.get("on_ground") is True and spawn_name in PRE_HURT_MELEE_MOBS and spawn_dist <= 3.5
                and isinstance(spawn_health, (int, float)) and 0 < spawn_health <= 12.0
            )
            if (post_spawn_hurts or exposed_spawn) and status.get("ready") and status.get("alive") is not False:
                try:
                    current = self.bot.call("status", method="GET", timeout_s=0.6)
                except (ApiError, httpx.HTTPError):
                    current = {}
                if (
                    current.get("ready") and current.get("alive") is not False
                    and current.get("last_death") == status.get("last_death")
                    and current.get("dimension") == status.get("dimension")
                ):
                    if (
                        not post_spawn_hurts and current.get("on_ground") is True and current.get("pos")
                        and not any(current.get(key) for key in ("in_water", "in_lava", "submerged"))
                    ):
                        warning = self._detect_pre_hurt_trigger(current)
                        if warning is not None and warning.get("primary_pos") is not None:
                            spawn_warning = warning
                    if post_spawn_hurts or spawn_warning is not None:
                        status = current
                        hurts = post_spawn_hurts
                        life_transition_pending = False
            if life_transition_pending and status.get("alive") is not False:
                combat_hurts = [
                    h for h in post_spawn_hurts
                    if str(h.get("source") or "").strip().lower() in MELEE_HURT_SOURCES | RANGED_HURT_SOURCES
                ]
                if combat_hurts:
                    life = (status.get("dimension"), status.get("last_death"))
                    self._pending_spawn_hurts = (life, now + self.SPAWN_HURT_RESUME_S, combat_hurts)

        falling_over_lava = False
        if not life_transition_pending and self._nether_fast_fall(status):
            landing, falling_over_lava = self._observe_nether_fall(status)
            if not self._same_life(status, landing):
                return self._incident(landing, ["death_or_connection_changed"], [], fresh)
            status = landing

        health = status.get("health")
        ready = bool(status.get("ready", status.get("alive", True)))
        resumed_spawn_hurt = False
        if self._pending_spawn_hurts is not None and not life_transition_pending:
            life, deadline, pending_hurts = self._pending_spawn_hurts
            if now >= deadline or life != (status.get("dimension"), status.get("last_death")) or status.get("alive") is False:
                self._pending_spawn_hurts = None
            elif ready:
                self._pending_spawn_hurts = None
                if isinstance(health, (int, float)) and 0 < health < 20.0:
                    hurts = [*pending_hurts, *hurts]
                    resumed_spawn_hurt = True
                    self.events.emit("guardian.spawn_hurt_resumed", event_seqs=[h["seq"] for h in pending_hurts])
        dropped = isinstance(health, (int, float)) and self.previous_health is not None and health < self.previous_health - 0.2
        disconnected = self.previous_ready is True and not ready
        self.previous_ready = ready
        self.previous_health = float(health) if isinstance(health, (int, float)) else None
        other_hurts = [e for e in hurts if str(e.get("source") or "").strip().lower() != "hot_floor"]
        in_wall_hurt = any(str(h.get("source") or "") in IN_WALL_HURT_SOURCES for h in hurts)
        in_water = bool(status.get("in_water"))
        raw_in_lava = bool(status.get("in_lava"))
        if raw_in_lava and ready and status.get("alive") is not False and not life_transition_pending:
            self._last_lava_contact_at = now
        hurt_by_lava = any(str(h.get("source") or "").strip().lower() in LAVA_HURT_SOURCES for h in hurts)
        in_lava = raw_in_lava or falling_over_lava or (
            hurt_by_lava and not life_transition_pending and not disconnected and ready and status.get("alive") is not False
        )
        if (
            not in_lava and not in_water and not life_transition_pending and ready and status.get("alive") is not False
            and status.get("on_ground") is False and "nether" in str(status.get("dimension") or "").lower()
            and status.get("pos")
            and any(
                source in BLAST_HURT_SOURCES
                or (source in MELEE_HURT_SOURCES and isinstance(health, (int, float)) and health >= 11.0)
                for source in (str(h.get("source") or "").strip().lower() for h in hurts)
            )
        ):
            probe_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 0.5)
            try:
                in_lava = CombatEnv(probe_bot)._is_airborne_above_lava(status, start_pos=status["pos"])
            except _RecoveryBudgetExceeded:
                pass
        if not in_lava and status.get("on_ground") is True:
            self._last_stalled_lava = None
        hot_floor_only = bool(hurts) and not other_hurts and not in_water and not in_lava
        underwater_hot_floor = in_water and len(other_hurts) < len(hurts)
        needs_hot_floor_guard = (
            len(other_hurts) < len(hurts) and not in_water and not in_lava
            and self._needs_hot_floor_stance(status, now)
        )
        oxygen = status.get("oxygen")
        low_oxygen = water_oxygen_below(status, 16)
        critical_drowning = water_oxygen_below(status, 10)
        drowning = in_water and (
            (isinstance(oxygen, (int, float)) and oxygen <= 0)
            or any(str(h.get("source") or "").strip().lower() == "drown" for h in hurts)
        )
        no_water_active = now < self._no_water_until
        recent_water_escape_failed = (
            self._last_water_escape_failed and now - self._last_water_escape_at < self.WATER_ESCAPE_RETRY_WINDOW_S
        )
        water_retry_gap = 8.0 if self._last_water_escape_failed else 1.5
        no_water_reentry = (
            in_water
            and no_water_active
            and (now - self._last_water_escape_at >= water_retry_gap)
        )
        nearby_water_threat = self._has_close_water_threat(status)
        needs_float = (
            in_water
            and (
                drowning
                or (nearby_water_threat and now - self._last_water_escape_at >= water_retry_gap)
                or (
                    not status.get("jumping")
                    and (bool(status.get("submerged")) or low_oxygen)
                    and (critical_drowning or (now - self._last_water_escape_at >= 20.0))
                )
            )
        )
        if (
            not in_water
            and not in_lava
            and not status.get("submerged")
            and status.get("on_ground") is True
            and not deaths
            and not disconnected
            and ready
            and status.get("alive") is not False
        ):
            self.combat_env.remember_dry_bank(status)
            self._water_hurt_streak = 0
            self._last_water_escape_failed = False
        food = status.get("food")
        needs_food = isinstance(food, (int, float)) and (food <= 16 or (isinstance(health, (int, float)) and health < 18 and food < 20))
        pending_ready_eat = (
            self.retry_after_ready
            and ready
            and not deaths
            and not disconnected
            and status.get("alive") is not False
            and not in_lava
            and not in_water
            and isinstance(food, (int, float))
            and food < 20
        )
        if (
            self.retry_after_ready
            and ready
            and not deaths
            and not disconnected
            and status.get("alive") is not False
            and (not isinstance(food, (int, float)) or food >= 20)
        ):
            self.retry_after_ready = False
        nh_name0, nh_dist0, nh_id0, _ = self._extract_hostile_info(status)
        sonic_hurt = next((hit for hit in reversed(other_hurts) if hit.get("source") == "sonic_boom"), None)
        exposed_in_water = (
            needs_float and now < self._ranged_combat_stance_until
            and (nh_name0 in RANGED_MOBS or nh_name0 == "ghast") and nh_dist0 <= 16.0
        )
        should_eat = (
            (needs_food or pending_ready_eat) and not (hot_floor_only or needs_hot_floor_guard)
            and spawn_warning is None and sonic_hurt is None
            and self._can_auto_eat_safely(status, min_dist=5.5)
        )
        hurt = bool(hurts) or (dropped and spawn_warning is None)
        fall_only = self._unattributed_fall_only(hurts)
        contact_fire = any(str(h.get("source") or "").strip().lower() == "in_fire" for h in hurts)
        recent_lava = now - self._last_lava_contact_at < 15.0
        fire_hurt = any(str(h.get("source") or "").strip().lower() in FIRE_HURT_SOURCES for h in hurts)
        continuing_burn = recent_lava and status.get("on_fire") and (
            fire_hurt or (nh_dist0 > 8.0 and now - self._last_burn_hurt_at < 5.0)
        )
        environmental_burning = (
            bool(hurts) and ((fire_hurt and (contact_fire or recent_lava)) or continuing_burn)
            and all(
                str(h.get("source") or "").strip().lower() in FIRE_HURT_SOURCES
                or (
                    continuing_burn and not str(h.get("source") or "").strip()
                    and not (h.get("source_entity") or h.get("likely_attacker"))
                )
                for h in hurts
            )
        )
        # A typed magic tick can arrive without an attacker. Corroborate the
        # current witch before routing the hit away from environmental damage.
        magic_witch = None
        if (
            ready and status.get("alive") is not False and not (in_water or in_lava or life_transition_pending)
            and nh_name0 == "witch" and nh_id0 is not None and 0 <= nh_dist0 <= 12.0
            and hurts and all(
                str(hit.get("source") or "").strip().lower() in {"magic", "poison"}
                and not hit.get("source_entity") and not hit.get("likely_attacker")
                for hit in hurts
            )
        ):
            magic_witch = self._find_visible_witch(nh_id0)
        non_combat_only_hurt = (
            hurt
            and bool(hurts)
            and not in_lava
            and not in_water
            and not life_transition_pending
            and magic_witch is None
            and (
                environmental_burning or fall_only or (
                    (not nh_name0 or nh_dist0 > 8.0)
                    and all(
                        str(h.get("source") or "").strip().lower() in NON_COMBAT_HURT_SOURCES
                        and not (h.get("source_entity") or h.get("likely_attacker"))
                        for h in hurts
                    )
                )
            )
        )
        fire_hurt_only = non_combat_only_hurt and (fire_hurt or environmental_burning)
        hp_cur = float(health) if isinstance(health, (int, float)) else 20.0

        pre_trigger = spawn_warning or (
            None
            if (
                (hurt and not (fall_only and status.get("on_ground") is True))
                or deaths or disconnected or not ready or in_water or in_lava
            )
            else self._detect_pre_hurt_trigger(status)
        )
        if self._stationary_melee_dig_timeout is not None:
            retry_at, anchor, target_pos, scan_at = self._stationary_melee_dig_timeout
            if hurt or now >= retry_at or not self._same_stationary_melee_timeout_state(anchor, status):
                self._stationary_melee_dig_timeout = None
            elif pre_trigger is not None and pre_trigger.get("source") == "pre_hurt_melee" and spawn_warning is None:
                if not self._same_observed_melee_position(target_pos, pre_trigger.get("primary_pos")):
                    self._stationary_melee_dig_timeout = None
                elif scan_at is None or now >= scan_at:
                    target_id = pre_trigger.get("primary_entity_id")
                    if self._only_stationary_melee_threat(target_id, target_pos):
                        if scan_at is None:
                            self.events.emit(
                                "guardian.melee_timeout_deferred", target_id=target_id, target_pos=target_pos,
                                retry_in_s=round(retry_at - now, 3),
                            )
                        self._stationary_melee_dig_timeout = (
                            retry_at, anchor, target_pos, now + self.MELEE_DIG_TIMEOUT_RESCAN_S,
                        )
                    else:
                        self._stationary_melee_dig_timeout = None
                if self._stationary_melee_dig_timeout is not None:
                    pre_trigger = None
        effective_combat_hurt = hurt and not (non_combat_only_hurt or hot_floor_only)
        ranged_hurt_only = (
            effective_combat_hurt
            and bool(hurts)
            and all(
                str(h.get("source") or "").strip().lower() in RANGED_HURT_SOURCES
                for h in hurts
            )
            and nh_name0 != "creeper"
            and nh_dist0 > 3.5
        )
        sustained_shield_damage = (
            ranged_hurt_only and status.get("blocking") and status.get("on_ground") is True
            and not in_water and not in_lava and not status.get("on_fire") and 4.0 < hp_cur <= 16.0
            and str(status.get("dimension") or "").removeprefix("minecraft:") == "overworld"
            and len(hurts) >= 2 and all(str(h.get("source") or "").strip().lower() == "arrow" for h in hurts)
        )
        already_in_ranged_guard = (
            bool(status.get("blocking"))
            or (now < self._ranged_cover_active_until)
            or (now < self._ranged_combat_stance_until)
        )
        urgent_melee_hurt = (
            effective_combat_hurt and 4.0 < hp_cur <= 8.0 and not status.get("blocking") and bool(other_hurts)
            and all(str(h.get("source") or "").strip().lower() in MELEE_HURT_SOURCES for h in other_hurts)
            and (
                len(other_hurts) >= 2
                or (
                    nh_name0 in PRE_HURT_MELEE_MOBS and 0 <= nh_dist0 <= 3.5
                    and status["nearest_hostile"].get("blocked_by_wall") is not True
                )
            )
        )
        if effective_combat_hurt and not ranged_hurt_only:
            self._pre_hurt_suppress_until = float("-inf")
        critical_override = bool(
            deaths
            or in_lava
            or needs_hot_floor_guard
            or drowning
            or sustained_shield_damage
            or urgent_melee_hurt
            or resumed_spawn_hurt
            or (effective_combat_hurt and in_water)
            or (effective_combat_hurt and other_hurts and hp_cur <= 4.0)
            or no_water_reentry
            or (pre_trigger is not None and pre_trigger.get("source") == "pre_hurt_creeper")
            or in_wall_hurt
        )
        critical_non_combat = non_combat_only_hurt and not hot_floor_only and hp_cur <= 6.0 and (now - self._last_non_combat_alert_at >= 10.0)
        can_try_fire_douse = (
            fire_hurt_only
            and not status.get("busy")
            and "nether" not in str(status.get("dimension") or "").lower()
            and (now - self._last_fire_douse_at >= 12.0)
        )
        fire_pos = status.get("pos")
        can_clear_fire = (
            environmental_burning and contact_fire and not in_water and not in_lava and not status.get("busy")
            and status.get("on_ground") is True and isinstance(fire_pos, (list, tuple)) and len(fire_pos) >= 3
            and now - self._last_fire_clear_at >= 2.0
        )

        if not (
            effective_combat_hurt
            or needs_hot_floor_guard
            or critical_non_combat
            or can_try_fire_douse
            or can_clear_fire
            or deaths
            or disconnected
            or needs_float
            or no_water_reentry
            or should_eat
            or in_lava
            or pre_trigger is not None
        ):
            self._consecutive_flees = 0
            self._shield_only_streak = 0
            return None
        if now - self.last_reaction < 3.0 and not (critical_override or pending_ready_eat or can_clear_fire):
            return None

        actions: list[dict[str, Any]] = []
        reasons: list[str] = []
        if effective_combat_hurt or critical_non_combat:
            reasons.append("fresh_hurt")
        if pre_trigger is not None:
            reasons.append(str(pre_trigger["source"]))
        if in_lava:
            reasons.append("in_lava")
        if deaths or disconnected:
            reasons.append("death_or_connection_changed")
        if needs_float:
            reasons.append("water_float")
        if no_water_reentry:
            reasons.append("water_avoid_cooldown")
        if needs_hot_floor_guard:
            reasons.append("hot_floor")

        if deaths or disconnected or not ready or status.get("alive") is False:
            self._consecutive_flees = 0
            self._water_hurt_streak = 0
        waiting_for_life = life_transition_pending or disconnected or not ready or status.get("alive") is False
        if (waiting_for_life or spawn_warning is not None) and needs_food and not self.retry_after_ready:
            self.retry_after_ready = True
            self.events.emit("guardian.eat_deferred", reason="deaths_or_not_ready", retry_after_ready=True)
        if waiting_for_life:
            if not (deaths or disconnected):
                return None
            if not disconnected and status.get("busy"):
                try:
                    self.bot.call("stop")
                except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                    raise
                except Exception:
                    pass
            return self._incident(status, reasons, actions, fresh)

        if status.get("busy"):
            if in_lava or needs_float or no_water_reentry or hurt or (pre_trigger and pre_trigger.get("source") == "pre_hurt_creeper"):
                try:
                    self.bot.call("stop")
                except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                    raise
                except Exception:
                    pass
            else:
                return None

        self._in_reflex = True
        try:
            if needs_hot_floor_guard:
                sneak = self._action("hot_floor_sneak", lambda: self._sneak_on_hot_floor(status))
                actions.append(sneak)
                if sneak.get("ok") and (sneak.get("result") or {}).get("sneaking") is not False:
                    status = {**status, "sneaking": True}

            escape = None
            if in_lava:
                escape = self._action(
                    "escape_lava", lambda: self.combat_env.escape_lava(force_rim_escape=hurt_by_lava and not raw_in_lava),
                )
                actions.append(escape)

            try:
                needs_inventory = (
                    (hurt and not hot_floor_only) or should_eat or in_lava or no_water_reentry
                    or exposed_in_water or (needs_float and nearby_water_threat) or pre_trigger is not None
                )
                inv = self.bot.inventory() if needs_inventory else {}
            except (ApiError, httpx.HTTPError):
                inv = {}
            counts, has_shield, has_melee_weapon = self._parse_inventory_summary(inv)
            if (
                not in_lava and isinstance(inv, dict)
                and (isinstance(inv.get("armor"), dict) or "offhand" in inv)
                and (now - self._last_gear_check_at >= 12.0)
            ):
                auto_equip_survival_gear(self.bot, inv, allow_armor=True, allow_shield=False)
                self._last_gear_check_at = now

            if (
                escape and escape.get("ok") and not counts.get("water_bucket")
                and "nether" not in str(status.get("dimension") or "").lower()
            ):
                douse = self._douse_after_lava(status)
                if douse is not None:
                    actions.append(douse)

            def _already(name: str) -> bool:
                return any(a.get("action") == name for a in actions)

            nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)

            if (needs_float or no_water_reentry) and not in_lava and not hurt:
                if (no_water_reentry or exposed_in_water or nearby_water_threat) and nh_name and has_shield:
                    self._equip_and_raise_shield_for_water(
                        status,
                        actions,
                        inv=inv,
                        attacker=status.get("nearest_hostile") if isinstance(status.get("nearest_hostile"), dict) else None,
                    )
                submerged = bool(status.get("submerged"))
                continuing_water_escape = recent_water_escape_failed and (submerged or status.get("on_ground") is False)
                if (
                    no_water_reentry or exposed_in_water or nearby_water_threat
                    or drowning or self._swimming_at_low_health(status)
                    or continuing_water_escape
                ):
                    if (exposed_in_water or nearby_water_threat) and nh_pos is None:
                        nh_pos = self._find_hostile_position(nh_id, nh_name, timeout_s=0.6)
                    shore_act = self._action(
                        "escape_to_shore",
                        lambda: self.combat_env.escape_deep_water_to_shore(
                            nh_pos,
                            keep_shield=has_shield or bool(status.get("blocking")),
                            shield_target_id=nh_id,
                            **({"surface_first": True} if drowning or (continuing_water_escape and submerged) else {}),
                            **({"avoid_close_threat": True} if nearby_water_threat and nh_pos else {}),
                        ),
                    )
                    actions.append(shore_act)
                    shore_res = shore_act.get("result") if isinstance(shore_act, dict) and isinstance(shore_act.get("result"), dict) else {}
                    self._last_water_escape_failed = not bool(shore_act.get("ok") and shore_res.get("in_water") is not True)
                else:
                    esc_f = self._action("escape_water", lambda: self._escape_water(status))
                    actions.append(esc_f)
                    esc_f_res = esc_f.get("result") if isinstance(esc_f.get("result"), dict) else {}
                    esc_f_ok = bool(esc_f.get("ok") and esc_f_res.get("in_water") is not True)
                    esc_f_err = esc_f["error"].get("code") if isinstance(esc_f.get("error"), dict) else None
                    if (
                        not esc_f_ok and not esc_f_res.get("respawned") and esc_f_res.get("mode") != "lava_escape"
                        and esc_f_err not in {"not_connected", "not_ready", "wrong_server", "network_uncertain"}
                    ):
                        shore_f = self._action(
                            "escape_to_shore",
                            lambda: self.combat_env.escape_deep_water_to_shore(
                                nh_pos, keep_shield=has_shield, shield_target_id=nh_id,
                            ),
                        )
                        actions.append(shore_f)
                        shore_f_res = shore_f.get("result") if isinstance(shore_f, dict) and isinstance(shore_f.get("result"), dict) else {}
                        self._last_water_escape_failed = not bool(shore_f.get("ok") and shore_f_res.get("in_water") is not True)
                    else:
                        self._last_water_escape_failed = bool(
                            esc_f_err == "network_uncertain" and esc_f["error"].get("timeout") is True
                        )
                self._last_water_escape_at = time.monotonic()

            if pre_trigger is not None and not in_lava and not in_water:
                if pre_trigger.get("source") == "pre_hurt_melee":
                    self._last_pre_hurt_melee_at = now
                self._mark_pre_hurt_cooldown(pre_trigger, now)
                sc_out = self._action(
                    "subcortical_combat",
                    lambda: self._run_subcortical_combat(pre_trigger),
                )
                self._mark_pre_hurt_cooldown(pre_trigger, time.monotonic())
                actions.append(sc_out)
                sc_res = sc_out.get("result") if isinstance(sc_out, dict) else None
                in_sc_cover = isinstance(sc_res, dict) and sc_res.get("mode") in {"cover_pocket", "bunker_sealed", "sealed_shaft"}
                if pre_trigger.get("source") == "pre_hurt_ranged" or str(pre_trigger.get("primary_type") or "") in RANGED_MOBS:
                    self._mark_ranged_engagement(time.monotonic(), trigger=pre_trigger, in_cover=in_sc_cover)
                if isinstance(sc_res, dict):
                    st_after_sc = sc_res.get("status_after") if isinstance(sc_res.get("status_after"), dict) else status
                    pos_sc = st_after_sc.get("pos") or status.get("pos")
                    pos_before_sc = status.get("pos")
                    if isinstance(pos_sc, (list, tuple)) and len(pos_sc) >= 3:
                        blk_sc = (int(math.floor(float(pos_sc[0]))), int(math.floor(float(pos_sc[1]))), int(math.floor(float(pos_sc[2]))))
                        blk_b = (
                            (int(math.floor(float(pos_before_sc[0]))), int(math.floor(float(pos_before_sc[1]))), int(math.floor(float(pos_before_sc[2]))))
                            if isinstance(pos_before_sc, (list, tuple)) and len(pos_before_sc) >= 3
                            else None
                        )
                        if sc_res.get("mode") in {"cover_pocket", "cover_or_flee"} or sc_res.get("latched_guard") or blk_sc == blk_b:
                            self._pre_hurt_suppress_block = blk_sc
                            self._pre_hurt_suppress_until = now + 15.0
            elif hurt and in_water:
                self._last_water_escape_at = now
                self._water_hurt_streak += 1
                hurt_src_w = str(hurts[-1].get("source") or "").strip().lower() if hurts else ""
                att_w = (
                    (hurts[-1].get("source_entity") or hurts[-1].get("likely_attacker"))
                    if hurts
                    else None
                ) or status.get("nearest_hostile")
                att_w_dict = att_w if isinstance(att_w, dict) else None
                att_pos_w = att_w_dict.get("pos") if att_w_dict else nh_pos
                close_drowned_hit = nh_name == "drowned" and nh_dist <= 6.0 and hurt_src_w in MELEE_HURT_SOURCES
                close_water_melee_hit = nearby_water_threat and nh_name in PRE_HURT_MELEE_MOBS and hurt_src_w in MELEE_HURT_SOURCES
                urgent_water_melee = self._urgent_water_melee(status, hurts)
                melee_shore = close_water_melee_hit or urgent_water_melee
                swimming = status.get("on_ground") is False or bool(status.get("submerged"))
                retry_shore_after_hit = (
                    swimming and recent_water_escape_failed
                    and self._short_water_escape_after_hit(status, hurt_src_w)
                )
                urgent_trident_shore = swimming and hurt_src_w == "trident" and 0.0 < hp_cur < 10.0
                should_shield_water = (
                    has_shield
                    and not status.get("blocking")
                    and (
                        hurt_src_w in RANGED_HURT_SOURCES
                        or (att_w_dict is not None and (att_w_dict.get("id") is not None or att_w_dict.get("name")))
                        or self._water_hurt_streak >= 2
                    )
                )

                transient_err = False
                if should_shield_water:
                    _, transient_err = self._equip_and_raise_shield_for_water(
                        status,
                        actions,
                        inv=inv,
                        attacker=att_w_dict,
                    )

                forced_water_shore = self._water_hurt_streak >= 3
                if forced_water_shore:
                    self._no_water_until = max(self._no_water_until, now + 30.0)
                    self.events.emit("guardian.no_water_cooldown", duration_s=30.0, streak=self._water_hurt_streak)
                    if "water_avoid_cooldown" not in reasons:
                        reasons.append("water_avoid_cooldown")

                esc_succeeded = False
                esc_respawned = False
                esc_handled_lava = False
                esc_err_code: str | None = None
                if (
                    not transient_err and not forced_water_shore and not no_water_reentry and not drowning
                    and not self._swimming_at_low_health(status) and not (close_drowned_hit or melee_shore)
                    and not (retry_shore_after_hit or urgent_trident_shore or underwater_hot_floor)
                ):
                    if not _already("escape_water"):
                        short_escape = any(self._short_water_escape_after_hit(status, h.get("source")) for h in hurts)
                        budget = {"budget_s": self.COMBAT_WATER_ESCAPE_BUDGET_S} if short_escape else {}
                        actions.append(self._action("escape_water", lambda: self._escape_water(status, **budget)))
                    esc_act = next((a for a in actions if a.get("action") == "escape_water"), None)
                    esc_res = esc_act.get("result") if isinstance(esc_act, dict) and isinstance(esc_act.get("result"), dict) else {}
                    esc_succeeded = bool(esc_act and esc_act.get("ok") and esc_res.get("in_water") is not True)
                    esc_respawned = bool(esc_res.get("respawned"))
                    esc_handled_lava = esc_res.get("mode") == "lava_escape"
                    esc_err_code = (
                        esc_act["error"].get("code")
                        if isinstance(esc_act, dict) and isinstance(esc_act.get("error"), dict)
                        else None
                    )

                shore_succeeded = esc_succeeded
                if (
                    not transient_err
                    and not esc_succeeded
                    and not esc_respawned and not esc_handled_lava
                    and esc_err_code not in {"not_connected", "not_ready", "wrong_server", "network_uncertain"}
                ):
                    if (close_drowned_hit or melee_shore) and att_pos_w is None:
                        target_id = att_w_dict.get("id") if att_w_dict else nh_id
                        target_name = str(att_w_dict.get("name") or "").removeprefix("minecraft:") if att_w_dict else nh_name
                        if target_id is not None or target_name:
                            att_pos_w = self._find_hostile_position(target_id, target_name, timeout_s=0.6)
                    shore_act = self._action(
                        "escape_to_shore",
                        lambda: self.combat_env.escape_deep_water_to_shore(
                            att_pos_w, keep_shield=has_shield,
                            shield_target_id=att_w_dict.get("id") if att_w_dict else nh_id,
                            **({"surface_first": True} if drowning else {}),
                            **({"avoid_close_threat": True} if melee_shore and att_pos_w else {}),
                            **({"under_attack": True} if urgent_water_melee else {}),
                            **({"sneak_on_magma": True} if underwater_hot_floor else {}),
                        ),
                    )
                    actions.append(shore_act)
                    shore_res = shore_act.get("result") if isinstance(shore_act, dict) and isinstance(shore_act.get("result"), dict) else {}
                    shore_succeeded = bool(shore_act.get("ok") and shore_res.get("in_water") is not True)

                if shore_succeeded:
                    self._water_hurt_streak = 0
                    self._last_water_escape_failed = False
                else:
                    self._last_water_escape_failed = True
                self._last_water_escape_at = time.monotonic()
                if (
                    not shore_succeeded
                    and not transient_err
                    and not esc_respawned and not esc_handled_lava
                    and should_shield_water
                    and not _already("raise_shield")
                    and esc_err_code not in {"not_connected", "not_ready", "wrong_server", "network_uncertain"}
                ):
                    try:
                        st_post_w = self.bot.status()
                    except (ApiError, httpx.HTTPError):
                        st_post_w = {}
                    if (
                        st_post_w.get("ready", True)
                        and st_post_w.get("alive") is not False
                        and bool(st_post_w.get("in_water"))
                        and not st_post_w.get("blocking")
                    ):
                        self._equip_and_raise_shield_for_water(
                            st_post_w,
                            actions,
                            inv=inv,
                            attacker=att_w_dict,
                        )
            elif non_combat_only_hurt and not in_water and not in_lava:
                fire_cleared = False
                if can_clear_fire:
                    self._last_fire_clear_at = now
                    clear = self._action(
                        "clear_contact_fire", lambda: self._clear_contact_fire(fire_pos, sneaking=bool(status.get("sneaking"))),
                    )
                    actions.append(clear)
                    fire_cleared = clear["ok"]
                if can_try_fire_douse and not fire_cleared:
                    if counts.get("water_bucket", 0) > 0:
                        self._last_fire_douse_at = now
                        actions.append(self._action("douse_fire", lambda: self.combat_env.build_tactical("place_water_feet")))
                    elif recent_lava:
                        douse = self._douse_after_lava(status, after=status)
                        if douse is not None:
                            self._last_fire_douse_at = now
                            actions.append(douse)
                if critical_non_combat:
                    self._last_non_combat_alert_at = now
            elif effective_combat_hurt and not in_water and not in_lava:
                combat_hurt = next(
                    (h for h in reversed(other_hurts) if str(h.get("source") or "").strip().lower() not in NON_COMBAT_HURT_SOURCES),
                    None,
                )
                recent_hurt = sonic_hurt or combat_hurt or (other_hurts[-1] if other_hurts else {})
                attacker = (
                    recent_hurt.get("source_entity") or recent_hurt.get("likely_attacker")
                    if other_hurts else status.get("nearest_hostile")
                )
                hurt_src = str(recent_hurt.get("source") or "").strip().lower()
                if magic_witch is None and not hurt_src and not recent_hurt.get("source_entity") and any(
                    str(hit.get("source") or "").strip().lower() in {"magic", "poison", "indirect_magic"}
                    for hit in other_hurts
                ):
                    magic_witch = self._find_visible_witch()
                indirect_magic = hurt_src == "indirect_magic" or magic_witch is not None
                if magic_witch is not None:
                    attacker = magic_witch
                ranged_defense = hurt_src in RANGED_HURT_SOURCES or (
                    nh_name in {"blaze", "ghast"}
                    and (hurt_src in FIRE_HURT_SOURCES or (not hurt_src and status.get("on_fire")))
                )
                hp_val = float(health) if isinstance(health, (int, float)) else 20.0
                already_blocking = bool(status.get("blocking"))
                shooter = attacker or status.get("nearest_hostile")
                shooter_name, _, shooter_id, _ = self._extract_hostile_info({"nearest_hostile": shooter})
                blaze_burn_only = (
                    not self._custom_combat_registered and shooter_name != "ghast" and nh_name != "ghast"
                    and str(status.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
                    and status.get("on_ground") is True and self._low_health_burning_after_ranged_hit(status, now)
                    and bool(hurts) and all(str(hit.get("source") or "").strip().lower() == "on_fire" for hit in hurts)
                )

                def hold_blaze_cover() -> dict[str, Any]:
                    blaze_id = shooter_id if shooter_name == "blaze" else None
                    result = self._action("hold_blaze_cover", lambda: self._hold_blaze_cover(status, blaze_id))
                    actions.append(result)
                    if result.get("ok"):
                        self._consecutive_flees = 0
                        self._mark_ranged_engagement(
                            time.monotonic(), attacker=shooter if shooter_name == "blaze" else None, in_cover=True,
                        )
                    if (result.get("result") or {}).get("respawned") and "death_or_connection_changed" not in reasons:
                        reasons.append("death_or_connection_changed")
                    return result

                held_cover = hold_blaze_cover() if blaze_burn_only and now < self._ranged_cover_active_until else None
                cf = None
                if held_cover and (held_cover.get("ok") or (held_cover.get("result") or {}).get("respawned")):
                    self._shield_only_streak = 0
                elif in_wall_hurt:
                    actions.append(self._action("unstick_in_wall", lambda: self._unstick_in_wall(status)))
                elif self._custom_combat_registered or sonic_hurt is not None or nh_name == "warden":
                    att_id = attacker.get("id") if isinstance(attacker, dict) and isinstance(attacker.get("id"), int) else nh_id
                    att_type = (
                        str(attacker.get("name") or nh_name).removeprefix("minecraft:")
                        if isinstance(attacker, dict)
                        else nh_name
                    )
                    trig: ThreatTrigger = {
                        "source": "guard_poll_hurt",
                        "primary_entity_id": att_id,
                        "primary_type": att_type,
                        "primary_pos": attacker.get("pos") if isinstance(attacker, dict) else nh_pos,
                        "distance": float(attacker.get("dist", nh_dist)) if isinstance(attacker, dict) else nh_dist,
                        "hurt_source": hurt_src,
                    }
                    actions.append(
                        self._action(
                            "subcortical_combat",
                            lambda: self._run_subcortical_combat(trig),
                        )
                    )
                    if hurt_src in RANGED_HURT_SOURCES or att_type in RANGED_MOBS:
                        self._mark_ranged_engagement(time.monotonic(), trigger=trig)
                elif (
                    has_shield
                    and not already_blocking
                    and not ranged_defense
                    and not indirect_magic
                    and hp_val >= 8.0
                    and (now - self._last_shield_only_at >= 12.0 or self._shield_only_streak < 1)
                ):
                    self._shield_only_streak += 1
                    self._last_shield_only_at = now
                    if attacker and attacker.get("id") is not None:
                        actions.append(self._action("face_attacker", lambda: self.bot.call("look", {"entity_id": attacker["id"]})))
                    actions.append(
                        self._action(
                            "raise_shield",
                            lambda: self.bot.call(
                                "posture",
                                {
                                    "shield": True,
                                    "jump": needs_float or bool(status.get("jumping")),
                                    "sneak": bool(status.get("sneaking")),
                                },
                            ),
                        )
                    )
                elif ranged_defense:
                    self._shield_only_streak = 0
                    att_obj = attacker if isinstance(attacker, dict) else (status.get("nearest_hostile") if isinstance(status.get("nearest_hostile"), dict) else None)
                    self._mark_ranged_engagement(now, attacker=att_obj)
                    att_pos_r = (attacker.get("pos") if isinstance(attacker, dict) else None) or nh_pos
                    att_id_r = attacker.get("id") if isinstance(attacker, dict) else None
                    if att_id_r is None and (nh_name in RANGED_MOBS or (hurt_src == "trident" and nh_name == "drowned")):
                        att_id_r = nh_id
                    if already_blocking and hp_val <= LOW_HP_BUNKER_MAX and att_id_r is not None:
                        actions.append(self._action("face_attacker", lambda: self.bot.call("look", {"entity_id": att_id_r})))
                    pos_r = status.get("pos")
                    if (
                        not already_blocking
                        and isinstance(pos_r, (list, tuple))
                        and len(pos_r) >= 3
                        and isinstance(att_pos_r, (list, tuple))
                        and len(att_pos_r) >= 3
                        and status.get("on_ground") is True
                    ):
                        to_yaw_r, _ = aim_yaw_pitch(pos_r, att_pos_r)
                        self.combat_env.invalidate_block_cache()
                        for offset in (math.pi / 2.0, -math.pi / 2.0):
                            chosen_side = to_yaw_r + offset
                            route = self.combat_env.probe_retreat(pos_r, chosen_side, 3.5)
                            if route is not None and not route[1]:
                                def _sidestep_2m() -> dict[str, Any]:
                                    self.bot.call("look", {"yaw": chosen_side, "pitch": 0.0})
                                    return self.bot.call(
                                        "control",
                                        {"forward": True, "sprint": True, "jump": False, "ms": 400, "stop_on": ["death"]},
                                    )

                                actions.append(self._action("lateral_sidestep", _sidestep_2m))
                                break
                        if has_shield:
                            self._equip_and_raise_shield_for_water(
                                {**status, "in_water": False, "jumping": False},
                                actions,
                                inv=inv if isinstance(inv, dict) else {},
                                attacker=attacker if isinstance(attacker, dict) else status.get("nearest_hostile"),
                            )
                    att_name_r = str((att_obj or {}).get("name") or nh_name or "").removeprefix("minecraft:").strip().lower()
                    trident_attacker_id = None
                    if hurt_src == "trident":
                        if att_name_r == "drowned" and isinstance(att_id_r, int):
                            trident_attacker_id = att_id_r
                        elif nh_name == "drowned" and isinstance(nh_id, int):
                            trident_attacker_id = nh_id
                    fresh_blast = hurt_src in {"fireball", "small_fireball", "wither_skull"}
                    skip_cover_for_blast = fresh_blast or att_name_r in {"blaze", "ghast"} or bool(status.get("on_fire"))
                    source_name, _, source_id, _ = self._extract_hostile_info({"nearest_hostile": recent_hurt.get("source_entity")})
                    confirmed_blaze_fireball = (
                        str(status.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
                        and att_name_r == "blaze" and isinstance(att_id_r, int)
                        and (hurt_src == "small_fireball" or (hurt_src == "fireball" and source_name == "blaze" and source_id == att_id_r))
                    )
                    close_unshielded_shooter = (
                        not has_shield and hp_val <= LOW_HP_BUNKER_MAX and nh_name in RANGED_MOBS and nh_dist <= 3.5
                    )
                    def cover_block(pos: Any) -> tuple[int, int, int] | None:
                        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                            return math.floor(float(pos[0])), math.floor(float(pos[1]) + 0.05), math.floor(float(pos[2]))
                        return None

                    def checked_cover() -> dict[str, Any]:
                        result = self._run_recovery_macro(
                            status, self.combat_env.dig_cover_pocket, budget_s=self.COMBAT_COVER_BUDGET_S,
                        )
                        if result.get("ok") or result.get("respawned"):
                            return result
                        try:
                            after = self.bot.call("status", method="GET", timeout_s=0.6, allow_reauth=False)
                        except (ApiError, httpx.HTTPError):
                            return result
                        if after.get("alive") is False or (
                            after.get("last_death") is not None and after["last_death"] != status.get("last_death")
                        ):
                            return {**result, "ok": False, "respawned": True, "requires_replan": True}
                        return result

                    def try_cover(block: tuple[int, int, int] | None) -> dict[str, Any]:
                        result = self._action("dig_cover", checked_cover)
                        actions.append(result)
                        if (result.get("result") or {}).get("respawned"):
                            if "death_or_connection_changed" not in reasons:
                                reasons.append("death_or_connection_changed")
                            return result
                        self._last_dig_cover_block = block
                        self._last_dig_cover_at = time.monotonic()
                        if result.get("ok"):
                            self._last_dig_cover_fail_at = float("-inf")
                            self._mark_ranged_engagement(now, attacker=att_obj, in_cover=True)
                        else:
                            self._last_dig_cover_fail_at = now
                        return result

                    cur_blk_r = cover_block(pos_r)
                    same_spot_cover_ineffective = (
                        cur_blk_r is not None
                        and cur_blk_r == self._last_dig_cover_block
                        and (now - self._last_dig_cover_at < 20.0)
                    )
                    already_in_dug_cover = (
                        not skip_cover_for_blast
                        and not sustained_shield_damage
                        and not same_spot_cover_ineffective
                        and (now < self._ranged_cover_active_until)
                        and hp_val > 10.0
                        and ranged_hurt_only
                    )
                    surface_projectile_cover = (
                        not skip_cover_for_blast and status.get("on_ground") is True
                        and str(status.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                    )
                    recent_cover_fail = (
                        already_blocking and ranged_hurt_only and now - self._last_dig_cover_fail_at < 15.0
                    )
                    seek_cover_after_failed_dig = (
                        recent_cover_fail and surface_projectile_cover and hurt_src == "arrow" and 0 < hp_val <= 10.0
                    )
                    cover: dict[str, Any] | None = None
                    if (
                        status.get("on_ground") is not False
                        and not skip_cover_for_blast
                        and not sustained_shield_damage
                        and not close_unshielded_shooter
                        and not same_spot_cover_ineffective
                        and not already_in_dug_cover
                        and not (recent_cover_fail and hp_val > 10.0)
                        and not seek_cover_after_failed_dig
                    ):
                        cover = try_cover(cur_blk_r)
                    cover_ok = bool(cover and cover.get("ok")) or already_in_dug_cover
                    cover_changed_life = bool(((cover or {}).get("result") or {}).get("respawned"))
                    if not cover_ok and not cover_changed_life and (
                        sustained_shield_damage or skip_cover_for_blast
                        or not already_blocking or hp_val <= 10.0 or not ranged_hurt_only
                    ):
                        if (
                            has_shield
                            and not already_blocking
                            and not _already("raise_shield")
                            and isinstance(inv, dict)
                            and "offhand" in inv
                        ):
                            self._equip_and_raise_shield_for_water(
                                {**status, "in_water": False, "jumping": False},
                                actions,
                                inv=inv,
                                attacker=attacker if isinstance(attacker, dict) else None,
                            )
                        critical_shielded_arrows = (
                            surface_projectile_cover and ranged_hurt_only and already_blocking
                            and hurt_src == "arrow" and 4.0 < hp_val <= 6.0
                        )
                        shielded_cover = (
                            sustained_shield_damage or seek_cover_after_failed_dig or critical_shielded_arrows
                            or (surface_projectile_cover and ranged_hurt_only and already_blocking and 0 < hp_val <= 4.0)
                        )
                        unshielded_cover = not has_shield and not close_unshielded_shooter and hp_val <= LOW_HP_BUNKER_MAX
                        trident_cover = (
                            surface_projectile_cover and trident_attacker_id is not None and 0 < hp_val <= LOW_HP_BUNKER_MAX
                        )
                        pocket_reason = ((cover or {}).get("result") or {}).get("reason")
                        pocket_unavailable_here = pocket_reason in NO_DIG_COVER_REASONS
                        pocket_unavailable = (
                            surface_projectile_cover and not has_shield and (ranged_hurt_only or nh_name in RANGED_MOBS)
                            and pocket_unavailable_here
                        )
                        if not pocket_unavailable and not has_shield and not att_pos_r and isinstance(att_id_r, int):
                            shooter_pos = self._find_hostile_position(att_id_r, att_name_r)
                            if shooter_pos is not None:
                                attacker = {**(att_obj or {}), "pos": shooter_pos}
                        cf = self._action(
                            "combat_or_flee",
                            lambda: self._defend_or_flee(
                                status, attacker, counts, ranged=True,
                                prefer_cover=unshielded_cover or shielded_cover or pocket_unavailable or trident_cover,
                                allow_stationary_cover=not (
                                    fresh_blast or pocket_unavailable or sustained_shield_damage
                                    or seek_cover_after_failed_dig or critical_shielded_arrows
                                ),
                                verify_live_shooters=surface_projectile_cover and hurt_src == "arrow" and not has_shield,
                                **({"trident_attacker_id": trident_attacker_id} if trident_cover else {}),
                            ),
                        )
                        actions.append(cf)
                        cf_result = cf.get("result") or {}
                        cf_mode = cf_result.get("mode")
                        if cf.get("ok") and cf_mode == "natural_cover":
                            self._mark_ranged_engagement(time.monotonic(), attacker=att_obj, in_cover=True)
                        if cf_mode == "flee":
                            self._consecutive_flees += 1
                            moved_dist = cf_result.get("moved_dist")
                            moved_after_failed_pocket = (
                                already_blocking and pocket_unavailable_here
                                and isinstance(moved_dist, (int, float)) and moved_dist > 0.0
                            )
                            shore = None
                            if cf_result.get("reason") == "unsafe_landing" and not cf_result.get("respawned"):
                                shore = self._recover_water_landing_after_retreat(
                                    status, self.combat_env._safe_status(timeout_s=0.6),
                                    threat_pos=att_pos_r, threat_id=att_id_r,
                                    threat_name=att_name_r, has_shield=has_shield,
                                )
                                if shore is not None:
                                    actions.append(shore)
                            if (
                                confirmed_blaze_fireball and not cf_result.get("respawned")
                                and cf_result.get("reason") == "no_safe_retreat"
                            ):
                                try:
                                    landing = self.bot.call("status", method="GET", timeout_s=0.45)
                                except (ApiError, httpx.HTTPError):
                                    landing = {}
                                if (
                                    landing.get("ready") is True and landing.get("alive") is True
                                    and landing.get("on_ground") is True and landing.get("pos")
                                    and not any(landing.get(key) for key in ("busy", "in_water", "in_lava"))
                                    and all(landing.get(key) == status.get(key) for key in ("dimension", "last_death"))
                                ):
                                    blaze_cover = self._action(
                                        "natural_cover_after_retreat",
                                        lambda: self._defend_or_flee(
                                            landing, {"id": att_id_r, "name": "blaze"}, counts, ranged=True,
                                            cover_only=True, allow_stationary_cover=False, verify_live_shooters=True,
                                            confirmed_blaze_cover=True,
                                        ),
                                    )
                                    actions.append(blaze_cover)
                                    if blaze_cover.get("ok"):
                                        self._consecutive_flees = 0
                                        self._mark_ranged_engagement(time.monotonic(), attacker=att_obj, in_cover=True)
                            stalled_under_arrows = (
                                not has_shield and hurt_src == "arrow" and 0 < hp_val <= LOW_HP_BUNKER_MAX
                                and cf_result.get("reason") in {"no_safe_retreat", "unsafe_landing"}
                            )
                            stalled_under_trident = (
                                hurt_src == "trident" and source_name in {"", "drowned"}
                                and not cf.get("ok") and 0 < hp_val <= 6.0
                                and cf_result.get("reason") in {"no_safe_retreat", "unsafe_landing"}
                            )
                            retry_pocket_after_knockback = (
                                not has_shield and not skip_cover_for_blast and status.get("on_ground") is False
                                and cf_result.get("reason") == "no_safe_retreat"
                                and not (stalled_under_trident and pocket_reason == "no_filler_block")
                            )

                            def confirm_trident_landing(landing: dict[str, Any]) -> dict[str, Any]:
                                return self._defend_or_flee(
                                    landing, None, counts, ranged=True, prefer_cover=True, cover_only=True,
                                    trident_attacker_id=source_id if source_name == "drowned" else None,
                                    verify_live_shooters=True,
                                    confirm_trident_cover=True,
                                )

                            cover_material = (
                                self.combat_env.arrow_cover_material(counts) if hurt_src == "arrow" and not has_shield else None
                            )
                            full_cover_stack = cover_material is not None and counts[cover_material] >= 2
                            repeated_arrow_damage = (
                                cover_material is not None and ranged_hurt_only
                                and (len(hurts) >= 2 or (not deaths and now - previous_ranged_hurt_at < 8.0))
                            )
                            cover_hp_limit = 4.0
                            if full_cover_stack:
                                cover_hp_limit = 7.0 if repeated_arrow_damage else 6.0
                            elif repeated_arrow_damage:
                                cover_hp_limit = 6.0
                            critical_landing = (
                                cf.get("ok") and (not has_shield or recent_cover_fail or moved_after_failed_pocket)
                                and (hurt_src == "arrow" or trident_attacker_id is not None)
                                and (0 < hp_val <= 6.0 or repeated_arrow_damage)
                            )
                            natural_cover = None
                            overworld_landing = str(status.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                            if (
                                shore is None and not cf_result.get("respawned")
                                and (critical_landing or stalled_under_arrows or stalled_under_trident) and overworld_landing
                            ):
                                try:
                                    landing = self.bot.call("status", method="GET", timeout_s=0.6)
                                except (ApiError, httpx.HTTPError):
                                    landing = {}
                                landing_hp = landing.get("health")
                                pocket_already_tried = (
                                    cover_block(landing.get("pos")) == self._last_dig_cover_block
                                    and now - self._last_dig_cover_at < 20.0
                                )
                                if (
                                    landing.get("ready") is True and landing.get("alive") is True
                                    and landing.get("on_ground") is True and landing.get("pos")
                                    and not any(landing.get(key) for key in ("in_water", "in_lava", "on_fire", "busy"))
                                    and (not stalled_under_trident or not landing.get("submerged"))
                                    and bool(landing.get("blocking")) == has_shield
                                    and all(landing.get(key) == status.get(key) for key in ("last_death", "dimension"))
                                    and isinstance(landing_hp, (int, float))
                                    and 0 < landing_hp <= (6.0 if stalled_under_trident else cover_hp_limit)
                                    and (not retry_pocket_after_knockback or pocket_already_tried)
                                ):
                                    natural_cover = self._action(
                                        "natural_cover_after_retreat",
                                        lambda: confirm_trident_landing(landing) if stalled_under_trident else self._defend_or_flee(
                                            landing, None, counts, ranged=True, prefer_cover=True, cover_only=True,
                                            trident_attacker_id=trident_attacker_id,
                                            verify_live_shooters=hurt_src == "arrow" and not has_shield,
                                        ),
                                    )
                            if retry_pocket_after_knockback and natural_cover is None:
                                # The retreat waited out knockback but found no route.
                                landing = self.combat_env._safe_status({"ready": False})
                                landing_block = cover_block(landing.get("pos"))
                                if (
                                    landing_block is not None and landing.get("on_ground") is True
                                    and landing.get("ready") is not False and landing.get("alive") is not False
                                    and not landing.get("in_water") and not landing.get("in_lava") and not landing.get("on_fire")
                                    and landing.get("last_death") == status.get("last_death")
                                    and landing.get("dimension") == status.get("dimension")
                                    and (
                                        landing_block != self._last_dig_cover_block or now - self._last_dig_cover_at >= 20.0
                                    )
                                ):
                                    landing_cover = try_cover(landing_block).get("result") or {}
                                    if (
                                        hurt_src == "arrow" and landing_cover.get("reason") in NO_DIG_COVER_REASONS
                                        and not landing_cover.get("respawned")
                                    ):
                                        natural_cover = self._action(
                                            "natural_cover",
                                            lambda: self._defend_or_flee(
                                                landing, attacker, counts, ranged=True, prefer_cover=True,
                                                allow_stationary_cover=False, cover_only=True, verify_live_shooters=True,
                                            ),
                                        )
                                    elif (
                                        stalled_under_trident and shore is None and overworld_landing
                                        and landing_cover.get("reason") in NO_DIG_COVER_REASONS
                                        and not landing_cover.get("respawned")
                                        and not landing.get("busy") and not landing.get("submerged")
                                        and isinstance(landing.get("health"), (int, float)) and 0 < landing["health"] <= 6.0
                                    ):
                                        natural_cover = self._action(
                                            "natural_cover_after_retreat", lambda: confirm_trident_landing(landing),
                                        )
                            if natural_cover is not None:
                                actions.append(natural_cover)
                                landing_hp = landing.get("health")
                                if natural_cover.get("ok"):
                                    self._mark_ranged_engagement(
                                        time.monotonic(),
                                        attacker=att_obj if not stalled_under_trident or source_name == "drowned" else None,
                                        in_cover=True,
                                    )
                                elif (
                                    overworld_landing and cover_material is not None and not landing.get("blocking")
                                    and isinstance(landing_hp, (int, float)) and 0 < landing_hp <= cover_hp_limit
                                    and not (natural_cover.get("result") or {}).get("respawned")
                                ):
                                    placed_cover = self._action(
                                        "place_arrow_cover", lambda: self._place_arrow_cover(landing, counts, att_id_r),
                                    )
                                    actions.append(placed_cover)
                                    if placed_cover.get("ok"):
                                        self._mark_ranged_engagement(time.monotonic(), attacker=att_obj, in_cover=True)
                            if (
                                cf.get("ok") and has_shield and isinstance(att_id_r, int)
                                and hp_val <= LOW_HP_BUNKER_MAX
                                and (already_blocking or any(a["action"] == "raise_shield" and a["ok"] for a in actions))
                            ):
                                actions.append(self._action(
                                    "face_attacker_after_retreat",
                                    lambda: self.bot.call("look", {"entity_id": att_id_r}, timeout_s=0.9),
                                ))
                        else:
                            self._consecutive_flees = 0
                else:
                    self._shield_only_streak = 0
                    dimension = str(status.get("dimension") or "").removeprefix("minecraft:")
                    nearest = status.get("nearest_hostile") or {}
                    threat = attacker or nearest
                    raw_dist = threat.get("dist", 99.0)
                    try:
                        eff_dist = float(raw_dist)
                    except (TypeError, ValueError):
                        eff_dist = 99.0
                    melee_or_airborne = (
                        (status.get("on_ground") is not True)
                        or hurt_src in MELEE_HURT_SOURCES
                        or hurt_src in {"player_explosion", "entity_explosion"}
                        or (eff_dist <= 4.5)
                    )
                    bunker_grounded = status.get("on_ground") is True and nh_name != "creeper"
                    inside_shaft = bunker_grounded and hp_val <= LOW_HP_BUNKER_MAX and self._inside_own_shaft()
                    exposed_to_lethal_melee = (
                        dimension == "the_nether" and 0 < hp_val <= 4.0 and not inside_shaft
                        and (hurt_src in MELEE_HURT_SOURCES or now - self._last_melee_hurt_at < 4.0)
                        and (
                            (nh_name in PRE_HURT_MELEE_MOBS and 0 <= nh_dist <= 3.5 and not nearest.get("blocked_by_wall"))
                            or (
                                0 <= eff_dist <= 3.5 and not threat.get("blocked_by_wall")
                                and str(threat.get("name") or "").removeprefix("minecraft:") in PRE_HURT_MELEE_MOBS
                            )
                        )
                    )
                    can_surface_night_bunker = (
                        bunker_grounded
                        and (
                            (hp_val <= LOW_HP_BUNKER_MAX and (inside_shaft or not has_melee_weapon or self._consecutive_flees >= 1))
                            or (not has_melee_weapon and status.get("is_day") is False and self._consecutive_flees >= 1 and hp_val <= 14.0)
                        )
                    )
                    bunker = None
                    if not indirect_magic and not exposed_to_lethal_melee and (can_surface_night_bunker or not melee_or_airborne):
                        bunker = self._try_emergency_bunker(status)
                    if bunker is not None:
                        actions.append(bunker)
                    bunker_result = (bunker or {}).get("result") or {}
                    if self._bunker_life_unavailable(bunker):
                        reasons.append("death_or_connection_changed")
                    elif not (bunker and bunker["ok"]):
                        unsafe_footing = bunker_result.get("reason") in {"unsafe_floor", "unsafe_open_side"} or (
                            self._blocked_bunker is not None and self._blocked_bunker[0] == self._bunker_site(status)
                        )
                        cf = self._action(
                            "combat_or_flee",
                            lambda: self._defend_or_flee(
                                status, attacker, counts,
                                **({"ranged": True} if indirect_magic else {}),
                                **({"require_safe_footing": True} if unsafe_footing else {}),
                            ),
                        )
                        actions.append(cf)
                        if isinstance(cf.get("result"), dict) and cf["result"].get("mode") == "flee":
                            self._consecutive_flees += 1
                        else:
                            self._consecutive_flees = 0
                    cf_result = (cf or {}).get("result") or {}
                    piglin_hurt = dimension == "the_nether" and nh_name == "piglin" and hurt_src == "mob_attack"
                    if (
                        not has_shield and has_melee_weapon and hurt_src in MELEE_HURT_SOURCES
                        and 0 < hp_val <= (CORNERED_PIGLIN_MAX_HEALTH if piglin_hurt else 8.0)
                        and not cf_result.get("ok") and not cf_result.get("respawned")
                        and cf_result.get("reason") in {"no_safe_retreat", "unsafe_landing"}
                    ):
                        strike = self._try_cornered_melee_strike(status, attacker, counts, hurt_source=hurt_src)
                        if strike is not None:
                            actions.append(strike)
                    elif (
                        dimension == "the_nether" and nh_name == "wither_skeleton" and nh_id is not None
                        and already_blocking and has_melee_weapon and 0 < hp_val <= 6.0
                        and (
                            hurt_src == "mob_attack"
                            or (hurt_src in {"", "wither"} and now - self._last_melee_hurt_at < 4.0)
                        )
                        and (not isinstance(threat.get("id"), int) or threat["id"] == nh_id)
                        and not cf_result.get("ok") and not cf_result.get("respawned")
                        and cf_result.get("reason") == "no_safe_retreat" and cf_result.get("moved_dist") == 0.0
                    ):
                        actions.extend(self._cornered_wither_skeleton_response(status, counts, nh_id))
                    melee_retreat = cf_result.get("mode") == "flee"
                    enderman_name, _, enderman_id, _ = self._extract_hostile_info({"nearest_hostile": threat})
                    if (
                        melee_retreat and has_shield and hurt_src == "mob_attack" and not cf_result.get("respawned")
                        and enderman_name == "enderman" and enderman_id is not None
                    ):
                        actions.append(self._action(
                            "guard_enderman_pursuit", lambda: self.combat_env.guard_enderman_pursuit(status, enderman_id),
                        ))
                    nether_melee_retreat = dimension == "the_nether" and melee_retreat
                    recent_ranged_hurt = now - self._last_ranged_hurt_at < 8.0
                    mixed_surface_retreat = (
                        dimension == "overworld" and melee_retreat and 0.0 < hp_val <= 4.0
                        and recent_ranged_hurt
                    )
                    face_after_melee_retreat = (
                        already_blocking and (nether_melee_retreat or mixed_surface_retreat)
                        and cf_result.get("reason") in {None, "no_safe_retreat", "unsafe_landing"}
                    )
                    check_critical_surface_landing = (
                        dimension == "overworld" and melee_retreat and already_blocking
                        and cf_result.get("ok") and hp_val > 4.0
                    )
                    check_unraised_shield = (
                        dimension == "overworld" and melee_retreat and has_shield
                        and cf_result.get("ok") and not cf_result.get("respawned")
                        and (hp_val <= LOW_HP_BUNKER_MAX or (not already_blocking and recent_ranged_hurt))
                    )
                    shield_when_cornered = (
                        nether_melee_retreat and has_shield and not already_blocking
                        and cf_result.get("reason") == "no_safe_retreat"
                    )
                    follow_unarmed_retreat = (
                        not has_shield and not has_melee_weapon and cf and melee_retreat
                        and not cf_result.get("respawned")
                        and retreat_made_progress(bool(cf.get("ok")), cf_result.get("moved_dist"), cf_result.get("reason"))
                    )
                    melee_after_retreat = hurt_src in MELEE_HURT_SOURCES or (
                        nether_melee_retreat and not hurt_src and now - self._last_melee_hurt_at < 4.0
                    )
                    retry_bunker_after_landing = (
                        dimension == "overworld" and melee_retreat and not (has_shield or has_melee_weapon)
                        and cf_result.get("reason") == "no_safe_retreat" and not cf_result.get("respawned")
                        and (
                            bunker_result.get("reason") == "not_grounded"
                            or (bunker is None and status.get("on_ground") is False)
                        )
                    )
                    shore = None
                    landing = None
                    if melee_retreat and cf_result.get("reason") == "unsafe_landing" and not cf_result.get("respawned"):
                        landing = self.combat_env._safe_status(timeout_s=0.6)
                        target = attacker if isinstance(attacker, dict) else {}
                        shore = self._recover_water_landing_after_retreat(
                            status, landing, threat_pos=target.get("pos") or nh_pos,
                            threat_id=target.get("id") or nh_id, threat_name=target.get("name") or nh_name,
                            has_shield=has_shield, avoid_close_threat=True,
                        )
                        if shore is not None:
                            actions.append(shore)
                    retry_after_knockback = (
                        dimension == "overworld" and not (has_shield or has_melee_weapon or recent_ranged_hurt)
                        and landing is not None and landing.get("ready") is True
                        and not (landing.get("busy") or landing.get("on_fire"))
                        and cf_result.get("on_ground") is False and cf_result.get("moved_dist") == 0.0
                        and not (cf_result.get("in_water") or cf_result.get("in_lava"))
                        and nh_id is not None and (landing.get("nearest_hostile") or {}).get("id") == nh_id
                        and not (landing.get("nearest_hostile") or {}).get("blocked_by_wall")
                    )
                    if shore is None and melee_after_retreat and (
                        face_after_melee_retreat or check_critical_surface_landing
                        or check_unraised_shield or shield_when_cornered or follow_unarmed_retreat
                        or retry_bunker_after_landing or retry_after_knockback
                    ):
                        if landing is None:
                            landing = self.combat_env._safe_status(timeout_s=0.6)
                        mob, dist, target_id, target_pos = self._extract_hostile_info(landing)
                        current_hp = landing.get("health")
                        if (
                            check_unraised_shield and landing.get("on_ground") is True
                            and landing.get("blocking") is False and not landing.get("on_fire")
                            and isinstance(current_hp, (int, float)) and 0 < current_hp <= 4.0
                            and (
                                (mob in PRE_HURT_MELEE_MOBS and dist <= 3.5)
                                or (recent_ranged_hurt and mob in RANGED_MOBS and dist <= 12.0)
                            )
                        ):
                            actions.append(self._action(
                                "raise_shield_after_retreat",
                                lambda: {"ok": self._restore_shield_near_threat(status, landing, self.bot)},
                            ))
                        if (
                            landing.get("on_ground") is True
                            and landing.get("ready") is not False and landing.get("alive") is not False
                            and not landing.get("in_lava") and not landing.get("in_water")
                            and landing.get("last_death") == status.get("last_death")
                            and landing.get("dimension") == status.get("dimension")
                            and isinstance(current_hp, (int, float)) and 0 < current_hp <= LOW_HP_BUNKER_MAX
                            and mob in PRE_HURT_MELEE_MOBS and target_id is not None and dist <= 3.5
                        ):
                            if landing.get("blocking") and (
                                face_after_melee_retreat or (check_critical_surface_landing and current_hp <= 4.0)
                            ):
                                actions.append(self._action(
                                    "face_attacker_after_retreat",
                                    lambda: self.bot.call("look", {"entity_id": target_id}, timeout_s=0.9),
                                ))
                            elif shield_when_cornered:
                                self._equip_and_raise_shield_for_water(
                                    landing, actions, inv=inv, attacker={"id": target_id},
                                )
                            elif retry_bunker_after_landing and landing.get("pos") and not (
                                landing.get("busy") or landing.get("on_fire")
                            ):
                                late_bunker = self._try_emergency_bunker(landing, budget_s=self.COMBAT_COVER_BUDGET_S)
                                if late_bunker is not None:
                                    actions.append(late_bunker)
                                    if self._bunker_life_unavailable(late_bunker):
                                        reasons.append("death_or_connection_changed")
                            elif (follow_unarmed_retreat or retry_after_knockback) and landing.get("pos"):
                                if target_pos is None:
                                    target_pos = self._find_hostile_position(target_id, mob, timeout_s=0.6)
                                if (
                                    target_pos and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in target_pos[:3])
                                    and math.dist(landing["pos"][:3], target_pos[:3]) <= 3.5
                                ):
                                    actions.append(self._action(
                                        "followup_melee_retreat",
                                        lambda: self.combat_env.step_away_from_melee(
                                            landing, target_pos, deadline=time.monotonic() + 1.5, target_id=target_id,
                                        ),
                                    ))

                if blaze_burn_only and held_cover is None and cf and not cf.get("ok"):
                    retreat = cf.get("result") or {}
                    if (
                        retreat.get("mode") == "flee" and retreat.get("reason") == "no_safe_retreat"
                        and not retreat.get("respawned")
                    ):
                        hold_blaze_cover()

            if should_eat and (
                not effective_combat_hurt
                or any(
                    a["action"] in {"emergency_bunker", "combat_or_flee", "subcortical_combat"} and a["ok"]
                    for a in actions
                )
            ):
                cur_eat_st = status
                eat_events_post: list[dict[str, Any]] = []
                if actions:
                    try:
                        cur_eat_st = self.bot.status()
                        eat_events_post = self._drain_events_pages()
                    except (ApiError, httpx.HTTPError):
                        cur_eat_st = {**status, "ready": False}
                if eat_events_post:
                    fresh.extend(eat_events_post)
                post_deaths = deaths or [
                    e for e in eat_events_post if e.get("type") in {"death", "death_loop", "disconnected", "spawn"}
                ]
                cur_food = cur_eat_st.get("food")
                if post_deaths or not cur_eat_st.get("ready", True) or cur_eat_st.get("alive") is False:
                    self.retry_after_ready = True
                    self.events.emit("guardian.eat_deferred", reason="post_combat_not_ready", retry_after_ready=True)
                else:
                    can_eat_now = self._can_auto_eat_safely(cur_eat_st, min_dist=5.5) and (
                        not isinstance(cur_food, (int, float)) or cur_food < 20
                    )
                    best_food = next((name for name in SAFE_FOOD_PRIORITY if counts.get(name, 0) > 0), None) if can_eat_now else None
                    if best_food:
                        eat_out = self._try_eat_food(best_food, cur_eat_st)
                        if eat_out is not None:
                            reasons.append("needs_food")
                            actions.append(eat_out)
                            if eat_out.get("ok"):
                                self.retry_after_ready = False
                    elif can_eat_now:
                        self.retry_after_ready = False
        finally:
            self._in_reflex = False

        if not actions and not critical_non_combat and not (deaths or disconnected or not ready or status.get("alive") is False or status.get("busy")):
            return None
        eat_interrupted = any(
            a.get("action") == "eat"
            and isinstance(a.get("result"), dict)
            and a["result"].get("interrupted_by") == "hurt"
            for a in actions
        )
        if eat_interrupted and "fresh_hurt" not in reasons:
            reasons.append("fresh_hurt")
        exempt_ranged_cancel = (
            ranged_hurt_only
            and hp_cur > 10.0
            and already_in_ranged_guard
            and not eat_interrupted
            and not deaths
            and not disconnected
            and not in_lava
            and not in_water
            and not needs_float
            and not any(a["action"] == "escape_to_shore" for a in actions)
            and not (sustained_shield_damage and any(a["action"] == "combat_or_flee" and a.get("ok") for a in actions))
            and not status.get("busy")
        )
        replan = (
            not exempt_ranged_cancel
            and bool(
                effective_combat_hurt
                or critical_non_combat
                or pre_trigger is not None
                or in_lava
                or deaths
                or disconnected
                or needs_float
                or not ready
                or status.get("alive") is False
                or any(
                    a.get("action") == "hot_floor_sneak"
                    and (not a.get("ok") or (a.get("result") or {}).get("mode") == "stepped_off")
                    for a in actions
                )
                or any(r not in {"needs_food", "hot_floor"} for r in reasons)
            )
        )
        try:
            post_status = self.bot.status()
        except (ApiError, httpx.HTTPError):
            post_status = {**status, "ready": False, "alive": False, "busy": None}
            if "death_or_connection_changed" not in reasons:
                reasons.append("death_or_connection_changed")
            replan = True
        if (
            not in_lava and self._same_life(status, post_status)
            and any(a["action"] in {"subcortical_combat", "combat_or_flee"} for a in actions)
            and not any(
                isinstance(a.get("result"), dict) and a["result"].get("mode") == "lava_escape" for a in actions
            )
        ):
            falling_over_lava = False
            if self._nether_fast_fall(post_status):
                post_status, falling_over_lava = self._observe_nether_fall(post_status)
            if not self._same_life(status, post_status):
                if "death_or_connection_changed" not in reasons:
                    reasons.append("death_or_connection_changed")
                replan = True
            elif post_status.get("in_lava") or falling_over_lava:
                if post_status.get("in_lava"):
                    self._last_lava_contact_at = time.monotonic()
                self._in_reflex = True
                try:
                    actions.append(self._action("escape_lava", self.combat_env.escape_lava))
                    post_status = self.combat_env._safe_status(post_status)
                finally:
                    self._in_reflex = False
                reasons.append("in_lava")
                replan = True
        post_events_verified = False
        if actions:
            try:
                post_fresh = self._drain_events_pages()
                post_events_verified = True
            except (ApiError, httpx.HTTPError):
                post_fresh = []
            if post_fresh:
                fresh.extend(post_fresh)
                if any(e.get("type") in {"death", "death_loop", "disconnected", "spawn"} for e in post_fresh):
                    if "death_or_connection_changed" not in reasons:
                        reasons.append("death_or_connection_changed")
                    replan = True
                elif (
                    any(e.get("type") == "hurt" and e.get("source") == "hot_floor" for e in post_fresh)
                    and post_status.get("ready") is True and post_status.get("alive") is not False
                    and not (post_status.get("in_water") or post_status.get("in_lava") or post_status.get("busy"))
                    and post_status.get("last_death") == status.get("last_death")
                    and post_status.get("dimension") == status.get("dimension")
                    and self._hot_floor_block(post_status) is not None
                    and self._needs_hot_floor_stance(post_status, time.monotonic())
                ):
                    # Combat can leave the bot on magma after the initial event scan.
                    try:
                        with self.bot.limit_until(time.monotonic() + 0.5):
                            self.combat_env.invalidate_block_cache()
                            on_magma = self.combat_env.is_on_hot_floor(post_status["pos"])
                    except _RecoveryBudgetExceeded:
                        on_magma = False
                    if on_magma:
                        actions.append(self._action("hot_floor_sneak", lambda: self._sneak_on_hot_floor(post_status)))
                        if "hot_floor" not in reasons:
                            reasons.append("hot_floor")
                        replan = True
                        post_status = self.combat_env._safe_status(post_status)
        if (
            post_status.get("on_ground") is True and "death_or_connection_changed" not in reasons
            and any(
                action["action"] == "subcortical_combat" and (action.get("error") or {}).get("timeout") is True
                for action in actions
            )
            and self._restore_shield_near_threat(status, post_status, self.bot)
        ):
            actions.append({"action": "raise_shield_after_combat_timeout", "ok": True})
            post_status = {**post_status, "blocking": True}
        if pre_trigger is not None and post_events_verified and not any(e.get("type") == "hurt" for e in fresh):
            if reasons == ["pre_hurt_melee"] and len(actions) == 1 and actions[0]["action"] == "subcortical_combat":
                outcome = actions[0].get("result") or {}
                target_id = pre_trigger.get("primary_entity_id")
                target_pos = pre_trigger.get("primary_pos")
                if (
                    outcome.get("mode") == "combat_timeout" and outcome.get("timeout_endpoint") == "dig"
                    and target_id == self._extract_hostile_info(post_status)[2] and target_pos is not None
                    and self._same_stationary_melee_timeout_state(status, post_status)
                    and self._only_stationary_melee_threat(target_id, target_pos)
                ):
                    self._stationary_melee_dig_timeout = (
                        time.monotonic() + self.MELEE_DIG_TIMEOUT_RETRY_S, deepcopy(post_status), list(target_pos[:3]), None,
                    )
            if reasons in (["pre_hurt_creeper"], ["pre_hurt_melee"]) and self._repeat_stationary_retreat_warning(
                pre_trigger, status, post_status, actions,
            ):
                replan = False
            elif reasons == ["pre_hurt_melee"] and self._resolved_melee_warning(pre_trigger, status, post_status, actions):
                replan = False
            elif reasons == ["pre_hurt_ranged"] and self._unchanged_ranged_warning(pre_trigger, status, post_status, actions):
                replan = False
        if reasons == ["in_lava"] and post_events_verified and not fresh:
            if self._repeat_stalled_lava_escape(status, post_status, actions):
                replan = False
        else:
            self._last_stalled_lava = None
        return self._incident(post_status, reasons, actions, fresh, requires_replan=replan)

    def _only_stationary_melee_threat(self, target_id: int | None, target_pos: list[float]) -> bool:
        try:
            result = self.bot.call(
                "get_entities", {"radius": 8, "types": ["hostile"]}, timeout_s=0.3, allow_reauth=False,
            )
        except (ApiError, httpx.HTTPError):
            return False
        entities = result.get("entities") if isinstance(result, dict) else None
        if target_id is None or not isinstance(entities, list) or len(entities) != 1:
            return False
        name, distance, observed_id, observed_pos = self._extract_hostile_info({"nearest_hostile": entities[0]})
        return (
            observed_id == target_id and name in PRE_HURT_MELEE_MOBS and 2.3 < distance <= 3.5
            and self._same_observed_melee_position(target_pos, observed_pos)
        )

    @staticmethod
    def _same_observed_melee_position(start: Any, end: Any) -> bool:
        return all(
            isinstance(pos, (list, tuple)) and len(pos) >= 3
            and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in pos[:3])
            for pos in (start, end)
        ) and math.dist(start[:3], end[:3]) <= 0.25

    def _same_stationary_melee_timeout_state(self, before: dict[str, Any], after: dict[str, Any]) -> bool:
        health = after.get("health")
        if (
            not isinstance(health, (int, float)) or not 6.0 < health <= 20.0
            or str(after.get("dimension") or "").removeprefix("minecraft:") != "overworld"
            or not self._same_observed_melee_position(before.get("pos"), after.get("pos"))
            or any(before.get(key) != after.get(key) for key in (
                "dimension", "last_death", "health", "food", "held", "offhand", "blocking", "sneaking", "armor_points",
            ))
        ):
            return False
        if any(
            status.get("ready") is not True or status.get("alive") is not True or status.get("on_ground") is not True
            or any(status.get(key) is not False for key in ("in_water", "submerged", "in_lava", "on_fire"))
            or any(key not in status for key in ("busy", "held_controls", "held", "offhand", "armor_points"))
            or not isinstance(status.get("blocking"), bool) or status.get("busy") or status.get("held_controls")
            for status in (before, after)
        ):
            return False
        old_name, old_dist, old_id, _ = self._extract_hostile_info(before)
        name, dist, target_id, _ = self._extract_hostile_info(after)
        return (
            target_id is not None and target_id == old_id and name == old_name and name in PRE_HURT_MELEE_MOBS
            and 2.3 < old_dist <= 3.5 and 2.3 < dist <= 3.5 and abs(dist - old_dist) <= 0.25
        )

    def _repeat_stalled_lava_escape(
        self, before: dict[str, Any], after: dict[str, Any], actions: list[dict[str, Any]],
    ) -> bool:
        attempt = actions[0] if len(actions) == 1 else {}
        result = attempt.get("result") or {}
        start, end = before.get("pos"), after.get("pos")
        health = after.get("health")
        keys = ("dimension", "last_death", "health", "on_fire")
        state = tuple(after.get(key) for key in keys)

        def same_rim(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> bool:
            return abs(a[1] - b[1]) <= 0.9 and math.hypot(a[0] - b[0], a[2] - b[2]) <= 0.35

        if (
            attempt.get("action") != "escape_lava" or attempt.get("ok") or result.get("respawned")
            or result.get("reason") not in {None, "lava_landing_unconfirmed"}
            or not (after.get("in_lava") or (after.get("on_ground") is False and result.get("airborne_above_lava")))
            or after.get("ready") is not True or after.get("alive") is not True or after.get("busy")
            or not isinstance(health, (int, float)) or health < 16.0 or state != tuple(before.get(key) for key in keys)
            or not start or not end or not same_rim(start, end)
        ):
            self._last_stalled_lava = None
            return False
        previous = self._last_stalled_lava
        now = time.monotonic()
        if previous and previous[1] == state and now - previous[2] < 10.0 and same_rim(previous[0], end):
            return True
        self._last_stalled_lava = (tuple(end[:3]), state, now)
        return False

    def _resolved_melee_warning(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any], actions: list[dict[str, Any]],
    ) -> bool:
        if len(actions) != 1 or actions[0]["action"] != "subcortical_combat" or not actions[0].get("ok"):
            return False
        outcome = actions[0].get("result")
        if not isinstance(outcome, dict):
            return False
        hits = outcome.get("actions")
        target = trigger.get("primary_entity_id")
        if outcome.get("mode") == "sealed_shaft":
            return bool(
                not outcome.get("latched_guard") and isinstance(hits, list) and len(hits) == 1
                and self._unchanged_melee_seal(before, after, target, hits[0])
            )
        if outcome.get("mode") != "counter_killed":
            return False
        if isinstance(hits, list) and hits and isinstance(hits[0], dict) and hits[0].get("action") == "raise_shield_entry":
            # The slot releases temporary posture before poll samples the final state.
            shield_before, shield_after = before.get("offhand"), after.get("offhand")
            if (
                hits[0].get("ok") is not True or outcome.get("latched_guard")
                or not isinstance(shield_before, dict) or not isinstance(shield_after, dict)
                or (shield_before.get("name"), shield_before.get("slot"))
                != (shield_after.get("name"), shield_after.get("slot"))
            ):
                return False
            hits = hits[1:]
        if (
            target is None or not isinstance(hits, list) or not hits
            or not all(
                isinstance(a, dict) and a.get("action") == "melee_attack" and a.get("ok") and a.get("entity_id") == target
                for a in hits
            )
            or hits[-1].get("killed") is not True
        ):
            return False
        _, distance, remaining, _ = self._extract_hostile_info(after)
        health = after.get("health")
        pos_before, pos_after = before.get("pos"), after.get("pos")
        held_before, held_after = before.get("held"), after.get("held")
        return bool(
            "nearest_hostile" in after and remaining != target and distance > 16.0
            and after.get("ready") is True and after.get("alive") is True
            and before.get("on_ground") is True and after.get("on_ground") is True
            and not before.get("busy") and not after.get("busy")
            and not any(after.get(key) for key in ("in_water", "in_lava", "on_fire"))
            and isinstance(health, (int, float)) and health > LOW_HP_BUNKER_MAX and health == before.get("health")
            and all(after.get(key) == before.get(key) for key in ("dimension", "last_death", "food", "blocking"))
            and isinstance(held_before, dict) and isinstance(held_after, dict)
            and (held_after.get("name"), held_after.get("slot")) == (held_before.get("name"), held_before.get("slot"))
            and pos_before and pos_after and math.dist(pos_before[:3], pos_after[:3]) <= 0.2
        )

    def _unchanged_melee_seal(self, before: dict[str, Any], after: dict[str, Any], target: Any, seal: Any) -> bool:
        if not (
            isinstance(seal, dict) and seal.get("action") == "seal_shaft" and seal.get("ok") is True
            and seal.get("reason") == "already_sealed" and seal.get("placed") == seal.get("placement_attempts") == 0
        ):
            return False
        stable = ("health", "food", "held", "offhand", "blocking", "sneaking", "armor_points", "effects", "yaw")
        health = after.get("health")
        _, old_dist, old_id, _ = self._extract_hostile_info(before)
        _, distance, entity_id, _ = self._extract_hostile_info(after)
        nearest = after.get("nearest_hostile")
        return (
            isinstance(health, (int, float)) and 16.0 <= health <= 20.0
            and before.get("ready") is True and before.get("alive") is True
            and before.get("on_ground") is True and self.combat_env._on_same_footing(before, after)
            and before.get("pos") == after.get("pos")
            and all(key in before and key in after and before[key] == after[key] for key in stable)
            and all(
                "held_controls" in state and not state["held_controls"] and not state.get("busy")
                and all(state.get(key) is False for key in ("submerged", "in_water", "in_lava", "on_fire"))
                for state in (before, after)
            )
            and isinstance(nearest, dict) and nearest.get("blocked_by_wall") is not False
            and target is not None and entity_id == old_id == target and distance >= old_dist - 0.25
        )

    def _unchanged_ranged_warning(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any], actions: list[dict[str, Any]],
    ) -> bool:
        if len(actions) != 1 or actions[0].get("action") != "subcortical_combat":
            return False
        outcome = actions[0].get("result") or {}
        steps = outcome.get("actions")
        if outcome.get("mode") != "cover_or_flee" or not isinstance(steps, list) or len(steps) != 1:
            return False
        retreat = steps[0]
        if (
            retreat.get("action") != "flee" or retreat.get("ok") is not False
            or retreat.get("reason") != "no_safe_retreat" or retreat.get("moved") != 0.0
        ):
            return False
        mob, distance, entity_id, _ = self._extract_hostile_info(after)
        start, end = before.get("pos"), after.get("pos")
        previous_health, health = before.get("health"), after.get("health")
        return bool(
            str(after.get("dimension") or "").removeprefix("minecraft:") == "overworld"
            and mob in {"skeleton", "stray", "bogged", "pillager"} and entity_id is not None
            and entity_id == trigger.get("primary_entity_id")
            and min(distance, trigger["distance"]) >= 10.0
            and after.get("ready") is True and after.get("alive") is True
            and before.get("on_ground") is True and after.get("on_ground") is True
            and not before.get("busy") and not after.get("busy")
            and not any(after.get(key) for key in ("in_water", "in_lava", "on_fire"))
            and isinstance(previous_health, (int, float)) and isinstance(health, (int, float))
            and health >= previous_health >= 16.0
            and all(after.get(key) == before.get(key) for key in (
                "last_death", "dimension", "food", "held", "offhand", "blocking",
            ))
            and start and end and math.dist(start[:3], end[:3]) <= 0.2
        )

    def _observe_stationary_zombie(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any],
    ) -> tuple[tuple[float, ...], tuple[Any, ...]] | None:
        """Check a rounded 2.3-metre warning against live coordinates and unchanged bot state."""
        state_keys = (
            "pos", "dimension", "health", "food", "held", "offhand", "armor_points", "effects",
            "blocking", "sneaking", "yaw", "busy", "held_controls", "ready", "alive", "on_ground",
            "in_water", "in_lava", "submerged", "on_fire",
        )
        health = after.get("health")
        # The status API omits last_death until the bot has died at least once.
        last_death = before.get("last_death")
        if (
            not isinstance(health, (int, float)) or not math.isfinite(health) or health < 16.0
            or after.get("last_death") != last_death
            or any(k not in before or k not in after or before[k] != after[k] for k in state_keys)
            or after.get("held_controls") or after.get("submerged") is not False
            or after.get("in_water") is not False or after.get("in_lava") is not False or after.get("on_fire") is not False
        ):
            return None
        try:
            entities = self.bot.call(
                "get_entities", {"radius": 8, "types": ["hostile"]}, timeout_s=0.35, allow_reauth=False,
            ).get("entities")
            if not isinstance(entities, list) or len(entities) != 1:
                return None
            target = entities[0]
            name, distance, entity_id, point = self._extract_hostile_info({"nearest_hostile": target})
            first_point = trigger.get("primary_pos")
            if (
                name != "zombie" or entity_id != trigger.get("primary_entity_id") or distance != 2.3
                or target.get("alive") is False or target.get("blocked_by_wall")
                or not self._same_observed_melee_position(point, first_point)
                or tuple(point[:3]) != tuple(first_point[:3]) or round(math.dist(after["pos"][:3], point[:3]), 6) < 2.3
            ):
                return None
            current = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            return None
        nearest = current.get("nearest_hostile")
        current_name, current_distance, current_id, current_point = self._extract_hostile_info(current)
        if (
            not isinstance(nearest, dict) or (current_name, current_distance, current_id) != ("zombie", 2.3, entity_id)
            or (current_point is not None and current_point[:3] != point[:3])
            or current.get("last_death") != last_death
            or nearest.get("blocked_by_wall") or any(k not in current or current[k] != after[k] for k in state_keys)
        ):
            return None
        return tuple(point[:3]), deepcopy(tuple(after[k] for k in state_keys))

    def _repeat_stationary_retreat_warning(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any], actions: list[dict[str, Any]],
    ) -> bool:
        """Keep a generation running when a repeated threat leaves the retreat state unchanged."""
        source = trigger["source"]
        melee_warning = source == "pre_hurt_melee"
        if len(actions) != 1 or actions[0]["action"] != "subcortical_combat":
            return False
        outcome = actions[0].get("result")
        combat_actions = outcome.get("actions") if isinstance(outcome, dict) else None
        if not isinstance(combat_actions, list) or len(combat_actions) != 1:
            return False
        retreat = combat_actions[0]
        expected_action = "flee" if melee_warning else "retreat_from_creeper"
        if (
            not isinstance(retreat, dict) or retreat.get("action") != expected_action
            or retreat.get("reason") != "no_safe_retreat"
            or (melee_warning and (retreat.get("moved") != 0.0 or retreat.get("ok") is not False))
        ):
            return False

        name, distance, entity_id, _ = self._extract_hostile_info(after)
        pos_before, pos_after = before.get("pos"), after.get("pos")
        if (
            entity_id is None or entity_id != trigger.get("primary_entity_id")
            or after.get("ready") is not True or after.get("alive") is not True or after.get("on_ground") is not True
            or before.get("busy") or after.get("busy") or after.get("in_water") or after.get("in_lava")
            or after.get("health") != before.get("health") or after.get("blocking") != before.get("blocking")
            or after.get("dimension") != before.get("dimension") or after.get("last_death") != before.get("last_death")
            or not pos_before or not pos_after or math.dist(pos_before[:3], pos_after[:3]) > 0.2
            or (melee_warning and (
                name not in PRE_HURT_MELEE_MOBS or before.get("on_ground") is not True or after.get("on_fire")
                or any(after.get(key) != before.get(key) for key in ("food", "held", "offhand"))
            ))
        ):
            return False

        warning_distance = trigger.get("distance")
        closest = min(distance, warning_distance) if isinstance(warning_distance, (int, float)) else distance
        # Compare creeper readings taken after each failed retreat. An earlier reading can
        # still flag immediate danger, but is taken before the bot and target have settled.
        if melee_warning:
            distance = closest
        previous = self._stationary_warnings.get(source)
        now = time.monotonic()
        life = (after.get("dimension"), after.get("last_death"))
        close_melee_observation = None
        if melee_warning and name == "zombie" and closest == warning_distance == 2.3:
            close_melee_observation = self._observe_stationary_zombie(trigger, before, after)
        if (
            previous is not None and previous.entity_id == entity_id and now - previous.observed_at < 45.0
            and previous.life == life and math.dist(pos_after[:3], previous.pos) <= 0.25
        ):
            same_close_melee = (
                close_melee_observation is not None and previous.close_melee_observation == close_melee_observation
            )
            if same_close_melee:
                self.events.emit(
                    "guardian.stationary_melee_verified", entity_id=entity_id, pos=pos_after,
                    target_pos=close_melee_observation[0], distance=closest,
                )
            if same_close_melee or (closest > 2.3 and distance >= previous.distance - 0.25):
                # Refresh the observation while comparing position and distance to the initial warning.
                if melee_warning:
                    self._stationary_warnings[source] = replace(
                        previous, observed_at=now, close_melee_observation=close_melee_observation,
                    )
                return True
        self._stationary_warnings[source] = _StationaryWarning(
            entity_id, tuple(pos_after[:3]), distance, now, life, close_melee_observation,
        )
        return False

    # ---- Layer 2: Active Macro-Tool & `pre_ensure` Preemption ----

    def _verify_unstick_exit(self, body: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        if status.get("in_water"):
            return body
        pos = status.get("pos")
        if (
            not pos or status.get("ready") is False or status.get("alive") is False
            or status.get("on_ground") is False or status.get("in_lava")
        ):
            raise _UnsafeUnstickExit
        self.combat_env.invalidate_block_cache()
        yaw = float(body["yaw"])
        ms = min(int(body.get("ms") or 350), 350)
        route = self.combat_env.probe_retreat(pos, yaw, 2.2)
        if route is None:
            foot_y = math.floor(float(pos[1]) + 0.05)
            if (
                status.get("on_ground") is not True or any(body.get(key) for key in ("back", "left", "right"))
                or self.combat_env.probe_retreat(pos, yaw, 1.0) != (foot_y, False)
            ):
                raise _UnsafeUnstickExit
            # A short walking pulse can stop on the supported space just ahead.
            ms = min(ms, 150)
            route = (foot_y, False)
            self.events.emit("guardian.unstick_exit_shortened", pos=pos, yaw=yaw, ms=ms)
        _, needs_jump = route
        return {**body, "sprint": False, "jump": needs_jump, "ms": ms}

    def _restore_unstick_shield_facing(self, before: dict[str, Any], call: Callable[..., dict[str, Any]]) -> None:
        name, distance, entity_id, _ = self._extract_hostile_info(before)
        health = before.get("health")
        if not (
            before.get("blocking") and not before.get("in_water") and name in RANGED_MOBS
            and entity_id is not None and distance <= 16.0
            and isinstance(health, (int, float)) and 0 < health <= LOW_HP_BUNKER_MAX
        ):
            return
        try:
            after = call("status", None, method="GET", timeout_s=0.6, allow_reauth=False)
            if (
                after.get("ready") is False or after.get("alive") is False or not after.get("blocking")
                or after.get("in_lava") or after.get("in_water")
                or after.get("last_death") != before.get("last_death")
                or after.get("dimension") != before.get("dimension")
            ):
                return
            self._action(
                "face_attacker_after_retreat",
                lambda: call("look", {"entity_id": entity_id}, timeout_s=0.9, allow_reauth=False),
            )
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
            pass

    def _verify_flee_step(
        self, action: str, body: dict[str, Any], *, deadline: float | None = None,
    ) -> dict[str, Any]:
        status = self.bot.status()
        if status.get("in_water") and status.get("alive") is not False and status.get("ready") is not False:
            return body
        pos = status.get("pos")
        if (
            not pos or status.get("ready") is False or status.get("alive") is False
            or status.get("in_lava") or status.get("on_ground") is False
        ):
            raise _UnsafeFleeExit

        probe_deadline = time.monotonic() + 2.0
        if deadline is not None:
            probe_deadline = min(probe_deadline, deadline)
        probe_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=probe_deadline)
        probe_env = CombatEnv(probe_bot, geofence=self.geofence)

        def probe(yaw: float, distance: float) -> tuple[int, bool]:
            try:
                route = probe_env.probe_retreat(pos, yaw, distance)
            except _RecoveryBudgetExceeded:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                route = None
            if route is None:
                raise _UnsafeFleeExit
            return route

        if action == "control":
            _, needs_jump = probe(float(body["yaw"]), 3.5)
            return {**body, "jump": needs_jump, "ms": min(int(body.get("ms") or 450), 450)}

        x, _, z = map(float, pos[:3])
        dx, dz = float(body["x"]) - x, float(body["z"]) - z
        desired_distance = math.hypot(dx, dz)
        if desired_distance == 0:
            return body
        distance = min(6.0, desired_distance)
        yaw = math.atan2(-dx, -dz)
        landing_y, _ = probe(yaw, distance)
        fraction = distance / desired_distance
        return {**body, "x": x + dx * fraction, "y": landing_y, "z": z + dz * fraction, "range": 1.0}

    def _retreat_after_unsafe_exit(self, deadline: float | None, *, allow_cover: bool = False) -> dict[str, Any] | None:
        now = time.monotonic()
        if deadline is None or now >= deadline:
            return None
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=min(now + 4.0, deadline))
        try:
            status = bot.status()
            if (
                status.get("ready") is False or status.get("alive") is False
                or status.get("in_water") or status.get("in_lava")
            ):
                return None
            name, distance, entity_id, threat_pos = self._extract_hostile_info(status)
            ranged = name in RANGED_MOBS
            if not name or distance > (16.0 if ranged else 6.0):
                return None
            health = status.get("health")
            dimension = str(status.get("dimension") or "").lower().removeprefix("minecraft:")
            recent_nether_cover = (
                "nether" in dimension
                and now < self._ranged_cover_active_until
                and now - max(self._last_ranged_hurt_at, self._last_melee_hurt_at) >= 4.0
            )
            critical_overworld_arrows = (
                dimension == "overworld" and status.get("blocking")
                and isinstance(health, (int, float)) and 0 < health <= 4.0
                and now - self._last_ranged_hurt_at < 8.0 and now - self._last_melee_hurt_at >= 4.0
            )
            other_shooters = []
            if (
                allow_cover and ranged and status.get("on_ground") is True and status.get("pos")
                and (recent_nether_cover or critical_overworld_arrows)
            ):
                cover_budget = 2.25 if critical_overworld_arrows else 1.25
                cover, live_target, other_shooters = self._cover_and_shooters_after_blocked_flee(
                    status["pos"], entity_id, min(now + cover_budget, deadline),
                    allow_stationary=not (critical_overworld_arrows or self._low_health_burning_after_ranged_hit(status, now)),
                    require_melee_clearance=critical_overworld_arrows,
                )
                if cover is not None:
                    return {**cover, "requires_replan": True, "pos_before": status["pos"]}
                threat_pos = live_target or threat_pos
            if threat_pos is None:
                threat_pos = self._find_hostile_position(entity_id, name, session=bot)
            env = CombatEnv(bot, geofence=self.geofence)
            recovery = self._action(
                "unsafe_exit_retreat",
                lambda: env.flee(
                    threat_pos, distance=8.0, allow_shaft_seal=False,
                    wait_for_ground=ranged or name in PRE_HURT_MELEE_MOBS,
                    **({"keep_blast_clearance": True} if name == "creeper" else {}),
                    **({"other_shooters": other_shooters} if other_shooters else {}),
                ),
                raise_on_http_error=False,
            )
            result = recovery.get("result")
            if isinstance(result, dict) and (
                result.get("respawned") or (result.get("reason") == "unsafe_landing" and result.get("pos_after"))
            ):
                return {**result, "requires_replan": True, "pos_before": status.get("pos")}
            if (
                allow_cover and isinstance(result, dict) and result.get("reason") == "no_safe_retreat"
                and result.get("pos_after") and status.get("on_ground") is True
            ):
                self._blocked_flee_site = (
                    tuple(result["pos_after"][:3]), (status.get("dimension"), status.get("last_death")), time.monotonic(),
                )
            if recovery.get("ok") and isinstance(result, dict):
                if name == "creeper":
                    return {
                        **self._confirm_creeper_exit(bot, status, result),
                        "requires_replan": True, "pos_before": status.get("pos"),
                    }
                cover_deadline = min(now + 4.0, deadline)
                if (
                    allow_cover and ranged and dimension == "overworld"
                    and (not status.get("blocking") or critical_overworld_arrows)
                    and isinstance(health, (int, float)) and 0 < health <= 10.0
                    and time.monotonic() + 1.5 < cover_deadline
                ):
                    try:
                        after = bot.call("status", method="GET", timeout_s=0.3, allow_reauth=False)
                    except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
                        after = {}
                    if after.get("alive") is False or any(
                        after.get(key) is not None and after[key] != status.get(key)
                        for key in ("last_death", "dimension")
                    ):
                        return {
                            "ok": False, "mode": "respawned", "respawned": True, "requires_replan": True,
                            "moved_dist": 0.0, "pos_before": status.get("pos"), "pos_after": after.get("pos"),
                        }
                    shooter, shooter_dist, shooter_id, _ = self._extract_hostile_info(after)
                    hp = after.get("health")
                    if (
                        shooter in RANGED_MOBS and shooter_id is not None
                        and shooter_dist <= (16.0 if critical_overworld_arrows else 8.0)
                        and isinstance(hp, (int, float)) and 0 < hp <= (4.0 if critical_overworld_arrows else 6.0)
                        and after.get("pos") and bool(after.get("blocking")) == bool(status.get("blocking"))
                        and after.get("ready") is True and after.get("alive") is not False and after.get("on_ground") is True
                        and not any(after.get(key) for key in ("in_water", "in_lava", "on_fire", "busy"))
                        and all(after.get(key) == status.get(key) for key in ("last_death", "dimension"))
                    ):
                        cover, _, _ = self._cover_and_shooters_after_blocked_flee(
                            after["pos"], shooter_id, min(cover_deadline, time.monotonic() + 2.25), allow_stationary=False,
                            require_melee_clearance=critical_overworld_arrows,
                        )
                        if cover is not None:
                            return {**cover, "requires_replan": True, "pos_before": status.get("pos")}
                if (
                    ranged and entity_id is not None and status.get("blocking")
                    and isinstance(health, (int, float)) and health <= LOW_HP_BUNKER_MAX
                ):
                    try:
                        self._action(
                            "face_attacker_after_retreat",
                            lambda: bot.call("look", {"entity_id": entity_id}, timeout_s=0.9),
                        )
                    except _RecoveryBudgetExceeded:
                        # The confirmed retreat remains valid if its remaining time cannot fit a look.
                        pass
                return {**result, "requires_replan": True, "pos_before": status.get("pos")}
            return None
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return None
        except RunTimedOut:
            self._stop_after_recovery_timeout()
            raise
        except (ApiError, httpx.HTTPError):
            return None

    def _hold_blaze_cover(self, before: dict[str, Any], blaze_id: int | None) -> dict[str, Any]:
        """Verify existing cover without moving while taking residual fire damage."""
        failed = {"ok": False, "mode": "natural_cover", "reason": "blaze_cover_unconfirmed"}
        changed_life = {**failed, "reason": "life_changed", "respawned": True}
        deadline = time.monotonic() + 1.25
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        env = CombatEnv(bot, geofence=self.geofence)
        other_ranged = (RANGED_MOBS - {"blaze"}) | {"drowned", "ghast", "warden", "wither"}

        def life_changed(current: dict[str, Any]) -> bool:
            return current.get("alive") is False or any(
                current.get(key) != before.get(key) for key in ("dimension", "last_death")
            )

        def safe_position(current: dict[str, Any]) -> Sequence[float] | None:
            pos = current.get("pos")
            if (
                current.get("ready") is True and current.get("alive") is True and current.get("on_ground") is True
                and not any(current.get(key) for key in ("busy", "in_water", "in_lava", "submerged"))
                and isinstance(pos, (list, tuple)) and len(pos) >= 3
                and all(type(coord) in (int, float) and math.isfinite(coord) for coord in pos[:3])
            ):
                return pos
            return None

        try:
            first = bot.call("status", method="GET", timeout_s=0.3, allow_reauth=False)
            if life_changed(first):
                return changed_life
            pos = safe_position(first)
            if pos is None:
                return failed
            entities = bot.call(
                "get_entities", {"radius": 16, "types": ["hostile"]}, timeout_s=0.4, allow_reauth=False,
            ).get("entities")
            if not isinstance(entities, list) or not all(isinstance(entity, dict) for entity in entities):
                return failed
            if not melee_clear_for_projectile_cover(entities):
                return failed
            blazes = []
            for entity in entities:
                name = str(entity.get("name") or "").strip().lower().removeprefix("minecraft:")
                if not name or name in other_ranged:
                    return failed
                if name == "blaze":
                    blazes.append(entity)
            shooters = ranged_retreat_positions(pos, blazes, max_distance=16.0)
            if not shooters or len(shooters) != len(blazes):
                return failed
            if blaze_id is not None and not any(entity_id == blaze_id for entity_id, _ in shooters):
                return failed
            if not env.has_safe_footing(pos, require_observation=True) or not all(
                env._blocks_shot_across_hitbox(pos, shooter, deadline=deadline) for _, shooter in shooters
            ):
                return failed
            after = bot.call("status", method="GET", timeout_s=0.3, allow_reauth=False)
            if life_changed(after):
                return changed_life
            settled = safe_position(after)
            if settled is None or math.dist(settled[:3], pos[:3]) > 0.18 or footing_blocks(settled) != footing_blocks(pos):
                return failed
            nearest = after.get("nearest_hostile")
            if isinstance(nearest, dict):
                name = str(nearest.get("name") or "").strip().lower().removeprefix("minecraft:")
                if name in other_ranged or not melee_clear_for_projectile_cover([nearest]):
                    return failed
                if name == "blaze" and not any(entity_id == nearest.get("id") for entity_id, _ in shooters):
                    return failed
            return {"ok": True, "mode": "natural_cover", "pos_after": settled, "moved_dist": 0.0}
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            return failed

    def _confirm_creeper_exit(
        self, bot: _GuardSession, before: dict[str, Any], retreat: dict[str, Any],
    ) -> dict[str, Any]:
        unconfirmed = {
            **retreat, "ok": False, "reason": "blast_clearance_unconfirmed",
            "error": {"code": "blast_clearance_unconfirmed", "message": "已移动，但尚未确认离开苦力怕爆炸范围。"},
        }
        try:
            after = bot.call("status", method="GET", timeout_s=0.3, allow_reauth=False)
            if after.get("alive") is False or any(
                after.get(key) is not None and after[key] != before.get(key) for key in ("dimension", "last_death")
            ):
                return {
                    **retreat, "ok": False, "mode": "respawned", "respawned": True,
                    "moved_dist": 0.0, "pos_after": after.get("pos"),
                }
            if (
                not self._same_life(before, after) or not after.get("pos") or after.get("on_ground") is not True
                or after.get("in_water") or after.get("in_lava")
            ):
                return unconfirmed
            sighting = bot.call("get_entities", {"radius": 8, "types": ["hostile"]}, timeout_s=0.35, allow_reauth=False)
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
            return unconfirmed

        entities = sighting.get("entities") if isinstance(sighting, dict) else None
        if not isinstance(entities, list):
            return unconfirmed
        distances = []
        nearest = after.get("nearest_hostile")
        for entity in [*entities, *([nearest] if isinstance(nearest, dict) else [])]:
            name = entity.get("name") if isinstance(entity, dict) else None
            if not isinstance(name, str) or not name:
                return unconfirmed
            if name.removeprefix("minecraft:") != "creeper" or entity.get("alive") is False:
                continue
            distance = entity.get("dist")
            if type(distance) not in (int, float) or not math.isfinite(distance) or distance < 0:
                return unconfirmed
            distances.append(distance)
        closest = min(distances, default=math.inf)
        if closest <= 5.5:
            return {
                **retreat, "ok": False, "reason": "creeper_still_close", "creeper_distance": closest,
                "error": {"code": "creeper_still_close", "message": "已移动，但苦力怕仍在爆炸范围内，请继续撤离。"},
            }
        return retreat

    def _cover_and_shooters_after_blocked_flee(
        self, pos: list[float], target_id: int | None, deadline: float, *, allow_stationary: bool,
        require_melee_clearance: bool = False,
    ) -> tuple[dict[str, Any] | None, Sequence[float] | None, list[Sequence[float]]]:
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        target = None
        others = []
        try:
            sighting = bot.call("get_entities", {"radius": 16, "types": ["hostile"]}, timeout_s=0.6)
            entities = sighting.get("entities") or []
            shooters = ranged_retreat_positions(pos, entities, max_distance=16.0)
            target = next((point for entity_id, point in shooters if target_id is not None and entity_id == target_id), None)
            others = [point for entity_id, point in shooters if entity_id != target_id]
            if target is None or (require_melee_clearance and not melee_clear_for_projectile_cover(entities)):
                return None, target, others
            cover = self._action(
                "unsafe_exit_cover",
                lambda: CombatEnv(bot, geofence=self.geofence).seek_natural_cover(
                    target, other_shooters=others, allow_stationary=allow_stationary,
                ),
            )
            cover_result = cover.get("result")
            if isinstance(cover_result, dict):
                landing = cover_result.get("pos_after")
                if (
                    cover_result.get("ok") or cover_result.get("respawned")
                    or (landing and math.dist(landing[:3], pos[:3]) > 0.2)
                ):
                    # Even an unconfirmed landing invalidates the earlier retreat position.
                    return cover_result, target, others
            return None, target, others
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
        except (ApiError, httpx.HTTPError):
            pass
        return None, target, others

    def _place_arrow_cover(
        self, landing: dict[str, Any], counts: dict[str, int], attacker_id: int | None,
    ) -> dict[str, Any]:
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 2.1)

        def live_shooter(pos: Sequence[float], expected_id: int | None) -> tuple[int, Sequence[float]] | None:
            sighting = bot.call("get_entities", {"radius": 16, "types": ["hostile"]}, timeout_s=0.4)
            entities = sighting.get("entities")
            if not isinstance(entities, list) or not melee_clear_for_projectile_cover(entities):
                return None
            shooters = ranged_retreat_positions(pos, entities, max_distance=16.0)
            if len(shooters) != 1 or (expected_id is not None and shooters[0][0] != expected_id):
                return None
            shooter_id, shooter_pos = shooters[0]
            if isinstance(shooter_id, int) and any(
                isinstance(entity, dict) and entity.get("id") == shooter_id
                and str(entity.get("name") or "").removeprefix("minecraft:") in {"skeleton", "stray", "bogged", "pillager"}
                for entity in entities
            ):
                return shooter_id, shooter_pos
            return None

        try:
            current = bot.call("status", method="GET", timeout_s=0.35)
            pos = current.get("pos")
            if (
                not pos or current.get("ready") is not True or current.get("alive") is not True
                or current.get("on_ground") is not True
                or any(current.get(key) for key in ("busy", "in_water", "in_lava", "on_fire"))
                or any(current.get(key) != landing.get(key) for key in ("dimension", "last_death"))
            ):
                return {"ok": False, "reason": "landing_changed"}
            shooter = live_shooter(pos, attacker_id)
            if shooter is None:
                return {"ok": False, "reason": "projectile_lane_unconfirmed"}
            env = CombatEnv(bot, geofence=self.geofence)
            result = env.place_arrow_cover(current, shooter[1], counts)
            if result.get("ok"):
                settled = result["pos_after"]
                shooter_after = live_shooter(settled, shooter[0])
                env.invalidate_block_cache()
                if shooter_after is None or not env._blocks_shot_across_hitbox(settled, shooter_after[1]):
                    return {**result, "ok": False, "reason": "projectile_lane_unconfirmed"}
            return result
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return {"ok": False, "reason": "time_budget"}

    def _verify_combat_step(self, body: dict[str, Any]) -> dict[str, Any]:
        probe_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 0.6)
        try:
            status = probe_bot.status()
            if "nether" not in str(status.get("dimension") or "").lower():
                return body
            if status.get("in_lava"):
                self._stop_after_recovery_timeout()
                raise ReflexPreempted({"source": "boundary_threat", "primary_type": "lava"})
            pos, yaw = status.get("pos"), body.get("yaw", status.get("yaw"))
            if (
                not pos or not isinstance(yaw, (int, float)) or not math.isfinite(yaw)
                or status.get("ready") is False or status.get("alive") is False or status.get("on_ground") is not True
            ):
                raise _UnsafeCombatStep
            # Cover a preceding movement pulse still in flight as well as the next pulse.
            distance = 3.0 if body.get("sprint") else 2.25
            route = CombatEnv(probe_bot, geofence=self.geofence).probe_retreat(pos, yaw, distance)
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
            raise _UnsafeCombatStep from None
        if route is None:
            raise _UnsafeCombatStep
        return {**body, "ms": min(body["ms"], 250)}

    def _verify_attack_approach(self, entity_id: Any, call: Callable[..., dict[str, Any]]) -> None:
        if not isinstance(entity_id, int):
            return
        deadline = time.monotonic() + 0.8
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline, call_override=call)
        near_failed_retreat = False
        try:
            status = bot.status()
            if status.get("ready") is False or status.get("alive") is False:
                return
            name, distance, nearest_id, target = self._extract_hostile_info(status)
            pos = status.get("pos")
            if pos and self._blocked_flee_site is not None:
                blocked_pos, life, blocked_at = self._blocked_flee_site
                near_failed_retreat = (
                    time.monotonic() - blocked_at < 10.0
                    and life == (status.get("dimension"), status.get("last_death"))
                    and math.dist(pos[:3], blocked_pos) <= 2.5
                )
            if nearest_id == entity_id and name == "creeper":
                raise _UnsafeCreeperAttack(entity_id)
            if nearest_id != entity_id or not name:
                sighting = bot.call("get_entities", {"radius": 64, "types": ["creeper"]}, timeout_s=0.3)
                if any(
                    isinstance(entity, dict) and entity.get("id") == entity_id
                    and str(entity.get("name") or "").removeprefix("minecraft:") == "creeper"
                    for entity in sighting.get("entities") or []
                ):
                    raise _UnsafeCreeperAttack(entity_id)
            if near_failed_retreat and not status.get("in_water"):
                if status.get("on_ground") is not True or status.get("in_lava"):
                    raise _UnsafeCombatStep
                if nearest_id != entity_id or target is None:
                    target = self._find_hostile_position(entity_id, "", session=bot, timeout_s=0.3)
                if target is None:
                    raise _UnsafeCombatStep
                travel = math.hypot(target[0] - pos[0], target[2] - pos[2])
                if travel < 0.9 and abs(target[1] - pos[1]) > 2.0:
                    raise _UnsafeCombatStep("cliff")
                if travel >= 0.9:
                    yaw, _ = aim_yaw_pitch(pos, target)
                    route = CombatEnv(bot, geofence=self.geofence).probe_ray(
                        pos, yaw, dist=min(travel, 6.0), deadline=deadline,
                    )
                    if not route["safe"]:
                        raise _UnsafeCombatStep(route["hazard"])
            if nearest_id != entity_id or not pos or status.get("on_ground") is not True or status.get("in_lava"):
                return
            dimension = str(status.get("dimension") or "").lower().removeprefix("minecraft:")
            health = status.get("health")
            critical_shore_chase = (
                dimension == "overworld" and name in RANGED_MOBS and 3.5 < distance <= 13.5
                and isinstance(health, (int, float)) and 0 < health <= 4.0 and not status.get("in_water")
                and time.monotonic() - self._last_water_escape_at < self.WATER_ESCAPE_RETRY_WINDOW_S
            )
            nearby_nether_shooter = "nether" in dimension and name in {"blaze", "ghast"} and distance <= 3.5
            if not (critical_shore_chase or nearby_nether_shooter):
                return
            if target is None:
                target = self._find_hostile_position(entity_id, name, session=bot, timeout_s=0.3)
            if target is None:
                return
            travel = math.hypot(target[0] - pos[0], target[2] - pos[2])
            yaw, _ = aim_yaw_pitch(pos, target)
            env = CombatEnv(bot, geofence=self.geofence)
            if critical_shore_chase and travel > 3.5:
                route = env.probe_ray(pos, yaw, dist=min(travel, 8.5), deadline=deadline)
                if route["hazard"] == "water":
                    raise _UnsafeCombatStep("water")
            elif nearby_nether_shooter and 0.35 <= travel <= 3.5:
                if env.probe_retreat(pos, yaw, travel, deadline=deadline) is None:
                    raise _UnsafeCombatStep
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            if near_failed_retreat and not status.get("in_water"):
                raise _UnsafeCombatStep from None
            return

    @contextmanager
    def _intercept_bot_calls(
        self,
        *,
        dig_progress: dict[str, Any] | None = None,
        last_work_action: dict[str, Any] | None = None,
        enable_creeper_watcher: bool = True,
        deadline: float | None = None,
        check_unstick_exit: bool = False,
        watch_hot_floor: bool = False,
        watch_scout_water: bool = False,
        watch_mining_water: bool = False,
        watch_move_water: bool = False,
        check_flee_exit: bool = False,
        watch_attack_emergency: bool = False,
        check_combat_step: bool = False,
        check_support_dig: bool = False,
        check_mining_landing: bool = False,
        check_gathering_reentry: bool = False,
        check_food_use: bool = False,
    ):
        """Intercept threats during actor macros, or watch immediate survival hazards during an attack."""
        orig_call = getattr(self.raw_bot, "call", None)
        if not callable(orig_call) or self._in_reflex:
            yield {}
            return

        actor_call = orig_call
        if deadline is not None:
            actor_call = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline, call_override=orig_call).call

        unstick_failure: _UnstickTransportInterrupted | None = None
        if check_unstick_exit:
            call_unstick = actor_call

            def track_unstick_call(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
                nonlocal unstick_failure
                try:
                    return call_unstick(path, body, **kwargs)
                except httpx.TransportError as error:
                    endpoint = path.lstrip("/")
                    action_pending = endpoint in ACTION_LOCK_ENDPOINTS and str(kwargs.get("method") or "").upper() != "GET"
                    unstick_failure = _UnstickTransportInterrupted(error, endpoint, action_pending)
                    raise

            actor_call = track_unstick_call

        stop_evt = threading.Event()
        attack_active = threading.Event()
        attack_state_lock = threading.Lock()
        attack_target_id: int | None = None
        actor_thread_id = threading.get_ident()
        pending_trigger: list[ThreatTrigger] = []
        unstick_start: dict[str, Any] = {}
        mining_landing_error: _MiningLandingDenied | None = None
        mining_landing_retry_at = 0.0
        mining_support_changed = False
        mining_forward_waypoint: list[float] | None = None
        blocked_collar_approaches: set[tuple[float, float]] = set()
        watch_terrain_damage = check_support_dig or watch_mining_water
        watch_low_oxygen = watch_mining_water or check_unstick_exit
        drowned_threat_range = None
        if watch_move_water:
            drowned_threat_range = self.MOVE_DROWNED_THREAT_RANGE
        elif watch_scout_water or watch_mining_water:
            drowned_threat_range = 6.0

        def terrain_damage_trigger(status: dict[str, Any], call: Callable[..., dict[str, Any]]) -> ThreatTrigger | None:
            if (
                not watch_terrain_damage or status.get("ready") is False or status.get("alive") is False
                or status.get("in_water") or status.get("in_lava") or not status.get("pos")
            ):
                return None
            event = self._fresh_tool_hurt_event(call)
            source = str((event or {}).get("source") or "").strip().lower()
            if watch_mining_water and source in LAVA_HURT_SOURCES:
                return {
                    "source": "boundary_threat", "primary_type": "lava", "primary_pos": status["pos"],
                    "primary_entity_id": None, "distance": 0.0, "hurt_source": source,
                    "observed_life": (status.get("dimension"), status.get("last_death")),
                }
            if source not in IN_WALL_HURT_SOURCES:
                return None
            return {
                "source": "tool_interrupted_hurt", "primary_type": "in_wall", "primary_pos": status["pos"],
                "primary_entity_id": None, "distance": 0.0, "hurt_source": source,
                "observed_life": (status.get("dimension"), status.get("last_death")),
            }

        def terrain_hit_near_distant_melee(name: str, distance: float, call: Callable[..., dict[str, Any]]) -> bool:
            if distance <= 4.5 or (name != "creeper" and name not in PRE_HURT_MELEE_MOBS):
                return False
            event = self._fresh_tool_hurt_event(call) or {}
            return str(event.get("source") or "").strip().lower() in {"fall", "stalagmite", "falling_stalactite"}

        def interrupted_action_trigger(max_enemy_distance: float, *, terrain_only: bool) -> ThreatTrigger | None:
            try:
                status = actor_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
            except (ApiError, httpx.HTTPError):
                return None
            if not isinstance(status, dict):
                return None
            if status.get("in_lava") and (watch_mining_water or not terrain_only):
                return {
                    "source": "boundary_threat", "primary_entity_id": None, "primary_type": "lava",
                    "primary_pos": None, "distance": 0.0,
                }
            terrain = terrain_damage_trigger(status, actor_call)
            if terrain is not None or terrain_only:
                return terrain
            name, dist, entity_id, pos = self._extract_hostile_info(status)
            if name and dist <= max_enemy_distance and not terrain_hit_near_distant_melee(name, dist, actor_call):
                return {
                    "source": "tool_interrupted_hurt", "primary_entity_id": entity_id, "primary_type": name,
                    "primary_pos": pos, "distance": dist,
                    "observed_life": (status.get("dimension"), status.get("last_death")),
                }
            return None

        def confirmed_wall_cover(status: dict[str, Any], threats: list[dict[str, Any]]) -> bool:
            origin = status.get("pos")
            terrain = _GuardSession(
                self.raw_bot, lambda: None, deadline=time.monotonic() + 0.35, call_override=orig_call,
            )
            blocks: dict[tuple[int, int, int], str] = {}
            for threat in threats:
                if threat.get("blocked_by_wall") is True:
                    continue
                target = threat.get("pos")
                if not all(
                    isinstance(point, (list, tuple)) and len(point) >= 3
                    and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3])
                    for point in (origin, target)
                ):
                    return False
                try:
                    if ray_wall_blocker(terrain, tuple(origin[:3]), tuple(target[:3]), block_cache=blocks) is None:
                        return False
                except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded):
                    return False
            return True

        def stop_watched_action(trigger: ThreatTrigger) -> None:
            try:
                result = orig_call("stop", {}, allow_reauth=False, timeout_s=1.5)
            except Exception:
                return
            released = self._reported_action_release(result)
            if released is not None:
                trigger["action_released"] = released

        def _watcher() -> None:
            # Delay 0.30s before first poll so fast (<5ms) offline mock calls never incur background HTTP reads
            if stop_evt.wait(0.30):
                return
            watch_hp0: float | None = None
            combat_life: tuple[Any, Any] | None = None
            last_terrain_health: float | None = None
            previous_water_health: tuple[tuple[Any, Any], float] | None = None
            submersion_start: tuple[tuple[Any, Any], float] | None = None
            was_sinking = False
            previous_tile = checked_hot_floor = None
            while not stop_evt.is_set():
                try:
                    if watch_attack_emergency and not attack_active.is_set():
                        if stop_evt.wait(0.30):
                            return
                        continue
                    st = orig_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
                    if not isinstance(st, dict):
                        submersion_start, was_sinking = None, False
                        if stop_evt.wait(0.30):
                            return
                        continue
                    cur_hp = st.get("health")
                    observed_life = (st.get("dimension"), st.get("last_death"))
                    available = st.get("ready") is not False and st.get("alive") is not False
                    if check_combat_step and (observed_life != combat_life or not available):
                        watch_hp0 = None
                        combat_life = observed_life if available else None
                    if watch_hp0 is None and isinstance(cur_hp, (int, float)):
                        watch_hp0 = float(cur_hp)
                    nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(st)
                    in_lava_now = bool(st.get("in_lava"))
                    if watch_attack_emergency:
                        lava_danger = in_lava_now and st.get("ready") is not False and st.get("alive") is not False
                        water_danger = (
                            st.get("in_water")
                            and st.get("ready") is not False
                            and st.get("alive") is not False
                            and (st.get("submerged") or (
                                nh_dist <= self.ATTACK_WATER_THREAT_RANGE
                                and (nh_name == "drowned" or self._swimming_at_low_health(st))
                            ))
                        )
                        held = st.get("held")
                        held_name = str(held.get("name") or "").removeprefix("minecraft:") if isinstance(held, dict) else ""
                        unarmed = not st.get("blocking") and held_name not in MELEE_WEAPON_ORDER
                        retreat_hp = 14.0 if unarmed else 10.0
                        losing_health_on_land = (
                            not st.get("in_water")
                            and st.get("ready") is not False and st.get("alive") is not False
                            and isinstance(cur_hp, (int, float)) and 0 < cur_hp
                            and watch_hp0 is not None and watch_hp0 - cur_hp >= 4.0
                        )
                        with attack_state_lock:
                            other_attacker = nh_id is not None and attack_target_id is not None and nh_id != attack_target_id
                            melee_danger = (
                                losing_health_on_land and cur_hp <= retreat_hp and nh_dist <= 4.0
                                and nh_name in PRE_HURT_MELEE_MOBS and (unarmed or other_attacker)
                            )
                            ranged_danger = (
                                losing_health_on_land and cur_hp <= 10.0 and nh_dist <= 4.0
                                and nh_name in RANGED_MOBS and other_attacker
                            )
                            stop_attack = (
                                (lava_danger or water_danger or melee_danger or ranged_danger)
                                and attack_active.is_set() and not stop_evt.is_set()
                            )
                            if stop_attack:
                                pending_trigger.append({
                                    "source": "boundary_threat" if lava_danger or water_danger else "tool_interrupted_hurt",
                                    "primary_entity_id": None if lava_danger else nh_id,
                                    "primary_type": "lava" if lava_danger else nh_name,
                                    "primary_pos": None if lava_danger else nh_pos,
                                    "distance": 0.0 if lava_danger else nh_dist,
                                })
                        if stop_attack:
                            stop_watched_action(pending_trigger[0])
                            return
                        if stop_evt.wait(0.30):
                            return
                        continue
                    terrain = None
                    if watch_terrain_damage and isinstance(cur_hp, (int, float)):
                        if last_terrain_health is not None and cur_hp < last_terrain_health:
                            terrain = terrain_damage_trigger(st, orig_call)
                        last_terrain_health = float(cur_hp)
                    if terrain is not None and not stop_evt.is_set():
                        pending_trigger.append(terrain)
                        stop_watched_action(terrain)
                        return
                    hp_dropped = (
                        isinstance(watch_hp0, (int, float))
                        and isinstance(cur_hp, (int, float))
                        and float(cur_hp) <= float(watch_hp0) - 1.0
                        and bool(nh_name)
                        and nh_dist <= 16.0
                    )
                    if hp_dropped and not in_lava_now and not st.get("in_water") and terrain_hit_near_distant_melee(
                        nh_name, nh_dist, orig_call,
                    ):
                        hp_dropped = False
                        watch_hp0 = float(cur_hp)
                    fireball_source = None
                    if (
                        check_combat_step and not hp_dropped and st.get("ready") is True and available
                        and not in_lava_now and not st.get("in_water")
                        and str(st.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
                        and watch_hp0 is not None and isinstance(cur_hp, (int, float))
                        and 0 < cur_hp <= 10.0 and watch_hp0 - cur_hp >= 4.0
                    ):
                        # A ranged fight can keep polling outside the ordinary 16-block hurt radius.
                        hit = self._fresh_tool_hurt_event(orig_call, ignore_burn_ticks=True) or {}
                        source = str(hit.get("source") or "").strip().lower()
                        if source in {"fireball", "small_fireball"}:
                            hp_dropped = True
                            fireball_source = source
                            shooter_name, shooter_dist, shooter_id, shooter_pos = self._extract_hostile_info(
                                {"nearest_hostile": hit.get("source_entity")},
                            )
                            if shooter_name in {"blaze", "ghast"} and shooter_id is not None:
                                nh_name, nh_dist, nh_id, nh_pos = shooter_name, shooter_dist, shooter_id, shooter_pos
                            elif nh_name not in {"blaze", "ghast"}:
                                nh_name, nh_dist, nh_id, nh_pos = "hostile", 999.0, None, None
                    melee_ambush = (
                        nh_name in PRE_HURT_MELEE_MOBS
                        and nh_dist <= 3.0
                        and not self._is_pre_hurt_debounced(nh_id, nh_name)
                    )
                    creeper_close = (
                        nh_name == "creeper"
                        and nh_dist <= 4.5
                        and not self._is_pre_hurt_debounced(nh_id, nh_name)
                    )
                    water_hp_dropped = False
                    submerged_for_s = None
                    sustained_sinking = False
                    submerged = st.get("submerged")
                    if watch_low_oxygen:
                        if available and isinstance(cur_hp, (int, float)) and cur_hp > 0:
                            water_hp_dropped = (
                                previous_water_health is not None and previous_water_health[0] == observed_life
                                and cur_hp < previous_water_health[1]
                            )
                            previous_water_health = (observed_life, float(cur_hp))
                        else:
                            previous_water_health = None
                        if available and st.get("in_water") and submerged is True:
                            now_water = time.monotonic()
                            if submersion_start is None or submersion_start[0] != observed_life:
                                submersion_start, was_sinking = (observed_life, now_water), False
                            submerged_for_s = now_water - submersion_start[1]
                            velocity = st.get("velocity")
                            sinking = (
                                isinstance(velocity, (list, tuple)) and len(velocity) >= 2
                                and isinstance(velocity[1], (int, float)) and velocity[1] <= -1.0
                            )
                            sustained_sinking = was_sinking and sinking
                            was_sinking = sinking
                        else:
                            submersion_start, was_sinking = None, False
                    oxygen = st.get("oxygen")
                    numeric_oxygen = type(oxygen) in (int, float) and math.isfinite(oxygen)
                    water_risk = None
                    if available and st.get("in_water"):
                        if drowned_threat_range is not None and nh_name == "drowned" and nh_dist <= drowned_threat_range and (
                            st.get("on_ground") is False or st.get("submerged") is True
                            or (watch_mining_water and not st["nearest_hostile"].get("blocked_by_wall"))
                        ):
                            water_risk = "drowned"
                        elif water_hp_dropped:
                            water_risk = "hurt"
                        elif (
                            watch_low_oxygen and submerged is not False and numeric_oxygen
                            and oxygen <= self.TOOL_WATER_ESCAPE_BUBBLES
                        ):
                            water_risk = "low_oxygen"
                        elif (
                            watch_low_oxygen and submerged is None and numeric_oxygen
                            and self.STATUS_FULL_OXYGEN < oxygen <= self.TOOL_LEGACY_OXYGEN_HINT_MAX
                        ):
                            # Preserve support for legacy status payloads with no head immersion field.
                            water_risk = "legacy_oxygen_hint"
                        elif submerged_for_s is not None:
                            if isinstance(cur_hp, (int, float)) and 0 < cur_hp <= 6.0:
                                water_risk = "critical_health"
                            elif sustained_sinking:
                                water_risk = "sustained_sinking"
                            elif submerged_for_s >= self.TOOL_WATER_SUBMERSION_GRACE_S:
                                water_risk = "sustained_submersion"
                    check_creeper_cover = creeper_close and nh_dist > 2.3
                    if (
                        (check_creeper_cover or melee_ambush) and isinstance(nh_id, int)
                        and st.get("on_ground") is True and not st.get("in_water")
                        and not in_lava_now and not hp_dropped
                    ):
                        try:
                            sighting = orig_call(
                                "get_entities", {"radius": 6, "types": ["hostile"]},
                                timeout_s=0.3, allow_reauth=False,
                            )
                        except (ApiError, httpx.HTTPError):
                            sighting = {}
                        entities = (sighting.get("entities") or []) if isinstance(sighting, dict) else []
                        threat_names = {"creeper"}
                        if melee_ambush:
                            threat_names.update(PRE_HURT_MELEE_MOBS)
                        nearby_threats = [
                            entity for entity in entities
                            if isinstance(entity, dict) and (
                                entity.get("id") == nh_id
                                or str(entity.get("name") or "").removeprefix("minecraft:") in threat_names
                            )
                        ]
                        if (
                            any(entity.get("id") == nh_id for entity in nearby_threats)
                            and confirmed_wall_cover(st, nearby_threats)
                        ):
                            if check_creeper_cover:
                                creeper_close = False
                            else:
                                melee_ambush = False
                    hot_floor_now = False
                    if watch_hot_floor and not (in_lava_now or water_risk or creeper_close or melee_ambush or hp_dropped):
                        tile = self._hot_floor_block(st)
                        if (
                            tile is not None and tile == previous_tile and tile != checked_hot_floor
                            and "nether" in tile[0].lower() and st.get("ready") is not False and st.get("alive") is not False
                            and not st.get("in_water")
                            and isinstance(cur_hp, (int, float)) and cur_hp < 20
                            and self._needs_hot_floor_stance(st, time.monotonic())
                        ):
                            checked_hot_floor = tile
                            _, x, y, z = tile
                            try:
                                floor = orig_call("get_block", {"pos": [x, y - 1, z]}, timeout_s=0.8, allow_reauth=False)
                                hot_floor_now = str(floor.get("name") or "").removeprefix("minecraft:") == "magma_block"
                            except (ApiError, httpx.HTTPError):
                                pass
                        previous_tile = tile
                    if stop_evt.is_set():
                        return
                    if in_lava_now or water_risk or creeper_close or melee_ambush or hp_dropped or hot_floor_now:
                        if in_lava_now:
                            src_w, eid_w, tp_w, pos_w, dist_w = "boundary_threat", None, "lava", None, 0.0
                        elif water_risk:
                            src_w, eid_w, tp_w, pos_w, dist_w = "tool_water_threat", nh_id, nh_name, nh_pos, nh_dist
                        elif creeper_close:
                            src_w, eid_w, tp_w, pos_w, dist_w = "pre_hurt_creeper", nh_id, "creeper", nh_pos, nh_dist
                        elif hp_dropped:
                            src_w, eid_w, tp_w, pos_w, dist_w = "tool_interrupted_hurt", nh_id, (nh_name or "hostile"), nh_pos, nh_dist
                        elif hot_floor_now:
                            src_w, eid_w, tp_w, pos_w, dist_w = "tool_interrupted_hurt", None, "hot_floor", st.get("pos"), 0.0
                        else:
                            src_w, eid_w, tp_w, pos_w, dist_w = "pre_hurt_melee", nh_id, nh_name, nh_pos, nh_dist
                        pending_trigger.append(
                            {
                                "source": src_w,
                                "primary_entity_id": eid_w,
                                "primary_type": tp_w,
                                "primary_pos": pos_w,
                                "distance": dist_w,
                                **({"observed_life": observed_life}
                                   if src_w in {"tool_water_threat", "tool_interrupted_hurt"} else {}),
                                **({"hurt_source": fireball_source} if fireball_source and src_w == "tool_interrupted_hurt" else {}),
                                **({"water_observation": {
                                    "reason": water_risk,
                                    "submerged_for_s": round(submerged_for_s, 3) if submerged_for_s is not None else None,
                                    **{key: st.get(key) for key in (
                                        "pos", "health", "oxygen", "in_water", "submerged", "on_ground", "nearest_hostile",
                                        "ready", "alive", "dimension", "last_death", "velocity",
                                    )},
                                }} if src_w == "tool_water_threat" else {}),
                            }
                        )
                        stop_watched_action(pending_trigger[0])
                        return
                except Exception:
                    submersion_start, was_sinking = None, False
                if stop_evt.wait(0.30):
                    return

        watcher_thread: threading.Thread | None = None
        if enable_creeper_watcher or watch_attack_emergency:
            watcher_thread = threading.Thread(target=_watcher, name="mc-creeper-watch", daemon=True)
            watcher_thread.start()

        def verify_mining_approach(target: list[float]) -> None:
            nonlocal mining_landing_error, mining_landing_retry_at, mining_support_changed
            xz = target[0], target[2]
            wet_site = self._mining_water_reentry_site
            cached_denial = mining_landing_error
            if (
                cached_denial is None or cached_denial.code != "unsafe_landing"
                or cached_denial.context.get("collar_approach") or xz in blocked_collar_approaches
                or mining_support_changed or time.monotonic() >= mining_landing_retry_at
                or (wet_site is not None and tuple(map(math.floor, target)) == wet_site[0])
                or footing_blocks(cached_denial.context["target"]) != footing_blocks(target)
            ):
                cached_denial = None
            mining_support_changed = False
            try:
                if cached_denial is not None:
                    raise _MiningLandingDenied(
                        cached_denial.code, cached_denial.message, context={**cached_denial.context, "target": target},
                    )
                if xz in blocked_collar_approaches:
                    self._verify_mining_collar_approach(target)
                    blocked_collar_approaches.discard(xz)
                self._verify_mining_landing(target)
            except _MiningLandingDenied as error:
                if pending_trigger:
                    raise ReflexPreempted(pending_trigger[0]) from None
                mining_landing_error = error
                if cached_denial is None:
                    mining_landing_retry_at = time.monotonic() + self.MINING_UNSAFE_FOOTING_RECHECK_S
                self.events.emit(
                    "guardian.mining_landing_blocked", tool="mc_skill_mine_dfs", target=target, reason=error.code,
                    **{key: error.context[key] for key in ("hazard", "collar_approach", "water_reentry_site") if key in error.context},
                    **({"probe_reused": True} if cached_denial is not None else {}), **(dig_progress or {}),
                )
                raise
            if pending_trigger:
                raise ReflexPreempted(pending_trigger[0])
            mining_landing_error = None

        def _wrapped_call(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            nonlocal attack_target_id, mining_support_changed, mining_forward_waypoint
            clean_path = str(path or "").lstrip("/")
            is_actor_thread = threading.get_ident() == actor_thread_id
            is_readonly_poll = (
                str(kwargs.get("method") or "").upper() == "GET"
                or clean_path in READ_ONLY_POLL_ENDPOINTS
            )
            if not is_actor_thread:
                return orig_call(path, body, **kwargs)
            if watch_attack_emergency:
                if clean_path != "attack":
                    return actor_call(path, body, **kwargs)
                entity_id = body.get("entity_id") if isinstance(body, dict) else None
                self._verify_attack_approach(entity_id, orig_call)
                with attack_state_lock:
                    attack_target_id = entity_id if isinstance(entity_id, int) else None
                    attack_active.set()
                try:
                    try:
                        result = actor_call(path, body, **kwargs)
                    finally:
                        with attack_state_lock:
                            attack_active.clear()
                            attack_target_id = None
                except ApiError as exc:
                    if exc.code == "interrupted" and pending_trigger:
                        raise ReflexPreempted(pending_trigger[0]) from None
                    raise
                if pending_trigger:
                    raise ReflexPreempted(pending_trigger[0])
                return result
            if check_flee_exit:
                if isinstance(body, dict) and body.get("stop_on") == ["death"] and (
                    (clean_path == "control" and body.get("forward") and isinstance(body.get("yaw"), (int, float)))
                    or (clean_path == "move_to" and "y" not in body and all(k in body for k in ("x", "z")))
                ):
                    body = self._verify_flee_step(clean_path, body, deadline=deadline)
                if clean_path == "move_to" and isinstance(body, dict) and deadline is not None:
                    remaining_ms = math.floor((deadline - time.monotonic()) * 1000)
                    if remaining_ms <= 0:
                        raise _RecoveryBudgetExceeded
                    timeout_ms = body.get("timeout")
                    if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
                        remaining_ms = min(remaining_ms, int(timeout_ms))
                    body = {**body, "timeout": remaining_ms}
                return actor_call(path, body, **kwargs)
            if pending_trigger and not self._in_reflex and (check_combat_step or not is_readonly_poll):
                raise ReflexPreempted(pending_trigger[0])
            if is_readonly_poll:
                try:
                    observed = actor_call(path, body, **kwargs)
                except (ApiError, httpx.HTTPError):
                    if check_combat_step and pending_trigger and not self._in_reflex:
                        raise ReflexPreempted(pending_trigger[0]) from None
                    raise
                if check_combat_step and pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0])
                if check_unstick_exit and clean_path == "status" and not unstick_start and isinstance(observed, dict):
                    unstick_start.update(observed)
                return observed
            if check_mining_landing and isinstance(body, dict):
                if clean_path == "move_to":
                    mining_forward_waypoint = None
                    target = [body.get(axis) for axis in ("x", "y", "z")]
                    x, y, z = target
                    if all(type(coord) in (int, float) and math.isfinite(coord) for coord in (x, z)):
                        if body.get("range") in (0.3, 0.35) and type(y) in (int, float) and math.isfinite(y):
                            verify_mining_approach(target)
                            mining_forward_waypoint = target
                        elif y is None and body.get("range") in (1.0, 1.2, 1.4):
                            try:
                                self._verify_mining_collar_approach(target)
                            except _MiningLandingDenied as error:
                                if pending_trigger:
                                    raise ReflexPreempted(pending_trigger[0]) from None
                                blocked_collar_approaches.add((x, z))
                                self.events.emit("guardian.mining_collar_blocked", reason=error.code, **error.context)
                                raise
                            blocked_collar_approaches.discard((x, z))
                            if pending_trigger:
                                raise ReflexPreempted(pending_trigger[0])
                elif clean_path == "control" and body.get("forward"):
                    if mining_landing_error is not None:
                        # Keep a rejected waypoint blocked until a repair is verified.
                        if not mining_support_changed:
                            raise _MiningLandingDenied(
                                mining_landing_error.code, mining_landing_error.message, context=mining_landing_error.context,
                            )
                        mining_forward_waypoint = mining_landing_error.context["target"]
                        verify_mining_approach(mining_forward_waypoint)
                    if mining_forward_waypoint is not None:
                        hazard = self._mining_fallback_hazard(body)
                        if pending_trigger:
                            raise ReflexPreempted(pending_trigger[0])
                        if hazard:
                            context = {"target": mining_forward_waypoint, "hazard": hazard}
                            self.events.emit("guardian.mining_fallback_blocked", **context)
                            raise _MiningLandingDenied(
                                "unsafe_landing", "短跑可能越过采矿落点进入危险地形，请改用有支撑的路线。", context=context,
                            )
            if (
                check_combat_step and clean_path == "control" and isinstance(body, dict)
                and body.get("forward") and body.get("wait") is False
                and not any(body.get(key) for key in ("back", "left", "right", "jump"))
                and isinstance(body.get("ms"), int) and body["ms"] > 0
            ):
                try:
                    body = self._verify_combat_step(body)
                except _UnsafeCombatStep:
                    if pending_trigger:
                        raise ReflexPreempted(pending_trigger[0]) from None
                    raise
                if pending_trigger:
                    raise ReflexPreempted(pending_trigger[0])
            if (
                check_food_use and clean_path == "use_item" and isinstance(body, dict) and not body.get("target")
                and str(body.get("item") or "").removeprefix("minecraft:") in SAFE_FOOD_PRIORITY
            ):
                try:
                    state = actor_call("status", method="GET", timeout_s=0.6, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    state = {}
                if (
                    state and state.get("ready") is not False and state.get("alive") is not False
                    and not self._can_auto_eat_safely(state)
                ):
                    raise _UnsafeFoodUse
            if (check_support_dig or check_unstick_exit) and clean_path == "dig" and isinstance(body, dict):
                dig_hazard = self._dig_hazard(body.get("pos"), check_support=check_support_dig, check_roof=check_unstick_exit)
                if pending_trigger:
                    raise ReflexPreempted(pending_trigger[0])
                if dig_hazard:
                    raise _UnsafeDig(dig_hazard, list(body["pos"]))
            unstick_before = None
            if (
                check_unstick_exit and clean_path == "control" and isinstance(body, dict)
                and body.get("forward") and body.get("stop_on") == [] and isinstance(body.get("yaw"), (int, float))
            ):
                unstick_before = self.bot.status()
                body = self._verify_unstick_exit(body, unstick_before)
                if pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0])
            # Do not preempt explicit stop_on=['death'] emergency swim/trap digs
            caller_ignores_hurt = isinstance(body, dict) and body.get("stop_on") == ["death"]

            def apply_pending_preemption() -> None:
                if self._in_reflex or not pending_trigger:
                    return
                trigger = pending_trigger[0]
                moving = clean_path == "move_to" or (
                    (watch_hot_floor or watch_scout_water)
                    and clean_path == "control" and isinstance(body, dict) and body.get("forward")
                )
                movement_emergency = moving and (
                    trigger.get("primary_type") == "hot_floor"
                    or trigger.get("source") in {"pre_hurt_creeper", "tool_water_threat"}
                )
                water_emergency = watch_low_oxygen and trigger.get("source") == "tool_water_threat"
                if (
                    not caller_ignores_hurt or movement_emergency or water_emergency
                    or trigger.get("primary_type") in {"in_wall", "lava"}
                ):
                    raise ReflexPreempted(trigger) from None

            if last_work_action is not None and clean_path in {"dig", "move_to"} and isinstance(body, dict):
                target = body.get("pos") if clean_path == "dig" else [body.get(axis) for axis in ("x", "y", "z")]
                if (
                    isinstance(target, (list, tuple)) and len(target) == 3
                    and all(
                        (clean_path == "move_to" and axis == 1 and coord is None)
                        or type(coord) is int or (type(coord) is float and math.isfinite(coord))
                        for axis, coord in enumerate(target)
                    )
                ):
                    if check_gathering_reentry and clean_path == "dig":
                        threat_id = self._gathering_reentry_enemy(target, actor_call)
                        apply_pending_preemption()
                        if threat_id is not None:
                            context = {"target": list(target), "entity_id": threat_id}
                            self.events.emit("guardian.mining_melee_reentry_blocked", **context)
                            raise ApiError(
                                "mining_melee_reentry", "同一只近战怪仍在刚才中断的作业点附近，请换一处采集。", context=context,
                            )
                    last_work_action.update(endpoint=clean_path, target=list(target))

            try:
                res = actor_call(path, body, **kwargs)
            except _RecoveryBudgetExceeded:
                if pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0]) from None
                raise
            except ApiError as err:
                apply_pending_preemption()
                if not self._in_reflex and (not caller_ignores_hurt or watch_terrain_damage):
                    if err.code == "interrupted" and clean_path in ACTION_LOCK_ENDPOINTS:
                        trigger = interrupted_action_trigger(14.0, terrain_only=caller_ignores_hurt)
                        if trigger is not None:
                            raise ReflexPreempted(trigger) from None
                raise

            if dig_progress is not None and clean_path == "dig" and isinstance(res, dict) and res.get("ok") is not False:
                # Record acknowledged effects before a pending stop can unwind the actor tool.
                dug = res.get("dug")
                if isinstance(dug, str) and dug.removeprefix("minecraft:") not in {"", "air", "cave_air", "void_air"}:
                    dig_progress["dug_count"] = dig_progress.get("dug_count", 0) + 1
                gained = res.get("gained")
                if isinstance(gained, dict):
                    for name, amount in gained.items():
                        if type(amount) is int and amount > 0:
                            confirmed_gains = dig_progress.setdefault("gained", {})
                            confirmed_gains[name] = confirmed_gains.get(name, 0) + amount
            apply_pending_preemption()
            if (
                clean_path in {"place", "dig"} and isinstance(body, dict) and mining_landing_error is not None
                and isinstance(res, dict) and res.get("ok") is not False
            ):
                changed = body.get("pos")
                if isinstance(changed, (tuple, list)) and tuple(changed) in footing_blocks(mining_landing_error.context["target"]):
                    mining_support_changed = True
            if not self._in_reflex and (not caller_ignores_hurt or watch_terrain_damage):
                if isinstance(res, dict) and res.get("interrupted_by") == "hurt" and clean_path in ACTION_LOCK_ENDPOINTS:
                    trigger = interrupted_action_trigger(15.0, terrain_only=caller_ignores_hurt)
                    if trigger is not None:
                        raise ReflexPreempted(trigger)
            if unstick_before is not None:
                self._restore_unstick_shield_facing(unstick_before, actor_call)
            return res

        try:
            self.raw_bot.call = _wrapped_call
            yield unstick_start
        except (RuntimeError, httpx.TransportError) as error:
            cause = error if isinstance(error, httpx.TransportError) else error.__cause__ or error.__context__
            if unstick_failure is not None and cause is unstick_failure.transport_error:
                if pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0]) from None
                raise unstick_failure from None
            raise
        finally:
            stop_evt.set()
            self.raw_bot.call = orig_call
            if watcher_thread is not None:
                # Finish a dispatched /stop before issuing a recovery mutation.
                watcher_thread.join(timeout=1.55 if pending_trigger else 0.05)
        if check_combat_step and pending_trigger:
            raise ReflexPreempted(pending_trigger[0])

    def _fresh_tool_hurt_event(
        self, call: Callable[..., dict[str, Any]], *, ignore_burn_ticks: bool = False,
    ) -> dict[str, Any] | None:
        since = self.cursor
        try:
            page = call(
                "events", method="GET", params={"since": since, "limit": 100}, timeout_s=0.4, allow_reauth=False,
            )
        except (ApiError, httpx.HTTPError):
            return None
        if not isinstance(page, dict):
            return None
        for event in reversed(page.get("events") or []):
            if not isinstance(event, dict) or not isinstance(event.get("seq"), int) or event["seq"] <= since:
                continue
            if event.get("type") in {"death", "death_loop", "disconnected", "spawn"}:
                return None
            if event.get("type") != "hurt":
                continue
            timestamp = event.get("t")
            if isinstance(timestamp, (int, float)) and time.time() - timestamp / 1000 > 3.0:
                return None
            if ignore_burn_ticks and str(event.get("source") or "").strip().lower() in FIRE_HURT_SOURCES:
                continue
            return event
        return None

    def handle_preemption(
        self, trigger: ThreatTrigger, *, interrupted_name: str, last_work_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute full 0-LLM subcortical combat/evasion takeover on the main thread (no tool auto-replay)."""
        self._in_reflex = True
        try:
            self.stats["tool_preemptions"] += 1
            try:
                st_pre = self.bot.status()
            except (ApiError, httpx.HTTPError):
                st_pre = {}

            def life_changed(after: dict[str, Any]) -> bool:
                return bool(
                    after.get("ready") is False or after.get("alive") is False
                    or (after.get("last_death") is not None and after["last_death"] != st_pre.get("last_death"))
                    or (after.get("dimension") and st_pre.get("dimension") and after["dimension"] != st_pre["dimension"])
                )

            observed_life = trigger.get("observed_life")
            life_unavailable = st_pre.get("ready") is False or st_pre.get("alive") is False or (
                observed_life is not None and observed_life != (st_pre.get("dimension"), st_pre.get("last_death"))
            )
            tool_water_threat = trigger.get("source") == "tool_water_threat"
            shore_jump_settled = False
            fall_start_pos = None
            falling_summary = f"执行 {interrupted_name} 途中快速下坠，尚未确认落点，请待落地后重新规划"
            if tool_water_threat and not life_unavailable and st_pre.get("in_water") is False:
                settled = self._settle_dry_landing(st_pre, st_pre, self.bot, deadline=time.monotonic() + 0.7)
                life_unavailable = life_changed(settled) or (
                    observed_life is not None
                    and observed_life != (settled.get("dimension"), settled.get("last_death"))
                )
                st_pre = settled
            elif not life_unavailable and trigger.get("primary_type") != "lava" and self._nether_fast_fall(st_pre):
                fall_start_pos = st_pre["pos"]
                settled = self._settle_dry_landing(st_pre, st_pre, self.bot, deadline=time.monotonic() + 0.7)
                life_unavailable = life_changed(settled)
                st_pre = settled
            lava_threat = trigger.get("primary_type") == "lava" or bool(st_pre.get("in_lava"))
            distance = trigger.get("distance")
            hp_pre = st_pre.get("health")
            trigger_name = str(trigger.get("primary_type") or "").removeprefix("minecraft:")
            distant_creeper = trigger_name == "creeper" and isinstance(distance, (int, float)) and distance > 8.5
            missing_arrow_source = (
                trigger_name in RANGED_MOBS and isinstance(hp_pre, (int, float)) and 0 < hp_pre <= 4.0
                and not trigger.get("hurt_source")
            )
            if (
                not life_unavailable and not lava_threat and not st_pre.get("in_water")
                and trigger.get("source") == "tool_interrupted_hurt" and (distant_creeper or missing_arrow_source)
            ):
                hurt = self._fresh_tool_hurt_event(self.bot.call) or {}
                hurt_source = str(hurt.get("source") or "").strip().lower()
                arrow_hit = missing_arrow_source and hurt_source == "arrow"
                if arrow_hit or (distant_creeper and hurt_source in MELEE_HURT_SOURCES):
                    trigger = {**trigger, "hurt_source": hurt_source}
                    attacker = hurt.get("source_entity")
                    if not arrow_hit and not attacker:
                        attacker = hurt.get("likely_attacker")
                    attacker = attacker if isinstance(attacker, dict) else {}
                    attacker_name = str(attacker.get("name") or "").removeprefix("minecraft:")
                    if attacker_name and isinstance(attacker.get("id"), int) and (not arrow_hit or attacker_name in RANGED_MOBS):
                        same_attacker = attacker["id"] == trigger.get("primary_entity_id")
                        trigger.update(
                            primary_entity_id=attacker["id"], primary_type=attacker_name,
                            primary_pos=attacker.get("pos") or (trigger.get("primary_pos") if same_attacker else None),
                            distance=attacker.get("dist", trigger.get("distance") if same_attacker else None),
                        )
            hot_floor_trigger = trigger.get("primary_type") == "hot_floor"
            dimension = str(st_pre.get("dimension") or "").lower().removeprefix("minecraft:")
            check_hot_floor = hot_floor_trigger or (
                (
                    (interrupted_name in HOT_FLOOR_WATCH_TOOLS and "nether" in dimension)
                    or (interrupted_name == "pre_ensure" and dimension == "overworld")
                )
                and trigger.get("source") == "tool_interrupted_hurt"
                and isinstance(hp_pre, (int, float)) and 0 < hp_pre <= 4.0
                and self._needs_hot_floor_stance(st_pre, time.monotonic())
            )
            on_hot_floor = False
            if (
                check_hot_floor and not lava_threat and not life_unavailable
                and st_pre.get("on_ground") is True
                and not st_pre.get("in_water") and st_pre.get("pos")
            ):
                probe_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 0.6)
                try:
                    on_hot_floor = CombatEnv(probe_bot).is_on_hot_floor(st_pre["pos"])
                except _RecoveryBudgetExceeded:
                    pass
            if life_unavailable:
                st_after = st_pre
                summary = f"执行 {interrupted_name} 时角色已死亡或尚未就绪，等待角色就绪后重新规划"
                outcome = {"handled": False, "mode": "life_unavailable", "actions": []}
            elif lava_threat:
                lava_hurt = trigger.get("hurt_source") in LAVA_HURT_SOURCES
                if st_pre.get("in_lava") or lava_hurt:
                    self._last_lava_contact_at = time.monotonic()
                force_rim_escape = lava_hurt and not st_pre.get("in_lava")
                act = self._action(
                    "escape_lava",
                    lambda: self.combat_env.escape_lava(**({"force_rim_escape": True} if force_rim_escape else {})),
                    raise_on_http_error=False,
                )
                act_res = act.get("result") if isinstance(act.get("result"), dict) else {}
                try:
                    st_after = self.bot.status()
                except (ApiError, httpx.HTTPError):
                    st_after = st_pre
                landing_pending = act_res.get("reason") == "lava_landing_unconfirmed"
                if landing_pending:
                    st_after = self._settle_dry_landing(st_pre, st_after, self.bot, deadline=time.monotonic() + 0.7)
                lava_died = bool(act_res.get("respawned") or life_changed(st_after))
                esc_ok = bool(
                    not lava_died and not st_after.get("in_lava") and not act_res.get("in_lava")
                    and (
                        (act.get("ok") and (st_after.get("on_ground") is not False or st_after.get("in_water")))
                        or (landing_pending and st_after.get("on_ground") is True)
                    )
                )
                lava_actions = [act]
                if esc_ok:
                    douse = self._douse_after_lava(st_pre, after=st_after)
                    if douse is not None:
                        lava_actions.append(douse)
                        st_after = self.combat_env._safe_status(st_after)
                        lava_died = life_changed(st_after)
                        esc_ok = not lava_died and not st_after.get("in_lava")
                if lava_died:
                    summary = f"执行 {interrupted_name} 途中滑入岩浆，抢占逃生期间已死亡并重生（装备已掉落，先重整背包再继续）"
                elif esc_ok:
                    summary = f"执行 {interrupted_name} 途中滑入岩浆，已中断原工具并脱困，当前 HP={st_after.get('health')}"
                else:
                    summary = (
                        f"执行 {interrupted_name} 途中滑入岩浆，已中断原工具但尚未完全脱困"
                        f"（in_lava={st_after.get('in_lava', act_res.get('in_lava'))}"
                        f"，on_ground={st_after.get('on_ground')}），当前 HP={st_after.get('health')}"
                    )
                outcome = {
                    "handled": esc_ok,
                    "mode": "lava_escape",
                    "summary": summary,
                    "actions": lava_actions,
                    "status_after": st_after,
                    **({"respawned": True} if lava_died else {}),
                }
            elif (
                fall_start_pos is not None and st_pre.get("on_ground") is False
                and not (st_pre.get("in_water") or st_pre.get("submerged"))
            ):
                st_after = st_pre
                summary = falling_summary
                outcome = {"handled": False, "mode": "falling", "actions": []}
            elif trigger.get("primary_type") == "in_wall" and not st_pre.get("in_water"):
                unstick = self._action("unstick_in_wall", lambda: self._unstick_in_wall(st_pre))
                st_after = self.combat_env._safe_status(st_pre)
                life_unavailable = life_changed(st_after) or bool((unstick.get("result") or {}).get("respawned"))
                mode = "life_unavailable" if life_unavailable else "unstick_in_wall"
                summary = f"执行 {interrupted_name} 途中窒息，已中断动作并尝试脱困，当前 HP={st_after.get('health')}"
                outcome = {"handled": bool(unstick.get("ok")) and not life_unavailable, "mode": mode, "actions": [unstick]}
            elif hot_floor_trigger or on_hot_floor:
                actions = []
                st_after = st_pre
                if on_hot_floor:
                    actions.append(self._action("hot_floor_sneak", lambda: self._sneak_on_hot_floor(st_pre)))
                    self._hot_floor_retry_at = time.monotonic() + self.HOT_FLOOR_RETRY_S
                    try:
                        st_after = self.bot.status()
                    except (ApiError, httpx.HTTPError):
                        pass
                summary = f"执行 {interrupted_name} 途中踩到岩浆块，已中断当前动作，当前 HP={st_after.get('health')}"
                outcome = {"handled": bool(actions and actions[0].get("ok")), "mode": "hot_floor", "actions": actions}
            elif tool_water_threat and not st_pre.get("in_water"):
                st_after = st_pre
                landed = (
                    st_after.get("in_water") is False and not st_after.get("submerged")
                    and st_after.get("on_ground") is True
                )
                self._last_water_escape_at = time.monotonic()
                self._last_water_escape_failed = not landed
                if landed:
                    self._water_hurt_streak = 0
                recovery = "已脱离水域" if landed else "尚未确认安全登岸"
                summary = f"执行 {interrupted_name} 途中水下遇险，{recovery}，当前 HP={st_after.get('health')}"
                outcome = {"handled": landed, "mode": "water_surface", "actions": []}
            else:
                now_pre = time.monotonic()
                oxy_pre = st_pre.get("oxygen")
                hurt_pre_water = trigger.get("source") in {"tool_interrupted_hurt", "boundary_threat", "pre_hurt_ranged"}
                drowning_pre = (
                    (isinstance(oxy_pre, (int, float)) and oxy_pre <= 0) or trigger.get("hurt_source") == "drown"
                )
                mining_water_pre = interrupted_name in MINING_WATER_WATCH_TOOLS
                surface_first_pre = (
                    (mining_water_pre or interrupted_name == "mc_skill_unstick")
                    and tool_water_threat
                )
                underwater_danger = bool(st_pre.get("in_water")) and (
                    bool(st_pre.get("submerged"))
                    or water_oxygen_below(st_pre, 12)
                    or drowning_pre
                    or trigger.get("primary_type") == "drowned"
                    or tool_water_threat
                    or hurt_pre_water
                    or now_pre < self._no_water_until
                    or self._water_hurt_streak >= 2
                )
                if underwater_danger:
                    pre_actions: list[dict[str, Any]] = []
                    water_pos = st_pre.get("pos")
                    repeated_underground_water = False
                    surface_mining_water = mining_water_pre and surface_first_pre and water_pos and water_pos[1] >= 55
                    recent_banks: tuple[tuple[float, float, float], ...] = ()
                    repeated_surface_water = False
                    life = (st_pre.get("dimension"), st_pre.get("last_death"))
                    if surface_first_pre and water_pos and water_pos[1] < 55:
                        previous = self._last_underground_water_preemption
                        repeated_underground_water = (
                            previous is not None and previous[0] == life and now_pre - previous[1] < 60.0
                        )
                        self._last_underground_water_preemption = (life, now_pre)
                    elif surface_mining_water and self._last_surface_mining_water is not None:
                        previous_life, previous_at, previous_wet_pos, previous_banks = self._last_surface_mining_water
                        repeated_surface_water = (
                            previous_life == life and now_pre - previous_at < 60.0
                            and math.dist(water_pos[:3], previous_wet_pos) <= 5.0
                        )
                        if repeated_surface_water:
                            recent_banks = previous_banks
                    nh_pre_name, nh_pre_dist, _, _ = self._extract_hostile_info(st_pre)
                    drowned_during_tool = nh_pre_name == "drowned" and (
                        (interrupted_name == "mc_attack" and nh_pre_dist <= self.ATTACK_WATER_THREAT_RANGE)
                        or (interrupted_name == "mc_move_to" and nh_pre_dist <= self.MOVE_DROWNED_THREAT_RANGE)
                        or (interrupted_name == "mc_skill_scout_surface" and nh_pre_dist <= 6.0)
                        or (mining_water_pre and surface_first_pre and nh_pre_dist <= 6.0)
                    )
                    if hurt_pre_water:
                        self._water_hurt_streak += 1
                    urgent_water_melee = self._urgent_water_melee(st_pre, interrupted_source=trigger.get("hurt_source"))
                    forced_pre_shore = (
                        self._water_hurt_streak >= 3 or now_pre < self._no_water_until
                        or self._swimming_at_low_health(st_pre) or drowned_during_tool
                        or surface_first_pre or urgent_water_melee
                    )
                    if self._water_hurt_streak >= 3:
                        self._no_water_until = max(self._no_water_until, now_pre + 30.0)
                        self.events.emit("guardian.no_water_cooldown", duration_s=30.0, streak=self._water_hurt_streak)

                    try:
                        inv_pre = self.bot.inventory()
                    except (ApiError, httpx.HTTPError):
                        inv_pre = {}
                    _, has_shield_pre, _ = self._parse_inventory_summary(inv_pre)
                    att_pre = (
                        {"id": trigger["primary_entity_id"], "pos": trigger.get("primary_pos")}
                        if trigger.get("primary_entity_id") is not None
                        else (st_pre.get("nearest_hostile") if isinstance(st_pre.get("nearest_hostile"), dict) else None)
                    )
                    if has_shield_pre and (att_pre is not None or trigger.get("primary_type")):
                        self._equip_and_raise_shield_for_water(
                            st_pre,
                            pre_actions,
                            inv=inv_pre,
                            attacker=att_pre,
                            raise_on_http_error=False,
                        )

                    esc_pre_ok = False
                    esc_pre_respawned = False
                    esc_pre_handled_lava = False
                    shore_result: dict[str, Any] = {}
                    if not forced_pre_shore and not drowning_pre:
                        short_escape = self._short_water_escape_after_hit(st_pre, trigger.get("hurt_source")) or (
                            trigger.get("source") == "tool_interrupted_hurt"
                            and (
                                trigger.get("primary_type") == "drowned" or nh_pre_name == "drowned"
                                or (nh_pre_name in RANGED_MOBS and nh_pre_dist <= 16.0)
                            )
                        )
                        budget = {"budget_s": self.COMBAT_WATER_ESCAPE_BUDGET_S} if short_escape else {}
                        esc_act = self._action(
                            "escape_water", lambda: self._escape_water(st_pre, **budget), raise_on_http_error=False,
                        )
                        pre_actions.append(esc_act)
                        esc_pre_res = esc_act.get("result") if isinstance(esc_act, dict) and isinstance(esc_act.get("result"), dict) else {}
                        esc_pre_ok = bool(esc_act.get("ok") and esc_pre_res.get("in_water") is not True)
                        esc_pre_respawned = bool(esc_pre_res.get("respawned")) or esc_pre_res.get("mode") == "respawned"
                        esc_pre_handled_lava = esc_pre_res.get("mode") == "lava_escape"
                    if not esc_pre_ok and not esc_pre_respawned and not esc_pre_handled_lava:
                        shore_threat_pos = trigger.get("primary_pos") or (att_pre.get("pos") if isinstance(att_pre, dict) else None)
                        if (drowned_during_tool or urgent_water_melee) and shore_threat_pos is None and att_pre:
                            shore_threat_pos = self._find_hostile_position(att_pre.get("id"), nh_pre_name, timeout_s=0.6)
                        shore_pre = self._action(
                            "escape_to_shore",
                            lambda: self.combat_env.escape_deep_water_to_shore(
                                shore_threat_pos,
                                keep_shield=has_shield_pre,
                                shield_target_id=att_pre.get("id") if isinstance(att_pre, dict) else None,
                                **({"surface_first": True} if drowning_pre or surface_first_pre else {}),
                                **({"prefer_recent_dry_bank": True} if repeated_underground_water else {}),
                                **({"recently_used_banks": recent_banks} if recent_banks else {}),
                                **({"avoid_close_threat": True}
                                   if repeated_underground_water or (urgent_water_melee and shore_threat_pos) else {}),
                                **({"under_attack": True} if urgent_water_melee else {}),
                            ),
                            raise_on_http_error=False,
                        )
                        pre_actions.append(shore_pre)
                        shore_result = shore_pre.get("result") or {}
                        esc_pre_respawned = bool(shore_result.get("respawned")) or shore_result.get("mode") == "respawned"
                    try:
                        st_after = self.bot.status()
                    except (ApiError, httpx.HTTPError):
                        st_after = st_pre
                    water_observation = trigger.get("water_observation") or {}
                    if (
                        mining_water_pre and tool_water_threat and not esc_pre_respawned
                        and trigger.get("action_released") is True
                        and water_observation.get("reason") == "sustained_submersion"
                    ):
                        settled = self._settle_mining_shore_jump(st_pre, st_after, shore_result)
                        shore_jump_settled = (
                            settled is not st_after and self._same_life(st_pre, settled)
                            and settled.get("in_water") is False and settled.get("on_ground") is True
                            and not (settled.get("submerged") or settled.get("in_lava"))
                        )
                        st_after = settled
                    life_unavailable = esc_pre_respawned or life_changed(st_after)
                    if life_unavailable:
                        self._last_water_escape_at = float("-inf")
                        self._last_water_escape_failed = False
                        self._water_hurt_streak = 0
                        summary = f"执行 {interrupted_name} 时脱水期间角色状态已变化，请按最新状态重新规划"
                        outcome = {"handled": False, "mode": "life_unavailable", "actions": pre_actions}
                    else:
                        left_water = (
                            not st_after.get("in_water") and not st_after.get("in_lava")
                            and st_after.get("on_ground") is not False
                        )
                        self._last_water_escape_at = time.monotonic()
                        self._last_water_escape_failed = not left_water
                        if left_water:
                            self._water_hurt_streak = 0
                        recovery = "已脱离水域" if left_water else f"上浮后 in_water={bool(st_after.get('in_water'))}"
                        summary = f"执行 {interrupted_name} 途中水下遇险，{recovery}，当前 HP={st_after.get('health')}"
                        if repeated_underground_water:
                            water_block = tuple(map(math.floor, water_pos[:3]))
                            summary += f"。矿道重复进水，请避开水格 {water_block}，沿确认干燥可通行的方向换一段巷道再采矿"
                        if surface_mining_water:
                            dry_pos = shore_result.get("pos_after")
                            if shore_result.get("ok") and dry_pos:
                                bank = (
                                    math.floor(dry_pos[0]) + 0.5, float(math.floor(dry_pos[1] + 0.05)),
                                    math.floor(dry_pos[2]) + 0.5,
                                )
                                if bank not in recent_banks:
                                    recent_banks = (*recent_banks[-3:], bank)
                            self._last_surface_mining_water = (life, now_pre, tuple(water_pos[:3]), recent_banks)
                            if repeated_surface_water:
                                summary += "。海岸重复进水，请沿干地离开这段水边，换一个采矿入口"
                        outcome = {
                            "handled": True, "mode": "water_surface", "summary": summary,
                            "actions": pre_actions, "status_after": st_after,
                        }
                else:
                    act = self._action(
                        "subcortical_combat",
                        lambda: self._run_subcortical_combat(trigger),
                        raise_on_http_error=False,
                    )
                    if act.get("ok") is False and isinstance(act.get("error"), dict) and act["error"].get("code") in {"network_timeout", "network_error", "network_uncertain"}:
                        for rst in (
                            lambda: self.raw_bot.call("control", {"wait": False}, allow_reauth=False, timeout_s=1.5),
                            lambda: self.raw_bot.call("posture", {}, allow_reauth=False, timeout_s=1.5),
                        ):
                            try:
                                rst()
                            except Exception:
                                pass
                        try:
                            st_after = self.bot.status()
                        except (ApiError, httpx.HTTPError):
                            st_after = st_pre
                        life_unavailable = life_changed(st_after)
                        _, hostile_dist, _, _ = self._extract_hostile_info(st_after)
                        summary = f"执行 {interrupted_name} 途中遇袭，小脑接管超时（{act['error'].get('message')}）"
                        if life_unavailable:
                            summary += "，角色状态已变化，请按最新状态重新规划"
                        elif 0 <= hostile_dist <= 4.0 and not st_after.get("in_lava") and not st_after.get("in_water"):
                            summary += (
                                f"，敌对生物仍在 {hostile_dist:.1f}m 内。下一步立即调用 mc_skill_flee 撤退，"
                                "或先在安全位置封堵敌人来路。脱险前禁止继续挖掘或调用 mc_skill_mine_dfs（包括 reset）"
                            )
                        outcome = {
                            "handled": False, "mode": "life_unavailable" if life_unavailable else "network_timeout",
                            "summary": summary, "actions": [act], "status_after": st_after,
                        }
                    elif isinstance(act.get("error"), dict):
                        try:
                            st_after = self.bot.call("status", method="GET", timeout_s=0.6, allow_reauth=False)
                        except (ApiError, httpx.HTTPError):
                            st_after = {}
                        life_unavailable = life_changed(st_after)
                        error_code = act["error"].get("code")
                        summary = f"执行 {interrupted_name} 途中遇袭，战斗反射受阻（{error_code}）"
                        if life_unavailable:
                            summary += "，角色状态已变化"
                        outcome = {
                            "handled": False, "mode": "life_unavailable" if life_unavailable else "combat_failed",
                            "actions": [act], "status_after": st_after,
                        }
                    else:
                        outcome = act.get("result") if isinstance(act.get("result"), dict) else {"handled": True, "actions": [act]}
                        try:
                            st_after = outcome.get("status_after") or self.bot.status()
                        except (ApiError, httpx.HTTPError):
                            st_after = st_pre
                        life_unavailable = outcome.get("mode") == "life_unavailable" or (
                            isinstance(st_after, dict) and life_changed(st_after)
                        )
                        falling_after = isinstance(st_after, dict) and not life_unavailable and self._nether_fast_fall(st_after)
                        if falling_after:
                            st_after = self._settle_dry_landing(st_pre, st_after, self.bot, deadline=time.monotonic() + 0.7)
                            life_unavailable = life_changed(st_after)
                        post_name, post_dist, post_id, post_pos = self._extract_hostile_info(st_after if isinstance(st_after, dict) else {})
                        close_drowned_after_reflex = (
                            trigger.get("source") == "tool_interrupted_hurt"
                            and post_name == "drowned" and post_dist <= 6.0
                            and st_after.get("ready") is not False and st_after.get("alive") is not False
                            and st_after.get("last_death") == st_pre.get("last_death")
                            and st_after.get("dimension") == st_pre.get("dimension")
                        )
                        if not life_unavailable and isinstance(st_after, dict) and st_after.get("in_lava"):
                            self._last_lava_contact_at = time.monotonic()
                            lava_escape = self._action("escape_lava", self.combat_env.escape_lava, raise_on_http_error=False)
                            try:
                                st_after = self.bot.status()
                            except (ApiError, httpx.HTTPError):
                                pass
                            if falling_after:
                                lava_result = lava_escape.get("result") or {}
                                life_unavailable = bool(lava_result.get("respawned") or life_changed(st_after))
                                escaped = (
                                    not life_unavailable and lava_escape.get("ok") is True
                                    and st_after.get("ready") is True
                                    and st_after.get("in_lava") is False and st_after.get("on_ground") is True
                                )
                                escape_summary = "已脱困" if escaped else "尚未确认安全落地"
                                outcome = {
                                    **outcome, "handled": escaped, "mode": "lava_escape", "status_after": st_after,
                                    "summary": f"执行 {interrupted_name} 途中下坠后接触岩浆，已转为脱困。{escape_summary}",
                                }
                        elif (
                            falling_after and not life_unavailable and st_after.get("on_ground") is False
                            and not (st_after.get("in_water") or st_after.get("submerged"))
                        ):
                            outcome = {
                                **outcome, "handled": False, "mode": "falling", "summary": falling_summary,
                                "status_after": st_after,
                            }
                        elif (
                            isinstance(st_after, dict)
                            and not life_unavailable
                            and st_after.get("in_water")
                            and (self._custom_combat_registered or outcome.get("mode") not in {"water_surface", "water_surface_abort"})
                            and (
                                close_drowned_after_reflex or st_after.get("submerged")
                                or water_oxygen_below(st_after, 12)
                                or time.monotonic() < self._no_water_until
                            )
                        ):
                            if close_drowned_after_reflex:
                                if post_pos is None:
                                    post_pos = self._find_hostile_position(post_id, post_name, timeout_s=0.6)
                                offhand = st_after.get("offhand")
                                keep_shield = bool(st_after.get("blocking")) or (
                                    isinstance(offhand, dict) and str(offhand.get("name") or "").removeprefix("minecraft:") == "shield"
                                )
                                self._action(
                                    "escape_to_shore",
                                    lambda: self.combat_env.escape_deep_water_to_shore(
                                        post_pos, keep_shield=keep_shield, shield_target_id=post_id, avoid_close_threat=True,
                                    ),
                                    raise_on_http_error=False,
                                )
                            else:
                                esc_post = self._action("escape_water", lambda: self._escape_water(st_after), raise_on_http_error=False)
                                esc_post_res = esc_post.get("result") if isinstance(esc_post.get("result"), dict) else {}
                                if (
                                    not esc_post_res.get("respawned") and esc_post_res.get("mode") != "lava_escape"
                                    and not (esc_post.get("ok") and esc_post_res.get("in_water") is not True)
                                ):
                                    self._action(
                                        "escape_to_shore",
                                        lambda: self.combat_env.escape_deep_water_to_shore(trigger.get("primary_pos")),
                                        raise_on_http_error=False,
                                    )
                            try:
                                st_after = self.bot.status()
                            except (ApiError, httpx.HTTPError):
                                pass
                        life_unavailable = life_unavailable or (isinstance(st_after, dict) and life_changed(st_after))
                        if life_unavailable:
                            outcome = {**outcome, "handled": False, "mode": "life_unavailable"}
                            summary = f"执行 {interrupted_name} 途中遇袭，战斗反射期间角色状态已变化，请按最新状态重新规划"
                        else:
                            summary = str(outcome.get("summary") or f"执行 {interrupted_name} 途中遇袭，已完成 0-LLM 本能处置")
                            if trigger.get("source") == "pre_hurt_creeper":
                                self._repeat_stationary_retreat_warning(trigger, st_pre, st_after, [act])
            self.last_reaction = time.monotonic()
            if not life_unavailable:
                self._mark_pre_hurt_cooldown(trigger, self.last_reaction)
            self.previous_health = float(st_after.get("health")) if isinstance(st_after.get("health"), (int, float)) else None
            try:
                self._drain_events_pages()
            except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                raise
            except Exception:
                pass
            stop_reason = trigger.get("source") or "guardian_preempted"
            water_observation = trigger.get("water_observation")
            wet_pos = (water_observation or {}).get("pos")
            preempt_pos = wet_pos or fall_start_pos or st_pre.get("pos")
            release = {"action_released": trigger["action_released"]} if "action_released" in trigger else {}
            hurt_detail = (
                {"hurt_source": trigger["hurt_source"]}
                if interrupted_name == "mc_skill_combat" and trigger.get("hurt_source") else {}
            )
            work_observation: dict[str, Any] = {"shore_landing_confirmed_after_jump": True} if shore_jump_settled else {}
            if (
                interrupted_name in MINING_WATER_WATCH_TOOLS and tool_water_threat and outcome.get("mode") == "water_surface"
                and trigger.get("action_released") is True
                and water_observation and water_observation.get("reason") == "sustained_submersion"
                and water_observation.get("submerged") is True and water_observation.get("in_water") is True
                and isinstance(wet_pos, (list, tuple)) and len(wet_pos) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in wet_pos[:3])
                and st_after.get("ready") is True and st_after.get("alive") is not False
                and st_after.get("in_water") is False and st_after.get("on_ground") is True
                and not (st_after.get("submerged") or st_after.get("in_lava") or life_changed(st_after))
            ):
                water_site = [math.floor(coord) for coord in wet_pos[:3]]
                work_observation["water_reentry_site"] = water_site
                dimension, last_death = st_after.get("dimension"), st_after.get("last_death")
                if (
                    isinstance(dimension, str) and dimension
                    and (water_observation.get("dimension"), water_observation.get("last_death")) == (dimension, last_death)
                ):
                    self._mining_water_reentry_site = (tuple(water_site), (dimension, deepcopy(last_death)))
                summary += f"。请沿干地换一个采矿入口，避开刚才持续沉头的水坑 {water_site}。仅改采集数量仍可能回到原处"
            if last_work_action:
                work_observation["last_work_action"] = last_work_action
                threat_id = trigger.get("primary_entity_id")
                if isinstance(threat_id, int):
                    work_observation["preemption_threat"] = {"id": threat_id, "name": trigger.get("primary_type")}
                    target = last_work_action.get("target")
                    contact_pos = st_pre.get("pos")
                    dimension = st_pre.get("dimension")
                    if (
                        interrupted_name in GATHERING_TOOLS and trigger.get("source") == "pre_hurt_melee"
                        and trigger_name in PRE_HURT_MELEE_MOBS and trigger.get("action_released") is True
                        and last_work_action.get("endpoint") == "dig" and isinstance(target, (list, tuple))
                        and len(target) == 3 and all(type(coord) is int for coord in target)
                        and isinstance(contact_pos, (list, tuple)) and len(contact_pos) >= 3
                        and all(type(coord) in (int, float) and math.isfinite(coord) for coord in contact_pos[:3])
                        and isinstance(dimension, str) and dimension and self._same_life(st_pre, st_after)
                    ):
                        self._mining_melee_site = _MiningMeleeSite(
                            tuple(target), tuple(contact_pos[:3]), (dimension, deepcopy(st_pre.get("last_death"))),
                            threat_id, time.monotonic() + self.MINING_MELEE_REENTRY_S,
                        )
            receipt = {
                "ok": False,
                "requires_replan": True,
                "preempted_by_reflex": True,
                "interrupted_tool": interrupted_name,
                "error": {"code": "guardian_preempted", "message": summary},
                "stop_reason": stop_reason,
                "reflex_mode": outcome.get("mode"),
                "message": f"[小脑本能抢占接管] {summary}（原工具未自动重放，请基于当前最新状态决策下一步）。",
                "status_after": {
                    k: st_after.get(k)
                    for k in ("pos", "health", "food", "in_water", "on_ground", "is_day", "nearest_hostile")
                },
                **({"pos_at_preemption": preempt_pos} if preempt_pos is not None else {}),
                **({"pos_after": st_after["pos"]} if st_after.get("pos") is not None else {}),
                **({"water_risk": water_observation["reason"]} if water_observation and water_observation.get("reason") else {}),
                **release,
                **hurt_detail,
                **work_observation,
            }
            if outcome.get("mode") == "network_timeout":
                receipt["status_after"].update({
                    key: st_after.get(key) for key in ("dimension", "last_death")
                })
            if outcome.get("mode") == "falling":
                receipt["status_after"]["velocity"] = st_after.get("velocity")
            lava_status = {}
            if outcome.get("mode") == "lava_escape":
                receipt["status_after"].update({key: st_after.get(key) for key in ("in_lava", "on_fire")})
                lava_status["status_after"] = receipt["status_after"]
            self.events.emit(
                "guardian.preempted_tool", tool=interrupted_name, mode=outcome.get("mode"), summary=summary,
                stop_reason=stop_reason, **lava_status, **release, **hurt_detail, **work_observation,
                **({"water_observation": water_observation} if water_observation is not None else {}),
            )
            return receipt
        finally:
            self._in_reflex = False

    def _verify_mining_collar_approach(self, target: list[float | None]) -> None:
        # Legacy collar moves leave Y to the pathfinder. Check the departure before it can enter a drop.
        context = {"target": target, "collar_approach": True}
        deadline = time.monotonic() + 1.0
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        try:
            status = bot.call("status", method="GET", timeout_s=0.35)
            pos = status.get("pos")
            if (
                status.get("ready") is False or status.get("alive") is False
                or not isinstance(pos, (list, tuple)) or len(pos) < 3
                or not all(type(coord) in (int, float) and math.isfinite(coord) for coord in pos[:3])
            ):
                raise _TerrainProbeExpired
            context["pos"] = pos[:3]
            hazard = None
            if status.get("in_lava"):
                hazard = "lava"
            elif status.get("in_water") or status.get("submerged"):
                return
            elif status.get("on_ground") is False:
                velocity = status.get("velocity")
                if (
                    isinstance(velocity, (list, tuple)) and len(velocity) >= 2
                    and type(velocity[1]) in (int, float) and math.isfinite(velocity[1]) and velocity[1] <= -10.0
                ):
                    hazard = "falling"
            elif status.get("on_ground") is True:
                x, _, z = target
                dx, dz = x - pos[0], z - pos[2]
                distance = min(3.5, math.hypot(dx, dz))
                if distance >= 0.9:
                    route = CombatEnv(bot, geofence=self.geofence).probe_ray(
                        pos, math.atan2(-dx, -dz), dist=distance, deadline=deadline,
                    )
                    if route["hazard"] in {"cliff", "lava", "geofence"}:
                        hazard = route["hazard"]
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            raise _MiningLandingDenied(
                "mining_footing_unavailable", "暂时无法确认前往井口的近端路面，请换一条路线。", context=context,
            ) from None
        if hazard:
            raise _MiningLandingDenied(
                "unsafe_landing", "井口方向的近端存在危险落脚，请改道。", context={**context, "hazard": hazard},
            )

    def _verify_mining_landing(self, target: list[float]) -> None:
        deadline = time.monotonic() + 2.5
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        self._verify_mining_water_reentry(bot, target, deadline)
        for _ in range(2):
            if time.monotonic() >= deadline:
                break
            try:
                if CombatEnv(bot).has_safe_footing(target, require_observation=True):
                    return
            except (_RecoveryBudgetExceeded, _TerrainProbeExpired):
                continue
            raise _MiningLandingDenied(
                "unsafe_landing", "采矿落点缺少安全支撑，请补齐落脚点或改道。", context={"target": target},
            )
        raise _MiningLandingDenied(
            "mining_footing_unavailable", "暂时无法确认采矿落点，请从有支撑的地点改道。", context={"target": target},
        )

    def _mining_fallback_hazard(self, body: dict[str, Any]) -> str | None:
        ms = body.get("ms")
        if type(ms) is not int or ms <= 0 or any(body.get(key) for key in ("back", "left", "right")):
            return None
        deadline = time.monotonic() + 0.65
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        try:
            status = bot.call("status", method="GET", timeout_s=0.25, allow_reauth=False)
            if not isinstance(status, dict):
                return None
            pos, yaw = status.get("pos"), body.get("yaw", status.get("yaw"))
            if (
                status.get("ready") is not True or status.get("alive") is False or status.get("on_ground") is not True
                or status.get("in_water") or status.get("in_lava")
                or not isinstance(pos, (list, tuple)) or len(pos) < 3
                or not all(type(coord) in (int, float) and math.isfinite(coord) for coord in pos[:3])
                or type(yaw) not in (int, float) or not math.isfinite(yaw)
            ):
                return None
            # The input lasts past the intended waypoint if the pathfinder did not move.
            speed = 5.8 if body.get("sprint") or body.get("jump") else 4.4
            travel = ms / 1000 * speed + 0.45
            ray = CombatEnv(bot, geofence=self.geofence).probe_ray(pos, yaw, dist=travel, deadline=deadline)
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            return None
        return ray["hazard"] if ray["hazard"] in {"cliff", "lava", "geofence"} else None

    def _gathering_reentry_enemy(self, target: Sequence[float], call: Callable[..., dict[str, Any]]) -> int | None:
        site = self._mining_melee_site
        now = time.monotonic()
        if site is None or tuple(target) != site.target or now >= site.expires_at:
            return None
        deadline = now + 0.8
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline, call_override=call)
        try:
            status = bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
            if (
                not isinstance(status, dict) or status.get("ready") is not True or status.get("alive") is False
                or (status.get("dimension"), status.get("last_death")) != site.life
            ):
                return None
            enemy = self._find_hostile(site.entity_id, "", session=bot, timeout_s=0.35, require_position=True)
            name, _, _, pos = self._extract_hostile_info({"nearest_hostile": enemy})
            if (
                enemy is None or enemy.get("alive") is False or name not in PRE_HURT_MELEE_MOBS
                or pos is None or not all(type(coord) in (int, float) and math.isfinite(coord) for coord in pos[:3])
                or math.dist(site.contact_pos, pos[:3]) > 4.5
                or not CombatEnv(bot).has_los(site.contact_pos, pos, deadline=deadline).get("clear")
            ):
                return None
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            return None
        return site.entity_id

    def _verify_mining_water_reentry(self, bot: _GuardSession, target: list[float], deadline: float) -> None:
        record = self._mining_water_reentry_site
        if record is None or tuple(math.floor(coord) for coord in target) != record[0]:
            return
        site, life = record
        context = {"target": target, "water_reentry_site": list(site)}
        try:
            current = bot.call("status", method="GET", timeout_s=0.5)
            if not isinstance(current, dict):
                raise _TerrainProbeExpired
            dimension, last_death = current.get("dimension"), current.get("last_death")
            if (
                current.get("ready") is not True or current.get("alive") is False
                or not isinstance(dimension, str) or not dimension
                or (dimension == life[0] and life[1] is not None and last_death is None)
            ):
                raise _TerrainProbeExpired
            if (dimension, last_death) != life:
                return
            x, y, z = site
            env = CombatEnv(bot)
            for height in (y, y + 1):
                name = env._get_block_name(x, height, z, strict=True, deadline=deadline)
                if name in {"water", "flowing_water"}:
                    break
            else:
                return
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            raise _MiningLandingDenied(
                "mining_footing_unavailable", "暂时无法确认原水坑已排干，请沿干地改道。", context=context,
            ) from None
        raise _MiningLandingDenied(
            "mining_water_reentry", "这个航点仍有水，刚才曾在此持续沉头，请沿干地改道。", context=context,
        )

    def _dig_hazard(self, target: Any, *, check_support: bool, check_roof: bool) -> str | None:
        if not isinstance(target, (list, tuple)) or len(target) != 3 or not all(isinstance(v, int) for v in target):
            return None
        try:
            status = self.bot.call("status", method="GET", timeout_s=0.6)
        except (ApiError, httpx.HTTPError):
            return None
        pos = status.get("pos")
        if (
            not isinstance(pos, (list, tuple)) or len(pos) < 3
            or status.get("ready") is False or status.get("alive") is False
        ):
            return None

        deadline = time.monotonic() + 1.0
        probe_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        env = CombatEnv(probe_bot)
        check_nether_lava = (
            check_roof and str(status.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
            and not any(status.get(key) for key in ("in_lava", "in_water", "submerged"))
        )
        try:
            if check_roof and status.get("on_ground") is True:
                fluid = env.fluid_above_headroom(pos, target)
                if fluid == "lava" or (fluid == "water" and not status.get("in_water") and not status.get("submerged")):
                    self.events.emit(f"guardian.{fluid}_roof_dig_blocked", target=list(target), pos=pos)
                    return f"unsafe_{fluid}_roof"
            if check_nether_lava:
                lava = env.lava_at_unstick_opening(pos, target, deadline=deadline)
                if lava is not None:
                    self.events.emit("guardian.lava_dig_blocked", target=list(target), lava_pos=list(lava), pos=pos)
                    return "unsafe_lava_dig"
            if not check_support or env.can_remove_footing(pos, target):
                return None
        except (_RecoveryBudgetExceeded, _TerrainProbeExpired):
            if check_nether_lava:
                return "unstick_dig_unverified"
            if not check_support:
                return None
        self.events.emit("guardian.support_dig_blocked", target=list(target), pos=pos)
        return "unsafe_landing"

    def _confirm_unstick_water_exit(self, receipt: str, before: dict[str, Any]) -> str:
        try:
            result = json.loads(receipt)
        except (TypeError, ValueError):
            return receipt
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("escaped_water") is not True:
            return receipt
        try:
            after = self.bot.call("status", method="GET", timeout_s=0.6, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            after = {}
        if before.get("in_water") is True:
            after = self._settle_dry_landing(before, after, self.bot, deadline=time.monotonic() + 0.7)
        respawned = after.get("alive") is False or (
            bool(before) and after.get("last_death") is not None and after["last_death"] != before.get("last_death")
        )
        if (
            not respawned and before.get("in_water") is True and after.get("ready") is True
            and after.get("in_water") is False and not after.get("in_lava") and not after.get("submerged")
            and after.get("on_ground") is True
            and after.get("dimension") == before.get("dimension")
        ):
            return json.dumps({**result, "pos_after": after.get("pos") or result.get("pos_after")}, ensure_ascii=False)
        code = "water_escape_failed" if after.get("in_water") is True else "shore_landing_unconfirmed"
        if respawned:
            code = "respawned"
        message = "脱水期间角色死亡或重生，请按最新状态重新规划。" if respawned else "脱困结束后未确认登岸，请先检查当前状态。"
        return json.dumps({
            **result, "ok": False, "escaped_water": False, "requires_replan": True,
            "in_water": after.get("in_water"), "pos_after": after.get("pos") or result.get("pos_after"),
            **({"respawned": True} if respawned else {}),
            "error": {"code": code, "message": message},
        }, ensure_ascii=False)

    def _unstick_transport_receipt(self, failure: _UnstickTransportInterrupted) -> dict[str, Any]:
        released = self._stop_after_recovery_timeout() if failure.action_pending else None
        try:
            status = self.bot.call("status", method="GET", timeout_s=0.6)
        except (ApiError, httpx.HTTPError):
            status = {}
        cause = failure.transport_error
        timed_out = isinstance(cause, httpx.TimeoutException)
        release_fields = {"action_released": released} if released is not None else {}
        self.events.emit(
            "guardian.unstick_transport_failed", endpoint=failure.endpoint, transport=type(cause).__name__,
            stop_requested=failure.action_pending, **release_fields,
        )
        message = (
            "脱困动作结果未确认，已请求停止。请确认当前动作已释放并核对站位，再换路线。"
            if failure.action_pending else "脱困所需的状态读取失败。请核对当前站位，再换路线。"
        )
        status_fields = ("pos", "ready", "alive", "on_ground", "in_water", "in_lava", "busy")
        return {
            "ok": False, "requires_replan": True, "interrupted_tool": "mc_skill_unstick",
            "stop_reason": "network_timeout" if timed_out else "network_error",
            "error": {"code": "network_uncertain", "message": message, "endpoint": failure.endpoint, "timeout": timed_out},
            **release_fields,
            **({"status_after": {key: status[key] for key in status_fields if key in status}} if status else {}),
        }

    def _confirm_shoot_feedback(self, raw: str) -> str:
        try:
            receipt = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            return raw
        shot = receipt.get("result")
        if not isinstance(shot, dict) or "fired" not in shot or shot_release_confirmed(shot):
            return raw
        message = "未确认射出投射物或耗用弹药，请复查武器、弹药和当前射位。"
        try:
            status = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            status = {}
        if isinstance(status, dict) and status.get("blocking") is True:
            message += "当前仍在举盾，先确认实体掩护，在掩护内解除举盾后再试。"
        receipt.update(ok=False, requires_replan=True, error={"code": "shoot_unconfirmed", "message": message})
        return json.dumps(receipt, ensure_ascii=False)

    def run_tool_with_reflex(self, tool_name: str, invoke_fn: Callable[[], str]) -> str:
        """Wrap an exclusive tool invocation with mid-execution subcortical preemption."""
        is_attack = tool_name == "mc_attack"
        if (is_attack or tool_name == "mc_move_to") and not self._in_reflex:
            receipt = self._protect_action_on_hot_floor(is_attack=is_attack)
            if receipt is not None:
                return receipt
        is_flee = tool_name == "mc_skill_flee"
        if tool_name == "mc_shoot" and not self._in_reflex:
            return self._confirm_shoot_feedback(invoke_fn())
        if (tool_name in NON_PREEMPTABLE_TOOLS and not (is_flee or is_attack)) or self._in_reflex:
            return invoke_fn()
        is_unstick = tool_name == "mc_skill_unstick"
        budget_s = None
        if is_flee:
            budget_s = self.FLEE_BUDGET_S
        elif is_unstick:
            budget_s = self.UNSTICK_BUDGET_S
        # Leave time for the watcher to exit and for the final bounded stop request.
        deadline = time.monotonic() + budget_s - 2.0 if budget_s is not None else None
        dig_progress: dict[str, Any] = {}
        last_work_action: dict[str, Any] | None = (
            {} if is_unstick or tool_name in MINING_WATER_WATCH_TOOLS or tool_name in GATHERING_TOOLS else None
        )
        try:
            with self._intercept_bot_calls(
                dig_progress=dig_progress,
                last_work_action=last_work_action,
                enable_creeper_watcher=not (is_flee or is_attack),
                deadline=deadline,
                check_unstick_exit=is_unstick,
                watch_hot_floor=tool_name in HOT_FLOOR_WATCH_TOOLS,
                watch_scout_water=tool_name == "mc_skill_scout_surface",
                watch_mining_water=tool_name in MINING_WATER_WATCH_TOOLS,
                watch_move_water=tool_name == "mc_move_to",
                check_flee_exit=is_flee,
                watch_attack_emergency=is_attack,
                check_combat_step=tool_name == "mc_skill_combat",
                check_support_dig=tool_name in {"mc_dig", "mc_skill_staircase", "mc_skill_mine_dfs"},
                check_mining_landing=tool_name == "mc_skill_mine_dfs",
                check_gathering_reentry=tool_name in GATHERING_TOOLS,
                check_food_use=tool_name == "mc_use_item",
            ) as unstick_start:
                result = invoke_fn()
                if deadline is not None and time.monotonic() >= deadline:
                    raise _RecoveryBudgetExceeded
            return self._confirm_unstick_water_exit(result, unstick_start) if is_unstick else result
        except _UnsafeFoodUse:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {"code": "unsafe_to_eat", "message": "当前位置有环境危险或敌人威胁，请先到安全位置再进食。"},
            }, ensure_ascii=False)
        except _UnsafeCreeperAttack as error:
            entity_id = error.args[0]
            self.events.emit("guardian.creeper_attack_blocked", entity_id=entity_id)
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {
                    "code": "unsafe_creeper_attack", "entity_id": entity_id,
                    "message": "目标是苦力怕。请拉开距离后远程攻击，或改走安全路线。",
                },
            }, ensure_ascii=False)
        except _UnsafeDig as error:
            code = error.args[0]
            message = {
                "unsafe_lava_roof": "头顶待挖方块上方有岩浆，请从侧面寻找脱困路线。",
                "unsafe_water_roof": "头顶待挖方块上方有水，请从侧面寻找脱困路线。",
                "unsafe_lava_dig": "待挖方块旁有岩浆，请保留挡浆方块，改走已确认干燥的出口。",
                "unstick_dig_unverified": "暂时无法确认待挖方块周围有无岩浆，请保留该方块并改走干燥出口。",
                "unsafe_landing": "移除脚下方块后没有确认安全落点。请先退到有支撑的位置。",
            }[code]
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {"code": code, "message": message, "target": error.args[1]},
                **({"partial_progress": True, **dig_progress} if dig_progress else {}),
            }, ensure_ascii=False)
        except _MiningLandingDenied as error:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {"code": error.code, "message": error.message, **error.context},
                **({"partial_progress": True, **dig_progress} if dig_progress else {}),
            }, ensure_ascii=False)
        except _UnsafeCombatStep as error:
            if not is_attack:
                self._stop_after_recovery_timeout()
            hazard = error.args[0] if error.args else None
            self.events.emit("guardian.combat_step_blocked", tool=tool_name, **({"hazard": hazard} if hazard else {}))
            message = (
                "追击方向有水，请沿干地撤离或从掩体后远程攻击。"
                if hazard == "water" else "追击方向缺少安全落脚点，请在有支撑的位置接战。"
            )
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {"code": "unsafe_landing", "message": message},
            }, ensure_ascii=False)
        except (_UnsafeFleeExit, _UnsafeUnstickExit):
            self.events.emit("guardian.flee_exit_blocked" if is_flee else "guardian.unstick_exit_blocked", tool=tool_name)
            recovery = self._retreat_after_unsafe_exit(deadline, allow_cover=is_flee)
            if recovery is not None and (recovery.get("respawned") or recovery.get("reason") != "unsafe_landing"):
                return json.dumps(recovery, ensure_ascii=False)
            message = (
                "撤退方向缺少安全落脚点，请改从有支撑的方向离开。"
                if is_flee else "脱困出口缺少安全落脚点，请从当前位置寻找另一条路线。"
            )
            return json.dumps({
                **(recovery or {}),
                "ok": False,
                "requires_replan": True,
                "error": {"code": "unsafe_landing", "message": message},
            }, ensure_ascii=False)
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            self.events.emit("guardian.recovery_budget_exceeded", tool=tool_name, budget_s=budget_s)
            message = (
                "撤退超过时间预算，已请求停止移动，请根据当前位置换安全路线。"
                if is_flee else "脱困超过时间预算，已请求停止动作，请按当前位置调整方案。"
            )
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {"code": "time_budget", "message": message},
                **({"interrupted_tool": tool_name} if is_unstick else {}),
            }, ensure_ascii=False)
        except _UnstickTransportInterrupted as error:
            receipt = self._unstick_transport_receipt(error)
            if dig_progress:
                receipt.update(partial_progress=True, **dig_progress)
            return json.dumps(receipt, ensure_ascii=False)
        except RunTimedOut:
            if is_flee:
                self._stop_after_recovery_timeout()
            raise
        except ReflexPreempted as preempt:
            receipt = self.handle_preemption(preempt.trigger, interrupted_name=tool_name, last_work_action=last_work_action)
            if dig_progress:
                receipt.update(partial_progress=True, **dig_progress)
            return json.dumps(receipt, ensure_ascii=False)

    # ---- Layer 3: Post-Tool Synchronous Boundary Stabilization (Before `rate_wait`!) ----

    def stabilize_after_tools(self, tool_results: list[str] | None = None) -> str | None:
        """Run 0-LLM boundary stabilization immediately after `ToolScheduler.run()` BEFORE `rate_wait`."""
        if self._in_reflex:
            return None
        try:
            status = self.bot.status()
            fresh = self._drain_events_pages()
        except (ApiError, httpx.HTTPError):
            return None

        if not status.get("ready", True) or status.get("alive") is False or status.get("busy"):
            return None
        self.combat_env.remember_dry_bank(status)

        now_b = time.monotonic()
        in_water = bool(status.get("in_water"))
        raw_in_lava = bool(status.get("in_lava"))
        if raw_in_lava:
            self._last_lava_contact_at = now_b
        oxygen = status.get("oxygen")
        hurts = [e for e in fresh if e.get("type") == "hurt"]
        fall_only = self._unattributed_fall_only(hurts)
        other_hurts = [e for e in hurts if str(e.get("source") or "").strip().lower() != "hot_floor"]
        hurt_by_lava = any(str(h.get("source") or "").strip().lower() in LAVA_HURT_SOURCES for h in hurts)
        in_lava = raw_in_lava or hurt_by_lava
        hot_floor_only = bool(hurts) and not other_hurts and not in_water and not in_lava
        underwater_hot_floor = in_water and len(other_hurts) < len(hurts)
        needs_hot_floor_guard = (
            len(other_hurts) < len(hurts) and not in_water and not in_lava
            and self._needs_hot_floor_stance(status, now_b)
        )
        drowning = in_water and (
            (isinstance(oxygen, (int, float)) and oxygen <= 0)
            or any(str(h.get("source") or "").strip().lower() == "drown" for h in hurts)
        )
        nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)

        if not in_water and not in_lava and not status.get("submerged") and status.get("on_ground") is True:
            self._water_hurt_streak = 0
            self._last_water_escape_failed = False

        no_water_reentry_b = in_water and now_b < self._no_water_until
        ended_wet_in_payload = any('"in_water": true' in str(r or "").lower() for r in (tool_results or []))
        tool_stopped_in_water = False
        handled_water_in_tool = False
        unstick_still_wet = False
        tool_returned_in_water = False
        waiting_for_shore_landing = False
        same_threat_after_timeout = False
        staircase_surface_exit = False
        flowing_lava_target: list[int] | None = None
        staircase_lava_target: list[int] | None = None
        if tool_results:
            try:
                last_tool_result = json.loads(tool_results[-1])
            except (TypeError, ValueError):
                last_tool_result = None
            if isinstance(last_tool_result, dict):
                surface_evidence = last_tool_result.get("surface_evidence")
                if (
                    last_tool_result.get("ok") is True and last_tool_result.get("stop_reason") == "reached_surface"
                    and last_tool_result.get("vertical") == "up" and isinstance(surface_evidence, dict)
                    and status.get("on_ground") is True
                    and not any(event.get("type") in {"death", "death_loop", "disconnected", "spawn"} for event in fresh)
                ):
                    exit_pos = surface_evidence.get("pos")
                    current_pos = status.get("pos")
                    staircase_surface_exit = all(
                        isinstance(point, (list, tuple)) and len(point) >= 3
                        and all(type(coord) in (int, float) and math.isfinite(coord) for coord in point[:3])
                        for point in (exit_pos, current_pos)
                    ) and math.dist(exit_pos[:3], current_pos[:3]) <= 0.5
                error = last_tool_result.get("error")
                if (
                    isinstance(error, dict) and error.get("code") == "interact_flowing_fluid"
                    and error.get("block") in ("lava", "flowing_lava")
                ):
                    target = error.get("target")
                    if isinstance(target, list) and len(target) == 3 and all(type(coord) is int for coord in target):
                        flowing_lava_target = target
                blocker = last_tool_result.get("blocker")
                if (
                    last_tool_result.get("stop_reason") == "fluid_or_bedrock"
                    and last_tool_result.get("vertical") in {"up", "down"}
                    and str(status.get("dimension") or "").removeprefix("minecraft:") == "the_nether"
                    and isinstance(blocker, dict) and blocker.get("block") in {"lava", "flowing_lava"}
                    and not any(event.get("type") in {"death", "death_loop", "disconnected", "spawn"} for event in fresh)
                ):
                    target = blocker.get("pos")
                    end_pos, current_pos = last_tool_result.get("pos_after"), status.get("pos")
                    if (
                        isinstance(target, list) and len(target) == 3 and all(type(coord) is int for coord in target)
                        and all(
                            isinstance(point, (list, tuple)) and len(point) >= 3
                            and all(type(coord) in (int, float) and math.isfinite(coord) for coord in point[:3])
                            for point in (end_pos, current_pos)
                        ) and math.dist(end_pos[:3], current_pos[:3]) <= 0.5
                    ):
                        staircase_lava_target = target
                tool_returned_in_water = in_water
                tool_stopped_in_water = last_tool_result.get("stop_reason") == "in_water"
                tool_status = last_tool_result.get("status_after")
                if (
                    last_tool_result.get("preempted_by_reflex") is True
                    and last_tool_result.get("reflex_mode") == "network_timeout"
                    and isinstance(tool_status, dict) and not fresh
                ):
                    previous_name, previous_dist, previous_id, _ = self._extract_hostile_info(tool_status)
                    pos, previous_pos = status.get("pos"), tool_status.get("pos")
                    health = status.get("health")
                    same_threat_after_timeout = (
                        nh_name in PRE_HURT_MELEE_MOBS and nh_name == previous_name
                        and nh_id is not None and nh_id == previous_id and nh_dist >= max(1.5, previous_dist - 0.25)
                        and isinstance(health, (int, float)) and health > LOW_HP_BUNKER_MAX
                        and health == tool_status.get("health")
                        and status.get("on_ground") is True and tool_status.get("on_ground") is True
                        and all(key in tool_status and tool_status[key] == status.get(key) for key in ("dimension", "last_death"))
                        and not status.get("on_fire")
                        and all(
                            isinstance(point, (list, tuple)) and len(point) >= 3
                            and all(isinstance(coord, (int, float)) for coord in point[:3])
                            for point in (pos, previous_pos)
                        )
                        and math.dist(pos[:3], previous_pos[:3]) <= 0.2
                    )
                water_reflex = (
                    last_tool_result.get("preempted_by_reflex") is True
                    and last_tool_result.get("reflex_mode") == "water_surface"
                )
                if water_reflex and isinstance(tool_status, dict):
                    handled_water_in_tool = tool_status.get("in_water") is True
                    shore_hostile = tool_status.get("nearest_hostile")
                    waiting_for_shore_landing = (
                        not in_water and status.get("on_ground") is False and tool_status.get("on_ground") is False
                        and tool_status.get("in_water") is False and not other_hurts
                        and nh_name == "drowned" and nh_id is not None
                        and isinstance(shore_hostile, dict) and shore_hostile.get("id") == nh_id
                    )
                tool_error = last_tool_result.get("error")
                unstick_still_wet = (
                    isinstance(tool_error, dict) and (
                        (tool_error.get("code") == "water_escape_failed" and last_tool_result.get("in_water") is True)
                        or (tool_error.get("code") == "time_budget"
                            and last_tool_result.get("interrupted_tool") == "mc_skill_unstick")
                    )
                )
        retry_water_escape = not handled_water_in_tool or drowning or bool(hurts) or bool(status.get("submerged"))
        nearby_water_threat = self._has_close_water_threat(status)
        needs_water_escape = in_water and retry_water_escape and (
            not status.get("on_ground")
            or status.get("submerged")
            or (ended_wet_in_payload and not self._is_grounded_wading(status))
            or tool_stopped_in_water
            or unstick_still_wet
            or no_water_reentry_b
            or drowning
            or nearby_water_threat
            or bool(hurts)
            or water_oxygen_below(status, 18)
        )

        boundary_trigger: ThreatTrigger | None = None
        sonic_hurt = any(hit.get("source") == "sonic_boom" for hit in other_hurts)
        if not (
            in_water or in_lava or hot_floor_only or waiting_for_shore_landing or same_threat_after_timeout
            or (fall_only and status.get("on_ground") is False)
        ):
            boundary_trigger = (
                {"source": "boundary_threat", "hurt_source": "sonic_boom"}
                if sonic_hurt else self._detect_pre_hurt_trigger(status)
            )
        if boundary_trigger is None and not in_water and not in_lava and not fall_only and other_hurts and nh_name and nh_dist <= 15.0:
            boundary_trigger = {
                "source": "boundary_threat",
                "primary_entity_id": nh_id,
                "primary_type": nh_name,
                "primary_pos": nh_pos,
                "distance": nh_dist,
            }

        hp = float(status.get("health") or 20.0)
        food = int(status.get("food") or 20)
        needs_post_eat = (
            not (hot_floor_only or needs_hot_floor_guard or sonic_hurt)
            and self._can_auto_eat_safely(status, min_dist=8.0)
            and (hp <= 13.0 or food <= 13) and food < 20
        )
        in_wall_hurt = any(str(h.get("source") or "") in IN_WALL_HURT_SOURCES for h in hurts)
        pos_b = status.get("pos") or [0.0, 64.0, 0.0]
        by_b = float(pos_b[1]) if isinstance(pos_b, (list, tuple)) and len(pos_b) >= 2 else 64.0
        dim_b = str(status.get("dimension") or "").lower()
        tool_blob_b = " ".join(str(r or "") for r in (tool_results or [])).lower()
        needs_lava_edge_guard = (
            not in_lava
            and not in_water
            and status.get("on_ground") is True
            and ("lava" in tool_blob_b or ("nether" in dim_b and by_b <= 36.0))
        )
        needs_cave_prep = (
            not in_lava
            and not in_water
            and status.get("on_ground") is True
            and nh_dist > 8.0
            and by_b < 60.0
            and "nether" not in dim_b
            and "cave" in tool_blob_b
        )

        if not (
            in_lava
            or needs_hot_floor_guard
            or needs_water_escape
            or in_wall_hurt
            or boundary_trigger is not None
            or needs_post_eat
            or needs_lava_edge_guard
            or needs_cave_prep
        ):
            self.previous_health = hp
            return None

        self._in_reflex = True
        notes: list[str] = []
        try:
            still_lava = in_lava
            still_wet = in_water
            if needs_hot_floor_guard:
                act = self._action("boundary_hot_floor_sneak", lambda: self._sneak_on_hot_floor(status))
                if act.get("ok"):
                    if (act.get("result") or {}).get("mode") == "stepped_off":
                        notes.append("已移离岩浆块")
                    else:
                        notes.append("岩浆块受伤，已潜行避伤")
            if in_lava:
                esc_l = self._action(
                    "boundary_escape_lava",
                    lambda: self.combat_env.escape_lava(force_rim_escape=hurt_by_lava and not raw_in_lava),
                    raise_on_http_error=False,
                )
                still_lava = not bool(esc_l.get("ok"))
                notes.append("脱离岩浆" if not still_lava else "尝试脱离岩浆")
                if not still_lava and "nether" not in dim_b:
                    douse = self._douse_after_lava(status)
                    if douse and douse.get("ok"):
                        still_wet = True
                        notes.append("进入浅水灭火")
            elif needs_lava_edge_guard and not in_wall_hurt:
                edge_act = self._action(
                    "boundary_guard_lava_edge",
                    lambda: self.combat_env.guard_lava_edge(
                        pos_b, flowing_lava_target=flowing_lava_target, staircase_lava_target=staircase_lava_target,
                    ),
                    raise_on_http_error=False,
                )
                edge_res = edge_act.get("result") if isinstance(edge_act.get("result"), dict) else {}
                if edge_res.get("near_lava"):
                    notes.append("岩浆边潜行封边" if edge_res.get("sealed") else "岩浆边强制潜行")
                    landing = self.combat_env._safe_status()
                    if (
                        landing.get("in_lava") and landing.get("ready") is True and landing.get("alive") is not False
                        and all(landing.get(key) == status.get(key) for key in ("last_death", "dimension"))
                    ):
                        self._last_lava_contact_at = time.monotonic()
                        escape = self._action("boundary_escape_lava", self.combat_env.escape_lava)
                        still_lava = not bool(escape.get("ok"))
                        notes.append("封边后跌入岩浆，脱困尚未确认" if still_lava else "封边后脱离岩浆")
            elif needs_water_escape:
                if hurts:
                    self._water_hurt_streak += 1
                    if self._water_hurt_streak >= 3:
                        self._no_water_until = max(self._no_water_until, now_b + 30.0)
                        self.events.emit("guardian.no_water_cooldown", duration_s=30.0, streak=self._water_hurt_streak)
                has_shield_b = False
                shield_target_b = nh_id
                shore_threat_name = nh_name
                shore_threat_pos = nh_pos
                if hurts or nh_name or no_water_reentry_b:
                    try:
                        inv_b = self.bot.inventory()
                    except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                        raise
                    except Exception:
                        inv_b = {}
                    b_actions: list[dict[str, Any]] = []
                    att_b = (
                        (hurts[-1].get("source_entity") or hurts[-1].get("likely_attacker"))
                        if hurts
                        else None
                    ) or (status.get("nearest_hostile") if isinstance(status.get("nearest_hostile"), dict) else None)
                    if isinstance(att_b, dict) and isinstance(att_b.get("id"), int):
                        shield_target_b = att_b["id"]
                        shore_threat_name = str(att_b.get("name") or (nh_name if shield_target_b == nh_id else ""))
                        shore_threat_name = shore_threat_name.removeprefix("minecraft:").lower()
                        shore_threat_pos = att_b.get("pos") or (nh_pos if shield_target_b == nh_id else None)
                    has_shield_b, _ = self._equip_and_raise_shield_for_water(
                        status,
                        b_actions,
                        inv=inv_b,
                        attacker=att_b if isinstance(att_b, dict) else None,
                        action_prefix="boundary_",
                        raise_on_http_error=False,
                    )
                    if any(a.get("action") == "boundary_raise_shield" and a.get("ok") for a in b_actions):
                        notes.append("举盾护体")

                continuing_water_escape = (
                    self._last_water_escape_failed
                    and not handled_water_in_tool
                    and now_b - self._last_water_escape_at < self.WATER_ESCAPE_RETRY_WINDOW_S
                    and (bool(status.get("submerged")) or status.get("on_ground") is False)
                )
                surface_first = drowning or (continuing_water_escape and bool(status.get("submerged")))
                urgent_water_melee = self._urgent_water_melee(status, hurts) and not any(
                    e.get("type") in {"death", "death_loop", "disconnected", "spawn"} for e in fresh
                )
                forced_shore_b = (
                    self._water_hurt_streak >= 3 or no_water_reentry_b or drowning
                    or handled_water_in_tool or unstick_still_wet or self._swimming_at_low_health(status)
                    or continuing_water_escape or underwater_hot_floor or nearby_water_threat or urgent_water_melee
                    or (shore_threat_name == "creeper" and nh_dist <= 6.0)
                )
                esc_b_ok = False
                esc_b_respawned = False
                esc_b_res: dict[str, Any] = {}
                if not forced_shore_b:
                    short_escape = any(self._short_water_escape_after_hit(status, h.get("source")) for h in hurts)
                    budget = {}
                    if short_escape:
                        budget["budget_s"] = self.COMBAT_WATER_ESCAPE_BUDGET_S
                    elif tool_returned_in_water:
                        budget["budget_s"] = self.SURFACE_WATER_ESCAPE_BUDGET_S
                    esc = self._action(
                        "boundary_escape_water",
                        lambda: self._escape_water(status, **budget),
                        raise_on_http_error=False,
                    )
                    esc_b_res = esc.get("result") if isinstance(esc.get("result"), dict) else {}
                    esc_b_ok = bool(esc.get("ok") and esc_b_res.get("in_water") is not True)
                    esc_b_respawned = bool(esc_b_res.get("respawned"))
                if esc_b_res.get("mode") == "lava_escape":
                    still_lava = not esc_b_ok
                    still_wet = bool(esc_b_res.get("in_water"))
                    notes.append("脱离岩浆" if esc_b_ok else "尝试脱离岩浆")
                elif esc_b_ok:
                    still_wet = False
                    self._water_hurt_streak = 0
                    self._last_water_escape_failed = False
                    notes.append("自动浮水登岸")
                elif not esc_b_respawned:
                    if shore_threat_pos is None and nh_dist <= 16.0 and (shield_target_b is not None or shore_threat_name):
                        shore_threat_pos = self._find_hostile_position(shield_target_b, shore_threat_name, timeout_s=0.6)
                    avoid_water_attacker = shore_threat_name == "creeper" or nearby_water_threat or (
                        urgent_water_melee and shore_threat_pos is not None
                    )
                    shore_b = self._action(
                        "boundary_escape_to_shore",
                        lambda: self.combat_env.escape_deep_water_to_shore(
                            shore_threat_pos, keep_shield=has_shield_b, shield_target_id=shield_target_b,
                            **({"surface_first": True} if surface_first else {}),
                            **({"avoid_close_threat": True} if avoid_water_attacker else {}),
                            **({"under_attack": True} if urgent_water_melee else {}),
                            **({"sneak_on_magma": True} if underwater_hot_floor else {}),
                        ),
                        raise_on_http_error=False,
                    )
                    shore_b_res = shore_b.get("result") if isinstance(shore_b.get("result"), dict) else {}
                    shore_b_ok = bool(shore_b.get("ok") and shore_b_res.get("in_water") is not True)
                    still_wet = not shore_b_ok
                    self._last_water_escape_failed = not shore_b_ok
                    if shore_b_ok:
                        self._water_hurt_streak = 0
                        notes.append("游向最近干燥陆地登岸")
                    elif self._water_hurt_streak >= 3:
                        notes.append("3次水下受击强制上岸并禁下水30s")
                    elif no_water_reentry_b:
                        notes.append("30s禁水冷却内驱离水面")
                self._last_water_escape_at = time.monotonic()
            elif in_wall_hurt:
                uns = self._action("boundary_unstick_wall", lambda: self._unstick_in_wall(status), raise_on_http_error=False)
                uns_result = uns.get("result") or {}
                if uns_result.get("respawned"):
                    return None
                if uns.get("ok"):
                    notes.append("塌方破墙脱困")

            if boundary_trigger is not None and not still_lava and not still_wet:
                if staircase_surface_exit and boundary_trigger.get("source") == "pre_hurt_ranged":
                    boundary_trigger["staircase_surface_exit"] = True
                self._mark_pre_hurt_cooldown(boundary_trigger, time.monotonic())
                act = self._action(
                    "boundary_subcortical_combat",
                    lambda: self._run_subcortical_combat(boundary_trigger),
                    raise_on_http_error=False,
                )
                res_d = act.get("result") if isinstance(act.get("result"), dict) else {}
                if res_d.get("mode") == "life_unavailable":
                    return None
                self._mark_pre_hurt_cooldown(boundary_trigger, time.monotonic())
                if boundary_trigger.get("source") == "pre_hurt_ranged" or str(boundary_trigger.get("primary_type") or "") in RANGED_MOBS:
                    self._mark_ranged_engagement(
                        time.monotonic(),
                        trigger=boundary_trigger,
                        in_cover=res_d.get("mode") in {"cover_pocket", "bunker_sealed", "sealed_shaft", "natural_cover"},
                    )
                if act.get("ok"):
                    notes.append(str(res_d.get("summary") or f"处置近身威胁 {nh_name}"))
                elif isinstance(act.get("error"), dict):
                    notes.append(f"近身处置受阻({act['error'].get('code')})")
                landing = res_d.get("status_after")
                if (
                    res_d.get("mode") != "lava_escape" and isinstance(landing, dict) and landing.get("in_lava")
                    and landing.get("ready") is True and landing.get("alive") is not False
                    and all(landing.get(key) == status.get(key) for key in ("last_death", "dimension"))
                ):
                    self._last_lava_contact_at = time.monotonic()
                    escape = self._action("boundary_escape_lava", self.combat_env.escape_lava)
                    if escape.get("ok"):
                        notes.append("战斗后脱离岩浆")
                    else:
                        notes.append("战斗后落入岩浆，脱困尚未确认")

            if (needs_post_eat or needs_cave_prep) and boundary_trigger is None and not still_lava and not still_wet:
                try:
                    inv = self.bot.inventory()
                except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                    raise
                except Exception:
                    inv = {}
                counts, _, _ = self._parse_inventory_summary(inv)
                if isinstance(inv, dict) and (isinstance(inv.get("armor"), dict) or "offhand" in inv):
                    geared = auto_equip_survival_gear(self.bot, inv, allow_armor=True, allow_shield=True)
                    if geared:
                        self._last_gear_check_at = now_b
                        notes.append("自动穿戴护甲/盾")
                if (
                    status.get("on_ground") is True
                    and nh_dist > 8.0
                    and by_b < 60.0
                    and "nether" not in dim_b
                    and counts.get("torch", 0) > 0
                    and (now_b - self._last_torch_place_at >= 20.0)
                ):
                    tx_b, ty_b, tz_b = math.floor(float(pos_b[0])), math.floor(by_b + 0.05), math.floor(float(pos_b[2]))
                    try:
                        foot_b = self.combat_env._get_block_name(tx_b, ty_b, tz_b)
                        floor_b = self.combat_env._get_block_name(tx_b, ty_b - 1, tz_b)
                        if foot_b in {"air", "cave_air"} and floor_b not in {
                            "air", "cave_air", "void_air", "lava", "flowing_lava", "water", "flowing_water"
                        }:
                            self.bot.call("place", {"item": "torch", "pos": [tx_b, ty_b, tz_b]})
                            self._last_torch_place_at = now_b
                            notes.append("补放洞穴火把")
                    except ApiError:
                        pass
                if needs_post_eat:
                    best_food = next((f for f in SAFE_FOOD_PRIORITY if counts.get(f, 0) > 0), None)
                    if best_food:
                        eat_act = self._try_eat_food(best_food, status, action_name="boundary_eat")
                        if eat_act is not None and eat_act.get("ok"):
                            notes.append(f"顺手进食 {best_food}")
                            self.retry_after_ready = False

            st_after = self.bot.status()
            self.previous_health = float(st_after.get("health") or hp)
            self.last_reaction = time.monotonic()
            if not notes:
                return None
            self.stats["boundary_stabilizations"] += 1
            msg = (
                f"[脊髓边界自稳] {'; '.join(notes)} -> "
                f"pos={st_after.get('pos')} HP={st_after.get('health')} food={st_after.get('food')} in_water={st_after.get('in_water')}"
            )
            self.events.emit("guardian.boundary_stabilized", note=msg)
            return msg
        except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
            raise
        except Exception:
            return None
        finally:
            self._in_reflex = False

    def _incident(
        self,
        status: dict[str, Any],
        reasons: list[str],
        actions: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
        *,
        requires_replan: bool = True,
    ) -> dict[str, Any]:
        self.last_reaction = time.monotonic()
        self.previous_health = status.get("health")
        self.stats["incidents"] += 1
        if requires_replan:
            note = "响应不代表整体安全；按最新状态重新规划，勿继续被中断的旧动作链。"
        elif "fresh_hurt" in reasons:
            note = "处于举盾/掩体/远程交战姿态，未打断当前生成。"
        elif "pre_hurt_creeper" in reasons:
            note = "已复查苦力怕威胁，继续当前生成。"
        elif "pre_hurt_melee" in reasons:
            combat_result = actions[0].get("result") if actions else None
            combat_mode = combat_result.get("mode") if isinstance(combat_result, dict) else None
            note = {
                "sealed_shaft": "井口掩体复查仍有效，本轮未放置方块，继续当前生成。",
                "cover_or_flee": "同一近敌仍在，站位与风险复查无变化，继续当前生成。",
            }.get(combat_mode, "近战威胁已清除，复查无新险情，继续当前生成。")
        elif "in_lava" in reasons:
            note = "仍在原处尝试脱离岩浆，未观察到新伤害，继续当前生成。"
        elif "hot_floor" in reasons:
            note = "已潜行避开岩浆块伤害，未打断当前生成。"
        else:
            note = "后台已静默进食，未打断当前生成。"
        rem_no_water = self._no_water_until - time.monotonic()
        if requires_replan and rem_no_water > 0.0:
            note += f" 禁止下水冷却中(剩余{int(math.ceil(rem_no_water))}s)，保持干燥陆地避险。"
        incident: dict[str, Any] = {
            "reasons": reasons,
            "actions": actions,
            "status": {
                k: status.get(k)
                for k in ("pos", "health", "food", "ready", "alive", "busy", "in_water", "in_lava", "on_ground", "blocking")
            },
            "event_seqs": [
                e.get("seq")
                for e in fresh
                if e.get("type") in {"hurt", "death", "spawn", "death_loop", "disconnected"}
            ],
            "requires_replan": requires_replan,
            "note": note,
        }
        if requires_replan and status.get("ready") and status.get("alive") is not False:
            try:
                incident["inventory_after"] = self.bot.inventory()
            except (ApiError, httpx.HTTPError):
                incident["inventory_after"] = None
                incident["inventory_note"] = "动作后背包读取失败；重新查询，不得引用开局材料数量。"
        self.events.emit("guardian.incident", **incident)
        return incident
