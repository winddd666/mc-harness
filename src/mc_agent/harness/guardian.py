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
import json
import math
import threading
import time
from typing import Any, Callable

import httpx

from ..errors import ApiError
from ..skills.combat_env import (
    LONE_COUNTERKILL_MOBS,
    MELEE_WEAPON_ORDER,
    RANGED_MOBS,
    CombatController,
    CombatControllerSlot,
    CombatEnv,
    ThreatTrigger,
)
from ..skills.library import MacroSkills
from ..skills.safety import SAFE_FOOD_PRIORITY

# Hurt sources that mean "my body is embedded in a block", not "something is attacking me".
IN_WALL_HURT_SOURCES = frozenset({"in_wall", "suffocation", "cramming", "inside_block"})

# At or below this health, stop trying to walk away while being hit: seal up in place instead.
LOW_HP_BUNKER_MAX = 10.0

# Ranged damage: sprinting away in open cave air just makes the firing line longer.
RANGED_HURT_SOURCES = frozenset({"arrow", "projectile", "trident", "fireball", "small_fireball", "skeleton"})

# Tools that already manage combat/stabilization directly and must not be recursively preempted.
NON_PREEMPTABLE_TOOLS = frozenset(
    {
        "mc_attack",
        "mc_shoot",
        "mc_posture",
        "mc_look",
        "mc_stop",
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


class ReflexPreempted(BaseException):
    """Internal signal raised to unwind an active macro skill into 0-LLM subcortical takeover."""

    def __init__(self, trigger: ThreatTrigger) -> None:
        super().__init__(f"Reflex preempted by {trigger.get('source')}: {trigger.get('primary_type')}")
        self.trigger = trigger


class _GuardSession:
    def __init__(self, bot: Any, checkpoint: Callable[[], None]) -> None:
        self.bot = bot
        self.checkpoint = checkpoint

    def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        self.checkpoint()
        readonly = action in {"status", "events", "inventory", "get_entities", "get_block", "find_blocks"}
        kwargs.update(allow_reauth=False, timeout_s=2.0 if readonly else 8.0)
        result = self.bot.call(action, body, **kwargs)
        self.checkpoint()
        return result

    def status(self) -> dict[str, Any]:
        return self.call("status", method="GET")

    def inventory(self) -> dict[str, Any]:
        return self.call("inventory", method="GET")


class GenerationGuard:
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
        self.combat_env = CombatEnv(self.bot, skills=self.skills, geofence=self.geofence)
        self.combat_slot = CombatControllerSlot(combat_controller)
        self._custom_combat_registered = combat_controller is not None
        self.last_poll = float("-inf")
        self.last_reaction = float("-inf")
        self.previous_health: float | None = None
        self.previous_ready: bool | None = None
        self.cursor = int(getattr(bot, "_last_event_seq", 0) or 0)
        self.failures = 0
        self.disconnected = False
        self._in_reflex = False
        self._pre_hurt_suppress_block: tuple[int, int, int] | None = None
        self._pre_hurt_suppress_until: float = float("-inf")
        self.stats = {
            "polls": 0,
            "incidents": 0,
            "actions": 0,
            "cancelled_generations": 0,
            "tool_preemptions": 0,
            "boundary_stabilizations": 0,
        }

    def register_combat_controller(self, controller: CombatController) -> None:
        self.combat_slot.register(controller)
        self._custom_combat_registered = True

    def prime(self) -> None:
        """Drain historical `/events` cursor and record baseline health before guarded execution."""
        try:
            st = self.bot.status()
            self.previous_health = float(st["health"]) if isinstance(st.get("health"), (int, float)) else None
            self.previous_ready = bool(st.get("ready", st.get("alive", True)))
            self._drain_events_pages()
            setattr(self.raw_bot, "_last_event_seq", self.cursor)
        except Exception:
            pass

    def _skills(self) -> Any:
        """Live MacroSkills (with MiningDFS shaft state) when wired, else a throwaway binding."""
        return self.skills if self.skills is not None else MacroSkills(self.bot)

    def _inside_own_shaft(self) -> bool:
        """Only bunker-in-place when we are inside our own carved tunnel (never in open cave air)."""
        eng = getattr(self.skills, "mining_dfs", None) if self.skills is not None else None
        if eng is None or not getattr(eng, "nodes", None):
            return False
        try:
            return bool(eng.is_inside_shaft())
        except Exception:
            return False

    def _action(self, name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
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
        except httpx.HTTPError as error:
            raise RuntimeError(f"生存响应 {name} 的网络结果不确定：{type(error).__name__}") from None
        self.stats["actions"] += 1
        self.events.emit("guardian.action.completed", duration=time.monotonic() - started, **outcome)
        return outcome

    def _drain_events_pages(self) -> list[dict[str, Any]]:
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

    def _detect_pre_hurt_trigger(self, status: dict[str, Any]) -> ThreatTrigger | None:
        nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)
        if not nh_name:
            return None
        is_creeper = nh_name == "creeper" and nh_dist <= 4.5
        is_melee = nh_name in LONE_COUNTERKILL_MOBS and nh_dist <= 3.3
        is_ranged = nh_name in RANGED_MOBS and nh_dist <= 13.5
        if not (is_creeper or is_melee or is_ranged):
            return None

        pos = status.get("pos")
        cur_blk: tuple[int, int, int] | None = None
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            cur_blk = (int(math.floor(float(pos[0]))), int(math.floor(float(pos[1]))), int(math.floor(float(pos[2]))))
            if (
                self._pre_hurt_suppress_block == cur_blk
                and time.monotonic() < self._pre_hurt_suppress_until
                and not (is_creeper and nh_dist <= 2.3)
            ):
                return None

        # Live `/status` omits `nearest_hostile.pos`: resolve 3D entity coords via `get_entities` so `has_los` works on all mobs!
        if nh_pos is None:
            try:
                ent_resp = self.bot.call("get_entities", {"radius": 16})
                for th in (ent_resp or {}).get("entities") or []:
                    if not isinstance(th, dict):
                        continue
                    th_nm = str(th.get("name") or "").removeprefix("minecraft:").strip().lower()
                    if (nh_id is not None and th.get("id") == nh_id) or (nh_id is None and th_nm == nh_name):
                        tpos = th.get("pos")
                        if isinstance(tpos, (list, tuple)) and len(tpos) >= 3:
                            nh_pos = list(tpos)
                            break
            except Exception:
                nh_pos = None

        # Never fire `pre_hurt_*` through solid bunker walls! Real damage still triggers `guard_poll_hurt` unconditionally.
        if isinstance(pos, (list, tuple)) and len(pos) >= 3 and isinstance(nh_pos, (list, tuple)) and len(nh_pos) >= 3:
            self.combat_env.invalidate_block_cache()
            los = self.combat_env.has_los(pos, nh_pos)
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
        deaths = [e for e in fresh if e.get("type") in {"death", "death_loop", "disconnected", "spawn"}]
        in_water = bool(status.get("in_water"))
        in_lava = bool(status.get("in_lava"))
        oxygen = status.get("oxygen")
        needs_float = in_water and not status.get("jumping") and (
            status.get("submerged") or not status.get("on_ground") or (isinstance(oxygen, (int, float)) and oxygen < 240)
        )
        food = status.get("food")
        needs_food = isinstance(food, (int, float)) and (food <= 16 or (isinstance(health, (int, float)) and health < 18 and food < 20))
        hurt = bool(hurts) or dropped
        if hurt:
            self._pre_hurt_suppress_until = float("-inf")

        pre_trigger = None if (hurt or deaths or disconnected or not ready) else self._detect_pre_hurt_trigger(status)
        critical_override = bool(
            deaths
            or in_lava
            or (pre_trigger is not None and pre_trigger.get("source") == "pre_hurt_creeper")
            or any(str(h.get("source") or "") in IN_WALL_HURT_SOURCES for h in hurts)
        )

        if not (hurt or deaths or disconnected or needs_float or needs_food or in_lava or pre_trigger is not None):
            return None
        if now - self.last_reaction < 3.0 and not critical_override:
            return None

        actions: list[dict[str, Any]] = []
        reasons: list[str] = []
        if hurt:
            reasons.append("fresh_hurt")
        if pre_trigger is not None:
            reasons.append(str(pre_trigger["source"]))
        if in_lava:
            reasons.append("in_lava")
        if deaths or disconnected:
            reasons.append("death_or_connection_changed")
        if needs_float:
            reasons.append("water_float")

        if deaths or disconnected or not ready or status.get("alive") is False:
            return self._incident(status, reasons, actions, fresh)

        if status.get("busy") and not (in_lava or (pre_trigger and pre_trigger.get("source") == "pre_hurt_creeper")):
            return self._incident(status, reasons + ["unexpected_action_still_busy"], actions, fresh)

        self._in_reflex = True
        try:
            try:
                inv = self.bot.inventory() if (hurt or needs_food or in_lava or pre_trigger is not None) else {}
            except (ApiError, httpx.HTTPError):
                inv = {}
            counts, has_shield, has_melee_weapon = self._parse_inventory_summary(inv)

            if in_lava:
                if counts.get("water_bucket", 0) > 0:
                    actions.append(self._action("douse_lava", lambda: self.combat_env.build_tactical("place_water_feet")))
                actions.append(self._action("escape_lava", lambda: self._skills().escape_water()))

            if needs_float and not in_lava:
                actions.append(self._action("escape_water", lambda: self._skills().escape_water()))

            def _already(name: str) -> bool:
                return any(a.get("action") == name for a in actions)

            nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)

            if pre_trigger is not None and not in_lava and not in_water:
                sc_out = self._action(
                    "subcortical_combat",
                    lambda: self.combat_slot.handle(self.combat_env, pre_trigger),
                )
                actions.append(sc_out)
                sc_res = sc_out.get("result") if isinstance(sc_out, dict) else None
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
                if not _already("escape_water"):
                    actions.append(self._action("escape_water", lambda: self._skills().escape_water()))
            elif hurt and not in_water and not in_lava:
                attacker = (hurts[-1].get("source_entity") or hurts[-1].get("likely_attacker")) if hurts else status.get("nearest_hostile")
                hurt_src = str(hurts[-1].get("source") or "") if hurts else ""
                if hurt_src in IN_WALL_HURT_SOURCES:
                    actions.append(self._action("unstick_in_wall", lambda: self._skills().unstick()))
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
                elif has_shield:
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
                elif not has_shield and hurt_src in RANGED_HURT_SOURCES:
                    cover = self._action("dig_cover", lambda: self._skills().dig_cover_pocket())
                    actions.append(cover)
                    if not cover["ok"]:
                        actions.append(
                            self._action(
                                "combat_or_flee",
                                lambda: self._skills().defend_or_flee(
                                    attacker=attacker,
                                    counts=counts,
                                    health=health,
                                    urgent=True,
                                    geofence=self.geofence,
                                ),
                            )
                        )
                else:
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
                    hp_val = float(health) if isinstance(health, (int, float)) else 20.0
                    if melee_or_airborne and hp_val <= LOW_HP_BUNKER_MAX and self._inside_own_shaft():
                        bunker = self._action("emergency_bunker", lambda: self._skills().dig_safety_bunker(urgent=True))
                        actions.append(bunker)
                        if not bunker["ok"]:
                            actions.append(
                                self._action(
                                    "combat_or_flee",
                                    lambda: self._skills().defend_or_flee(
                                        attacker=attacker,
                                        counts=counts,
                                        health=health,
                                        urgent=True,
                                        geofence=self.geofence,
                                    ),
                                )
                            )
                    elif not melee_or_airborne:
                        bunker = self._action("emergency_bunker", lambda: self._skills().dig_safety_bunker(urgent=True))
                        actions.append(bunker)
                        if not bunker["ok"]:
                            actions.append(
                                self._action(
                                    "combat_or_flee",
                                    lambda: self._skills().defend_or_flee(
                                        attacker=attacker,
                                        counts=counts,
                                        health=health,
                                        urgent=True,
                                        geofence=self.geofence,
                                    ),
                                )
                            )
                    else:
                        actions.append(
                            self._action(
                                "combat_or_flee",
                                lambda: self._skills().defend_or_flee(
                                    attacker=attacker,
                                    counts=counts,
                                    health=health,
                                    urgent=True,
                                    geofence=self.geofence,
                                ),
                            )
                        )

            if needs_food and (
                not hurt
                or any(
                    a["action"] in {"emergency_bunker", "combat_or_flee", "subcortical_combat"} and a["ok"]
                    for a in actions
                )
            ):
                best_food = next((name for name in SAFE_FOOD_PRIORITY if counts.get(name, 0) > 0), None)
                if best_food:
                    reasons.append("needs_food")
                    actions.append(self._action("eat", lambda: self.bot.call("use_item", {"item": best_food})))
        finally:
            self._in_reflex = False

        if not actions and not reasons:
            return None
        replan = bool(
            hurt
            or pre_trigger is not None
            or in_lava
            or deaths
            or disconnected
            or needs_float
            or not ready
            or status.get("alive") is False
            or any(r != "needs_food" for r in reasons)
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

    @contextmanager
    def _intercept_bot_calls(self, *, enable_creeper_watcher: bool = True):
        """Intercept `interrupted_by=='hurt'` and silent creeper approach inside multi-step macro skills."""
        orig_call = getattr(self.raw_bot, "call", None)
        if not callable(orig_call) or self._in_reflex:
            yield
            return

        stop_evt = threading.Event()
        pending_trigger: list[ThreatTrigger] = []

        def _watcher() -> None:
            # Delay 0.30s before first poll so fast (<5ms) offline mock calls never incur background HTTP reads
            if stop_evt.wait(0.30):
                return
            while not stop_evt.is_set():
                try:
                    st = orig_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
                    nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(st if isinstance(st, dict) else {})
                    if isinstance(st, dict) and (
                        bool(st.get("in_lava")) or (nh_name == "creeper" and nh_dist <= 4.5)
                    ):
                        pending_trigger.append(
                            {
                                "source": "pre_hurt_creeper" if nh_name == "creeper" else "boundary_threat",
                                "primary_entity_id": nh_id,
                                "primary_type": nh_name or "lava",
                                "primary_pos": nh_pos,
                                "distance": nh_dist,
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
            if pending_trigger and not self._in_reflex:
                raise ReflexPreempted(pending_trigger[0])
            clean_path = str(path or "").lstrip("/")
            # Do not preempt explicit stop_on=['death'] emergency swim/trap digs
            caller_ignores_hurt = isinstance(body, dict) and body.get("stop_on") == ["death"]
            try:
                res = orig_call(path, body, **kwargs)
            except ApiError as err:
                if not self._in_reflex and not caller_ignores_hurt:
                    if pending_trigger:
                        raise ReflexPreempted(pending_trigger[0]) from None
                    if err.code == "interrupted" and clean_path in ACTION_LOCK_ENDPOINTS:
                        st_err = {}
                        try:
                            st_err = orig_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
                        except Exception:
                            pass
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
                        st_int = orig_call("status", None, method="GET", allow_reauth=False, timeout_s=1.5)
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
            if trigger.get("primary_type") == "lava":
                act = self._action("escape_lava", lambda: self._skills().escape_water())
                st_after = self.bot.status()
                summary = f"执行 {interrupted_name} 途中滑入岩浆，已中断原工具并脱困，当前 HP={st_after.get('health')}"
                outcome = {"handled": True, "mode": "lava_escape", "summary": summary, "actions": [act], "status_after": st_after}
            else:
                act = self._action("subcortical_combat", lambda: self.combat_slot.handle(self.combat_env, trigger))
                outcome = act.get("result") if isinstance(act.get("result"), dict) else {"handled": True, "actions": [act]}
                st_after = outcome.get("status_after") or self.bot.status()
                summary = str(outcome.get("summary") or f"执行 {interrupted_name} 途中遇袭，已完成 0-LLM 本能处置")
            self.last_reaction = time.monotonic()
            self.previous_health = float(st_after.get("health")) if isinstance(st_after.get("health"), (int, float)) else None
            try:
                self._drain_events_pages()
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
        if tool_name in NON_PREEMPTABLE_TOOLS or self._in_reflex:
            return invoke_fn()
        try:
            with self._intercept_bot_calls(enable_creeper_watcher=True):
                return invoke_fn()
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

        in_water = bool(status.get("in_water"))
        in_lava = bool(status.get("in_lava"))
        oxygen = status.get("oxygen")
        hurts = [e for e in fresh if e.get("type") == "hurt"]
        nh_name, nh_dist, nh_id, nh_pos = self._extract_hostile_info(status)

        ended_wet_in_payload = any('"in_water": true' in str(r or "").lower() for r in (tool_results or []))
        needs_water_escape = in_water and (
            not status.get("on_ground")
            or status.get("submerged")
            or ended_wet_in_payload
            or (isinstance(oxygen, (int, float)) and oxygen < 270)
        )

        boundary_trigger: ThreatTrigger | None = self._detect_pre_hurt_trigger(status)
        if boundary_trigger is None and hurts and nh_name and nh_dist <= 15.0:
            boundary_trigger = {
                "source": "boundary_threat",
                "primary_entity_id": nh_id,
                "primary_type": nh_name,
                "primary_pos": nh_pos,
                "distance": nh_dist,
            }

        hp = float(status.get("health") or 20.0)
        food = int(status.get("food") or 20)
        needs_post_eat = (nh_dist > 8.0) and (hp <= 13.0 or food <= 13)
        in_wall_hurt = any(str(h.get("source") or "") in IN_WALL_HURT_SOURCES for h in hurts)

        if not (in_lava or needs_water_escape or in_wall_hurt or boundary_trigger is not None or needs_post_eat):
            self.previous_health = hp
            return None

        self._in_reflex = True
        notes: list[str] = []
        try:
            if in_lava:
                self._action("boundary_escape_lava", lambda: self._skills().escape_water())
                notes.append("脱离岩浆")
            elif needs_water_escape:
                esc = self._action("boundary_escape_water", lambda: self._skills().escape_water())
                if esc.get("ok"):
                    notes.append("自动浮水登岸")
            elif in_wall_hurt:
                uns = self._action("boundary_unstick_wall", lambda: self._skills().unstick())
                if uns.get("ok"):
                    notes.append("塌方破墙脱困")

            if boundary_trigger is not None and not in_lava:
                act = self._action(
                    "boundary_subcortical_combat",
                    lambda: self.combat_slot.handle(self.combat_env, boundary_trigger),
                )
                res_d = act.get("result") if isinstance(act.get("result"), dict) else {}
                notes.append(str(res_d.get("summary") or f"处置近身威胁 {nh_name}"))

            if needs_post_eat and boundary_trigger is None and not in_lava:
                try:
                    inv = self.bot.inventory()
                except Exception:
                    inv = {}
                counts, _, _ = self._parse_inventory_summary(inv)
                best_food = next((f for f in SAFE_FOOD_PRIORITY if counts.get(f, 0) > 0), None)
                if best_food:
                    eat_act = self._action("boundary_eat", lambda: self.bot.call("use_item", {"item": best_food}))
                    if eat_act.get("ok"):
                        notes.append(f"顺手进食 {best_food}")

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
        note = (
            "响应不代表整体安全；按最新状态重新规划，勿继续被中断的旧动作链。"
            if requires_replan
            else "后台已静默进食，未打断当前生成。"
        )
        incident: dict[str, Any] = {
            "reasons": reasons,
            "actions": actions,
            "status": {
                k: status.get(k)
                for k in ("pos", "health", "food", "ready", "alive", "busy", "in_water", "blocking")
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
