"""MiningDFSEngine mixin: climb_drops (split from legacy mining_dfs.py)."""

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


class _ClimbDropsMixin:
    def _check_target_drop_positions(
        self,
        *,
        expected_drops: set[str] | frozenset[str],
        before_count: int,
        ore_pt: tuple[int, int, int],
        stand_y: int,
        cache: dict[tuple[int, int, int], str],
        can_open_lower: bool,
        collect_until: float,
        allow_ore_step: bool,
    ) -> dict[str, int]:
        def verified_ore_step(upper: tuple[int, int, int]) -> bool:
            x, y, z = ore_pt
            return (
                allow_ore_step and upper[1] == y + 1
                and abs(upper[0] - x) + abs(upper[2] - z) == 1
                and self._level_walk_is_clear(upper, upper)
                and self._level_walk_is_clear(ore_pt, ore_pt)
                and self._get_block_name((x, y + 2, z)) in PASSABLE_BLOCKS
            )

        inv, counts = _inventory_snapshot_and_counts(self.bot)
        origin = None
        moved = False
        interrupted = False
        attempted = {
            entity_id: (row["item_pos"], row["reason"]) for entity_id, row in self.drop_receipts.items()
            if row["reason"] in {"move_failed", "pickup_unconfirmed"}
        }

        for attempt in range(3):
            if time.monotonic() + 0.2 >= collect_until:
                break
            try:
                drops = self._scan_target_drops(expected_drops)
                if not drops:
                    if attempt == 0 and sum(counts.get(it, 0) for it in expected_drops) <= before_count:
                        time.sleep(0.16)
                        counts = _inventory_counts(self.bot)
                        continue
                    break

                stance = self.bot.status()
                pos = stance["pos"]
                current = _floor_pos(pos)
                if origin is None:
                    origin = current
                if not stance.get("on_ground", True) or stance.get("in_water") or stance.get("in_lava"):
                    break
                for row in drops:
                    self.drop_receipts[row["id"]] = {
                        **row, "reason": "pickup_budget", "trigger_ore_pos": list(ore_pt),
                    }

                for row in sorted(drops, key=lambda row: math.dist(pos, row["item_pos"])):
                    entity_id, item, drop_pos = row["id"], row["item"], row["item_pos"]
                    self.drop_receipts[entity_id]["reason"] = "height_difference"
                    if abs(drop_pos[1] - current[1]) > 1.3:
                        continue
                    drop_cell = _floor_pos(drop_pos)
                    x, _, z = drop_cell
                    goal = (x, current[1], z)
                    if drop_cell == ore_pt and current == origin and verified_ore_step(current):
                        goal = ore_pt
                    elif not self._level_walk_is_clear(current, goal):
                        self.drop_receipts[entity_id]["reason"] = "unsafe_route"
                        if (
                            attempt == 1 and can_open_lower and ore_pt[1] >= stand_y + 1
                            and goal == (ore_pt[0], stand_y, ore_pt[2])
                        ):
                            self._open_drop_pocket_base(goal, cache)
                        continue
                    previous = attempted.get(entity_id)
                    if previous is not None and previous[0] == drop_pos:
                        self.drop_receipts[entity_id]["reason"] = previous[1]
                        continue
                    if _compute_empty_slots(inv) == 0 and not _can_stack_expected_drop(inv, expected_drops):
                        inv, _ = self.skills._ensure_free_slots(min_free=3, inv_snapshot=inv)
                    reason = "pickup_unconfirmed"
                    try:
                        if not self._move_short_distance(goal, deadline=collect_until - 0.5):
                            self.drop_receipts[entity_id]["reason"] = "pickup_budget"
                            break
                    except ApiError as err:
                        reason = "move_failed"
                        interrupted = err.code in {"hurt", "death", "interrupted", "dead", "disconnected"}
                    moved = True
                    self.drop_receipts[entity_id]["reason"] = reason
                    attempted[entity_id] = (drop_pos, reason)
                    if interrupted:
                        break
                    time.sleep(0.12)
                    inv, after = _inventory_snapshot_and_counts(self.bot)
                    gain = after.get(item, 0) - counts.get(item, 0)
                    counts = after
                    if gain > 0:
                        self._verify_drop_receipts(drops, expected_drops, item, gain)
                    break  # Refresh moving items and the bot's stance before the next approach.
                else:
                    if attempt < 2:
                        time.sleep(0.12)
                if interrupted:
                    break
            except ApiError:
                break

        if moved and not interrupted and origin is not None and time.monotonic() + 0.2 < collect_until:
            try:
                stance = self.bot.status()
                current = _floor_pos(stance["pos"])
                if current != origin and stance.get("on_ground", True) and (
                    (current == ore_pt and verified_ore_step(origin)) or self._level_walk_is_clear(current, origin)
                ):
                    self._move_short_distance(origin, deadline=collect_until)
            except ApiError:
                pass
        if (
            not interrupted and time.monotonic() + 0.2 < collect_until
            and sum(counts.get(it, 0) for it in expected_drops) <= before_count
        ):
            counts = _inventory_counts(self.bot)
        return counts


    def _find_peel_blockers(
        self,
        anchor: tuple[int, int, int],
        ore_pt: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
        *,
        max_blocks: int = 2,
    ) -> list[tuple[int, int, int]]:
        eye = (anchor[0], anchor[1] + 1, anchor[2])
        cands: list[tuple[float, tuple[int, int, int]]] = []
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nb = (ore_pt[0] + ax, ore_pt[1] + ay, ore_pt[2] + az)
            if nb in {anchor, eye, ore_pt}:
                continue
            if abs(nb[1] - anchor[1]) > 3 or (abs(nb[0] - anchor[0]) + abs(nb[2] - anchor[2])) > 4:
                continue
            try:
                b = self._get_block_name(nb, cache)
            except ApiError:
                continue
            if b in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"}:
                continue
            if self._adjacent_fluid_or_falling([nb], cache) is not None:
                continue
            cands.append((math.dist(eye, nb), nb))
        cands.sort(key=lambda r: r[0])
        if not cands:
            return []
        first = cands[0][1]
        if max_blocks <= 1 or abs(first[0] - anchor[0]) + abs(first[2] - anchor[2]) <= 2:
            return [first]
        step_x = first[0] + (1 if anchor[0] > first[0] else (-1 if anchor[0] < first[0] else 0))
        step_z = first[2] + (1 if anchor[2] > first[2] else (-1 if anchor[2] < first[2] else 0))
        mid = (step_x, first[1], step_z)
        if mid not in {anchor, eye, ore_pt, first}:
            try:
                mb = self._get_block_name(mid, cache)
                if mb not in PASSABLE_BLOCKS | FLUID_BLOCKS | {"bedrock"} and self._adjacent_fluid_or_falling([mid], cache) is None:
                    return [mid, first]
            except ApiError:
                pass
        return [first]


    def _drop_cell_reachable(
        self,
        pt: tuple[int, int, int],
        stand: tuple[int, int, int],
        cache: dict[tuple[int, int, int], str],
    ) -> bool:
        """True when the drop of `pt` would land within pickup range of our feet position."""
        if (
            max(abs(pt[0] - stand[0]), abs(pt[2] - stand[2])) <= 1
            and abs(pt[1] - stand[1]) <= 1
        ):
            return True
        eye = (stand[0], stand[1] + 1, stand[2])
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nb = (pt[0] + ax, pt[1] + ay, pt[2] + az)
            if nb in {stand, eye}:
                return True
            if abs(nb[0] - stand[0]) + abs(nb[1] - stand[1]) + abs(nb[2] - stand[2]) <= 1:
                try:
                    if self._get_block_name(nb, cache) in PASSABLE_BLOCKS:
                        return True
                except ApiError:
                    continue
        return False


    def _escape_primitive(self, name: str, *, deadline: float, **kwargs: Any) -> dict[str, Any]:
        skill = getattr(self.skills, name)
        params = inspect.signature(skill).parameters
        if "deadline" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            kwargs["deadline"] = deadline
        return skill(**kwargs)


    def _climb_construction_steps(self, pos: list[float] | None, direction: str, limit: int) -> int:
        fence = self._active_geofence
        if fence is None or not fence.enabled:
            return limit
        if not pos or len(pos) < 3:
            return 0
        distance = fence.distance_from_center(pos[0], pos[2])
        outside = distance > fence.safe_radius
        if direction == "pillar":
            return 0 if outside else limit
        x, _, z = _floor_pos(pos)
        dx, dz = DIRECTIONS[direction]
        if outside:
            # Recheck the actual landing before extending an inward path from outside the fence.
            limit = 1
        for step in range(1, limit + 1):
            landing = fence.distance_from_center(x + dx * step + 0.5, z + dz * step + 0.5)
            if landing > fence.safe_radius and (not outside or landing >= distance):
                return step - 1
        return limit


    def _climb_outside_fence(self, pos: list[float] | None) -> bool:
        fence = self._active_geofence
        return bool(
            pos and fence is not None and fence.enabled
            and fence.distance_from_center(pos[0], pos[2]) > fence.safe_radius
        )


    def _climb_target_reached(self, status: dict[str, Any], minimum_y: int) -> bool:
        pos = status.get("pos")
        return (
            bool(pos) and bool(status.get("on_ground", True))
            and status.get("alive") is not False and status.get("ready") is not False
            and not (status.get("in_water") or status.get("in_lava"))
            and _floor_pos(pos)[1] >= minimum_y
            and not self._climb_outside_fence(pos)
        )


    def _climb_fence_progress(self, climb: dict[str, Any]) -> bool:
        scoped, fence = self._budget_session, self._active_geofence
        if scoped is None or fence is None or not fence.enabled:
            return False
        status = scoped.last_status
        start, end = scoped.first_pos, status.get("pos")
        if not start or not end:
            return False
        start_distance = fence.distance_from_center(start[0], start[2])
        end_distance = fence.distance_from_center(end[0], end[2])
        if max(start_distance, end_distance) <= fence.safe_radius:
            return False
        climb["geofence"] = {
            "start_distance": round(start_distance, 2),
            "end_distance": round(end_distance, 2),
            "safe_radius": fence.safe_radius,
        }
        return (
            start_distance > fence.safe_radius and end_distance < start_distance
            and (math.floor(start[0]), math.floor(start[2])) != (math.floor(end[0]), math.floor(end[2]))
            and status.get("on_ground") is True and status.get("alive") is not False and status.get("ready") is not False
            and not (status.get("in_water") or status.get("in_lava"))
        )


    def _climb_recorded_route_progress(self, climb: dict[str, Any]) -> bool:
        scoped = self._budget_session
        if scoped is None or not scoped.first_pos or not scoped.climb_routes or climb["start_y"] != climb["end_y"]:
            return False
        status = scoped.last_status
        if status.get("on_ground") is not True or not self._climb_target_reached(status, climb["start_y"]):
            return False
        end = _floor_pos(status["pos"])
        if end == _floor_pos(scoped.first_pos):
            return False
        for start_index, route in scoped.climb_routes:
            if end in route and set(route[:route.index(end)]).intersection(scoped.positions[start_index:-1]):
                climb["recorded_route_progress"] = True
                return True
        return False


    def _confirm_climb_slice_progress(self, result: dict[str, Any], scoped: _BudgetSession) -> None:
        if result.get("stop_reason") != "climb_progress":
            return
        status = scoped.last_status
        before, after = scoped.first_pos, status.get("pos")
        if (
            not before or not after or status.get("on_ground") is not True
            or status.get("alive") is False or status.get("ready") is False
            or status.get("in_water") or status.get("in_lava")
        ):
            return
        climb = result["climb"]
        end_y = _floor_pos(after)[1]
        if end_y == climb["end_y"] and (
            end_y > _floor_pos(before)[1] or climb.get("recorded_route_progress") or self._climb_fence_progress(climb)
        ):
            # The harness must retain partial progress without allowing the next queued action.
            result.update(ok=True, pos_before=before, pos_after=after)


    def _climb_out(self, *, deadline: float, max_attempts: int = 40) -> dict[str, Any]:
        """Climb to the surface datum in ONE call: probe directions and keep going.

        Live s09/s12: one `staircase(up, steps=6)` per model turn (10-20s each) plus one wasted
        turn per blocked direction made a 120-block climb cost 10-15 minutes. Doing the probing
        and the repetition in-call turns that into 1-3 turns.
        """
        start_y = _floor_pos(self.bot.status().get("pos") or [0, 64, 0])[1]
        # explore refuses at y <= ceiling-20, so climbing must overshoot that boundary by a few
        # blocks. With target_y == ceiling-20 the two thresholds coincide and live n0332 looped
        # forever: climb_out "reached" at 42, explore rejected 42, climb again, ...
        target_y = (int(self.collar_ceiling_y) - 16) if self.collar_ceiling_y is not None else start_y
        minimum_y = max(start_y, target_y - CLIMB_HEIGHT_TOLERANCE)
        method = "stair"
        try:
            hp_now = float(self.bot.status().get("health") or 20.0)
        except (ApiError, TypeError, ValueError):
            hp_now = 20.0
        hp_ref = [hp_now]

        # 1) Preferred: walk our own corridor to a higher collar.
        walked = False
        collar = self.nodes.get(self.collar_node_id)
        attempted_collar = collar.anchor if collar is not None else None
        if collar is not None and collar.anchor[1] > start_y:
            previous_y = start_y
            while True:
                walked, walk_error = self._walk_to_collar(
                    collar.node_id, min_hp_ref=hp_ref, deadline=deadline, allow_lower_rejoin=False,
                )
                if walked:
                    break
                partial_status = self.bot.status()
                y_part = _floor_pos(partial_status.get("pos") or [0, start_y, 0])[1]
                reached = self._climb_target_reached(partial_status, minimum_y)
                eligible_to_retry = (
                    walk_error not in {"walk_time_slice", "hurt", "not_ready", "not_grounded"}
                    and deadline - time.monotonic() >= 10.0
                    and partial_status.get("on_ground", True)
                    and partial_status.get("alive") is not False and partial_status.get("ready") is not False
                    and not (partial_status.get("in_water") or partial_status.get("in_lava"))
                )
                if reached or (y_part > start_y and not eligible_to_retry):
                    if reached:
                        self._detach_retired_shaft()
                    return {
                        "start_y": start_y,
                        "end_y": y_part,
                        "climbed": y_part - start_y,
                        "target_y": target_y,
                        "minimum_y": minimum_y,
                        "reached": reached,
                        "attempts": 0,
                        "method": "walk",
                    }
                # A recorded walk can stop at an obstruction after making progress.
                # Rejoin once from the new height before trying an alternate escape.
                if not eligible_to_retry or y_part <= previous_y:
                    break
                previous_y = y_part
        # 2) Look for a different nearby saved route before cutting new stairs.
        if not walked:
            cur_now = _floor_pos(self.bot.status().get("pos") or [0, start_y, 0])
            if self._try_restore_saved_shaft(
                cur_now, min_hp_ref=hp_ref, allow_retired=True, skip_collar=attempted_collar,
            ):
                ok_walk, _err_walk = self._navigate_to_node(
                    self.collar_node_id, min_hp_ref=hp_ref, deadline=deadline, allow_lower_rejoin=False,
                )
                if ok_walk:
                    walked = True
                    # Travel only: do not keep this retired tree as the mining tree.
                    self._clear_active_tree_only()
        if walked:
            y_walk = _floor_pos(self.bot.status().get("pos") or [0, start_y, 0])[1]
            # _navigate_to_node can report success while we are still deep (e.g. the active node
            # already equals the collar id). Only trust it when it actually moved us up.
            if y_walk <= start_y and y_walk < minimum_y:
                walked = False
        if walked:
            method = "walk"
            walk_status = self.bot.status()
            y_walk = _floor_pos(walk_status.get("pos") or [0, start_y, 0])[1]
            if self._climb_target_reached(walk_status, minimum_y):
                self._detach_retired_shaft()
                return {
                    "start_y": start_y,
                    "end_y": y_walk,
                    "climbed": y_walk - start_y,
                    "target_y": target_y,
                    "minimum_y": minimum_y,
                    "reached": True,
                    "attempts": 0,
                    "method": method,
                }

        dirs = ["north", "south", "east", "west"]
        if getattr(self, "_climb_dir", None) in dirs:
            dirs.remove(self._climb_dir)
            dirs.insert(0, self._climb_dir)
        attempts = 0
        while time.monotonic() < deadline and attempts < max_attempts:
            stance = self.bot.status()
            cur_cell = _floor_pos(stance.get("pos") or [0, 64, 0])
            cur_y = cur_cell[1]
            at_height = cur_y >= minimum_y
            if at_height and not self._climb_outside_fence(stance.get("pos")):
                break
            if len(self._climb_failures) > 128:
                self._climb_failures.pop(next(iter(self._climb_failures)))
            failed = self._climb_failures.setdefault(cur_cell, set())
            moved = 0
            stair_dirs = [] if at_height else [d for d in dirs if ("stair", d) not in failed]
            for d in stair_dirs:
                if time.monotonic() >= deadline or attempts >= max_attempts:
                    break
                if not self._climb_construction_steps(stance.get("pos"), d, 1):
                    continue
                attempts += 1
                failed.add(("stair", d))
                try:
                    self._escape_primitive("dig_staircase", deadline=deadline,
                                           direction=d, steps=1, vertical="up", stop_on_ore=False)
                except (ApiError, ValueError, KeyError, TypeError):
                    if self._active_geofence is not None and self._active_geofence.enabled:
                        stance = self.bot.status()
                    continue
                stance = self.bot.status()
                gained = _floor_pos(stance.get("pos") or [0, cur_y, 0])[1] - int(cur_y)
                if gained > 0:
                    self._climb_failures.pop(cur_cell, None)
                    self._climb_dir = d
                    moved += gained
                    cur_y += gained
                    break          # keep using the direction that just worked
            if moved > 0:
                continue
            if time.monotonic() >= deadline or attempts >= max_attempts:
                break
            if (
                not at_height and ("pillar", "pillar") not in failed
                and self._climb_construction_steps(stance.get("pos"), "pillar", 1)
            ):
                failed.add(("pillar", "pillar"))
                attempts += 1
                try:
                    self._escape_primitive("dig_staircase", deadline=deadline,
                                           direction="pillar", steps=1, vertical="pillar", stop_on_ore=False)
                except (ApiError, ValueError, KeyError, TypeError):
                    pass
                continue
            # Tunnel sideways to clear a blocked ascent, or toward the fence interior
            # without climbing higher once the height target is met.
            escaped = False
            for d in dirs:
                if time.monotonic() >= deadline or attempts >= max_attempts:
                    break
                if ("tunnel", d) in failed:
                    continue
                pos = self.bot.status().get("pos")
                steps = self._climb_construction_steps(pos, d, 3)
                if not steps:
                    continue
                before = _floor_pos(pos or [0, cur_y, 0])
                failed.add(("tunnel", d))
                attempts += 1
                try:
                    tunnel = self._escape_primitive("dig_level_tunnel", deadline=deadline,
                                                    direction=d, steps=steps, stop_on_ore=False)
                except _MiningDeadline:
                    failed.discard(("tunnel", d))
                    raise
                except (ApiError, ValueError, KeyError, TypeError):
                    continue
                if tunnel.get("stop_reason") == "time_slice":
                    failed.discard(("tunnel", d))
                    break
                after = _floor_pos(self.bot.status().get("pos") or [0, cur_y, 0])
                if after != before:
                    self._climb_dir = d
                    escaped = True
                    break
            if escaped:
                continue
            break
        end_status = self.bot.status()
        end_y = _floor_pos(end_status.get("pos") or [0, 64, 0])[1]
        reached = self._climb_target_reached(end_status, minimum_y)
        if reached:
            self._detach_retired_shaft()
        return {
            "start_y": start_y,
            "end_y": end_y,
            "climbed": end_y - start_y,
            "target_y": target_y,
            "minimum_y": minimum_y,
            "reached": reached,
            "attempts": attempts,
            "method": method if not walked else f"{method}+stair",
        }


    @staticmethod
    def _climb_arrival_message(climb: dict[str, Any]) -> str:
        message = (
            f"已达到本轮回撤高度，当前 y={climb['end_y']}"
            f"（最低 y={climb['minimum_y']}，计划目标 y={climb['target_y']}）"
        )
        if climb["recorded_collar_y"] is not None:
            message += f"，最高记录井口 y={climb['recorded_collar_y']}"
        return (
            message + "。若任务要求到地表，请确认已出洞。仍在洞内时可用 mc_skill_staircase(vertical='up') 继续上行。"
            "若只需换井，可以在安全站位继续 explore。"
        )


    @staticmethod
    def _climb_progress_message(climb: dict[str, Any]) -> str:
        fence = climb.get("geofence")
        if fence and fence["end_distance"] > fence["safe_radius"]:
            return (
                f"本轮回撤到 y={climb['end_y']}，距围栏中心 {fence['end_distance']} 格"
                f"（安全半径 {fence['safe_radius']} 格）。可从当前站位继续 climb_out 向内回撤。"
            )
        if climb.get("recorded_route_progress"):
            return f"本轮已沿记录矿道回撤，当前 y={climb['end_y']}（目标 y={climb['target_y']}）。可从当前站位继续 climb_out。"
        return f"本轮爬到 y={climb['end_y']}（目标 y={climb['target_y']}），可继续 climb_out。"


    def _surface_climb_hint(self) -> str:
        """Tell the model exactly how high to climb before calling explore again."""
        if self.collar_ceiling_y is None:
            return (
                "请调用 action='climb_out'，优先走自建巷道，再挖台阶。"
                "若返回 climb_progress，可从当前站位继续 climb_out。"
            )
        need_y = int(self.collar_ceiling_y) - 20
        return (
            f"请调用 action='climb_out'，优先沿自建巷道向上（目标 y>={need_y}，"
            f"地表基准 y={self.collar_ceiling_y}）。若返回 climb_progress，可从当前站位继续 climb_out。"
            "到达地表后再 explore 开新井。"
        )


    def _mine_vein_ore_cell(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        anchor: tuple[int, int, int],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
    ) -> tuple[bool, int, int]:
        if pt in self.mined_ore_positions or self._is_ore_unreachable(pt) or self.skills._is_block_avoided(pt):
            return False, 0, 0
        try:
            if self._get_block_name(pt, cache) != bname:
                return False, 0, 0
        except ApiError:
            return False, 0, 0
        try:
            stance = self.bot.status()
        except ApiError:
            stance = {}
        stand = _floor_pos(stance["pos"]) if stance.get("pos") else anchor
        distance = sum(abs(ore_coord - foot_coord) for ore_coord, foot_coord in zip(pt, stand))
        replaces_footing = pt == (stand[0], stand[1] - 1, stand[2])
        if replaces_footing or distance > DROP_PICKUP_RADIUS_MANHATTAN + 2:
            return self._mine_vein_cell_from_side(
                pt, bname, stance=stance, target_set=target_set, cache=cache, replaces_footing=replaces_footing,
            )
        return self._dig_vein_cell(pt, bname, anchor=anchor, target_set=target_set, cache=cache)


    def _mine_vein_cell_from_side(
        self,
        pt: tuple[int, int, int],
        bname: str,
        *,
        stance: dict[str, Any],
        target_set: set[str],
        cache: dict[tuple[int, int, int], str],
        replaces_footing: bool,
    ) -> tuple[bool, int, int]:
        def dry_footing(status: dict[str, Any]) -> bool:
            return (
                bool(status.get("pos")) and status.get("on_ground", True)
                and status.get("alive") is not False and status.get("ready") is not False
                and not (status.get("in_water") or status.get("in_lava"))
            )

        if not dry_footing(stance):
            return False, 0, 0
        origin = _floor_pos(stance["pos"])
        x, _, z = pt
        candidates = [(x + dx, origin[1], z + dz) for dx, dz in DIRECTIONS.values()]
        candidates.sort(key=lambda cell: math.dist(origin, cell))
        deadline = self._deadline or math.inf
        try:
            goal = next((
                cell for cell in candidates
                if self._drop_cell_reachable(pt, cell, cache) and self._level_walk_is_clear(origin, cell)
            ), None)
        except ApiError:
            return False, 0, 0
        if goal is None:
            return False, 0, 0

        can_dig = False
        try:
            if not self._move_short_distance(goal, deadline=deadline):
                return False, 0, 0
            landed = self.bot.status()
            can_dig = (
                dry_footing(landed) and _floor_pos(landed["pos"]) == goal
                and self._level_walk_is_clear(goal, goal)
                and self._get_block_name(pt) == bname
            )
        except ApiError as err:
            if err.code != "path_not_found":
                return False, 0, 0

        result = (
            self._dig_vein_cell(
                pt, bname, anchor=goal, target_set=target_set, cache=cache, replaces_footing=replaces_footing,
            )
            if can_dig else (False, 0, 0)
        )

        try:
            current = self.bot.status()
            if dry_footing(current) and (cell := _floor_pos(current["pos"])) != origin:
                if (
                    replaces_footing and result[0] and cell == goal
                    and self._get_block_name(pt) in AIR_BLOCKS
                    and self._level_walk_is_clear(cell, cell)
                ):
                    self._place_bridge_block(pt, cache)
                if self._level_walk_is_clear(cell, origin):
                    self._move_short_distance(origin, deadline=deadline)
        except ApiError:
            pass
        return result

