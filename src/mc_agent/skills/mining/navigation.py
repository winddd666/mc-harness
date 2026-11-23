"""MiningDFSEngine mixin: navigation (split from legacy mining_dfs.py)."""

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


class _NavigationMixin:
    def reset(self, *, geofence: Any = None) -> dict[str, Any]:
        if not self.nodes and self.collar_node_id is None:
            # Live s07/s08: an empty tree made reset a free "ok" no-op, so the model pressed it
            # 250+ times at ~3.5s/turn (it was drowning and looking for any way out).
            return {
                "ok": False,
                "requires_replan": True,
                "action": "reset",
                "strategy": self.strategy,
                "stop_reason": "nothing_to_reset",
                "message": (
                    "当前没有可重置的矿井（空间树为空），reset 不会改变任何状态。"
                    "若被困或在水里：先用 mc_skill_unstick，或 mc_skill_staircase(vertical='up') 爬到岸上，"
                    "再直接 explore 开新井。"
                ),
                "tree": self.snapshot(),
            }
        collar = self.nodes.get(self.collar_node_id)
        active = self.nodes.get(self.active_node_id)
        reset_origin = collar or active
        had_collar = reset_origin.anchor if reset_origin is not None else None

        deep_ceiling = self.collar_ceiling_y
        ref_y: int | None = None
        if had_collar is not None:
            ref_y = had_collar[1]
        if deep_ceiling is not None:
            ref_y = int(deep_ceiling) if ref_y is None else max(ref_y, int(deep_ceiling))
        if ref_y is not None:
            try:
                st_chk = self.bot.status()
                if st_chk.get("pos") and _floor_pos(st_chk["pos"])[1] <= ref_y - 20:
                    now_y = _floor_pos(st_chk["pos"])[1]
                    # Underground reset would orphan the tree with no surface anchor and no shaft
                    # to climb; live s01 burned 8 resets at y=-54 and never found ore, and s03
                    # cleared the tree at y=8 then spun on no_progress.
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "action": "reset",
                        "strategy": self.strategy,
                        "stop_reason": "reset_needs_surface",
                        "message": (
                            f"你当前在 y={now_y}，比本局最高井口 (y={ref_y}) 低 20 格以上，不能就地重置。"
                            "如果只是这条支巷挖干了，直接 action='explore' 会继续开支巷；"
                            "如果要换井：" + self._surface_climb_hint()
                        ),
                        "tree": self.snapshot(),
                    }
            except (ApiError, ValueError, KeyError):
                pass

        for node in (collar, active):
            if node is not None:
                if node.anchor not in self.exhausted_collars:
                    self.exhausted_collars.append(node.anchor)
                self.ledger.record_exhausted_collar(node.anchor)
        self._clear_active_tree_only()
        self.deferred_ores.clear()
        self.radar_targets.clear()
        self.ever_seen_radar_ores.clear()
        self.locked_cluster = None
        self.catch_blocks_placed.clear()
        self.bridge_blocks_placed.clear()
        self.stone_pick_rock_equips = 0
        self.ore_pick_equips = 0
        self.dug_during_return = 0
        # A reset starts a fresh shaft: a stale hazard streak made the very next explore
        # return shaft_exhausted instantly (live s03: reset -> explore -> shaft_exhausted x6).
        self.fluid_aborts = 0
        self.fluid_abandoned_deep = False

        relocated_from: list[int] | None = None
        relocated_to: list[int] | None = None
        if had_collar is not None:
            try:
                st = self.bot.status()
                if st.get("alive") is not False and st.get("ready") is not False and st.get("pos"):
                    cur_pos = _floor_pos(st["pos"])
                    if self._is_near_exhausted_collar(cur_pos):
                        relocated_from = list(cur_pos)
                        new_pos = self._relocate_to_fresh_collar_site(cur_pos, geofence=geofence)
                        if new_pos != cur_pos:
                            relocated_to = list(new_pos)
            except (ApiError, ValueError, KeyError):
                pass
        self.ledger.save()
        out: dict[str, Any] = {
            "ok": True,
            "action": "reset",
            "strategy": self.strategy,
            "message": (
                f"MiningDFS ({self.strategy}) 矿井空间树已重置并自动换点至 {relocated_to}。"
                if relocated_to is not None
                else f"MiningDFS ({self.strategy}) 矿井空间树已重置（如需换井请确保已离开旧井口 >=14 格）。"
            ),
        }
        if relocated_from is not None:
            out["relocated_from"] = relocated_from
        if relocated_to is not None:
            out["relocated_to"] = relocated_to
        return out


    def snapshot(self) -> dict[str, Any]:
        node_summaries = [
            {
                "node_id": n.node_id,
                "kind": n.kind,
                "anchor": list(n.anchor),
                "parent_id": n.parent_id,
                "edge_kind": n.edge_from_parent.edge_kind if n.edge_from_parent else None,
                "pending_branches": [
                    {"kind": b.kind, "dir": b.direction, "steps": b.steps}
                    for b in n.branches
                    if b.status == "pending"
                ],
                "pruned_branches": [
                    {"kind": b.kind, "dir": b.direction, "reason": b.prune_reason}
                    for b in n.branches
                    if b.status == "pruned"
                ],
            }
            for n in self.nodes.values()
        ]
        snap: dict[str, Any] = {
            "strategy": self.strategy,
            "collar_node_id": self.collar_node_id,
            "active_node_id": self.active_node_id,
            "target_y": self.target_y,
            "node_count": len(self.nodes),
            "dry_calls": self.dry_calls,
            "nodes": node_summaries[-12:],
            "deferred_ores": [
                {
                    "pos": list(rec.pos),
                    "block": rec.block_name,
                    "required_tier": rec.required_tier,
                    "nearest_node_id": rec.nearest_node_id,
                }
                for rec in self.deferred_ores.values()
            ],
            "radar_targets": [list(p) for p in self.radar_targets[:8]],
            "radar_ever_seen_count": len(self.ever_seen_radar_ores),
            "exhausted_collars": [list(p) for p in self.exhausted_collars[-6:]],
            "sealed_count": len(self.sealed_positions),
            "catch_blocks_placed": len(self.catch_blocks_placed),
            "bridge_blocks_placed": len(self.bridge_blocks_placed),
            "stone_pick_rock_equips": self.stone_pick_rock_equips,
            "ore_pick_equips": self.ore_pick_equips,
            "relocations_count": self.relocations_count,
            "surgical_extractions": self.surgical_extractions,
            "verified_ore_pickups": self.verified_ore_pickups,
        }
        if self.locked_cluster is not None:
            snap["locked_cluster"] = {
                "rep": list(self.locked_cluster["rep"]),
                "size": int(self.locked_cluster["size"]),
                "mob_nearby": bool(self.locked_cluster.get("mob_nearby", False)),
            }
        return snap


    def _record_excavated(self, pt: tuple[int, int, int]) -> None:
        self.excavated_positions.add(pt)
        self.ledger.record_excavated(pt)


    def _record_corridor(self, pt: tuple[int, int, int]) -> None:
        self.known_corridor_positions.add(pt)
        self.known_corridor_positions.add((pt[0], pt[1] + 1, pt[2]))
        self.ledger.record_corridor(pt)


    def _is_self_cavity(self, pts: list[tuple[int, int, int]] | tuple[tuple[int, int, int], ...]) -> bool:
        return any(p in self.excavated_positions or p in self.known_corridor_positions for p in pts)


    def _is_near_exhausted_collar(self, pos: tuple[int, int, int], *, min_dist: float = 12.0) -> bool:
        for ex in self.exhausted_collars:
            if math.hypot(pos[0] - ex[0], pos[2] - ex[2]) < min_dist and abs(pos[1] - ex[1]) <= 18:
                return True
        return False


    def _get_block_name(self, pt: tuple[int, int, int], cache: dict[tuple[int, int, int], str] | None = None) -> str:
        if cache is not None and pt in cache:
            return cache[pt]
        res = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
        raw = res.get("name") or res.get("block")
        if not raw:
            raise ApiError("unknown_block", f"无法确认方块 {list(pt)}")
        name = str(raw).removeprefix("minecraft:")
        if cache is not None:
            cache[pt] = name
        if name in {"crafting_table", "furnace"}:
            self.ledger.record_utility(name, pt)
        return name


    def _scan_hostile_positions(self, radius: int = 32) -> list[tuple[float, float, float]]:
        try:
            res = self.bot.call("get_entities", {"radius": radius, "types": ["hostile"]})
            ents = res.get("entities") or []
        except ApiError:
            return []
        out: list[tuple[float, float, float]] = []
        for e in ents:
            if not isinstance(e, dict):
                continue
            p = e.get("pos")
            if isinstance(p, (list, tuple)) and len(p) >= 3:
                out.append((float(p[0]), float(p[1]), float(p[2])))
        return out


    def _find_offhand_swap_item(self, inv: dict[str, Any], *, max_exclude_tier: int = 3) -> str | None:
        items = [
            r
            for r in (inv.get("items") or [])
            if isinstance(r, dict) and r.get("name") and int(r.get("count") or 1) > 0
        ]
        counts: dict[str, int] = {}
        for r in items:
            n = str(r["name"]).removeprefix("minecraft:")
            counts[n] = counts.get(n, 0) + int(r.get("count") or 1)
        seal = _pick_seal_material(counts)
        if seal:
            return seal
        non_pick = [
            str(r["name"]).removeprefix("minecraft:")
            for r in items
            if str(r["name"]).removeprefix("minecraft:") not in PICKAXE_TIERS
        ]
        if non_pick:
            return non_pick[0]
        lower_pick = [
            str(r["name"]).removeprefix("minecraft:")
            for r in items
            if PICKAXE_TIERS.get(str(r["name"]).removeprefix("minecraft:"), 99) < max_exclude_tier
        ]
        return lower_pick[0] if lower_pick else None


    def _equip_for_block_tier(
        self,
        required_tier: int,
        *,
        for_ore: bool = False,
        inv_snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, int, int]:
        """Protect 1 high-tier pickaxe in offhand while mining rock; equip required tier on demand."""
        inv = inv_snapshot if isinstance(inv_snapshot, dict) else self.bot.inventory()
        picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
        held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None

        if required_tier <= 2:
            low_main = [p for p in picks if not p.get("in_offhand") and int(p["tier"]) <= 2]
            high_main = [p for p in picks if not p.get("in_offhand") and int(p["tier"]) >= 3]
            high_off = [p for p in picks if p.get("in_offhand") and int(p["tier"]) >= 3]
            if low_main and high_main and not high_off:
                high_keep = max(high_main, key=lambda p: (int(p["tier"]), int(p["remaining"])))
                try:
                    self.bot.call("equip", {"item": high_keep["name"], "slot": "off-hand"})
                    inv = self.bot.inventory()
                    picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
                    held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None
                except ApiError:
                    pass

        chosen = _choose_pickaxe_for_tier(picks, required_tier, prefer_stone=not for_ore)
        if chosen is not None and chosen.get("in_offhand"):
            swap_item = self._find_offhand_swap_item(inv, max_exclude_tier=int(chosen["tier"]))
            if swap_item:
                try:
                    self.bot.call("equip", {"item": swap_item, "slot": "off-hand"})
                    inv = self.bot.inventory()
                    picks, max_tier, total_rem = _pickaxe_inventory_summary(inv)
                    held_name = str((inv.get("held") or {}).get("name") or "").removeprefix("minecraft:") or None
                    chosen = _choose_pickaxe_for_tier(picks, required_tier, prefer_stone=not for_ore)
                except ApiError:
                    pass

        if chosen is not None and (chosen.get("in_offhand") or chosen["name"] != held_name):
            try:
                self.bot.call("equip", {"item": chosen["name"], "slot": "hand"})
                held_name = chosen["name"]
                if for_ore:
                    self.ore_pick_equips += 1
                elif chosen["name"] in {"stone_pickaxe", "wooden_pickaxe"}:
                    self.stone_pick_rock_equips += 1
            except ApiError:
                pass
        return chosen, held_name, max_tier, total_rem


    def _geofence_blocked(self, geofence: Any, x: float, z: float, *, action: str) -> dict[str, Any] | None:
        if geofence is None:
            return None
        return geofence.check_point(x + 0.5, z + 0.5, action=action)


    def _inspect_step_hazards(
        self,
        targets: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        *,
        forward_delta: tuple[int, int] | None = None,
    ) -> tuple[tuple[str, dict[str, Any]] | None, list[tuple[int, int, int]]]:
        """Distinguish in-path fluid/bedrock hazards from sealable lateral/overhead fluids."""
        if not targets:
            return None, []
        target_set = set(targets)
        sealable_fluids: list[tuple[int, int, int]] = []
        seen_fluids: set[tuple[int, int, int]] = set()
        top_target = targets[0]

        for p in targets:
            b = self._get_block_name(p, cache)
            if b in FLUID_BLOCKS or b == "bedrock":
                return ("fluid_or_bedrock", {"pos": list(p), "block": b, "relation": "in_path"}), []
            if b == "pointed_dripstone":
                return ("falling_material_adjacent", {"pos": list(p), "block": b, "relation": "in_path"}), []
            for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, -1)):
                nb = (p[0] + ax, p[1] + ay, p[2] + az)
                if nb in target_set:
                    continue
                nb_name = self._get_block_name(nb, cache)
                if ay == 1 and nb_name == "pointed_dripstone":
                    return (
                        "falling_material_adjacent",
                        {"pos": list(nb), "block": nb_name, "relation": "overhead", "target": list(p)},
                    ), []
                if nb_name in FLUID_BLOCKS:
                    if forward_delta is not None and (ax, az) == forward_delta and nb_name in LAVA_BLOCKS:
                        return (
                            "fluid_or_bedrock",
                            {"pos": list(nb), "block": nb_name, "relation": "forward_lava", "target": list(p)},
                        ), []
                    if nb not in seen_fluids:
                        seen_fluids.add(nb)
                        sealable_fluids.append(nb)

        above1 = (top_target[0], top_target[1] + 1, top_target[2])
        above2 = (top_target[0], top_target[1] + 2, top_target[2])
        a1_name = self._get_block_name(above1, cache)
        if a1_name in FALLING_BLOCKS and self._get_block_name(above2, cache) in FALLING_BLOCKS:
            return (
                "falling_material_adjacent",
                {
                    "pos": list(above1),
                    "block": a1_name,
                    "relation": "overhead_falling_column",
                    "target": list(top_target),
                },
            ), sealable_fluids
        return None, sealable_fluids


    def _adjacent_fluid_or_falling(
        self,
        targets: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[str, dict[str, Any]] | None:
        hazard, adj_fluids = self._inspect_step_hazards(targets, cache)
        if hazard is not None:
            return hazard
        if adj_fluids:
            nb = adj_fluids[0]
            return "fluid_or_bedrock", {"pos": list(nb), "block": cache.get(nb, "fluid"), "relation": "adjacent_fluid"}
        return None


    def _is_solid_floor(self, pt: tuple[int, int, int], cache: dict[tuple[int, int, int], str]) -> bool:
        b = self._get_block_name(pt, cache)
        if b in PASSABLE_BLOCKS | FLUID_BLOCKS | HAZARD_FLOOR_BLOCKS | FLIMSY_FLOOR_BLOCKS:
            return False
        if b.endswith(FLIMSY_FLOOR_SUFFIXES):
            return False
        if b in FALLING_BLOCKS:
            below = self._get_block_name((pt[0], pt[1] - 1, pt[2]), cache)
            if below in PASSABLE_BLOCKS | FLUID_BLOCKS | FLIMSY_FLOOR_BLOCKS or below.endswith(FLIMSY_FLOOR_SUFFIXES):
                return False
        return True


    def _dig_cell_safely(
        self,
        dpos: tuple[int, int, int],
        *,
        collect_rock: bool,
        parent_node_id: int,
        cache: dict[tuple[int, int, int], str],
        verify_pickaxe: bool = False,
    ) -> tuple[bool, str | None, bool, int]:
        """Dig one corridor cell safely; if it is a tracked ore, verify real drop pickup."""
        deferred_added = 0
        mined_ore = False
        stale_passable = cache.get(dpos) in PASSABLE_BLOCKS | FALLING_BLOCKS
        bname = self._get_block_name(dpos, None if stale_passable else cache)
        if bname in PASSABLE_BLOCKS:
            cache[dpos] = bname
            return True, None, False, 0
        if bname == "pointed_dripstone":
            return False, "falling_material_adjacent", False, 0
        avoidance = self.skills.temporary_avoidance.get(dpos)
        if avoidance and avoidance.get("reason") == "dig_timeout" and self.skills._is_block_avoided(dpos):
            return False, "dig_cell_avoided", False, 0

        is_tracked = bname in TRACKED_ORES
        expected_ore_drops = _expected_drops_for_blocks([bname]) if is_tracked else frozenset()
        before_ore_cnt = 0
        if is_tracked and expected_ore_drops:
            inv_pre, counts_pre = _inventory_snapshot_and_counts(self.bot)
            if _compute_empty_slots(inv_pre) < 2 and not _can_stack_expected_drop(inv_pre, expected_ore_drops):
                inv_pre, _ = self.skills._ensure_free_slots(min_free=4, inv_snapshot=inv_pre)
                _, counts_pre = _inventory_snapshot_and_counts(self.bot)
            before_ore_cnt = sum(int(counts_pre.get(it, 0)) for it in expected_ore_drops)

        for _ in range(5):
            req_t = int(BLOCK_MIN_PICKAXE_TIER.get(bname, 1))
            if req_t > 1:
                chosen_o, _, max_t, _ = self._equip_for_block_tier(req_t, for_ore=(bname in TRACKED_ORES))
                if chosen_o is None or max_t < req_t:
                    if bname in TRACKED_ORES and dpos not in self.deferred_ores:
                        self.deferred_ores[dpos] = DeferredOreRecord(dpos, bname, req_t, parent_node_id)
                        self.ledger.record_ore(dpos, bname, req_t)
                        deferred_added += 1
                    return False, "insufficient_pickaxe_tier", mined_ore, deferred_added
            elif verify_pickaxe:
                chosen, held, _, _ = self._equip_for_block_tier(1)
                if chosen is None or held not in PICKAXE_TIERS:
                    return False, "pickaxe_low", mined_ore, deferred_added

            is_tracked = bname in TRACKED_ORES
            should_collect = bool(is_tracked or collect_rock)
            retried_tool = False
            while True:
                try:
                    self.bot.call("dig", {"pos": [dpos[0], dpos[1], dpos[2]], "collect": should_collect, "stop_on": ["hurt", "death"]})
                    cache[dpos] = "air"
                    self._record_excavated(dpos)
                    if is_tracked:
                        self.mined_ore_positions.add(dpos)
                        self.ledger.remove_ore(dpos)
                        if expected_ore_drops:
                            gained_ok = self._collect_and_verify_target_drop(
                                expected_drops=expected_ore_drops,
                                before_count=before_ore_cnt,
                                ore_pt=dpos,
                                stand_y=dpos[1],
                                cache=cache,
                                can_open_lower=False,
                            )
                            mined_ore = gained_ok
                        else:
                            mined_ore = True
                    break
                except ApiError as err:
                    if err.code == "nothing_to_dig":
                        cache[dpos] = "air"
                        self._record_excavated(dpos)
                        return True, None, mined_ore, deferred_added
                    if err.code in {"hurt", "death", "interrupted", "protected_region"}:
                        return False, ("hurt" if err.code in {"hurt", "death"} else err.code), mined_ore, deferred_added
                    if not retried_tool:
                        retried_tool = True
                        st = self.bot.status()
                        if st.get("alive") is False:
                            return False, "not_ready", mined_ore, deferred_added
                        cache.pop(dpos, None)
                        live_blk = self._get_block_name(dpos, cache)
                        if live_blk in PASSABLE_BLOCKS:
                            return True, None, mined_ore, deferred_added
                        bname = live_blk
                        self._equip_for_block_tier(int(BLOCK_MIN_PICKAXE_TIER.get(bname, 1)), for_ore=(bname in TRACKED_ORES))
                        continue
                    return False, "dig_failed", mined_ore, deferred_added

            above_pt = (dpos[0], dpos[1] + 1, dpos[2])
            if bname in FALLING_BLOCKS or self._get_block_name(above_pt, cache) in FALLING_BLOCKS:
                time.sleep(0.14)
                cache.pop(dpos, None)
                cache.pop(above_pt, None)
                refill = self._get_block_name(dpos, cache)
                if refill in PASSABLE_BLOCKS:
                    return True, None, mined_ore, deferred_added
                if refill in FLUID_BLOCKS:
                    return False, "fluid_or_bedrock", mined_ore, deferred_added
                bname = refill
                continue
            return True, None, mined_ore, deferred_added

        return False, "falling_material_adjacent", mined_ore, deferred_added


    def _seal_breach(
        self,
        seal_coords: list[tuple[int, int, int]],
        cache: dict[tuple[int, int, int], str],
        *,
        allow_corridor: bool = False,
    ) -> list[list[int]]:
        """Place solid seal blocks at exposed air/fluid openings while preserving return corridors."""
        placed: list[list[int]] = []
        counts = _inventory_counts(self.bot)
        filler = _pick_seal_material(counts)
        if not filler:
            return placed
        replaceable = (PASSABLE_BLOCKS | FLUID_BLOCKS) - {"torch", "wall_torch"}
        for pt in seal_coords:
            if pt in self.sealed_positions:
                continue
            if not allow_corridor and pt in self.known_corridor_positions:
                continue
            cache.pop(pt, None)
            try:
                cur = self._get_block_name(pt, cache)
            except ApiError:
                continue
            if cur not in replaceable:
                continue
            try:
                st = self.bot.status()
                bpos = _floor_pos(st.get("pos") or [0, 0, 0])
                if pt in {bpos, (bpos[0], bpos[1] + 1, bpos[2])}:
                    continue
            except (ApiError, ValueError):
                continue
            try:
                self.bot.call("place", {"item": filler, "pos": [pt[0], pt[1], pt[2]]})
                cache[pt] = filler
                self.sealed_positions.add(pt)
                placed.append([pt[0], pt[1], pt[2]])
            except ApiError:
                counts = _inventory_counts(self.bot)
                filler = _pick_seal_material(counts)
                if not filler:
                    break
        return placed


    def _place_bridge_block(
        self,
        support_pt: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> bool:
        """Place a solid floor/parapet block across self-dug cavities, gaps, or window sills."""
        counts = _inventory_counts(self.bot)
        filler = _pick_seal_material(counts)
        if not filler:
            return False
        cache.pop(support_pt, None)
        try:
            cur = self._get_block_name(support_pt, cache)
        except ApiError:
            return False
        if self._is_solid_floor(support_pt, cache):
            return True
        replaceable = (PASSABLE_BLOCKS | FLUID_BLOCKS | FLIMSY_FLOOR_BLOCKS) - {"torch", "wall_torch"}
        if cur.endswith(FLIMSY_FLOOR_SUFFIXES) and cur not in replaceable:
            try:
                self.bot.call("dig", {"pos": [support_pt[0], support_pt[1], support_pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                cache[support_pt] = "air"
            except ApiError:
                return False
        elif cur not in replaceable:
            return False
        try:
            self.bot.call("place", {"item": filler, "pos": [support_pt[0], support_pt[1], support_pt[2]]})
            cache[support_pt] = filler
            self.bridge_blocks_placed.append([support_pt[0], support_pt[1], support_pt[2]])
            return True
        except ApiError:
            return False


    def _settled_position(self, cur: list[float] | None = None) -> tuple[list[float], bool]:
        pos = list(cur) if cur else [0.0, 64.0, 0.0]
        on_ground = True
        for _ in range(6):
            st = self.bot.status()
            if st.get("pos"):
                pos = [float(v) for v in st["pos"][:3]]
            if st.get("in_water") or st.get("in_lava") or st.get("alive") is False or st.get("ready") is False:
                return pos, False
            on_ground = bool(st.get("on_ground", True))
            if on_ground:
                break
            time.sleep(0.08)
        return pos, on_ground


    def _move_exact(self, pt: tuple[int, int, int], *, timeout_ms: int = 4500) -> bool:
        fx, fy, fz = pt[0] + 0.5, float(pt[1]), pt[2] + 0.5
        try:
            self.bot.call("move_to", {"x": fx, "y": fy, "z": fz, "range": 0.35, "timeout": timeout_ms, "stop_on": ["hurt", "death"]})
        except ApiError:
            pass
        cur, on_ground = self._settled_position()
        if not on_ground:
            return False
        if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.78:
            self._record_corridor(pt)
            return True

        if math.dist((cur[0], cur[2]), (fx, fz)) <= 2.6:
            need_jump = fy > cur[1] + 0.25
            try:
                self.bot.call("look", {"x": fx, "y": fy + 0.4, "z": fz})
                ctrl_body: dict[str, Any] = {
                    "forward": True,
                    "ms": 540 if need_jump else 420,
                    "stop_on": ["hurt", "death"],
                }
                if need_jump:
                    ctrl_body["jump"] = True
                self.bot.call("control", ctrl_body)
            except ApiError:
                pass
            time.sleep(0.08)
            cur, on_ground = self._settled_position(cur)
            if not on_ground:
                return False
            if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.85:
                self._record_corridor(pt)
                return True

        if math.dist((cur[0], cur[2]), (fx, fz)) <= 2.6:
            try:
                self.bot.call("move_to", {"x": fx, "y": fy, "z": fz, "range": 0.3, "timeout": 2500, "stop_on": ["hurt", "death"]})
            except ApiError:
                pass
            cur, on_ground = self._settled_position(cur)
            if math.floor(cur[1]) == pt[1] and math.dist((cur[0], cur[2]), (fx, fz)) <= 0.85 and on_ground:
                self._record_corridor(pt)
                return True
        return False


    def _step_to(
        self,
        cell: tuple[int, int, int],
        *,
        from_cell: tuple[int, int, int] | None = None,
        repair_support: bool = False,
    ) -> bool:
        """One-cell traversal that also handles step-ups, gaps and 1-block drops.

        Live n0112: repeated `backtrack_blocked@...,(feet=air,head=air)` — nothing obstructed,
        move_exact simply cannot jump a gap or a 1-block ledge, so the whole route was abandoned.
        """
        # On a recorded ascending stair, try the local jump before asking the
        # pathfinder to solve the entire adjacent step.
        jump_first = (
            from_cell is not None and cell[1] == from_cell[1] + 1
            and abs(cell[0] - from_cell[0]) + abs(cell[2] - from_cell[2]) == 1
        )
        if jump_first and self._hop_up_to(cell, repair_support=repair_support):
            return True
        if self._move_exact(cell):
            return True
        if not jump_first and self._hop_up_to(cell):
            return True
        try:
            pos, grounded = self._settled_position()
        except (ApiError, TypeError, ValueError):
            return False
        if not grounded:
            return False
        dx = cell[0] + 0.5 - float(pos[0])
        dz = cell[2] + 0.5 - float(pos[2])
        if max(abs(dx), abs(dz)) > 1.6:
            return False
        yaw = math.atan2(-dx, -dz)
        for jump in (True, False):
            try:
                self.bot.call(
                    "control",
                    {"forward": True, "jump": jump, "yaw": yaw, "ms": 650, "stop_on": ["hurt", "death"]},
                )
            except ApiError as exc:
                if exc.code in {"hurt", "death", "dead", "not_ready", "disconnected"}:
                    return False
            try:
                position, grounded = self._settled_position(pos)
            except (ApiError, TypeError, ValueError):
                return False
            if not grounded:
                return False
            if _floor_pos(position) == cell:
                return True
        return False


    def _hop_up_to(self, cell: tuple[int, int, int], *, ms: int = 700, repair_support: bool = False) -> bool:
        """Jump-step onto an air cell one block higher (move_exact cannot jump).

        Live s19: the route block was reported as feet=air,head=air — nothing was obstructing,
        the bot simply refused a 1-block step-up, which aborted the whole walk home.
        """
        try:
            st = self.bot.status()
            if (
                not st.get("on_ground", True) or st.get("in_water") or st.get("in_lava")
                or st.get("alive") is False or st.get("ready") is False
            ):
                return False
            pos = st.get("pos") or [0.0, 64.0, 0.0]
            cur = (math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2])))
        except (ApiError, TypeError, ValueError):
            return False
        if cell[1] - cur[1] != 1 or max(abs(cell[0] - cur[0]), abs(cell[2] - cur[2])) > 1:
            return False
        x, y, z = cell
        support = (x, y - 1, z)
        cache: dict[tuple[int, int, int], str] = {}
        try:
            if (
                self._get_block_name(cell) not in PASSABLE_BLOCKS
                or self._get_block_name((x, y + 1, z)) not in PASSABLE_BLOCKS
            ):
                return False
            if not self._is_solid_floor(support, cache):
                # Repair an open, recorded stair from the neighboring step. The
                # bot can already be standing in the missing support cell.
                if (
                    not repair_support or support == cur or cache.get(support) not in AIR_BLOCKS
                    or self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                    or not self._is_solid_floor((cur[0], cur[1] - 1, cur[2]), cache)
                    or self._adjacent_fluid_or_falling([(x, y + 1, z), cell, support], cache) is not None
                ):
                    return False
                if not self._place_bridge_block(support, cache):
                    return False
                cache.pop(support, None)
                if not self._is_solid_floor(support, cache):
                    return False
        except ApiError:
            return False
        dx = cell[0] + 0.5 - float(pos[0])
        dz = cell[2] + 0.5 - float(pos[2])
        yaw = math.atan2(-dx, -dz)
        try:
            self.bot.call(
                "control",
                {"forward": True, "jump": True, "yaw": yaw, "ms": ms, "stop_on": ["hurt", "death"]},
            )
        except ApiError:
            return False
        try:
            position, grounded = self._settled_position(pos)
        except (ApiError, TypeError, ValueError):
            return False
        if grounded and _floor_pos(position) == cell:
            self._record_corridor(cell)
            return True
        return False


    def _pillar_to_recorded_step(
        self, cell: tuple[int, int, int], *, deadline: float | None, min_hp: float
    ) -> bool:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        x, y, z = cell
        support = (x, y - 1, z)
        try:
            st = self.bot.status()
            if (
                st.get("alive") is False or st.get("ready") is False
                or not st.get("on_ground", True) or st.get("health", min_hp) < min_hp
                or _floor_pos(st.get("pos")) != support
                or self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                or self.skills._is_block_avoided(support)
            ):
                return False
            cache: dict[tuple[int, int, int], str] = {}
            if any(self._get_block_name(pt, cache) not in AIR_BLOCKS for pt in (support, cell, (x, y + 1, z))):
                return False
            if not self._is_solid_floor((x, y - 2, z), cache):
                return False
            result = self.skills.dig_staircase(vertical="pillar", steps=1, stop_on_ore=False, deadline=deadline)
            self.dug_during_return += int(result.get("dug_count", 0))
            if result.get("placed_count"):
                self.bridge_blocks_placed.append(list(support))
            position, grounded = self._settled_position(st["pos"])
        except (ApiError, TypeError, ValueError):
            return False
        if grounded and _floor_pos(position) == cell:
            self._record_corridor(cell)
            return True
        return False


    def _drop_to_recorded_step(self, cell: tuple[int, int, int], *, min_hp: float) -> bool:
        x, y, z = cell
        cache: dict[tuple[int, int, int], str] = {}
        try:
            stance = self.bot.status()
            if (
                stance.get("alive") is False or stance.get("ready") is False
                or not stance.get("on_ground", True) or stance.get("health", min_hp) < min_hp
                or _floor_pos(stance["pos"]) != (x, y + 1, z)
                or self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None
                or self.skills._is_block_avoided(cell)
            ):
                return False
            if (
                self._get_block_name(cell, cache) in PASSABLE_BLOCKS | FLUID_BLOCKS | UTILITY_BLOCKS | {"bedrock"}
                or self._get_block_name((x, y + 1, z), cache) not in PASSABLE_BLOCKS
                or not self._is_solid_floor((x, y - 1, z), cache)
                or self._adjacent_fluid_or_falling([cell], cache) is not None
            ):
                return False
            self.bot.call("dig", {"pos": list(cell), "collect": False, "stop_on": ["hurt", "death"]})
            self.dug_during_return += 1
            pos, on_ground = self._settled_position(stance["pos"])
        except ApiError:
            return False
        if not on_ground or _floor_pos(pos) != cell:
            return False
        self._record_corridor(cell)
        return True


    def _clear_route_obstruction(self, target_wp: tuple[int, int, int]) -> int:
        """Re-open a recorded corridor cell: clear ordinary fill while preserving fluids and utility blocks.

        Returns the number of blocks dug, or -1 when the route is unrecoverable here.
        The cell belongs to our own carved corridor, so clearing it cannot open new terrain.
        """
        x, y, z = target_wp
        cells = (target_wp, (x, y + 1, z))
        try:
            names = [self._get_block_name(pt) for pt in cells]
        except ApiError:
            return -1
        if any(name in FLUID_BLOCKS | UTILITY_BLOCKS | {"bedrock"} for name in names):
            return -1
        dug = 0
        for pt, name in zip(cells, names):
            if name in PASSABLE_BLOCKS:
                continue
            try:
                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                dug += 1
            except ApiError:
                return -1
        return dug


    def _clear_incidental_obstruction_only(self, target_wp: tuple[int, int, int]) -> int:
        """During lossless backtrack, clear loose debris AND self-placed seal/catch/bridge blocks at waypoint."""
        dug = 0
        self_placed = (
            self.sealed_positions
            | {tuple(p) for p in self.catch_blocks_placed}
            | {tuple(p) for p in self.bridge_blocks_placed}
        )
        for pt in (target_wp, (target_wp[0], target_wp[1] + 1, target_wp[2])):
            if pt not in self_placed:
                continue
            try:
                if self._get_block_name(pt) in UTILITY_BLOCKS:
                    continue
                self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                self.sealed_positions.discard(pt)
                dug += 1
            except ApiError:
                pass
        for dy in (1, 0):
            pt = (target_wp[0], target_wp[1] + dy, target_wp[2])
            try:
                b = self._get_block_name(pt)
            except ApiError:
                continue
            if b in FALLING_BLOCKS:
                try:
                    self.bot.call("dig", {"pos": [pt[0], pt[1], pt[2]], "collect": False, "stop_on": ["hurt", "death"]})
                    dug += 1
                except ApiError:
                    pass
        return dug


    def _recorded_mixed_stair(
        self, lower: tuple[int, int, int], upper: tuple[int, int, int],
        *, cache: dict[tuple[int, int, int], str] | None = None,
    ) -> list[tuple[int, int, int]] | None:
        """Recover an omitted flat step from the actual footing on a short recorded ascent."""
        dx, dy, dz = (b - a for a, b in zip(lower, upper))
        length = abs(dx) + abs(dz)
        if dx * dz or length not in (3, 4) or dy != length - 1:
            return None
        route = [lower]
        if cache is None:
            cache = {}
        try:
            for offset in range(1, length):
                x, z = lower[0] + dx // length * offset, lower[2] + dz // length * offset
                low_y = max(route[-1][1], upper[1] - (length - offset))
                high_y = min(route[-1][1] + 1, upper[1])
                if self._geofence_blocked(self._active_geofence, x, z, action="mine_dfs_rejoin") is not None:
                    return None
                for y in range(high_y, low_y - 1, -1):
                    if (
                        self._get_block_name((x, y, z), cache) in PASSABLE_BLOCKS
                        and self._get_block_name((x, y + 1, z), cache) in PASSABLE_BLOCKS
                        and self._is_solid_floor((x, y - 1, z), cache)
                    ):
                        route.append((x, y, z))
                        break
                else:
                    return None
        except ApiError:
            return None
        return [*route, upper]


    def _route_at_position(
        self,
        waypoints: list[tuple[int, int, int]],
        pos: tuple[int, int, int],
        edge_kind: str | None,
    ) -> tuple[list[tuple[int, int, int]], int] | None:
        if pos in waypoints:
            return waypoints, waypoints.index(pos)
        if (offset := _omitted_waypoint_index(waypoints, pos, edge_kind)) is not None:
            return [*waypoints[:offset], pos, *waypoints[offset:]], offset
        if edge_kind not in {"stair_down", "stair_up"}:
            return None
        for index, (start, end) in enumerate(zip(waypoints, waypoints[1:])):
            if not all(min(a, b) <= c <= max(a, b) for a, b, c in zip(start, end, pos)):
                continue
            ascending = start[1] < end[1]
            lower, upper = (start, end) if ascending else (end, start)
            route = self._recorded_mixed_stair(lower, upper)
            if route is None or pos not in route[1:-1]:
                continue
            if not ascending:
                route.reverse()
            return [*waypoints[:index], *route, *waypoints[index + 2:]], index + route.index(pos)
        return None


    def _walk_waypoints(
        self,
        waypoints: list[tuple[int, int, int]],
        *,
        reverse: bool = False,
        min_hp_ref: list[float],
        deadline: float | None = None,
        resume_edge_kind: str | None = None,
        record_climb: bool = False,
    ) -> tuple[bool, str | None]:
        if deadline is None:
            deadline = getattr(self, "_deadline", None)
        ordered = list(reversed(waypoints)) if reverse else list(waypoints)
        if resume_edge_kind is not None and ordered:
            if deadline is not None and time.monotonic() >= deadline:
                return False, "walk_time_slice"
            stance = self.bot.status()
            if stance.get("on_ground", True):
                current = _floor_pos(stance["pos"])
                resumed = self._route_at_position(ordered, current, resume_edge_kind)
                if resumed is not None:
                    route, offset = resumed
                    ordered = route[offset:]
        if record_climb and len(ordered) > 1 and (scoped := self._budget_session) is not None:
            scoped.climb_routes.append((max(0, len(scoped.positions) - 1), ordered))
        for index, wp in enumerate(ordered):
            if deadline is not None and time.monotonic() >= deadline:
                # Live n0211: a 60-waypoint walk held one tool call for 3+ minutes with the bot
                # unattended. Abort on the slice boundary and report it instead.
                return False, "walk_time_slice"
            st = self.bot.status()
            if st.get("alive") is False or st.get("ready") is False:
                return False, "not_ready"
            hp_now = st.get("health")
            if isinstance(hp_now, (int, float)) and hp_now < min_hp_ref[0]:
                return False, "hurt"
            cur = _floor_pos(st["pos"])
            if abs(cur[0] - wp[0]) == 0 and abs(cur[1] - wp[1]) == 0 and abs(cur[2] - wp[2]) == 0:
                continue
            # Waypoints can be far apart (node anchors). Walk the straight line cell by cell:
            # a single move_exact over a 30-block climb always fails, which used to end the
            # route walk ("backtrack_blocked") and force a full staircase re-dig (live s18).
            recorded_segment = index > 0 and cur == ordered[index - 1]
            mixed_stair = self._recorded_mixed_stair(cur, wp) if recorded_segment else None
            hops = mixed_stair or _interpolate_cells(cur, wp)
            if len(hops) > 1:
                # Recorded stairs can omit an intermediate stair or pillar step.
                recorded_stair = recorded_segment and _is_two_step_stair(cur, wp)
                recorded_vertical_gap = recorded_segment and _is_two_block_vertical_gap(cur, wp)
                stepped_all = True
                for previous, hop in zip(hops, hops[1:]):
                    if deadline is not None and time.monotonic() >= deadline:
                        return False, "walk_time_slice"
                    if (hop == wp or recorded_vertical_gap) and previous == (hop[0], hop[1] - 1, hop[2]):
                        if self._pillar_to_recorded_step(hop, deadline=deadline, min_hp=min_hp_ref[0]):
                            continue
                    if not self._step_to(hop, from_cell=previous, repair_support=(hop == wp or recorded_stair)):
                        stepped_all = False
                        break
                if stepped_all:
                    continue
            if deadline is not None and time.monotonic() >= deadline:
                return False, "walk_time_slice"
            if self._move_exact(wp):
                continue
            dx_w = wp[0] - cur[0]
            dy_w = wp[1] - cur[1]
            dz_w = wp[2] - cur[2]
            if dy_w == 0:
                detour_deadline = min(deadline if deadline is not None else math.inf, time.monotonic() + 8.0)
                detoured, detour_error = self._walk_level_detour(wp, min_hp=min_hp_ref[0], deadline=detour_deadline)
                if not detoured and detour_error is None and index + 1 < len(ordered):
                    # The blocked cell can itself be a stored waypoint; try rejoining just past it.
                    detoured, detour_error = self._walk_level_detour(
                        ordered[index + 1], min_hp=min_hp_ref[0], deadline=detour_deadline, bypass=wp,
                    )
                if detoured:
                    continue
                if detour_error:
                    return False, detour_error
            if max(abs(dx_w), abs(dz_w)) == 2 and abs(dy_w) in (0, 2):
                mid_wp = (cur[0] + dx_w // 2, cur[1] + (dy_w // 2), cur[2] + dz_w // 2)
                if self._move_exact(mid_wp) and self._move_exact(wp):
                    continue
            # A failed approach can still land directly below the recorded step.
            if dy_w > 0 and self._pillar_to_recorded_step(wp, deadline=deadline, min_hp=min_hp_ref[0]):
                continue
            cleared = self._clear_incidental_obstruction_only(wp)
            self.dug_during_return += cleared
            if cleared > 0 and self._move_exact(wp):
                continue
            # A recorded corridor cell that got filled in (collapse / inflow debris) used to end
            # the walk here ("backtrack_blocked") and force a full staircase re-dig (live s12/s15).
            repaired = self._clear_route_obstruction(wp)
            if repaired > 0:
                self.dug_during_return += repaired
                if self._move_exact(wp):
                    continue
            try:
                feet = self._get_block_name((wp[0], wp[1], wp[2]))
                head = self._get_block_name((wp[0], wp[1] + 1, wp[2]))
            except ApiError:
                feet, head = "?", "?"
            return (
                False,
                f"backtrack_blocked@{wp[0]},{wp[1]},{wp[2]}"
                f"(feet={feet},head={head},from={cur[0]},{cur[1]},{cur[2]})",
            )
        return True, None


    def _find_node_path(self, start_id: int, goal_id: int) -> list[tuple[list[tuple[int, int, int]], bool, int]]:
        if start_id == goal_id:
            return []
        ancestors_a: list[int] = []
        cur: int | None = start_id
        while cur is not None and cur in self.nodes:
            ancestors_a.append(cur)
            cur = self.nodes[cur].parent_id
        set_a = set(ancestors_a)

        ancestors_b: list[int] = []
        cur_b: int | None = goal_id
        lca: int | None = None
        while cur_b is not None and cur_b in self.nodes:
            if cur_b in set_a:
                lca = cur_b
                break
            ancestors_b.append(cur_b)
            cur_b = self.nodes[cur_b].parent_id

        if lca is None:
            return []

        segments: list[tuple[list[tuple[int, int, int]], bool, int]] = []
        walk = start_id
        while walk != lca:
            node = self.nodes[walk]
            if node.edge_from_parent is None or node.parent_id is None:
                break
            segments.append((node.edge_from_parent.waypoints, True, node.parent_id))
            walk = node.parent_id
        for child_id in reversed(ancestors_b):
            node = self.nodes[child_id]
            if node.edge_from_parent is not None:
                segments.append((node.edge_from_parent.waypoints, False, child_id))
        return segments


    def _is_recorded_shaft_position(self, pos: tuple[int, int, int]) -> bool:
        for node in self.nodes.values():
            if pos == node.anchor:
                return True
            edge = node.edge_from_parent
            if edge is not None and self._route_at_position(edge.waypoints, pos, edge.edge_kind) is not None:
                return True
            if any(
                branch.kind == "branch_leaf" and branch.status == "completed"
                and branch.completed_waypoints and branch.completed_waypoints[0] == node.anchor
                and pos in branch.completed_waypoints
                for branch in node.branches
            ):
                return True
        return False


    def _nearby_node_without_ascent(
        self, pos: tuple[int, int, int], goal_id: int,
    ) -> ShaftNode | None:
        candidates = sorted(self.nodes.values(), key=lambda node: math.dist(pos, node.anchor))
        for candidate in candidates:
            if math.dist(pos, candidate.anchor) > NODE_REJOIN_DISTANCE:
                break
            if candidate.anchor[1] > pos[1]:
                continue
            if candidate.node_id == goal_id:
                return candidate
            route = self._find_node_path(candidate.node_id, goal_id)
            if route and all(
                points and self.nodes[dest_id].anchor[1] <= pos[1] and all(pt[1] <= pos[1] for pt in points)
                for points, _, dest_id in route
            ):
                return candidate
        return None


    def _navigate_to_node(
        self,
        goal_node_id: int,
        *,
        min_hp_ref: list[float],
        deadline: float | None = None,
        allow_lower_rejoin: bool = True,
    ) -> tuple[bool, str | None]:
        if self.active_node_id is None or self.active_node_id not in self.nodes or goal_node_id not in self.nodes:
            return False, "no_active_tree"
        if deadline is None:
            deadline = getattr(self, "_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            return False, "walk_time_slice"
        st = self.bot.status()
        if st.get("alive") is False or st.get("ready") is False:
            return False, "not_ready"
        if st.get("on_ground", True):
            cur_pos = _floor_pos(st["pos"])
        else:
            settled, grounded = self._settled_position(st["pos"])
            if not grounded:
                return False, "not_grounded"
            cur_pos = _floor_pos(settled)
        # A slice can end between nodes. Resume the recorded edge toward the
        # goal subtree, without first walking to the far end and doubling back.
        if cur_pos == self.nodes[goal_node_id].anchor:
            self.active_node_id = goal_node_id
            return True, None
        if cur_pos != self.nodes[self.active_node_id].anchor:
            for node_id, node in self.nodes.items():
                if node.anchor == cur_pos:
                    self.active_node_id = node_id
                    break
            else:
                goal_ancestors = {goal_node_id}
                parent_id = self.nodes[goal_node_id].parent_id
                while parent_id in self.nodes and parent_id not in goal_ancestors:
                    goal_ancestors.add(parent_id)
                    parent_id = self.nodes[parent_id].parent_id

                for node in self.nodes.values():
                    edge = node.edge_from_parent
                    parent_id = node.parent_id
                    if edge is None or parent_id is None or parent_id not in self.nodes:
                        continue
                    resumed = self._route_at_position(edge.waypoints, cur_pos, edge.edge_kind)
                    if resumed is None:
                        continue
                    waypoints, offset = resumed
                    if node.node_id in goal_ancestors:
                        dest = node.node_id
                        route = waypoints[offset:]
                    else:
                        dest = parent_id
                        route = list(reversed(waypoints[:offset + 1]))
                    if route[-1] != self.nodes[dest].anchor:
                        route = [*route, self.nodes[dest].anchor]
                    ok, err = self._walk_waypoints(
                        route, min_hp_ref=min_hp_ref, deadline=deadline, record_climb=not allow_lower_rejoin,
                    )
                    if not ok:
                        return False, err
                    self.active_node_id = dest
                    cur_pos = self.nodes[dest].anchor
                    break
                else:
                    # Finished side branches keep waypoints without adding a node.
                    # Follow the recorded branch back to its junction first.
                    leaf = next((
                        (node.node_id, branch.completed_waypoints)
                        for node in self.nodes.values()
                        for branch in node.branches
                        if branch.kind == "branch_leaf" and branch.status == "completed"
                        and branch.completed_waypoints and branch.completed_waypoints[0] == node.anchor
                        and cur_pos in branch.completed_waypoints
                    ), None)
                    if leaf is not None:
                        dest, waypoints = leaf
                        route = list(reversed(waypoints[:waypoints.index(cur_pos) + 1]))
                        ok, err = self._walk_waypoints(
                            route, min_hp_ref=min_hp_ref, deadline=deadline, record_climb=not allow_lower_rejoin,
                        )
                        if not ok:
                            return False, err
                        self.active_node_id = dest
                        cur_pos = self.nodes[dest].anchor
        anchor_now = self.nodes[self.active_node_id].anchor
        if cur_pos != anchor_now:
            best_near_id: int | None = None
            best_near_d = float("inf")
            min_rejoin_y = -math.inf if allow_lower_rejoin else cur_pos[1]
            for nid, nd in self.nodes.items():
                if nd.anchor[1] < min_rejoin_y:
                    continue
                d = math.dist(cur_pos, nd.anchor)
                if d < best_near_d:
                    best_near_d = d
                    best_near_id = nid
            lower_join = None
            if (
                allow_lower_rejoin and best_near_id is not None
                and self.nodes[best_near_id].anchor[1] > cur_pos[1] >= self.nodes[goal_node_id].anchor[1]
            ):
                lower_join = self._nearby_node_without_ascent(cur_pos, goal_node_id)
            if lower_join is not None and self._move_exact(lower_join.anchor):
                self.active_node_id = lower_join.node_id
            elif best_near_id is not None and best_near_d <= NODE_REJOIN_DISTANCE and (
                best_near_id == self.collar_node_id or best_near_d <= math.dist(cur_pos, anchor_now)
            ):
                nearest = self.nodes[best_near_id]
                anchor = nearest.anchor
                edge = nearest.edge_from_parent
                repair_support = edge is not None and edge.edge_kind in {"stair_down", "stair_up"}
                adjacent_ascent = anchor[1] == cur_pos[1] + 1 and best_near_d <= math.sqrt(2)
                stepped_up = adjacent_ascent and (
                    self._hop_up_to(anchor, repair_support=repair_support)
                    or (best_near_d == 1.0 and self._pillar_to_recorded_step(anchor, deadline=deadline, min_hp=min_hp_ref[0]))
                )
                if not stepped_up and not self._move_exact(anchor):
                    directly_above = cur_pos == (anchor[0], anchor[1] + 1, anchor[2])
                    if not directly_above or not self._drop_to_recorded_step(anchor, min_hp=min_hp_ref[0]):
                        return False, "cannot_reach_nearest_node"
                self.active_node_id = best_near_id
            elif (
                self.collar_node_id in self.nodes and self.nodes[self.collar_node_id].anchor[1] >= min_rejoin_y
                and math.hypot(
                    cur_pos[0] - self.nodes[self.collar_node_id].anchor[0],
                    cur_pos[2] - self.nodes[self.collar_node_id].anchor[2],
                ) <= 48.0
            ):
                collar_anc = self.nodes[self.collar_node_id].anchor
                if allow_lower_rejoin:
                    try:
                        self.bot.call(
                            "move_to",
                            {
                                "x": collar_anc[0] + 0.5,
                                "z": collar_anc[2] + 0.5,
                                "range": 1.4,
                                "timeout": 9000,
                                "stop_on": ["hurt", "death"],
                            },
                        )
                    except ApiError:
                        pass
                if not self._move_exact(collar_anc):
                    return False, "cannot_reach_collar"
                self.active_node_id = self.collar_node_id
            elif anchor_now[1] < min_rejoin_y or not self._move_exact(anchor_now):
                return False, "cannot_reach_anchor"

        if self.active_node_id == goal_node_id:
            return True, None

        segments = self._find_node_path(self.active_node_id, goal_node_id)
        for waypoints, rev, dest_id in segments:
            if not waypoints:
                return False, "no_recorded_route"
            route = list(reversed(waypoints)) if rev else list(waypoints)
            anchor = self.nodes[dest_id].anchor
            if route[-1] != anchor:
                route.append(anchor)
            ok, err = self._walk_waypoints(
                route, min_hp_ref=min_hp_ref, deadline=deadline, record_climb=not allow_lower_rejoin,
            )
            if not ok:
                return False, err
            self.active_node_id = dest_id
        if self.active_node_id != goal_node_id:
            return False, "no_recorded_route"
        return True, None


    def _walk_to_collar(
        self, collar_id: int, *, min_hp_ref: list[float], deadline: float | None = None,
        allow_lower_rejoin: bool = True,
    ) -> tuple[bool, str | None]:
        mining_node_id = self.active_node_id
        try:
            return self._navigate_to_node(
                collar_id, min_hp_ref=min_hp_ref, deadline=deadline, allow_lower_rejoin=allow_lower_rejoin,
            )
        finally:
            self.active_node_id = mining_node_id


    def _resume_shaft_tip(self, goal_node_id: int, *, min_hp_ref: list[float]) -> tuple[bool, str | None]:
        try:
            ok, reason = self._navigate_to_node(goal_node_id, min_hp_ref=min_hp_ref)
            if reason == "walk_time_slice":
                raise _MiningDeadline
            return ok, reason
        except _MiningDeadline:
            # Navigation may have updated the active node at an intermediate anchor.
            # Keep the mining destination so the next slice continues along the route.
            self.active_node_id = goal_node_id
            raise


    def _should_resume_at_depth(self, node: ShaftNode, pos: tuple[int, int, int]) -> bool:
        collar = self.nodes[self.collar_node_id]
        ceiling = collar.anchor[1]
        if self.collar_ceiling_y is not None:
            ceiling = max(ceiling, self.collar_ceiling_y)
        return (
            self.target_y - SCAN_BAND <= pos[1] <= ceiling - 20
            and node.anchor[1] > max(pos[1], self.target_y) + DEPTH_RESET_THRESHOLD
        )


    def _resume_explore_at_depth(
        self, node: ShaftNode, pos: tuple[int, int, int], *, min_hp_ref: list[float],
    ) -> bool:
        if self._is_recorded_shaft_position(pos):
            return self._resume_deepest_branch(node, min_hp_ref=min_hp_ref, max_y=pos[1])
        if self._resume_local_branch(node.node_id, min_hp_ref=min_hp_ref):
            return True
        if pos[1] <= self.target_y:
            return False

        # A cutoff can leave the bot below the last saved waypoint. Start a
        # new branch here only after verifying dry footing and an exit direction.
        geofence = self._active_geofence
        branches = self._probe_dry_descent(
            pos, directions=tuple(DIRECTIONS), geofence=geofence, min_hp_ref=min_hp_ref,
        )
        if not branches:
            return False
        self._archive_current_shaft_to_ledger(mark_exhausted=False)
        self._clear_active_tree_only()
        self._init_collar(
            pos, target_y=self.target_y, geofence=geofence,
            viable_dirs=[branch.direction for branch in branches],
        )
        return True


    def _recover_failed_shaft_rejoin(
        self, node: ShaftNode, pos: tuple[int, int, int], reason: str | None, *, min_hp_ref: list[float],
    ) -> bool:
        if (
            reason != "cannot_reach_nearest_node"
            or not self.target_y + SPINE_DESCEND_MIN_DELTA < pos[1] < node.anchor[1]
            or not self._active_deep_branch_pending()
        ):
            return False
        current = _floor_pos(self.bot.status()["pos"])
        return current[1] <= pos[1] and self._resume_explore_at_depth(node, current, min_hp_ref=min_hp_ref)


    @staticmethod
    def _ahead_of_pending_descent(node: ShaftNode, pos: tuple[int, int, int]) -> bool:
        dx, dy, dz = (p - a for p, a in zip(pos, node.anchor))
        distance = abs(dx) + abs(dz)
        return distance > 0 and abs(distance + dy) <= 1 and any(
            branch.kind == "stair_down" and branch.status == "pending"
            and (dx, dz) == tuple(distance * component for component in DIRECTIONS[branch.direction])
            for branch in node.branches
        )


    def _resume_deepest_branch(
        self, node: ShaftNode, *, min_hp_ref: list[float], descendants_only: bool = False,
        max_y: int | None = None, blocked_branches: set[int] | None = None,
    ) -> bool:
        ceiling = node.anchor[1] if max_y is None else min(max_y, node.anchor[1])
        candidates = []
        for candidate in self.nodes.values():
            if candidate.node_id == node.node_id or candidate.anchor[1] > ceiling:
                continue
            if not any(
                branch.status == "pending" and (blocked_branches is None or id(branch) not in blocked_branches)
                for branch in candidate.branches
            ):
                continue
            if descendants_only:
                route = self._find_node_path(node.node_id, candidate.node_id)
                if not route or any(reverse for _, reverse, _ in route):
                    continue
            candidates.append(candidate)
        for candidate in sorted(candidates, key=lambda candidate: candidate.anchor[1]):
            reached, reason = self._resume_shaft_tip(candidate.node_id, min_hp_ref=min_hp_ref)
            if reached:
                return True
            self.active_node_id = node.node_id
            if reason in {"hurt", "not_ready", "not_grounded"}:
                break
        return False


    def _utility_blocks_backtrack(self, reason: str) -> bool:
        return reason.startswith("backtrack_blocked@") and any(
            f"feet={block}," in reason or f"head={block}," in reason for block in UTILITY_BLOCKS
        )


    def _resume_local_branch(self, blocked_node_id: int, *, min_hp_ref: list[float]) -> bool:
        stance = self.bot.status()
        if (
            stance.get("alive") is False or stance.get("ready") is False
            or not stance.get("on_ground", True) or stance.get("in_water") or stance.get("in_lava")
            or stance.get("health", min_hp_ref[0]) < min_hp_ref[0]
        ):
            return False
        current = _floor_pos(stance["pos"])
        candidates = sorted(self.nodes.values(), key=lambda node: math.dist(current, node.anchor))
        for candidate in candidates:
            if candidate.node_id == blocked_node_id or not any(b.status == "pending" for b in candidate.branches):
                continue
            try:
                if not self._level_walk_is_clear(current, candidate.anchor):
                    continue
                if current == candidate.anchor or self._move_short_distance(
                    candidate.anchor, deadline=self._deadline or math.inf,
                ):
                    settled, on_ground = self._settled_position()
                    if on_ground and _floor_pos(settled) == candidate.anchor:
                        self.active_node_id = candidate.node_id
                        return True
            except ApiError as err:
                if err.code in {"hurt", "death", "interrupted", "dead", "disconnected"}:
                    return False
            # Try another candidate only from a newly verified stance on this level.
            stance = self.bot.status()
            if not stance.get("on_ground", True) or _floor_pos(stance["pos"]) != current:
                return False
        return False


    def _cluster_ore_points(
        self,
        anchor: tuple[int, int, int],
        pts: list[tuple[int, int, int]],
        hostiles: list[tuple[float, float, float]],
    ) -> list[dict[str, Any]]:
        if not pts:
            return []
        remaining = set(pts)
        clusters: list[dict[str, Any]] = []
        while remaining:
            seed = next(iter(remaining))
            remaining.remove(seed)
            comp = [seed]
            q: deque[tuple[int, int, int]] = deque([seed])
            while q:
                cur = q.popleft()
                neighbors = [
                    p for p in list(remaining)
                    if abs(p[0] - cur[0]) + abs(p[1] - cur[1]) + abs(p[2] - cur[2]) <= 2
                ]
                for nb in neighbors:
                    remaining.remove(nb)
                    comp.append(nb)
                    q.append(nb)
            rep = min(comp, key=lambda p: math.dist(anchor, p))
            dist_a = math.dist(anchor, rep)
            min_h = min(
                (math.dist((rep[0] + 0.5, rep[1] + 0.5, rep[2] + 0.5), hp) for hp in hostiles),
                default=999.0,
            )
            mob_nearby = min_h <= 9.0
            # Safe mob-free clusters always outrank mob-guarded caves; rich close veins rank highest
            eff_cost = dist_a / (1.0 + 0.35 * min(len(comp), 6))
            clusters.append(
                {
                    "rep": rep,
                    "members": comp,
                    "size": len(comp),
                    "dist": round(dist_a, 2),
                    "min_hostile_dist": round(min_h, 2),
                    "mob_nearby": mob_nearby,
                    "rank_key": (1 if mob_nearby else 0, eff_cost),
                }
            )
        clusters.sort(key=lambda c: c["rank_key"])
        return clusters

