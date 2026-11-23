"""GenerationGuard mixin: sensing (split from legacy guardian.py)."""

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


class _SensingMixin:
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

