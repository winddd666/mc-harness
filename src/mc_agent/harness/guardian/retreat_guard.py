"""GenerationGuard mixin: retreat_guard (split from legacy guardian.py)."""

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


class _RetreatGuardMixin:
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


    @staticmethod
    def _only_stalled_ranged_retreat(outcome: dict[str, Any]) -> bool:
        steps = outcome.get("actions")
        if outcome.get("mode") != "cover_or_flee" or not isinstance(steps, list) or len(steps) != 1:
            return False
        retreat = steps[0]
        return (
            isinstance(retreat, dict) and retreat.get("action") == "flee" and retreat.get("ok") is False
            and retreat.get("reason") == "no_safe_retreat" and retreat.get("moved") == 0.0
        )


    def _unchanged_ranged_warning(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any], actions: list[dict[str, Any]],
    ) -> bool:
        if (
            len(actions) != 1 or actions[0].get("action") != "subcortical_combat"
            or not self._only_stalled_ranged_retreat(actions[0].get("result") or {})
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


    def _observe_stationary_melee(
        self, trigger: ThreatTrigger, before: dict[str, Any], after: dict[str, Any], *, failed_cover: bool = False,
    ) -> tuple[tuple[float, ...], tuple[Any, ...]] | None:
        """Verify a close melee warning against live coordinates and unchanged bot state."""
        mob = trigger.get("primary_type")
        spider_warning = mob == "spider" and not failed_cover
        if not spider_warning and mob != "zombie":
            return None
        state_keys = (
            "pos", "dimension", "health", "food", "held", "offhand", "armor_points", "effects",
            "blocking", "sneaking", "yaw", "busy", "held_controls", "ready", "alive", "on_ground",
            "in_water", "in_lava", "submerged", "on_fire",
        )
        if spider_warning:
            if (
                str(after.get("dimension") or "").removeprefix("minecraft:") != "overworld"
                or not isinstance(after.get("is_day"), bool)
            ):
                return None
            state_keys += ("is_day",)
        health = after.get("health")
        # The status API omits last_death until the bot has died at least once.
        last_death = before.get("last_death")
        if (
            not isinstance(health, (int, float)) or not math.isfinite(health) or health <= 0
            or (not failed_cover and health < 16.0)
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
            warning_distance = trigger.get("distance")
            if failed_cover:
                within_proof_distance = (
                    2.3 < distance <= 3.5 and isinstance(warning_distance, (int, float)) and 2.3 < warning_distance <= 3.5
                )
            elif spider_warning:
                within_proof_distance = 1.6 <= distance <= 2.3 and distance == warning_distance
            else:
                within_proof_distance = distance == 2.3
            if (
                name != mob or entity_id != trigger.get("primary_entity_id") or not within_proof_distance
                or target.get("alive") is False or target.get("blocked_by_wall")
                or not self._same_observed_melee_position(point, first_point)
                or (not failed_cover and tuple(point[:3]) != tuple(first_point[:3]))
                or round(math.dist(after["pos"][:3], point[:3]), 6) < (1.55 if spider_warning else 2.3)
            ):
                return None
            current = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
            if failed_cover or spider_warning:
                page = self.bot.call(
                    "events", method="GET", params={"since": self.cursor, "limit": 20},
                    timeout_s=0.25, allow_reauth=False,
                )
                if not isinstance(page, dict) or page.get("has_more") or not isinstance(page.get("events"), list):
                    return None
                if any(
                    not isinstance(event, dict) or event.get("type") in {"hurt", "death", "death_loop", "spawn", "disconnected"}
                    for event in page["events"]
                ):
                    return None
        except (ApiError, httpx.HTTPError):
            return None
        if not isinstance(current, dict):
            return None
        nearest = current.get("nearest_hostile")
        current_name, current_distance, current_id, current_point = self._extract_hostile_info(current)
        if (
            not isinstance(nearest, dict) or (current_name, current_distance, current_id) != (mob, distance, entity_id)
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
        previous = self._stationary_warnings.get(source)
        if previous is not None and previous.failed_cover:
            self._stationary_warnings.pop(source)
        if len(actions) != 1 or actions[0]["action"] != "subcortical_combat":
            return False
        outcome = actions[0].get("result")
        combat_actions = outcome.get("actions") if isinstance(outcome, dict) else None
        if not isinstance(combat_actions, list) or not combat_actions:
            return False
        failed_cover = False
        if len(combat_actions) != 1:
            cover = combat_actions[-1]
            failed_cover = (
                melee_warning and len(combat_actions) == 2 and isinstance(cover, dict)
                and cover.get("action") == "place_melee_cover" and cover.get("ok") is False
                and cover.get("placed") == cover.get("placement_attempts") == 0
            )
            if not failed_cover:
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
        now = time.monotonic()
        life = (after.get("dimension"), after.get("last_death"))
        close_melee_observation = None
        if melee_warning and (
            (name == "zombie" and (failed_cover or closest == warning_distance == 2.3))
            or (name == "spider" and not failed_cover and 1.6 <= closest == warning_distance <= 2.3)
        ):
            close_melee_observation = self._observe_stationary_melee(trigger, before, after, failed_cover=failed_cover)
        if (
            previous is not None and previous.entity_id == entity_id and now - previous.observed_at < 45.0
            and previous.life == life and previous.failed_cover == failed_cover
            and math.dist(pos_after[:3], previous.pos) <= 0.25
        ):
            previous_observation = previous.close_melee_observation
            if failed_cover and previous_observation is not None and close_melee_observation is not None and (
                close_melee_observation[1] == previous_observation[1] and distance >= previous.distance
                and self._same_observed_melee_position(previous_observation[0], close_melee_observation[0])
            ):
                self.events.emit(
                    "guardian.stationary_melee_cover_verified", entity_id=entity_id, pos=pos_after,
                    target_pos=close_melee_observation[0], distance=closest,
                )
                self._stationary_warnings[source] = replace(previous, observed_at=now, distance=distance)
                return True
            same_close_melee = (
                not failed_cover and close_melee_observation is not None and previous_observation == close_melee_observation
            )
            if same_close_melee:
                self.events.emit(
                    "guardian.stationary_melee_verified", entity_id=entity_id, pos=pos_after,
                    target_pos=close_melee_observation[0], distance=closest,
                )
            if not failed_cover and (same_close_melee or (closest > 2.3 and distance >= previous.distance - 0.25)):
                # Refresh the observation while comparing position and distance to the initial warning.
                if melee_warning:
                    self._stationary_warnings[source] = replace(
                        previous, observed_at=now, close_melee_observation=close_melee_observation,
                    )
                return True
        self._stationary_warnings[source] = _StationaryWarning(
            entity_id, tuple(pos_after[:3]), distance, now, life, close_melee_observation, failed_cover,
        )
        return False


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

        def probe(yaw: float, distance: float) -> tuple[int, bool] | None:
            try:
                route = probe_env.probe_retreat(pos, yaw, distance)
            except _RecoveryBudgetExceeded:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                route = None
            return route

        def short_route(yaw: float, lengths: tuple[float, ...]) -> tuple[float, int] | None:
            if (
                not lengths or time.monotonic() >= probe_deadline
                or status.get("on_ground") is not True or status.get("ready") is not True
                or any(status.get(key) for key in ("busy", "in_water", "submerged", "in_lava"))
            ):
                return None
            name, enemy_distance, entity_id, enemy_pos = self._extract_hostile_info(status)
            if name and enemy_distance <= (12.0 if name in RANGED_MOBS else 6.0):
                if enemy_pos is None and entity_id is not None:
                    enemy_pos = self._find_hostile_position(entity_id, name, timeout_s=0.25, session=probe_bot)
                ux, uz = yaw_to_unit_xz(yaw)
                if not enemy_pos or (enemy_pos[0] - pos[0]) * ux + (enemy_pos[2] - pos[2]) * uz > 0.05:
                    return None
            foot_y = math.floor(pos[1] + 0.05)
            length = next((length for length in lengths if probe(yaw, length) == (foot_y, False)), None)
            if length is None:
                return None
            try:
                if not probe_env.has_safe_footing(pos, deadline=probe_deadline):
                    return None
                current = probe_env._safe_status(timeout_s=0.25)
            except (_RecoveryBudgetExceeded, _TerrainProbeExpired):
                if deadline is not None and time.monotonic() >= deadline:
                    raise _RecoveryBudgetExceeded from None
                return None
            point = current.get("pos")
            if current.get("alive") is False or (point and any(
                current.get(key) != status.get(key) for key in ("last_death", "dimension")
            )):
                raise _UnsafeFleeExit("respawned", point)
            if (
                not self._same_life(status, current) or current.get("on_ground") is not True
                or any(current.get(key) for key in ("busy", "in_water", "submerged", "in_lava"))
                or not point or math.dist(point[:3], pos[:3]) > 0.2
            ):
                return None
            self.events.emit("guardian.flee_exit_shortened", action=action, pos=point, yaw=yaw, distance=length)
            return length, foot_y

        if action == "control":
            yaw = float(body["yaw"])
            route = probe(yaw, 3.5)
            duration = min(int(body.get("ms") or 450), 450)
            if route is not None:
                return {**body, "jump": route[1], "ms": duration}
            if (
                not body.get("sprint") or any(body.get(key) for key in ("back", "left", "right"))
                or short_route(yaw, (1.65,)) is None
            ):
                raise _UnsafeFleeExit
            return {**body, "sprint": False, "jump": False, "ms": min(duration, 220)}

        x, _, z = map(float, pos[:3])
        dx, dz = float(body["x"]) - x, float(body["z"]) - z
        desired_distance = math.hypot(dx, dz)
        if desired_distance == 0:
            return body
        distance = min(6.0, desired_distance)
        yaw = math.atan2(-dx, -dz)
        route = probe(yaw, distance)
        allowed_range = 1.0
        if route is None:
            shortened = short_route(yaw, tuple(length for length in (3.5, 1.65) if length < distance))
            if shortened is None:
                raise _UnsafeFleeExit
            distance, landing_y = shortened
            allowed_range = 0.6
        else:
            landing_y = route[0]
        fraction = distance / desired_distance
        return {**body, "x": x + dx * fraction, "y": landing_y, "z": z + dz * fraction, "range": allowed_range}


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
        deadline = time.monotonic() + self.ARROW_COVER_PLACEMENT_BUDGET_S
        bot = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=deadline)

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


    def _creeper_at_move_goal(
        self, body: dict[str, Any], status: dict[str, Any], call: Callable[..., dict[str, Any]],
    ) -> int | None:
        arrival_range = body.get("range")
        origin = status.get("pos")
        name, distance, entity_id, creeper_pos = self._extract_hostile_info(status)
        if (
            type(arrival_range) not in (int, float) or not 0 < arrival_range <= 5.5
            or status.get("ready") is not True or status.get("alive") is False
            or name != "creeper" or entity_id is None or not 0 <= distance <= 16.0
            or status["nearest_hostile"].get("alive") is False or status["nearest_hostile"].get("blocked_by_wall") is True
        ):
            return None
        if body.get("entity_id") == entity_id:
            return entity_id
        x, y, z = (body.get(axis) for axis in ("x", "y", "z"))
        if (
            body.get("entity_id") is not None
            or not isinstance(origin, (list, tuple)) or len(origin) < 3
            or not all(type(coord) in (int, float) and math.isfinite(coord) for coord in (*origin[:3], x, z))
            or (y is not None and (type(y) not in (int, float) or not math.isfinite(y)))
        ):
            return None
        if creeper_pos is None:
            scout = _GuardSession(self.raw_bot, self.bot.checkpoint, deadline=time.monotonic() + 0.5, call_override=call)
            live = self._find_hostile(entity_id, "creeper", session=scout, timeout_s=0.3, require_position=True)
            if (
                live is None or live.get("alive") is False or live.get("blocked_by_wall") is True
                or str(live.get("name") or "").removeprefix("minecraft:") != "creeper"
            ):
                return None
            creeper_pos = live["pos"]
        if not all(type(coord) in (int, float) and math.isfinite(coord) for coord in creeper_pos[:3]):
            return None
        separation = math.dist((x, origin[1] if y is None else y, z), creeper_pos[:3])
        if separation <= 1.5 and separation + arrival_range <= 5.5 and separation < math.dist(origin[:3], creeper_pos[:3]):
            return entity_id
        return None


    @staticmethod
    def _underground_move_position(pos: Any) -> bool:
        return (
            isinstance(pos, (list, tuple)) and len(pos) >= 3
            and all(type(coord) in (int, float) and math.isfinite(coord) for coord in pos[:3])
            and pos[1] < 55.0
        )


    def _underground_submersion(self, status: dict[str, Any]) -> bool:
        return (
            status.get("in_water") is True and status.get("submerged") is True
            and str(status.get("dimension") or "").removeprefix("minecraft:") == "overworld"
            and self._underground_move_position(status.get("pos"))
        )


    @staticmethod
    def _tool_water_observation(status: dict[str, Any], reason: str, submerged_for_s: float | None = None) -> dict[str, Any]:
        return {
            "reason": reason,
            "submerged_for_s": round(submerged_for_s, 3) if submerged_for_s is not None else None,
            **{key: status.get(key) for key in (
                "pos", "health", "oxygen", "in_water", "submerged", "on_ground", "nearest_hostile",
                "ready", "alive", "dimension", "last_death", "velocity",
            )},
        }


    def _underground_move_water_status(
        self, result: Any, call: Callable[..., dict[str, Any]], before: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if (
            not isinstance(result, dict) or result.get("ok") is False
            or not self._underground_move_position(result.get("pos"))
        ):
            return None
        try:
            after = call("status", method="GET", timeout_s=0.35, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            return None
        if (
            isinstance(after, dict) and after.get("ready") is True and after.get("alive") is not False
            and self._underground_submersion(after) and (before is None or self._same_life(before, after))
        ):
            return after
        return None


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

