"""MiningDFSEngine mixin: edges_branches (split from legacy mining_dfs.py)."""

from __future__ import annotations

from mc_agent.skills.mining._base import (
    deque,
    Callable,
    copy,
    dataclasses,
    inspect,
    json,
    math,
    os,
    pathlib,
    re,
    time,
    Any,
    httpx,
    ApiError,
    assess_world_target,
    normalize_dimension,
    BLOCK_MIN_PICKAXE_TIER,
    EXPECTED_BLOCK_DROPS,
    PICKAXE_TIERS,
    SHAFT_SEAL_BLOCKS,
    _best_pickaxe_in_inv,
    _compute_empty_slots,
    _expected_drops_for_blocks,
    _inventory_counts,
    _inventory_counts_from_raw,
    _inventory_snapshot_and_counts,
    _normalize_drop_name,
    _positive_inv_delta,
    _total_planks,
    DIRECTIONS,
    OPPOSITE_DIR,
    LEFT_RIGHT_DIRS,
    AIR_BLOCKS,
    FLUID_BLOCKS,
    LAVA_BLOCKS,
    FALLING_BLOCKS,
    UTILITY_BLOCKS,
    PASSABLE_BLOCKS,
    FLIMSY_FLOOR_BLOCKS,
    FLIMSY_FLOOR_SUFFIXES,
    HAZARD_FLOOR_BLOCKS,
    ROCK_BLOCKS,
    SURFACE_GROUND_BLOCKS,
    TRACKED_ORES,
    ORE_DFS_PROFILES,
    DEFAULT_MAX_DURABILITY,
    RADAR_SCAN_RADIUS,
    VEIN_HOOK_RADAR_RADIUS,
    VEIN_HOOK_FISHBONE_RADIUS,
    VEIN_HOOK_SCAN_RADIUS,
    DEPTH_RESET_THRESHOLD,
    SCAN_BAND,
    LAYER_SWITCH_MIN_DELTA,
    CLIMB_HEIGHT_TOLERANCE,
    OFFSET_RETRY_STEPS,
    NODE_REJOIN_DISTANCE,
    SPINE_DESCEND_MIN_DELTA,
    RECRAFT_PICK_DURABILITY,
    MAX_IDLE_ITERATIONS,
    MAX_IDLE_SECONDS,
    DIG_TIMEOUT_COOLDOWN_S,
    DIG_RECOVERY_RESERVE_S,
    WATER_SHAFT_COOLDOWN_S,
    FLUID_ABORT_LIMIT,
    IDLE_CALLS_BEFORE_RELOCATE,
    DRY_CALLS_BEFORE_ABANDON,
    DROP_PICKUP_RADIUS_MANHATTAN,
    FLUID_ABORT_REASONS,
    EXHAUSTED_COLLAR_MIN_SEP,
    UNREACHABLE_ORE_COOLDOWN_S,
    _MiningDeadline,
    _CollarResupplyDeadline,
    _MiningLifeChanged,
    _BudgetSession,
    normalize_dfs_strategy,
    BranchSpec,
    ShaftEdge,
    ShaftNode,
    _WaterInterruptedShaft,
    DeferredOreRecord,
    WorldLedger,
    _default_ledger_path_for_bot,
    _should_focus_target_only,
    _resolve_target_profile,
    _extract_item_remaining_durability,
    _pickaxe_inventory_summary,
    _choose_pickaxe_for_tier,
    _seal_reserve,
    _pick_seal_material,
    _stone_pickaxe_headroom,
    _pickaxe_low_hint,
    _has_collar_resupply_materials,
    _can_stack_expected_drop,
    _is_two_step_stair,
    _is_two_block_vertical_gap,
    _descent_start_beside_anchor,
    _descending_waypoint_prefix,
    _omitted_waypoint_index,
    _interpolate_cells,
    _floor_pos,
)


class _EdgesBranchesMixin:
    def _dig_stair_down_edge(
        self,
        *,
        direction: str,
        max_steps: int,
        target_y: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dx, dz = DIRECTIONS[direction]
        waypoints: list[tuple[int, int, int]] = []
        cache: dict[tuple[int, int, int], str] = {}
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints.append(start_anchor)

        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            cur = _floor_pos(st["pos"])
            bx, by, bz = cur
            if by <= target_y:
                stop_reason = "reached_target_y"
                break

            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if self._should_recraft_burner_pickaxe(chosen_rock, total_rem, in_deepslate=(by < 0)) and self._try_recraft_burner_pickaxe(
                counts_now, at_durability_redline=total_rem <= redline,
            ):
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
                chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_stair") is not None:
                stop_reason = "geofence_blocked"
                break

            cur_head = (bx, by + 1, bz)
            head_ok, head_err, head_ores, head_def = self._dig_step_cells(
                [cur_head],
                collect_rock=collect_rock,
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= 1,
            )
            deferred_added += head_def
            mined_ores.extend(head_ores)
            if not head_ok:
                stop_reason = head_err or "dig_failed"
                break

            support_pt = (nx, by - 2, nz)
            try:
                support_blk = self._get_block_name(support_pt, cache)
                support_ok = self._is_solid_floor(support_pt, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            down_targets = [(nx, by + 1, nz), (nx, by, nz), (nx, by - 1, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(down_targets, cache, forward_delta=(dx, dz))
            if hazard is not None or (support_blk == "bedrock" and by - 1 > target_y) or support_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                breach_pts = [(nx, by, nz), (nx, by + 1, nz)]
                sealed = self._seal_breach(breach_pts, cache)
                breaches_sealed += len(sealed)
                stop_reason = hazard[0] if hazard is not None else ("fluid_or_bedrock" if support_blk in FLUID_BLOCKS | {"bedrock"} else "cave_or_drop_ahead")
                break

            nh = st.get("nearest_hostile")
            hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 9.0
            need_bridge = False
            if not support_ok:
                is_self_cav = self._is_self_cavity([support_pt, *down_targets])
                try:
                    below_sup_solid = self._is_solid_floor((nx, by - 3, nz), cache)
                except ApiError:
                    below_sup_solid = False
                can_bridge = is_self_cav or (below_sup_solid and not hostile_near)
                if not can_bridge:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "cave_or_drop_ahead"
                    break
                need_bridge = True

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "fluid_or_bedrock"
                    break
                pre_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(pre_sealed)

            try:
                pre_names = [self._get_block_name(p, cache) for p in down_targets]
            except ApiError:
                stop_reason = "unknown_block"
                break
            if (
                pre_names[1] in PASSABLE_BLOCKS
                and pre_names[2] in PASSABLE_BLOCKS
                and hostile_near
                and not self._is_self_cavity(down_targets[1:])
            ):
                sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                breaches_sealed += len(sealed)
                stop_reason = "hostile_in_cave_ahead"
                break

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                down_targets,
                collect_rock=(collect_rock or need_bridge or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= 1 + len(down_targets),
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break

            if adj_fluids:
                post_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(post_sealed)
                flooded = False
                for dp in down_targets:
                    cache.pop(dp, None)
                    if self._get_block_name(dp, cache) in FLUID_BLOCKS:
                        plug = self._seal_breach([dp], cache)
                        breaches_sealed += len(plug)
                        flooded = True
                if flooded:
                    stop_reason = "fluid_or_bedrock"
                    break

            if need_bridge:
                if not self._place_bridge_block(support_pt, cache) or not self._is_solid_floor(support_pt, cache):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by - 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "cave_or_drop_ahead"
                    break

            step_pt = (nx, by - 1, nz)
            move_failure = self._move_excavated_step(
                step_pt, from_cell=cur, head_cells=down_targets[:2], cache=cache,
                deadline=deadline, min_hp=min_hp_ref[0],
            )
            if move_failure is not None:
                stop_reason = move_failure
                break
            steps_done += 1
            if steps_done % 2 == 0 or step_pt[1] <= target_y:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if steps_done % 2 == 0 or step_pt[1] <= target_y:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                    min_hp_ref=min_hp_ref,
                    quota_met=quota_met,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }


    def _dig_stair_up_edge(
        self,
        *,
        direction: str,
        max_steps: int,
        target_y: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Dig safe rock-enclosed ascending staircase toward higher radar ore clusters ('radar' mode)."""
        dx, dz = DIRECTIONS[direction]
        cache: dict[tuple[int, int, int], str] = {}
        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints: list[tuple[int, int, int]] = [start_anchor]
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            bx, by, bz = _floor_pos(st["pos"])
            if by >= target_y:
                stop_reason = "reached_target_y"
                break

            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if self._should_recraft_burner_pickaxe(chosen_rock, total_rem, in_deepslate=(by < 0)) and self._try_recraft_burner_pickaxe(
                counts_now, at_durability_redline=total_rem <= redline,
            ):
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
                chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_stair_up") is not None:
                stop_reason = "geofence_blocked"
                break

            up_cells = [(bx, by + 2, bz), (nx, by + 2, nz), (nx, by + 1, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(up_cells, cache, forward_delta=(dx, dz))
            step_floor = (nx, by, nz)
            try:
                fl_blk = self._get_block_name(step_floor, cache)
                fl_ok = self._is_solid_floor(step_floor, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            if hazard is not None or fl_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                breaches_sealed += len(self._seal_breach([(nx, by + 1, nz), (nx, by + 2, nz)], cache))
                stop_reason = hazard[0] if hazard is not None else "fluid_or_bedrock"
                break

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    stop_reason = "fluid_or_bedrock"
                    break
                breaches_sealed += len(self._seal_breach(adj_fluids, cache))

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                up_cells,
                collect_rock=(collect_rock or not fl_ok or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= len(up_cells),
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break
            if adj_fluids:
                breaches_sealed += len(self._seal_breach(adj_fluids, cache))
            if not fl_ok and (not self._place_bridge_block(step_floor, cache) or not self._is_solid_floor(step_floor, cache)):
                stop_reason = "cave_or_drop_ahead"
                break

            step_pt = (nx, by + 1, nz)
            move_failure = self._move_excavated_step(
                step_pt, from_cell=(bx, by, bz), head_cells=[(nx, by + 2, nz), (nx, by + 1, nz)], cache=cache,
                deadline=deadline, min_hp=min_hp_ref[0],
            )
            if move_failure is not None:
                stop_reason = move_failure
                break
            steps_done += 1
            if self._budget_session is not None:
                self._budget_session.confirmed_ascent_levels.add(step_pt[1])
            if steps_done % 2 == 0 or step_pt[1] >= target_y:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if steps_done % 2 == 0 or step_pt[1] >= target_y:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                    min_hp_ref=min_hp_ref,
                    quota_met=quota_met,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }


    def _dig_level_edge(
        self,
        *,
        direction: str,
        steps: int,
        target_blocks: tuple[str, ...],
        parent_node_id: int,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        min_target_tier: int = 1,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        dx, dz = DIRECTIONS[direction]
        cache: dict[tuple[int, int, int], str] = {}
        st0 = self.bot.status()
        start_anchor = _floor_pos(st0["pos"])
        self._record_corridor(start_anchor)
        waypoints: list[tuple[int, int, int]] = [start_anchor]
        steps_done = 0
        stop_reason = "completed"
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        mined_ores: list[list[int]] = []

        for _ in range(steps):
            if time.monotonic() >= deadline:
                stop_reason = "time_slice"
                break
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                stop_reason = "not_ready"
                break
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                stop_reason = "hurt"
                break
            if isinstance(hp_now, (int, float)):
                min_hp_ref[0] = float(hp_now)

            cur = _floor_pos(st["pos"])
            bx, by, bz = cur
            inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_raw) < 3:
                inv_raw, _ = self.skills._ensure_free_slots(min_free=6, inv_snapshot=inv_raw)
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
            chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            redline = self._pickaxe_redline_threshold(by, counts_now)
            if self._should_recraft_burner_pickaxe(chosen_rock, total_rem, in_deepslate=(by < 0)) and self._try_recraft_burner_pickaxe(
                counts_now, at_durability_redline=total_rem <= redline,
            ):
                inv_raw, counts_now = _inventory_snapshot_and_counts(self.bot)
                chosen_rock, _, _, total_rem = self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_raw)
            if chosen_rock is None or total_rem <= redline:
                stop_reason = "pickaxe_low"
                break
            collect_rock = _seal_reserve(counts_now) < 32

            nx, nz = bx + dx, bz + dz
            if self._geofence_blocked(geofence, nx, nz, action="mine_dfs_tunnel") is not None:
                stop_reason = "geofence_blocked"
                break

            floor_pt = (nx, by - 1, nz)
            try:
                floor_blk = self._get_block_name(floor_pt, cache)
                floor_ok = self._is_solid_floor(floor_pt, cache)
            except ApiError:
                stop_reason = "unknown_block"
                break
            faces = [(nx, by + 1, nz), (nx, by, nz)]
            hazard, adj_fluids = self._inspect_step_hazards(faces, cache, forward_delta=(dx, dz))
            if hazard is not None or floor_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS:
                sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                breaches_sealed += len(sealed)
                stop_reason = hazard[0] if hazard is not None else "fluid_or_bedrock"
                break

            nh = st.get("nearest_hostile")
            hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 9.0
            need_bridge = False
            if not floor_ok:
                is_self_cav = self._is_self_cavity([floor_pt, *faces])
                try:
                    below_fl_solid = self._is_solid_floor((nx, by - 2, nz), cache)
                except ApiError:
                    below_fl_solid = False
                can_bridge = is_self_cav or (below_fl_solid and not hostile_near)
                if not can_bridge:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "unsafe_floor_or_cave"
                    break
                need_bridge = True

            if adj_fluids:
                if _seal_reserve(counts_now) == 0 and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "fluid_or_bedrock"
                    break
                pre_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(pre_sealed)

            try:
                face_names = [self._get_block_name(p, cache) for p in faces]
            except ApiError:
                stop_reason = "unknown_block"
                break
            if face_names[0] in PASSABLE_BLOCKS and face_names[1] in PASSABLE_BLOCKS:
                far_floor = (nx + dx, by - 1, nz + dz)
                is_known_intersection = self._is_self_cavity([*faces, far_floor])
                if hostile_near and not is_known_intersection:
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "hostile_in_cave_ahead"
                    break
                if not is_known_intersection:
                    try:
                        if not self._is_solid_floor(far_floor, cache):
                            sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                            breaches_sealed += len(sealed)
                            stop_reason = "cave_breach_sealed"
                            break
                    except ApiError:
                        pass

            dig_ok, dig_err, step_ores, step_def = self._dig_step_cells(
                faces,
                collect_rock=(collect_rock or need_bridge or bool(adj_fluids)),
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=int(chosen_rock["remaining"]) <= len(faces),
            )
            deferred_added += step_def
            mined_ores.extend(step_ores)
            if not dig_ok:
                stop_reason = dig_err or "dig_failed"
                break

            if adj_fluids:
                post_sealed = self._seal_breach(adj_fluids, cache)
                breaches_sealed += len(post_sealed)
                flooded = False
                for fp in faces:
                    cache.pop(fp, None)
                    if self._get_block_name(fp, cache) in FLUID_BLOCKS:
                        plug = self._seal_breach([fp], cache)
                        breaches_sealed += len(plug)
                        flooded = True
                if flooded:
                    stop_reason = "fluid_or_bedrock"
                    break

            if need_bridge:
                if not self._place_bridge_block(floor_pt, cache) or not self._is_solid_floor(floor_pt, cache):
                    sealed = self._seal_breach([(nx, by, nz), (nx, by + 1, nz)], cache)
                    breaches_sealed += len(sealed)
                    stop_reason = "unsafe_floor_or_cave"
                    break

            step_pt = (nx, by, nz)
            move_failure = self._move_excavated_step(
                step_pt, from_cell=cur, head_cells=faces, cache=cache,
                deadline=deadline, min_hp=min_hp_ref[0],
            )
            if move_failure is not None:
                stop_reason = move_failure
                break
            steps_done += 1
            if steps_done % 3 == 0:
                waypoints.append(step_pt)

            if step_ores and quota_met is not None and quota_met():
                stop_reason = "quota_reached"
                break

            if target_blocks:
                hook = self._run_vein_hook(
                    step_pt,
                    target_blocks=target_blocks,
                    current_node_id=parent_node_id,
                    geofence=geofence,
                    cache=cache,
                    min_target_tier=min_target_tier,
                    min_hp_ref=min_hp_ref,
                    quota_met=quota_met,
                )
                mined_ores.extend(hook["mined_coords"])
                deferred_added += hook["deferred_added"]
                catch_placed += hook["catch_placed"]
                breaches_sealed += hook["breaches_sealed"]
                if hook["mined_coords"] and quota_met is not None and quota_met():
                    stop_reason = "quota_reached"
                    break

        st_end = self.bot.status()
        end_anchor = _floor_pos(st_end["pos"])
        self._record_corridor(end_anchor)
        if waypoints[-1] != end_anchor:
            waypoints.append(end_anchor)
        return {
            "steps_done": steps_done,
            "stop_reason": stop_reason,
            "start_anchor": start_anchor,
            "end_anchor": end_anchor,
            "waypoints": waypoints,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "mined_ores": mined_ores,
        }


    def _create_node(
        self,
        *,
        kind: str,
        anchor: tuple[int, int, int],
        parent_id: int | None,
        edge_from_parent: ShaftEdge | None,
        branches: list[BranchSpec],
    ) -> ShaftNode:
        nid = self.next_node_id
        self.next_node_id += 1
        node = ShaftNode(
            node_id=nid,
            kind=kind,
            anchor=anchor,
            parent_id=parent_id,
            edge_from_parent=edge_from_parent,
            branches=branches,
        )
        self.nodes[nid] = node
        self.active_node_id = nid
        self._record_corridor(anchor)
        for rec in self.deferred_ores.values():
            old_node = self.nodes.get(rec.nearest_node_id)
            if old_node is None or math.dist(rec.pos, anchor) < math.dist(rec.pos, old_node.anchor):
                rec.nearest_node_id = nid
        return node


    def _effective_collar_target_y(self, anchor: tuple[int, int, int]) -> int:
        if self.radar_enabled:
            rt = self._best_radar_target_for_anchor(anchor, at_spine=False)
            if rt is not None and rt[1] < anchor[1] - 2:
                return int(rt[1])
        return self.target_y


    def _collar_starts_at_level(self, anchor: tuple[int, int, int], effective_y: int) -> bool:
        radar_target = self._best_radar_target_for_anchor(anchor, at_spine=True) if self.radar_enabled else None
        return anchor[1] <= effective_y and (radar_target is None or radar_target[1] >= anchor[1] - 2)


    def _collar_branch_plan(
        self,
        anchor: tuple[int, int, int],
        cands: list[str],
        *,
        geofence: Any,
    ) -> tuple[str, list[BranchSpec]]:
        eff_y = self._effective_collar_target_y(anchor)

        # If Collar is initialized directly AT or BELOW target depth (unless radar sees an overhead cluster),
        # attach Spine branches directly so we never execute 0-step stair_down transitions!
        if self._collar_starts_at_level(anchor, eff_y):
            sp_branches = [
                branch for branch in self._spine_branches(cands[0], anchor, geofence)
                if branch.kind == "stair_up" or branch.direction in cands
            ]
            existing_dirs = {b.direction for b in sp_branches if b.kind in {"spine_forward", "branch_leaf"}}
            for extra_d in cands:
                if extra_d not in existing_dirs:
                    sp_branches.append(BranchSpec(kind="branch_leaf", direction=extra_d, steps=8))
            return "spine", sp_branches

        branches: list[BranchSpec] = []
        depth = max(4, anchor[1] - eff_y)
        for d in cands:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_collar") is not None:
                continue
            branches.append(BranchSpec(kind="stair_down", direction=d, steps=depth, branch_target_y=eff_y))
        if not branches:
            branches.append(BranchSpec(kind="stair_down", direction="north", steps=depth, branch_target_y=eff_y))
        return "collar", branches


    def _init_collar(
        self,
        anchor: tuple[int, int, int],
        *,
        target_y: int,
        geofence: Any,
        viable_dirs: list[str] | None = None,
    ) -> ShaftNode:
        cands = viable_dirs or self._probe_collar_dirs(anchor, geofence=geofence)
        kind, branches = self._collar_branch_plan(anchor, cands or ["north", "east", "south", "west"], geofence=geofence)
        node = self._create_node(kind=kind, anchor=anchor, parent_id=None, edge_from_parent=None, branches=branches)
        self.collar_node_id = node.node_id
        self.fluid_aborts = 0
        if kind == "collar":
            self.dry_calls = 0
        self.fluid_abandoned_deep = False
        if self.collar_ceiling_y is None or anchor[1] > self.collar_ceiling_y:
            self.collar_ceiling_y = int(anchor[1])
        return node


    def _has_stale_descent(self, target_y: int, *, observed_layers: set[int] | None = None) -> bool:
        return any(
            branch.status in {"pending", "active"}
            and (branch.kind == "stair_down" or branch.kind == "offset_retry" and branch.resume_kind == "stair_down")
            and branch.branch_target_y is not None
            and target_y - branch.branch_target_y >= LAYER_SWITCH_MIN_DELTA
            and (observed_layers is None or branch.branch_target_y not in observed_layers)
            for node in self.nodes.values()
            for branch in node.branches
        )


    def _replan_shallower_layer(
        self, stance: dict[str, Any], pos: tuple[int, int, int], *,
        target_y: int, geofence: Any, target_blocks: tuple[str, ...] = (), max_tier: int = 0,
    ) -> bool:
        if (
            pos[1] < target_y - SCAN_BAND or stance.get("on_ground") is not True
            or not stance.get("pos") or stance.get("in_water") or stance.get("in_lava")
            or stance.get("alive") is False or stance.get("ready") is False
        ):
            return False
        try:
            if not self._level_walk_is_clear(pos, pos):
                return False
        except ApiError:
            return False

        node = next((node for node in self.nodes.values() if node.anchor == pos), None)
        parent_id = None
        incoming_edge = node.edge_from_parent if node is not None else None
        if node is None:
            for child in self.nodes.values():
                edge, parent = child.edge_from_parent, self.nodes.get(child.parent_id)
                if edge is None or parent is None:
                    continue
                route = self._route_at_position(edge.waypoints, pos, edge.edge_kind)
                if route is not None and route[0][0] == parent.anchor:
                    waypoints, index = route
                    parent_id = parent.node_id
                    incoming_edge = ShaftEdge(edge.edge_kind, edge.direction, waypoints[:index + 1])
                    break

        previous_y = self.target_y
        try:
            self.target_y = target_y
            if target_blocks:
                self._scan_ore_radar(pos, target_blocks=target_blocks, max_tier=max_tier, geofence=geofence, force=True)
            candidates = tuple(d for d in DIRECTIONS if not self._cuts_incoming_stair(pos, incoming_edge, d))
            directions = self._probe_collar_dirs(pos, geofence=geofence, dry_only=True, directions=candidates)
            if not directions:
                return False
            kind, branches = self._collar_branch_plan(pos, directions, geofence=geofence)
        finally:
            self.target_y = previous_y

        if node is None and incoming_edge is not None:
            node = self._create_node(
                kind=kind, anchor=pos, parent_id=parent_id, edge_from_parent=incoming_edge, branches=[],
            )
        if node is None:
            self._archive_current_shaft_to_ledger(mark_exhausted=False)
            self._clear_active_tree_only()
            self.target_y = target_y
            self._init_collar(pos, target_y=target_y, geofence=geofence, viable_dirs=directions)
        else:
            # The checked waypoints remain available for return. Only unfinished work belongs to the old target.
            for saved_node in self.nodes.values():
                saved_node.branches = [branch for branch in saved_node.branches if branch.status == "completed"]
            node.kind = "switchback" if kind == "collar" and node.parent_id is not None else kind
            node.branches.extend(branches)
            self.active_node_id = node.node_id
            self.target_y = target_y
            self.dry_calls = 0
        return True


    def _spine_branches(self, forward_dir: str, anchor: tuple[int, int, int], geofence: Any) -> list[BranchSpec]:
        left_d, right_d = LEFT_RIGHT_DIRS[forward_dir]
        raw_specs: list[tuple[str, str, int]] = [
            ("branch_leaf", left_d, 8),
            ("branch_leaf", right_d, 8),
            ("spine_forward", forward_dir, 4),
        ]

        if not self.radar_enabled:
            out_fb: list[BranchSpec] = []
            # Fishbone has no vertical branch by design (one shaft = one layer). If the
            # caller switched to a deeper target_y mid-shaft, schedule a stair_down first
            # so the tree keeps digging down instead of tunneling sideways forever.
            drop = anchor[1] - self.target_y
            if drop > SPINE_DESCEND_MIN_DELTA:
                for d in (forward_dir, left_d, right_d):
                    dx, dz = DIRECTIONS[d]
                    if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_spine_drop") is not None:
                        continue
                    out_fb.append(
                        BranchSpec(
                            kind="stair_down",
                            direction=d,
                            steps=min(16, max(3, drop)),
                            branch_target_y=self.target_y,
                        )
                    )
                    break
            else:
                # Symmetric case: the shaft overshot below target_y (e.g. diamond layer -> iron layer).
                # Without a stair_up the skeleton would stay buried and never reach the requested layer.
                rise = self.target_y - anchor[1]
                if rise > SPINE_DESCEND_MIN_DELTA:
                    for d in (forward_dir, left_d, right_d):
                        dx, dz = DIRECTIONS[d]
                        if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_spine_climb") is not None:
                            continue
                        out_fb.append(
                            BranchSpec(
                                kind="stair_up",
                                direction=d,
                                steps=min(16, max(3, rise)),
                                branch_target_y=self.target_y,
                            )
                        )
                        break
            for kind, d, ln in raw_specs:
                dx, dz = DIRECTIONS[d]
                if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                    continue
                out_fb.append(BranchSpec(kind=kind, direction=d, steps=ln))
            return out_fb

        rt = self._best_radar_target_for_anchor(anchor, at_spine=True)
        if not self.surgical_enabled:
            # 'v5' mode: bug-fixed original v4 Spine steering (fixed 8/4 step lengths, stair_down only, no stair_up)
            extra_descent: BranchSpec | None = None
            if rt is not None:
                projs_v5 = {d: self._radar_direction_projection(anchor, d, at_spine=True) for d in (left_d, right_d, forward_dir)}
                best_d_v5, best_proj_v5 = max(projs_v5.items(), key=lambda kv: kv[1])
                if best_proj_v5 > 2.0:
                    if best_d_v5 != forward_dir and best_proj_v5 > 7.0:
                        other_lat_v5 = right_d if best_d_v5 == left_d else left_d
                        steer_steps_v5 = min(8, max(4, int(best_proj_v5)))
                        raw_specs = [
                            ("spine_forward", best_d_v5, steer_steps_v5),
                            ("branch_leaf", forward_dir, 6),
                            ("branch_leaf", other_lat_v5, 8),
                        ]
                    else:
                        raw_specs.sort(key=lambda item: (-projs_v5.get(item[1], 0.0)))
                    if rt[1] < anchor[1] - 4:
                        dx_v5, dz_v5 = DIRECTIONS[best_d_v5]
                        if self._geofence_blocked(geofence, anchor[0] + dx_v5 * 3, anchor[2] + dz_v5 * 3, action="mine_dfs_spine_drop") is None:
                            extra_descent = BranchSpec(
                                kind="stair_down",
                                direction=best_d_v5,
                                steps=min(16, max(3, anchor[1] - rt[1])),
                                branch_target_y=int(rt[1]),
                            )
            out_v5: list[BranchSpec] = []
            if extra_descent is not None:
                out_v5.append(extra_descent)
            for kind, d, ln in raw_specs:
                dx, dz = DIRECTIONS[d]
                if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                    continue
                out_v5.append(BranchSpec(kind=kind, direction=d, steps=ln))
            return out_v5

        # 'radar' mode: 3D pinpoint alignment (exact horizontal step clamping + vertical stair_down / stair_up)
        vertical_branch: BranchSpec | None = None
        defer_ascent = False
        if rt is not None:
            projs = {d: self._radar_direction_projection(anchor, d, at_spine=True) for d in (left_d, right_d, forward_dir)}
            best_d, best_proj = max(projs.items(), key=lambda kv: kv[1])
            vy = rt[1] - anchor[1]

            if best_proj >= 1.0:
                align_steps = min(8, max(2, int(round(best_proj))))
                if best_d != forward_dir and best_proj >= 3.0:
                    other_lat = right_d if best_d == left_d else left_d
                    raw_specs = [
                        ("spine_forward", best_d, align_steps),
                        ("branch_leaf", forward_dir, 6),
                        ("branch_leaf", other_lat, 8),
                    ]
                else:
                    adjusted: list[tuple[str, str, int]] = []
                    for kind, d, ln in raw_specs:
                        if d == best_d:
                            adjusted.append(("spine_forward" if kind == "spine_forward" else kind, d, align_steps))
                        else:
                            adjusted.append((kind, d, ln))
                    adjusted.sort(key=lambda item: (-projs.get(item[1], 0.0)))
                    raw_specs = adjusted

            stair_dir = best_d if best_proj > 0 else forward_dir
            dx_r, dz_r = DIRECTIONS[stair_dir]
            if vy < -3 and self._geofence_blocked(geofence, anchor[0] + dx_r * 3, anchor[2] + dz_r * 3, action="mine_dfs_spine_drop") is None:
                vertical_branch = BranchSpec(
                    kind="stair_down",
                    direction=stair_dir,
                    steps=min(16, max(2, anchor[1] - rt[1])),
                    branch_target_y=int(rt[1]),
                )
            elif vy > 3 and self._geofence_blocked(geofence, anchor[0] + dx_r * 3, anchor[2] + dz_r * 3, action="mine_dfs_spine_climb") is None:
                # Stay at the requested depth while closing the horizontal gap to a high, distant cluster.
                defer_ascent = (
                    abs(anchor[1] - self.target_y) <= SCAN_BAND
                    and rt[1] > self.target_y + DEPTH_RESET_THRESHOLD
                    and vy > DEPTH_RESET_THRESHOLD
                    and math.hypot(rt[0] - anchor[0], rt[2] - anchor[2]) > VEIN_HOOK_RADAR_RADIUS
                )
                vertical_branch = BranchSpec(
                    kind="stair_up",
                    direction=stair_dir,
                    steps=min(14, max(2, rt[1] - anchor[1])),
                    branch_target_y=int(rt[1]),
                )

        out: list[BranchSpec] = []
        for kind, d, ln in raw_specs:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * ln, anchor[2] + dz * ln, action="mine_dfs_spine") is not None:
                continue
            out.append(BranchSpec(kind=kind, direction=d, steps=ln))
        if vertical_branch is not None:
            out.insert(len(out) if defer_ascent else 0, vertical_branch)
        return out


    def _switchback_branches(self, prev_dir: str, cur_y: int, target_y: int, anchor: tuple[int, int, int], geofence: Any) -> list[BranchSpec]:
        left_d, right_d = LEFT_RIGHT_DIRS[prev_dir]
        cand_dirs = [left_d, right_d]
        if self.radar_enabled:
            cand_dirs.sort(key=lambda d: -self._radar_direction_projection(anchor, d, at_spine=False))
        depth = max(3, cur_y - target_y)
        out: list[BranchSpec] = []
        for d in cand_dirs:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_switchback") is not None:
                continue
            out.append(BranchSpec(kind="stair_down", direction=d, steps=depth, branch_target_y=target_y))
        return out


    @staticmethod
    def _cuts_incoming_stair(anchor: tuple[int, int, int], edge: ShaftEdge | None, direction: str) -> bool:
        if edge is None or edge.edge_kind != "stair_down":
            return False
        x, y, z = anchor
        dx, dz = DIRECTIONS[direction]
        # The offset cuts two blocks high. Either block can be the floor of
        # the return stair, including a step omitted from the waypoint list.
        for step in range(1, OFFSET_RETRY_STEPS + 1):
            for rise in (1, 2):
                stair_cell = (x + dx * step, y + rise, z + dz * step)
                if stair_cell in edge.waypoints or _omitted_waypoint_index(
                    edge.waypoints, stair_cell, edge.edge_kind,
                ) is not None:
                    return True
        return False


    def _enqueue_offset_retry_if_eligible(
        self,
        node: ShaftNode,
        failed_branch: BranchSpec,
        prune_reason: str,
        geofence: Any,
        *,
        after_exhaustion: bool = False,
    ) -> bool:
        if failed_branch.offset_tried or failed_branch.kind != "stair_down" or node.kind == "spine":
            return False
        if prune_reason not in {
            "cave_or_drop_ahead",
            "unsafe_floor_or_cave",
            "cave_breach_sealed",
            "fluid_or_bedrock",
            "falling_material_adjacent",
        } and not (after_exhaustion and prune_reason == "movement_not_confirmed"):
            return False

        left_d, right_d = LEFT_RIGHT_DIRS[failed_branch.direction]
        for lat_d in (left_d, right_d):
            if self._cuts_incoming_stair(node.anchor, node.edge_from_parent, lat_d):
                continue
            lx, lz = DIRECTIONS[lat_d]
            if self._geofence_blocked(
                geofence, node.anchor[0] + lx * OFFSET_RETRY_STEPS,
                node.anchor[2] + lz * OFFSET_RETRY_STEPS, action="mine_dfs_offset",
            ) is not None:
                continue
            already = any(
                b.kind == "offset_retry" and b.direction == failed_branch.direction and b.sidestep_dir == lat_d
                for b in node.branches
            )
            if already:
                continue
            node.branches.append(
                BranchSpec(
                    kind="offset_retry",
                    direction=failed_branch.direction,
                    steps=failed_branch.steps,
                    offset_tried=True,
                    sidestep_dir=lat_d,
                    resume_kind=failed_branch.kind,
                    branch_target_y=failed_branch.branch_target_y,
                )
            )
            return True
        return False


    def _maybe_place_torch_at_node(self, node: ShaftNode) -> bool:
        if node.torch_placed:
            return False
        counts = _inventory_counts(self.bot)
        if counts.get("torch", 0) <= 0:
            return False
        tpos = [node.anchor[0], node.anchor[1] + 1, node.anchor[2]]
        try:
            self.bot.call("place", {"item": "torch", "pos": tpos})
            node.torch_placed = True
            return True
        except ApiError:
            return False


    def _maybe_return_collar_for_resupply(
        self,
        counts_now: dict[str, int],
        *,
        min_hp_ref: list[float],
    ) -> bool:
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            return False
        _, _, sticks = _stone_pickaxe_headroom(counts_now)
        if sticks < 2 or counts_now.get("crafting_table", 0) >= 1:
            return False
        total_planks = sum(v for k, v in counts_now.items() if k.endswith("_planks"))
        total_logs = sum(v for k, v in counts_now.items() if k.endswith(("_log", "_stem")))
        if total_planks >= 4 or total_logs >= 1:
            return False
        if not _has_collar_resupply_materials(counts_now) and self.ledger.nearest_utility("crafting_table", self.nodes[self.collar_node_id].anchor, max_dist=16.0) is None:
            return False
        try:
            ok_ret, reason = self._walk_to_collar(self.collar_node_id, min_hp_ref=min_hp_ref)
        except _MiningDeadline as exc:
            raise _CollarResupplyDeadline from exc
        if reason == "walk_time_slice":
            raise _CollarResupplyDeadline
        return ok_ret


    def _pickaxe_low_recovery(self, counts: dict[str, int], pos: list[float] | None) -> dict[str, Any]:
        _, _, sticks = _stone_pickaxe_headroom(counts)
        if (
            sticks < 2 and pos and self.collar_ceiling_y is not None
            and _floor_pos(pos)[1] <= min(19, self.collar_ceiling_y - 20)
        ):
            climb_target_y = self.collar_ceiling_y - 16
            return {
                "next_action": "climb_out",
                "message": (
                    "当前井下缺少合成新镐的木料。先调用 action='climb_out' 沿已有巷道回撤，"
                    f"本次爬升目标为 y>={climb_target_y}。若收到 climb_progress，可从当前位置继续回撤。"
                    "回到能采木的位置后用 mc_skill_harvest_logs(target_logs=1) 补原木或菌柄，合成可用镐后继续。"
                ),
            }
        return {"message": _pickaxe_low_hint(counts)}


    def _confirmed_straight_descent(
        self, anchor: tuple[int, int, int], direction: str, observations: list[tuple[int, int, int]],
    ) -> list[tuple[int, int, int]]:
        dx, dz = DIRECTIONS[direction]
        route = [anchor]
        for point in observations:
            x, y, z = route[-1]
            if point == (x + dx, y - 1, z + dz) and point in self.known_corridor_positions:
                route.append(point)
        return route


    def _preserve_interrupted_branch(
        self, scoped: _BudgetSession, *, deadline_failure: _MiningDeadline | None,
    ) -> bool:
        node = self.nodes.get(self.active_node_id)
        if node is None:
            return False
        for index, branch in enumerate(node.branches):
            if branch.status == "active":
                break
        else:
            return False

        branch.status = "pending"
        if isinstance(deadline_failure, _CollarResupplyDeadline):
            return True
        if scoped.timeout_path == "dig" and not scoped.dug_count:
            # Give the other branches a first step before retrying an unconfirmed dig.
            node.branches.append(node.branches.pop(index))
        route = scoped.positions
        if node.anchor in route:
            route = route[len(route) - 1 - list(reversed(route)).index(node.anchor):]
            steps_taken = len(route) - 1
        elif (
            branch.kind == "stair_down" and len(route) > 1
            and route[0] in self.known_corridor_positions
            and _descent_start_beside_anchor(route[0], node.anchor, branch.direction)
            and route[-1][1] < node.anchor[1]
            and not (scoped.last_status.get("in_water") or scoped.last_status.get("in_lava"))
            and all(math.dist(a, b) <= math.sqrt(2) and b[1] <= a[1] for a, b in zip(route, route[1:]))
        ):
            # Connect a descent that began one cell away from the saved node.
            steps_taken = node.anchor[1] - route[-1][1]
            route = [node.anchor, *route]
        elif branch.kind == "stair_down" and len(route) > 1:
            # The previous slice can finish two confirmed steps below its saved node.
            dx, dz = DIRECTIONS[branch.direction]
            x, y, z = node.anchor
            skipped_step = (x + dx, y - 1, z + dz)
            skipped_head = (x + dx, y, z + dz)
            status = scoped.last_status
            if (
                route[0] != (x + 2 * dx, y - 2, z + 2 * dz)
                or skipped_head not in self.known_corridor_positions
                or status.get("on_ground") is not True
                or status.get("in_water") or status.get("in_lava")
                or status.get("alive") is False or status.get("ready") is False
                or not status.get("pos") or _floor_pos(status["pos"]) != route[-1]
            ):
                return True
            connected = self._confirmed_straight_descent(node.anchor, branch.direction, [skipped_step, *route])
            if connected[-1] != route[-1]:
                return True
            route = connected
            steps_taken = node.anchor[1] - route[-1][1]
        else:
            return True
        if branch.kind == "stair_down":
            verified_route = _descending_waypoint_prefix(route, node.anchor[1])
            if len(verified_route) < len(route):
                if not verified_route:
                    return True
                status = scoped.last_status
                if deadline_failure is not None and status.get("on_ground") and not (
                    status.get("in_water") or status.get("in_lava")
                    or status.get("alive") is False or status.get("ready") is False
                ):
                    # A pickup or a jump can briefly take the bot off its new stair.
                    # Only skip those observations if every step to the final stance is known.
                    connected = self._confirmed_straight_descent(node.anchor, branch.direction, route[1:])
                    if connected[-1] == route[-1] and connected[-1][1] < verified_route[-1][1]:
                        verified_route = connected
                route = verified_route
                steps_taken = node.anchor[1] - route[-1][1]
        elif branch.kind != "stair_up" and any(pt[1] != node.anchor[1] for pt in route):
            return True
        if steps_taken <= 0 or route[-1] == node.anchor:
            return True
        if deadline_failure is None:
            # External interruption can occur during movement or resupply.
            # Only promote the contiguous, confirmed part of a descending branch.
            status = scoped.last_status
            if (
                branch.kind != "stair_down" or route[-1][1] >= node.anchor[1]
                or status.get("in_water") or status.get("in_lava")
                or status.get("alive") is False or status.get("ready") is False
                or any(pt not in self.known_corridor_positions for pt in route[1:])
                or any(math.dist(a, b) > math.sqrt(2) or b[1] > a[1] for a, b in zip(route, route[1:]))
            ):
                return True
            steps_taken = node.anchor[1] - route[-1][1]
        self._record_partial_branch(node, branch, route, steps_taken)
        return True


    def _record_partial_branch(
        self, node: ShaftNode, branch: BranchSpec, route: list[tuple[int, int, int]], steps_taken: int,
    ) -> None:
        if branch.kind == "offset_retry":
            direction = branch.sidestep_dir or LEFT_RIGHT_DIRS[branch.direction][0]
            dx, dz = DIRECTIONS[direction]
            x, y, z = node.anchor
            if 2 <= steps_taken <= OFFSET_RETRY_STEPS and all(
                pt == (x + step * dx, y, z + step * dz) for step, pt in enumerate(route)
            ):
                self._record_offset_branch(node, branch, route, direction)
            return
        branch.status = "completed"
        self._create_node(
            kind="switchback" if branch.kind.startswith("stair") else "spine",
            anchor=route[-1], parent_id=node.node_id,
            edge_from_parent=ShaftEdge(branch.kind, branch.direction, list(route)),
            branches=[dataclasses.replace(
                branch, status="pending", completed_waypoints=[],
                steps=max(1, branch.steps - steps_taken),
            )],
        )


    def _record_offset_branch(
        self, node: ShaftNode, branch: BranchSpec, route: list[tuple[int, int, int]], direction: str,
    ) -> ShaftNode:
        resume_kind = branch.resume_kind or "stair_down"
        branch.status = "completed"
        return self._create_node(
            kind="spine" if resume_kind == "spine_forward" else "switchback",
            anchor=route[-1], parent_id=node.node_id,
            edge_from_parent=ShaftEdge("offset_sidestep", direction, list(route)),
            branches=[BranchSpec(
                kind=resume_kind, direction=branch.direction, steps=branch.steps,
                offset_tried=True, branch_target_y=branch.branch_target_y,
            )],
        )

