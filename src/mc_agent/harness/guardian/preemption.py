"""GenerationGuard mixin: preemption (split from legacy guardian.py)."""

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


class _PreemptionMixin:
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
                underground_move_water = interrupted_name == "mc_move_to" and self._underground_submersion(st_pre)
                surface_first_pre = (
                    (mining_water_pre or interrupted_name == "mc_skill_unstick" or underground_move_water)
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
                        or (interrupted_name in {"mc_move_to", "pre_ensure"} and nh_pre_dist <= self.MOVE_DROWNED_THREAT_RANGE)
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
                        falling_after = False
                        over_lava = False
                        if isinstance(st_after, dict) and not life_unavailable:
                            if self._nether_fast_fall(st_after):
                                falling_after = True
                                st_after = self._settle_dry_landing(st_pre, st_after, self.bot, deadline=time.monotonic() + 0.7)
                            elif interrupted_name == "pre_ensure" and self._failed_melee_retreat_airborne(
                                trigger, st_pre, st_after, outcome,
                            ):
                                falling_after = True
                                falling_summary = f"执行 {interrupted_name} 途中撤退未果，角色仍在半空，请待确认落点后重新规划"
                                st_after, over_lava = self._observe_fall_contact(
                                    st_after, event_name="guardian.melee_retreat_fall_lava", budget_s=0.95, checks=4,
                                )
                            life_unavailable = life_changed(st_after)
                        post_name, post_dist, post_id, post_pos = self._extract_hostile_info(st_after if isinstance(st_after, dict) else {})
                        close_drowned_after_reflex = (
                            trigger.get("source") == "tool_interrupted_hurt"
                            and post_name == "drowned" and post_dist <= 6.0
                            and st_after.get("ready") is not False and st_after.get("alive") is not False
                            and st_after.get("last_death") == st_pre.get("last_death")
                            and st_after.get("dimension") == st_pre.get("dimension")
                        )
                        if not life_unavailable and isinstance(st_after, dict) and (st_after.get("in_lava") or over_lava):
                            if st_after.get("in_lava"):
                                self._last_lava_contact_at = time.monotonic()
                            lava_escape = self._action(
                                "escape_lava",
                                lambda: self.combat_env.escape_lava(**({"force_rim_escape": True} if over_lava else {})),
                                raise_on_http_error=False,
                            )
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
                                hazard = "下坠至岩浆上方" if over_lava else "下坠后接触岩浆"
                                outcome = {
                                    **outcome, "handled": escaped, "mode": "lava_escape", "status_after": st_after,
                                    "summary": f"执行 {interrupted_name} 途中{hazard}，已转为脱困。{escape_summary}",
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
            mode_now = outcome.get("mode")
            extra_directive = ""
            # BUG-095: if guardian timed out with a close hostile, force immediate flee directive.
            if mode_now == "network_timeout":
                nh = st_after.get("nearest_hostile")
                if isinstance(nh, dict) and float(nh.get("dist", 99.0)) <= 4.5:
                    extra_directive = (
                        f"【紧急保命】贴脸遇怪({nh.get('name')} d={nh.get('dist')}m)且小脑超时，"
                        "下一轮必须立刻调 mc_skill_flee 或封路撤退，严禁继续挖矿或 reset！"
                    )
            # BUG-098: detect repeated underground water_surface preemption loops.
            if mode_now == "water_surface":
                now_ws = time.monotonic()
                ws_hist = getattr(self, "_water_surface_hits", [])
                ws_hist = [t for t in ws_hist if now_ws - t <= 60.0]
                ws_hist.append(now_ws)
                self._water_surface_hits = ws_hist
                pos_after = st_after.get("pos")
                if len(ws_hist) >= 2 and isinstance(pos_after, (list, tuple)) and len(pos_after) >= 2 and float(pos_after[1]) < 55.0:
                    extra_directive = (
                        f"【地下连续遇水×{len(ws_hist)}】60s 内已多次触发上浮抢占，"
                        "禁止原地重复调 mine_dfs/unstick/mine_blocks，请立刻向干燥方向 mc_move_to 或平推 8+ 格彻底远离当前水袋！"
                    )
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
                "reflex_mode": mode_now,
                "message": f"[小脑本能抢占接管] {summary}（原工具未自动重放，请基于当前最新状态决策下一步）。{extra_directive}",
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


    def _near_mining_water_site(self, cell: tuple[int, ...]) -> bool:
        record = self._mining_water_reentry_site
        if record is None:
            return False
        x, y, z = record[0]
        return abs(cell[0] - x) + abs(cell[2] - z) <= 2 and abs(cell[1] - y) <= 2


    def _verify_mining_water_reentry(
        self, bot: _GuardSession, target: list[float], deadline: float, *, opening_status: dict[str, Any] | None = None,
    ) -> None:
        record = self._mining_water_reentry_site
        cell = tuple(map(math.floor, target))
        if record is None or not self._near_mining_water_site(cell):
            return
        site, life = record
        context: dict[str, Any] = {"target": target, "water_reentry_site": list(site)}
        try:
            current = opening_status if opening_status is not None else bot.call("status", method="GET", timeout_s=0.5)
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
            x, y, z = cell
            if opening_status is None:
                probes = [cell, (x, y + 1, z)]
            else:
                probes = [(x, y + 1, z), *((x + dx, y, z + dz) for dx, dz in CARDINAL_VECTORS.values())]
            env = CombatEnv(bot)
            for probe in probes:
                name = env._get_block_name(*probe, strict=True, deadline=deadline)
                if name in {"water", "flowing_water"}:
                    if opening_status is not None:
                        context["water_pos"] = list(probe)
                    break
            else:
                return
        except (ApiError, httpx.HTTPError, _RecoveryBudgetExceeded, _TerrainProbeExpired):
            raise _MiningLandingDenied(
                "mining_footing_unavailable", "暂时无法确认原水坑已排干，请沿干地改道。", context=context,
            ) from None
        message = (
            "挖开此格可能让相邻积水流入巷道，请保留挡水方块并沿干地改道。"
            if opening_status is not None else "这个航点仍有水，附近刚发生过持续沉头，请沿干地改道。"
        )
        raise _MiningLandingDenied("mining_water_reentry", message, context=context)


    def _dig_hazard(self, target: Any, *, check_support: bool, check_roof: bool, check_mining_water: bool = False) -> str | None:
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
            if check_mining_water:
                self._verify_mining_water_reentry(probe_bot, list(target), deadline, opening_status=status)
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
                check_move_safety=tool_name == "mc_move_to",
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
        except _UnsafeCreeperMove as error:
            entity_id = error.args[0]
            self.events.emit("guardian.creeper_move_blocked", entity_id=entity_id)
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {
                    "code": "unsafe_creeper_move", "entity_id": entity_id,
                    "message": "移动目标在苦力怕爆炸警戒范围内，请改到远离它的落脚点。",
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
        except (_UnsafeFleeExit, _UnsafeUnstickExit) as error:
            self.events.emit("guardian.flee_exit_blocked" if is_flee else "guardian.unstick_exit_blocked", tool=tool_name)
            if isinstance(error, _UnsafeFleeExit) and error.args and error.args[0] == "respawned":
                return json.dumps({
                    "ok": False, "mode": "respawned", "respawned": True, "requires_replan": True,
                    "pos_after": error.args[1], "error": {"code": "respawned", "message": "撤退核验期间角色已换命，请按最新位置重新规划。"},
                }, ensure_ascii=False)
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
                    unsealed = edge_res.get("unsealed_lava_target")
                    if (
                        unsealed is not None and boundary_trigger is None and self._extract_hostile_info(landing)[1] > 6.0
                        and self.combat_env._on_same_footing(status, landing)
                    ):
                        retreat = self._action(
                            "boundary_retreat_from_lava_edge", lambda: self._back_off_unsealed_lava(landing, unsealed),
                        )
                        notes.append("已撤离未封住的岩浆" if retreat["ok"] else "岩浆仍未封住，撤离尚未确认")
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

