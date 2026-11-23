"""GenerationGuard mixin: intercept (split from legacy guardian.py)."""

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


class _InterceptMixin:
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
        watch_move_water: bool = True,
        check_move_safety: bool = False,
        check_flee_exit: bool = False,
        watch_attack_emergency: bool = False,
        check_combat_step: bool = False,
        check_support_dig: bool = False,
        check_mining_landing: bool = False,
        check_gathering_reentry: bool = False,
        check_food_use: bool = False,
    ):
        """Watch threats during tools and prerequisite gathering, including water travel by default."""
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
        move_in_progress = threading.Event()
        attack_active = threading.Event()
        attack_state_lock = threading.Lock()
        attack_target_id: int | None = None
        actor_thread_id = threading.get_ident()
        pending_trigger: list[ThreatTrigger] = []
        unstick_start: dict[str, Any] = {}
        move_start: tuple[float, dict[str, Any]] | None = None
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

        def terrain_damage_trigger(
            status: dict[str, Any], call: Callable[..., dict[str, Any]], *, movement_lava: bool = False,
        ) -> ThreatTrigger | None:
            if (
                not (watch_terrain_damage or movement_lava)
                or status.get("ready") is False or status.get("alive") is False or not status.get("pos")
                or status.get("in_water") or (status.get("in_lava") and not movement_lava)
            ):
                return None
            if status.get("in_lava"):
                source = "lava"
            else:
                event = self._fresh_tool_hurt_event(call, ignore_burn_ticks=movement_lava)
                source = str((event or {}).get("source") or "").strip().lower()
            if (watch_mining_water or movement_lava) and source in LAVA_HURT_SOURCES:
                return {
                    "source": "boundary_threat", "primary_type": "lava", "primary_pos": status["pos"],
                    "primary_entity_id": None, "distance": 0.0, "hurt_source": source,
                    "observed_life": (status.get("dimension"), status.get("last_death")),
                }
            if movement_lava or source not in IN_WALL_HURT_SOURCES:
                return None
            return {
                "source": "tool_interrupted_hurt", "primary_type": "in_wall", "primary_pos": status["pos"],
                "primary_entity_id": None, "distance": 0.0, "hurt_source": source,
                "observed_life": (status.get("dimension"), status.get("last_death")),
            }

        def observed_move_lava(
            before: dict[str, Any], current: dict[str, Any], call: Callable[..., dict[str, Any]],
            previous_health: float | None = None,
        ) -> ThreatTrigger | None:
            if not self._same_life(before, current):
                return None
            starting_hp = previous_health if previous_health is not None else before.get("health")
            health = current.get("health")
            if current.get("in_lava") or (
                isinstance(starting_hp, (int, float)) and isinstance(health, (int, float)) and 0 < health < starting_hp
            ):
                return terrain_damage_trigger(current, call, movement_lava=True)
            return None

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
                    if check_move_safety:
                        start = move_start
                        if start is not None and move_in_progress.is_set():
                            terrain = observed_move_lava(start[1], st, orig_call, last_terrain_health)
                            last_terrain_health = float(cur_hp) if isinstance(cur_hp, (int, float)) else None
                        else:
                            last_terrain_health = None
                    elif watch_terrain_damage and isinstance(cur_hp, (int, float)):
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
                    watch_submersion = watch_low_oxygen or (check_move_safety and self._underground_submersion(st))
                    if watch_submersion:
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
                    else:
                        previous_water_health = None
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
                            watch_submersion and submerged is not False and numeric_oxygen
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
                                **({"water_observation": self._tool_water_observation(st, water_risk, submerged_for_s)}
                                   if water_risk and src_w == "tool_water_threat" else {}),
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
            cached_denial = mining_landing_error
            if (
                cached_denial is None or cached_denial.code != "unsafe_landing"
                or cached_denial.context.get("collar_approach") or xz in blocked_collar_approaches
                or mining_support_changed or time.monotonic() >= mining_landing_retry_at
                or self._near_mining_water_site(tuple(map(math.floor, target)))
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
            nonlocal attack_target_id, mining_support_changed, mining_forward_waypoint, move_start
            clean_path = str(path or "").lstrip("/")
            track_move = check_move_safety and clean_path == "move_to"
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
                if check_move_safety and clean_path == "status" and isinstance(observed, dict):
                    move_start = (time.monotonic(), observed)
                return observed
            if track_move and isinstance(body, dict) and move_start is not None:
                observed_at, observed = move_start
                if time.monotonic() - observed_at <= 1.0:
                    creeper = self._creeper_at_move_goal(body, observed, actor_call)
                    if pending_trigger:
                        raise ReflexPreempted(pending_trigger[0])
                    if creeper is not None:
                        raise _UnsafeCreeperMove(creeper)
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
                    # On a recorded stair, DFS can jump with a supplied yaw before it tries move_to.
                    if mining_forward_waypoint is not None or body.get("yaw") is not None:
                        hazard = self._mining_fallback_hazard(body)
                        if pending_trigger:
                            raise ReflexPreempted(pending_trigger[0])
                        if hazard:
                            context = {"hazard": hazard}
                            if mining_forward_waypoint is not None:
                                context["target"] = mining_forward_waypoint
                            else:
                                context["yaw"] = body["yaw"]
                            self.events.emit("guardian.mining_fallback_blocked", **context)
                            raise _MiningLandingDenied(
                                "unsafe_landing", "向前移动可能越过采矿落点进入危险地形，请改用有支撑的路线。", context=context,
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
                try:
                    dig_hazard = self._dig_hazard(
                        body.get("pos"), check_support=check_support_dig, check_roof=check_unstick_exit,
                        check_mining_water=check_mining_landing,
                    )
                except _MiningLandingDenied as error:
                    if pending_trigger:
                        raise ReflexPreempted(pending_trigger[0]) from None
                    self.events.emit("guardian.mining_water_dig_blocked", reason=error.code, **error.context)
                    raise
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
                if track_move:
                    move_in_progress.set()
                try:
                    res = actor_call(path, body, **kwargs)
                finally:
                    if track_move:
                        move_in_progress.clear()
            except _RecoveryBudgetExceeded:
                if pending_trigger and not self._in_reflex:
                    raise ReflexPreempted(pending_trigger[0]) from None
                raise
            except httpx.ReadTimeout:
                if track_move:
                    apply_pending_preemption()
                    if move_start is not None:
                        try:
                            after = actor_call("status", method="GET", timeout_s=0.35, allow_reauth=False)
                        except (ApiError, httpx.HTTPError):
                            after = None
                        if isinstance(after, dict):
                            trigger = observed_move_lava(move_start[1], after, actor_call)
                            if trigger is not None:
                                stop_evt.set()
                                apply_pending_preemption()
                                stop_watched_action(trigger)
                                raise ReflexPreempted(trigger) from None
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
            if track_move:
                after = self._underground_move_water_status(res, actor_call, move_start[1] if move_start else None)
                if after is not None:
                    stop_evt.set()
                apply_pending_preemption()
                if after is not None:
                    name, distance, entity_id, hostile_pos = self._extract_hostile_info(after)
                    trigger = ThreatTrigger(
                        source="tool_water_threat", primary_type=name, primary_entity_id=entity_id,
                        primary_pos=hostile_pos, distance=distance, observed_life=(after.get("dimension"), after.get("last_death")),
                        water_observation=self._tool_water_observation(after, "underground_move_submerged"),
                    )
                    if after.get("busy"):
                        stop_watched_action(trigger)
                    raise ReflexPreempted(trigger)
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

