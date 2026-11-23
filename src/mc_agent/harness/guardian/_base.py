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

from ...bot import BotSession
from ...errors import ApiError
from ..control import RequestCancelled, RunTimedOut
from ...skills.combat_env import (
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
)
from ...skills.library import MacroSkills, _inventory_counts_from_raw, best_melee_weapon_in_inv, ray_wall_blocker
from ...skills.safety import SAFE_FOOD_PRIORITY, auto_equip_survival_gear, water_oxygen_below

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


class _UnsafeCreeperMove(BaseException):
    """Unwind a movement request whose goal would approach a confirmed creeper."""


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
    failed_cover: bool = False


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
