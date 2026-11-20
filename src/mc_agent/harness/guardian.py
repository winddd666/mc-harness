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
from copy import copy
import json
import math
import threading
import time
from typing import Any, Callable

import httpx

from ..errors import ApiError
from .control import RequestCancelled, RunTimedOut
from ..skills.combat_env import (
    LONE_COUNTERKILL_MOBS,
    MELEE_WEAPON_ORDER,
    PRE_HURT_MELEE_MOBS,
    RANGED_MOBS,
    CombatController,
    CombatControllerSlot,
    CombatEnv,
    ThreatTrigger,
    aim_yaw_pitch,
)
from ..skills.library import MacroSkills
from ..skills.safety import SAFE_FOOD_PRIORITY, auto_equip_survival_gear

# Hurt sources that mean "my body is embedded in a block", not "something is attacking me".
IN_WALL_HURT_SOURCES = frozenset({"in_wall", "suffocation", "cramming", "inside_block"})

# At or below this health, stop trying to walk away while being hit: seal up in place instead.
LOW_HP_BUNKER_MAX = 10.0

# Ranged damage: sprinting away in open cave air just makes the firing line longer.
RANGED_HURT_SOURCES = frozenset({"arrow", "projectile", "trident", "fireball", "small_fireball", "wither_skull", "skeleton"})
MELEE_HURT_SOURCES = frozenset({"mob_attack", "player_attack"})

# Environmental / tick damage sources that a shield cannot block and must not trigger fake combat reflexes.
LAVA_HURT_SOURCES = frozenset({"lava"})
FIRE_HURT_SOURCES = frozenset({"on_fire", "in_fire", "fire"})
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


class ReflexPreempted(BaseException):
    """Internal signal raised to unwind an active macro skill into 0-LLM subcortical takeover."""

    def __init__(self, trigger: ThreatTrigger) -> None:
        super().__init__(f"Reflex preempted by {trigger.get('source')}: {trigger.get('primary_type')}")
        self.trigger = trigger


class _RecoveryBudgetExceeded(BaseException):
    """Unwind a recovery macro through its transport and retry handlers."""


class _UnsafeUnstickExit(BaseException):
    """Unwind an unstick whose final movement has no verified footing."""


class _UnsafeFleeExit(BaseException):
    """Unwind an explicit retreat before an unsupported ground movement."""


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
        self.deadline = deadline
        self._call_override = call_override

    def _time_remaining(self) -> float:
        remaining = float("inf") if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise _RecoveryBudgetExceeded
        return remaining

    def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        self.checkpoint()
        readonly = action in {"status", "events", "inventory", "get_entities", "get_block", "find_blocks"}
        timeout = kwargs.get("timeout_s")
        if timeout is None:
            timeout = 2.0 if readonly else 8.0
        if self.deadline is not None:
            remaining = self._time_remaining()
            if isinstance(timeout, httpx.Timeout):
                # BotSession can retry a closed keepalive after a 150ms delay.
                attempt_budget = (remaining - 0.15) / 2
                if attempt_budget <= 0:
                    raise _RecoveryBudgetExceeded
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
            self._time_remaining()
            raise
        self.checkpoint()
        self._time_remaining()
        return result

    def status(self) -> dict[str, Any]:
        return self.call("status", method="GET")

    def inventory(self) -> dict[str, Any]:
        return self.call("inventory", method="GET")


class GenerationGuard:
    WATER_ESCAPE_BUDGET_S = 20.0
    WATER_SHORE_MAX_HP = 6.0
    WATER_ESCAPE_RETRY_WINDOW_S = 30.0
    IN_WALL_RECOVERY_BUDGET_S = 6.0
    COMBAT_RECOVERY_BUDGET_S = 8.0
    UNSTICK_BUDGET_S = 30.0
    FLEE_BUDGET_S = 35.0
    HOT_FLOOR_RETRY_S = 5.0
    MELEE_EAT_DEFER_S = 10.0
    PRE_HURT_TARGET_COOLDOWN_S = 3.0
    PRE_HURT_RANGED_COOLDOWN_S = 30.0
    PRE_HURT_MELEE_COOLDOWN_S = 12.0
    RANGED_COMBAT_STANCE_S = 35.0

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
            self.bot, skills=self.skills, geofence=self.geofence, water_escape=self._escape_water,
        )
        self.combat_slot = CombatControllerSlot(combat_controller)
        self._custom_combat_registered = combat_controller is not None
        self.last_poll = float("-inf")
        self.last_reaction = float("-inf")
        self._last_water_escape_at = float("-inf")
        self._last_water_escape_failed = False
        self._water_hurt_streak = 0
        self._no_water_until = float("-inf")
        self._last_pre_hurt_melee_at = float("-inf")
        self._last_fire_douse_at = float("-inf")
        self._last_non_combat_alert_at = float("-inf")
        self._last_gear_check_at = float("-inf")
        self._last_torch_place_at = float("-inf")
        self._hot_floor_retry_at = float("-inf")
        self._last_hot_floor_block: tuple[str, int, int, int] | None = None
        self.previous_health: float | None = None
        self.previous_ready: bool | None = None
        self.cursor = int(getattr(bot, "_last_event_seq", 0) or 0)
        self.failures = 0
        self.disconnected = False
        self.retry_after_ready = False
        self._in_reflex = False
        self._consecutive_flees = 0
        self._shield_only_streak = 0
        self._last_shield_only_at = float("-inf")
        self._pre_hurt_suppress_block: tuple[int, int, int] | None = None
        self._pre_hurt_suppress_until: float = float("-inf")
        self._pre_hurt_target_cooldown: dict[str, float] = {}
        self._ranged_combat_stance_until: float = float("-inf")
        self._ranged_cover_active_until: float = float("-inf")
        self._last_ranged_hurt_at = float("-inf")
        self._last_melee_hurt_at = float("-inf")
        self._last_dig_cover_fail_at: float = float("-inf")
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

    def _stop_after_recovery_timeout(self) -> None:
        try:
            self.raw_bot.call("stop", {}, timeout_s=1.5, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            pass

    def _run_recovery_macro(
        self,
        before: dict[str, Any],
        run: Callable[[Any], dict[str, Any]],
        *,
        budget_s: float,
        timeout_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        skills = self._skills()
        recovery_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + budget_s)
        if isinstance(skills, MacroSkills):
            # Keep the shared avoidance records while leaving the tool runner's session binding intact.
            skills = copy(skills)
            skills.bot = recovery_bot
        try:
            result = run(skills)
            if not result.get("ok"):
                return result
            after = recovery_bot.status()
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
        return result

    def _escape_water(self, before: dict[str, Any]) -> dict[str, Any]:
        return self._run_recovery_macro(
            before, lambda skills: skills.escape_water(), budget_s=self.WATER_ESCAPE_BUDGET_S,
            timeout_details={"in_water": True},
        )

    def _swimming_at_low_health(self, status: dict[str, Any]) -> bool:
        health = status.get("health")
        return (
            isinstance(health, (int, float)) and health <= self.WATER_SHORE_MAX_HP
            and (status.get("on_ground") is False or bool(status.get("submerged")))
        )

    def _unstick_in_wall(self, before: dict[str, Any]) -> dict[str, Any]:
        return self._run_recovery_macro(before, lambda skills: skills.unstick(), budget_s=self.IN_WALL_RECOVERY_BUDGET_S)

    def _defend_or_flee(
        self, status: dict[str, Any], attacker: dict[str, Any] | None, counts: dict[str, int],
        *, ranged: bool = False, prefer_cover: bool = False,
    ) -> dict[str, Any]:
        skills = self._skills()
        health = status.get("health")
        # The legacy macro cannot counter below 11 HP and falls through to an unchecked sprint-jump.
        low_health = isinstance(health, (int, float)) and health < 11.0
        retreat_only = ranged or low_health
        if "nether" in str(status.get("dimension") or "").lower() or (retreat_only and isinstance(skills, MacroSkills)):
            pos = attacker.get("pos") if isinstance(attacker, dict) else None
            if not pos:
                _, _, _, pos = self._extract_hostile_info(status)
            if prefer_cover and pos:
                cover_bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 2.25)
                try:
                    cover = CombatEnv(cover_bot, geofence=self.geofence).seek_natural_cover(pos)
                except _RecoveryBudgetExceeded:
                    self._stop_after_recovery_timeout()
                    cover = None
                if cover is not None:
                    return cover
            return self.combat_env.flee(pos, wait_for_ground=retreat_only)
        return self._run_recovery_macro(
            status,
            lambda skills: skills.defend_or_flee(
                attacker=attacker, counts=counts, health=health, urgent=True, geofence=self.geofence,
            ),
            budget_s=self.COMBAT_RECOVERY_BUDGET_S,
        )

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
        self.events.emit("guardian.action.completed", duration=time.monotonic() - started, **outcome)
        return outcome

    def _drain_events_pages(self, *, observe_combat_hurt: bool = True) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for _ in range(5):
            page = self.bot.call("events", method="GET", params={"since": self.cursor, "limit": 100})
            rows = [e for e in page.get("events", []) if int(e.get("seq", 0)) > self.cursor]
            rec_fn = getattr(self.bot.bot, "record_death_event", None)
            if callable(rec_fn):
                for ev in rows:
                    if str(ev.get("type") or "").lower() == "death":
                        rec_fn(ev.get("seq") if ev.get("seq") is not None else ev.get("t"))
            fresh.extend(rows)
            self.cursor = max([self.cursor, int(page.get("last_seq", 0))] + [int(e.get("seq", 0)) for e in rows])
            if not page.get("has_more") or not rows:
                break
        if observe_combat_hurt and fresh:
            now = time.monotonic()
            for event in fresh:
                if event.get("type") in {"death", "death_loop", "disconnected", "spawn"}:
                    self._last_ranged_hurt_at = float("-inf")
                    self._last_melee_hurt_at = float("-inf")
                elif event.get("type") == "hurt":
                    source = str(event.get("source") or "").strip().lower()
                    if source in RANGED_HURT_SOURCES:
                        self._last_ranged_hurt_at = now
                        self._last_melee_hurt_at = float("-inf")
                    elif source in MELEE_HURT_SOURCES:
                        self._last_melee_hurt_at = now
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

    def _find_hostile_position(
        self, entity_id: int | None, entity_name: str, *, session: _GuardSession | None = None,
    ) -> list[float] | None:
        # Live status identifies the nearest hostile without always including coordinates.
        try:
            response = (session or self.bot).call("get_entities", {"radius": 16})
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
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    return list(pos)
        return None

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
        nh_name, nh_dist, _, _ = self._extract_hostile_info(status)
        if nh_name and nh_dist <= min_dist:
            return False
        ranged_in_range = (nh_name in RANGED_MOBS or nh_name == "ghast") and nh_dist <= ranged_min_dist
        now = time.monotonic()
        protecting_against_recent_hit = status.get("blocking") and (
            now - self._last_ranged_hurt_at < 8.0 or now - self._last_melee_hurt_at < self.MELEE_EAT_DEFER_S
        )
        if (ranged_in_range or protecting_against_recent_hit) and not self._inside_own_shaft():
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
        return status.get("on_ground") is True and not status.get("sneaking") and (
            now >= self._hot_floor_retry_at or self._hot_floor_block(status) != self._last_hot_floor_block
        )

    def _sneak_on_hot_floor(self, status: dict[str, Any]) -> dict[str, Any]:
        self._hot_floor_retry_at = time.monotonic() + self.HOT_FLOOR_RETRY_S
        self._last_hot_floor_block = self._hot_floor_block(status)
        posture = {"sneak": True, "shield": bool(status.get("blocking")), "jump": False}
        result = self.bot.call("posture", posture, timeout_s=1.0)
        if result.get("sneaking") is False:
            # The saved posture can still say sneak=true after the backend clears the actual key.
            self.bot.call("posture", {**posture, "sneak": False}, timeout_s=1.0)
            result = self.bot.call("posture", posture, timeout_s=1.0)
        ok = result.get("sneaking") is not False
        if ok:
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

    def _protect_attack_on_hot_floor(self) -> str | None:
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
        self.combat_env.invalidate_block_cache()
        if not self.combat_env.is_on_hot_floor(status["pos"]):
            return None
        step = self._action("hot_floor_exit_before_attack", lambda: self._step_off_hot_floor(status))
        return json.dumps({
            "ok": False,
            "requires_replan": True,
            "error": {"code": "hot_floor", "message": "脚下是岩浆块，请确认安全落脚后重新选择攻击目标。"},
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
            nh_pos = self._find_hostile_position(nh_id, nh_name)

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
        try:
            status = self.bot.status()
            fresh = self._drain_events_pages()
            self.failures = 0
        except (ApiError, httpx.HTTPError) as error:
            if isinstance(error, ApiError) and error.code in {"not_ready", "wrong_server"}:
                self.events.emit("guardian.observation_not_ready", code=error.code)
                return None
            self.failures += 1
            if self.failures == 1:
                self.events.emit("guardian.observation_failed", error=type(error).__name__, code=getattr(error, "code", None))
            if self.failures >= 3:
                self.disconnected = True
            return None

        health = status.get("health")
        dropped = isinstance(health, (int, float)) and self.previous_health is not None and health < self.previous_health - 0.2
        ready = bool(status.get("ready", status.get("alive", True)))
        disconnected = self.previous_ready is True and not ready
        self.previous_ready = ready
        self.previous_health = float(health) if isinstance(health, (int, float)) else None
        hurts = [e for e in fresh if e.get("type") == "hurt"]
        other_hurts = [e for e in hurts if str(e.get("source") or "").strip().lower() != "hot_floor"]
        in_wall_hurt = any(str(h.get("source") or "") in IN_WALL_HURT_SOURCES for h in hurts)
        deaths = [e for e in fresh if e.get("type") in {"death", "death_loop", "disconnected", "spawn"}]
        in_water = bool(status.get("in_water"))
        raw_in_lava = bool(status.get("in_lava"))
        hurt_by_lava = any(str(h.get("source") or "").strip().lower() in LAVA_HURT_SOURCES for h in hurts)
        in_lava = raw_in_lava or (
            hurt_by_lava and not deaths and not disconnected and ready and status.get("alive") is not False
        )
        hot_floor_only = bool(hurts) and not other_hurts and not in_water and not in_lava
        needs_hot_floor_guard = (
            len(other_hurts) < len(hurts) and not in_water and not in_lava
            and self._needs_hot_floor_stance(status, now)
        )
        oxygen = status.get("oxygen")
        low_oxygen = isinstance(oxygen, (int, float)) and oxygen < 240
        critical_drowning = isinstance(oxygen, (int, float)) and oxygen < 150
        drowning = in_water and (
            (isinstance(oxygen, (int, float)) and oxygen <= 0)
            or any(str(h.get("source") or "").strip().lower() == "drown" for h in hurts)
        )
        no_water_active = now < self._no_water_until
        water_retry_gap = 8.0 if self._last_water_escape_failed else 1.5
        no_water_reentry = (
            in_water
            and no_water_active
            and (now - self._last_water_escape_at >= water_retry_gap)
        )
        needs_float = (
            in_water
            and (
                drowning
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
            and not deaths
            and not disconnected
            and ready
            and status.get("alive") is not False
        ):
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
        nh_name0, nh_dist0, _, _ = self._extract_hostile_info(status)
        eat_safe_distance = self._can_auto_eat_safely(status, min_dist=5.5)
        should_eat = (needs_food or pending_ready_eat) and eat_safe_distance and not (hot_floor_only or needs_hot_floor_guard)
        hurt = bool(hurts) or dropped
        non_combat_only_hurt = (
            hurt
            and bool(hurts)
            and not in_lava
            and not in_water
            and not deaths
            and all(
                str(h.get("source") or "").strip().lower() in NON_COMBAT_HURT_SOURCES
                and not (h.get("source_entity") or h.get("likely_attacker"))
                for h in hurts
            )
            and (not nh_name0 or nh_dist0 > 8.0)
        )
        fire_hurt_only = non_combat_only_hurt and any(
            str(h.get("source") or "").strip().lower() in FIRE_HURT_SOURCES for h in hurts
        )
        hp_cur = float(health) if isinstance(health, (int, float)) else 20.0

        pre_trigger = (
            None
            if (hurt or deaths or disconnected or not ready or in_water or in_lava)
            else self._detect_pre_hurt_trigger(status)
        )
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
        already_in_ranged_guard = (
            bool(status.get("blocking"))
            or (now < self._ranged_cover_active_until)
            or (now < self._ranged_combat_stance_until)
        )
        if effective_combat_hurt and not ranged_hurt_only:
            self._pre_hurt_suppress_until = float("-inf")
        critical_override = bool(
            deaths
            or in_lava
            or needs_hot_floor_guard
            or drowning
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

        if not (
            effective_combat_hurt
            or needs_hot_floor_guard
            or critical_non_combat
            or can_try_fire_douse
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
        if now - self.last_reaction < 3.0 and not (critical_override or pending_ready_eat):
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
            if needs_food and not self.retry_after_ready:
                self.retry_after_ready = True
                self.events.emit("guardian.eat_deferred", reason="deaths_or_not_ready", retry_after_ready=True)
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

            try:
                inv = self.bot.inventory() if ((hurt and not hot_floor_only) or should_eat or in_lava or no_water_reentry or pre_trigger is not None) else {}
            except (ApiError, httpx.HTTPError):
                inv = {}
            counts, has_shield, has_melee_weapon = self._parse_inventory_summary(inv)
            if (
                isinstance(inv, dict)
                and (isinstance(inv.get("armor"), dict) or "offhand" in inv)
                and (now - self._last_gear_check_at >= 12.0)
            ):
                auto_equip_survival_gear(self.bot, inv, allow_armor=True, allow_shield=False)
                self._last_gear_check_at = now

            if in_lava:
                if counts.get("water_bucket", 0) > 0 and "nether" not in str(status.get("dimension") or "").lower():
                    actions.append(self._action("douse_lava", lambda: self.combat_env.build_tactical("place_water_feet")))
                actions.append(
                    self._action(
                        "escape_lava",
                        lambda: self.combat_env.escape_lava(force_rim_escape=hurt_by_lava and not raw_in_lava),
                    )
                )

            def _already(name: str) -> bool:
                return any(a.get("action") == name for a in actions)

            nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)

            if (needs_float or no_water_reentry) and not in_lava and not hurt:
                if no_water_reentry and nh_name and has_shield:
                    self._equip_and_raise_shield_for_water(
                        status,
                        actions,
                        inv=inv,
                        attacker=status.get("nearest_hostile") if isinstance(status.get("nearest_hostile"), dict) else None,
                    )
                if no_water_reentry or drowning or self._swimming_at_low_health(status):
                    shore_act = self._action(
                        "escape_to_shore",
                        lambda: self.combat_env.escape_deep_water_to_shore(
                            nh_pos,
                            keep_shield=has_shield or bool(status.get("blocking")),
                            **({"surface_first": True} if drowning else {}),
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
                    if not esc_f_ok and not esc_f_res.get("respawned") and esc_f_err not in {"not_connected", "not_ready", "wrong_server", "network_uncertain"}:
                        shore_f = self._action(
                            "escape_to_shore",
                            lambda: self.combat_env.escape_deep_water_to_shore(nh_pos, keep_shield=has_shield),
                        )
                        actions.append(shore_f)
                        shore_f_res = shore_f.get("result") if isinstance(shore_f, dict) and isinstance(shore_f.get("result"), dict) else {}
                        self._last_water_escape_failed = not bool(shore_f.get("ok") and shore_f_res.get("in_water") is not True)
                    else:
                        self._last_water_escape_failed = False
                self._last_water_escape_at = time.monotonic()

            if pre_trigger is not None and not in_lava and not in_water:
                if pre_trigger.get("source") == "pre_hurt_melee":
                    self._last_pre_hurt_melee_at = now
                self._mark_pre_hurt_cooldown(pre_trigger, now)
                sc_out = self._action(
                    "subcortical_combat",
                    lambda: self.combat_slot.handle(self.combat_env, pre_trigger),
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
                esc_err_code: str | None = None
                if (
                    not transient_err and not forced_water_shore and not no_water_reentry and not drowning
                    and not self._swimming_at_low_health(status)
                ):
                    if not _already("escape_water"):
                        actions.append(self._action("escape_water", lambda: self._escape_water(status)))
                    esc_act = next((a for a in actions if a.get("action") == "escape_water"), None)
                    esc_res = esc_act.get("result") if isinstance(esc_act, dict) and isinstance(esc_act.get("result"), dict) else {}
                    esc_succeeded = bool(esc_act and esc_act.get("ok") and esc_res.get("in_water") is not True)
                    esc_respawned = bool(esc_res.get("respawned"))
                    esc_err_code = (
                        esc_act["error"].get("code")
                        if isinstance(esc_act, dict) and isinstance(esc_act.get("error"), dict)
                        else None
                    )

                shore_succeeded = esc_succeeded
                if (
                    not transient_err
                    and not esc_succeeded
                    and not esc_respawned
                    and esc_err_code not in {"not_connected", "not_ready", "wrong_server", "network_uncertain"}
                ):
                    shore_act = self._action(
                        "escape_to_shore",
                        lambda: self.combat_env.escape_deep_water_to_shore(
                            att_pos_w, keep_shield=has_shield, **({"surface_first": True} if drowning else {}),
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
                    and not esc_respawned
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
                if can_try_fire_douse:
                    self._last_fire_douse_at = now
                    if counts.get("water_bucket", 0) > 0:
                        actions.append(self._action("douse_fire", lambda: self.combat_env.build_tactical("place_water_feet")))
                if critical_non_combat:
                    self._last_non_combat_alert_at = now
            elif effective_combat_hurt and not in_water and not in_lava:
                combat_hurt = next(
                    (h for h in reversed(other_hurts) if str(h.get("source") or "").strip().lower() not in NON_COMBAT_HURT_SOURCES),
                    None,
                )
                recent_hurt = combat_hurt or (other_hurts[-1] if other_hurts else {})
                attacker = (
                    recent_hurt.get("source_entity") or recent_hurt.get("likely_attacker")
                    if other_hurts else status.get("nearest_hostile")
                )
                hurt_src = str(recent_hurt.get("source") or "").strip().lower()
                ranged_defense = hurt_src in RANGED_HURT_SOURCES or (
                    nh_name in {"blaze", "ghast"}
                    and (hurt_src in FIRE_HURT_SOURCES or (not hurt_src and status.get("on_fire")))
                )
                hp_val = float(health) if isinstance(health, (int, float)) else 20.0
                already_blocking = bool(status.get("blocking"))
                if in_wall_hurt:
                    actions.append(self._action("unstick_in_wall", lambda: self._unstick_in_wall(status)))
                elif self._custom_combat_registered:
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
                            lambda: self.combat_slot.handle(self.combat_env, trig),
                        )
                    )
                    if hurt_src in RANGED_HURT_SOURCES or att_type in RANGED_MOBS:
                        self._mark_ranged_engagement(time.monotonic(), trigger=trig)
                elif (
                    has_shield
                    and not already_blocking
                    and not ranged_defense
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
                    if att_id_r is None and nh_name in RANGED_MOBS:
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
                    skip_cover_for_blast = (
                        hurt_src in {"fireball", "small_fireball", "wither_skull"}
                        or att_name_r in {"blaze", "ghast"}
                        or bool(status.get("on_fire"))
                    )
                    cur_blk_r = (
                        (math.floor(float(pos_r[0])), math.floor(float(pos_r[1]) + 0.05), math.floor(float(pos_r[2])))
                        if isinstance(pos_r, (list, tuple)) and len(pos_r) >= 3
                        else None
                    )
                    same_spot_cover_ineffective = (
                        cur_blk_r is not None
                        and cur_blk_r == self._last_dig_cover_block
                        and (now - self._last_dig_cover_at < 20.0)
                    )
                    already_in_dug_cover = (
                        not skip_cover_for_blast
                        and not same_spot_cover_ineffective
                        and (now < self._ranged_cover_active_until)
                        and hp_val > 10.0
                        and ranged_hurt_only
                    )
                    recent_cover_fail = already_blocking and ranged_hurt_only and hp_val > 10.0 and (now - self._last_dig_cover_fail_at < 15.0)
                    cover: dict[str, Any] | None = None
                    skills = self._skills()
                    if (
                        status.get("on_ground") is not False
                        and not skip_cover_for_blast
                        and not same_spot_cover_ineffective
                        and not already_in_dug_cover
                        and not recent_cover_fail
                    ):
                        cover = self._action("dig_cover", lambda: self.combat_env.dig_cover_pocket(skills))
                        actions.append(cover)
                        self._last_dig_cover_block = cur_blk_r
                        self._last_dig_cover_at = time.monotonic()
                        if cover.get("ok"):
                            self._mark_ranged_engagement(now, attacker=att_obj, in_cover=True)
                        else:
                            self._last_dig_cover_fail_at = now
                    cover_ok = bool(cover and cover.get("ok")) or already_in_dug_cover
                    if not cover_ok and (
                        skip_cover_for_blast or not already_blocking or hp_val <= 10.0 or not ranged_hurt_only
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
                        if not has_shield and not att_pos_r and isinstance(att_id_r, int):
                            shooter_pos = self._find_hostile_position(att_id_r, att_name_r)
                            if shooter_pos is not None:
                                attacker = {**(att_obj or {}), "pos": shooter_pos}
                        cf = self._action(
                            "combat_or_flee",
                            lambda: self._defend_or_flee(
                                status, attacker, counts, ranged=True, prefer_cover=not has_shield and hp_val <= LOW_HP_BUNKER_MAX,
                            ),
                        )
                        actions.append(cf)
                        cf_mode = (cf.get("result") or {}).get("mode")
                        if cf.get("ok") and cf_mode == "natural_cover":
                            self._mark_ranged_engagement(time.monotonic(), attacker=att_obj, in_cover=True)
                        if cf_mode == "flee":
                            self._consecutive_flees += 1
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
                    raw_dist = (attacker or status.get("nearest_hostile") or {}).get("dist", 99.0)
                    try:
                        eff_dist = float(raw_dist)
                    except (TypeError, ValueError):
                        eff_dist = 99.0
                    melee_or_airborne = (
                        (status.get("on_ground") is not True)
                        or (hurt_src in {"mob_attack", "player_explosion", "entity_explosion"})
                        or (eff_dist <= 4.5)
                    )
                    can_surface_night_bunker = (
                        status.get("on_ground") is True
                        and nh_name != "creeper"
                        and (
                            (hp_val <= LOW_HP_BUNKER_MAX and (self._inside_own_shaft() or not has_melee_weapon or self._consecutive_flees >= 1))
                            or (not has_melee_weapon and status.get("is_day") is False and self._consecutive_flees >= 1 and hp_val <= 14.0)
                        )
                    )
                    if (melee_or_airborne and can_surface_night_bunker) or not melee_or_airborne:
                        bunker = self._action("emergency_bunker", lambda: self._skills().dig_safety_bunker(urgent=True))
                        actions.append(bunker)
                        if bunker["ok"]:
                            self._consecutive_flees = 0
                            bunker_result = bunker.get("result") or {}
                            if bunker_result.get("inside_shaft") and bunker_result.get("roof_placed"):
                                self._last_melee_hurt_at = float("-inf")
                        else:
                            cf = self._action(
                                "combat_or_flee",
                                lambda: self._defend_or_flee(status, attacker, counts),
                            )
                            actions.append(cf)
                            if isinstance(cf.get("result"), dict) and cf["result"].get("mode") == "flee":
                                self._consecutive_flees += 1
                            else:
                                self._consecutive_flees = 0
                    else:
                        cf = self._action(
                            "combat_or_flee",
                            lambda: self._defend_or_flee(status, attacker, counts),
                        )
                        actions.append(cf)
                        if isinstance(cf.get("result"), dict) and cf["result"].get("mode") == "flee":
                            self._consecutive_flees += 1
                        else:
                            self._consecutive_flees = 0

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
        if actions:
            try:
                post_fresh = self._drain_events_pages()
            except (ApiError, httpx.HTTPError):
                post_fresh = []
            if post_fresh:
                fresh.extend(post_fresh)
                if any(e.get("type") in {"death", "death_loop", "disconnected", "spawn"} for e in post_fresh):
                    if "death_or_connection_changed" not in reasons:
                        reasons.append("death_or_connection_changed")
                    replan = True
        return self._incident(post_status, reasons, actions, fresh, requires_replan=replan)

    # ---- Layer 2: Active Macro-Tool & `pre_ensure` Preemption ----

    def _verify_unstick_exit(self, body: dict[str, Any]) -> dict[str, Any]:
        status = self.bot.status()
        if status.get("in_water"):
            return body
        pos = status.get("pos")
        if (
            not pos or status.get("ready") is False or status.get("alive") is False
            or status.get("on_ground") is False or status.get("in_lava")
        ):
            raise _UnsafeUnstickExit
        self.combat_env.invalidate_block_cache()
        route = self.combat_env.probe_retreat(pos, float(body["yaw"]), 2.2)
        if route is None:
            raise _UnsafeUnstickExit
        _, needs_jump = route
        return {**body, "sprint": False, "jump": needs_jump, "ms": min(int(body.get("ms") or 350), 350)}

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

    def _retreat_after_unsafe_exit(self, deadline: float | None) -> dict[str, Any] | None:
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
            if threat_pos is None:
                threat_pos = self._find_hostile_position(entity_id, name, session=bot)
            env = CombatEnv(bot, geofence=self.geofence)
            recovery = self._action(
                "unsafe_exit_retreat",
                lambda: env.flee(threat_pos, distance=8.0, allow_shaft_seal=False, wait_for_ground=ranged),
                raise_on_http_error=False,
            )
            result = recovery.get("result")
            if recovery.get("ok") and isinstance(result, dict):
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

    @contextmanager
    def _intercept_bot_calls(
        self,
        *,
        enable_creeper_watcher: bool = True,
        deadline: float | None = None,
        check_unstick_exit: bool = False,
        watch_hot_floor: bool = False,
        check_flee_exit: bool = False,
    ):
        """Intercept `interrupted_by=='hurt'` and silent creeper approach inside multi-step macro skills."""
        orig_call = getattr(self.raw_bot, "call", None)
        if not callable(orig_call) or self._in_reflex:
            yield
            return

        actor_call = orig_call
        if deadline is not None:
            actor_call = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline, call_override=orig_call).call

        stop_evt = threading.Event()
        actor_thread_id = threading.get_ident()
        pending_trigger: list[ThreatTrigger] = []

        def _watcher() -> None:
            # Delay 0.30s before first poll so fast (<5ms) offline mock calls never incur background HTTP reads
            if stop_evt.wait(0.30):
                return
            watch_hp0: float | None = None
            previous_tile = checked_hot_floor = None
            while not stop_evt.is_set():
                try:
                    st = orig_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
                    if not isinstance(st, dict):
                        if stop_evt.wait(0.30):
                            return
                        continue
                    cur_hp = st.get("health")
                    if watch_hp0 is None and isinstance(cur_hp, (int, float)):
                        watch_hp0 = float(cur_hp)
                    nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(st)
                    hp_dropped = (
                        isinstance(watch_hp0, (int, float))
                        and isinstance(cur_hp, (int, float))
                        and float(cur_hp) <= float(watch_hp0) - 1.0
                        and bool(nh_name)
                        and nh_dist <= 16.0
                    )
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
                    in_lava_now = bool(st.get("in_lava"))
                    hot_floor_now = False
                    if watch_hot_floor and not (in_lava_now or creeper_close or melee_ambush or hp_dropped):
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
                    if in_lava_now or creeper_close or melee_ambush or hp_dropped or hot_floor_now:
                        if in_lava_now:
                            src_w, eid_w, tp_w, pos_w, dist_w = "boundary_threat", None, "lava", None, 0.0
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
                            }
                        )
                        try:
                            orig_call("stop", {}, allow_reauth=False, timeout_s=1.5)
                        except Exception:
                            pass
                        return
                except Exception:
                    pass
                if stop_evt.wait(0.30):
                    return

        watcher_thread: threading.Thread | None = None
        if enable_creeper_watcher:
            watcher_thread = threading.Thread(target=_watcher, name="mc-creeper-watch", daemon=True)
            watcher_thread.start()

        def _wrapped_call(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            clean_path = str(path or "").lstrip("/")
            is_actor_thread = threading.get_ident() == actor_thread_id
            is_readonly_poll = (
                str(kwargs.get("method") or "").upper() == "GET"
                or clean_path in READ_ONLY_POLL_ENDPOINTS
            )
            if not is_actor_thread:
                return orig_call(path, body, **kwargs)
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
            if is_readonly_poll:
                return actor_call(path, body, **kwargs)
            if pending_trigger and not self._in_reflex:
                raise ReflexPreempted(pending_trigger[0])
            if (
                check_unstick_exit and clean_path == "control" and isinstance(body, dict)
                and body.get("forward") and body.get("stop_on") == [] and isinstance(body.get("yaw"), (int, float))
            ):
                body = self._verify_unstick_exit(body)
                if pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0])
            # Do not preempt explicit stop_on=['death'] emergency swim/trap digs
            caller_ignores_hurt = isinstance(body, dict) and body.get("stop_on") == ["death"]
            try:
                res = actor_call(path, body, **kwargs)
            except _RecoveryBudgetExceeded:
                if pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0]) from None
                raise
            except ApiError as err:
                if not self._in_reflex and not caller_ignores_hurt:
                    if pending_trigger:
                        raise ReflexPreempted(pending_trigger[0]) from None
                    if err.code == "interrupted" and clean_path in ACTION_LOCK_ENDPOINTS:
                        st_err = {}
                        try:
                            st_err = actor_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
                        except Exception:
                            pass
                        if isinstance(st_err, dict) and bool(st_err.get("in_lava")):
                            raise ReflexPreempted(
                                {
                                    "source": "boundary_threat",
                                    "primary_entity_id": None,
                                    "primary_type": "lava",
                                    "primary_pos": None,
                                    "distance": 0.0,
                                }
                            ) from None
                        nh_n, nh_d, nh_i, nh_p = self._extract_hostile_info(st_err)
                        if nh_n and nh_d <= 14.0:
                            raise ReflexPreempted(
                                {
                                    "source": "tool_interrupted_hurt",
                                    "primary_entity_id": nh_i,
                                    "primary_type": nh_n,
                                    "primary_pos": nh_p,
                                    "distance": nh_d,
                                }
                            ) from None
                raise

            if not self._in_reflex and not caller_ignores_hurt:
                if pending_trigger:
                    raise ReflexPreempted(pending_trigger[0])
                if isinstance(res, dict) and res.get("interrupted_by") == "hurt" and clean_path in ACTION_LOCK_ENDPOINTS:
                    st_int = {}
                    try:
                        st_int = actor_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
                    except Exception:
                        pass
                    nh_n, nh_d, nh_i, nh_p = self._extract_hostile_info(st_int)
                    if nh_n and nh_d <= 15.0:
                        raise ReflexPreempted(
                            {
                                "source": "tool_interrupted_hurt",
                                "primary_entity_id": nh_i,
                                "primary_type": nh_n,
                                "primary_pos": nh_p,
                                "distance": nh_d,
                            }
                        )
            return res

        try:
            self.raw_bot.call = _wrapped_call
            yield
        finally:
            stop_evt.set()
            self.raw_bot.call = orig_call
            if watcher_thread is not None:
                watcher_thread.join(timeout=0.05)

    def handle_preemption(self, trigger: ThreatTrigger, *, interrupted_name: str) -> dict[str, Any]:
        """Execute full 0-LLM subcortical combat/evasion takeover on the main thread (no tool auto-replay)."""
        self._in_reflex = True
        try:
            self.stats["tool_preemptions"] += 1
            try:
                st_pre = self.bot.status()
            except (ApiError, httpx.HTTPError):
                st_pre = {}
            if trigger.get("primary_type") == "lava" or bool(st_pre.get("in_lava")):
                act = self._action("escape_lava", lambda: self.combat_env.escape_lava(), raise_on_http_error=False)
                act_res = act.get("result") if isinstance(act.get("result"), dict) else {}
                try:
                    st_after = self.bot.status()
                except (ApiError, httpx.HTTPError):
                    st_after = st_pre
                hp_pre_l = st_pre.get("health")
                hp_post_l = st_after.get("health")
                dim_pre_l = str(st_pre.get("dimension") or "").lower()
                dim_post_l = str(st_after.get("dimension") or "").lower()
                ld_post_l = st_after.get("last_death")
                lava_died = bool(
                    act_res.get("respawned")
                    or st_after.get("alive") is False
                    or st_after.get("ready") is False
                    or (dim_pre_l and dim_post_l and dim_pre_l != dim_post_l)
                    or (ld_post_l is not None and ld_post_l != st_pre.get("last_death"))
                    or (
                        isinstance(hp_pre_l, (int, float))
                        and isinstance(hp_post_l, (int, float))
                        and float(hp_pre_l) < 18.0
                        and float(hp_post_l) >= 19.5
                        and (float(hp_post_l) - float(hp_pre_l)) >= 2.0
                    )
                )
                esc_ok = bool(act.get("ok") and not act_res.get("in_lava") and not lava_died)
                if lava_died:
                    summary = f"执行 {interrupted_name} 途中滑入岩浆，抢占逃生期间已死亡并重生（装备已掉落，先重整背包再继续）"
                elif esc_ok:
                    summary = f"执行 {interrupted_name} 途中滑入岩浆，已中断原工具并脱困，当前 HP={st_after.get('health')}"
                else:
                    summary = (
                        f"执行 {interrupted_name} 途中滑入岩浆，已中断原工具但尚未完全脱困"
                        f"（in_lava={act_res.get('in_lava', st_after.get('in_lava'))}），当前 HP={st_after.get('health')}"
                    )
                outcome = {
                    "handled": esc_ok,
                    "mode": "lava_escape",
                    "summary": summary,
                    "actions": [act],
                    "status_after": st_after,
                    **({"respawned": True} if lava_died else {}),
                }
            elif trigger.get("primary_type") == "hot_floor":
                actions = []
                st_after = st_pre
                if st_pre.get("ready") is not False and st_pre.get("alive") is not False and (
                    st_pre.get("on_ground") is True and not st_pre.get("in_water") and st_pre.get("pos")
                ):
                    self.combat_env.invalidate_block_cache()
                    if self.combat_env.is_on_hot_floor(st_pre["pos"]):
                        actions.append(self._action("hot_floor_sneak", lambda: self._sneak_on_hot_floor(st_pre)))
                        self._hot_floor_retry_at = time.monotonic() + self.HOT_FLOOR_RETRY_S
                        try:
                            st_after = self.bot.status()
                        except (ApiError, httpx.HTTPError):
                            pass
                summary = f"执行 {interrupted_name} 途中踩到岩浆块，已中断当前动作，当前 HP={st_after.get('health')}"
                outcome = {"handled": bool(actions and actions[0].get("ok")), "mode": "hot_floor", "actions": actions}
            else:
                now_pre = time.monotonic()
                oxy_pre = st_pre.get("oxygen")
                hurt_pre_water = trigger.get("source") in {"tool_interrupted_hurt", "boundary_threat", "pre_hurt_ranged"}
                drowning_pre = (
                    (isinstance(oxy_pre, (int, float)) and oxy_pre <= 0) or trigger.get("hurt_source") == "drown"
                )
                underwater_danger = bool(st_pre.get("in_water")) and (
                    bool(st_pre.get("submerged"))
                    or (isinstance(oxy_pre, (int, float)) and oxy_pre < 180)
                    or drowning_pre
                    or trigger.get("primary_type") == "drowned"
                    or hurt_pre_water
                    or now_pre < self._no_water_until
                    or self._water_hurt_streak >= 2
                )
                if underwater_danger:
                    pre_actions: list[dict[str, Any]] = []
                    if hurt_pre_water:
                        self._water_hurt_streak += 1
                    forced_pre_shore = (
                        self._water_hurt_streak >= 3 or now_pre < self._no_water_until
                        or self._swimming_at_low_health(st_pre)
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
                    if not forced_pre_shore and not drowning_pre:
                        esc_act = self._action("escape_water", lambda: self._escape_water(st_pre), raise_on_http_error=False)
                        pre_actions.append(esc_act)
                        esc_pre_res = esc_act.get("result") if isinstance(esc_act, dict) and isinstance(esc_act.get("result"), dict) else {}
                        esc_pre_ok = bool(esc_act.get("ok") and esc_pre_res.get("in_water") is not True)
                        esc_pre_respawned = bool(esc_pre_res.get("respawned"))
                    if not esc_pre_ok and not esc_pre_respawned:
                        shore_pre = self._action(
                            "escape_to_shore",
                            lambda: self.combat_env.escape_deep_water_to_shore(
                                trigger.get("primary_pos") or (att_pre.get("pos") if isinstance(att_pre, dict) else None),
                                keep_shield=has_shield_pre,
                                **({"surface_first": True} if drowning_pre else {}),
                            ),
                            raise_on_http_error=False,
                        )
                        pre_actions.append(shore_pre)
                        shore_result = shore_pre.get("result") or {}
                        esc_pre_respawned = shore_result.get("mode") == "respawned"
                    try:
                        st_after = self.bot.status()
                    except (ApiError, httpx.HTTPError):
                        st_after = st_pre
                    left_water = (
                        not bool(st_after.get("in_water"))
                        and not st_after.get("in_lava")
                        and st_after.get("on_ground") is not False
                        and st_after.get("ready", True)
                        and st_after.get("alive") is not False
                        and not esc_pre_respawned
                    )
                    self._last_water_escape_at = time.monotonic()
                    self._last_water_escape_failed = not left_water
                    if left_water:
                        self._water_hurt_streak = 0
                    recovery = "已脱离水域" if left_water else f"上浮后 in_water={bool(st_after.get('in_water'))}"
                    summary = f"执行 {interrupted_name} 途中水下遇险，{recovery}，当前 HP={st_after.get('health')}"
                    outcome = {"handled": True, "mode": "water_surface", "summary": summary, "actions": pre_actions, "status_after": st_after}
                else:
                    act = self._action(
                        "subcortical_combat",
                        lambda: self.combat_slot.handle(self.combat_env, trigger),
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
                        summary = f"执行 {interrupted_name} 途中遇袭，小脑接管超时已安全止步（{act['error'].get('message')}）"
                        outcome = {"handled": False, "mode": "network_timeout", "summary": summary, "actions": [act], "status_after": st_after}
                    else:
                        outcome = act.get("result") if isinstance(act.get("result"), dict) else {"handled": True, "actions": [act]}
                        try:
                            st_after = outcome.get("status_after") or self.bot.status()
                        except (ApiError, httpx.HTTPError):
                            st_after = st_pre
                        if isinstance(st_after, dict) and st_after.get("in_lava"):
                            self._action("escape_lava", lambda: self.combat_env.escape_lava(), raise_on_http_error=False)
                            try:
                                st_after = self.bot.status()
                            except (ApiError, httpx.HTTPError):
                                pass
                        elif (
                            isinstance(st_after, dict)
                            and st_after.get("in_water")
                            and (self._custom_combat_registered or outcome.get("mode") not in {"water_surface", "water_surface_abort"})
                            and (
                                st_after.get("submerged")
                                or (isinstance(st_after.get("oxygen"), (int, float)) and st_after["oxygen"] < 180)
                                or time.monotonic() < self._no_water_until
                            )
                        ):
                            esc_post = self._action("escape_water", lambda: self._escape_water(st_after), raise_on_http_error=False)
                            esc_post_res = esc_post.get("result") if isinstance(esc_post.get("result"), dict) else {}
                            if not esc_post_res.get("respawned") and not (esc_post.get("ok") and esc_post_res.get("in_water") is not True):
                                self._action(
                                    "escape_to_shore",
                                    lambda: self.combat_env.escape_deep_water_to_shore(trigger.get("primary_pos")),
                                    raise_on_http_error=False,
                                )
                            try:
                                st_after = self.bot.status()
                            except (ApiError, httpx.HTTPError):
                                pass
                        summary = str(outcome.get("summary") or f"执行 {interrupted_name} 途中遇袭，已完成 0-LLM 本能处置")
            self.last_reaction = time.monotonic()
            self._mark_pre_hurt_cooldown(trigger, self.last_reaction)
            self.previous_health = float(st_after.get("health")) if isinstance(st_after.get("health"), (int, float)) else None
            try:
                self._drain_events_pages()
            except (KeyboardInterrupt, RunTimedOut, RequestCancelled):
                raise
            except Exception:
                pass
            receipt = {
                "ok": False,
                "requires_replan": True,
                "preempted_by_reflex": True,
                "interrupted_tool": interrupted_name,
                "reflex_mode": outcome.get("mode"),
                "message": f"[小脑本能抢占接管] {summary}（原工具未自动重放，请基于当前最新状态决策下一步）。",
                "status_after": {
                    k: st_after.get(k)
                    for k in ("pos", "health", "food", "in_water", "on_ground", "is_day", "nearest_hostile")
                },
            }
            self.events.emit("guardian.preempted_tool", tool=interrupted_name, mode=outcome.get("mode"), summary=summary)
            return receipt
        finally:
            self._in_reflex = False

    def run_tool_with_reflex(self, tool_name: str, invoke_fn: Callable[[], str]) -> str:
        """Wrap an exclusive tool invocation with mid-execution subcortical preemption."""
        if tool_name == "mc_attack" and not self._in_reflex:
            receipt = self._protect_attack_on_hot_floor()
            if receipt is not None:
                return receipt
        is_flee = tool_name == "mc_skill_flee"
        if (tool_name in NON_PREEMPTABLE_TOOLS and not is_flee) or self._in_reflex:
            return invoke_fn()
        is_unstick = tool_name == "mc_skill_unstick"
        budget_s = None
        if is_flee:
            budget_s = self.FLEE_BUDGET_S
        elif is_unstick:
            budget_s = self.UNSTICK_BUDGET_S
        # Leave time for the watcher to exit and for the final bounded stop request.
        deadline = time.monotonic() + budget_s - 2.0 if budget_s is not None else None
        try:
            with self._intercept_bot_calls(
                enable_creeper_watcher=not is_flee,
                deadline=deadline,
                check_unstick_exit=is_unstick,
                watch_hot_floor=tool_name in {"mc_skill_mine_dfs", "mc_skill_staircase"},
                check_flee_exit=is_flee,
            ):
                result = invoke_fn()
                if deadline is not None and time.monotonic() >= deadline:
                    raise _RecoveryBudgetExceeded
                return result
        except (_UnsafeFleeExit, _UnsafeUnstickExit):
            self.events.emit("guardian.flee_exit_blocked" if is_flee else "guardian.unstick_exit_blocked", tool=tool_name)
            recovery = self._retreat_after_unsafe_exit(deadline)
            if recovery is not None:
                return json.dumps(recovery, ensure_ascii=False)
            message = (
                "撤退方向缺少安全落脚点，请改从有支撑的方向离开。"
                if is_flee else "脱困出口缺少安全落脚点，请从当前位置寻找另一条路线。"
            )
            return json.dumps({
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
            }, ensure_ascii=False)
        except RunTimedOut:
            if is_flee:
                self._stop_after_recovery_timeout()
            raise
        except ReflexPreempted as preempt:
            receipt = self.handle_preemption(preempt.trigger, interrupted_name=tool_name)
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

        now_b = time.monotonic()
        in_water = bool(status.get("in_water"))
        raw_in_lava = bool(status.get("in_lava"))
        oxygen = status.get("oxygen")
        hurts = [e for e in fresh if e.get("type") == "hurt"]
        other_hurts = [e for e in hurts if str(e.get("source") or "").strip().lower() != "hot_floor"]
        hurt_by_lava = any(str(h.get("source") or "").strip().lower() in LAVA_HURT_SOURCES for h in hurts)
        in_lava = raw_in_lava or hurt_by_lava
        hot_floor_only = bool(hurts) and not other_hurts and not in_water and not in_lava
        needs_hot_floor_guard = (
            len(other_hurts) < len(hurts) and not in_water and not in_lava
            and self._needs_hot_floor_stance(status, now_b)
        )
        drowning = in_water and (
            (isinstance(oxygen, (int, float)) and oxygen <= 0)
            or any(str(h.get("source") or "").strip().lower() == "drown" for h in hurts)
        )
        nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)

        if not in_water and not in_lava:
            self._water_hurt_streak = 0
            self._last_water_escape_failed = False

        no_water_reentry_b = in_water and now_b < self._no_water_until
        ended_wet_in_payload = any('"in_water": true' in str(r or "").lower() for r in (tool_results or []))
        handled_water_in_tool = False
        unstick_still_wet = False
        if in_water and tool_results:
            try:
                last_tool_result = json.loads(tool_results[-1])
            except (TypeError, ValueError):
                last_tool_result = None
            if isinstance(last_tool_result, dict):
                tool_status = last_tool_result.get("status_after")
                handled_water_in_tool = (
                    last_tool_result.get("preempted_by_reflex") is True
                    and last_tool_result.get("reflex_mode") == "water_surface"
                    and isinstance(tool_status, dict) and tool_status.get("in_water") is True
                )
                tool_error = last_tool_result.get("error")
                unstick_still_wet = (
                    isinstance(tool_error, dict) and tool_error.get("code") == "water_escape_failed"
                    and last_tool_result.get("in_water") is True
                )
        retry_water_escape = not handled_water_in_tool or drowning or bool(hurts) or bool(status.get("submerged"))
        needs_water_escape = in_water and retry_water_escape and (
            not status.get("on_ground")
            or status.get("submerged")
            or ended_wet_in_payload
            or no_water_reentry_b
            or bool(hurts)
            or (isinstance(oxygen, (int, float)) and oxygen < 270)
        )

        boundary_trigger: ThreatTrigger | None = None if (in_water or in_lava or hot_floor_only) else self._detect_pre_hurt_trigger(status)
        if boundary_trigger is None and not in_water and not in_lava and other_hurts and nh_name and nh_dist <= 15.0:
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
            not (hot_floor_only or needs_hot_floor_guard)
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
            elif needs_lava_edge_guard and not in_wall_hurt:
                edge_act = self._action(
                    "boundary_guard_lava_edge",
                    lambda: self.combat_env.guard_lava_edge(pos_b),
                    raise_on_http_error=False,
                )
                edge_res = edge_act.get("result") if isinstance(edge_act.get("result"), dict) else {}
                if edge_res.get("near_lava"):
                    notes.append("岩浆边潜行封边" if edge_res.get("sealed") else "岩浆边强制潜行")
            elif needs_water_escape:
                if hurts:
                    self._water_hurt_streak += 1
                    if self._water_hurt_streak >= 3:
                        self._no_water_until = max(self._no_water_until, now_b + 30.0)
                        self.events.emit("guardian.no_water_cooldown", duration_s=30.0, streak=self._water_hurt_streak)
                has_shield_b = False
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
                forced_shore_b = (
                    self._water_hurt_streak >= 3 or no_water_reentry_b or drowning
                    or handled_water_in_tool or unstick_still_wet or self._swimming_at_low_health(status)
                    or continuing_water_escape
                )
                esc_b_ok = False
                esc_b_respawned = False
                if not forced_shore_b:
                    esc = self._action("boundary_escape_water", lambda: self._escape_water(status), raise_on_http_error=False)
                    esc_b_res = esc.get("result") if isinstance(esc.get("result"), dict) else {}
                    esc_b_ok = bool(esc.get("ok") and esc_b_res.get("in_water") is not True)
                    esc_b_respawned = bool(esc_b_res.get("respawned"))
                if esc_b_ok:
                    still_wet = False
                    self._water_hurt_streak = 0
                    self._last_water_escape_failed = False
                    notes.append("自动浮水登岸")
                elif not esc_b_respawned:
                    if nh_pos is None and nh_name in RANGED_MOBS and nh_dist <= 16.0:
                        nh_pos = self._find_hostile_position(nh_id, nh_name)
                    shore_b = self._action(
                        "boundary_escape_to_shore",
                        lambda: self.combat_env.escape_deep_water_to_shore(
                            nh_pos, keep_shield=has_shield_b, **({"surface_first": True} if surface_first else {}),
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
                self._mark_pre_hurt_cooldown(boundary_trigger, time.monotonic())
                act = self._action(
                    "boundary_subcortical_combat",
                    lambda: self.combat_slot.handle(self.combat_env, boundary_trigger),
                    raise_on_http_error=False,
                )
                self._mark_pre_hurt_cooldown(boundary_trigger, time.monotonic())
                res_d = act.get("result") if isinstance(act.get("result"), dict) else {}
                if boundary_trigger.get("source") == "pre_hurt_ranged" or str(boundary_trigger.get("primary_type") or "") in RANGED_MOBS:
                    self._mark_ranged_engagement(
                        time.monotonic(),
                        trigger=boundary_trigger,
                        in_cover=res_d.get("mode") in {"cover_pocket", "bunker_sealed", "sealed_shaft"},
                    )
                if act.get("ok"):
                    notes.append(str(res_d.get("summary") or f"处置近身威胁 {nh_name}"))
                elif isinstance(act.get("error"), dict):
                    notes.append(f"近身处置受阻({act['error'].get('code')})")

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
