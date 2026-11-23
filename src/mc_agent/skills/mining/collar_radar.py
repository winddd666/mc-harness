"""MiningDFSEngine mixin: collar_radar (split from legacy mining_dfs.py)."""

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


class _CollarRadarMixin:
    def _scan_ore_radar(
        self,
        anchor: tuple[int, int, int],
        *,
        target_blocks: tuple[str, ...],
        max_tier: int,
        geofence: Any,
        force: bool = False,
    ) -> list[tuple[int, int, int]]:
        """64-block radar ('fishbone': off; 'fishbone_scan': read-only in target band; 'v5': nearest-point; 'radar': cluster + triage)."""
        _scan_band = self.strategy == "fishbone_scan" and abs(anchor[1] - self.target_y) <= SCAN_BAND
        if not self.radar_enabled and not _scan_band:
            self.radar_targets = []
            self.locked_cluster = None
            return []

        self.radar_targets = [
            pt for pt in self.radar_targets
            if pt not in self.mined_ore_positions
            and not self._is_ore_unreachable(pt)
            and not self.skills._is_block_avoided(pt)
        ]
        recheck_dist = 8.0 if self.surgical_enabled else 10.0
        if (
            not force
            and self._last_radar_anchor is not None
            and math.dist(anchor, self._last_radar_anchor) < recheck_dist
            and self.radar_targets
        ):
            if not self.surgical_enabled:
                self.locked_cluster = None
                self.radar_targets.sort(key=lambda p: math.dist(anchor, p))
                return self.radar_targets
            hostiles_cached = self._scan_hostile_positions(radius=24)
            clusters = self._cluster_ore_points(anchor, self.radar_targets, hostiles_cached)
            self.locked_cluster = clusters[0] if clusters else None
            self.radar_targets = [c["rep"] for c in clusters] + [
                p for c in clusters for p in c["members"] if p != c["rep"]
            ]
            return self.radar_targets

        self._last_radar_anchor = anchor
        target_set = set(target_blocks)
        found_pts: list[tuple[int, int, int]] = []
        seen: set[tuple[int, int, int]] = set()
        scan_limit = 60 if self.surgical_enabled else 32

        try:
            res = self.bot.call(
                "find_blocks",
                {"radius": RADAR_SCAN_RADIUS, "types": list(target_blocks), "limit": scan_limit},
            )
            blocks = res.get("blocks") or []
        except ApiError:
            blocks = []

        nearest_nid = self.active_node_id or self.collar_node_id or 1
        for blk in blocks:
            bname = str(blk.get("name") or "").removeprefix("minecraft:")
            bpos = blk.get("pos")
            if bname not in target_set or not isinstance(bpos, (list, tuple)) or len(bpos) < 3:
                continue
            pt = (int(bpos[0]), int(bpos[1]), int(bpos[2]))
            if (
                pt in seen
                or pt in self.mined_ore_positions
                or self._is_ore_unreachable(pt)
                or self.skills._is_block_avoided(pt)
            ):
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="ore_radar") is not None:
                continue
            seen.add(pt)
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 2))
            self.ledger.record_ore(pt, bname, req_t)
            if req_t > max_tier:
                if pt not in self.deferred_ores:
                    self.deferred_ores[pt] = DeferredOreRecord(pt, bname, req_t, nearest_nid)
                continue
            self.ever_seen_radar_ores.add(pt)
            found_pts.append(pt)

        for pt, meta in list(self.ledger.discovered_ores.items()):
            if (
                pt in seen
                or pt in self.mined_ore_positions
                or self._is_ore_unreachable(pt)
                or self.skills._is_block_avoided(pt)
            ):
                continue
            if meta.get("block") not in target_set or int(meta.get("required_tier", 2)) > max_tier:
                continue
            if math.dist(anchor, pt) > RADAR_SCAN_RADIUS + 4:
                continue
            if geofence is not None and geofence.check_point(pt[0] + 0.5, pt[2] + 0.5, action="ore_radar") is not None:
                continue
            if meta.get("from_disk"):
                try:
                    live_name = self._get_block_name(pt)
                except ApiError:
                    continue
                if live_name != meta.get("block"):
                    self.ledger.remove_ore(pt)
                    continue
                meta["from_disk"] = False
            seen.add(pt)
            self.ever_seen_radar_ores.add(pt)
            found_pts.append(pt)

        if self.strategy in {"radar", "v5"} and found_pts:
            # Never lock onto bedrock-embedded ores (Overworld bedrock spans Y=-64..-60);
            # diving into Y<=-60 triggers repeated `shaft_exhausted` stalls.
            if self.target_y < 0:
                above_bedrock = [p for p in found_pts if p[1] >= -59]
                if above_bedrock:
                    found_pts = above_bedrock
            in_band = [p for p in found_pts if abs(p[1] - self.target_y) <= 8]
            if in_band:
                found_pts = in_band
            else:
                near_band = [p for p in found_pts if abs(p[1] - self.target_y) <= 10]
                if near_band:
                    found_pts = near_band

        if not self.surgical_enabled:
            # 'v5' mode: original v4 nearest single-point radar ordering (no hostile cluster triage)
            self.locked_cluster = None
            found_pts.sort(key=lambda p: (abs(p[1] - self.target_y) // 12, math.dist(anchor, p)))
            self.radar_targets = found_pts[:24]
            return self.radar_targets

        hostiles = self._scan_hostile_positions(radius=32)
        clusters = self._cluster_ore_points(anchor, found_pts, hostiles)
        self.locked_cluster = clusters[0] if clusters else None
        ordered_pts = [c["rep"] for c in clusters] + [
            p for c in clusters for p in c["members"] if p != c["rep"]
        ]
        self.radar_targets = ordered_pts[:32]
        return self.radar_targets


    def _best_radar_target_for_anchor(
        self,
        anchor: tuple[int, int, int],
        *,
        at_spine: bool = False,
    ) -> tuple[int, int, int] | None:
        if not self.radar_enabled:
            return None
        if self.surgical_enabled and self.locked_cluster is not None:
            rep = tuple(self.locked_cluster["rep"])
            if (
                rep not in self.mined_ore_positions
                and not self._is_ore_unreachable(rep)
                and not self.skills._is_block_avoided(rep)
            ):
                return rep
        valid = [
            pt for pt in self.radar_targets
            if pt not in self.mined_ore_positions
            and not self._is_ore_unreachable(pt)
            and not self.skills._is_block_avoided(pt)
        ]
        if not valid:
            return None
        band_tol = 16 if self.surgical_enabled else 10
        if at_spine:
            same_band = [pt for pt in valid if abs(pt[1] - anchor[1]) <= band_tol]
            if same_band:
                return min(same_band, key=lambda p: math.dist(anchor, p))
        return min(valid, key=lambda p: math.dist(anchor, p))


    def _radar_direction_projection(
        self,
        anchor: tuple[int, int, int],
        direction: str,
        *,
        at_spine: bool = False,
    ) -> float:
        rt = self._best_radar_target_for_anchor(anchor, at_spine=at_spine)
        if rt is None:
            return 0.0
        dx, dz = DIRECTIONS[direction]
        vx, vz = rt[0] - anchor[0], rt[2] - anchor[2]
        return float(vx * dx + vz * dz)


    def _probe_collar_dirs(
        self,
        anchor: tuple[int, int, int],
        *,
        geofence: Any,
        cache: dict[tuple[int, int, int], str] | None = None,
        counts: dict[str, int] | None = None,
        directions: tuple[str, ...] = ("north", "east", "south", "west"),
        dry_only: bool = False,
        blockers: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        blk_cache = cache if cache is not None else {}
        inv_counts = counts if counts is not None else _inventory_counts(self.bot)
        has_seal = _seal_reserve(inv_counts) >= 1
        canonical = {"north": 0, "east": 1, "south": 2, "west": 3}
        scored: list[tuple[int, float, int, str]] = []
        effective_y = self._effective_collar_target_y(anchor)
        at_target_layer = self._collar_starts_at_level(anchor, effective_y)
        step_y = anchor[1] if at_target_layer else anchor[1] - 1

        for d in directions:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, anchor[0] + dx * 3, anchor[2] + dz * 3, action="mine_dfs_collar") is not None:
                if blockers is not None:
                    blockers[d] = {"code": "void_geofence_blocked"}
                continue
            nx, nz = anchor[0] + dx, anchor[2] + dz
            # At the target layer, the block below the face is the floor.
            # Probing it as a dig cell rejects walkable bedrock and probes a
            # descent that this collar will never actually take.
            dig_cells = [(nx, y, nz) for y in range(anchor[1] + 1, step_y - 1, -1)]
            try:
                hazard, adj_fluids = self._inspect_step_hazards(dig_cells, blk_cache, forward_delta=(dx, dz))
                if hazard is not None:
                    if blockers is not None:
                        code, details = hazard
                        blockers[d] = {"code": code, **details}
                    continue
                if adj_fluids and (dry_only or not has_seal and any(blk_cache.get(nb) in LAVA_BLOCKS for nb in adj_fluids)):
                    if blockers is not None:
                        fluid = adj_fluids[0] if dry_only else next(nb for nb in adj_fluids if blk_cache[nb] in LAVA_BLOCKS)
                        blockers[d] = {"code": "adjacent_fluid", "pos": list(fluid), "block": blk_cache[fluid]}
                    continue
                support_pt = (nx, step_y - 1, nz)
                support_blk = self._get_block_name(support_pt, blk_cache)
                if (
                    support_blk in FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS
                    or (support_blk == "bedrock" and not at_target_layer and step_y > effective_y)
                ):
                    if blockers is not None:
                        blockers[d] = {"code": "blocked_floor", "pos": list(support_pt), "block": support_blk}
                    continue
                support_ok = self._is_solid_floor(support_pt, blk_cache)
                if not support_ok:
                    can_bridge = has_seal and (
                        self._is_self_cavity([support_pt, *dig_cells])
                        or self._is_solid_floor((nx, step_y - 2, nz), blk_cache)
                    )
                    if not can_bridge:
                        if blockers is not None:
                            blocker = {"code": "missing_support", "pos": list(support_pt), "block": support_blk}
                            level_floor = (nx, anchor[1] - 1, nz)
                            if (
                                not at_target_layer and not adj_fluids
                                and blk_cache[(nx, anchor[1], nz)] in AIR_BLOCKS
                                and blk_cache[(nx, anchor[1] + 1, nz)] in AIR_BLOCKS
                                and blk_cache[level_floor] not in FALLING_BLOCKS
                                and self._is_solid_floor(level_floor, blk_cache)
                            ):
                                # The descent can lack support one block below a walkable ledge.
                                blocker["safe_side_step"] = [nx + 0.5, anchor[1], nz + 0.5]
                            blockers[d] = blocker
                        continue
            except ApiError as exc:
                if blockers is not None:
                    blockers[d] = {"code": "collar_probe_failed", "error": exc.code}
                continue

            proj = self._radar_direction_projection(anchor, d, at_spine=False)
            fluid_penalty = 1 if adj_fluids else 0
            radar_rank = -proj if proj > 1.0 else 0.0
            scored.append((fluid_penalty, radar_rank, canonical[d], d))

        scored.sort(key=lambda r: (r[0], r[1], r[2]))
        return [r[3] for r in scored]


    def _snap_from_hitbox_ledge_if_needed(
        self,
        pos: tuple[int, int, int],
        *,
        geofence: Any,
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[int, int, int]:
        """Fix underground hitbox-edge false 'no_solid_collar_ground' when standing across block boundary."""
        for dy in (0, -1):
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                cand = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                if self._geofence_blocked(geofence, cand[0], cand[2], action="collar_snap") is not None:
                    continue
                try:
                    if not self._is_solid_floor((cand[0], cand[1] - 1, cand[2]), cache):
                        continue
                    if self._get_block_name(cand, cache) not in PASSABLE_BLOCKS:
                        continue
                    if self._get_block_name((cand[0], cand[1] + 1, cand[2]), cache) not in PASSABLE_BLOCKS:
                        continue
                except ApiError:
                    continue
                if self._move_exact(cand, timeout_ms=2000):
                    return cand
        return pos


    def _descend_tree_canopy_if_needed(self, cur_pos: tuple[int, int, int]) -> tuple[int, int, int]:
        """Remove canopy leaves only when there is a confirmed landing within three blocks."""
        cache: dict[tuple[int, int, int], str] = {}
        pos = cur_pos
        for _ in range(10):
            under_pt = (pos[0], pos[1] - 1, pos[2])
            try:
                if not self._get_block_name(under_pt).endswith("_leaves"):
                    break
                for drop in range(1, 4):
                    floor = (under_pt[0], under_pt[1] - drop, under_pt[2])
                    block = self._get_block_name(floor, cache)
                    if block in PASSABLE_BLOCKS:
                        continue
                    if not (block.endswith("_leaves") or self._is_solid_floor(floor, cache)):
                        return pos
                    break
                else:
                    break
            except ApiError:
                break
            try:
                self.bot.call("dig", {"pos": list(under_pt), "collect": False, "stop_on": ["hurt", "death"]})
            except ApiError:
                break
            settled, grounded = self._settled_position()
            pos = _floor_pos(settled)
            if not grounded or pos != (floor[0], floor[1] + 1, floor[2]):
                break
        return pos


    def _clear_lateral_leaves_toward(self, stand: tuple[int, int, int]) -> None:
        """Clear blocking *_leaves laterally when walking off a dense tree branch onto nearby solid ground."""
        for _ in range(6):
            st = self.bot.status()
            cur = _floor_pos(st["pos"])
            vx, vz = stand[0] - cur[0], stand[2] - cur[2]
            if vx == 0 and vz == 0:
                return
            sx = (1 if vx > 0 else -1) if abs(vx) >= abs(vz) else 0
            sz = (1 if vz > 0 else -1) if sx == 0 else 0
            cleared_leaf = False
            for dy in (1, 0, -1):
                lp = (cur[0] + sx, cur[1] + dy, cur[2] + sz)
                try:
                    bn = self._get_block_name(lp)
                except ApiError:
                    continue
                if bn.endswith("_leaves"):
                    try:
                        self.bot.call("dig", {"pos": [lp[0], lp[1], lp[2]], "collect": False, "stop_on": ["hurt", "death"]})
                        cleared_leaf = True
                    except ApiError:
                        pass
            if not cleared_leaf:
                return
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": (cur[0] + sx) + 0.5,
                        "z": (cur[2] + sz) + 0.5,
                        "range": 1.0,
                        "timeout": 2500,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                pass


    def _find_solid_collar_candidates(
        self,
        origin: tuple[int, int, int],
        *,
        geofence: Any,
        avoid_exhausted: bool,
        radius: int = 20,
    ) -> list[tuple[int, int, int]]:
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": min(64, max(8, radius)), "types": list(SURFACE_GROUND_BLOCKS), "limit": 48},
            )
            blocks = found.get("blocks") or []
        except ApiError:
            blocks = []

        cache: dict[tuple[int, int, int], str] = {}
        counts = _inventory_counts(self.bot)
        scored: list[tuple[int, float, tuple[int, int, int]]] = []
        seen: set[tuple[int, int, int]] = set()

        raw_coords = [
            (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2]))
            for b in blocks
            if isinstance(b.get("pos"), (list, tuple)) and len(b["pos"]) >= 3
        ]
        # Underground supplement: probe local floor neighborhood directly so ceiling blocks don't starve limit=48
        if origin[1] < 55 and not avoid_exhausted:
            for dx in range(-4, 5, 2):
                for dz in range(-4, 5, 2):
                    for dy in (-2, -1, 0):
                        raw_coords.append((origin[0] + dx, origin[1] + dy, origin[2] + dz))

        for gx, gy, gz in raw_coords:
            stand = (gx, gy + 1, gz)
            if stand in seen:
                continue
            seen.add(stand)
            if self._geofence_blocked(geofence, gx, gz, action="collar_seek") is not None:
                continue
            if avoid_exhausted and self._is_near_exhausted_collar(stand, min_dist=EXHAUSTED_COLLAR_MIN_SEP):
                continue
            hdist = math.hypot(stand[0] - origin[0], stand[2] - origin[2])
            if avoid_exhausted and hdist < 10.0:
                continue
            try:
                if not self._is_solid_floor((gx, gy, gz), cache):
                    continue
                if self._get_block_name(stand, cache) not in PASSABLE_BLOCKS:
                    continue
                if self._get_block_name((gx, gy + 2, gz), cache) not in PASSABLE_BLOCKS:
                    continue
                dirs = self._probe_collar_dirs(stand, geofence=geofence, cache=cache, counts=counts)
            except ApiError:
                continue
            if not dirs:
                continue
            dir_tier = 0 if len(dirs) >= 2 else 1
            scored.append((dir_tier, math.dist(origin, stand), stand))

        scored.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in scored[:8]]


    def _skip_stalled_first_dig(self, *, deadline: float, min_hp: float) -> bool:
        scoped = self._budget_session
        if (
            scoped is None or scoped.dug_count or scoped.timeout_path != "dig"
            or scoped.timeout_dig_pos is None or scoped.timeout_dig_read_s < 1.0
            or min(deadline, scoped.deadline) - time.monotonic() <= DIG_RECOVERY_RESERVE_S
            or not scoped.release_timed_out_action()
        ):
            return False
        before = scoped.last_status
        try:
            stance = self.bot.status()
        except ApiError:
            return False
        hp = stance.get("health")
        if (
            stance.get("alive") is not True or stance.get("ready") is not True
            or stance.get("on_ground") is not True or stance.get("in_water") or stance.get("in_lava")
            or not isinstance(hp, (int, float)) or hp < min_hp
            or (stance.get("dimension"), stance.get("last_death")) != (before.get("dimension"), before.get("last_death"))
            or not before.get("pos") or not stance.get("pos") or _floor_pos(stance["pos"]) != _floor_pos(before["pos"])
        ):
            return False
        # The dig might have changed the cell before its reply was lost. A different heading
        # can be explored after release without replaying that mutation.
        self.skills._avoid_temporarily(scoped.timeout_dig_pos, "dig_timeout", DIG_TIMEOUT_COOLDOWN_S)
        scoped.timeout_path = None
        scoped.timeout_dig_pos = None
        scoped.timeout_dig_read_s = 0.0
        return True


    def _relocate_to_fresh_collar_site(
        self,
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any = None,
        deadline: float | None = None,
        min_hp_ref: list[float] | None = None,
    ) -> tuple[int, int, int]:
        cands = self._find_solid_collar_candidates(
            cur_pos,
            geofence=geofence,
            avoid_exhausted=True,
            radius=28,
        )
        for stand in cands[:5]:
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": stand[0] + 0.5,
                        "z": stand[2] + 0.5,
                        "range": 1.2,
                        "timeout": 8000,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                pass
            self.bot.status()
            self._move_exact(stand, timeout_ms=2500)
            st = self.bot.status()
            now_pos = _floor_pos(st["pos"])
            if not self._is_near_exhausted_collar(now_pos, min_dist=10.0):
                if self._probe_collar_dirs(now_pos, geofence=geofence):
                    self.relocations_count += 1
                    return now_pos

        ex_ref = self.exhausted_collars[-1] if self.exhausted_collars else cur_pos
        dir_order = sorted(
            ("north", "east", "south", "west"),
            key=lambda d: -(
                (cur_pos[0] + DIRECTIONS[d][0] * 16 - ex_ref[0]) ** 2
                + (cur_pos[2] + DIRECTIONS[d][1] * 16 - ex_ref[2]) ** 2
            ),
        )
        hp_ref = min_hp_ref if min_hp_ref is not None else [20.0]
        dl = deadline if deadline is not None else (time.monotonic() + 22.0)
        turned_after_timeout = False
        for d in dir_order:
            dx, dz = DIRECTIONS[d]
            if self._geofence_blocked(geofence, cur_pos[0] + dx * 16, cur_pos[2] + dz * 16, action="collar_relocate_drift") is not None:
                continue
            try:
                drift = self._dig_level_edge(
                    direction=d,
                    steps=16,
                    target_blocks=(),
                    parent_node_id=0,
                    geofence=geofence,
                    deadline=dl,
                    min_hp_ref=hp_ref,
                    min_target_tier=1,
                )
            except _MiningDeadline:
                if turned_after_timeout or not self._skip_stalled_first_dig(deadline=dl, min_hp=hp_ref[0]):
                    raise
                turned_after_timeout = True
                continue
            if int(drift["steps_done"]) >= 4:
                self.relocations_count += 1
                return tuple(drift["end_anchor"])

        return cur_pos


    def _ensure_solid_collar_anchor(
        self,
        cur_pos: tuple[int, int, int],
        *,
        geofence: Any,
        deadline: float,
        min_hp_ref: list[float],
    ) -> tuple[tuple[int, int, int], list[str], dict[str, Any] | None]:
        cache: dict[tuple[int, int, int], str] = {}
        pos = cur_pos
        near_ex = self._is_near_exhausted_collar(pos)
        under_pt = (pos[0], pos[1] - 1, pos[2])
        try:
            on_solid = self._is_solid_floor(under_pt, cache)
        except ApiError:
            on_solid = False

        if not on_solid and not near_ex:
            snapped = self._snap_from_hitbox_ledge_if_needed(pos, geofence=geofence, cache=cache)
            if snapped != pos:
                pos = snapped
                on_solid = True

        if on_solid and not near_ex:
            dirs0 = self._probe_collar_dirs(pos, geofence=geofence, cache=cache)
            if dirs0:
                return pos, dirs0, None

        pos = self._descend_tree_canopy_if_needed(pos)
        cache.clear()
        near_ex = self._is_near_exhausted_collar(pos)
        try:
            on_solid = self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), cache)
        except ApiError:
            on_solid = False
        if on_solid and not near_ex:
            dirs1 = self._probe_collar_dirs(pos, geofence=geofence, cache=cache)
            if dirs1:
                return pos, dirs1, None

        if near_ex:
            pos = self._relocate_to_fresh_collar_site(
                pos,
                geofence=geofence,
                deadline=deadline,
                min_hp_ref=min_hp_ref,
            )
            dirs_rel = self._probe_collar_dirs(pos, geofence=geofence)
            if dirs_rel and self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {}):
                return pos, dirs_rel, None

        cands = self._find_solid_collar_candidates(
            pos,
            geofence=geofence,
            avoid_exhausted=near_ex,
            radius=20,
        )
        for stand in cands[:5]:
            if time.monotonic() >= deadline:
                break
            try:
                self.bot.call(
                    "move_to",
                    {
                        "x": stand[0] + 0.5,
                        "z": stand[2] + 0.5,
                        "range": 1.2,
                        "timeout": 7000,
                        "stop_on": ["hurt", "death"],
                    },
                )
            except ApiError:
                self._clear_lateral_leaves_toward(stand)
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": stand[0] + 0.5,
                            "z": stand[2] + 0.5,
                            "range": 1.2,
                            "timeout": 5000,
                            "stop_on": ["hurt", "death"],
                        },
                    )
                except ApiError:
                    pass
            self.bot.status()
            self._move_exact(stand, timeout_ms=2200)
            st = self.bot.status()
            pos = _floor_pos(st["pos"])
            if self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {}):
                dirs_c = self._probe_collar_dirs(pos, geofence=geofence)
                if dirs_c:
                    return pos, dirs_c, None

        try:
            still_solid = self._is_solid_floor((pos[0], pos[1] - 1, pos[2]), {})
        except ApiError:
            still_solid = False
        if not still_solid:
            return pos, [], {"stop_reason": "no_solid_collar_ground"}

        blockers: dict[str, dict[str, Any]] = {}
        dirs_final = self._probe_collar_dirs(pos, geofence=geofence, blockers=blockers)
        if dirs_final:
            return pos, dirs_final, None
        return pos, [], {"stop_reason": "no_viable_collar_direction", "collar_blockers": blockers}


    def _try_restore_saved_shaft(
        self,
        cur_pos: tuple[int, int, int],
        *,
        min_hp_ref: list[float],
        allow_retired: bool = False,
        skip_collar: tuple[int, int, int] | None = None,
    ) -> bool:
        """Restore a nearby shaft. A blocked deep rejoin leaves its graph loaded."""
        best_sh: dict[str, Any] | None = None
        best_d = 36.0
        for sh in self.ledger.saved_shafts:
            anc_raw = sh.get("collar_anchor")
            if not isinstance(anc_raw, list) or len(anc_raw) < 3:
                continue
            anc = (int(anc_raw[0]), int(anc_raw[1]), int(anc_raw[2]))
            wet_shaft = self._water_interrupted_shaft
            if (
                anc == skip_collar
                or (not allow_retired and (
                    sh.get("retired") or anc in self.exhausted_collars
                    or (wet_shaft is not None and anc == wet_shaft.collar)
                ))
            ):
                continue
            if allow_retired and anc[1] <= cur_pos[1]:
                continue
            # Pending stairs still carry their original branch target layer.
            saved_target_y = sh.get("target_y")
            if not allow_retired and saved_target_y is not None and abs(int(saved_target_y) - self.target_y) > SCAN_BAND:
                continue
            hd = math.hypot(cur_pos[0] - anc[0], cur_pos[2] - anc[2])
            vertical_near = abs(cur_pos[1] - anc[1]) <= 20
            if allow_retired or not vertical_near or hd > 36.0:
                nodes = sh.get("nodes", [])
                edges = [
                    (edge.get("edge_kind"), [tuple(wp) for wp in edge.get("waypoints", [])])
                    for n in nodes if (edge := n.get("edge_from_parent"))
                ]
                points = [tuple(n["anchor"]) for n in nodes if n.get("anchor")]
                points.extend(wp for _, waypoints in edges for wp in waypoints)
                for n in nodes:
                    for branch in n.get("branches", []):
                        waypoints = branch.get("completed_waypoints") or []
                        if (branch.get("kind") == "branch_leaf" and branch.get("status") == "completed"
                                and waypoints and waypoints[0] == n.get("anchor")):
                            points.extend(tuple(wp) for wp in waypoints)
                if allow_retired and points:
                    hd = min(math.dist(cur_pos, pt) for pt in points)
                    vertical_near = hd <= 6.0
                elif cur_pos in points or any(
                    self._route_at_position(waypoints, cur_pos, kind) is not None for kind, waypoints in edges
                ):
                    hd = 0.0
                    vertical_near = True
            if hd <= best_d and vertical_near:
                best_d = hd
                best_sh = sh

        if best_sh is None:
            return False

        raw_nodes = best_sh.get("nodes") or []
        if not raw_nodes:
            return False
        restored: dict[int, ShaftNode] = {}
        for rn in raw_nodes:
            if isinstance(rn, dict):
                nd = ShaftNode.from_dict(rn)
                restored[nd.node_id] = nd
        collar_id = int(best_sh.get("collar_node_id", 1))
        if collar_id not in restored:
            return False

        if best_sh.get("from_disk"):
            collar_anc = restored[collar_id].anchor
            sample_wp: tuple[int, int, int] | None = None
            for nd in restored.values():
                if nd.edge_from_parent:
                    for wp in nd.edge_from_parent.waypoints:
                        if wp != collar_anc:
                            sample_wp = wp
                            break
                if sample_wp is not None:
                    break
            if sample_wp is not None:
                try:
                    if self._get_block_name(sample_wp) not in PASSABLE_BLOCKS:
                        self.ledger.saved_shafts = [s for s in self.ledger.saved_shafts if s is not best_sh]
                        return False
                except ApiError:
                    return False

        self.nodes = restored
        self.collar_node_id = collar_id
        # Restoring a saved shaft is a fresh attempt: don't inherit a hazard streak that would
        # make the next explore bail out as shaft_exhausted on its first branch.
        self.fluid_aborts = 0
        self.fluid_abandoned_deep = False
        if self.collar_ceiling_y is None or restored[collar_id].anchor[1] > self.collar_ceiling_y:
            self.collar_ceiling_y = int(restored[collar_id].anchor[1])
        self.next_node_id = max(restored.keys(), default=1) + 1
        active_id = int(best_sh.get("active_node_id") or collar_id)
        self.active_node_id = active_id if active_id in restored else collar_id
        if not allow_retired and not self._tree_has_pending_branches():
            self._clear_active_tree_only()
            return False
        if allow_retired:
            # A return trip only needs the route. Leave saved ores for explore
            # to verify when it restores a mining shaft.
            return True

        for o_pt, meta in list(self.ledger.discovered_ores.items()):
            if o_pt in self.mined_ore_positions or o_pt in self.deferred_ores:
                continue
            bname = str(meta.get("block") or "")
            if not bname:
                continue
            if meta.get("from_disk"):
                try:
                    if self._get_block_name(o_pt) != bname:
                        self.ledger.remove_ore(o_pt)
                        continue
                    meta["from_disk"] = False
                except ApiError:
                    continue
            near_nid = min(self.nodes.keys(), key=lambda nid: math.dist(o_pt, self.nodes[nid].anchor))
            self.deferred_ores[o_pt] = DeferredOreRecord(
                pos=o_pt,
                block_name=bname,
                required_tier=int(meta.get("required_tier", 2)),
                nearest_node_id=near_nid,
            )

        active = self.nodes[self.active_node_id]
        if self._has_stale_descent(self.target_y, observed_layers={point[1] for point in self.radar_targets}):
            return self._replan_shallower_layer(
                self.bot.status(), cur_pos, target_y=self.target_y, geofence=self._active_geofence,
            )
        if self._should_resume_at_depth(active, cur_pos):
            return self._resume_explore_at_depth(active, cur_pos, min_hp_ref=min_hp_ref)
        ok_nav, _ = self._resume_shaft_tip(active.node_id, min_hp_ref=min_hp_ref)
        if not ok_nav:
            self._clear_active_tree_only()
            return False
        return True


    def _scan_target_drops(self, expected_drops: set[str] | frozenset[str]) -> list[dict[str, Any]]:
        entities = self.bot.call("get_entities", {"radius": 6, "types": ["item"]}).get("entities") or []
        drops = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            label = entity.get("item")
            item = _normalize_drop_name(label)
            if item not in expected_drops or not isinstance(entity.get("id"), int):
                continue
            try:
                position = [float(value) for value in entity["pos"][:3]]
                _floor_pos(position)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            quantity = str(label).rpartition("x")[-1]
            count = max(1, int(quantity)) if quantity.isdecimal() and len(quantity) <= 4 else 1
            drops.append({"id": entity["id"], "item": item, "item_pos": position, "stack_count": count})
        return drops


    def _verify_drop_receipts(
        self,
        drops: list[dict[str, Any]],
        expected_drops: set[str] | frozenset[str],
        item: str,
        gain: int,
    ) -> None:
        remaining_ids = {drop["id"] for drop in self._scan_target_drops(expected_drops)}
        vanished = [drop for drop in drops if drop["item"] == item and drop["id"] not in remaining_ids]
        if not vanished:
            return
        position = self.bot.status()["pos"]
        nearby = [drop for drop in vanished if math.dist(position[:3], drop["item_pos"]) <= 1.5]
        for drop in vanished:
            self.drop_receipts[drop["id"]]["reason"] = "pickup_unconfirmed"
        if sum(drop["stack_count"] for drop in nearby) <= gain:
            for drop in nearby:
                self.drop_receipts[drop["id"]].update(reason="picked_up", picked_count=drop["stack_count"])


    def _level_walk_is_clear(self, start: tuple[int, int, int], end: tuple[int, int, int]) -> bool:
        if start[1] != end[1] or math.dist(start, end) > 4.0:
            return False
        route = _interpolate_cells(start, end)
        cache: dict[tuple[int, int, int], str] = {}

        def standable(cell: tuple[int, int, int]) -> bool:
            x, y, z = cell
            return (
                self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_local_walk") is None
                and self._get_block_name(cell, cache) in PASSABLE_BLOCKS
                and self._get_block_name((x, y + 1, z), cache) in PASSABLE_BLOCKS
                and self._is_solid_floor((x, y - 1, z), cache)
            )

        if not all(standable(cell) for cell in route):
            return False
        for prior, cell in zip(route, route[1:]):
            if prior[0] != cell[0] and prior[2] != cell[2] and not (
                standable((prior[0], cell[1], cell[2])) or standable((cell[0], cell[1], prior[2]))
            ):
                return False
        return True


    def _walk_level_detour(
        self, destination: tuple[int, int, int], *, min_hp: float, deadline: float,
        bypass: tuple[int, int, int] | None = None,
    ) -> tuple[bool, str | None]:
        """Try an open parallel passage one block beside a blocked level route."""
        if deadline - time.monotonic() < 3.0:
            return False, None

        def stance_error(status: dict[str, Any]) -> str | None:
            if not status.get("pos") or status.get("alive") is False or status.get("ready") is False:
                return "not_ready"
            hp = status.get("health")
            if isinstance(hp, (int, float)) and hp < min_hp:
                return "hurt"
            if status.get("on_ground") is not True or status.get("in_water") or status.get("in_lava"):
                return "not_grounded"
            return None

        try:
            stance = self.bot.status()
            if (error := stance_error(stance)) is not None:
                return False, error
            start = _floor_pos(stance["pos"])
            dx, dy, dz = (end - begin for begin, end in zip(start, destination))
            if dy or dx * dz or not 2 <= abs(dx) + abs(dz) <= 4:
                return False, None
            if bypass is not None and bypass not in _interpolate_cells(start, destination)[1:-1]:
                return False, None

            for side in (1, -1):
                ox, oz = (0, side) if dx else (side, 0)
                side_start = (start[0] + ox, start[1], start[2] + oz)
                side_end = (destination[0] + ox, destination[1], destination[2] + oz)
                corners = [start, side_start, side_end, destination]
                if all(self._level_walk_is_clear(a, b) for a, b in zip(corners, corners[1:])):
                    break
            else:
                return False, None
        except ApiError:
            return False, None

        for cell in [*_interpolate_cells(side_start, side_end), destination]:
            try:
                if not self._move_short_distance(cell, deadline=deadline):
                    return False, "movement_not_confirmed"
                stance = self.bot.status()
            except ApiError as exc:
                if exc.code in {"hurt", "death", "interrupted"}:
                    return False, "hurt" if exc.code == "hurt" else "not_ready"
                return False, "movement_not_confirmed"
            if (error := stance_error(stance)) is not None:
                return False, error
            if _floor_pos(stance["pos"]) != cell:
                return False, "movement_not_confirmed"
        return True, None


    def _move_short_distance(self, cell: tuple[int, int, int], *, deadline: float) -> bool:
        timeout = int(min(1.1, deadline - time.monotonic()) * 1000)
        if timeout < 200:
            return False
        x, y, z = cell
        self.bot.call("move_to", {
            "x": x + 0.5, "y": y, "z": z + 0.5, "range": 0.3,
            "timeout": timeout, "stop_on": ["hurt", "death"],
        })
        return True


    def _open_drop_pocket_base(
        self,
        base: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> None:
        x, y, z = base
        if (
            self._get_block_name(base, cache) not in PASSABLE_BLOCKS | FLUID_BLOCKS | UTILITY_BLOCKS | {"bedrock"}
            and self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_pickup") is None
            and self._is_solid_floor((x, y - 1, z), cache)
            and self._adjacent_fluid_or_falling([base], cache) is None
        ):
            self._dig_cell_safely(base, collect_rock=False, parent_node_id=0, cache=cache)


    def _collect_and_verify_target_drop(
        self,
        *,
        expected_drops: set[str] | frozenset[str],
        before_count: int,
        ore_pt: tuple[int, int, int],
        stand_y: int,
        cache: dict[tuple[int, int, int], str],
        can_open_lower: bool = False,
        allow_ore_step: bool = False,
    ) -> bool:
        """Verify inventory gain and check for reachable split stacks before leaving the ore."""
        scoped = self._budget_session
        started = time.monotonic()
        outer_deadline = scoped.deadline if scoped is not None else self._deadline or math.inf
        allowance = min(3.0, scoped.pickup_budget_remaining) if scoped is not None else 3.0
        pickup_deadline = min(outer_deadline, started + allowance)
        collect_until = pickup_deadline - min(0.5, allowance / 5)
        if scoped is not None:
            scoped.deadline = collect_until
        try:
            counts = self._check_target_drop_positions(
                expected_drops=expected_drops, before_count=before_count, ore_pt=ore_pt,
                stand_y=stand_y, cache=cache, can_open_lower=can_open_lower, collect_until=collect_until,
                allow_ore_step=allow_ore_step,
            )
        except _MiningDeadline:
            # A timed-out observation can be skipped. An action may still hold the
            # server lease, in which case the outer slice must stop and preserve its progress.
            if scoped is None:
                raise
            if scoped.timeout_path is not None and scoped.timeout_path not in scoped._RETRYABLE_PROBES:
                scoped.pickup_stop_deadline = pickup_deadline
                raise
            if pickup_deadline >= outer_deadline:
                raise
            counts = _inventory_counts_from_raw(scoped.last_inventory or {})
            scoped.timeout_path = None
        finally:
            if scoped is not None:
                scoped.deadline = outer_deadline
                scoped.pickup_budget_remaining = max(0.0, scoped.pickup_budget_remaining - (time.monotonic() - started))
        gained = sum(counts.get(it, 0) for it in expected_drops) > before_count
        if gained:
            self.verified_ore_pickups += 1
        return gained

