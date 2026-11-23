"""CombatEnv mixin: water_shore."""

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


class _WaterShoreMixin:
    def _find_walkable_shore_target(
        self,
        pos0: Sequence[float],
        *,
        block_types: Sequence[str],
        max_radius: int = 24,
        min_y: int = -64,
        max_y: int | None = None,
        threat_pos: Sequence[float] | None = None,
        max_candidates: int = 10,
        avoid_close_threat: bool = False,
        deadline: float | None = None,
        excluded_shores: Collection[tuple[float, ...]] = (),
        prefer_shallow: bool = False,
    ) -> list[float] | None:
        """Query `find_blocks` for nearby solid shoreline with 2-high passable headroom."""
        start_x, start_y, start_z = map(float, pos0[:3])
        has_threat = isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3
        tx_th = float(threat_pos[0]) if has_threat else 0.0
        tz_th = float(threat_pos[2]) if has_threat else 0.0
        threat_dist0 = math.hypot(start_x - tx_th, start_z - tz_th) if has_threat else 0.0

        def shore_rank(x: float, y: float, z: float) -> tuple[bool, float]:
            distance = math.hypot(x - start_x, z - start_z)
            shallow_nearby = prefer_shallow and distance <= 3.5 and abs(y - start_y) <= 1.25
            away_penalty = 1.5 if has_threat and math.hypot(x - tx_th, z - tz_th) < threat_dist0 else 0.0
            return not shallow_nearby, distance + away_penalty

        def _pick_from_query(
            q_types: Sequence[str], *, q_limit: int, shallow_only: bool = False,
            probe_deadline: float | None = deadline,
        ) -> list[float] | None:
            transport = {}
            if probe_deadline is not None:
                remaining = probe_deadline - time.monotonic()
                if remaining <= 0:
                    return None
                transport = {"timeout_s": remaining, "allow_reauth": False}
            try:
                found = self.bot.call(
                    "find_blocks",
                    {
                        "radius": max(8, min(28, int(max_radius))),
                        "types": list(q_types),
                        "limit": q_limit,
                    },
                    **transport,
                )
            except Exception:
                found = None

            candidates: list[tuple[tuple[bool, float], int, int, int]] = []
            for row in (found or {}).get("blocks") or []:
                if not isinstance(row, dict):
                    continue
                rpos = row.get("pos")
                if not isinstance(rpos, (list, tuple)) or len(rpos) < 3:
                    continue
                cx, cy, cz = int(rpos[0]), int(rpos[1]), int(rpos[2])
                if cy < min_y or (max_y is not None and cy > max_y):
                    continue
                if (cx + 0.5, float(cy + 1), cz + 0.5) in excluded_shores:
                    continue
                if not _is_solid_ground(_clean_block_name(row.get("name"))):
                    continue
                if not self._point_allowed(cx + 0.5, cz + 0.5, action="lava_shore"):
                    continue
                if avoid_close_threat and has_threat and self._crosses_near_water_threat(pos0, cx + 0.5, cz + 0.5, threat_pos):
                    continue
                rank = shore_rank(cx + 0.5, cy + 1, cz + 0.5)
                if not shallow_only or not rank[0]:
                    candidates.append((rank, cx, cy, cz))

            candidates.sort(key=lambda item: item[0])
            for _, cx, cy, cz in candidates[:max(1, int(max_candidates))]:
                probe_kwargs = {"deadline": probe_deadline} if probe_deadline is not None else {}
                foot_b = self._get_block_name(cx, cy + 1, cz, **probe_kwargs)
                head_b = self._get_block_name(cx, cy + 2, cz, **probe_kwargs)
                if _is_passable(foot_b) and _is_passable(head_b):
                    return [cx + 0.5, float(cy + 1), cz + 0.5]
            return None

        surface = None
        if tuple(block_types) == SHORE_LAND_BLOCKS:
            surface = _pick_from_query(SURFACE_SHORE_PRIORITY_BLOCKS, q_limit=48)
            if surface is not None and (not prefer_shallow or not shore_rank(*surface)[0]):
                return surface
        if surface is None:
            return _pick_from_query(block_types, q_limit=120)
        # A broader scan can find a low bank made of stone or another non-priority block.
        low_bank_deadline = min(time.monotonic() + 0.65, deadline if deadline is not None else math.inf)
        try:
            low_bank = _pick_from_query(block_types, q_limit=120, shallow_only=True, probe_deadline=low_bank_deadline)
        except _TerrainProbeExpired:
            return surface
        return low_bank or surface


    def remember_dry_bank(self, status: dict[str, Any]) -> None:
        pos = status.get("pos")
        if (
            pos and status.get("on_ground") is True and status.get("ready") is True and status.get("alive") is True
            and not status.get("in_water") and not status.get("submerged") and not status.get("in_lava")
        ):
            x, y, z = map(math.floor, pos[:3])
            bank = (x + 0.5, float(y), z + 0.5)
            life = (status.get("dimension"), status.get("last_death"))
            self._last_dry_bank = (bank, life, time.monotonic())
            stalled = self._stalled_water_shores.get(bank)
            if stalled is not None and stalled[1] == life:
                del self._stalled_water_shores[bank]


    def _dry_bank_is_walkable(self, bank: Sequence[float]) -> bool:
        self.invalidate_block_cache()
        bx, by, bz = map(math.floor, bank)
        deadline = time.monotonic() + 0.6
        try:
            floor, feet, head = (
                self._get_block_name(bx, by + dy, bz, strict=True, deadline=deadline) for dy in (-1, 0, 1)
            )
        except _TerrainProbeExpired:
            return False
        return (
            floor not in HAZARD_BLOCK_TYPES and _is_solid_ground(floor)
            and _is_passable(feet) and _is_passable(head)
        )


    def _water_pocket_column_is_clear(self, x: int, z: int, foot_y: int, air_y: int, *, deadline: float) -> bool:
        return all(
            self._get_block_name(x, y, z, strict=True, deadline=deadline) in BREATHABLE_AIR_BLOCKS
            for y in (air_y, air_y + 1)
        ) and all(
            self._get_block_name(x, y, z, strict=True, deadline=deadline) in WATER_BLOCKS | BREATHABLE_AIR_BLOCKS
            for y in range(foot_y, air_y)
        )


    def _nearby_water_air_pocket(
        self, pos: Sequence[float], *, max_steps: int, max_rise: int = 0, threat_pos: Sequence[float] | None = None,
    ) -> tuple[list[float], int | None] | None:
        x, y, z = pos[:3]
        bx, by, bz = math.floor(x), math.floor(y), math.floor(z)
        swim_clearance = WATER_BLOCKS | BREATHABLE_AIR_BLOCKS
        overhead_hazards = set(HAZARD_BLOCK_TYPES) - WATER_BLOCKS
        deadline = time.monotonic() + 0.5

        def clear_swim_step(px: float, pz: float) -> bool:
            return self._point_allowed(px, pz, action="water_retreat") and all(
                self._get_block_name(hx, by, hz, deadline=deadline) in swim_clearance
                and self._get_block_name(hx, by + 1, hz, deadline=deadline) in swim_clearance
                and self._get_block_name(hx, by + 2, hz, deadline=deadline) not in overhead_hazards
                for hx, hz in _hitbox_columns(px, pz)
            )

        self.invalidate_block_cache()
        try:
            if self._get_block_name(bx, by + 1, bz, deadline=deadline) not in WATER_BLOCKS:
                return None
            columns = [(bx + dx * distance, bz + dz * distance)
                       for distance in range(1, max_steps + 1) for dx, dz in CARDINAL_VECTORS.values()]
            for air_y in range(by + 2, by + 3 + max_rise):
                for target_x, target_z in columns:
                    tx, tz = target_x + 0.5, target_z + 0.5
                    if threat_pos is not None and self._crosses_near_water_threat(pos, tx, tz, threat_pos):
                        continue
                    if not self._water_pocket_column_is_clear(target_x, target_z, by, air_y, deadline=deadline):
                        continue
                    slices = max(4, math.ceil(math.hypot(tx - x, tz - z) / 0.4))
                    if all(
                        clear_swim_step(x + (tx - x) * step / slices, z + (tz - z) * step / slices)
                        for step in range(1, slices + 1)
                    ):
                        return [tx, y, tz], air_y if air_y > by + 2 else None
        except _TerrainProbeExpired:
            pass
        return None


    def _rise_in_water_pocket(
        self, status: dict[str, Any], target: Sequence[float], air_y: int,
    ) -> tuple[dict[str, Any], bool] | None:
        point = status.get("pos")
        if not (
            point and status.get("ready") is True and status.get("in_water") and status.get("submerged") is True
            and math.floor(point[1]) < air_y
        ):
            return None
        x, z = math.floor(target[0]), math.floor(target[2])
        if _hitbox_columns(point[0], point[2]) != [(x, z)]:
            return None
        self.invalidate_block_cache()
        try:
            if not self._water_pocket_column_is_clear(x, z, math.floor(point[1]), air_y, deadline=time.monotonic() + 0.35):
                return None
        except _TerrainProbeExpired:
            return None
        current = self._safe_status({**status, "ready": False}, timeout_s=0.25)
        current_pos = current.get("pos")
        if (
            current.get("ready") is not True or current.get("alive") is False
            or any(current.get(key) != status.get(key) for key in ("dimension", "last_death"))
            or not current.get("in_water") or current.get("submerged") is not True or current.get("busy")
            or not current_pos or math.floor(current_pos[1]) < math.floor(point[1])
            or _hitbox_columns(current_pos[0], current_pos[2]) != [(x, z)]
        ):
            return current, False
        try:
            self.bot.call("control", {"jump": True, "ms": 750, "stop_on": ["death"]}, timeout_s=1.0, allow_reauth=False)
        except httpx.HTTPError:
            self._best_effort_stop(timeout_s=0.3)
        except ApiError:
            pass
        return self._safe_status({**current, "ready": False}, timeout_s=0.3), True


    def escape_deep_water_to_shore(
        self,
        threat_pos: Sequence[float] | None = None,
        *,
        max_radius: int = 24,
        keep_shield: bool = False,
        shield_target_id: int | None = None,
        surface_first: bool = False,
        avoid_close_threat: bool = False,
        sneak_on_magma: bool = False,
        prefer_recent_dry_bank: bool = False,
        under_attack: bool = False,
        recently_used_banks: Sequence[tuple[float, float, float]] = (),
    ) -> dict[str, Any]:
        """Swim up and move toward the nearest walkable dry shoreline block (or retreat at surface)."""
        st0 = self.bot.status()
        previous_bank = self._nearby_water_bank
        self._nearby_water_bank = None
        if st0.get("alive") is False or st0.get("ready") is False:
            return {
                "ok": False,
                "in_water": bool(st0.get("in_water", True)),
                "mode": "not_ready",
                "pos_after": st0.get("pos"),
            }
        if st0.get("in_lava"):
            return {
                "ok": False, "in_water": bool(st0.get("in_water")), "in_lava": True,
                "mode": "entered_lava", "pos_after": st0.get("pos"),
            }
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        start_x, start_z = float(pos0[0]), float(pos0[2])
        actions: list[str] = []
        shore_target: list[float] | None = None
        has_threat = isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3

        def interrupted_water_escape(st: dict[str, Any]) -> dict[str, Any] | None:
            pos = st.get("pos") or pos0
            respawned = (
                math.hypot(float(pos[0]) - start_x, float(pos[2]) - start_z) > 64.0
                or (st.get("last_death") is not None and st["last_death"] != st0.get("last_death"))
                or (st.get("dimension") and st0.get("dimension") and st["dimension"] != st0["dimension"])
            )
            if respawned:
                mode = "respawned"
            elif st.get("alive") is False or st.get("ready") is False:
                mode = "not_ready"
            elif st.get("in_lava"):
                mode = "entered_lava"
            else:
                return None
            return {
                "ok": False,
                "in_water": bool(st.get("in_water", True)),
                "mode": mode,
                **({"in_lava": True} if mode == "entered_lava" else {}),
                "shore_target": shore_target,
                "actions": actions,
                "pos_after": pos,
            }

        def settle_landing(st: dict[str, Any], bank: Sequence[float] | None = None) -> dict[str, Any]:
            deadline = time.monotonic() + 0.45
            for _ in range(3):
                pos = st.get("pos")
                if (
                    st.get("in_water") or st.get("on_ground") is not False or not pos
                    or deadline - time.monotonic() <= 0.1
                    or (bank is not None and (
                        math.hypot(pos[0] - bank[0], pos[2] - bank[2]) > 0.6 or not -0.6 <= pos[1] - bank[1] <= 1.25
                    ))
                ):
                    break
                time.sleep(0.1)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    sample = self.bot.call("status", method="GET", timeout_s=min(0.25, remaining), allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    break
                if not isinstance(sample, dict):
                    break
                if interrupted_water_escape(sample) is not None:
                    return sample
                if not sample.get("pos"):
                    break
                st = sample
            return st

        if not st0.get("in_water"):
            refreshed = settle_landing(st0)
            interruption = interrupted_water_escape(refreshed)
            if interruption is not None:
                return interruption
            if not refreshed.get("in_water"):
                landed = refreshed.get("on_ground") is not False
                if landed:
                    self.remember_dry_bank(refreshed)
                return {
                    "ok": landed,
                    "in_water": False,
                    "mode": "already_dry" if landed else "shore_landing_unconfirmed",
                    "pos_after": refreshed.get("pos"),
                }
            st0 = refreshed
            pos0 = st0["pos"]
            start_x, start_z = float(pos0[0]), float(pos0[2])
        search_status = st0

        def swim_step_allowed(
            pos: Sequence[float], yaw: float, distance: float = 5.0,
            *, inward_target: tuple[float, float] | None = None,
        ) -> bool:
            ux, uz = yaw_to_unit_xz(yaw)
            x, z = float(pos[0]) + distance * ux, float(pos[2]) + distance * uz
            can_step_inward = (
                inward_target is not None
                and not self._point_allowed(pos[0], pos[2], action="water_retreat")
                and math.hypot(inward_target[0] - pos[0], inward_target[1] - pos[2]) >= distance
                and math.hypot(inward_target[0] - x, inward_target[1] - z)
                < math.hypot(inward_target[0] - pos[0], inward_target[1] - pos[2])
            )
            return (can_step_inward or self._point_allowed(x, z, action="water_retreat")) and not (
                avoid_close_threat and has_threat and self._crosses_near_water_threat(pos, x, z, threat_pos)
            )

        def shore_bank_in_reach(
            pos: Sequence[float], bank: Sequence[float], *, max_dist: float = 3.5, max_height: float = 1.25,
        ) -> bool:
            return (
                math.hypot(bank[0] - pos[0], bank[2] - pos[2]) <= max_dist
                and abs(bank[1] - pos[1]) <= max_height
                and self._point_allowed(bank[0], bank[2], action="water_shore")
                and not (
                    avoid_close_threat and has_threat
                    and self._crosses_near_water_threat(pos, bank[0], bank[2], threat_pos)
                )
            )

        life = (st0.get("dimension"), st0.get("last_death"))
        retry_bank = None
        if previous_bank is not None:
            bank, bank_life, bank_at = previous_bank
            if (
                life == bank_life and time.monotonic() - bank_at < 12.0
                and shore_bank_in_reach(pos0, bank) and self._dry_bank_is_walkable(bank)
            ):
                retry_bank = list(bank)

        # Hold buoyancy + shield while locating shoreline or breaking surface.
        try:
            self.bot.call(
                "posture",
                {"shield": bool(keep_shield), "sneak": sneak_on_magma, "jump": True},
            )
        except ApiError:
            pass

        if surface_first or _water_requires_surface(st0, oxygen_bubbles=260 / 15):
            swim_forward = (
                not (surface_first or has_threat or keep_shield)
                and retry_bank is None
                and swim_step_allowed(pos0, float(st0.get("yaw") or 0.0))
            )
            for _ in range(2):
                surge_origin = search_status.get("pos") or pos0
                try:
                    self.bot.call(
                        "control",
                        {"forward": swim_forward, "jump": True, "sprint": swim_forward, "ms": 750, "stop_on": ["death"]},
                    )
                    actions.append("water_surface_surge")
                except ApiError:
                    pass
                search_status = self.bot.status()
                interruption = interrupted_water_escape(search_status)
                if interruption is not None:
                    return interruption
                oxygen = search_status.get("oxygen")
                raised_pos = search_status.get("pos")
                # Continue a successful ascent while the bot has no air.
                if not (
                    surface_first and search_status.get("in_water") and search_status.get("submerged")
                    and isinstance(oxygen, (int, float)) and oxygen <= 0
                    and raised_pos and raised_pos[1] - pos0[1] >= 0.5
                ):
                    break
            below_surface = bool(raised_pos and raised_pos[1] < 55)
            if (
                surface_first and search_status.get("in_water") and search_status.get("submerged") is True and raised_pos
                and (below_surface or (isinstance(oxygen, (int, float)) and oxygen <= 0))
                and raised_pos[1] - surge_origin[1] < 0.5
            ):
                escape_pocket = self._nearby_water_air_pocket(
                    raised_pos, max_steps=2 if below_surface else 1,
                    max_rise=2 if below_surface and water_oxygen_below(search_status, 5) else 0,
                    threat_pos=threat_pos if avoid_close_threat else None,
                )
                if escape_pocket is not None:
                    pocket, air_y = escape_pocket
                    try:
                        current = self.bot.call("status", method="GET", timeout_s=0.25, allow_reauth=False)
                    except (ApiError, httpx.HTTPError):
                        current = search_status
                        pocket = None
                    interruption = interrupted_water_escape(current)
                    if interruption is not None:
                        return interruption
                    search_status = current
                    current_pos = current.get("pos")
                    current_oxygen = current.get("oxygen")
                    if (
                        pocket is not None and current.get("in_water") and current.get("submerged") is True
                        and (below_surface or (isinstance(current_oxygen, (int, float)) and current_oxygen <= 0))
                        and current_pos and math.dist(current_pos[:3], raised_pos[:3]) <= 0.25
                    ):
                        yaw, _ = aim_yaw_pitch(current_pos, pocket)
                        swim_ms = 420 if math.dist(current_pos[:3], pocket) <= 1.65 else 800
                        try:
                            self.bot.call("look", {"yaw": yaw, "pitch": -15.0}, timeout_s=0.2, allow_reauth=False)
                            self.bot.call(
                                "control", {"forward": True, "jump": True, "ms": swim_ms, "stop_on": ["death"]},
                                timeout_s=swim_ms / 1000 + 0.23, allow_reauth=False,
                            )
                            actions.append("water_air_pocket")
                        except httpx.HTTPError:
                            self._best_effort_stop(timeout_s=0.3)
                        except ApiError:
                            pass
                        try:
                            search_status = self.bot.call("status", method="GET", timeout_s=0.3, allow_reauth=False)
                        except (ApiError, httpx.HTTPError):
                            search_status = {**current, "ready": False}
                        interruption = interrupted_water_escape(search_status)
                        if interruption is not None:
                            return interruption
                        if air_y is not None:
                            ascended = self._rise_in_water_pocket(search_status, pocket, air_y)
                            if ascended is not None:
                                search_status, tried_ascent = ascended
                                if tried_ascent:
                                    actions.append("water_pocket_ascent")
                                interruption = interrupted_water_escape(search_status)
                                if interruption is not None:
                                    return interruption
                    oxygen = search_status.get("oxygen")
                    if (
                        search_status.get("in_water") and search_status.get("submerged") is False
                        and isinstance(oxygen, (int, float)) and oxygen > 0
                    ):
                        return {
                            "ok": False, "in_water": True, "mode": "breathing", "actions": actions,
                            "pos_after": search_status.get("pos"),
                        }
            if surface_first and not search_status.get("in_water") and search_status.get("on_ground") is not False:
                if sneak_on_magma:
                    try:
                        self.bot.call("posture", {"shield": bool(keep_shield), "sneak": False, "jump": False})
                    except (ApiError, httpx.HTTPError):
                        pass
                return {
                    "ok": True, "in_water": False, "mode": "surfaced",
                    "actions": actions, "pos_after": search_status.get("pos"),
                }

        # The surge can change both the shoreline height and the bot's horizontal position.
        search_pos = search_status.get("pos") or pos0
        shore_x, shore_y, shore_z = map(float, search_pos[:3])
        low_air = (
            surface_first and search_status.get("in_water") and search_status.get("submerged") is True
            and water_oxygen_below(search_status, 5)
        )
        urgent_escape = under_attack or low_air
        by = math.floor(shore_y + 0.05)
        now = time.monotonic()
        self._stalled_water_shores = {
            bank: (rim, bank_life, at) for bank, (rim, bank_life, at) in self._stalled_water_shores.items()
            if bank_life == life and now - at < 30.0
        }

        def stalled_from_here(bank: Sequence[float], rim: Sequence[float]) -> bool:
            high_lip = bank[1] - rim[1] >= 1.75
            height = bank[1] - shore_y
            if high_lip and height <= 1.25:
                return False
            if math.dist(search_pos[:3], rim) <= 2.5:
                return True
            # A swim that drifts along the same side does not make a high lip easier to climb.
            same_side = (shore_x - bank[0]) * (rim[0] - bank[0]) + (shore_z - bank[2]) * (rim[2] - bank[2]) > 0
            return high_lip and height >= 1.75 and same_side and math.hypot(shore_x - rim[0], shore_z - rim[2]) <= 8.0

        stalled_shores = {
            bank for bank, (rim, _, _) in self._stalled_water_shores.items() if stalled_from_here(bank, rim)
        }
        if stalled_shores:
            actions.append("shore_lip_stalled")
        excluded_shores = stalled_shores.union(recently_used_banks)

        def return_to_dry_bank(*, max_dist: float, max_height: float) -> list[float] | None:
            if self._last_dry_bank is None:
                return None
            bank, bank_life, bank_at = self._last_dry_bank
            if (
                life == bank_life and time.monotonic() - bank_at < 60.0 and bank not in excluded_shores
                and shore_bank_in_reach(search_pos, bank, max_dist=max_dist, max_height=max_height)
                and self._dry_bank_is_walkable(bank)
            ):
                self._last_dry_bank = None
                actions.append("return_to_dry_bank")
                return list(bank)
            return None

        retrying_bank = (
            retry_bank is not None and tuple(retry_bank) not in excluded_shores and shore_bank_in_reach(search_pos, retry_bank)
        )
        if retrying_bank:
            shore_target = retry_bank
            actions.append("shore_bank_retry")
        elif prefer_recent_dry_bank:
            shore_target = return_to_dry_bank(max_dist=3.5, max_height=1.25)
        if shore_target is None:
            shore_target = self._find_walkable_shore_target(
                search_pos,
                block_types=SHORE_LAND_BLOCKS,
                max_radius=max_radius,
                min_y=by - 1,
                max_y=by + 2,
                threat_pos=threat_pos,
                avoid_close_threat=avoid_close_threat,
                excluded_shores=excluded_shores,
                prefer_shallow=not search_status.get("submerged") and (not has_threat or avoid_close_threat),
            )

        # 2. Radial fallback if find_blocks yielded no dry surface spot.
        if shore_target is None:
            base_yaw = (
                aim_yaw_pitch(threat_pos, search_pos)[0]
                if has_threat
                else float(search_status.get("yaw") or 0.0)
            )
            y_probe = by
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                cyaw = base_yaw + math.radians(deg)
                ux, uz = yaw_to_unit_xz(cyaw)
                for dist in (4, 8, 14):
                    px = math.floor(shore_x + ux * dist)
                    pz = math.floor(shore_z + uz * dist)
                    candidate = (px + 0.5, float(y_probe + 1), pz + 0.5)
                    if candidate in excluded_shores or not self._point_allowed(px + 0.5, pz + 0.5, action="water_shore"):
                        continue
                    if avoid_close_threat and has_threat and self._crosses_near_water_threat(search_pos, px + 0.5, pz + 0.5, threat_pos):
                        continue
                    g_name = self._get_block_name(px, y_probe, pz, strict=True)
                    if g_name and g_name not in HAZARD_BLOCK_TYPES and _is_solid_ground(g_name):
                        foot_p = self._get_block_name(px, y_probe + 1, pz, strict=True)
                        head_p = self._get_block_name(px, y_probe + 2, pz, strict=True)
                        if _is_passable(foot_p) and _is_passable(head_p):
                            shore_target = list(candidate)
                            break
                if shore_target is not None:
                    break

        if shore_target is None:
            shore_target = return_to_dry_bank(max_dist=36.0, max_height=2.0)

        if shore_target is None:
            # Preserve the confirmed way out when the search cannot find another bank.
            for bank in reversed(recently_used_banks):
                if bank not in stalled_shores and shore_bank_in_reach(search_pos, bank) and self._dry_bank_is_walkable(bank):
                    shore_target = list(bank)
                    break

        # 3. Move to shore target (plus shoreline lip jump) or swim up and away from threat.
        shore_path_unreachable = False
        if shore_target is not None:
            shore_dist = math.hypot(shore_target[0] - shore_x, shore_target[2] - shore_z)
            limit_pathfinder_wait = urgent_escape or ((has_threat or shield_target_id is not None) and shore_dist <= 5.0)

            def hop_to_shore(position: Sequence[float], *, ms: int, sprint: bool) -> None:
                try:
                    syaw, _ = aim_yaw_pitch(position, shore_target)
                    self.bot.call("look", {"yaw": syaw, "pitch": -10.0})
                    self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "sprint": sprint, "ms": ms, "stop_on": ["death"]},
                    )
                    actions.append("shore_lip_jump")
                except httpx.HTTPError:
                    self._best_effort_stop()
                except ApiError:
                    pass

            # Use the short hop before pathfinding when the bank is already within reach.
            hop_first = (
                (under_attack or not limit_pathfinder_wait) and not (retrying_bank or sneak_on_magma)
                and search_status.get("in_water") and not search_status.get("submerged")
                and 0.6 < shore_dist <= 1.8 and 0 <= shore_target[1] - shore_y <= 1.25
                and shore_bank_in_reach(search_pos, shore_target, max_dist=1.8)
                and self._dry_bank_is_walkable(shore_target)
            )
            st_mid = search_status
            if hop_first:
                hop_to_shore(search_pos, ms=450, sprint=True)
                st_mid = self._safe_status({**search_status, "ready": False})
                interruption = interrupted_water_escape(st_mid)
                if interruption is not None:
                    return interruption

            if not hop_first or st_mid.get("in_water"):
                try:
                    syaw, _ = aim_yaw_pitch(st_mid.get("pos") or search_pos, shore_target)
                    self.bot.call("look", {"yaw": syaw, "pitch": -10.0})
                except (ApiError, httpx.HTTPError):
                    pass
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": shore_target[0],
                            "y": shore_target[1],
                            "z": shore_target[2],
                            "range": 0.6,
                            "timeout": 1800 if limit_pathfinder_wait else 4200,
                            "stop_on": ["death"],
                        },
                        **({"timeout_s": 2.4, "allow_reauth": False} if limit_pathfinder_wait else {}),
                    )
                    actions.append("move_to_shore")
                except (ApiError, httpx.HTTPError) as err:
                    actions.append("move_to_shore_failed")
                    shore_path_unreachable = isinstance(err, ApiError) and err.code in {"path_not_found", "no_path"}
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.5 if limit_pathfinder_wait else None)
                st_mid = self._safe_status(st_mid)

            interruption = interrupted_water_escape(st_mid)
            if interruption is not None:
                return interruption
            mid_pos = st_mid.get("pos") or pos0
            if (
                bool(st_mid.get("in_water"))
                and st_mid.get("alive") is not False
                and st_mid.get("ready", True)
                and (not hop_first or math.dist(search_pos[:3], mid_pos[:3]) > 0.5)
            ):
                lip_height = float(shore_target[1]) - float(mid_pos[1])
                lip_distance = math.hypot(shore_target[0] - mid_pos[0], shore_target[2] - mid_pos[2])
                if lip_distance <= 0.3 and lip_height >= 0.75:
                    # The path ended below the bank, with no useful heading for a forward jump.
                    actions.append("shore_lip_directly_below")
                else:
                    shallow_lip = 0.6 < lip_distance <= 1.8 and 0.0 <= lip_height <= 1.25
                    # A failed short hop can leave the bot pressed against the same low bank.
                    sustained_climb = (
                        shallow_lip and retrying_bank and not (has_threat or shield_target_id is not None or sneak_on_magma)
                    )
                    if shallow_lip and not sustained_climb:
                        lip_ms = 450
                    else:
                        lip_ms = 1150 if lip_height > 0.05 else 850
                    hop_to_shore(mid_pos, ms=lip_ms, sprint=not sustained_climb)
        else:
            st_mid = self._safe_status(st0)
            interruption = interrupted_water_escape(st_mid)
            if interruption is not None:
                return interruption
            swim_pos = st_mid.get("pos") or pos0
            pref_yaw = aim_yaw_pitch(threat_pos, swim_pos)[0] if has_threat else float(st0.get("yaw") or 0.0)
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                away_yaw = pref_yaw + math.radians(deg)
                if swim_step_allowed(swim_pos, away_yaw):
                    break
            else:
                away_yaw = None

            inward_target = None
            clamp = getattr(self.geofence, "clamped_move_target", None)
            if away_yaw is None and callable(clamp):
                candidate = clamp(swim_pos[0], swim_pos[2])
                if candidate and self._point_allowed(*candidate, action="water_retreat") and not (
                    avoid_close_threat and has_threat
                    and self._crosses_near_water_threat(swim_pos, *candidate, threat_pos)
                ):
                    inward_target = candidate
                    away_yaw = aim_yaw_pitch(swim_pos, [candidate[0], swim_pos[1], candidate[1]])[0]
                    actions.append("surface_retreat_inward")

            if away_yaw is None:
                actions.append("surface_retreat_geofence_blocked")
            elif st_mid.get("in_water"):
                ux, uz = yaw_to_unit_xz(away_yaw)
                px, py, pz = map(float, swim_pos[:3])
                step_dist = 12.0 if swim_step_allowed(swim_pos, away_yaw, 12.0) else 5.0
                target_x, target_z = inward_target or (px + ux * step_dist, pz + uz * step_dist)
                try:
                    self.bot.call("look", {"yaw": away_yaw, "pitch": -15.0})
                except (ApiError, httpx.HTTPError):
                    pass
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": target_x,
                            "y": max(63.0, py + 1.0) if py >= 61.0 else py + 1.0,
                            "z": target_z,
                            "range": 1.5,
                            "timeout": 1000 if urgent_escape else 2800,
                            "stop_on": ["death"],
                        },
                        **({"timeout_s": 1.5, "allow_reauth": False} if urgent_escape else {}),
                    )
                    actions.append("surface_retreat_move")
                except (ApiError, httpx.HTTPError) as err:
                    if isinstance(err, httpx.HTTPError):
                        self._best_effort_stop(timeout_s=0.35 if urgent_escape else None)
                st_mid = self._safe_status(st_mid)
                interruption = interrupted_water_escape(st_mid)
                if interruption is not None:
                    return interruption
                current = st_mid.get("pos") or swim_pos
                if inward_target is not None and not self._point_allowed(current[0], current[2], action="water_retreat"):
                    away_yaw = aim_yaw_pitch(current, [target_x, current[1], target_z])[0]
                if st_mid.get("in_water") and swim_step_allowed(current, away_yaw, inward_target=inward_target):
                    try:
                        self.bot.call(
                            "control",
                            {"forward": True, "jump": True, "sprint": True, "yaw": away_yaw, "ms": 950, "stop_on": ["death"]},
                        )
                        actions.append("surface_retreat_jump")
                    except (ApiError, httpx.HTTPError) as err:
                        if isinstance(err, httpx.HTTPError):
                            self._best_effort_stop()

        st_end = self._safe_status()
        observed_end_pos = bool(st_end.get("pos"))
        st_end = st_end or st0
        interruption = interrupted_water_escape(st_end)
        if interruption is not None:
            return interruption
        pos_end = st_end.get("pos") or pos0

        still_wet = bool(st_end.get("in_water"))
        try:
            self.bot.call(
                "posture",
                {"shield": bool(keep_shield), "sneak": sneak_on_magma and still_wet, "jump": still_wet},
            )
        except ApiError:
            pass
        # A jump can clear the water flag before the bot has reached the bank.
        if (
            shore_target is not None and not still_wet and st_end.get("on_ground") is False
            and not st_end.get("in_lava") and math.dist(pos_end[:3], shore_target) <= 2.0
        ):
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": shore_target[0], "y": shore_target[1], "z": shore_target[2],
                        "range": 0.25, "timeout": 1600, "stop_on": ["death"],
                    },
                    timeout_s=2.5, allow_reauth=False,
                )
                actions.append("shore_landing")
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.HTTPError):
                    self._best_effort_stop()
            st_end = self._safe_status({**st_end, "ready": False})
            interruption = interrupted_water_escape(st_end)
            if interruption is not None:
                return interruption
            pos_end = st_end.get("pos") or pos_end
            still_wet = bool(st_end.get("in_water"))
            if still_wet:
                try:
                    self.bot.call("posture", {"shield": bool(keep_shield), "sneak": sneak_on_magma, "jump": True})
                except ApiError:
                    pass
        if keep_shield and isinstance(shield_target_id, int):
            try:
                self.bot.call("look", {"entity_id": shield_target_id}, timeout_s=0.9, allow_reauth=False)
                actions.append("face_water_attacker")
            except (ApiError, httpx.HTTPError):
                pass
        # A timed-out crossing can also find dry footing before the selected bank.
        if observed_end_pos and not still_wet:
            landing_bank = shore_target
            if landing_bank is not None and math.dist(pos_end[:3], landing_bank) > 2.0:
                landing_bank = None
            st_end = settle_landing(st_end, bank=landing_bank)
            interruption = interrupted_water_escape(st_end)
            if interruption is not None:
                return interruption
            pos_end = st_end.get("pos") or pos_end
            still_wet = bool(st_end.get("in_water"))
            if still_wet:
                try:
                    self.bot.call("posture", {"shield": bool(keep_shield), "sneak": sneak_on_magma, "jump": True})
                except ApiError:
                    pass
        landed = st_end.get("on_ground") is not False and not st_end.get("in_lava")
        if not still_wet and landed:
            self._stalled_water_shores.clear()
            self.remember_dry_bank(st_end)
        elif observed_end_pos and shore_target is not None and life == (st_end.get("dimension"), st_end.get("last_death")):
            tried_lip = "shore_lip_jump" in actions
            lip_height = shore_target[1] - pos_end[1]
            lip_distance = math.hypot(shore_target[0] - pos_end[0], shore_target[2] - pos_end[2])
            stalled_below_lip = (
                tried_lip and lip_distance <= 2.5 and lip_height >= (1.75 if still_wet else 0.5)
                and (still_wet or "shore_landing" in actions)
            )
            directly_below = (
                still_wet and "shore_lip_directly_below" in actions and lip_distance <= 0.6 and lip_height >= 0.75
            )
            no_approach = still_wet and (tried_lip or shore_path_unreachable) and (
                math.dist(search_pos[:3], shore_target) - math.dist(pos_end[:3], shore_target) < 0.5
            )
            if stalled_below_lip or directly_below or no_approach:
                self._stalled_water_shores[tuple(shore_target)] = (tuple(pos_end[:3]), life, time.monotonic())
        if (
            still_wet and not retrying_bank and shore_target is not None and "shore_lip_jump" in actions
            and "shore_lip_directly_below" not in actions
            and life == (st_end.get("dimension"), st_end.get("last_death"))
            and shore_bank_in_reach(pos_end, shore_target)
        ):
            self._nearby_water_bank = (tuple(shore_target), life, time.monotonic())
        return {
            "ok": not still_wet and landed,
            "in_water": still_wet,
            "mode": "move_to_shore" if shore_target is not None else "surface_retreat",
            "shore_target": shore_target,
            "actions": actions,
            "pos_after": pos_end,
            **({"reason": "shore_landing_unconfirmed"} if not still_wet and not landed else {}),
        }


    def guard_lava_edge(
        self,
        pos: Sequence[float] | None = None,
        *,
        counts: dict[str, int] | None = None,
        flowing_lava_target: list[int] | None = None,
        staircase_lava_target: list[int] | None = None,
    ) -> dict[str, Any]:
        """Force sneak posture and seal exposed lava around the current foothold."""
        st0 = self.bot.status()
        if bool(st0.get("in_lava")) or st0.get("on_ground") is not True or st0.get("ready") is False or st0.get("alive") is False:
            return {"ok": False, "near_lava": False, "sneak": bool(st0.get("sneaking")), "sealed": 0}

        cur_pos = st0.get("pos") or pos or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(cur_pos[0])), math.floor(float(cur_pos[1]) + 0.05), math.floor(float(cur_pos[2]))
        exposed_lava: list[list[int]] = []
        reported_lava = staircase_lava_target if staircase_lava_target is not None else flowing_lava_target
        if reported_lava is not None:
            fx, fy, fz = reported_lava
            min_height = -1 if staircase_lava_target is not None else 1
            # A staircase can expose a diagonal or upper cell the cardinal foothold scan misses.
            if min_height <= fy - by <= 2 and max(abs(fx - bx), abs(fz - bz)) == 1:
                self.invalidate_block_cache()
                if self._point_allowed(fx + 0.5, fz + 0.5, action="guard_lava_edge") and _is_lava_block(
                    self._get_block_name(fx, fy, fz, strict=True)
                ):
                    exposed_lava.append(reported_lava)
        for dx, dz in CARDINAL_VECTORS.values():
            nx, nz = bx + dx, bz + dz
            below_feet = self._get_block_name(nx, by - 1, nz, strict=True)
            if _is_lava_block(below_feet):
                exposed_lava.append([nx, by - 1, nz])
                continue
            at_feet = self._get_block_name(nx, by, nz, strict=True)
            if _is_lava_block(at_feet):
                exposed_lava.append([nx, by, nz])
            elif (
                _is_passable(at_feet) and _is_passable(below_feet)
                and _is_lava_block(self._get_block_name(nx, by - 2, nz, strict=True))
            ):
                exposed_lava.append([nx, by - 2, nz])

        if not exposed_lava:
            return {"ok": True, "near_lava": False, "sneak": bool(st0.get("sneaking")), "sealed": 0}

        sneaking = bool(st0.get("sneaking"))
        if not sneaking:
            try:
                self.bot.call(
                    "posture",
                    {"sneak": True, "shield": bool(st0.get("blocking")), "jump": False},
                )
                sneaking = True
            except ApiError:
                pass

        if counts is None:
            try:
                counts, _, _ = self._inventory_counts_and_shield()
            except Exception:
                counts = {}
        filler = self._pick_filler_block(counts)
        sealed_positions: list[list[int]] = []
        if filler is not None:
            unique_lava = list(dict.fromkeys(map(tuple, exposed_lava)))
            for index, coordinates in enumerate(unique_lava[:2]):
                lpos = list(coordinates)
                if index:
                    current = self._safe_status()
                    if (
                        current.get("in_lava") or current.get("on_ground") is not True
                        or current.get("ready") is False or current.get("alive") is False
                        or any(current.get(key) != st0.get(key) for key in ("last_death", "dimension"))
                    ):
                        break
                if not self._point_allowed(lpos[0] + 0.5, lpos[2] + 0.5, action="guard_lava_edge"):
                    continue
                try:
                    placed = self.bot.call("place", {"item": filler, "pos": lpos})
                except ApiError:
                    continue
                if isinstance(placed, dict) and placed.get("ok") is not False:
                    sealed_positions.append(lpos)
            if sealed_positions:
                self.invalidate_block_cache()
        unsealed_lava = None
        if staircase_lava_target is not None and staircase_lava_target in exposed_lava and staircase_lava_target[1] >= by:
            self.invalidate_block_cache()
            try:
                remaining = self._get_block_name(*staircase_lava_target, strict=True, deadline=time.monotonic() + 0.35)
            except _TerrainProbeExpired:
                remaining = ""
            if not _is_full_cover_block(remaining) and staircase_lava_target in sealed_positions:
                sealed_positions.remove(staircase_lava_target)
            if _is_lava_block(remaining):
                unsealed_lava = staircase_lava_target
        return {
            "ok": True, "near_lava": True, "sneak": sneaking,
            "sealed": len(sealed_positions), "sealed_positions": sealed_positions,
            **({"unsealed_lava_target": unsealed_lava} if unsealed_lava is not None else {}),
        }

