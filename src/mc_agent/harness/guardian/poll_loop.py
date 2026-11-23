"""GenerationGuard mixin: poll_loop (split from legacy guardian.py)."""

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


class _PollLoopMixin:
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
            landing, falling_over_lava = self._observe_fall_contact(status, event_name="guardian.nether_fall_lava")
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
                st_after_sc = sc_res.get("status_after") if isinstance(sc_res, dict) else None
                shooter_id = pre_trigger.get("primary_entity_id")
                if (
                    pre_trigger.get("source") == "pre_hurt_ranged" and not self._custom_combat_registered
                    and pre_trigger.get("primary_type") in {"skeleton", "stray", "bogged", "pillager"}
                    and 3.5 < pre_trigger["distance"] <= 5.0 and isinstance(shooter_id, int)
                    and not has_shield and isinstance(st_after_sc, dict)
                    and str(st_after_sc.get("dimension") or "").removeprefix("minecraft:") == "overworld"
                    and isinstance(sc_res, dict) and self._only_stalled_ranged_retreat(sc_res)
                    and self.combat_env._on_same_footing(status, st_after_sc)
                    and self.combat_env.arrow_cover_material(counts) is not None
                    and time.monotonic() - now < SUBCORTICAL_COMBAT_BUDGET_S - self.ARROW_COVER_PLACEMENT_BUDGET_S
                ):
                    cover = self._action("place_arrow_cover", lambda: self._place_arrow_cover(st_after_sc, counts, shooter_id))
                    actions.append(cover)
                    in_sc_cover = cover["ok"]
                if pre_trigger.get("source") == "pre_hurt_ranged" or str(pre_trigger.get("primary_type") or "") in RANGED_MOBS:
                    self._mark_ranged_engagement(time.monotonic(), trigger=pre_trigger, in_cover=in_sc_cover)
                if isinstance(sc_res, dict):
                    st_after_sc = st_after_sc if isinstance(st_after_sc, dict) else status
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
                shore_res = {}
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
                    shore_pos = shore_res.get("pos_after")
                    if (
                        (melee_shore or close_drowned_hit) and hurt_src_w in MELEE_HURT_SOURCES
                        and not (has_shield or has_melee_weapon)
                        and isinstance(inv, dict) and isinstance(inv.get("items"), list)
                        and shore_res.get("in_water") is False and isinstance(shore_pos, (list, tuple))
                    ):
                        attacker_id = (att_w_dict or {}).get("id")
                        followup = self._disengage_shore_pursuer(
                            status, shore_pos, attacker_id if isinstance(attacker_id, int) else None,
                        )
                        if followup is not None:
                            actions.append(followup)
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
                                and (
                                    cf_result.get("reason") in {"no_safe_retreat", "unsafe_landing"}
                                    or (
                                        not cf.get("ok")
                                        and retreat_made_progress(False, moved_dist, cf_result.get("reason"))
                                    )
                                )
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
                    uses_default_bunker = self.skills is None or isinstance(self.skills, MacroSkills)
                    exposed_to_lethal_melee = (
                        (
                            dimension == "the_nether"
                            or (dimension == "overworld" and uses_default_bunker and not has_shield and not already_blocking)
                        )
                        and 0 < hp_val <= 4.0 and not inside_shaft
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
                    # Every urgent roof material is also a tactical filler. An inconclusive
                    # inventory snapshot still goes through the macro's own check.
                    no_bunker_material = (
                        dimension == "overworld" and 0 < hp_val <= 6.0
                        and uses_default_bunker
                        and isinstance(inv.get("items"), list)
                        and self.combat_env._pick_filler_block(_inventory_counts_from_raw(inv)) is None
                    )
                    try_bunker = not indirect_magic and not exposed_to_lethal_melee and (
                        can_surface_night_bunker or not melee_or_airborne
                    )
                    if try_bunker:
                        if no_bunker_material:
                            self.events.emit("guardian.emergency_bunker_skipped", reason="no_roof_block", pos=status.get("pos"))
                        else:
                            bunker = self._try_emergency_bunker(status)
                    if bunker is not None:
                        actions.append(bunker)
                    bunker_result = (bunker or {}).get("result") or {}
                    if self._bunker_life_unavailable(bunker):
                        reasons.append("death_or_connection_changed")
                    elif not (bunker and bunker["ok"]):
                        unsafe_footing = (
                            exposed_to_lethal_melee or (try_bunker and no_bunker_material)
                            or bunker_result.get("reason") in {"unsafe_floor", "unsafe_open_side", "no_roof_block"}
                            or (self._blocked_bunker is not None and self._blocked_bunker[0] == self._bunker_site(status))
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
                    wither_cornered = (
                        dimension == "the_nether" and nh_name == "wither_skeleton" and nh_id is not None
                        and already_blocking and has_melee_weapon
                        and (
                            hurt_src == "mob_attack"
                            or (hurt_src in {"", "wither"} and now - self._last_melee_hurt_at < 4.0)
                        )
                        and (not isinstance(threat.get("id"), int) or threat["id"] == nh_id)
                        and not (cf_result.get("ok") or cf_result.get("respawned"))
                        and cf_result.get("moved_dist") == 0.0
                    )
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
                    elif wither_cornered and 0 < hp_val <= 6.0 and cf_result.get("reason") == "no_safe_retreat":
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
                        and not no_bunker_material
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
                    dry_airborne_retreat = (
                        landing is not None and cf_result.get("on_ground") is False
                        and cf_result.get("moved_dist") == 0.0
                        and not (cf_result.get("in_water") or cf_result.get("in_lava"))
                    )
                    wither_after_knockback = wither_cornered and dry_airborne_retreat
                    retry_after_knockback = (
                        dimension == "overworld" and not (has_shield or has_melee_weapon or recent_ranged_hurt)
                        and dry_airborne_retreat and landing.get("ready") is True
                        and not (landing.get("busy") or landing.get("on_fire"))
                        and nh_id is not None and (landing.get("nearest_hostile") or {}).get("id") == nh_id
                        and not (landing.get("nearest_hostile") or {}).get("blocked_by_wall")
                    )
                    if shore is None and (melee_after_retreat or wither_after_knockback) and (
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
                                face = self._action(
                                    "face_attacker_after_retreat",
                                    lambda: self.bot.call("look", {"entity_id": target_id}, timeout_s=0.9),
                                )
                                actions.append(face)
                                if wither_after_knockback and target_id == nh_id and face.get("ok"):
                                    actions.extend(self._cornered_wither_skeleton_response(status, counts, nh_id, landing=landing))
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
                                controller = self.combat_slot.controller
                                near_target = target_pos is None or (
                                    len(target_pos) >= 3
                                    and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in target_pos[:3])
                                    and math.dist(landing["pos"][:3], target_pos[:3]) <= 3.5
                                )
                                if (
                                    follow_unarmed_retreat and dimension == "overworld"
                                    and hurt_src == "mob_attack" and target_id == nh_id and current_hp <= 4.0
                                    and landing.get("ready") is True and not any(landing.get(key) for key in ("busy", "on_fire"))
                                    and near_target and isinstance(controller, SubcorticalInstinctController)
                                ):
                                    actions.append(self._action(
                                        "followup_melee_retreat",
                                        lambda: self._disengage_critical_melee(controller, landing, target_id),
                                    ))
                                else:
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
                post_status, falling_over_lava = self._observe_fall_contact(post_status, event_name="guardian.nether_fall_lava")
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
        fresh_damage = any(e.get("type") == "hurt" for e in fresh)
        if fresh_damage:
            previous = self._stationary_warnings.get("pre_hurt_melee")
            if previous is not None and previous.failed_cover:
                self._stationary_warnings.pop("pre_hurt_melee")
        if pre_trigger is not None and post_events_verified and not fresh_damage:
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

