"""CombatEnv mixin: terrain_tactics."""

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


class _TerrainTacticsMixin:
    def _get_block_name(
        self, x: int, y: int, z: int, *, strict: bool = False, deadline: float | None = None,
    ) -> str:
        key = (int(x), int(y), int(z))
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise _TerrainProbeExpired
        cached = self._block_cache.get(key)
        if cached is not None and (now - cached[1]) <= self.block_cache_ttl:
            name = cached[0]
        else:
            try:
                transport = {"timeout_s": min(0.35, deadline - now), "allow_reauth": False} if deadline is not None else {}
                res = self.bot.call("get_block", {"pos": [key[0], key[1], key[2]]}, **transport)
                name = _clean_block_name(res) if res.get("name") or res.get("type") else ""
            except Exception as error:
                if _is_control_signal(error):
                    raise
                name = None
            self._block_cache[key] = (name, now)
        if deadline is not None and (not name or time.monotonic() >= deadline):
            raise _TerrainProbeExpired
        if name or strict:
            return name or ""
        return "stone" if name is None else "air"


    def _inventory_counts_and_shield(self, inv: dict[str, Any] | None = None) -> tuple[dict[str, int], bool, str | None]:
        raw = inv if isinstance(inv, dict) else self.bot.call("inventory", method="GET")
        counts: dict[str, int] = {}
        for row in raw.get("items") or []:
            if isinstance(row, dict) and row.get("name"):
                nm = _clean_block_name(row["name"])
                cnt = int(row.get("count") or 0)
                if cnt > 0:
                    counts[nm] = counts.get(nm, 0) + cnt
        offhand = raw.get("offhand") if isinstance(raw.get("offhand"), dict) else {}
        off_name = _clean_block_name(offhand.get("name")) if offhand.get("name") else ""
        has_shield = off_name == "shield" or counts.get("shield", 0) > 0
        held = raw.get("held") if isinstance(raw.get("held"), dict) else {}
        held_name = _clean_block_name(held.get("name")) if held.get("name") else None
        return counts, has_shield, held_name


    def has_los(
        self,
        src_pos: Sequence[float] | dict[str, Any],
        dst_pos: Sequence[float] | dict[str, Any] | None = None,
        *,
        src_eye: float = 1.62,
        dst_height: float = 1.0,
        deadline: float | None = None,
        planned_cover: Collection[tuple[int, int, int]] = (),
    ) -> dict[str, Any]:
        """Check voxel line-of-sight between `src_pos` and `dst_pos`.

        Also accepts single-arg `has_los(target_pos_or_entity)` by reading `bot.status()['pos']`.
        """
        if dst_pos is None:
            target = src_pos
            st = self.bot.status()
            s_pos = st.get("pos") or [0.0, 64.0, 0.0]
        else:
            s_pos = src_pos
            target = dst_pos

        if isinstance(s_pos, dict):
            s_pos = s_pos.get("pos") or [0.0, 64.0, 0.0]
        if isinstance(target, dict):
            target = target.get("pos") or [0.0, 64.0, 0.0]

        sx, sy, sz = float(s_pos[0]), float(s_pos[1]) + float(src_eye), float(s_pos[2])
        tx, ty, tz = float(target[0]), float(target[1]) + float(dst_height), float(target[2])
        dx, dy, dz = tx - sx, ty - sy, tz - sz
        total_dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if total_dist < 0.35:
            return {"clear": True, "blocker": None, "dist": round(total_dist, 2)}

        steps = min(28, max(2, int(math.ceil(total_dist / 0.45))))
        start_voxel = (math.floor(sx), math.floor(sy), math.floor(sz))
        end_voxel = (math.floor(tx), math.floor(ty), math.floor(tz))
        seen_voxels: set[tuple[int, int, int]] = {start_voxel, end_voxel}
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}

        for i in range(1, steps):
            frac = i / steps
            px, py, pz = sx + dx * frac, sy + dy * frac, sz + dz * frac
            voxel = (math.floor(px), math.floor(py), math.floor(pz))
            if voxel in seen_voxels:
                continue
            seen_voxels.add(voxel)
            bname = "stone" if voxel in planned_cover else self._get_block_name(*voxel, **probe_kwargs)
            if not _is_los_transparent(bname):
                bdist = round(total_dist * frac, 2)
                return {
                    "clear": False,
                    "blocker": {"name": bname, "pos": [voxel[0], voxel[1], voxel[2]], "dist": bdist},
                    "dist": round(total_dist, 2),
                }
        return {"clear": True, "blocker": None, "dist": round(total_dist, 2)}


    def probe_ray(
        self,
        origin: Sequence[float],
        yaw: float,
        *,
        dist: float = 3.0,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Probe walkability, 1-block step-ups, cliffs, traps, fluids, and geofence along `yaw`."""
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
        base_y = math.floor(oy + 0.05)
        ux, uz = yaw_to_unit_xz(yaw)
        max_r = max(0.8, float(dist))

        needs_jump = False
        blocked_by_wall = False
        max_drop = 0
        hazard: str | None = None
        safe_dist = 0.0

        sample_d = 0.9
        cur_y = base_y
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        while sample_d <= max_r + 1e-6:
            nx = ox + ux * sample_d
            nz = oz + uz * sample_d
            if self.geofence is not None and hasattr(self.geofence, "check_point"):
                if self.geofence.check_point(nx, nz, action="probe_ray") is not None:
                    hazard = "geofence"
                    break

            bx, bz = math.floor(nx), math.floor(nz)
            foot_b = self._get_block_name(bx, cur_y, bz, **probe_kwargs)
            head_b = self._get_block_name(bx, cur_y + 1, bz, **probe_kwargs)

            if foot_b in {"lava", "flowing_lava"} or head_b in {"lava", "flowing_lava"}:
                hazard = "lava"
                break
            if foot_b in {"fire", "soul_fire"} or head_b in {"fire", "soul_fire"}:
                hazard = "fire"
                break
            if foot_b in TRAP_BLOCKS or head_b in TRAP_BLOCKS:
                hazard = "trap"
                break
            if foot_b in {"water", "flowing_water"}:
                hazard = "water"
                break

            if not _is_passable(head_b):
                blocked_by_wall = True
                hazard = "wall"
                break

            if not _is_passable(foot_b):
                # Check if this is a clean 1-block step-up (`cur_y + 1` and `cur_y + 2` + overhead at origin clear)
                step_head2 = self._get_block_name(bx, cur_y + 2, bz, **probe_kwargs)
                orig_above = self._get_block_name(math.floor(ox), cur_y + 2, math.floor(oz), **probe_kwargs)
                if _is_solid_ground(foot_b) and _is_passable(step_head2) and _is_passable(orig_above):
                    needs_jump = True
                    cur_y += 1
                    safe_dist = round(sample_d, 2)
                    sample_d = round(sample_d + 0.85, 4)
                    continue
                blocked_by_wall = True
                hazard = "wall"
                break

            # Foot and head at `cur_y` are passable -> measure drop below `(bx, cur_y - 1, bz)`
            drop = 0
            landed_hazard: str | None = None
            for dy_down in range(1, 6):
                below_b = self._get_block_name(bx, cur_y - dy_down, bz, **probe_kwargs)
                if below_b in {"lava", "flowing_lava"}:
                    landed_hazard = "lava"
                    break
                if below_b in {"fire", "soul_fire"}:
                    landed_hazard = "fire"
                    break
                if below_b in TRAP_BLOCKS:
                    landed_hazard = "trap"
                    break
                if below_b in {"water", "flowing_water"}:
                    landed_hazard = "water"
                    break
                if _is_solid_ground(below_b):
                    break
                drop = dy_down

            if landed_hazard is not None:
                hazard = landed_hazard
                break
            max_drop = max(max_drop, drop)
            if drop >= 4:
                hazard = "cliff"
                break
            cur_y -= min(drop, 1)
            safe_dist = round(sample_d, 2)
            sample_d = round(sample_d + 0.85, 4)

        if safe_dist >= max_r - 0.86 and hazard is None and not blocked_by_wall:
            safe_dist = round(max_r, 2)

        is_safe = (hazard is None) and (not blocked_by_wall) and (max_drop < 4) and (safe_dist >= min(1.5, max_r))
        return {
            "safe": bool(is_safe),
            "walkable_flat": bool(is_safe and not needs_jump and max_drop <= 1),
            "needs_jump": bool(needs_jump),
            "blocked_by_wall": bool(blocked_by_wall),
            "max_drop": int(max_drop),
            "hazard": hazard,
            "safe_dist": float(safe_dist),
        }


    def scan_safe_yaws(
        self,
        origin: Sequence[float],
        preferred_yaw: float,
        *,
        dist: float = 3.0,
        angles_deg: Sequence[float] = (0, 35, -35, 70, -70, 110, -110, 150, -150),
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Fan-probe around `preferred_yaw`, ranking safe directions with minimal angular deviation first."""
        candidates: list[dict[str, Any]] = []
        for deg in angles_deg:
            cand_yaw = float(preferred_yaw) + math.radians(float(deg))
            try:
                ray = self.probe_ray(origin, cand_yaw, dist=dist, **({"deadline": deadline} if deadline is not None else {}))
            except _TerrainProbeExpired:
                break
            candidates.append(
                {
                    "yaw": round(cand_yaw, 4),
                    "offset_deg": float(deg),
                    "safe": ray["safe"],
                    "needs_jump": ray["needs_jump"],
                    "safe_dist": ray["safe_dist"],
                    "hazard": ray["hazard"],
                }
            )

        safe_cands = [c for c in candidates if c["safe"]]
        safe_cands.sort(key=lambda c: (abs(c["offset_deg"]), 1 if c["needs_jump"] else 0, -c["safe_dist"]))
        if safe_cands:
            best = safe_cands[0]
            return {
                "best_yaw": best["yaw"],
                "best_needs_jump": best["needs_jump"],
                "cornered": False,
                "candidates": candidates,
            }
        return {
            "best_yaw": None,
            "best_needs_jump": False,
            "cornered": True,
            "candidates": candidates,
        }


    def fluid_above_headroom(self, pos: Sequence[float], target: Sequence[int]) -> str | None:
        """Identify water or lava held back by a block above the bot."""
        x, y, z = pos[:3]
        tx, ty, tz = target[:3]
        feet_y = math.floor(y + 0.05)
        if ty not in {feet_y + 1, feet_y + 2} or (tx, tz) not in _hitbox_columns(x, z):
            return None
        block = self._get_block_name(tx, ty + 1, tz, strict=True)
        if _is_lava_block(block):
            return "lava"
        if block in {"water", "flowing_water"}:
            return "water"
        return None


    def lava_at_unstick_opening(
        self, pos: Sequence[float], target: Sequence[int], *, deadline: float,
    ) -> tuple[int, int, int] | None:
        """Check the faces that could let lava flow into a nearby opening before digging it."""
        x, y, z = pos[:3]
        tx, ty, tz = target[:3]
        feet_y = math.floor(y + 0.05)
        if not feet_y <= ty <= feet_y + 2 or not any(
            abs(tx - bx) + abs(tz - bz) <= 1 for bx, bz in _hitbox_columns(x, z)
        ):
            return None
        neighbors = [(tx, ty + 1, tz)]
        neighbors.extend((tx + dx, ty, tz + dz) for dx, dz in CARDINAL_VECTORS.values())
        for cell in neighbors:
            if _is_lava_block(self._get_block_name(*cell, strict=True, deadline=deadline)):
                return cell
        return None


    def can_remove_footing(self, pos: Sequence[float], target: Sequence[int]) -> bool:
        """Allow ordinary digs and check the landing for a dig through the current floor."""
        x, y, z = pos[:3]
        tx, ty, tz = target[:3]
        if ty != math.floor(y + 0.05) - 1 or (tx, tz) not in _hitbox_columns(x, z):
            return True

        # A sliver of support at the edge of the hitbox can disappear with a small displacement.
        center_x, center_z = math.floor(x), math.floor(z)
        if (center_x, center_z) != (tx, tz):
            floor = self._get_block_name(center_x, ty, center_z, strict=True)
            if floor and floor not in HAZARD_BLOCK_TYPES and _is_solid_ground(floor):
                return True

        for depth in range(1, 4):
            floor = self._get_block_name(tx, ty - depth, tz, strict=True)
            if not floor or floor in HAZARD_BLOCK_TYPES:
                return False
            if _is_solid_ground(floor):
                return True
            if not _is_passable(floor):
                return False
        return False


    def has_safe_footing(
        self, pos: Sequence[float], *, require_observation: bool = False, deadline: float | None = None,
    ) -> bool:
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        for block in footing_blocks(pos):
            floor = self._get_block_name(*block, strict=True, **probe_kwargs)
            if not floor and require_observation:
                raise _TerrainProbeExpired
            if not floor or floor in HAZARD_BLOCK_TYPES or not _is_solid_ground(floor):
                return False
        return True


    def probe_retreat(
        self, origin: Sequence[float], yaw: float, dist: float, *, cross_magma: bool = False,
        deadline: float | None = None, allow_step_down: bool = False, short_climb: bool = False,
    ) -> tuple[int, bool] | None:
        """Check support and clearance under the entire hitbox along a short retreat."""
        ox, oy, oz = map(float, origin[:3])
        ux, uz = yaw_to_unit_xz(yaw)
        start_y = math.floor(oy + 0.05)
        walk_y = start_y
        needs_jump = False
        samples = max(1, math.ceil(dist / 0.45))
        crossing_hazards = (
            tuple(name for name in HAZARD_BLOCK_TYPES if name != "magma_block") if cross_magma else HAZARD_BLOCK_TYPES
        )
        probe_kwargs = {"deadline": deadline} if deadline is not None else {}
        for step in range(1, samples + 1):
            travel = dist * step / samples
            nx, nz = ox + ux * travel, oz + uz * travel
            if not self._point_allowed(nx, nz, action="flee"):
                return None
            bx, bz = math.floor(nx), math.floor(nz)
            foot = self._get_block_name(bx, walk_y, bz, strict=True, **probe_kwargs)
            if not foot or foot in HAZARD_BLOCK_TYPES:
                return None
            if _is_solid_ground(foot):
                walk_y += 1
                needs_jump = True
            elif _is_passable(self._get_block_name(bx, walk_y - 1, bz, strict=True, **probe_kwargs)):
                if short_climb:
                    return None
                walk_y -= 1
            if abs(walk_y - start_y) > 1:
                return None
            floor_hazards = crossing_hazards if step < samples else HAZARD_BLOCK_TYPES
            for hx, hz in _hitbox_columns(nx, nz):
                foot, head, floor = (
                    self._get_block_name(hx, walk_y + dy, hz, strict=True, **probe_kwargs) for dy in (0, 1, -1)
                )
                # Feet can remain on a trailing upper block until the hitbox clears its edge.
                if (
                    allow_step_down and walk_y == start_y - 1 and step < samples
                    and (hx, hz) != (bx, bz) and (hx + 0.5 - nx) * ux + (hz + 0.5 - nz) * uz < 0
                    and foot and foot not in HAZARD_BLOCK_TYPES and _is_solid_ground(foot) and _is_passable(head)
                    and _is_passable(self._get_block_name(hx, walk_y + 2, hz, strict=True, **probe_kwargs))
                ):
                    continue
                if not _is_passable(foot) or not _is_passable(head) or not floor or floor in floor_hazards:
                    return None
                if (hx, hz) != (bx, bz) and _is_passable(floor):
                    if allow_step_down and walk_y < start_y:
                        return None
                    floor = self._get_block_name(hx, walk_y - 2, hz, strict=True, **probe_kwargs)
                if not floor or floor in floor_hazards or not _is_solid_ground(floor):
                    return None
        if short_climb and needs_jump:
            # The jump can reach above the standing headroom before landing on the step.
            for step in range(samples + 1):
                travel = dist * step / samples
                for hx, hz in _hitbox_columns(ox + ux * travel, oz + uz * travel):
                    for height in (start_y + 2, start_y + 3):
                        head = self._get_block_name(hx, height, hz, strict=True, **probe_kwargs)
                        if not _is_passable(head) or head in HAZARD_BLOCK_TYPES:
                            return None
        return walk_y, needs_jump


    def is_on_hot_floor(self, pos: Sequence[float]) -> bool:
        x, y, z = map(float, pos[:3])
        ground_y = math.floor(y + 0.05) - 1
        return any(self._get_block_name(bx, ground_y, bz, strict=True) == "magma_block" for bx, bz in _hitbox_columns(x, z))


    def extinguish_contact_fire(self, pos: Sequence[float], *, sneaking: bool = False) -> dict[str, Any]:
        """Clear a verified flame intersecting the bot without moving or breaking support."""
        x, y, z = pos[:3]
        height = 1.5 if sneaking else 1.8
        columns = _hitbox_columns(x, z)
        flames = {"fire", "soul_fire"}
        self.invalidate_block_cache()
        for by in range(math.floor(y), math.ceil(y + height)):
            for bx, bz in columns:
                if self._get_block_name(bx, by, bz, strict=True) not in flames:
                    continue
                target = [bx, by, bz]
                self.bot.call("dig", {"pos": target, "collect": False, "stop_on": ["death"]},
                              timeout_s=0.6, allow_reauth=False)
                self.invalidate_block_cache()
                after = self._get_block_name(*target, strict=True)
                return {"ok": bool(after) and after not in flames, "block_pos": target, "block_after": after}
        return {"ok": False, "reason": "no_contact_fire"}


    def douse_in_adjacent_water(self, before: dict[str, Any]) -> dict[str, Any]:
        """Reach adjacent shallow water only across verified, supported terrain."""
        ox, oy, oz = before["pos"][:3]
        bx, by, bz = math.floor(ox), math.floor(oy + 0.05), math.floor(oz)
        self.invalidate_block_cache()

        def safe_floor(x: int, y: int, z: int) -> bool:
            block = self._get_block_name(x, y, z, strict=True)
            return bool(block) and block not in HAZARD_BLOCK_TYPES and _is_solid_ground(block)

        for dx, dz in CARDINAL_VECTORS.values():
            wx, wz = bx + dx, bz + dz
            water_y = next((y for y in (by, by - 1) if self._get_block_name(wx, y, wz, strict=True) == "water"), None)
            if water_y is None or not safe_floor(wx, water_y - 1, wz):
                continue
            if not all(_is_passable(self._get_block_name(wx, y, wz, strict=True)) for y in range(water_y + 1, by + 2)):
                continue
            x, z = wx + 0.5, wz + 0.5
            steps = max(1, math.ceil(math.hypot(x - ox, z - oz) / 0.3))
            path = [(ox + (x - ox) * i / steps, oz + (z - oz) * i / steps) for i in range(steps + 1)]
            if not all(self._point_allowed(px, pz, action="fire_douse") for px, pz in path):
                continue
            columns = {(cx, cz) for px, pz in path for cx, cz in _hitbox_columns(px, pz)}
            columns.discard((wx, wz))
            if not all(
                safe_floor(cx, by - 1, cz)
                and all(_is_passable(self._get_block_name(cx, y, cz, strict=True)) for y in (by, by + 1))
                for cx, cz in columns
            ):
                continue
            current = self._safe_status()
            current_pos = current.get("pos")
            if (
                not current_pos or math.dist(current_pos[:3], before["pos"][:3]) > 0.2
                or current.get("ready") is not True or current.get("alive") is False
                or current.get("on_ground") is not True or current.get("in_lava") or not current.get("on_fire")
                or current.get("dimension") != before.get("dimension") or current.get("last_death") != before.get("last_death")
            ):
                return {"ok": False, "reason": "position_or_hazard_changed"}
            try:
                self.bot.call(
                    "move_to", {"x": x, "y": water_y, "z": z, "range": 0.2, "timeout": 650, "stop_on": ["death"]},
                    timeout_s=0.95, allow_reauth=False,
                )
            except (ApiError, httpx.HTTPError) as error:
                if isinstance(error, httpx.HTTPError):
                    self._best_effort_stop(timeout_s=0.25)
            after = self._safe_status()
            ok = bool(
                after.get("ready") is True and after.get("alive") is not False
                and after.get("in_water") and not after.get("in_lava") and after.get("on_fire") is False
                and after.get("last_death") == before.get("last_death") and after.get("dimension") == before.get("dimension")
            )
            return {"ok": ok, "pos_after": after.get("pos"), **({"reason": "douse_unconfirmed"} if not ok else {})}
        return {"ok": False, "reason": "no_safe_shallow_water"}


    def step_off_hot_floor(self, status: dict[str, Any]) -> dict[str, Any]:
        pos = status.get("pos")
        if not pos or status.get("on_ground") is not True:
            return {"ok": False, "reason": "unsafe_landing"}
        ox, oy, oz = map(float, pos[:3])
        self.invalidate_block_cache()
        for steps in (1, 2):
            for dx, dz in CARDINAL_VECTORS.values():
                x = math.floor(ox) + 0.5 + steps * dx
                z = math.floor(oz) + 0.5 + steps * dz
                yaw, _ = aim_yaw_pitch(pos, [x, oy, z])
                route = self.probe_retreat(pos, yaw, math.hypot(x - ox, z - oz), cross_magma=True)
                if route is not None:
                    y, _ = route
                    return self._move_to_hot_floor_exit(status, [x, y, z])
        return {"ok": False, "reason": "no_safe_hot_floor_exit"}


    def _move_to_hot_floor_exit(self, before: dict[str, Any], target: list[float]) -> dict[str, Any]:
        x, y, z = target
        try:
            self.bot.call("move_to", {"x": x, "y": y, "z": z, "range": 0.15, "timeout": 1100, "stop_on": ["death"]})
        except (ApiError, httpx.HTTPError) as error:
            if isinstance(error, httpx.HTTPError):
                self._best_effort_stop()
        after = self._safe_status({"ready": False})
        pos_after = after.get("pos")
        if (
            not pos_after or after.get("ready") is False or after.get("alive") is False
            or after.get("on_ground") is not True or after.get("in_lava") or after.get("in_water")
            or (after.get("last_death") is not None and after["last_death"] != before.get("last_death"))
            or math.hypot(float(pos_after[0]) - x, float(pos_after[2]) - z) > 0.35
        ):
            return {"ok": False, "reason": "unsafe_landing", "pos_after": pos_after}
        self.invalidate_block_cache()
        ground_y = math.floor(float(pos_after[1]) + 0.05) - 1
        floors = [self._get_block_name(bx, ground_y, bz, strict=True) for bx, bz in _hitbox_columns(pos_after[0], pos_after[2])]
        safe = all(name and name not in HAZARD_BLOCK_TYPES and _is_solid_ground(name) for name in floors)
        if not safe:
            return {"ok": False, "reason": "unsafe_landing", "pos_after": pos_after}
        return {"ok": True, "mode": "stepped_off", "pos_after": pos_after}


    def _query_hazards(self, bx: int, by: int, bz: int, radius: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._hazard_cache is not None:
            c_origin, c_rad, c_ts, c_rows = self._hazard_cache
            if (
                c_rad == radius
                and (now - c_ts) <= self.hazard_cache_ttl
                and abs(c_origin[0] - bx) + abs(c_origin[1] - by) + abs(c_origin[2] - bz) <= 2
            ):
                return list(c_rows)
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": max(2, int(radius)), "types": list(HAZARD_BLOCK_TYPES), "limit": 24},
            )
            rows = []
            for b in (found or {}).get("blocks") or []:
                if isinstance(b, dict) and isinstance(b.get("pos"), list):
                    rows.append(
                        {
                            "name": _clean_block_name(b.get("name")),
                            "pos": [int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2])],
                            "dist": round(float(b.get("dist", 0.0)), 2),
                        }
                    )
        except Exception:
            rows = []
        self._hazard_cache = ((bx, by, bz), radius, now, rows)
        return list(rows)


    def arena_snapshot(
        self,
        pos: Sequence[float] | None = None,
        *,
        target_pos: Sequence[float] | None = None,
        hazard_radius: int = 10,
    ) -> dict[str, Any]:
        """Summarize local enclosure, overhead pillar clearance, nearby hazards, and target drop hazard."""
        if pos is None:
            st = self.bot.status()
            pos = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1]) + 0.05), math.floor(float(pos[2]))

        open_dirs: list[str] = []
        solid_wall_dirs: list[str] = []
        for dname, (dx, dz) in CARDINAL_VECTORS.items():
            foot_n = self._get_block_name(bx + dx, by, bz + dz)
            head_n = self._get_block_name(bx + dx, by + 1, bz + dz)
            if _is_passable(foot_n) and _is_passable(head_n):
                open_dirs.append(dname)
            else:
                solid_wall_dirs.append(dname)

        max_pillar = 0
        for h in range(1, 4):
            if _is_passable(self._get_block_name(bx, by + 1 + h, bz)):
                max_pillar = h
            else:
                break

        roof_solid = not _is_passable(self._get_block_name(bx, by + 2, bz))
        opposite_walls = (
            ("north" in solid_wall_dirs and "south" in solid_wall_dirs)
            or ("east" in solid_wall_dirs and "west" in solid_wall_dirs)
        )
        if len(solid_wall_dirs) == 4 and roof_solid:
            enclosure = "sealed_bunker"
        elif roof_solid and (len(solid_wall_dirs) >= 3 or opposite_walls):
            enclosure = "carved_shaft"
        elif roof_solid or max_pillar < 3:
            enclosure = "open_cave"
        else:
            enclosure = "surface"

        foot_here = self._get_block_name(bx, by, bz)
        in_water_or_lava = foot_here in {"water", "flowing_water", "lava", "flowing_lava"}
        hazards = self._query_hazards(bx, by, bz, int(hazard_radius))

        target_over_hazard = False
        if target_pos is not None and len(target_pos) >= 3:
            tx, ty, tz = math.floor(float(target_pos[0])), math.floor(float(target_pos[1])), math.floor(float(target_pos[2]))
            air_streak = 0
            for dy_down in range(1, 6):
                bn = self._get_block_name(tx, ty - dy_down, tz)
                if bn in {"lava", "flowing_lava"}:
                    target_over_hazard = True
                    break
                if _is_solid_ground(bn) or bn in {"water", "flowing_water"}:
                    break
                air_streak += 1
            if air_streak >= 5:
                target_over_hazard = True

        return {
            "enclosure": enclosure,
            "open_dirs": open_dirs,
            "solid_wall_dirs": solid_wall_dirs,
            "max_pillar_height": max_pillar,
            "in_water_or_lava": in_water_or_lava,
            "hazards": hazards,
            "target_over_hazard": target_over_hazard,
        }


    def chase_to_range(
        self,
        target: int | Sequence[float],
        *,
        range_m: float = 2.5,
        timeout_ms: int = 1200,
        stop_on: Sequence[str] = ("death",),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "range": max(0.5, float(range_m)),
            "timeout": max(300, int(timeout_ms)),
            "stop_on": list(stop_on),
        }
        target_coords: list[float] | None = None
        if isinstance(target, int):
            payload["entity_id"] = int(target)
        else:
            tx, tz = float(target[0]), float(target[2])
            if self.geofence is not None and hasattr(self.geofence, "check_point"):
                if self.geofence.check_point(tx, tz, action="chase_to_range") is not None:
                    st = self.bot.status()
                    return {
                        "ok": False,
                        "dist_after": 999.0,
                        "pos_after": st.get("pos"),
                        "reason": "geofence_blocked",
                    }
            payload["x"] = tx
            payload["z"] = tz
            if len(target) >= 3:
                payload["y"] = float(target[1])
                target_coords = [tx, float(target[1]), tz]

        reason = "reached"
        ok = True
        try:
            self.bot.call("move_to", payload)
        except ApiError as err:
            ok = False
            if err.code in {"path_not_found", "no_path"}:
                reason = "path_not_found"
            elif err.code in {"timeout", "action_timeout"}:
                reason = "timeout"
            else:
                reason = err.code

        st_after = self.bot.status()
        pos_after = st_after.get("pos") or [0.0, 64.0, 0.0]
        dist_after = 0.0
        if target_coords is not None:
            dist_after = round(
                math.sqrt(sum((float(pos_after[i]) - target_coords[i]) ** 2 for i in range(3))),
                2,
            )
        return {
            "ok": ok,
            "dist_after": dist_after,
            "pos_after": pos_after,
            "reason": reason,
        }


    def _pick_filler_block(self, counts: dict[str, int]) -> str | None:
        for cand in SAFE_TACTICAL_FILLERS:
            if counts.get(cand, 0) > 0:
                return cand
        for name, cnt in counts.items():
            if cnt > 0 and name.endswith("_planks"):
                return name
        return None


    def dig_cover_pocket(self, skills: Any) -> dict[str, Any]:
        result = skills.dig_cover_pocket()
        self.invalidate_block_cache()
        pocket = result.get("pocket")
        if not result.get("ok") or not pocket:
            return result
        pos = result.get("pos_after")
        if pos and [math.floor(pos[0]), math.floor(pos[1] + 0.05), math.floor(pos[2])] == pocket[0]:
            return result
        return {**result, "ok": False, "requires_replan": True, "reason": "cover_not_entered"}


    def _clear_shaft_seal_overlap(
        self, before: dict[str, Any], direction: tuple[int, int], column: tuple[int, int],
    ) -> dict[str, Any] | None:
        origin = before["pos"]
        if not self._on_same_footing(before, before):
            return None
        dx, dz = direction
        yaw = math.atan2(dx, dz)
        feet_y = math.floor(origin[1] + 0.05)
        self.invalidate_block_cache()
        try:
            if self.probe_retreat(origin, yaw, 0.55, deadline=time.monotonic() + 0.55) != (feet_y, False):
                return None
        except _TerrainProbeExpired:
            return None

        def refresh_footing() -> dict[str, Any]:
            current = self._safe_status(timeout_s=0.25)
            if not current or current.get("ready") is False or current.get("alive") is False or any(
                current.get(key) != before.get(key) for key in ("dimension", "last_death")
            ):
                raise _CombatFortificationInterrupted(current, life_unavailable=True)
            return current

        current = refresh_footing()
        if not self._on_same_footing(before, current) or math.dist(current["pos"][:3], origin[:3]) > 0.12:
            return None
        if column not in _hitbox_columns(current["pos"][0], current["pos"][2]):
            return current
        try:
            self.bot.call(
                "control", {"forward": True, "sneak": True, "yaw": yaw, "ms": 220, "stop_on": ["death"]},
                timeout_s=0.45, allow_reauth=False,
            )
        except ApiError:
            pass
        after = refresh_footing()
        point = after.get("pos")
        if (
            not point or math.dist(point[:3], origin[:3]) > 0.55
            or [math.floor(point[0]), math.floor(point[1] + 0.05), math.floor(point[2])]
            != [math.floor(origin[0]), feet_y, math.floor(origin[2])]
            or after.get("on_ground") is not True or any(after.get(key) for key in ("busy", "in_water", "in_lava"))
            or column in _hitbox_columns(point[0], point[2])
        ):
            return None
        return after


    def build_tactical(
        self,
        mode: str,
        *,
        direction: str | None = None,
        height: int = 2,
        threat_pos: Sequence[float] | None = None,
        creeper_seal_life: tuple[Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute tactical block micro-fortifications (`pillar`, `half_seal`, `full_seal`, `cover_pocket`, `place_water_feet`)."""
        norm_mode = str(mode or "").strip().lower()
        if norm_mode == "pillar_up":
            norm_mode = "pillar"
        elif norm_mode == "seal_tunnel":
            norm_mode = "full_seal"

        st = self.bot.status()
        if creeper_seal_life is not None and (
            st.get("ready") is False or st.get("alive") is False or not st.get("pos")
            or (st.get("dimension"), st.get("last_death")) != creeper_seal_life
        ):
            raise _CombatFortificationInterrupted(st, life_unavailable=True)
        pos0 = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos0[0])), math.floor(float(pos0[1]) + 0.05), math.floor(float(pos0[2]))
        counts, _, _ = self._inventory_counts_and_shield()

        if norm_mode == "place_water_feet":
            if counts.get("water_bucket", 0) < 1:
                return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "no_water_bucket"}
            try:
                self.bot.call("interact", {"pos": [bx, by - 1, bz], "item": "water_bucket"})
                self.invalidate_block_cache()
                return {"ok": True, "mode": norm_mode, "placed": 1, "pos_after": self.bot.status().get("pos"), "reason": None}
            except ApiError as err:
                return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": err.code}

        if norm_mode == "cover_pocket":
            if self.skills is not None and hasattr(self.skills, "dig_cover_pocket"):
                res = self.dig_cover_pocket(self.skills)
                return {
                    "ok": bool(res.get("ok")),
                    "mode": "cover_pocket",
                    "placed": len([x for x in (res.get("sealed") or []) if x]),
                    "pos_after": self.bot.status().get("pos"),
                    "reason": None if res.get("ok") else str(res.get("reason") or "cover_failed"),
                }
            return {"ok": False, "mode": "cover_pocket", "placed": 0, "pos_after": pos0, "reason": "no_skills"}

        filler = self._pick_filler_block(counts)
        if filler is None:
            return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "no_blocks"}

        if norm_mode == "pillar":
            want_h = max(1, min(3, int(height)))
            snap = self.arena_snapshot(pos0)
            if snap["max_pillar_height"] < want_h:
                return {
                    "ok": False,
                    "mode": "pillar",
                    "placed": 0,
                    "pos_after": pos0,
                    "reason": "low_ceiling",
                }
            if counts.get(filler, 0) < want_h:
                return {"ok": False, "mode": "pillar", "placed": 0, "pos_after": pos0, "reason": "insufficient_blocks"}
            placed = 0
            cur_y = by
            for _ in range(want_h):
                try:
                    self.bot.call("control", {"jump": True, "ms": 220, "wait": False})
                    time.sleep(0.21)
                    self.bot.call("place", {"item": filler, "pos": [bx, cur_y, bz]})
                    placed += 1
                    cur_y += 1
                    time.sleep(0.12)
                except ApiError:
                    break
            self.invalidate_block_cache()
            st_end = self.bot.status()
            return {
                "ok": placed > 0,
                "mode": "pillar",
                "placed": placed,
                "pos_after": st_end.get("pos"),
                "reason": None if placed == want_h else "partial_pillar",
            }

        if norm_mode in {"half_seal", "full_seal"}:
            # A wall in an adjacent column cannot separate entities already sharing this column.
            if (
                norm_mode == "full_seal" and threat_pos is not None and len(threat_pos) >= 3
                and bx <= float(threat_pos[0]) < bx + 1 and bz <= float(threat_pos[2]) < bz + 1
                and abs(float(threat_pos[1]) - float(pos0[1])) < 1.0
            ):
                return {
                    "ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0,
                    "reason": "threat_inside_shaft",
                }
            chosen_dir = str(direction or "").strip().lower()
            if chosen_dir not in CARDINAL_VECTORS:
                if threat_pos is not None and len(threat_pos) >= 3:
                    tdx = float(threat_pos[0]) - float(pos0[0])
                    tdz = float(threat_pos[2]) - float(pos0[2])
                    if abs(tdx) >= abs(tdz):
                        chosen_dir = "east" if tdx >= 0 else "west"
                    else:
                        chosen_dir = "south" if tdz >= 0 else "north"
                else:
                    snap = self.arena_snapshot(pos0)
                    chosen_dir = snap["open_dirs"][0] if snap["open_dirs"] else "north"
            dx, dz = CARDINAL_VECTORS[chosen_dir]
            tx, tz = bx + dx, bz + dz
            cells = [[tx, by + 1, tz]] if norm_mode == "half_seal" else [[tx, by, tz], [tx, by + 1, tz]]
            repositioned = False
            if creeper_seal_life is not None and (tx, tz) in _hitbox_columns(pos0[0], pos0[2]):
                shifted = self._clear_shaft_seal_overlap(st, (dx, dz), (tx, tz))
                if shifted is None:
                    return {
                        "ok": False, "mode": norm_mode, "direction": chosen_dir, "placed": 0,
                        "placement_attempts": 0, "seal_cells": cells, "reason": "seal_hitbox_overlap",
                    }
                st, pos0 = shifted, shifted["pos"]
                repositioned = True
            placed = 0
            placement_attempts = 0
            for cpos in cells:
                try:
                    if _is_passable(self._get_block_name(cpos[0], cpos[1], cpos[2])):
                        if repositioned:
                            current = self._safe_status(timeout_s=0.25)
                            if not current or current.get("ready") is False or current.get("alive") is False or (
                                current.get("dimension"), current.get("last_death"),
                            ) != creeper_seal_life:
                                raise _CombatFortificationInterrupted(current, life_unavailable=True)
                            if not self._on_same_footing(st, current) or (tx, tz) in _hitbox_columns(
                                current["pos"][0], current["pos"][2],
                            ):
                                break
                        placement_attempts += 1
                        self.bot.call("place", {"item": filler, "pos": cpos})
                        placed += 1
                except ApiError:
                    continue
            self.invalidate_block_cache()
            ok = placed > 0
            reason = None if ok else "place_failed"
            verified = [self._get_block_name(*cell, strict=True) for cell in cells] if norm_mode == "full_seal" else []
            st_end = self.bot.status()
            if norm_mode == "full_seal":
                wall_complete = all(block and _is_solid_ground(block) for block in verified)
                if placed:
                    ok = wall_complete
                    reason = None if ok else "seal_unconfirmed"
                elif wall_complete and threat_pos is not None and len(threat_pos) >= 3:
                    end = st_end.get("pos")
                    if (
                        end and math.dist(end[:3], pos0[:3]) <= 0.2 and st_end.get("on_ground") is True
                        and st_end.get("ready") is not False and st_end.get("alive") is not False
                        and not any(st_end.get(key) for key in ("in_water", "in_lava"))
                        and all(st_end.get(key) == st.get(key) for key in ("dimension", "last_death"))
                    ):
                        from ..library import assess_hostile_occlusion, ray_wall_blocker

                        block_cache = {tuple(cell): block for cell, block in zip(cells, verified)}
                        try:
                            blocker = ray_wall_blocker(self.bot, tuple(end[:3]), tuple(threat_pos[:3]), block_cache)
                            if blocker is not None and blocker.get("pos") in cells:
                                threats = assess_hostile_occlusion(
                                    self.bot, end, radius=6, fallback_nearest=st_end.get("nearest_hostile"),
                                    block_cache=block_cache,
                                )
                                ok = threats["complete"] and not threats["unoccluded"]
                        except (ApiError, httpx.HTTPError):
                            pass
                        if ok:
                            reason = "already_sealed"
            return {
                "ok": ok,
                "mode": norm_mode,
                "direction": chosen_dir,
                "placed": placed,
                "placement_attempts": placement_attempts,
                "seal_cells": cells,
                **({"repositioned_for_seal": True} if repositioned else {}),
                "pos_after": st_end.get("pos"),
                "reason": reason,
            }

        return {"ok": False, "mode": norm_mode, "placed": 0, "pos_after": pos0, "reason": "unknown_mode"}


    def _best_effort_stop(self, *, stop_control: bool = True, timeout_s: float | None = None) -> None:
        """Stop residual movement/pathfinder controls after a timeout or preemption."""
        transport = {"timeout_s": timeout_s, "allow_reauth": False} if timeout_s is not None else {}
        try:
            self.bot.call("stop", {}, **transport)
        except (ApiError, httpx.HTTPError):
            pass
        if stop_control:
            try:
                self.bot.call("control", {"wait": False}, **transport)
            except (ApiError, httpx.HTTPError):
                pass


    def _safe_status(
        self, fallback: dict[str, Any] | None = None, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Fetch live bot status safely without letting transient ReadTimeout abort active reflexes."""
        try:
            if timeout_s is not None:
                res = self.bot.call("status", method="GET", timeout_s=timeout_s, allow_reauth=False)
            elif callable(getattr(self.bot, "status", None)):
                res = self.bot.status()
            else:
                res = self.bot.call("status", method="GET")
            return res if isinstance(res, dict) else dict(fallback or {})
        except (ApiError, httpx.HTTPError):
            return dict(fallback or {})


    def guard_enderman_pursuit(self, before: dict[str, Any], target_id: int) -> dict[str, Any]:
        """Reorient the shield if the same Enderman remains in reach after a retreat."""
        unconfirmed = {"ok": False, "reason": "pursuit_unconfirmed"}

        def nearby(status: dict[str, Any]) -> bool:
            health = status.get("health")
            if not (
                status.get("ready") is True and status.get("alive") is not False
                and isinstance(health, (int, float)) and health > 0
                and not any(status.get(key) for key in ("in_water", "in_lava", "submerged", "busy"))
                and all(status.get(key) == before.get(key) for key in ("last_death", "dimension"))
            ):
                return False
            nearest = status.get("nearest_hostile")
            if nearest is None or nearest == {}:
                # A missing nearest-hostile sample does not rule out the attributed pursuer.
                # Only try the bounded entity scan if the landed bot can still raise its shield.
                offhand = status.get("offhand")
                return (
                    health <= 4.0 and status.get("on_ground") is True and status.get("blocking") is False
                    and isinstance(offhand, dict) and _clean_block_name(offhand.get("name")) == "shield"
                )
            distance = nearest.get("dist") if isinstance(nearest, dict) else None
            return (
                isinstance(nearest, dict) and nearest.get("id") == target_id
                and _clean_block_name(nearest.get("name")) == "enderman" and not nearest.get("blocked_by_wall")
                and isinstance(distance, (int, float)) and 0 <= distance <= 3.5
            )

        initial = self._safe_status(timeout_s=0.25)
        if not nearby(initial):
            return unconfirmed
        try:
            entities = self.bot.call(
                "get_entities", {"radius": 5, "types": ["hostile"]}, timeout_s=0.3, allow_reauth=False,
            ).get("entities")
            if not isinstance(entities, list):
                return unconfirmed
            target = next((
                entity for entity in entities if isinstance(entity, dict) and entity.get("id") == target_id
                and _clean_block_name(entity.get("name")) == "enderman"
                and entity.get("alive") is not False and not entity.get("blocked_by_wall")
            ), None)
            if target is None:
                return unconfirmed
            current = self._safe_status(timeout_s=0.25)
            pos, threat = current.get("pos"), target.get("pos")
            if not nearby(current) or not all(
                isinstance(point, (list, tuple)) and len(point) >= 3
                and all(isinstance(coord, (int, float)) and math.isfinite(coord) for coord in point[:3])
                for point in (pos, threat)
            ):
                return unconfirmed
            distance = math.dist(pos[:3], threat[:3])
            if distance > 3.5:
                return unconfirmed
            yaw, pitch = aim_yaw_pitch(pos, threat, dst_height=0.75)
            self.bot.call("look", {"yaw": yaw, "pitch": pitch}, timeout_s=0.25, allow_reauth=False)
            shield = self.bot.call(
                "posture", {"shield": True, "sneak": False, "jump": False}, timeout_s=0.3, allow_reauth=False,
            )
            nearest_missing = not (initial.get("nearest_hostile") and current.get("nearest_hostile"))
            return {
                "ok": shield.get("blocking") is True, "entity_id": target_id, "distance": distance,
                "threat_remaining": True,
                **({"nearest_hostile_missing": True} if nearest_missing else {}),
            }
        except (ApiError, httpx.HTTPError):
            return unconfirmed


    def _is_near_or_over_lava(self, st_now: dict[str, Any]) -> bool:
        """Return True when the bot's hitbox or immediate 1-block rim touches lava."""
        if bool(st_now.get("in_lava")):
            return True
        pos = st_now.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return False
        try:
            px, py_f, pz = float(pos[0]), float(pos[1]), float(pos[2])
            bx, by, bz = math.floor(px), math.floor(py_f + 0.05), math.floor(pz)
        except (TypeError, ValueError):
            return False
        for hx, hz in _hitbox_columns(px, pz):
            for cy in (by, by - 1):
                if _is_lava_block(self._get_block_name(hx, cy, hz)):
                    return True
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for cy in (by, by - 1):
                    if _is_lava_block(self._get_block_name(bx + dx, cy, bz + dz)):
                        return True
        return False


    def _is_airborne_above_lava(self, st_now: dict[str, Any], *, start_pos: Sequence[float]) -> bool:
        """Return True when in_lava=False only because a jump lifted the bot mid-air or grazing lava."""
        if bool(st_now.get("in_lava")) or st_now.get("alive") is False or st_now.get("ready") is False:
            return False
        if st_now.get("on_ground") is not False:
            return False
        p_now = st_now.get("pos") or start_pos
        try:
            px, py_f, pz = float(p_now[0]), float(p_now[1]), float(p_now[2])
            if math.hypot(px - float(start_pos[0]), pz - float(start_pos[2])) > 64.0:
                return False
            by = math.floor(py_f + 0.05)
        except (TypeError, ValueError, IndexError):
            return False
        for hx, hz in _hitbox_columns(px, pz):
            if _is_lava_block(self._get_block_name(hx, by, hz)):
                return True
            b1 = self._get_block_name(hx, by - 1, hz)
            if _is_lava_block(b1):
                return True
            if _is_passable(b1) and _is_lava_block(self._get_block_name(hx, by - 2, hz)):
                return True
        return False


    def escape_lava(self, *, force_rim_escape: bool = False) -> dict[str, Any]:
        """Deterministically escape lava using water douse (overworld), safe-yaw jump sprint, wall-notch/pillar climb, and live `in_lava` verification."""
        if self._run_lava_escape is not None:
            return self._run_lava_escape(lambda: self._escape_lava(force_rim_escape=force_rim_escape))
        return self._escape_lava(force_rim_escape=force_rim_escape)

