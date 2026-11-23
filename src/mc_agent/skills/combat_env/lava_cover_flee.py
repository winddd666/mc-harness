"""CombatEnv mixin: lava_cover_flee."""

from __future__ import annotations

from mc_agent.skills.combat_env._base import (
    math,
    time,
    Any,
    Callable,
    Collection,
    Literal,
    Protocol,
    Sequence,
    TypedDict,
    httpx,
    ApiError,
    SAFE_FOOD_PRIORITY,
    auto_equip_survival_gear,
    water_oxygen_below,
    AIR_LIKE_BLOCKS,
    BREATHABLE_AIR_BLOCKS,
    WATER_BLOCKS,
    PASSABLE_TRANSPARENT_BLOCKS,
    HAZARD_BLOCK_TYPES,
    SURFACE_SHORE_PRIORITY_BLOCKS,
    SHORE_LAND_BLOCKS,
    NETHER_SHORE_BLOCKS,
    LAVA_SHORE_BLOCKS,
    TRAP_BLOCKS,
    SAFE_TACTICAL_FILLERS,
    MELEE_WEAPON_ORDER,
    LONE_COUNTERKILL_MOBS,
    CORNERED_PIGLIN_MAX_HEALTH,
    CORNERED_PIGLIN_REACH,
    PRE_HURT_MELEE_MOBS,
    RANGED_MOBS,
    WARDEN_EVADE_DISTANCE,
    CARDINAL_VECTORS,
    attack_feedback_hits,
    shot_release_confirmed,
    _clean_block_name,
    _is_passable,
    _is_los_transparent,
    _is_full_cover_block,
    _is_lava_block,
    _water_requires_surface,
    _combat_status_unavailable,
    _hitbox_columns,
    footing_blocks,
    _is_control_signal,
    retreat_made_progress,
    ranged_retreat_positions,
    melee_clear_for_projectile_cover,
    _is_solid_ground,
    aim_yaw_pitch,
    yaw_to_unit_xz,
    ThreatTrigger,
    CombatOutcome,
    CombatController,
    _TerrainProbeExpired,
    _CombatFortificationInterrupted,
)


class _LavaCoverFleeMixin:
    def _escape_lava(self, *, force_rim_escape: bool) -> dict[str, Any]:
        def _landing_unconfirmed(status: dict[str, Any]) -> bool:
            return status.get("on_ground") is False and not status.get("in_water")

        try:
            st = self.bot.status()
        except (ApiError, httpx.HTTPError):
            self._best_effort_stop()
            return {
                "ok": False,
                "in_lava": True,
                "timeout": True,
                "pos_after": None,
                "actions": ["lava_status_timeout"],
            }
        rim_lava_active = False
        if not bool(st.get("in_lava")):
            if st.get("alive") is not False and st.get("ready") is not False:
                self.invalidate_block_cache()
                pos_chk = st.get("pos") or [0.0, 64.0, 0.0]
                rim_lava_active = self._is_airborne_above_lava(st, start_pos=pos_chk) or (
                    force_rim_escape and self._is_near_or_over_lava(st)
                )
            if not rim_lava_active:
                if not _landing_unconfirmed(st):
                    self._stalled_lava_shores.clear()
                    self._stalled_lava_swim = None
                unconfirmed = _landing_unconfirmed(st)
                return {
                    "ok": not unconfirmed,
                    "in_lava": False,
                    "already_dry": True,
                    "pos_after": st.get("pos"),
                    "actions": [],
                    **({"reason": "lava_landing_unconfirmed"} if unconfirmed else {}),
                }

        actions: list[str] = []
        notch_dig_failures: list[dict[str, Any]] = []
        lava_deadline = time.monotonic() + 5.0
        # 1. Surface before spending time on a bucket or scanning for a shore.
        self.invalidate_block_cache()
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]
        start_x, start_z = float(pos0[0]), float(pos0[2])
        dim0 = str(st.get("dimension") or "").lower()
        last_death0 = st.get("last_death")

        def _did_respawn_during_lava(st_now: dict[str, Any]) -> bool:
            pos_now = st_now.get("pos") or pos0
            moved = math.hypot(float(pos_now[0]) - start_x, float(pos_now[2]) - start_z)
            dim_now = str(st_now.get("dimension") or "").lower()
            ld_now = st_now.get("last_death")
            return bool(
                st_now.get("alive") is False
                or st_now.get("ready") is False
                or moved > 64.0
                or (dim0 and dim_now and dim_now != dim0)
                or (ld_now is not None and ld_now != last_death0)
            )

        def _should_exit_lava(st_now: dict[str, Any]) -> bool:
            if _did_respawn_during_lava(st_now):
                return True
            if (
                st_now.get("in_lava") or _landing_unconfirmed(st_now)
                or self._is_airborne_above_lava(st_now, start_pos=pos0)
            ):
                return False
            if rim_lava_active:
                p_chk = st_now.get("pos") or pos0
                moved_r = math.hypot(float(p_chk[0]) - float(pos0[0]), float(p_chk[2]) - float(pos0[2]))
                if moved_r < 0.45 and self._is_near_or_over_lava(st_now):
                    return False
            return True

        def _lava_result(st_now: dict[str, Any]) -> dict[str, Any]:
            still_in_lava = bool(st_now.get("in_lava"))
            pos_now = st_now.get("pos") or pos0
            respawned = _did_respawn_during_lava(st_now)
            if respawned or (not still_in_lava and not _landing_unconfirmed(st_now)):
                self._stalled_lava_shores.clear()
                self._stalled_lava_swim = None
            airborne_over_lava = (
                (not respawned)
                and (not still_in_lava)
                and self._is_airborne_above_lava(st_now, start_pos=pos0)
            )
            unconfirmed = not respawned and not still_in_lava and _landing_unconfirmed(st_now)
            return {
                "ok": not (respawned or still_in_lava or unconfirmed or airborne_over_lava),
                "in_lava": still_in_lava,
                "pos_after": pos_now,
                "actions": actions,
                **({"notch_dig_failures": notch_dig_failures} if notch_dig_failures else {}),
                **({"respawned": True} if respawned else {}),
                **({"airborne_above_lava": True} if airborne_over_lava else {}),
                **({"reason": "lava_landing_unconfirmed"} if unconfirmed else {}),
            }

        def jump_in_lava(ms: int, *, advance: bool) -> None:
            self.bot.call(
                "control", {"forward": advance, "jump": True, "sprint": advance, "ms": ms, "stop_on": ["death"]},
                timeout_s=ms / 1000.0 + 0.6, allow_reauth=False,
            )

        def critical_in_nether(status: dict[str, Any]) -> bool:
            health = status.get("health")
            return (
                "nether" in dim0 and status.get("in_lava") is True
                and isinstance(health, (int, float)) and 0 < health <= 6.0
            )

        tried_recent_bank = False
        max_ascent_pulses = 3
        for ascent in range(max_ascent_pulses + 1):
            if time.monotonic() >= lava_deadline:
                break
            px, py, pz = map(float, pos0[:3])
            hp_now = st.get("health")
            bank_target = None
            if (
                not tried_recent_bank and self._last_dry_bank is not None
                and st.get("in_lava") and isinstance(hp_now, (int, float)) and 0 < hp_now <= 12.0
            ):
                bank, bank_life, bank_at = self._last_dry_bank
                distance = math.hypot(bank[0] - px, bank[2] - pz)
                if (
                    bank_life == (st.get("dimension"), st.get("last_death")) and time.monotonic() - bank_at < 30.0
                    and 0.6 <= distance <= 3.0 and 0 <= bank[1] - py <= 1.5
                    and self._point_allowed(bank[0], bank[2], action="escape_lava")
                    and time.monotonic() + 1.25 < lava_deadline
                ):
                    tried_recent_bank = True
                    if self._dry_bank_is_walkable(bank) and time.monotonic() + 1.25 < lava_deadline:
                        bank_target = bank
            if bank_target is not None:
                try:
                    yaw, _ = aim_yaw_pitch(pos0, bank_target)
                    self.bot.call("look", {"yaw": yaw, "pitch": 0.0}, timeout_s=0.35, allow_reauth=False)
                    jump_in_lava(950, advance=True)
                    actions.append("lava_swim_to_recent_dry_bank")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.5)
                self.invalidate_block_cache()
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                pos0 = st.get("pos") or pos0
                continue
            # The last sample can confirm a reachable bank without another stationary pulse.
            if ascent == max_ascent_pulses:
                break
            head_deadline = time.monotonic() + 0.18 if critical_in_nether(st) else None
            try:
                head = self._get_block_name(math.floor(px), math.floor(py) + 1, math.floor(pz), deadline=head_deadline)
            except _TerrainProbeExpired:
                break
            if not _is_lava_block(head) or time.monotonic() >= lava_deadline:
                break
            try:
                # The current yaw can still face the attacker that knocked us in.
                # Preserve the horizontal position until the shore probe picks a heading.
                jump_in_lava(950, advance=False)
                actions.append("lava_surface_swim")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    actions.append("lava_surface_swim_timeout")
                    self._best_effort_stop(timeout_s=0.5)
            self.invalidate_block_cache()
            st = self._safe_status(st)
            if _should_exit_lava(st):
                return _lava_result(st)
            pos0 = st.get("pos") or pos0
            if float(pos0[1]) - py < 0.1:
                break

        try:
            inv = None
            if critical_in_nether(st):
                observed = self.bot.call("inventory", method="GET", timeout_s=0.2, allow_reauth=False)
                inv = observed if isinstance(observed, dict) else {}
            counts, _, held_name = self._inventory_counts_and_shield(inv)
        except (ApiError, httpx.HTTPError):
            counts, held_name = {}, None
        if counts.get("water_bucket", 0) > 0 and "nether" not in dim0 and time.monotonic() < lava_deadline:
            px, py, pz = map(float, pos0[:3])
            head = self._get_block_name(math.floor(px), math.floor(py) + 1, math.floor(pz), strict=True)
            if head and not _is_lava_block(head):
                try:
                    douse = self.build_tactical("place_water_feet")
                    actions.append("place_water_feet" if douse.get("ok") else "place_water_feet_failed")
                except (ApiError, httpx.HTTPError):
                    actions.append("place_water_feet_failed")
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                try:
                    self.bot.call("use_item", {"item": "water_bucket"})
                    actions.append("use_water_bucket")
                except (ApiError, httpx.HTTPError):
                    pass
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                pos0 = st.get("pos") or pos0
                self.invalidate_block_cache()

        # 2. Try scanning safe non-lava exits (including 1-block step-ups, plus wide shore search) and jump-sprinting out
        terrain_started = time.monotonic()
        health_after_ascent = st.get("health")
        critical_nether_lava = critical_in_nether(st)
        # With only a few lava hits left, leave time to launch a swim or lay the first block.
        terrain_budget = 0.65 if critical_nether_lava else 1.5
        terrain_deadline = min(lava_deadline - 2.0, terrain_started + terrain_budget)
        ray_deadline = terrain_deadline
        if st.get("in_lava"):
            # Nearby rays can all start in lava. Leave time to query and verify a reachable shore.
            shore_budget = 0.5 if critical_nether_lava else 0.9
            ray_deadline = max(terrain_started, terrain_deadline - shore_budget)
        scan = self.scan_safe_yaws(
            pos0,
            float(st.get("yaw") or 0.0),
            dist=2.8,
            angles_deg=(0, 45, -45, 90, -90, 135, -135, 180),
            deadline=ray_deadline,
        )
        tried_yaws: list[float] = []
        if scan.get("best_yaw") is not None:
            tried_yaws.append(float(scan["best_yaw"]))
        for c in scan.get("candidates") or []:
            if c.get("hazard") not in {"lava", "geofence"} and float(c.get("yaw", 0.0)) not in tried_yaws:
                if c.get("safe") or c.get("hazard") in {None, "water"}:
                    tried_yaws.append(float(c["yaw"]))

        py = math.floor(float(pos0[1]) + 0.05)
        lava_shore_target: list[float] | None = None
        filler = self._pick_filler_block(counts)
        blind_swim = False
        defer_blind_swim = False
        now = time.monotonic()
        self._stalled_lava_shores = {
            bank: (rim, at) for bank, (rim, at) in self._stalled_lava_shores.items() if now - at < 15.0
        }

        def stalled_shores_near(pos: Sequence[float]) -> set[tuple[float, ...]]:
            return {
                bank for bank, (rim, at) in self._stalled_lava_shores.items()
                if time.monotonic() - at < 15.0 and abs(pos[1] - rim[1]) <= 2.0
                and math.hypot(pos[0] - rim[0], pos[2] - rim[2]) <= 1.25
            }

        if not tried_yaws and time.monotonic() < lava_deadline:
            # Wide lava lake: filter shore types by dimension to keep find_blocks fast (<1s).
            shore_blocks = NETHER_SHORE_BLOCKS if "nether" in dim0 else SHORE_LAND_BLOCKS
            excluded_shores = stalled_shores_near(pos0)
            try:
                lava_shore_target = self._find_walkable_shore_target(
                    pos0,
                    block_types=shore_blocks,
                    max_radius=18,
                    min_y=py - 1,
                    max_y=py + 2,
                    max_candidates=6,
                    deadline=terrain_deadline,
                    excluded_shores=excluded_shores,
                )
            except _TerrainProbeExpired:
                pass
            if lava_shore_target is not None:
                tried_yaws = [aim_yaw_pitch(pos0, lava_shore_target)[0]]
            else:
                if excluded_shores:
                    actions.append("lava_shore_path_stalled")
                base_yaw = float(st.get("yaw") or 0.0)
                best_shore_yaw: float | None = None
                for probe_dist in (4.5, 9.0):
                    if time.monotonic() >= terrain_deadline:
                        break
                    for deg in (0, 90, -90, 180, 45, -45, 135, -135):
                        if time.monotonic() >= terrain_deadline:
                            break
                        cand_yaw = base_yaw + math.radians(deg)
                        ux, uz = yaw_to_unit_xz(cand_yaw)
                        px = math.floor(float(pos0[0]) + ux * probe_dist)
                        pz = math.floor(float(pos0[2]) + uz * probe_dist)
                        for y_chk in (py, py + 1):
                            try:
                                floor = self._get_block_name(px, y_chk, pz, deadline=terrain_deadline)
                                if _is_solid_ground(floor) and _is_passable(self._get_block_name(px, y_chk + 1, pz, deadline=terrain_deadline)):
                                    best_shore_yaw = cand_yaw
                                    break
                            except _TerrainProbeExpired:
                                break
                        if best_shore_yaw is not None:
                            break
                    if best_shore_yaw is not None:
                        break
                blind_swim = best_shore_yaw is None
                tried_yaws = [base_yaw if blind_swim else best_shore_yaw]
                stalled_swim = self._stalled_lava_swim
                if blind_swim and stalled_swim is not None:
                    stalled_pos, stalled_yaw, stalled_dim, stalled_at = stalled_swim
                    if (
                        stalled_dim == dim0 and time.monotonic() - stalled_at < 15.0
                        and abs(pos0[1] - stalled_pos[1]) <= 2.0
                        and math.hypot(pos0[0] - stalled_pos[0], pos0[2] - stalled_pos[2]) <= 1.25
                        and abs(math.remainder(base_yaw - stalled_yaw, math.tau)) <= math.radians(15)
                    ):
                        tried_yaws = []
                        for offset in (math.pi / 2, -math.pi / 2, math.pi):
                            candidate = base_yaw + offset
                            ux, uz = yaw_to_unit_xz(candidate)
                            if self._point_allowed(pos0[0] + ux * 2.8, pos0[2] + uz * 2.8, action="escape_lava"):
                                tried_yaws.append(candidate)
                                break
                        actions.append("lava_swim_heading_stalled")
                # At critical health, reserve time for usable blocks before following an unverified yaw.
                defer_blind_swim = (
                    blind_swim and bool(tried_yaws) and filler is not None
                    and isinstance(health_after_ascent, (int, float)) and health_after_ascent < 10.0
                )

        if critical_nether_lava:
            current = self._safe_status(timeout_s=0.25)
            if not current:
                return {**_lava_result(st), "ok": False, "reason": "lava_status_unavailable"}
            st = current
            if _should_exit_lava(st):
                return _lava_result(st)

        swim_ms = 1400 if (lava_shore_target is not None or len(tried_yaws) == 1) else 950

        def _run_jump_sprint(cyaw: float) -> bool:
            nonlocal st
            swim_start = st.get("pos") or pos0
            completed = False
            try:
                self.bot.call("look", {"yaw": cyaw, "pitch": 0.0}, timeout_s=0.6, allow_reauth=False)
                jump_in_lava(swim_ms, advance=True)
                completed = True
                actions.append("lava_jump_sprint")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    actions.append("lava_jump_sprint_timeout")
                    self._best_effort_stop(timeout_s=0.5)
            self.invalidate_block_cache()
            observed = self._safe_status()
            if observed:
                st = observed
                if blind_swim and completed and st.get("in_lava") and st.get("pos") and not _did_respawn_during_lava(st):
                    end = st["pos"]
                    advanced = math.hypot(end[0] - swim_start[0], end[2] - swim_start[2]) >= 0.35
                    self._stalled_lava_swim = None if advanced else (tuple(end[:3]), cyaw, dim0, time.monotonic())
            return _should_exit_lava(st)

        if tried_yaws and not defer_blind_swim and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[0]):
                return _lava_result(st)
            landing = st.get("pos")
            if (
                lava_shore_target is not None and landing and not st.get("in_lava") and st.get("on_ground") is False
                and math.hypot(landing[0] - lava_shore_target[0], landing[2] - lava_shore_target[2]) <= 0.35
                and -0.05 <= landing[1] - lava_shore_target[1] <= 0.9
                and lava_deadline - time.monotonic() >= 0.35
            ):
                # The swim pulse can end above the bank just before the next physics tick lands.
                time.sleep(0.1)
                try:
                    st = self.bot.call("status", method="GET", timeout_s=0.25, allow_reauth=False)
                    actions.append("lava_shore_landing_checked")
                except (ApiError, httpx.HTTPError):
                    pass
                if _should_exit_lava(st):
                    return _lava_result(st)

        # 2b. Short pathfinder step onto close shore (`<=6.5m`) guarded against pathfinder ReadTimeout stalls.
        if lava_shore_target is not None and time.monotonic() + 1.5 < lava_deadline:
            swim_pos = st.get("pos") or pos0
            shore_d = math.hypot(lava_shore_target[0] - float(swim_pos[0]), lava_shore_target[2] - float(swim_pos[2]))
            bank = tuple(lava_shore_target)
            if bank in stalled_shores_near(swim_pos):
                actions.append("lava_shore_path_stalled")
            elif shore_d <= 6.5:
                # The swim can leave the bot with only a few ticks of health.
                critical_shore = critical_in_nether(st)
                if critical_shore:
                    actions.append("lava_shore_critical_budget")
                # Keep the attempted bank if the recovery deadline interrupts the status recheck.
                self._stalled_lava_shores[bank] = (tuple(swim_pos[:3]), time.monotonic())
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": lava_shore_target[0],
                            "y": lava_shore_target[1],
                            "z": lava_shore_target[2],
                            # Keep the entire hitbox on the confirmed bank tile.
                            "range": 0.15,
                            "timeout": 500 if critical_shore else 1200,
                            "stop_on": ["death"],
                        },
                        timeout_s=0.8 if critical_shore else 2.0,
                        allow_reauth=False,
                    )
                    actions.append("lava_move_to_shore")
                except (ApiError, httpx.HTTPError) as err:
                    actions.append("lava_move_to_shore_timeout" if isinstance(err, httpx.HTTPError) else "lava_move_to_shore_err")
                    self._best_effort_stop(timeout_s=0.25 if critical_shore else 0.5)
                self.invalidate_block_cache()
                if critical_shore:
                    observed = self._safe_status(timeout_s=0.25)
                    if not observed:
                        return {**_lava_result(st), "ok": False, "reason": "lava_status_unavailable"}
                    st = observed
                else:
                    st = self._safe_status(st)
                if _should_exit_lava(st):
                    return _lava_result(st)
                end = st.get("pos") or swim_pos
                remaining = math.hypot(bank[0] - end[0], bank[2] - end[2])
                if remaining >= shore_d - 0.75:
                    self._stalled_lava_shores[bank] = (tuple(end[:3]), time.monotonic())
                else:
                    self._stalled_lava_shores.pop(bank, None)

        # 3. Boxed-in pit or deep lava: try bridging beneath feet or against adjacent solid bank/floor
        if filler is not None and time.monotonic() < lava_deadline + 0.8:
            bridge_deadline = lava_deadline + 0.8
            checked_bridge_landing = False
            p_cur = st.get("pos") or pos0
            bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
            place_targets = [[bx, by, bz], [bx, by - 1, bz]]

            def bridge_call(
                action: str, body: dict[str, Any] | None, *, timeout_s: float, **kwargs: Any,
            ) -> dict[str, Any]:
                remaining = bridge_deadline - time.monotonic()
                if remaining <= 0:
                    raise _TerrainProbeExpired
                return self.bot.call(action, body, timeout_s=min(timeout_s, remaining), allow_reauth=False, **kwargs)

            bridge_probe_budget = 0.25 if critical_in_nether(st) else 0.6
            probe_until = min(bridge_deadline - 0.8, time.monotonic() + bridge_probe_budget)
            try:
                if st.get("on_ground") is False and _is_passable(self._get_block_name(bx, by - 1, bz, deadline=probe_until)):
                    place_targets.append([bx, by - 2, bz])
                for dx, dz in CARDINAL_VECTORS.values():
                    for cy in (by, by - 1):
                        tx, tz = bx + dx, bz + dz
                        if not self._point_allowed(tx + 0.5, tz + 0.5, action="escape_lava"):
                            continue
                        cur_bn = self._get_block_name(tx, cy, tz, deadline=probe_until)
                        if cur_bn not in {"lava", "flowing_lava"} and not _is_passable(cur_bn):
                            continue
                        has_support = any(
                            _is_solid_ground(self._get_block_name(tx + ox, cy + oy, tz + oz, deadline=probe_until))
                            for ox, oy, oz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, -1, 0), (0, 1, 0))
                        )
                        if has_support and [tx, cy, tz] not in place_targets:
                            place_targets.append([tx, cy, tz])
                            if len(place_targets) >= 5:
                                break
                    if len(place_targets) >= 5:
                        break
            except _TerrainProbeExpired:
                actions.append("lava_bridge_probe_incomplete")
                try:
                    st = bridge_call("status", None, timeout_s=0.25, method="GET")
                except (_TerrainProbeExpired, ApiError, httpx.HTTPError):
                    pass
                if _should_exit_lava(st):
                    return _lava_result(st)

            for place_pos in place_targets:
                if time.monotonic() >= bridge_deadline:
                    break
                try:
                    bridge_call("control", {"jump": True, "ms": 220, "wait": False}, timeout_s=0.4)
                    time.sleep(0.18)
                    bridge_call("place", {"item": filler, "pos": place_pos}, timeout_s=0.7)
                    actions.append(f"lava_place_{filler}")
                    self.invalidate_block_cache()
                    side_step = place_pos[0] != bx or place_pos[2] != bz
                    if side_step:
                        try:
                            byaw, _ = aim_yaw_pitch(p_cur, [place_pos[0] + 0.5, place_pos[1] + 1.0, place_pos[2] + 0.5])
                            bridge_call("look", {"yaw": byaw, "pitch": 0.0}, timeout_s=0.35)
                            bridge_call(
                                "control", {"forward": True, "jump": True, "sprint": True, "ms": 650, "stop_on": ["death"]},
                                timeout_s=1.0,
                            )
                            actions.append("lava_step_bridge")
                        except (ApiError, httpx.HTTPError) as err_step:
                            if isinstance(err_step, httpx.HTTPError):
                                self._best_effort_stop(timeout_s=0.5)
                    st = self._safe_status(st)
                    if (
                        not checked_bridge_landing and not st.get("in_lava") and _landing_unconfirmed(st)
                        and not _did_respawn_during_lava(st) and bridge_deadline - time.monotonic() >= 0.2
                    ):
                        # Allow a jump that cleared the lava to settle before trying another placement.
                        checked_bridge_landing = True
                        landing_deadline = min(bridge_deadline, time.monotonic() + 0.35)
                        for sample in range(2):
                            if sample:
                                # The bot can land with its center over the bridge and its hitbox straddling the edge.
                                p_landing = st.get("pos")
                                if (
                                    not side_step or st.get("in_lava") or not _landing_unconfirmed(st)
                                    or _did_respawn_during_lava(st) or not p_landing
                                    or p_landing[1] < place_pos[1] + 0.95
                                    or (math.floor(p_landing[0]), math.floor(p_landing[2])) != (place_pos[0], place_pos[2])
                                ):
                                    break
                            if landing_deadline - time.monotonic() <= 0.1:
                                break
                            time.sleep(0.1)
                            remaining = landing_deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            try:
                                st = self.bot.call("status", method="GET", timeout_s=min(0.25, remaining), allow_reauth=False)
                            except (ApiError, httpx.HTTPError):
                                break
                    if _should_exit_lava(st):
                        if st.get("on_ground") is True and not st.get("in_water") and not _did_respawn_during_lava(st):
                            # Residual movement can carry the bot off either kind of one-block landing.
                            try:
                                bridge_call("posture", {"sneak": True, "jump": False, "shield": bool(st.get("blocking"))}, timeout_s=0.35)
                                actions.append("lava_bridge_sneak")
                                st = bridge_call("status", None, timeout_s=0.35, method="GET")
                            except (_TerrainProbeExpired, ApiError, httpx.HTTPError):
                                return {**_lava_result(st), "ok": False, "reason": "lava_bridge_landing_unconfirmed"}
                            if not _did_respawn_during_lava(st) and st.get("on_ground") is not True:
                                return {**_lava_result(st), "ok": False, "reason": "lava_landing_unconfirmed"}
                        return _lava_result(st)
                except _TerrainProbeExpired:
                    break
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.5)
                    continue

        if defer_blind_swim and st.get("in_lava") and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[0]):
                return _lava_result(st)
        if len(tried_yaws) > 1 and time.monotonic() < lava_deadline:
            if _run_jump_sprint(tried_yaws[1]):
                return _lava_result(st)

        # 4. Last resort: carve a 2-high notch out of an adjacent solid bank and leap onto it
        notch_deadline = lava_deadline + 0.8
        now = time.monotonic()
        self._stalled_lava_notch = {key: until for key, until in self._stalled_lava_notch.items() if now < until}
        for dname, (dx, dz) in CARDINAL_VECTORS.items():
            if time.monotonic() >= lava_deadline:
                break
            p_cur = st.get("pos") or pos0
            bx, by, bz = math.floor(float(p_cur[0])), math.floor(float(p_cur[1]) + 0.05), math.floor(float(p_cur[2]))
            ex, ez = bx + dx, bz + dz
            if not self._point_allowed(ex + 0.5, ez + 0.5, action="escape_lava"):
                continue
            foot = self._get_block_name(ex, by, ez, strict=True)
            if not foot or foot in HAZARD_BLOCK_TYPES:
                continue
            floor_y = by if _is_solid_ground(foot) else by - 1
            floor = self._get_block_name(ex, floor_y, ez, strict=True)
            if not floor or not _is_solid_ground(floor) or floor in HAZARD_BLOCK_TYPES:
                continue
            clearance = (floor_y + 1, floor_y + 2)
            blocks = [self._get_block_name(ex, cy, ez, strict=True) for cy in clearance]
            if any(not block or block in HAZARD_BLOCK_TYPES or block == "bedrock" for block in blocks):
                continue
            if any(block in {"obsidian", "crying_obsidian"} for block in blocks):
                # Neither block can clear within the one-second emergency dig budget.
                actions.append("lava_notch_hard_block")
                continue
            dig_keys = [
                (dim0, (ex, cy, ez), block, held_name)
                for cy, block in zip(clearance, blocks)
                if not _is_passable(block)
            ]
            now = time.monotonic()
            if any(self._stalled_lava_notch.get(key, now) > now for key in dig_keys):
                actions.append("lava_notch_dig_deferred")
                continue
            for key in dig_keys:
                # Reserve time to reach the notch after opening it.
                dig_budget = min(1.0, notch_deadline - time.monotonic() - 1.5)
                if dig_budget < 0.25:
                    break
                try:
                    self.bot.call(
                        "dig", {"pos": list(key[1]), "collect": False, "stop_on": ["death"]},
                        timeout_s=dig_budget, allow_reauth=False,
                    )
                except (ApiError, httpx.HTTPError) as err:
                    error = err.code if isinstance(err, ApiError) else type(err).__name__
                    notch_dig_failures.append({
                        "pos": list(key[1]), "block": key[2], "held": key[3], "error": error,
                    })
                    timed_out = isinstance(err, httpx.TimeoutException) or error in {"timeout", "action_timeout"}
                    if timed_out:
                        self._stalled_lava_notch[key] = time.monotonic() + 15.0
                        actions.append("lava_notch_dig_timeout")
                    if timed_out or isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.25)
                    break
            if time.monotonic() + 1.25 >= notch_deadline:
                break
            self.invalidate_block_cache()
            floor = self._get_block_name(ex, floor_y, ez, strict=True)
            if (
                floor and _is_solid_ground(floor) and floor not in HAZARD_BLOCK_TYPES
                and all(_is_passable(self._get_block_name(ex, cy, ez, strict=True)) for cy in clearance)
            ):
                yaw_out = math.atan2(-(ex + 0.5 - float(p_cur[0])), -(ez + 0.5 - float(p_cur[2])))
                try:
                    self.bot.call("look", {"yaw": yaw_out, "pitch": 0.0}, timeout_s=0.3, allow_reauth=False)
                    self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "sprint": True, "ms": 1000, "stop_on": ["death"]},
                        timeout_s=1.3, allow_reauth=False,
                    )
                    actions.append(f"lava_notch_{dname}")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.25)
                self.invalidate_block_cache()
                st = self._safe_status(st)
                if _should_exit_lava(st):
                    break

        st_end = self._safe_status(st)
        still_in_lava = bool(st_end.get("in_lava"))
        if still_in_lava and st_end.get("alive") is not False and st_end.get("ready") is not False:
            try:
                self.bot.call("posture", {"jump": True})
            except (ApiError, httpx.HTTPError):
                pass
        return _lava_result(st_end)


    def _blocks_shot(
        self, pos: Sequence[float], threat_pos: Sequence[float], *, deadline: float | None = None,
    ) -> bool:
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        for height in (0.75, 1.6):
            blocker = self.has_los(threat_pos, pos, dst_height=height, **probe_kwargs).get("blocker")
            if blocker is None:
                return False
            name = self._get_block_name(*blocker["pos"], strict=True, **probe_kwargs)
            if not name or _is_los_transparent(name):
                return False
        return True


    def _blocks_shot_across_hitbox(
        self, pos: Sequence[float], threat_pos: Sequence[float], *, deadline: float | None = None,
    ) -> bool:
        dx, dz = threat_pos[0] - pos[0], threat_pos[2] - pos[2]
        horizontal = math.hypot(dx, dz)
        samples = [pos]
        if horizontal > 0.05:
            # Check both sides of the player's hitbox where it can extend around a corner.
            side_x, side_z = -0.3 * dz / horizontal, 0.3 * dx / horizontal
            samples.extend((
                [pos[0] + side_x, pos[1], pos[2] + side_z],
                [pos[0] - side_x, pos[1], pos[2] - side_z],
            ))
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        return all(self._blocks_shot(sample, threat_pos, **probe_kwargs) for sample in samples)


    def _creeper_cover_blocks_exposure(
        self, pos: Sequence[float], threat: Sequence[float], column: Collection[tuple[int, int, int]], *,
        deadline: float | None, planned_cover: Collection[tuple[int, int, int]] = (),
    ) -> bool:
        # Check the near-ground source against the feet, body and head across the hitbox.
        offsets = (-0.29, 0, 0.29)
        for dx in offsets:
            for dz in offsets:
                for height in (0.1, 0.9, 1.7):
                    blocker = self.has_los(
                        threat, [pos[0] + dx, pos[1], pos[2] + dz], src_eye=0.1, dst_height=height,
                        deadline=deadline, planned_cover=planned_cover,
                    )["blocker"]
                    if not blocker or tuple(blocker["pos"]) not in column or not _is_full_cover_block(blocker["name"]):
                        return False
        return True


    def arrow_cover_material(self, counts: dict[str, int]) -> str | None:
        # Prefer a stack that can finish a two-block wall before relying on an existing partial column.
        for minimum in (2, 1):
            available = {name: count for name, count in counts.items() if count >= minimum}
            material = self._pick_filler_block(available) or next((name for name in available if name.endswith("_log")), None)
            if material is not None:
                return material
        return None


    def place_arrow_cover(
        self, before: dict[str, Any], shooter: Sequence[float], counts: dict[str, int],
    ) -> dict[str, Any]:
        return self._place_stationary_cover(before, shooter, counts)


    @staticmethod
    def _on_same_footing(before: dict[str, Any], status: dict[str, Any]) -> bool:
        return bool(
            before.get("pos") and status.get("pos") and math.dist(status["pos"][:3], before["pos"][:3]) <= 0.2
            and status.get("ready") is True and status.get("alive") is True and status.get("on_ground") is True
            and not any(status.get(key) for key in ("busy", "in_water", "in_lava", "on_fire"))
            and all(status.get(key) == before.get(key) for key in ("last_death", "dimension"))
        )


    def _place_stationary_cover(
        self, before: dict[str, Any], threat: Sequence[float], counts: dict[str, int], *,
        melee: bool = False, creeper_id: int | None = None, deadline: float | None = None,
    ) -> dict[str, Any]:
        """Build close cover on a supported column without leaving the current footing."""
        origin = before["pos"]
        filler = self.arrow_cover_material(counts)
        failed = {"ok": False, "mode": "placed_cover", "placed": 0, "placement_attempts": 0}
        distance = math.hypot(threat[0] - origin[0], threat[2] - origin[2])
        blast = creeper_id is not None
        in_range = distance > 3.5
        if melee or blast:
            in_range = (1.6 if blast else 1.8) <= distance <= 3.5 and abs(threat[1] - origin[1]) < 0.8
        if filler is None or not in_range:
            return failed

        yaw, _ = aim_yaw_pitch(origin, threat)
        dx, dz = yaw_to_unit_xz(yaw)
        y = math.floor(origin[1] + 0.05)
        occupied = _hitbox_columns(origin[0], origin[2])
        self.invalidate_block_cache()
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        if melee or blast:
            occupied.extend(_hitbox_columns(threat[0], threat[2]))
            try:
                if not self.has_safe_footing(origin, require_observation=True, **probe_kwargs):
                    return failed
            except _TerrainProbeExpired:
                return failed
        columns = dict.fromkeys(
            (math.floor(origin[0] + dx * distance), math.floor(origin[2] + dz * distance))
            for distance in (0.65, 1.1, 1.55, 2.0)
        )
        action = "place_melee_cover" if melee else "place_arrow_cover"
        if blast:
            action = "place_blast_cover"
        for x, z in columns:
            if (x, z) in occupied or not self._point_allowed(x + 0.5, z + 0.5, action=action):
                continue
            # A supported column within reach keeps the place endpoint from pathfinding.
            floor = self._get_block_name(x, y - 1, z, strict=True, **probe_kwargs)
            if not floor or floor in HAZARD_BLOCK_TYPES or not _is_solid_ground(floor):
                continue
            cells = [(x, y, z), (x, y + 1, z)]
            blocks = [self._get_block_name(*cell, strict=True, **probe_kwargs) for cell in cells]
            if any(not block or block in HAZARD_BLOCK_TYPES or (
                block not in AIR_LIKE_BLOCKS and _is_los_transparent(block)
            ) for block in blocks):
                continue
            to_place = [cell for cell, block in zip(cells, blocks) if block in AIR_LIKE_BLOCKS]
            if not to_place or len(to_place) > counts[filler]:
                continue
            if blast and not self._creeper_cover_blocks_exposure(
                origin, threat, cells, deadline=deadline, planned_cover=to_place,
            ):
                continue
            break
        else:
            return failed

        placed = 0
        placement_attempts = 0
        placement_timed_out = False
        for cell in to_place:
            if deadline is not None and time.monotonic() + 0.65 >= deadline:
                break
            current = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
            if not self._on_same_footing(before, current):
                break
            if blast:
                nearest = current.get("nearest_hostile")
                if _combat_status_unavailable(current) or not isinstance(nearest, dict) or nearest.get("id") != creeper_id:
                    break
            placement_attempts += 1
            try:
                self.bot.call("place", {"item": filler, "pos": list(cell)}, timeout_s=0.65, allow_reauth=False)
            except ApiError:
                break
            except httpx.ReadTimeout:
                # The server may already have placed the block. Read it back once and stop building.
                placement_timed_out = True
                self._best_effort_stop(stop_control=False, timeout_s=0.2)
            self.invalidate_block_cache()
            block = self._get_block_name(*cell, strict=True)
            if block != filler:
                break
            placed += 1
            if placement_timed_out:
                break

        after = self.bot.call("status", method="GET", timeout_s=0.35, allow_reauth=False)
        confirmed = (
            placed > 0 and (not (melee or blast) or placed == len(to_place)) and self._on_same_footing(before, after)
        )
        if confirmed:
            try:
                confirmed = (
                    self._creeper_cover_blocks_exposure(after["pos"], threat, cells, deadline=deadline) if blast
                    else self._blocks_shot(after["pos"], threat)
                )
            except _TerrainProbeExpired:
                confirmed = False
        result = {
            "ok": confirmed, "mode": "placed_cover", "placed": placed, "placement_attempts": placement_attempts,
            "pos_after": after.get("pos"),
            **({"cover_cells": cells} if blast else {}),
        }
        if placement_timed_out:
            result["placement_timed_out"] = True
            if not confirmed:
                result["reason"] = "placement_unconfirmed"
        return result


    def seek_natural_cover(
        self, threat_pos: Sequence[float], *, other_shooters: Sequence[Sequence[float]] = (), allow_stationary: bool = True,
        verify_live_shooters: bool = False, expected_life: tuple[Any, Any] | None = None,
        trident_attacker_id: int | None = None, expected_shooter_id: int | None = None, deadline: float | None = None,
        allow_short_step: bool = False,
    ) -> dict[str, Any] | None:
        """Find a close, supported position behind existing terrain and verify the landing."""
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        required_shooter_id = trident_attacker_id if trident_attacker_id is not None else expected_shooter_id

        def status_refresh() -> dict[str, Any]:
            if deadline is None:
                return self._safe_status({"ready": False})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TerrainProbeExpired
            return self._safe_status({"ready": False}, timeout_s=min(0.3, remaining))

        before = self.bot.status() if deadline is None else status_refresh()
        if deadline is not None and not before.get("pos") and before.get("ready") is False:
            return None
        if expected_life is not None and expected_life != (before.get("dimension"), before.get("last_death")):
            return {"ok": False, "mode": "natural_cover", "reason": "life_changed", "respawned": True}
        origin = before.get("pos")
        if (
            not origin or before.get("on_ground") is not True
            or before.get("alive") is False or before.get("ready") is False
            or before.get("in_water") or before.get("in_lava")
        ):
            return None
        self.invalidate_block_cache()
        if required_shooter_id is not None and not self.has_safe_footing(origin, **probe_kwargs):
            return None
        shooters = (threat_pos, *other_shooters)
        blocks_shot = self._blocks_shot_across_hitbox if verify_live_shooters else self._blocks_shot
        shooter_kwargs = {
            "trident_attacker_id": trident_attacker_id, "expected_shooter_id": expected_shooter_id, **probe_kwargs,
        }
        # A fresh blast at this location overrides otherwise favorable sight lines.
        already_covered = allow_stationary and all(blocks_shot(origin, shooter, **probe_kwargs) for shooter in shooters)
        if not already_covered:
            away, _ = aim_yaw_pitch(threat_pos, origin)
            distances = (1.15, 2.2) if allow_short_step else (2.2,)
            for distance, offset in ((d, angle) for d in distances for angle in (90, -90, 45, -45)):
                yaw = away + math.radians(offset)
                route = self.probe_retreat(origin, yaw, distance, **probe_kwargs)
                if route is None:
                    continue
                if distance < 2.0 and route != (math.floor(origin[1] + 0.05), False):
                    continue
                ux, uz = yaw_to_unit_xz(yaw)
                target = [origin[0] + ux * distance, route[0], origin[2] + uz * distance]
                if all(blocks_shot(target, shooter, **probe_kwargs) for shooter in shooters):
                    break
            else:
                return None
            if allow_short_step and not self._blocks_live_shots(target, **shooter_kwargs):
                return None

        current = status_refresh()
        current_pos = current.get("pos")
        if (
            not current_pos or math.dist(current_pos[:3], origin[:3]) > 0.18
            or current.get("on_ground") is not True
            or current.get("alive") is False or current.get("ready") is False
            or current.get("in_water") or current.get("in_lava")
            or current.get("last_death") != before.get("last_death")
            or current.get("dimension") != before.get("dimension")
        ):
            return None
        if required_shooter_id is not None and (
            current.get("busy") or current.get("submerged") or current.get("on_fire")
            or not melee_clear_for_projectile_cover([current.get("nearest_hostile")])
        ):
            return None
        if already_covered:
            confirmed = not verify_live_shooters or self._blocks_live_shots(current_pos, **shooter_kwargs)
            return {
                "ok": confirmed, "mode": "natural_cover", "pos_after": current_pos, "moved_dist": 0.0,
                **({"reason": "shooter_lanes_unconfirmed"} if not confirmed else {}),
            }
        # Keep time for confirmation and for stopping a move whose reply is lost.
        move_timeout = 1.5 if deadline is None else min(1.5, deadline - time.monotonic() - 0.6)
        if move_timeout < 0.45:
            return None
        try:
            self.bot.call(
                "move_to",
                {"x": target[0], "y": target[1], "z": target[2], "range": 0.25,
                 "timeout": min(1200, int((move_timeout - 0.25) * 1000)), "stop_on": ["death"]},
                timeout_s=move_timeout, allow_reauth=False,
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop(timeout_s=0.2 if deadline is not None else None)

        after = status_refresh()
        final_pos = after.get("pos")
        self.invalidate_block_cache()
        respawned = after.get("alive") is False or bool(
            final_pos and (after.get("last_death") != before.get("last_death")
                           or after.get("dimension") != before.get("dimension"))
        )
        confirmed = bool(
            final_pos and math.dist(final_pos[:3], target) <= 0.6 and after.get("on_ground") is True
            and after.get("ready") is not False and not respawned
            and not after.get("in_water") and not after.get("in_lava")
            and (required_shooter_id is None or not any(after.get(key) for key in ("busy", "submerged", "on_fire")))
            and all(blocks_shot(final_pos, shooter, **probe_kwargs) for shooter in shooters)
        )
        reason = "cover_unconfirmed" if not confirmed else None
        if confirmed and verify_live_shooters and not self._blocks_live_shots(final_pos, **shooter_kwargs):
            confirmed = False
            reason = "shooter_lanes_unconfirmed"
        if confirmed and allow_short_step and not self._on_same_footing(after, status_refresh()):
            confirmed = False
            reason = "cover_unconfirmed"
        return {
            "ok": confirmed, "mode": "natural_cover", "pos_after": final_pos,
            **({"reason": reason} if reason else {}),
            **({"respawned": True} if respawned else {}),
        }


    def _blocks_live_shots(
        self, pos: Sequence[float], *, trident_attacker_id: int | None = None,
        expected_shooter_id: int | None = None, deadline: float | None = None,
    ) -> bool:
        timeout = 0.4 if deadline is None else min(0.4, deadline - time.monotonic())
        if timeout <= 0:
            return False
        try:
            sighting = self.bot.call("get_entities", {"radius": 16, "types": ["hostile"]}, timeout_s=timeout, allow_reauth=False)
        except (ApiError, httpx.HTTPError):
            return False
        entities = sighting.get("entities")
        if not isinstance(entities, list):
            return False
        shooters = ranged_retreat_positions(
            pos, entities, max_distance=16.0, include_drowned=trident_attacker_id is not None,
        )
        # A wall checked before the entity scan may have changed while the scan was in flight.
        self.invalidate_block_cache()
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        required_shooter_id = trident_attacker_id if trident_attacker_id is not None else expected_shooter_id
        if required_shooter_id is not None and (
            not any(entity_id == required_shooter_id for entity_id, _ in shooters)
            or not melee_clear_for_projectile_cover(entities) or not self.has_safe_footing(pos, **probe_kwargs)
        ):
            return False
        if trident_attacker_id is not None and not any(
            isinstance(entity, dict) and entity.get("id") == trident_attacker_id
            and _clean_block_name(entity.get("name")) == "drowned" for entity in entities
        ):
            return False
        return bool(shooters) and all(self._blocks_shot_across_hitbox(pos, shooter, **probe_kwargs) for _, shooter in shooters)


    def flee(
        self,
        threat_pos: Sequence[float] | None = None,
        *,
        distance: float = 14.0,
        urgent: bool = True,
        allow_shaft_seal: bool = True,
        wait_for_ground: bool = False,
        initial_landing_checks: int = 3,
        other_shooters: Sequence[Sequence[float]] = (),
        occluded_shooters: Sequence[Sequence[float]] = (),
        other_melee: Sequence[Sequence[float]] = (),
        primary_melee: bool = False,
        other_creepers: Sequence[Sequence[float]] = (),
        avoid_blast: bool = False,
        keep_blast_clearance: bool = False,
        use_nearest_hostile: bool = True,
        away_only: bool = False,
        min_threat_distance: float | None = None,
        allow_second_leg: bool = True,
        expected_life: tuple[Any, Any] | None = None,
        terrain_deadline: float | None = None,
    ) -> dict[str, Any]:
        """Disengage from `threat_pos` along verified ground or seal a carved shaft."""
        def respawned_since(before: dict[str, Any], after: dict[str, Any]) -> bool:
            start, end = before.get("pos"), after.get("pos")
            return bool(
                after.get("alive") is False
                or (after.get("dimension") and before.get("dimension") and after["dimension"] != before["dimension"])
                or (after.get("last_death") is not None and after["last_death"] != before.get("last_death"))
                or (start and end and math.hypot(end[0] - start[0], end[2] - start[2]) > 64.0)
            )

        def settle_airborne(status: dict[str, Any], life: dict[str, Any], checks: int = 3) -> dict[str, Any]:
            # Each sample allows one physics interval, with a shared allowance for status reads.
            deadline = time.monotonic() + (checks * 100 + 150) / 1000
            sample = status
            for _ in range(checks):
                if (
                    sample.get("on_ground") is not False or sample.get("ready") is False
                    or sample.get("in_water") or sample.get("in_lava") or respawned_since(life, sample)
                ):
                    break
                if deadline - time.monotonic() <= 0.1:
                    break
                time.sleep(0.1)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    observed = self.bot.call("status", method="GET", timeout_s=remaining, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    break
                sample = {**observed, "on_ground": observed.get("on_ground") is True}
            return sample

        st0 = self.bot.status()
        if expected_life is not None and (st0.get("dimension"), st0.get("last_death")) != expected_life:
            return {"ok": False, "mode": "respawned", "respawned": True, "moved_dist": 0.0, "pos_after": st0.get("pos")}
        entered_water = False
        if (
            wait_for_ground and st0.get("pos") and st0.get("on_ground") is False
            and st0.get("alive") is not False and st0.get("ready") is not False
        ):
            before = st0
            st0 = settle_airborne(before, before, checks=initial_landing_checks)
            if respawned_since(before, st0):
                return {
                    "ok": False, "mode": "respawned", "respawned": True, "moved_dist": 0.0,
                    "pos_after": st0.get("pos") or before["pos"],
                }
            entered_water = bool(st0.get("in_water") and not before.get("in_water"))
        pos0 = st0.get("pos")
        if not pos0 or st0.get("alive") is False or st0.get("ready") is False:
            return {"ok": False, "mode": "flee", "reason": "not_ready", "moved_dist": 0.0, "pos_after": pos0}
        if st0.get("in_lava") or entered_water or st0.get("on_ground") is False:
            return {
                "ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0, "pos_after": pos0,
                **{key: st0.get(key) for key in ("on_ground", "in_water", "in_lava")},
            }
        if threat_pos is None and use_nearest_hostile:
            nh = st0.get("nearest_hostile")
            if isinstance(nh, dict) and isinstance(nh.get("pos"), list):
                threat_pos = nh["pos"]

        if allow_shaft_seal:
            snap = self.arena_snapshot(pos0)
            if snap["enclosure"] == "carved_shaft":
                seal = self.fortify(st0, lambda: self.build_tactical("full_seal", threat_pos=threat_pos))
                if seal.get("ok"):
                    pos_s = seal.get("pos_after") or pos0
                    return {"ok": True, "mode": "sealed", "moved_dist": 0.0, "pos_after": pos_s, "seal": seal}

        if threat_pos is not None and len(threat_pos) >= 3:
            away_dx = float(pos0[0]) - float(threat_pos[0])
            away_dz = float(pos0[2]) - float(threat_pos[2])
            pref_yaw = math.atan2(-away_dx, -away_dz) if math.hypot(away_dx, away_dz) > 0.05 else float(st0.get("yaw") or 0.0)
        else:
            pref_yaw = float(st0.get("yaw") or 0.0) + math.pi

        self.invalidate_block_cache()
        short_step = False
        disengage_range = min(float(distance), 8.5) if min_threat_distance is None else min_threat_distance
        angles = (0, 35, -35, 70, -70) if away_only else (0, 35, -35, 70, -70, 110, -110, 150, -150, 180)
        yaws = [pref_yaw + math.radians(angle) for angle in angles]
        covered_shooters = [(shooter, min(6.0, math.dist(pos0[:3], shooter[:3]))) for shooter in occluded_shooters]

        def avoids_uncovering_close_fire(point: Sequence[float], deadline: float) -> bool:
            try:
                return all(
                    math.dist(point[:3], shooter[:3]) >= clearance
                    or self._blocks_shot_across_hitbox(point, shooter, deadline=deadline)
                    for shooter, clearance in covered_shooters
                )
            except _TerrainProbeExpired:
                return False

        def probe_route(start: Sequence[float], yaw: float, length: float, **options: bool) -> tuple[int, bool] | None:
            if terrain_deadline is None:
                route = self.probe_retreat(start, yaw, length, **options)
            else:
                try:
                    route = self.probe_retreat(start, yaw, length, deadline=terrain_deadline, **options)
                except _TerrainProbeExpired:
                    return None
            if route is None or not covered_shooters:
                return route
            goal_y, _ = route
            ux, uz = yaw_to_unit_xz(yaw)
            steps = math.ceil(length)
            for step in range(1, steps + 1):
                travel = length * step / steps
                point = [start[0] + ux * travel, goal_y, start[2] + uz * travel]
                if not avoids_uncovering_close_fire(point, visibility_deadline):
                    return None
            return route

        require_blast_clearance = keep_blast_clearance or avoid_blast
        blast_offsets = [
            tuple(float(threat[axis]) - float(pos0[axis]) for axis in range(3))
            for threat in (threat_pos, *other_creepers)
        ] if (other_creepers or require_blast_clearance) and threat_pos is not None and len(threat_pos) >= 3 else []

        def blast_clearance(yaw: float, travel: float) -> tuple[float, float]:
            ux, uz = yaw_to_unit_xz(yaw)
            loss = 0.0
            endpoint = math.inf
            step = min(travel, 3.0)
            for dx, dy, dz in blast_offsets:
                nearest_step = max(0.0, min(travel, dx * ux + dz * uz))
                nearest = math.hypot(dx - ux * nearest_step, dy, dz - uz * nearest_step)
                loss = max(loss, min(6.0, math.hypot(dx, dy, dz)) - nearest)
                endpoint = min(endpoint, math.hypot(dx - ux * step, dy, dz - uz * step))
            return -loss, endpoint

        blast_route_length = min(float(distance), 9.5)
        if other_creepers and blast_offsets:
            yaws.extend((pref_yaw + math.pi / 2, pref_yaw - math.pi / 2))
            # The optional second leg can add six blocks in the same direction.
            yaws.sort(key=lambda yaw: blast_clearance(yaw, blast_route_length), reverse=True)
        elif (other_shooters or other_melee) and threat_pos is not None and len(threat_pos) >= 3:
            # A follow-up move keeps the selected heading. Account for both legs before probing terrain.
            yaws.extend((pref_yaw + math.pi / 2, pref_yaw - math.pi / 2))
            threat_offsets = [
                tuple(float(threat[axis]) - float(pos0[axis]) for axis in range(3))
                for threat in (threat_pos, *other_shooters, *other_melee)
            ]

            def threat_clearance(yaw: float) -> float:
                ux, uz = yaw_to_unit_xz(yaw)
                if require_blast_clearance:
                    dx, dy, dz = threat_offsets[0]
                    # Keep the original order unless the heading quickly clears the blast radius.
                    if dx * ux + dz * uz > 1e-6 or math.hypot(dx - ux * 3, dy, dz - uz * 3) < 5.5:
                        return -1.0
                nearest = math.inf
                for index, (dx, dy, dz) in enumerate(threat_offsets):
                    if require_blast_clearance and index == 0:
                        continue
                    # The primary melee attacker is already in reach. Compare clearance
                    # after the initial step away, and check the whole route when it approaches an attacker.
                    projection = dx * ux + dz * uz
                    stepping_away = index == 0 and (primary_melee or other_melee) and (not primary_melee or projection <= 0)
                    start = min(1.65, disengage_range) if stepping_away else 0.0
                    along_route = max(start, min(disengage_range, projection))
                    nearest = min(nearest, math.hypot(dx - ux * along_route, dy, dz - uz * along_route))
                return nearest

            yaws.sort(key=threat_clearance, reverse=True)
        visibility_deadline = time.monotonic() + 0.75
        for chosen_yaw in yaws:
            if require_blast_clearance and blast_clearance(chosen_yaw, blast_route_length)[0] < -1e-6:
                continue
            route = probe_route(pos0, chosen_yaw, 3.5)
            if route is not None:
                break
        else:
            if st0.get("on_ground") is True and not st0.get("in_water"):
                foot_y = math.floor(float(pos0[1]) + 0.05)
                short_yaws = [pref_yaw + math.pi / 2, pref_yaw - math.pi / 2]
                if blast_offsets:
                    short_yaws = yaws if other_melee and not other_creepers else sorted(
                        yaws, key=lambda yaw: blast_clearance(yaw, 1.65), reverse=True,
                    )
                elif primary_melee and other_shooters:
                    short_yaws = yaws
                elif wait_for_ground:
                    short_yaws.extend(yaws[:3])
                    if threat_pos is not None and not (other_shooters or other_melee):
                        short_yaws.extend(pref_yaw + math.radians(angle) for angle in (70, -70))
                for chosen_yaw in short_yaws:
                    if require_blast_clearance and blast_clearance(chosen_yaw, 1.65)[0] < -1e-6:
                        continue
                    route = probe_route(pos0, chosen_yaw, 1.65)
                    if route == (foot_y, False):
                        short_step = True
                        break
                if not short_step and wait_for_ground and str(st0.get("dimension") or "").removeprefix("minecraft:") == "overworld":
                    for chosen_yaw in short_yaws:
                        if require_blast_clearance and blast_clearance(chosen_yaw, 1.65)[0] < -1e-6:
                            continue
                        route = probe_route(pos0, chosen_yaw, 1.65, allow_step_down=True)
                        if route == (foot_y - 1, False):
                            short_step = True
                            break
                    if (
                        not short_step and threat_pos is not None and not require_blast_clearance
                        and not (other_melee or other_shooters)
                    ):
                        # A nearby step up can lead out of a depression even when no full retreat fits.
                        for angle in (0, 35, -35, 70, -70):
                            chosen_yaw = pref_yaw + math.radians(angle)
                            route = probe_route(pos0, chosen_yaw, 1.65, short_climb=True)
                            if route == (foot_y + 1, True):
                                short_step = True
                                break
            if not short_step:
                return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0, "pos_after": pos0}

        landing_y, needs_jump = route
        short_climb = short_step and needs_jump
        pulse_ms = 450 if urgent else 350
        if short_step:
            pulse_ms = 300 if short_climb else 220
        if expected_life is not None:
            current = self._safe_status(timeout_s=0.2)
            if respawned_since(st0, current):
                return {"ok": False, "mode": "respawned", "respawned": True, "moved_dist": 0.0, "pos_after": current.get("pos")}
            point = current.get("pos")
            if (
                (current.get("dimension"), current.get("last_death")) != expected_life
                or current.get("ready") is not True or current.get("on_ground") is not True
                or any(current.get(key) for key in ("in_lava", "in_water", "submerged"))
                or not point or math.dist(point[:3], pos0[:3]) > 0.3
            ):
                return {"ok": False, "mode": "flee", "reason": "retreat_position_changed", "moved_dist": 0.0}
        try:
            self.bot.call("look", {"yaw": chosen_yaw, "pitch": 0.0})
            self.bot.call(
                "control",
                {
                    "forward": True,
                    "sprint": not short_step,
                    "jump": needs_jump or bool(st0.get("in_water")),
                    "ms": pulse_ms,
                    "stop_on": ["death"],
                },
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop()

        def retreat_result(st: dict[str, Any]) -> dict[str, Any]:
            pos = st.get("pos") or pos0
            moved = math.hypot(float(pos[0]) - float(pos0[0]), float(pos[2]) - float(pos0[2]))
            respawned = respawned_since(st0, st)
            unsafe = (
                st.get("ready") is False or st.get("in_lava") or st.get("in_water") or st.get("on_ground") is False
            )
            if covered_shooters and not respawned and not unsafe:
                self.invalidate_block_cache()
                if not avoids_uncovering_close_fire(pos, time.monotonic() + 0.45):
                    return {
                        "ok": False, "mode": "flee", "pos_after": pos, "moved_dist": round(moved, 2),
                        "reason": "projectile_lane_unconfirmed",
                    }
            return {
                "ok": (
                    not respawned and not unsafe and moved >= (0.35 if short_step else 1.5)
                    and (not short_climb or math.floor(float(pos[1]) + 0.05) == landing_y)
                ),
                "mode": "respawned" if respawned else "flee",
                "moved_dist": 0.0 if respawned else round(moved, 2),
                "pos_after": pos,
                **({"respawned": True} if respawned else {}),
                **({
                    "reason": "unsafe_landing",
                    **{key: st.get(key) for key in ("on_ground", "in_water", "in_lava")},
                } if unsafe else {}),
            }

        st_mid = self._safe_status({**st0, "ready": False})
        if wait_for_ground:
            st_mid = settle_airborne(st_mid, st0)
        result = retreat_result(st_mid)
        if (
            not allow_second_leg or short_step or result.get("respawned")
            or result.get("reason") in {"unsafe_landing", "projectile_lane_unconfirmed"}
        ):
            return result

        mid_pos = result["pos_after"]
        threat_dist_mid = 0.0
        if threat_pos is not None and len(threat_pos) >= 3:
            threat_dist_mid = math.hypot(float(mid_pos[0]) - float(threat_pos[0]), float(mid_pos[2]) - float(threat_pos[2]))

        ux, uz = yaw_to_unit_xz(chosen_yaw)
        if (
            threat_pos is not None
            and threat_dist_mid >= disengage_range
            and result["ok"]
        ):
            return result

        leg_limit = 6.0 if min_threat_distance is None else 9.0
        remaining = min(leg_limit, float(distance) - result["moved_dist"])
        if remaining < 1.5:
            return result
        self.invalidate_block_cache()
        visibility_deadline = time.monotonic() + 0.75
        for leg in dict.fromkeys((remaining, min(remaining, 6.0), min(remaining, 3.5))):
            route = probe_route(mid_pos, chosen_yaw, leg)
            if route is not None:
                remaining = leg
                break
        else:
            return result
        goal_y, _ = route
        try:
            self.bot.call(
                "move_to",
                {
                    "x": float(mid_pos[0]) + ux * remaining,
                    "y": goal_y,
                    "z": float(mid_pos[2]) + uz * remaining,
                    "range": 1.0,
                    "timeout": 2500,
                    "stop_on": ["death"],
                },
            )
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, httpx.HTTPError):
                self._best_effort_stop()
        st_final = self._safe_status({**st_mid, "ready": False})
        if wait_for_ground:
            st_final = settle_airborne(st_final, st0)
        return retreat_result(st_final)


    def step_away_from_melee(
        self, status: dict[str, Any], threat_pos: Sequence[float], *, deadline: float, target_id: int | None = None,
    ) -> dict[str, Any]:
        """Take one short, supported step away from a still-close attacker."""
        outcome: dict[str, Any] = {"ok": False}
        if target_id is not None:
            outcome["entity_id"] = target_id
        origin = status["pos"]
        foot_y = math.floor(float(origin[1]) + 0.05)
        planned_from = origin
        probe_deadline = deadline - 0.85
        try:
            for _ in range(2):
                if time.monotonic() >= probe_deadline:
                    return outcome
                away, _ = aim_yaw_pitch(threat_pos, planned_from)
                self.invalidate_block_cache()
                yaw = None
                for offset in (0, 45, -45):
                    heading = away + math.radians(offset)
                    if self.probe_retreat(planned_from, heading, 1.65, deadline=probe_deadline) == (foot_y, False):
                        yaw = heading
                        break
                current = self.bot.call("status", method="GET", timeout_s=0.2, allow_reauth=False)
                pos = current.get("pos")
                if (
                    not pos or math.dist(pos[:3], origin[:3]) > 0.65 or math.floor(float(pos[1]) + 0.05) != foot_y
                    or current.get("on_ground") is not True or current.get("in_water") or current.get("in_lava")
                    or current.get("ready") is False or current.get("alive") is False
                    or current.get("last_death") != status.get("last_death") or current.get("dimension") != status.get("dimension")
                    or time.monotonic() + 0.65 >= deadline
                ):
                    return outcome
                if math.dist(pos[:3], planned_from[:3]) <= 0.2:
                    if yaw is None:
                        return outcome
                    break
                # The first retreat can still carry the bot a short distance. Recheck the new footing once.
                planned_from = pos
            else:
                return outcome
            self.bot.call("look", {"yaw": yaw, "pitch": 0.0}, timeout_s=0.15, allow_reauth=False)
            result = self.bot.call(
                "control", {"forward": True, "ms": 220, "stop_on": ["death"]},
                timeout_s=0.3, allow_reauth=False,
            )
            remaining = deadline - time.monotonic()
            if not result.get("ok", True) or remaining <= 0:
                return outcome
            landed = self.bot.call("status", method="GET", timeout_s=min(0.2, remaining), allow_reauth=False)
        except (ApiError, httpx.HTTPError, _TerrainProbeExpired):
            return outcome
        end = landed.get("pos")
        if (
            not end or landed.get("on_ground") is not True or landed.get("ready") is False
            or landed.get("alive") is False or landed.get("in_water") or landed.get("in_lava")
            or landed.get("last_death") != status.get("last_death") or landed.get("dimension") != status.get("dimension")
            or math.floor(float(end[1]) + 0.05) != foot_y
        ):
            return outcome
        moved = math.hypot(end[0] - pos[0], end[2] - pos[2])
        outcome.update(pos_after=end, moved_dist=round(moved, 2))
        outcome["ok"] = (
            moved >= 0.25
            and math.hypot(end[0] - threat_pos[0], end[2] - threat_pos[2])
            > math.hypot(pos[0] - threat_pos[0], pos[2] - threat_pos[2])
        )
        for key, snapshot in (("target_distance_before", current), ("target_distance_after", landed)):
            hostile = snapshot.get("nearest_hostile")
            if target_id is not None and isinstance(hostile, dict) and hostile.get("id") == target_id:
                distance = hostile.get("dist")
                if isinstance(distance, (int, float)) and math.isfinite(distance) and distance >= 0.0:
                    outcome[key] = distance
        before = outcome.get("target_distance_before")
        after = outcome.get("target_distance_after")
        if after is not None:
            outcome["target_in_melee_range"] = after <= 3.5
            if outcome["ok"] and before is not None and after <= before:
                outcome.update(ok=False, reason="melee_clearance_unconfirmed")
        return outcome


    def _point_allowed(self, x: float, z: float, *, action: str = "combat_env") -> bool:
        """Return True unless an enabled VoidGeofence rejects `(x, z)` via `check_point`."""
        if self.geofence is None or not hasattr(self.geofence, "check_point"):
            return True
        return self.geofence.check_point(float(x), float(z), action=action) is None


    @staticmethod
    def _crosses_near_water_threat(pos: Sequence[float], x: float, z: float, threat: Sequence[float]) -> bool:
        dx, dz = x - pos[0], z - pos[2]
        length_sq = dx * dx + dz * dz
        if length_sq == 0:
            return False
        tx, ty, tz = threat[0] - pos[0], threat[1] - pos[1], threat[2] - pos[2]
        fraction = max(0.0, min(1.0, (tx * dx + tz * dz) / length_sq))
        closest = math.hypot(tx - fraction * dx, ty, tz - fraction * dz)
        return closest < min(4.5, math.hypot(tx, ty, tz)) - 1e-6

