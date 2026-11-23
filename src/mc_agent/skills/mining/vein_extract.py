"""MiningDFSEngine mixin: vein_extract (split from legacy mining_dfs.py)."""

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


class _VeinExtractMixin:
    def _dig_vein_cell(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        anchor: tuple[int, int, int],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
        replaces_footing: bool = False,
    ) -> tuple[bool, int, int]:
        """Break the ore with fluid and cave protection, then verify the drop."""
        catch_placed = 0
        breaches_sealed = 0
        hazard, adj_fluids = self._inspect_step_hazards([pt], cache)
        if hazard is not None:
            self._mark_ore_unreachable(pt, hazard[0])
            return False, 0, 0
        if adj_fluids:
            sealed_f = self._seal_breach(adj_fluids, cache)
            breaches_sealed += len(sealed_f)
            if len(sealed_f) < len(adj_fluids) and any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids):
                self._mark_ore_unreachable(pt, "adjacent_lava_unsealable")
                return False, catch_placed, breaches_sealed

        # Inspect outer cave-air faces behind pt (away from anchor) BEFORE breaking pt
        outer_cave_faces: list[tuple[int, int, int]] = []
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
            nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
            if abs(nb[0] - anchor[0]) <= 1 and abs(nb[2] - anchor[2]) <= 1:
                continue
            if self._is_self_cavity([nb]):
                continue
            try:
                if self._get_block_name(nb, cache) in AIR_BLOCKS:
                    outer_cave_faces.append(nb)
            except ApiError:
                pass

        below_pt = (pt[0], pt[1] - 1, pt[2])
        try:
            below_name = self._get_block_name(below_pt, cache)
        except ApiError:
            below_name = "stone"
        if below_name in PASSABLE_BLOCKS and below_pt != anchor and below_pt != (anchor[0], anchor[1] + 1, anchor[2]):
            counts = _inventory_counts(self.bot)
            filler = _pick_seal_material(counts)
            if filler:
                try:
                    self.bot.call("place", {"item": filler, "pos": [below_pt[0], below_pt[1], below_pt[2]]})
                    cache[below_pt] = filler
                    self.catch_blocks_placed.append([below_pt[0], below_pt[1], below_pt[2]])
                    catch_placed += 1
                except ApiError:
                    pass

        expected_drops = _expected_drops_for_blocks([bname])
        inv_pre, counts_pre = _inventory_snapshot_and_counts(self.bot)
        if expected_drops and _compute_empty_slots(inv_pre) < 2 and not _can_stack_expected_drop(inv_pre, expected_drops):
            inv_pre, _ = self.skills._ensure_free_slots(min_free=4, inv_snapshot=inv_pre)
            inv_pre, counts_pre = _inventory_snapshot_and_counts(self.bot)
        before_target_cnt = sum(int(counts_pre.get(it, 0)) for it in expected_drops)

        req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
        chosen, _, _, total_rem = self._equip_for_block_tier(req_t, for_ore=True, inv_snapshot=inv_pre)
        if chosen is None or total_rem < 1:
            return False, catch_placed, breaches_sealed

        # Prevent Mineflayer collectDrops -> goNear from pulling the bot into a monster cave!
        hostile_near = False
        if outer_cave_faces:
            try:
                st_h = self.bot.status()
                nh = st_h.get("nearest_hostile")
                hostile_near = isinstance(nh, dict) and float(nh.get("dist", 999.0)) <= 12.0
            except ApiError:
                hostile_near = False
        safe_auto_collect = not (replaces_footing or (outer_cave_faces and hostile_near))

        dug = False
        _scan_band_cell = self.strategy == "fishbone_scan" and abs(anchor[1] - self.target_y) <= SCAN_BAND
        max_peel = 2 if (self.surgical_enabled or _scan_band_cell) else 1
        try:
            self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": safe_auto_collect, "stop_on": ["hurt", "death"]})
            dug = True
        except ApiError as err:
            if (
                err.code == "out_of_reach"
                and bname in target_set
                and abs(pt[1] - anchor[1]) <= (3 if (self.radar_enabled or _scan_band_cell) else 2)
                and (abs(pt[0] - anchor[0]) + abs(pt[2] - anchor[2])) <= (5 if (self.surgical_enabled or _scan_band_cell) else (4 if self.radar_enabled else 3))
            ):
                peel_pts = self._find_peel_blockers(anchor, pt, cache, max_blocks=max_peel)
                if peel_pts:
                    peeled_ok = True
                    for pp in peel_pts:
                        ok_p, _, _, _ = self._dig_cell_safely(pp, collect_rock=False, parent_node_id=0, cache=cache)
                        if not ok_p:
                            peeled_ok = False
                            break
                    if peeled_ok:
                        chosen2, _, _, rem2 = self._equip_for_block_tier(req_t, for_ore=True)
                        if chosen2 is not None and rem2 >= 1:
                            try:
                                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": safe_auto_collect, "stop_on": ["hurt", "death"]})
                                dug = True
                            except ApiError:
                                pass
        if not dug:
            return False, catch_placed, breaches_sealed

        cache[pt] = "air"
        self._record_excavated(pt)
        self.mined_ore_positions.add(pt)
        self.deferred_ores.pop(pt, None)
        self.ledger.remove_ore(pt)

        if adj_fluids:
            breaches_sealed += len(self._seal_breach(adj_fluids, cache))

        # Seal the OUTER cave opening behind pt FIRST (so monsters are walled off while pt stays open to collect drop!)
        outer_all_sealed = True
        if outer_cave_faces:
            sealed_out = self._seal_breach(outer_cave_faces, cache)
            breaches_sealed += len(sealed_out)
            outer_all_sealed = len(sealed_out) >= len(outer_cave_faces)

        collected_ok = True
        if expected_drops:
            collected_ok = self._collect_and_verify_target_drop(
                expected_drops=expected_drops,
                before_count=before_target_cnt,
                ore_pt=pt,
                stand_y=anchor[1],
                cache=cache,
                can_open_lower=outer_all_sealed,
                allow_ore_step=replaces_footing,
            )

        if expected_drops and not collected_ok:
            self.lost_drops += 1

        # Only AFTER drop pickup is verified/finished, plug pt itself if outer cave could not be fully sealed
        if outer_cave_faces and not outer_all_sealed:
            breaches_sealed += len(self._seal_breach([pt], cache))

        return collected_ok, catch_placed, breaches_sealed


    def _surgical_breach_to_ore(
        self,
        anchor: tuple[int, int, int],
        pt: tuple[int, int, int],
        bname: str,
        *,
        target_set: set[str],
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
        min_hp_ref: list[float],
    ) -> tuple[bool, int, int]:
        """Backside pure-rock 1-3 step pocket approach + waist parapet + 1x1 window extraction ('radar' only)."""
        catch_total = 0
        seal_total = 0
        # Pre-check pt for unsealable lava before investing pocket steps
        hazard0, adj_fl0 = self._inspect_step_hazards([pt], cache)
        if hazard0 is not None or any(cache.get(nb) in LAVA_BLOCKS for nb in adj_fl0):
            self._mark_ore_unreachable(pt, "lava_adjacent_target")
            return False, 0, 0

        pocket_wps: list[tuple[int, int, int]] = [anchor]
        cur = anchor

        for _ in range(3):
            h_manhattan = abs(pt[0] - cur[0]) + abs(pt[2] - cur[2])
            v_diff = pt[1] - cur[1]
            if h_manhattan <= 1 and abs(v_diff) <= 2:
                break
            vx, vz = pt[0] - cur[0], pt[2] - cur[2]
            if abs(vx) >= abs(vz) and vx != 0:
                sdx, sdz = (1 if vx > 0 else -1), 0
            elif vz != 0:
                sdx, sdz = 0, (1 if vz > 0 else -1)
            else:
                break
            nx, nz = cur[0] + sdx, cur[2] + sdz
            if self._geofence_blocked(geofence, nx, nz, action="surgical_pocket") is not None:
                break

            step_dy = -1 if v_diff <= -2 else (1 if v_diff >= 2 else 0)
            ny = cur[1] + step_dy
            if (nx, ny, nz) == pt or (nx, ny + 1, nz) == pt:
                break

            # STRICT RULE: Never step into natural cave_air! Stop inside solid rock backside!
            check_cells = [(nx, ny, nz), (nx, ny + 1, nz)]
            if step_dy == -1:
                check_cells.append((nx, cur[1] + 1, nz))
            elif step_dy == 1:
                check_cells.append((cur[0], cur[1] + 2, cur[2]))
            try:
                enters_natural_cave = any(
                    self._get_block_name(c, cache) in AIR_BLOCKS and not self._is_self_cavity([c])
                    for c in ((nx, ny, nz), (nx, ny + 1, nz))
                )
            except ApiError:
                break
            if enters_natural_cave:
                break

            haz_s, adj_fl_s = self._inspect_step_hazards(check_cells, cache, forward_delta=(sdx, sdz))
            if haz_s is not None:
                break
            floor_cell = (nx, ny - 1, nz)
            try:
                fl_blk = self._get_block_name(floor_cell, cache)
                fl_ok = self._is_solid_floor(floor_cell, cache)
            except ApiError:
                break
            if fl_blk in FLUID_BLOCKS | {"bedrock"} | HAZARD_FLOOR_BLOCKS:
                break
            if adj_fl_s:
                seal_total += len(self._seal_breach(adj_fl_s, cache))
            ok_d, _, _, _ = self._dig_step_cells(check_cells, collect_rock=True, parent_node_id=0, cache=cache)
            if not ok_d:
                break
            if adj_fl_s:
                seal_total += len(self._seal_breach(adj_fl_s, cache))
            if not fl_ok and not self._place_bridge_block(floor_cell, cache):
                break
            if not self._move_exact((nx, ny, nz), timeout_ms=2800):
                break
            cur = (nx, ny, nz)
            pocket_wps.append(cur)

        # Enforce Waist-High Parapet + 1x1 Eye Window between cur and pt!
        vx_f, vz_f = pt[0] - cur[0], pt[2] - cur[2]
        if abs(vx_f) >= abs(vz_f) and vx_f != 0:
            wdx, wdz = (1 if vx_f > 0 else -1), 0
        elif vz_f != 0:
            wdx, wdz = 0, (1 if vz_f > 0 else -1)
        else:
            wdx, wdz = 0, 0

        need_reseal_parapet: tuple[int, int, int] | None = None
        if (wdx, wdz) != (0, 0):
            parapet_pt = (cur[0] + wdx, cur[1], cur[2] + wdz)
            window_pt = (cur[0] + wdx, cur[1] + 1, cur[2] + wdz)
            sunk_under_parapet = parapet_pt == (pt[0], pt[1] + 1, pt[2])
            if parapet_pt != pt and not sunk_under_parapet:
                try:
                    if not self._is_solid_floor(parapet_pt, cache):
                        # Place waist-high cobblestone parapet so 1.8m mobs & pathfinder CANNOT cross!
                        self._place_bridge_block(parapet_pt, cache)
                except ApiError:
                    pass
            if window_pt != pt:
                try:
                    wb = self._get_block_name(window_pt, cache)
                    if wb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([window_pt], cache) is None:
                        self._dig_cell_safely(window_pt, collect_rock=False, parent_node_id=0, cache=cache)
                except ApiError:
                    pass
            if sunk_under_parapet:
                # Target ore is directly beneath parapet_pt (e.g. LocC-v4 sunken ore):
                # uncover its top cap to expose the ore from our elevated step, then re-plug parapet after pickup!
                try:
                    pb = self._get_block_name(parapet_pt, cache)
                    if pb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([parapet_pt], cache) is None:
                        self._dig_cell_safely(parapet_pt, collect_rock=False, parent_node_id=0, cache=cache)
                        need_reseal_parapet = parapet_pt
                except ApiError:
                    pass

        ok_m, c_delta, s_delta = self._mine_vein_ore_cell(
            pt,
            bname,
            anchor=cur,
            target_set=target_set,
            cache=cache,
        )
        catch_total += c_delta
        seal_total += s_delta
        if need_reseal_parapet is not None:
            try:
                for ax, az in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nb_c = (need_reseal_parapet[0] + ax, need_reseal_parapet[1], need_reseal_parapet[2] + az)
                    if nb_c != cur and not self._is_self_cavity([nb_c]) and self._get_block_name(nb_c, cache) in AIR_BLOCKS:
                        seal_total += len(self._seal_breach([need_reseal_parapet], cache, allow_corridor=True))
                        break
            except ApiError:
                pass
        if ok_m:
            self.surgical_extractions += 1
        elif abs(pt[0] - cur[0]) + abs(pt[2] - cur[2]) <= 2:
            self._mark_ore_unreachable(pt, "surgical_out_of_reach")

        if len(pocket_wps) > 1:
            self._walk_waypoints(pocket_wps, reverse=True, min_hp_ref=min_hp_ref)
        if pt != (anchor[0], anchor[1] - 1, anchor[2]):
            self._move_exact(anchor, timeout_ms=2500)
        return ok_m, catch_total, seal_total


    def _vein_hook_reach(self, *, target_ore: bool, scan_band: bool) -> tuple[int, int]:
        """Return the vertical and horizontal search limits from a corridor anchor."""
        if self.surgical_enabled:
            return (4, 6) if target_ore else (3, 4)
        if self.radar_enabled:
            return (4, 7) if target_ore else (3, 5)
        if scan_band:
            return (6, 8) if target_ore else (3, 4)
        return 2, 3


    def _run_vein_hook(
        self,
        anchor: tuple[int, int, int],
        *,
        target_blocks: tuple[str, ...],
        current_node_id: int,
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
        min_target_tier: int = 1,
        min_hp_ref: list[float] | None = None,
        quota_met: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Harvest exposed ores ('fishbone': strict corridor-adjacent; 'radar': 12m scan + 1x1 surgical breach)."""
        target_set = set(target_blocks)
        inv_raw, _inv_counts = _inventory_snapshot_and_counts(self.bot)
        _, max_tier, _ = _pickaxe_inventory_summary(inv_raw)
        focus_target_only = _should_focus_target_only(
            strategy=self.strategy,
            min_target_tier=min_target_tier,
            max_tier=max_tier,
            anchor_y=anchor[1],
            target_y=self.target_y,
        )
        # BUG-092: when iron stock < 12, include iron ores in query_types so close exposed iron (<=2.5m) can be grabbed on the way.
        if focus_target_only:
            if (_inv_counts.get("iron_ingot", 0) + _inv_counts.get("raw_iron", 0)) < 12:
                query_types = list(dict.fromkeys([*target_blocks, "iron_ore", "deepslate_iron_ore"]))
            else:
                query_types = list(target_blocks)
        else:
            query_types = list(TRACKED_ORES)
        # fishbone_scan: widen ore reach ONLY when standing in the target band (target layer),
        # keep the safe narrow corridor-adjacent behaviour on the transit layers.
        _scan_band = self.strategy == "fishbone_scan" and abs(anchor[1] - self.target_y) <= SCAN_BAND
        if self.radar_enabled:
            hook_radius = VEIN_HOOK_RADAR_RADIUS
            hook_limit = 24
        elif _scan_band:
            hook_radius = VEIN_HOOK_SCAN_RADIUS
            hook_limit = 20
        else:
            hook_radius = VEIN_HOOK_FISHBONE_RADIUS
            hook_limit = 12

        try:
            found = self.bot.call("find_blocks", {"radius": hook_radius, "types": query_types, "limit": hook_limit})
        except ApiError:
            return {"mined_coords": [], "deferred_added": 0, "catch_placed": 0, "breaches_sealed": 0}

        raw_blocks = found.get("blocks") or []
        mined_coords: list[list[int]] = []
        deferred_added = 0
        catch_placed = 0
        breaches_sealed = 0
        hp_ref = min_hp_ref if min_hp_ref is not None else [20.0]

        candidates: list[tuple[int, float, tuple[int, int, int], str]] = []
        for blk in raw_blocks:
            bname = str(blk.get("name") or "").removeprefix("minecraft:")
            bpos = blk.get("pos")
            if not bname or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            if pt in self.mined_ore_positions or self._is_ore_unreachable(pt) or self.skills._is_block_avoided(pt):
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="vein_hook") is not None:
                continue
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
            if bname in target_set:
                self.ledger.record_ore(pt, bname, req_t)

            max_y_diff, max_xz_diff = self._vein_hook_reach(target_ore=bname in target_set, scan_band=_scan_band)

            if abs(pt[1] - anchor[1]) > max_y_diff or (abs(pt[0] - anchor[0]) + abs(pt[2] - anchor[2])) > max_xz_diff:
                if (self.radar_enabled or _scan_band) and bname in target_set and req_t <= max_tier:
                    self.ever_seen_radar_ores.add(pt)
                    if pt not in self.radar_targets:
                        self.radar_targets.append(pt)
                elif req_t > max_tier and pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(pt, bname, req_t, current_node_id)
                    deferred_added += 1
                continue
            if req_t > max_tier:
                if pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(
                        pos=pt,
                        block_name=bname,
                        required_tier=req_t,
                        nearest_node_id=current_node_id,
                    )
                    deferred_added += 1
                continue
            if bname not in target_set:
                if focus_target_only:
                    # BUG-092: zero-detour opportunistic iron grab (<=2.5m) when iron stock < 12
                    if (
                        bname in {"iron_ore", "deepslate_iron_ore"}
                        and math.dist(anchor, pt) <= 2.5
                        and (_inv_counts.get("iron_ingot", 0) + _inv_counts.get("raw_iron", 0)) < 12
                    ):
                        pass
                    else:
                        continue
                elif bname not in {"coal_ore", "deepslate_coal_ore", "iron_ore", "deepslate_iron_ore", "diamond_ore", "deepslate_diamond_ore"}:
                    continue
            prio = 0 if bname in target_set else 1
            dist = math.dist(anchor, pt)
            candidates.append((prio, dist, pt, bname))

        candidates.sort(key=lambda r: (r[0], r[1]))
        vein_queue: deque[tuple[tuple[int, int, int], str]] = deque()
        queued_pts: set[tuple[int, int, int]] = set()
        for _, _, pt, bname in candidates[:6]:
            if pt not in queued_pts:
                vein_queue.append((pt, bname))
                queued_pts.add(pt)

        vein_limit = 16
        chain_limit = 7 if self.radar_enabled else 4
        while vein_queue and len(mined_coords) < vein_limit:
            pt, bname = vein_queue.popleft()
            try:
                ok_ore, catch_delta, seal_delta = self._mine_vein_ore_cell(
                    pt,
                    bname,
                    anchor=anchor,
                    target_set=target_set,
                    cache=cache,
                )
                catch_placed += catch_delta
                breaches_sealed += seal_delta
                if (
                    not ok_ore
                    and (self.surgical_enabled or _scan_band)
                    and bname in target_set
                    and pt not in self.mined_ore_positions
                    and min(abs(pt[0] - anchor[0]), abs(pt[2] - anchor[2])) <= 1
                    and max(abs(pt[0] - anchor[0]), abs(pt[2] - anchor[2])) <= 4
                ):
                    ok_ore, c_surg, s_surg = self._surgical_breach_to_ore(
                        anchor,
                        pt,
                        bname,
                        target_set=target_set,
                        geofence=geofence,
                        cache=cache,
                        min_hp_ref=hp_ref,
                    )
                    catch_placed += c_surg
                    breaches_sealed += s_surg
                elif not ok_ore and self.radar_enabled and bname in target_set and pt not in self.mined_ore_positions:
                    self.ever_seen_radar_ores.add(pt)
                    if pt not in self.radar_targets:
                        self.radar_targets.append(pt)
            except _MiningDeadline:
                scoped = self._budget_session
                if (
                    scoped is not None and not scoped.dug_count
                    and scoped.timeout_dig_read_s >= 1.0 and scoped.timeout_dig_pos != pt
                ):
                    # The next approach to this ore might dig a different blocker.
                    self.skills._avoid_temporarily(pt, "vein_dig_timeout", DIG_TIMEOUT_COOLDOWN_S)
                raise
            if not ok_ore:
                continue
            mined_coords.append([pt[0], pt[1], pt[2]])
            if bname in target_set:
                if quota_met is not None and quota_met():
                    break
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
                    if nb in queued_pts or nb in self.mined_ore_positions or self.skills._is_block_avoided(nb):
                        continue
                    if geofence is not None and geofence.check_point(nb[0] + 0.5, nb[2] + 0.5, action="vein_hook") is not None:
                        continue
                    if abs(nb[0] - anchor[0]) + abs(nb[1] - anchor[1]) + abs(nb[2] - anchor[2]) > chain_limit:
                        continue
                    try:
                        nb_name = self._get_block_name(nb, cache)
                    except ApiError:
                        continue
                    if nb_name in target_set:
                        vein_queue.append((nb, nb_name))
                        queued_pts.add(nb)

        if mined_coords:
            self._move_exact(anchor)
            self._equip_for_block_tier(1, for_ore=False)

        return {
            "mined_coords": mined_coords,
            "deferred_added": deferred_added,
            "catch_placed": catch_placed,
            "breaches_sealed": breaches_sealed,
        }


    def _dig_step_cells(
        self,
        cells: list[tuple[int, int, int]],
        *,
        collect_rock: bool,
        parent_node_id: int,
        cache: dict[tuple[int, int, int], str],
        verify_pickaxe: bool = False,
    ) -> tuple[bool, str | None, list[list[int]], int]:
        mined_ores: list[list[int]] = []
        deferred_added = 0
        for dpos in cells:
            ok_d, err_d, was_ore, def_delta = self._dig_cell_safely(
                dpos,
                collect_rock=collect_rock,
                parent_node_id=parent_node_id,
                cache=cache,
                verify_pickaxe=verify_pickaxe,
            )
            deferred_added += def_delta
            if was_ore:
                mined_ores.append([dpos[0], dpos[1], dpos[2]])
            if not ok_d:
                return False, err_d or "dig_failed", mined_ores, deferred_added
        return True, None, mined_ores, deferred_added


    def _move_excavated_step(
        self,
        step_pt: tuple[int, int, int],
        *,
        from_cell: tuple[int, int, int],
        head_cells: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        deadline: float,
        min_hp: float,
    ) -> str | None:
        floor = (step_pt[0], step_pt[1] - 1, step_pt[2])
        # Excavation or a failed bridge placement can invalidate the earlier support check.
        try:
            if not self._is_solid_floor(floor, {}):
                return "unsafe_floor_or_cave"
        except ApiError:
            return "unknown_block"
        if self._move_exact(step_pt):
            return None
        blocked = "movement_not_confirmed"
        if time.monotonic() + 2.5 >= deadline:
            return blocked
        try:
            stance = self.bot.status()
            if stance.get("alive") is False or stance.get("ready") is False:
                return "not_ready"
            hp = stance.get("health")
            if isinstance(hp, (int, float)) and hp < min_hp:
                return "hurt"
            if (
                not stance.get("on_ground", True)
                or stance.get("in_water") or stance.get("in_lava")
            ):
                return blocked
            if _floor_pos(stance["pos"]) != from_cell:
                if step_pt[1] == from_cell[1] - 1 and stance.get("on_ground") is True:
                    return self._rejoin_excavated_descent(step_pt, from_cell, stance, deadline=deadline, min_hp=min_hp)
                return blocked

            clearance = tuple(dict.fromkeys((*head_cells, step_pt)))
            for cell in (*clearance, floor):
                cache.pop(cell, None)
            names = [self._get_block_name(cell, cache) for cell in clearance]
            if (
                any(name not in PASSABLE_BLOCKS | FALLING_BLOCKS for name in names)
                or not self._is_solid_floor(floor, cache)
            ):
                return blocked
            for cell, name in zip(clearance, names):
                if name in FALLING_BLOCKS:
                    ok, reason, _, _ = self._dig_cell_safely(cell, collect_rock=False, parent_node_id=0, cache=cache)
                    if not ok:
                        return reason if reason in {"hurt", "not_ready", "interrupted"} else blocked
        except ApiError:
            return blocked

        if time.monotonic() + 2.5 < deadline and self._move_exact(step_pt, timeout_ms=1500):
            return None
        return blocked


    def _rejoin_excavated_descent(
        self,
        step: tuple[int, int, int],
        start: tuple[int, int, int],
        stance: dict[str, Any],
        *,
        deadline: float,
        min_hp: float,
    ) -> str | None:
        """Return to the excavated step after overshooting onto a short dry stair."""
        blocked = "movement_not_confirmed"
        current = _floor_pos(stance["pos"])
        dx, dz = step[0] - start[0], step[2] - start[2]
        overshoot = (current[0] - step[0], current[2] - step[2])
        rise = step[1] - current[1]
        if overshoot not in {(dx, dz), (2 * dx, 2 * dz)} or rise not in {0, 1}:
            return blocked
        cache: dict[tuple[int, int, int], str] = {}
        if rise == 1 and overshoot != (dx, dz):
            route = self._recorded_mixed_stair(current, start, cache=cache)
            if route is None or step not in route[1:-1]:
                return blocked
            recovery = route[:route.index(step) + 1]
        else:
            recovery = _interpolate_cells(current, step)
        for x, y, z in recovery:
            feet, head = (x, y, z), (x, y + 1, z)
            if (
                self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                or self.skills._is_block_avoided(feet)
                or any(self._get_block_name(cell, cache) not in PASSABLE_BLOCKS for cell in (feet, head))
                or not self._is_solid_floor((x, y - 1, z), cache)
                or self._adjacent_fluid_or_falling([head, feet], cache) is not None
            ):
                return blocked

        for x, y, z in recovery[1:]:
            if time.monotonic() + 2.5 >= deadline:
                return blocked
            self.bot.call("move_to", {
                "x": x + 0.5, "y": y, "z": z + 0.5, "range": 0.3, "timeout": 1500,
                "stop_on": ["hurt", "death"],
            }, timeout_s=2.5)
            actual = self.bot.status()
            if (
                actual.get("alive") is False or actual.get("ready") is False
                or actual.get("last_death") != stance.get("last_death")
                or actual.get("dimension") != stance.get("dimension")
            ):
                return "not_ready"
            hp = actual.get("health")
            if isinstance(hp, (int, float)) and hp < min_hp:
                return "hurt"
            if (
                actual.get("on_ground") is not True or actual.get("in_water") or actual.get("in_lava")
                or not actual.get("pos") or _floor_pos(actual["pos"]) != (x, y, z)
            ):
                return blocked
        self._record_corridor(step)
        return None


    def _should_recraft_burner_pickaxe(
        self,
        chosen_rock: dict[str, Any] | None,
        total_rem: int,
        *,
        in_deepslate: bool = False,
    ) -> bool:
        """Recraft a stone burner pickaxe when durability is low, only wooden remains (BUG-091), or we would burn iron/diamond on rock."""
        if total_rem <= RECRAFT_PICK_DURABILITY:
            return True
        if in_deepslate and chosen_rock is not None and int(chosen_rock.get("tier", 1)) >= 3 and total_rem > 160:
            return False
        if chosen_rock is not None and int(chosen_rock.get("tier", 1)) != 2:
            return True
        return False


    def _try_recraft_burner_pickaxe(
        self, counts_now: dict[str, int], *, at_durability_redline: bool = False,
    ) -> bool:
        """Craft one stone pickaxe from collected stone/wood so rock digging never eats the iron pick.

        Returns True when a fresh pickaxe is now in the inventory.
        """
        # Allow one replacement if the two worn picks together cannot sustain digging.
        stone_pickaxe_limit = 3 if at_durability_redline else 2
        if int(counts_now.get("stone_pickaxe", 0)) >= stone_pickaxe_limit:
            return False
        affordable, stone, _ = _stone_pickaxe_headroom(counts_now)
        if affordable <= 0 or stone < 3:
            return False
        has_table = int(counts_now.get("crafting_table", 0)) >= 1
        if not has_table:
            planks_equiv = _total_planks(counts_now) + 4 * sum(
                int(v) for k, v in counts_now.items() if k.endswith(("_log", "_stem"))
            )
            sticks = int(counts_now.get("stick", 0))
            need_planks = 4 + (0 if sticks >= 2 else 2)
            if planks_equiv < need_planks:
                return False
        scoped = self._budget_session
        confirmed_before = scoped.confirmed_stone_pickaxe_crafts if scoped is not None else 0
        try:
            res = self.skills.craft_gear(item="stone_pickaxe", count=1)
            if not isinstance(res, dict) or not res.get("ok") or res.get("already_owned") or res.get("gained") == 0:
                return False
            owned = _inventory_counts(self.bot).get("stone_pickaxe", 0)
        except _MiningDeadline:
            if scoped is not None:
                # The craft reply may arrive before the final inventory can be sampled.
                self.recrafted_picks += scoped.confirmed_stone_pickaxe_crafts - confirmed_before
            raise
        except (ApiError, ValueError, KeyError, TypeError):
            return False
        if owned <= counts_now.get("stone_pickaxe", 0):
            return False
        self.recrafted_picks += 1
        return True


    def _seal_toward_threat(
        self,
        pos: tuple[int, int, int],
        threat_pos: tuple[float, float, float],
    ) -> int:
        """Wall off the corridor cell facing the threat so melee mobs lose line of sight."""
        dx = float(threat_pos[0]) - float(pos[0])
        dz = float(threat_pos[2]) - float(pos[2])
        step = (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dz) else (0, 1 if dz > 0 else -1)
        if abs(dx) < 0.5 and abs(dz) < 0.5:
            return 0
        cols: list[tuple[int, int, int]] = []
        for dy in (-1, 0, 1):
            base = (pos[0] + step[0], pos[1] + dy, pos[2] + step[1])
            cols.extend([(base[0], base[1] + k, base[2]) for k in (0, 1, 2)])
        return len(self._seal_breach(cols, {}, allow_corridor=True))


    def is_inside_shaft(self, pos: tuple[int, int, int] | None = None) -> bool:
        """True when the bot stands in its own carved works (corridor, vein pocket or node)."""
        if pos is None:
            st = self.bot.status()
            pos = _floor_pos(st.get("pos") or [0, 64, 0])
        for group in (self.known_corridor_positions, self.excavated_positions):
            for c in group:
                if max(abs(pos[0] - c[0]), abs(pos[1] - c[1]), abs(pos[2] - c[2])) <= 1:
                    return True
        for n in self.nodes.values():
            if max(abs(pos[0] - n.anchor[0]), abs(pos[1] - n.anchor[1]), abs(pos[2] - n.anchor[2])) <= 2:
                return True
        return False


    def retreat_into_shaft(
        self,
        *,
        min_hp_ref: list[float] | None = None,
        max_hops: int = 3,
        seal_behind: bool = True,
        threat_pos: tuple[float, float, float] | None = None,
    ) -> dict[str, Any]:
        """Threat response that never leaves our own tunnel: back up the tree and seal the choke point.

        Skeleton mining only stays safe inside the corridor it carved. Free pathfinding flee at
        depth tends to run into open cave air, so prefer retreating along recorded waypoints.
        Returns ok=False when the bot is not inside its own shaft (caller should flee normally).
        """
        if not self.nodes or self.collar_node_id is None:
            return {"ok": False, "mode": "retreat_shaft", "reason": "no_shaft"}
        st = self.bot.status()
        pos = _floor_pos(st.get("pos") or [0, 64, 0])
        inside = self.is_inside_shaft(pos)
        if not inside:
            return {"ok": False, "mode": "retreat_shaft", "reason": "not_in_shaft", "pos": list(pos)}

        hp_ref = min_hp_ref if min_hp_ref is not None else [float(st.get("health") or 20.0)]
        hops = 0
        sealed = 0
        # Break line of sight FIRST: against an adjacent mob, walking backwards while being hit
        # just trades hits until death (observed live). Wall off the threat side, then retreat.
        sealed_first = 0
        if threat_pos is not None:
            sealed_first = self._seal_toward_threat(pos, threat_pos)
            sealed += sealed_first
        cur = self.nodes.get(self.active_node_id) if self.active_node_id is not None else None
        while hops < max(1, int(max_hops)) and cur is not None and cur.node_id != self.collar_node_id:
            parent = self.nodes.get(cur.parent_id) if cur.parent_id is not None else None
            if parent is None:
                break
            ok_walk, err = self._walk_waypoints(
                cur.edge_from_parent.waypoints, reverse=True, min_hp_ref=hp_ref
            )
            if not ok_walk:
                if hops == 0:
                    return {"ok": False, "mode": "retreat_shaft", "reason": err or "retreat_blocked", "pos": list(pos)}
                break
            self.active_node_id = parent.node_id
            hops += 1
            if seal_behind:
                choke = [(cur.anchor[0], cur.anchor[1] + dy, cur.anchor[2]) for dy in (0, 1, 2)]
                sealed += len(self._seal_breach(choke, {}, allow_corridor=True))
            cur = parent

        st_end = self.bot.status()
        return {
            "ok": hops > 0,
            "mode": "retreat_shaft",
            "structure": "carved_shaft",
            "hops": hops,
            "sealed_first": sealed_first,
            "sealed": sealed,
            "pos_before": [round(float(v), 1) for v in pos],
            "pos_after": st_end.get("pos"),
            "requires_replan": hops > 0,
            "note": (
                "沿自建巷道后退并封堵来路：始终留在鱼骨巷道内，不进入未知洞穴。"
                if hops > 0
                else "已在井口节点，无需继续后退。"
            ),
        }


    def _remember_water_interruption(self, trigger: Any, status: dict[str, Any]) -> bool:
        if not isinstance(trigger, dict) or trigger.get("source") != "tool_water_threat":
            return False
        collar = self.nodes.get(self.collar_node_id)
        observation = trigger.get("water_observation")
        life = (status.get("dimension"), status.get("last_death"))
        if (
            collar is None or not isinstance(observation, dict)
            or observation.get("in_water") is not True or trigger.get("observed_life") != life
        ):
            return False
        try:
            water_cell = _floor_pos(observation["pos"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        self._water_interrupted_shaft = _WaterInterruptedShaft(
            collar.anchor, water_cell, life, time.monotonic() + WATER_SHAFT_COOLDOWN_S,
        )
        return True


    def _switch_to_dry_collar_after_preemption(
        self,
        stance: dict[str, Any],
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any,
    ) -> bool:
        wet_shaft = self._water_interrupted_shaft
        if wet_shaft is None:
            return True
        if (
            wet_shaft.until <= time.monotonic()
            or wet_shaft.observed_life != (stance.get("dimension"), stance.get("last_death"))
        ):
            self._water_interrupted_shaft = None
            return True
        collar = self.nodes.get(self.collar_node_id)
        if collar is None or collar.anchor != wet_shaft.collar:
            return True
        x, y, z = cur_pos
        wet_x, wet_y, wet_z = wet_shaft.water_cell
        surface_y = self.collar_ceiling_y if self.collar_ceiling_y is not None else collar.anchor[1]
        if (
            stance.get("on_ground") is not True or stance.get("in_water") or stance.get("in_lava")
            or self._geofence_blocked(geofence, x, z, action="mine_dfs_collar") is not None
        ):
            return False
        offset_x, offset_z = x - wet_x, z - wet_z
        horizontal_distance = math.hypot(offset_x, offset_z)
        local_at_depth = y <= surface_y - 20
        probe_directions = tuple(DIRECTIONS)
        if local_at_depth:
            # A short dry relocation near the working layer should not restart the descent.
            if (
                horizontal_distance < 6.0 or abs(y - wet_y) > 2
                or abs(y - self.target_y) > DEPTH_RESET_THRESHOLD
            ):
                return False
            probe_directions = tuple(d for d, (dx, dz) in DIRECTIONS.items() if dx * offset_x + dz * offset_z >= 0)
        elif horizontal_distance < NODE_REJOIN_DISTANCE:
            return False

        cache: dict[tuple[int, int, int], str] = {}
        try:
            if (
                not self._is_solid_floor((x, y - 1, z), cache)
                or any(self._get_block_name((x, y + dy, z), cache) not in PASSABLE_BLOCKS for dy in (0, 1))
            ):
                return False
            directions = self._probe_collar_dirs(
                cur_pos, geofence=geofence, cache=cache, directions=probe_directions, dry_only=True,
            )
        except ApiError:
            return False
        if not directions:
            return False

        self._archive_current_shaft_to_ledger(mark_exhausted=False)
        self._clear_active_tree_only()
        new_collar = self._init_collar(cur_pos, target_y=self.target_y, geofence=geofence, viable_dirs=directions)
        if local_at_depth:
            new_collar.branches = [branch for branch in new_collar.branches if branch.direction in directions]
        return True


    def _note_fluid_abort(self, reason: str) -> bool:
        """Count aquifer/hazard aborts; True once this shaft looks like a water/cave trap."""
        if reason in FLUID_ABORT_REASONS:
            self.fluid_aborts += 1
        elif reason not in {"time_slice", "completed", "reached_target_y"}:
            self.fluid_aborts = 0
        return self.fluid_aborts >= FLUID_ABORT_LIMIT


    def _continue_descent_around_hazard(
        self,
        node: ShaftNode,
        branch: BranchSpec,
        waypoints: list[tuple[int, int, int]],
        *,
        geofence: Any,
        min_hp_ref: list[float],
    ) -> bool:
        """Keep the verified descent and queue dry side branches when available."""
        end = waypoints[-1]
        if waypoints[0] != node.anchor or end[1] >= node.anchor[1]:
            return False
        branches = self._probe_dry_descent(
            end, directions=LEFT_RIGHT_DIRS[branch.direction], geofence=geofence, min_hp_ref=min_hp_ref,
        )
        if branches is None:
            return False
        self._create_node(
            kind="switchback", anchor=end, parent_id=node.node_id,
            edge_from_parent=ShaftEdge(branch.kind, branch.direction, waypoints), branches=branches,
        )
        return bool(branches)


    def _probe_dry_descent(
        self,
        anchor: tuple[int, int, int],
        *,
        directions: tuple[str, ...],
        geofence: Any,
        min_hp_ref: list[float],
    ) -> list[BranchSpec] | None:
        """Return dry branch candidates, or None if the current stance is unconfirmed."""
        x, y, z = anchor
        status = self.bot.status()
        health = status.get("health")
        if (
            status.get("alive") is False or status.get("ready") is False
            or not status.get("on_ground") or status.get("in_water") or status.get("in_lava")
            or not status.get("pos") or _floor_pos(status["pos"]) != anchor
            or (isinstance(health, (int, float)) and health < min_hp_ref[0])
            or self._geofence_blocked(geofence, x, z, action="mine_dfs_switchback") is not None
        ):
            return None

        cache: dict[tuple[int, int, int], str] = {}
        try:
            if not self._is_solid_floor((x, y - 1, z), cache):
                return None
            if any(self._get_block_name(pt, cache) not in PASSABLE_BLOCKS for pt in (anchor, (x, y + 1, z))):
                return None
        except ApiError:
            return None
        if not directions:
            return []
        viable = self._probe_collar_dirs(
            anchor, geofence=geofence, cache=cache, directions=directions, dry_only=True,
        )
        return [BranchSpec("stair_down", d, y - self.target_y, branch_target_y=self.target_y) for d in viable]


    def _force_relocate_on_fluid(
        self,
        *,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
        auto_relocations: int,
    ) -> tuple[bool, int]:
        """Archive a water-logged shaft and open a fresh collar elsewhere.

        Only works at surface level: from depth -50 the candidate scan finds no solid, dry
        surface stand, so the caller is told to climb out first and reopen there.
        """
        cur_now = _floor_pos(self.bot.status()["pos"])
        collar_y: int | None = None
        if self.collar_node_id in self.nodes:
            collar_y = self.nodes[self.collar_node_id].anchor[1]
        if collar_y is not None and cur_now[1] <= collar_y - 6:
            # Live s03: 3 cave breeches at y=8 -> relocation failed -> model reset the tree
            # underground -> explore had no collar and spun on no_progress.
            self.fluid_abandoned_deep = True
            self._archive_current_shaft_to_ledger(mark_exhausted=True)
            self._clear_active_tree_only()
            return False, auto_relocations
        self._archive_current_shaft_to_ledger(mark_exhausted=True)
        if auto_relocations < 1 and time.monotonic() + 8.0 < deadline:
            self._clear_active_tree_only()
            new_site = self._relocate_to_fresh_collar_site(
                cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
            )
            new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
            if new_site != cur_now and new_dirs:
                self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                return True, auto_relocations + 1
        return False, auto_relocations


    def _retry_dry_junction(
        self,
        node: ShaftNode,
        *,
        geofence: Any,
        min_hp_ref: list[float],
    ) -> bool:
        collar = self.nodes.get(self.collar_node_id)
        if collar is None or node.anchor[1] > collar.anchor[1] - 6:
            return False
        if not any(branch.status == "pending" for branch in node.branches):
            return False
        if self._probe_dry_descent(node.anchor, directions=(), geofence=geofence, min_hp_ref=min_hp_ref) is None:
            return False
        self.fluid_aborts = 0
        return True


    def _retry_dry_parent_after_level_abort(
        self, node: ShaftNode, waypoints: list[tuple[int, int, int]],
        *, geofence: Any, min_hp_ref: list[float],
    ) -> bool:
        parent = self.nodes.get(node.parent_id)
        collar = self.nodes.get(self.collar_node_id)
        edge = node.edge_from_parent
        if (
            parent is None or collar is None or edge is None
            or parent.anchor[1] != node.anchor[1] or parent.anchor[1] > collar.anchor[1] - 6
            or not any(branch.status == "pending" for branch in parent.branches)
            or not waypoints or waypoints[0] != node.anchor
            or not edge.waypoints or edge.waypoints[0] != parent.anchor or edge.waypoints[-1] != node.anchor
        ):
            return False

        retreat = [*reversed(waypoints), *reversed(edge.waypoints[:-1])]
        if any(cell[1] != parent.anchor[1] for cell in retreat):
            return False
        if self._probe_dry_descent(retreat[0], directions=(), geofence=geofence, min_hp_ref=min_hp_ref) is None:
            return False
        walked, _ = self._walk_waypoints(retreat, min_hp_ref=min_hp_ref)
        if not walked or not self._retry_dry_junction(parent, geofence=geofence, min_hp_ref=min_hp_ref):
            return False
        self.active_node_id = parent.node_id
        return True


    def _pickaxe_redline_threshold(self, cur_y: int, counts_now: dict[str, int]) -> int:
        if self.collar_node_id in self.nodes:
            collar_y = self.nodes[self.collar_node_id].anchor[1]
            if collar_y - cur_y >= 10 and _has_collar_resupply_materials(counts_now):
                return 12
        return 6

