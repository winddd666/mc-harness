"""GenerationGuard mixin: recovery (split from legacy guardian.py)."""

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


class _RecoveryMixin:
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


    def _back_off_unsealed_lava(self, status: dict[str, Any], source: list[int]) -> dict[str, Any]:
        deadline = time.monotonic() + 2.5
        source_pos = [source[0] + 0.5, source[1], source[2] + 0.5]
        try:
            with self.bot.limit_until(deadline):
                retreat = self.combat_env.flee(
                    source_pos, distance=3.5, wait_for_ground=True, away_only=True,
                    allow_shaft_seal=False, allow_second_leg=False, use_nearest_hostile=False,
                    expected_life=(status.get("dimension"), status.get("last_death")), terrain_deadline=deadline - 0.7,
                )
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return {"ok": False, "reason": "time_budget"}
        if retreat.get("ok") and math.dist(retreat["pos_after"][:3], source_pos) <= math.dist(status["pos"][:3], source_pos):
            return {**retreat, "ok": False, "reason": "lava_clearance_unconfirmed"}
        return retreat


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


    def _observe_fall_contact(
        self, status: dict[str, Any], *, event_name: str, budget_s: float = 0.7, checks: int = 2,
    ) -> tuple[dict[str, Any], bool]:
        """Recheck an airborne character for contact or lava immediately below the hitbox."""
        deadline = time.monotonic() + budget_s
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)
        try:
            current = self._settle_dry_landing(status, status, bot, deadline=deadline, checks=checks)
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
                event_name, pos_before=status["pos"], velocity=status.get("velocity"),
                pos=current.get("pos"), in_lava=bool(current.get("in_lava")), above_lava=above_lava,
            )
        return current, above_lava


    def _failed_melee_retreat_airborne(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any], outcome: dict[str, Any],
    ) -> bool:
        health = after.get("health")
        if not (
            trigger.get("source") == "pre_hurt_melee" and outcome.get("mode") == "cover_or_flee"
            and self._same_life(before, after) and after.get("on_ground") is False and after.get("pos")
            and str(after.get("dimension") or "").removeprefix("minecraft:") == "overworld"
            and isinstance(health, (int, float)) and 0 < health < 11.0
            and not any(after.get(key) for key in ("in_water", "in_lava", "submerged"))
        ):
            return False
        name, distance, entity_id, _ = self._extract_hostile_info(after)
        if name not in PRE_HURT_MELEE_MOBS or not 0 <= distance <= 3.5 or entity_id is None:
            return False
        if entity_id != trigger.get("primary_entity_id"):
            return False
        retreats = [
            action for action in outcome.get("actions") or [] if isinstance(action, dict)
            and action.get("action") in {"flee", "followup_melee_retreat"}
        ]
        return bool(retreats) and all(action.get("ok") is False for action in retreats)


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
                and not status.get("on_fire")
                and isinstance(health, (int, float)) and 0 < health <= 6.0
                and (
                    (ranged and prefer_cover and status.get("on_ground") is False)
                    or (
                        not ranged and recent_melee and nh_name in PRE_HURT_MELEE_MOBS and nh_distance <= 3.5
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


    def _disengage_critical_melee(
        self, controller: SubcorticalInstinctController, landing: dict[str, Any], target_id: int,
    ) -> dict[str, Any]:
        try:
            with self.bot.limit_until(time.monotonic() + 2.2):
                retreat, _ = controller.ranked_melee_retreat(
                    self.combat_env, landing, target_id, critical=True, verify_melee_clearance=True,
                )
                return {"entity_id": target_id, **retreat}
        except _RecoveryBudgetExceeded:
            self._stop_after_recovery_timeout()
            return {"ok": False, "reason": "time_budget", "entity_id": target_id}


    def _disengage_shore_pursuer(
        self, before: dict[str, Any], shore_pos: Sequence[float], attacker_id: int | None,
    ) -> dict[str, Any] | None:
        controller = self.combat_slot.controller
        if not isinstance(controller, SubcorticalInstinctController):
            return None
        landing = self.combat_env._safe_status(timeout_s=0.3)
        health = landing.get("health")
        name, distance, target_id, _ = self._extract_hostile_info(landing)
        if (
            not self._same_life(before, landing) or landing.get("on_ground") is not True
            or any(landing.get(key) for key in ("in_water", "submerged", "in_lava", "busy", "on_fire", "blocking"))
            or not self._same_observed_melee_position(shore_pos, landing.get("pos"))
            or not isinstance(health, (int, float)) or not 0 < health <= 8.0
            or name not in PRE_HURT_MELEE_MOBS or not 0 <= distance <= 3.5 or target_id is None
            or (attacker_id is not None and target_id != attacker_id)
        ):
            return None
        return self._action(
            "followup_melee_retreat", lambda: self._disengage_critical_melee(controller, landing, target_id),
        )


    def _cornered_wither_skeleton_response(
        self, before: dict[str, Any], counts: dict[str, int], target_id: int,
        *, landing: dict[str, Any] | None = None,
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
            if landing is None:
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

