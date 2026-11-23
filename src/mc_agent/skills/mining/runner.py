"""MiningDFSEngine mixin: runner (split from legacy mining_dfs.py)."""

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


class _RunnerMixin:
    def run(
        self,
        *,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        max_duration_s: float = 32.0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        budget = max(0.0, min(90.0, float(max_duration_s)))
        deadline = time.monotonic() + budget
        act = str(action or "explore").strip().lower()
        original_bot, original_skills = self.bot, self.skills
        previous_deadline = self._deadline
        previous_budget_session = self._budget_session
        previous_active_geofence = self._active_geofence
        self._active_geofence = geofence
        self.drop_receipts.clear()
        return_dug_before = self.dug_during_return
        recrafted_before = self.recrafted_picks
        _norm_tgt = str(target or "").strip().lower().removeprefix("minecraft:")
        _dig_cap = 16.0 if _norm_tgt in {"obsidian", "crying_obsidian"} else 8.0
        scoped = _BudgetSession(original_bot, deadline - min(0.5, budget / 10), dig_timeout_s=_dig_cap)
        self._deadline = scoped.deadline
        self._budget_session = scoped
        self.bot = scoped.session
        self.skills = copy.copy(original_skills)
        self.skills.bot = self.bot
        try:
            result = self._run(target=target, goal_count=goal_count, target_y=target_y,
                               action=action, geofence=geofence,
                               strategy=strategy, deadline=scoped.deadline)
            if act in {"climb_out", "climb"} and result.get("stop_reason") == "surface_reached":
                result.update(pos_before=scoped.first_pos, pos_after=scoped.last_status.get("pos"))
            self._confirm_climb_slice_progress(result, scoped)
            if act == "explore":
                requested = max(1, min(64, int(goal_count)))
                t_gained = int(result.get("target_gained", 0))
                result["goal_met"] = t_gained >= requested
                result["dug_count"] = scoped.dug_count
                self._track_locked_cluster_yield(result, attempted="target_gained" in result)
                if scoped.last_inventory is not None:
                    profile = _resolve_target_profile(
                        target, target_y, dimension=normalize_dimension(scoped.last_status.get("dimension")),
                    )
                    self._report_target_inventory(
                        result, scoped.last_inventory, profile["expected_items"], requested,
                        target=profile["profile_key"],
                    )
                self._report_explore_depth(
                    result, target_y=self.target_y, budget=budget, ascent_levels=scoped.confirmed_ascent_levels,
                )
            if self.drop_receipts:
                result["target_drop_receipts"] = list(self.drop_receipts.values())
            if scoped.ore_breaks:
                result["ore_breaks"] = scoped.ore_breaks
            return result
        except _MiningLifeChanged:
            result = {
                "ok": False,
                "requires_replan": True,
                "action": act,
                "strategy": self.strategy,
                "stop_reason": "life_changed",
                "pos_before": scoped.first_pos,
                "pos_after": scoped.last_status.get("pos"),
                "message": "本轮因生命状态或维度变化结束。请先确认当前位置和背包，再决定如何继续。",
                "tree": self.snapshot(),
            }
            if scoped.ore_breaks:
                result["ore_breaks"] = scoped.ore_breaks
            return result
        except _MiningDeadline as exc:
            resupply_interrupted = isinstance(exc, _CollarResupplyDeadline)
            moved = len(scoped.positions) >= 2 and scoped.positions[-1] != scoped.positions[0]
            if scoped.timeout_dig_pos is not None and scoped.timeout_dig_read_s >= 1.0:
                last_pos = scoped.last_status.get("pos")
                stalled_at_feet = bool(last_pos and _floor_pos(last_pos) == scoped.timeout_dig_pos)
                # A stall at the current feet can repeat after productive excavation.
                if not scoped.dug_count or stalled_at_feet:
                    self.skills._avoid_temporarily(scoped.timeout_dig_pos, "dig_timeout", DIG_TIMEOUT_COOLDOWN_S)
            self._preserve_interrupted_branch(scoped, deadline_failure=exc)
            self._archive_current_shaft_to_ledger(mark_exhausted=False)
            result: dict[str, Any] = {
                "ok": False, "requires_replan": True, "action": act,
                "strategy": self.strategy, "stop_reason": "time_slice",
                "pos_before": scoped.first_pos, "pos_after": scoped.last_status.get("pos"),
                "tree": self.snapshot(),
            }
            if scoped.timeout_path is not None:
                result["timeout_path"] = scoped.timeout_path
            if scoped.timeout_dig_pos is not None:
                result["dig_target"] = list(scoped.timeout_dig_pos)
            if act == "explore":
                gained = {}
                if scoped.first_inventory is not None and scoped.last_inventory is not None:
                    gained = _positive_inv_delta(
                        _inventory_counts_from_raw(scoped.first_inventory),
                        _inventory_counts_from_raw(scoped.last_inventory),
                    )
                profile = _resolve_target_profile(
                    target, target_y, dimension=normalize_dimension(scoped.last_status.get("dimension")),
                )
                target_gained = sum(gained.get(item, 0) for item in profile["expected_items"])
                requested = max(1, min(64, int(goal_count)))
                recrafted = self.recrafted_picks - recrafted_before
                result.update(
                    goal_met=target_gained >= requested,
                    goal_count=requested,
                    target_gained=target_gained,
                    gained=gained,
                    dug_count=scoped.dug_count,
                    recrafted_picks=recrafted,
                )
                self._track_locked_cluster_yield(
                    result, attempted=bool(scoped.dug_count or scoped.timeout_dig_read_s >= 1.0),
                )
                self._report_target_inventory(
                    result, scoped.last_inventory, profile["expected_items"], requested,
                    target=profile["profile_key"],
                )
                if resupply_interrupted:
                    result.update(
                        stop_reason="pickaxe_low", next_action="return_collar", budget_effective_s=budget,
                        message="镐耐久不足，回井口补给途中达到时间上限。请继续 action='return_collar'，补好镐后再 explore。",
                    )
                else:
                    made_progress = bool(moved or scoped.dug_count or gained or recrafted)
                    if (
                        scoped.timeout_dig_pos is not None and scoped.timeout_dig_read_s >= 1.0
                        and not (scoped.dug_count or gained or recrafted)
                        and len(self.nodes) == 1 and self.collar_node_id in self.nodes
                        and scoped.first_pos and result["pos_after"]
                        and _floor_pos(scoped.first_pos) == _floor_pos(result["pos_after"])
                    ):
                        # A timed-out first dig bypasses the normal end-of-slice idle check.
                        self.idle_calls += 1
                    elif made_progress:
                        self.idle_calls = 0
                    if made_progress:
                        result.update(
                            ok=True,
                            requires_replan=False,
                            stop_reason="quota_reached" if result["goal_met"] else "time_slice_progress",
                            message="本轮已保留确认的采掘进展和回撤路径，可以继续 explore。",
                        )
                    self._report_explore_depth(
                        result, target_y=int(profile["target_y"]), budget=budget,
                        ascent_levels=scoped.confirmed_ascent_levels,
                    )
            if act in {"climb_out", "climb"} and scoped.positions:
                start_y, end_y = scoped.positions[0][1], scoped.positions[-1][1]
                target_height = int(self.collar_ceiling_y) - 16 if self.collar_ceiling_y is not None else start_y
                minimum_y = max(start_y, target_height - CLIMB_HEIGHT_TOLERANCE)
                status = scoped.last_status
                in_fluid = status.get("in_water") or status.get("in_lava")
                reached = self._climb_target_reached(status, minimum_y)
                result["climb"] = {
                    "start_y": start_y, "end_y": end_y, "climbed": end_y - start_y,
                    "target_y": target_height, "reached": reached,
                    "minimum_y": minimum_y,
                    "recorded_collar_y": self.collar_ceiling_y,
                    "method": "interrupted",
                }
                progressed_inward = self._climb_fence_progress(result["climb"])
                progressed_on_route = self._climb_recorded_route_progress(result["climb"])
                if reached:
                    self._detach_retired_shaft()
                    result.update(
                        ok=True, requires_replan=False, stop_reason="surface_reached",
                        message=self._climb_arrival_message(result["climb"]),
                        tree=self.snapshot(),
                    )
                elif (end_y > start_y or progressed_inward or progressed_on_route) and not in_fluid:
                    result.update(
                        stop_reason="climb_progress", next_action="climb_out",
                        message=self._climb_progress_message(result["climb"]),
                    )
                    remaining_height = result["climb"]["minimum_y"] - end_y
                    if 20 <= budget < 55 and remaining_height > end_y - start_y:
                        result["suggested_next"] = {"retry_with": {"max_duration_s": 55}}
                        result["message"] += "若本集时间允许，可保留其余参数，将 max_duration_s 调到 55 秒。"
                    self._confirm_climb_slice_progress(result, scoped)
            if act == "return_collar" and self.collar_node_id in self.nodes:
                collar = self.nodes[self.collar_node_id].anchor
                result["collar_anchor"] = list(collar)
                result["dug_during_return"] = self.dug_during_return - return_dug_before
                status = scoped.last_status
                if (
                    moved and not (status.get("in_water") or status.get("in_lava"))
                    and status.get("alive") is not False and status.get("ready") is not False
                    and math.dist(scoped.positions[-1], collar) < math.dist(scoped.positions[0], collar)
                ):
                    result.update(
                        requires_replan=False, stop_reason="return_collar_progress", next_action="return_collar",
                        message="本轮已沿矿道向井口回撤。请从当前站位继续 action='return_collar'。",
                    )
            if result["stop_reason"] == "time_slice":
                next_steps: dict[str, Any] = {
                    "if_blocked": "处理前方障碍，或换到安全站位后重新规划。",
                }
                result["message"] = "本轮因时间限制中止。请先检查站位及障碍，避免原地用相同参数重试。"
                if budget < 90:
                    next_steps["retry_with"] = {"max_duration_s": max(20, min(90, math.ceil(budget + 15)))}
                    result["message"] += "若路线畅通，可按 retry_with 调大预算并保留其余参数。"
                result["suggested_next"] = next_steps
            if self.drop_receipts:
                result["target_drop_receipts"] = list(self.drop_receipts.values())
            if scoped.ore_breaks:
                result["ore_breaks"] = scoped.ore_breaks
            return result
        except BaseException as exc:
            if act == "explore":
                preserved = self._preserve_interrupted_branch(scoped, deadline_failure=None)
                wet_shaft = self._remember_water_interruption(getattr(exc, "trigger", None), scoped.last_status)
                if preserved or wet_shaft:
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
            raise
        finally:
            self.bot, self.skills = original_bot, original_skills
            self._deadline = previous_deadline
            self._budget_session = previous_budget_session
            self._active_geofence = previous_active_geofence
            # Stop a request that may still be running server-side, within the reserved
            # portion of the caller's budget. Never reconnect or reauthenticate here.
            stop_deadline = min(deadline, scoped.pickup_stop_deadline or deadline)
            remaining = stop_deadline - time.monotonic()
            if remaining > 0.2 and (scoped.timeout_path is not None or time.monotonic() >= scoped.deadline - 0.2):
                try:
                    scoped.deadline = stop_deadline
                    scoped.call("stop", {}, timeout_s=0.25)
                except (_MiningDeadline, ApiError, httpx.HTTPError):
                    pass


    def _track_locked_cluster_yield(self, result: dict[str, Any], *, attempted: bool) -> None:
        if result.get("target_gained", 0) > 0:
            self._last_locked_rep = None
            self._locked_rep_dry_calls = 0
            return
        if not attempted:
            return
        raw_rep = self.locked_cluster.get("rep") if self.locked_cluster else None
        if not raw_rep:
            self._last_locked_rep = None
            self._locked_rep_dry_calls = 0
            return
        rep = _floor_pos(raw_rep)
        if rep != self._last_locked_rep:
            self._last_locked_rep = rep
            self._locked_rep_dry_calls = 0
            self._locked_rep_best_distance = math.inf

        before, after = result.get("pos_before"), result.get("pos_after")
        if isinstance(after, (list, tuple)) and len(after) >= 3:
            distance = math.dist(after[:3], rep)
            previous_best = self._locked_rep_best_distance
            self._locked_rep_best_distance = min(previous_best, distance)
            if isinstance(before, (list, tuple)) and len(before) >= 3:
                # Rewalking the same approach after being moved away does not find a new mining stance.
                if distance < min(previous_best, math.dist(before[:3], rep)) - 2.0:
                    self._locked_rep_dry_calls = 0
                    return
        self._locked_rep_dry_calls += 1
        if self._locked_rep_dry_calls >= 2:
            self._mark_ore_unreachable(rep, "stalled_locked_cluster")
            self._last_locked_rep = None
            self._locked_rep_dry_calls = 0
            result["radar_evicted_cluster"] = list(rep)
            result["tree"] = self.snapshot()
            if isinstance(result.get("radar_summary"), dict):
                result["radar_summary"]["locked_cluster"] = None
                result["radar_summary"]["cluster_size"] = 0


    @staticmethod
    def _report_target_inventory(
        result: dict[str, Any],
        inventory: dict[str, Any] | None,
        expected_items: tuple[str, ...],
        requested: int,
        *,
        target: str,
    ) -> None:
        if inventory is None:
            return
        counts = _inventory_counts_from_raw(inventory)
        target_items = {item: counts[item] for item in expected_items if counts.get(item, 0) > 0}
        total = sum(target_items.values())
        result["target_inventory_items"] = target_items
        result["target_inventory_count"] = total
        result["target_inventory_meets_goal"] = total >= requested
        if target == "iron":
            ingots, raw_iron = counts.get("iron_ingot", 0), counts.get("raw_iron", 0)
            result["iron_pickaxe_materials"] = {
                "iron_ingot": ingots,
                "raw_iron": raw_iron,
                "additional_iron_needed": max(0, 3 - ingots - raw_iron),
            }


    @staticmethod
    def _report_explore_depth(
        result: dict[str, Any], *, target_y: int, budget: float, ascent_levels: set[int],
    ) -> None:
        result["budget_effective_s"] = budget
        before, after = result.get("pos_before"), result.get("pos_after")
        if not before or not after:
            return
        start_y, end_y = _floor_pos(before)[1], _floor_pos(after)[1]
        depth_reference = max(start_y, target_y)
        planned_ascent = sum(depth_reference < y <= end_y for y in ascent_levels)
        if planned_ascent:
            result["planned_ascent_blocks"] = planned_ascent
        if result["goal_met"]:
            return
        if end_y > depth_reference + planned_ascent + DEPTH_RESET_THRESHOLD:
            result["progress_lost"] = True
            result["depth_lost"] = end_y - start_y
            if result.get("stop_reason") in {"time_slice_progress", "movement_not_confirmed"}:
                result.update(
                    ok=False, requires_replan=True, stop_reason="depth_reset",
                    message=f"本轮从 y={start_y} 回升到了 y={end_y}，目标层是 y={target_y}。请先检查下行路线再继续。",
                )
        elif result.get("stop_reason") == "time_slice_progress" and end_y < start_y and end_y - target_y >= 32:
            next_budget = min(90, max(60, math.ceil(30 + 0.4 * (end_y - target_y))))
            if next_budget > budget:
                result["suggested_next"] = {"retry_with": {"max_duration_s": next_budget}}
                result["message"] = (
                    f"本轮已下挖到 y={end_y}，距离目标层还有 {end_y - target_y} 格。"
                    f"下一轮可保留其余参数，将 max_duration_s 调到 {next_budget} 秒。"
                )


    def _collar_failure(
        self, failure: dict[str, Any] | None, pos: tuple[int, int, int], geofence: Any,
    ) -> dict[str, Any]:
        details = dict(failure or {})
        reason = details.pop("stop_reason", "no_viable_collar_direction")
        fence_error = self._geofence_blocked(geofence, pos[0], pos[2], action="mine_dfs_collar")
        if fence_error is not None:
            reason = "void_geofence_blocked"
            message = "当前站位超出安全范围，请核对坐标和岛中心后再进入安全区。"
            details.clear()
        elif reason == "no_viable_collar_direction":
            offset = next(
                (blocker["safe_side_step"] for blocker in details.get("collar_blockers", {}).values()
                 if "safe_side_step" in blocker),
                None,
            )
            if offset is not None:
                details["suggested_recovery_pos"] = offset
                message = f"当前站位无法安全下挖。可先尝试向同层有支撑的 {offset} 短移，确认落脚后再探查井口。"
            else:
                message = "当前站位附近没有可安全开挖的方向，请按方向受阻信息探查另一处实心站位再 explore。"
        else:
            message = "当前站位无法确认有实心支撑，请先移到安全的实心地面再 explore。"
        result: dict[str, Any] = {
            "ok": False,
            "requires_replan": True,
            "action": "explore",
            "strategy": self.strategy,
            "stop_reason": reason,
            "message": message,
            "pos_after": list(pos),
            "tree": self.snapshot(),
            **details,
        }
        if fence_error is not None:
            result["error"] = fence_error["error"]
        return result


    def _layer_switch_refusal(self, status: dict[str, Any], *, message: str | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "requires_replan": True,
            "action": "explore",
            "strategy": self.strategy,
            "stop_reason": "layer_switch_upward_refused",
            "pos_before": status.get("pos"),
            "pos_after": status.get("pos"),
            "message": message or "当前站位无法确认新层的安全下行或同层路线。请先沿旧井回撤到有支撑的干燥站位，再切换目标层。",
            "tree": self.snapshot(),
        }


    def _deep_shaft_rejoin_failure(self) -> dict[str, Any]:
        scoped = self._budget_session
        if self._has_stale_descent(self.target_y, observed_layers={point[1] for point in self.radar_targets}):
            return self._layer_switch_refusal(scoped.last_status if scoped is not None else {})
        return {
            "ok": False,
            "requires_replan": True,
            "action": "explore",
            "strategy": self.strategy,
            "stop_reason": "needs_surface_climb",
            "pos_before": scoped.first_pos if scoped is not None else None,
            "pos_after": scoped.last_status.get("pos") if scoped is not None else None,
            "message": (
                "当前深度附近的待挖节点暂时无法接上。请先在同层调整站位，再继续 explore。"
                "如果无法安全继续，可调用 climb_out 沿已记矿道回撤。"
            ),
            "tree": self.snapshot(),
        }


    def _run(
        self,
        *,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        strategy: str | None = None,
        deadline: float,
    ) -> dict[str, Any]:
        self._refresh_strategy(strategy)
        act = str(action or "explore").strip().lower()
        if act == "reset":
            return self.reset(geofence=geofence)
        if act in {"climb_out", "climb"}:
            climb = self._climb_out(deadline=deadline)
            climb["recorded_collar_y"] = self.collar_ceiling_y
            progressed_inward = self._climb_fence_progress(climb)
            progressed_on_route = self._climb_recorded_route_progress(climb)
            made_progress = climb["climbed"] > 0 or progressed_inward or progressed_on_route
            ok_climb = bool(climb["reached"])
            # Never hand control back while the bot is floating: live n0152 drowned during the
            # model's next (empty) turns after unstick left it in_water.
            wet_after = False
            try:
                if bool(self.bot.status().get("in_water")):
                    esc = self.skills.escape_water()
                    wet_after = bool(esc.get("in_water"))
                    if wet_after:
                        self.skills.unstick()
                        wet_after = bool(self.bot.status().get("in_water"))
            except (ApiError, ValueError, KeyError):
                wet_after = False
            if wet_after:
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": "climb_out",
                    "strategy": self.strategy,
                    "stop_reason": "in_water",
                    "climb": climb,
                    "message": "爬升途中仍在水中，请先用 staircase(vertical='up') 或 unstick 上岸/离开水域，再继续。",
                    "tree": self.snapshot(),
                }
            result = {
                "ok": ok_climb,
                "requires_replan": not ok_climb,
                "action": "climb_out",
                "strategy": self.strategy,
                "stop_reason": "surface_reached" if ok_climb else (
                    "climb_progress" if made_progress else "climb_blocked"
                ),
                "climb": climb,
                "message": (
                    self._climb_arrival_message(climb)
                    if ok_climb
                    else (self._climb_progress_message(climb)
                          if made_progress else "所有爬升方向均无法推进，请先处理障碍或换位置，避免原地重复 climb_out。")
                ),
                "tree": self.snapshot(),
            }
            if not ok_climb and made_progress:
                result["next_action"] = "climb_out"
            return result
        if act == "status":
            return {"ok": True, "action": "status", "strategy": self.strategy, "tree": self.snapshot()}

        st0 = self.bot.status()
        if st0.get("alive") is False:
            return {"ok": False, "requires_replan": True, "strategy": self.strategy, "stop_reason": "dead", "tree": self.snapshot()}
        raw_dimension = st0.get("dimension")
        dimension = normalize_dimension(raw_dimension)
        profile = _resolve_target_profile(target, target_y, dimension=dimension)
        if act == "explore" and raw_dimension:
            ecology = assess_world_target(profile["target_blocks"], dimension=dimension, mode="find")
            if ecology["verdict"] == "wrong_dimension":
                available_targets = {}
                if dimension == "the_nether":
                    available_targets = {
                        target_name: ORE_DFS_PROFILES[profile_key]["default_target_y"]
                        for target_name, profile_key in (
                            ("ancient_debris", "ancient_debris"),
                            ("quartz", "quartz"),
                            ("gold", "nether_gold_ore"),
                        )
                    }
                hints = [f"{name} (Y={y})" for name, y in available_targets.items()]
                message = ecology["hint"]
                if hints:
                    message += " DFS 可改采 " + "、".join(hints) + "。"
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "bad_target_for_dimension",
                    "target": profile["profile_key"],
                    "dimension": dimension,
                    "allowed_dimensions": ecology["allowed_dimensions"],
                    "available_targets": available_targets,
                    "message": message,
                    "pos_after": st0.get("pos"),
                    "tree": self.snapshot(),
                }
            if dimension == "the_nether" and target_y is not None and profile["target_y"] < 0:
                suggested_y = ORE_DFS_PROFILES.get(profile["profile_key"], {}).get("default_target_y")
                message = "下界 Y=0 以下没有可采掘的地形，请改用非负的 target_y。"
                if suggested_y is not None:
                    message += f"当前目标可先试 Y={suggested_y}。"
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "bad_target_y_for_dimension",
                    "target": profile["profile_key"],
                    "dimension": dimension,
                    "target_y": profile["target_y"],
                    "minimum_y": 0,
                    "suggested_target_y": suggested_y,
                    "message": message,
                    "pos_before": st0.get("pos"),
                    "pos_after": st0.get("pos"),
                    "tree": self.snapshot(),
                }
        if act == "explore" and self._active_shaft_is_retired():
            ceiling = self.collar_ceiling_y
            if ceiling is None:
                ceiling = self.nodes[self.collar_node_id].anchor[1]
            if not self._climb_target_reached(st0, ceiling - 16 - CLIMB_HEIGHT_TOLERANCE):
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "needs_surface_climb",
                    "next_action": "climb_out",
                    "pos_before": st0.get("pos"),
                    "pos_after": st0.get("pos"),
                    "message": "当前矿井已枯竭，请沿已记路线继续回撤。" + self._surface_climb_hint(),
                    "tree": self.snapshot(),
                }
            self._clear_active_tree_only()
        requested_y = int(profile["target_y"])
        shallower_layer = (
            act == "explore"
            and self.collar_node_id in self.nodes
            and requested_y - self.target_y >= LAYER_SWITCH_MIN_DELTA
        )
        if shallower_layer:
            try:
                cur_y = _floor_pos(st0.get("pos") or [0, 64, 0])[1]
            except (ApiError, TypeError, ValueError):
                cur_y = self.target_y
            # A paused descent still has its original target, even before it reaches that layer.
            if cur_y < requested_y - SCAN_BAND:
                return self._layer_switch_refusal(
                    st0,
                    message=(
                        f"当前位置 y={cur_y} 已低于新目标层 y={requested_y}，旧井仍保存 y={self.target_y} 的路线。"
                        "要换到浅层，请先用 action='return_collar' 或 'climb_out' 主动回撤，再以新目标开井。"
                        "要沿当前井继续下探，请使用原目标层。"
                    ),
                )
        active = self.nodes.get(self.active_node_id)
        replan_layer = shallower_layer and (
            self._has_stale_descent(requested_y)
            or active is not None and active.anchor[1] < requested_y - SCAN_BAND
        )
        if act == "explore" and not replan_layer:
            self.target_y = requested_y
        target_blocks = profile["target_blocks"]
        expected_items = profile["expected_items"]
        goal_count = max(1, min(64, int(goal_count)))

        recrafted_before = self.recrafted_picks
        if st0.get("in_water"):
            esc = self.skills.escape_water()
            if esc.get("in_water"):
                # Second attempt: unstick nudges the hitbox/jumps out of shallow water.
                try:
                    self.skills.unstick()
                except (ApiError, ValueError, KeyError):
                    pass
                st_w = self.bot.status()
                if st_w.get("in_water"):
                    # Only a flooded MINING layer condemns the shaft. A puddle/pond on the way
                    # down is a detour, not a dead shaft (live s11 churned 3 shafts at y=54).
                    try:
                        wet_y = _floor_pos(st_w["pos"])[1]
                    except (KeyError, TypeError, ValueError):
                        wet_y = self.target_y
                    if wet_y > int(self.target_y) + 12:
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "strategy": self.strategy,
                            "stop_reason": "in_water",
                            "water_escape": esc,
                            "message": (
                                "当前仍在水中，挖矿无法开始（reset 无效）。请先用 "
                                "mc_skill_staircase(vertical='up') 或 mc_skill_flee 离开水域/爬上岸，"
                                "然后直接 explore。"
                            ),
                        }
                    if self._note_fluid_abort("in_water"):
                        self._archive_current_shaft_to_ledger(mark_exhausted=True)
                        self._clear_active_tree_only()
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "action": act,
                            "strategy": self.strategy,
                            "stop_reason": "shaft_exhausted",
                            "water_escape": esc,
                            "message": (
                                "这口井已连续 3 次因溺水/水患中止，判定为水淹矿井并归档。"
                                + self._surface_climb_hint()
                            ),
                            "tree": self.snapshot(),
                        }
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "strategy": self.strategy,
                        "stop_reason": "in_water",
                        "water_escape": esc,
                        "message": (
                            "当前仍在水中，挖矿无法开始（reset 无效）。请先用 "
                            "mc_skill_staircase(vertical='up') 或 mc_skill_flee 离开水域/爬上岸，"
                            "然后直接 explore。"
                        ),
                    }
                st0 = st_w
            else:
                st0 = self.bot.status()

        cur_pos = _floor_pos(st0.get("pos") or [0, 64, 0])
        min_hp_ref = [float(st0.get("health") if isinstance(st0.get("health"), (int, float)) else 20.0)]

        if act == "return_collar":
            if self.collar_node_id is None or self.collar_node_id not in self.nodes:
                return {"ok": False, "requires_replan": True, "strategy": self.strategy, "stop_reason": "no_collar_recorded", "tree": self.snapshot()}
            dug_before = self.dug_during_return
            ok_ret, err_ret = self._walk_to_collar(
                self.collar_node_id,
                min_hp_ref=min_hp_ref,
                deadline=deadline,
            )
            if err_ret == "walk_time_slice":
                raise _MiningDeadline
            st_after = self.bot.status()
            hp_after = st_after.get("health")
            collar_anchor = self.nodes[self.collar_node_id].anchor
            result = {
                "ok": ok_ret,
                "requires_replan": not ok_ret,
                "action": "return_collar",
                "strategy": self.strategy,
                "stop_reason": "returned_to_collar" if ok_ret else (err_ret or "return_failed"),
                "collar_reached": ok_ret,
                "pos_before": st0.get("pos"),
                "pos_after": st_after.get("pos"),
                "collar_anchor": list(collar_anchor),
                "dug_during_return": self.dug_during_return - dug_before,
                "tree": self.snapshot(),
            }
            if (
                not ok_ret and (err_ret or "").startswith("backtrack_blocked@")
                and st_after.get("on_ground") is True
                and self._climb_target_reached(st_after, cur_pos[1])
                and (not isinstance(hp_after, (int, float)) or hp_after >= min_hp_ref[0])
            ):
                end = _floor_pos(st_after["pos"])
                if math.dist(end, collar_anchor) < math.dist(cur_pos, collar_anchor):
                    result.update(ok=True, stop_reason="return_collar_progress", rejoin_error=err_ret)
                if end[1] < collar_anchor[1]:
                    result["message"] = (
                        "已确认当前站位，记录的下一段回程受阻。若只需出井，可调用 action='climb_out' 查找安全上行路线。"
                        "若需要回原井口补给，请先按回执中的阻塞坐标核对踏点与绕路。"
                    )
            return result

        if act != "explore":
            return {"ok": False, "error": "bad_args", "message": f"未知 action={action!r}，支持 explore / return_collar / reset / status。"}

        inv0, counts0 = _inventory_snapshot_and_counts(self.bot)
        picks0, max_tier0, total_rem0 = _pickaxe_inventory_summary(inv0)
        if total_rem0 <= 6 and self._try_recraft_burner_pickaxe(counts0, at_durability_redline=True):
            # Entry gate used to bail out before any recraft attempt, which made the model walk back
            # to the collar, find no tools there either, and loop (live: 3x returned_to_collar).
            inv0, counts0 = _inventory_snapshot_and_counts(self.bot)
            picks0, max_tier0, total_rem0 = _pickaxe_inventory_summary(inv0)
        if not picks0 or total_rem0 <= 6:
            recovery = self._pickaxe_low_recovery(counts0, st0.get("pos"))
            recovery["message"] = "可用镐缺失或总剩余耐久过低。" + recovery["message"]
            return {
                "ok": False,
                "requires_replan": True,
                "strategy": self.strategy,
                "stop_reason": "pickaxe_low",
                "pos_after": st0.get("pos"),
                "tree": self.snapshot(),
                **recovery,
            }
        if max_tier0 < int(profile["min_tier"]):
            return {
                "ok": False,
                "requires_replan": True,
                "strategy": self.strategy,
                "stop_reason": "insufficient_pickaxe_tier",
                "required_tier": int(profile["min_tier"]),
                "current_max_tier": max_tier0,
                "message": f"MiningDFS 目标 {profile['profile_key']} 需要至少等级 {profile['min_tier']} 的镐，当前背包最高镐等级为 {max_tier0}，请先升镐或改采低阶矿。",
                "tree": self.snapshot(),
            }

        if not replan_layer:
            self._scan_ore_radar(cur_pos, target_blocks=target_blocks, max_tier=max_tier0, geofence=geofence, force=True)

        if not self._switch_to_dry_collar_after_preemption(st0, cur_pos, geofence=geofence):
            return {
                "ok": False,
                "requires_replan": True,
                "action": act,
                "strategy": self.strategy,
                "stop_reason": "water_reentry_avoided",
                "pos_before": st0.get("pos"),
                "pos_after": st0.get("pos"),
                "message": "旧矿道刚被积水中断。请沿干地离开进水点，选一个有支撑的干燥入口再 explore。",
                "tree": self.snapshot(),
            }

        # A failed restore leaves its return route loaded. Recheck the queued descent on the next explore too.
        needs_layer_plan = replan_layer or self._has_stale_descent(
            requested_y, observed_layers={point[1] for point in self.radar_targets},
        )
        if needs_layer_plan and not self._replan_shallower_layer(
            st0, cur_pos, target_y=requested_y, target_blocks=target_blocks if replan_layer else (),
            max_tier=max_tier0, geofence=geofence,
        ):
            return self._layer_switch_refusal(st0)

        # Tree initialization, WorldLedger shaft restoration, or resume from Collar / current node
        if self.collar_node_id is None or self.collar_node_id not in self.nodes:
            # Restore exactly once: this call mutates state, so it must not sit inside a boolean.
            restored = self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref)
            if (
                not restored
                and self.collar_node_id not in self.nodes
                and self.collar_ceiling_y is not None
                and cur_pos[1] <= int(self.collar_ceiling_y) - 20
            ):
                # Live s01/s03: after a deep reset this path opened a "collar" at y=-54,
                # so every later reset looked legitimate and the run churned underground.
                # An explore at or above the target layer must not spend its
                # slice digging away from it when no shaft can be restored.
                if cur_pos[1] >= self.target_y - SCAN_BAND or time.monotonic() + 6.0 >= deadline:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "action": act,
                        "strategy": self.strategy,
                        "stop_reason": "needs_surface_climb",
                        "next_action": "climb_out",
                        "pos_after": st0.get("pos"),
                        "message": self._surface_climb_hint(),
                        "tree": self.snapshot(),
                    }
                climb = self._climb_out(deadline=deadline)
                if not climb["reached"]:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "action": act,
                        "strategy": self.strategy,
                        "stop_reason": "needs_surface_climb",
                        "climb": climb,
                        "message": (
                            f"已尝试自动爬升，y {climb['start_y']}→{climb['end_y']}"
                            f"（目标 y={climb['target_y']}）。" + self._surface_climb_hint()
                        ),
                        "tree": self.snapshot(),
                    }
                cur_pos = _floor_pos(self.bot.status().get("pos") or cur_pos)
                restored = self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref)
            if not restored:
                if self.collar_node_id in self.nodes:
                    return self._deep_shaft_rejoin_failure()
                solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                    cur_pos,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                )
                if not viable_dirs:
                    return self._collar_failure(anchor_err, solid_pos, geofence)
                self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
        else:
            collar = self.nodes[self.collar_node_id]
            active = self.nodes.get(self.active_node_id or -1)
            if not self._tree_has_pending_branches():
                self._archive_current_shaft_to_ledger(mark_exhausted=True)
                self._clear_active_tree_only()
                solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                    cur_pos,
                    geofence=geofence,
                    deadline=deadline,
                    min_hp_ref=min_hp_ref,
                )
                if anchor_err is not None and not viable_dirs:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "strategy": self.strategy,
                        "stop_reason": "shaft_exhausted",
                        "message": "当前矿井已枯竭且附近无安全新井落点，请先用 mc_move_to 移动 20 格以上再调用 explore。",
                        "tree": self.snapshot(),
                    }
                self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
            elif active is None:
                self.active_node_id = collar.node_id
            elif (
                math.dist(cur_pos, active.anchor) > 6.0
                and math.hypot(cur_pos[0] - collar.anchor[0], cur_pos[2] - collar.anchor[2]) > 48.0
                and not self._is_recorded_shaft_position(cur_pos)
                # Combat can leave us just outside the mine near a deep tip,
                # far from the collar. Give local navigation a chance to rejoin.
                and (
                    cur_pos[1] > collar.anchor[1] - 20
                    or math.dist(cur_pos, active.anchor) > NODE_REJOIN_DISTANCE
                )
            ):
                self._archive_current_shaft_to_ledger(mark_exhausted=False)
                self._clear_active_tree_only()
                if not self._try_restore_saved_shaft(cur_pos, min_hp_ref=min_hp_ref):
                    if self.collar_node_id in self.nodes:
                        return self._deep_shaft_rejoin_failure()
                    solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                        cur_pos,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                    )
                    if not viable_dirs:
                        return self._collar_failure(anchor_err, solid_pos, geofence)
                    self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)
            # Rejoin the saved anchor when the current stance cannot connect to the descent.
            elif math.dist(cur_pos, active.anchor) > 3.5 or (
                cur_pos[1] >= active.anchor[1] and cur_pos != active.anchor
                and any(
                    branch.kind == "stair_down" and branch.status == "pending"
                    and not _descent_start_beside_anchor(cur_pos, active.anchor, branch.direction)
                    for branch in active.branches
                )
            ):
                resume_at_depth = self._should_resume_at_depth(active, cur_pos)
                err_nav = None
                local_obstruction = False
                if resume_at_depth:
                    ok_nav = self._resume_explore_at_depth(active, cur_pos, min_hp_ref=min_hp_ref)
                else:
                    ok_nav, err_nav = self._resume_shaft_tip(active.node_id, min_hp_ref=min_hp_ref)
                    local_obstruction = (
                        (err_nav or "").startswith("backtrack_blocked@")
                        and abs(cur_pos[1] - self.target_y) <= SCAN_BAND
                    )
                    if local_obstruction:
                        ok_nav = self._resume_local_branch(active.node_id, min_hp_ref=min_hp_ref)
                    elif (
                        not ok_nav
                        and (
                            self._ahead_of_pending_descent(active, cur_pos)
                            or (
                                active.kind == "spine"
                                and 0 < active.anchor[1] - cur_pos[1] <= SCAN_BAND
                                and math.dist(cur_pos, active.anchor) <= NODE_REJOIN_DISTANCE
                            )
                        )
                        and self._recover_failed_shaft_rejoin(active, cur_pos, err_nav, min_hp_ref=min_hp_ref)
                    ):
                        ok_nav = True
                    elif (
                        not ok_nav and active.kind in {"switchback", "spine"}
                        and err_nav not in {"hurt", "not_ready", "not_grounded"}
                    ):
                        ok_nav = self._resume_deepest_branch(active, min_hp_ref=min_hp_ref)
                if not ok_nav:
                    if local_obstruction:
                        self.active_node_id = active.node_id
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "action": act,
                            "strategy": self.strategy,
                            "stop_reason": "shaft_rejoin_blocked",
                            "rejoin_error": err_nav,
                            "pos_after": self.bot.status()["pos"],
                            "message": "目标层的矿道受阻。请根据阻挡坐标从同层绕行或清理可安全移除的障碍，再继续 explore。",
                            "tree": self.snapshot(),
                        }
                    if resume_at_depth:
                        return self._deep_shaft_rejoin_failure()
                    if (
                        self.collar_ceiling_y is not None
                        and cur_pos[1] <= int(self.collar_ceiling_y) - 20
                        and abs(cur_pos[1] - self.target_y) > 12
                    ):
                        # Same trap as the reset path: re-initialising far above the target layer
                        # plants a fake underground "collar". However, if we are ALREADY at the
                        # target depth band (e.g. Y=-54 for diamond, BUG-093), re-anchor locally
                        # instead of forcing an 80-block climb_out!
                        last_status = self._budget_session.last_status if self._budget_session is not None else {}
                        return {
                            "ok": False,
                            "requires_replan": True,
                            "action": act,
                            "strategy": self.strategy,
                            "stop_reason": "needs_surface_climb",
                            "rejoin_error": err_nav,
                            "pos_before": st0.get("pos"),
                            "pos_after": last_status.get("pos"),
                            "message": (
                                f"回撤到当前节点失败，且你已在地下 y={cur_pos[1]}"
                                f"（最高井口 y={self.collar_ceiling_y}）。" + self._surface_climb_hint()
                            ),
                            "tree": self.snapshot(),
                        }
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
                    self._clear_active_tree_only()
                    solid_pos, viable_dirs, anchor_err = self._ensure_solid_collar_anchor(
                        cur_pos, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                    )
                    if not viable_dirs:
                        return self._collar_failure(anchor_err, solid_pos, geofence)
                    self._init_collar(solid_pos, target_y=self.target_y, geofence=geofence, viable_dirs=viable_dirs)

        if self.dry_calls >= DRY_CALLS_BEFORE_ABANDON and self.collar_node_id in self.nodes:
            # Dead shaft: 40 minutes of zero target yield. Archive it and tell the model to move.
            self._archive_current_shaft_to_ledger(mark_exhausted=True)
            self._clear_active_tree_only()
            self.dry_calls = 0
            return {
                "ok": False,
                "requires_replan": True,
                "action": act,
                "strategy": self.strategy,
                "stop_reason": "shaft_dry_abandoned",
                "message": (
                    f"本井连续 {DRY_CALLS_BEFORE_ABANDON} 次调用没有产出任何 {profile['profile_key']}，"
                    "判定为死井并归档。请换到 16 格以外的新点位："
                    "mc_move_to 平移后 explore；若在地下先 action='climb_out' 回地表。"
                ),
                "tree": self.snapshot(),
            }

        if self.idle_calls >= IDLE_CALLS_BEFORE_RELOCATE:
            # Live s05: 9 consecutive explore calls returned ok=true with gained={} and an
            # unchanged position; the model had no signal and kept retrying the same dead node.
            self.idle_calls = 0
            # Do NOT clear the tree first: the helper needs the collar y to decide whether a
            # surface relocation is even physically possible from here.
            moved, _ = self._force_relocate_on_fluid(
                geofence=geofence, deadline=deadline,
                min_hp_ref=min_hp_ref, auto_relocations=0,
            )
            if not moved:
                return {
                    "ok": False,
                    "requires_replan": True,
                    "action": act,
                    "strategy": self.strategy,
                    "stop_reason": "shaft_exhausted",
                    "message": (
                        "连续 3 次调用都没有挖到东西也没移动，这条矿井已判定空转并归档。"
                        "请先 action='return_collar' 返回地表再 explore 开新井。"
                    ),
                    "tree": self.snapshot(),
                }

        mined_ores: list[list[int]] = []
        breaches_sealed = 0
        catch_placed = 0
        deferred_added = 0
        stop_reason = "time_slice_progress"
        auto_relocations_this_slice = 0

        def _gained_target_count(after_counts: dict[str, int]) -> int:
            delta = _positive_inv_delta(counts0, after_counts)
            return sum(int(delta.get(item_n, 0)) for item_n in expected_items)

        min_target_tier = int(profile["min_tier"])

        def _quota_met() -> bool:
            return _gained_target_count(_inventory_counts(self.bot)) >= goal_count

        # Priority 1: harvest unlocked deferred ores whose required tier is now satisfied!
        unlockable = [
            rec for rec in sorted(self.deferred_ores.values(), key=lambda r: (r.required_tier, r.nearest_node_id))
            if rec.block_name in target_blocks and rec.required_tier <= max_tier0
        ]
        working_node = self.nodes.get(self.active_node_id)
        preserve_depth = working_node is not None and any(branch.status == "pending" for branch in working_node.branches)
        deferred_ceiling = max(cur_pos[1], self.target_y) + DEPTH_RESET_THRESHOLD
        for rec in unlockable:
            if time.monotonic() >= deadline:
                break
            near_node = self.nodes.get(rec.nearest_node_id)
            if near_node is None:
                continue
            near_anc = near_node.anchor
            if preserve_depth and near_anc[1] > deferred_ceiling:
                continue
            # A wide scan can associate ore with a node outside both retrieval ranges.
            vertical = abs(rec.pos[1] - near_anc[1])
            horizontal = abs(rec.pos[0] - near_anc[0]) + abs(rec.pos[2] - near_anc[2])
            scan_band = self.strategy == "fishbone_scan" and abs(near_anc[1] - self.target_y) <= SCAN_BAND
            hook_y, hook_xz = self._vein_hook_reach(target_ore=True, scan_band=scan_band)
            if vertical + horizontal > DROP_PICKUP_RADIUS_MANHATTAN + 2 and (vertical > hook_y or horizontal > hook_xz):
                continue
            ok_nav, err_nav = self._navigate_to_node(rec.nearest_node_id, min_hp_ref=min_hp_ref)
            if not ok_nav:
                stop_reason = err_nav or "deferred_nav_failed"
                break
            hook = self._run_vein_hook(
                near_anc,
                target_blocks=target_blocks,
                current_node_id=rec.nearest_node_id,
                geofence=geofence,
                cache={},
                min_target_tier=min_target_tier,
                min_hp_ref=min_hp_ref,
                quota_met=_quota_met,
            )
            mined_ores.extend(hook["mined_coords"])
            catch_placed += hook["catch_placed"]
            breaches_sealed += hook["breaches_sealed"]
            if _quota_met():
                stop_reason = "quota_reached"
                break
            if rec.pos in self.deferred_ores:
                ok_direct, c_dir, s_dir = self._mine_vein_ore_cell(
                    rec.pos,
                    rec.block_name,
                    anchor=near_anc,
                    target_set=set(target_blocks),
                    cache={},
                )
                catch_placed += c_dir
                breaches_sealed += s_dir
                if ok_direct:
                    mined_ores.append(list(rec.pos))
                    if rec.pos != (near_anc[0], near_anc[1] - 1, near_anc[2]):
                        self._move_exact(near_anc)
                    self._equip_for_block_tier(1, for_ore=False)
                if _quota_met():
                    stop_reason = "quota_reached"
                    break

        # Priority 2: main DFS loop
        blocked_branches: set[int] = set()
        turned_after_first_dig_timeout = False
        idle_streak = 0
        idle_since = time.monotonic()
        last_excavated = len(self.excavated_positions)
        last_node_count = len(self.nodes)
        last_active_node = self.active_node_id
        while stop_reason != "quota_reached" and time.monotonic() < deadline:
            # Spin guard: this loop is deadline-bounded only, so an iteration that neither
            # excavates, nor grows the tree, nor switches nodes would burn the whole slice
            # doing nothing (observed live as a 32s no-op call).
            if (
                len(self.excavated_positions) == last_excavated
                and len(self.nodes) == last_node_count
                and self.active_node_id == last_active_node
            ):
                idle_streak += 1
                # Wall clock matters too: with a slow/rate-limited API one iteration can take
                # 10s, so a 4-iteration threshold may never fit inside a 32s slice (live: five
                # consecutive no-op calls each burned the full slice).
                import sys as _sys
                _max_idle_iter = getattr(_sys.modules.get("mc_agent.skills.mining_dfs"), "MAX_IDLE_ITERATIONS", MAX_IDLE_ITERATIONS)
                _max_idle_sec = getattr(_sys.modules.get("mc_agent.skills.mining_dfs"), "MAX_IDLE_SECONDS", MAX_IDLE_SECONDS)
                if idle_streak >= _max_idle_iter or (time.monotonic() - idle_since) >= _max_idle_sec:
                    if self._active_deep_branch_pending():
                        stop_reason = "no_progress"
                        break
                    idle_streak = 0
                    idle_since = time.monotonic()
                    # Stuck tree: try one in-slice relocation to a fresh collar before giving up,
                    # otherwise the next LLM round would hit the same dead end again.
                    if auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        self._archive_current_shaft_to_ledger(mark_exhausted=True)
                        self._clear_active_tree_only()
                        cur_now = _floor_pos(self.bot.status().get("pos") or [0, 64, 0])
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = "no_progress"
                    break
            else:
                idle_streak = 0
                idle_since = time.monotonic()
            last_excavated = len(self.excavated_positions)
            last_node_count = len(self.nodes)
            last_active_node = self.active_node_id

            if self.active_node_id is None or self.active_node_id not in self.nodes:
                stop_reason = "shaft_exhausted"
                break
            node = self.nodes[self.active_node_id]

            self._scan_ore_radar(node.anchor, target_blocks=target_blocks, max_tier=max_tier0, geofence=geofence)

            hook0 = self._run_vein_hook(
                node.anchor,
                target_blocks=target_blocks,
                current_node_id=node.node_id,
                geofence=geofence,
                cache={},
                min_target_tier=min_target_tier,
                min_hp_ref=min_hp_ref,
                quota_met=_quota_met,
            )
            mined_ores.extend(hook0["mined_coords"])
            deferred_added += hook0["deferred_added"]
            catch_placed += hook0["catch_placed"]
            breaches_sealed += hook0["breaches_sealed"]
            if _quota_met():
                stop_reason = "quota_reached"
                break

            edge = node.edge_from_parent
            if (
                node.kind == "spine" and edge is not None and not self.radar_enabled
                and node.anchor[1] - self.target_y > SPINE_DESCEND_MIN_DELTA
                and not any(b.kind == "stair_down" and b.branch_target_y == self.target_y for b in node.branches)
            ):
                # An existing spine can still have the branch plan from its old target layer.
                plan = self._spine_branches(edge.direction, node.anchor, geofence)
                if plan and plan[0].kind == "stair_down":
                    node.branches.insert(0, plan[0])

            next_branch = next((b for b in node.branches if b.status == "pending" and id(b) not in blocked_branches), None)
            if next_branch is None:
                descending = (
                    edge is not None and node.kind == "switchback" and node.anchor[1] > self.target_y + SCAN_BAND
                )
                alternatives = None
                if descending:
                    # Check untried dry turns even if the existing descent is cooling down.
                    tried = {b.direction for b in node.branches} | {OPPOSITE_DIR.get(edge.direction)}
                    directions = tuple(d for d in DIRECTIONS if d not in tried)
                    alternatives = self._probe_dry_descent(
                        node.anchor, directions=directions, geofence=geofence, min_hp_ref=min_hp_ref,
                    )
                    if alternatives:
                        node.branches.extend(alternatives)
                        continue
                if any(b.status == "pending" for b in node.branches):
                    if node.kind == "spine" and self._resume_deepest_branch(
                        node, min_hp_ref=min_hp_ref, blocked_branches=blocked_branches,
                    ):
                        continue
                    stop_reason = "dig_cell_avoided"
                    break
                spine_blocked = any(b.kind == "spine_forward" and b.status == "pruned" for b in node.branches)
                if node.kind == "spine" and spine_blocked:
                    promo = max(
                        (
                            b
                            for b in node.branches
                            if b.kind == "branch_leaf"
                            and b.status == "completed"
                            and b.prune_reason != "promoted_to_spine"
                            and len(b.completed_waypoints) >= 3
                        ),
                        key=lambda b: (
                            self._radar_direction_projection(node.anchor, b.direction, at_spine=True),
                            len(b.completed_waypoints),
                        ),
                        default=None,
                    )
                    if promo is not None:
                        promo.prune_reason = "promoted_to_spine"
                        ok_pr, err_pr = self._walk_waypoints(
                            promo.completed_waypoints,
                            reverse=False,
                            min_hp_ref=min_hp_ref,
                        )
                        if not ok_pr:
                            stop_reason = err_pr or "promote_walk_failed"
                            break
                        end_tip = promo.completed_waypoints[-1]
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_tip,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("branch_leaf", promo.direction, list(promo.completed_waypoints)),
                            branches=self._spine_branches(promo.direction, end_tip, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                        continue

                if node.parent_id is None or edge is None:
                    self._archive_current_shaft_to_ledger(mark_exhausted=True)
                    if auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        cur_now = _floor_pos(self.bot.status()["pos"])
                        self._clear_active_tree_only()
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now,
                            geofence=geofence,
                            deadline=deadline,
                            min_hp_ref=min_hp_ref,
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = "shaft_exhausted"
                    break
                at_final_stair = (
                    node.kind == "switchback" and edge.edge_kind == "stair_down"
                    and abs(node.anchor[1] - self.target_y) <= SCAN_BAND
                )
                if (descending and alternatives is not None) or at_final_stair:
                    # Avoid giving up the final stair when a same-level offset can bypass its blocked landing.
                    if any(
                        self._enqueue_offset_retry_if_eligible(
                            node, branch, branch.prune_reason or "", geofence, after_exhaustion=True,
                        )
                        for branch in tuple(node.branches)
                        if branch.status == "pruned"
                        and (descending or branch.prune_reason == "movement_not_confirmed")
                    ):
                        continue
                parent = self.nodes.get(node.parent_id)
                if parent is not None and parent.anchor[1] > node.anchor[1]:
                    # Resuming at a cleared transit node should lead back down
                    # to unfinished work below it before walking up the shaft.
                    if self._resume_deepest_branch(node, min_hp_ref=min_hp_ref, descendants_only=True):
                        continue
                reached_y = min(cur_pos[1], node.anchor[1])
                ascent_ceiling = max(reached_y, self.target_y) + DEPTH_RESET_THRESHOLD
                if reached_y <= self.target_y + SCAN_BAND and (
                    (parent is not None and parent.anchor[1] > ascent_ceiling)
                    or any(waypoint[1] > ascent_ceiling for waypoint in edge.waypoints)
                ):
                    if self._resume_local_branch(node.node_id, min_hp_ref=min_hp_ref):
                        continue
                    stop_reason = "shaft_rejoin_blocked"
                    break
                ok_bt, err_bt = self._walk_waypoints(
                    edge.waypoints, reverse=True, min_hp_ref=min_hp_ref, resume_edge_kind=edge.edge_kind,
                )
                if not ok_bt:
                    if abs(node.anchor[1] - self.target_y) <= SCAN_BAND and self._utility_blocks_backtrack(err_bt or ""):
                        if self._resume_local_branch(node.node_id, min_hp_ref=min_hp_ref):
                            continue
                        stop_reason = err_bt or "backtrack_failed"
                        break
                    if (err_bt or "").startswith("backtrack_blocked"):
                        if self._resume_deepest_branch(node, min_hp_ref=min_hp_ref):
                            continue
                    self._archive_current_shaft_to_ledger(mark_exhausted=False)
                    if not descending and auto_relocations_this_slice < 1 and time.monotonic() + 8.0 < deadline:
                        auto_relocations_this_slice += 1
                        cur_now = _floor_pos(self.bot.status()["pos"])
                        new_site = self._relocate_to_fresh_collar_site(
                            cur_now, geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref
                        )
                        new_dirs = self._probe_collar_dirs(new_site, geofence=geofence)
                        if new_site != cur_now and new_dirs:
                            # A deadline during relocation must leave the old shaft available.
                            self._clear_active_tree_only()
                            self._init_collar(new_site, target_y=self.target_y, geofence=geofence, viable_dirs=new_dirs)
                            continue
                    stop_reason = err_bt or "backtrack_failed"
                    break
                self.active_node_id = node.parent_id
                continue

            branch_kind = next_branch.kind
            is_level = branch_kind not in {"stair_down", "stair_up"}
            if branch_kind == "offset_retry":
                lat_dir = next_branch.sidestep_dir or LEFT_RIGHT_DIRS[next_branch.direction][0]
                if self._cuts_incoming_stair(node.anchor, node.edge_from_parent, lat_dir):
                    next_branch.status = "pruned"
                    next_branch.prune_reason = "stair_support"
                    continue
            if is_level or branch_kind == "stair_down":
                stance = _floor_pos(self.bot.status()["pos"])
                if is_level:
                    needs_rejoin = stance[1] != node.anchor[1] or (branch_kind == "offset_retry" and stance != node.anchor)
                else:
                    x, y, z = node.anchor
                    dx, dz = DIRECTIONS[next_branch.direction]
                    # A displaced start must still connect to this branch's saved anchor.
                    needs_rejoin = (
                        stance not in (node.anchor, (x + 2 * dx, y - 2, z + 2 * dz))
                        and not _descent_start_beside_anchor(stance, node.anchor, next_branch.direction)
                    )
                    if needs_rejoin and stance[1] < y - DEPTH_RESET_THRESHOLD:
                        stop_reason = "movement_not_confirmed"
                        break
                if needs_rejoin:
                    reached, reason = self._resume_shaft_tip(node.node_id, min_hp_ref=min_hp_ref)
                    if not reached:
                        if auto_relocations_this_slice < 1 and self._recover_failed_shaft_rejoin(
                            node, stance, reason, min_hp_ref=min_hp_ref,
                        ):
                            auto_relocations_this_slice += 1
                            continue
                        stop_reason = reason or "movement_not_confirmed"
                        break
            next_branch.status = "active"
            try:
                if branch_kind in {"stair_down", "stair_up"}:
                    is_up = branch_kind == "stair_up"
                    eff_y = next_branch.branch_target_y if next_branch.branch_target_y is not None else self.target_y
                    stair_fn = self._dig_stair_up_edge if is_up else self._dig_stair_down_edge
                    edge_res = stair_fn(
                        direction=next_branch.direction,
                        max_steps=next_branch.steps,
                        target_y=eff_y,
                        target_blocks=target_blocks,
                        parent_node_id=node.node_id,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                        min_target_tier=min_target_tier,
                        quota_met=_quota_met,
                    )
                else:
                    edge_res = self._dig_level_edge(
                        direction=lat_dir if branch_kind == "offset_retry" else next_branch.direction,
                        steps=OFFSET_RETRY_STEPS if branch_kind == "offset_retry" else next_branch.steps,
                        target_blocks=target_blocks,
                        parent_node_id=node.node_id,
                        geofence=geofence,
                        deadline=deadline,
                        min_hp_ref=min_hp_ref,
                        min_target_tier=min_target_tier,
                        quota_met=_quota_met,
                    )
            except _MiningDeadline:
                if (
                    turned_after_first_dig_timeout
                    or not any(b.status == "pending" and id(b) not in blocked_branches for b in node.branches)
                    or not self._skip_stalled_first_dig(deadline=deadline, min_hp=min_hp_ref[0])
                ):
                    raise
                next_branch.status = "pending"
                blocked_branches.add(id(next_branch))
                turned_after_first_dig_timeout = True
                idle_streak = 0
                idle_since = time.monotonic()
                continue
            steps_done = int(edge_res["steps_done"])
            er = str(edge_res["stop_reason"])
            breaches_sealed += int(edge_res["breaches_sealed"])
            catch_placed += int(edge_res["catch_placed"])
            deferred_added += int(edge_res["deferred_added"])
            mined_ores.extend(edge_res["mined_ores"])
            if is_level and any(pt[1] != node.anchor[1] for pt in edge_res["waypoints"]):
                # A sidestep or level corridor cannot connect nodes on different floors.
                next_branch.status = "pending"
                stop_reason = er if er in {"hurt", "not_ready", "pickaxe_low"} else "movement_not_confirmed"
                break
            if er == "dig_cell_avoided":
                if steps_done == 0:
                    next_branch.status = "pending"
                    blocked_branches.add(id(next_branch))
                    idle_streak = 0
                    idle_since = time.monotonic()
                    continue
                # Keep the traversed edge and resume after the cooldown.
                er = "time_slice"

            if branch_kind == "offset_retry":
                if er == "quota_reached":
                    next_branch.status = "completed"
                    if steps_done >= 1:
                        self._record_offset_branch(node, next_branch, edge_res["waypoints"], lat_dir)
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    stop_reason = er
                    break
                if er == "time_slice" and steps_done == 0:
                    next_branch.status = "pending"
                    stop_reason = "time_slice_progress"
                    break
                if steps_done >= 2:
                    sh_node = self._record_offset_branch(node, next_branch, edge_res["waypoints"], lat_dir)
                    self._maybe_place_torch_at_node(sh_node)
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if steps_done > 0:
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)

            elif branch_kind in {"stair_down", "stair_up"}:
                end_anchor = tuple(edge_res["end_anchor"])
                if not is_up:
                    waypoints = edge_res["waypoints"]
                    verified_route = _descending_waypoint_prefix(waypoints, node.anchor[1])
                    if len(verified_route) < len(waypoints):
                        next_branch.status = "pending"
                        if verified_route and verified_route[0] == node.anchor:
                            descent = node.anchor[1] - verified_route[-1][1]
                            if descent > 0:
                                self._record_partial_branch(node, next_branch, verified_route, descent)
                        stop_reason = er if er in {"hurt", "not_ready", "pickaxe_low"} else "movement_not_confirmed"
                        break
                reached_band = (end_anchor[1] >= eff_y - 1) if is_up else (end_anchor[1] <= eff_y + 2)

                if self._note_fluid_abort(er):
                    detoured = (
                        not is_up and steps_done > 0 and end_anchor[1] > self.target_y + 12
                        and self._continue_descent_around_hazard(
                            node, next_branch, edge_res["waypoints"], geofence=geofence, min_hp_ref=min_hp_ref,
                        )
                    )
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if detoured:
                        self.fluid_aborts = 0
                        continue
                    if not steps_done and self._retry_dry_junction(node, geofence=geofence, min_hp_ref=min_hp_ref):
                        continue
                    moved, auto_relocations_this_slice = self._force_relocate_on_fluid(
                        geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref,
                        auto_relocations=auto_relocations_this_slice,
                    )
                    if moved:
                        continue
                    stop_reason = "shaft_exhausted"
                    break

                if er == "quota_reached":
                    next_branch.status = "completed"
                    if steps_done > 0 and end_anchor != node.anchor:
                        rem_dq = max(1, abs(end_anchor[1] - eff_y))
                        q_node = self._create_node(
                            kind="spine" if reached_band else "switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=(
                                self._spine_branches(next_branch.direction, end_anchor, geofence)
                                if reached_band
                                else [BranchSpec(next_branch.kind, next_branch.direction, rem_dq, branch_target_y=eff_y)]
                                + self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                            ),
                        )
                        self._maybe_place_torch_at_node(q_node)
                    stop_reason = "quota_reached"
                    break

                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    elif steps_done > 0 and end_anchor != node.anchor:
                        rem_d = max(1, abs(end_anchor[1] - eff_y))
                        next_branch.status = "completed"
                        self._create_node(
                            kind="spine" if reached_band else "switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=(
                                self._spine_branches(next_branch.direction, end_anchor, geofence)
                                if reached_band
                                else [BranchSpec(next_branch.kind, next_branch.direction, rem_d, branch_target_y=eff_y)]
                                + self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                            ),
                        )
                    stop_reason = er
                    break

                # HARD SPIN GUARD: never spawn a 0-displacement child node at the exact same coordinate!
                if steps_done == 0 or end_anchor == node.anchor:
                    if er == "time_slice":
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    next_branch.status = "completed" if er == "reached_target_y" else "pruned"
                    if er != "reached_target_y":
                        next_branch.prune_reason = er
                        self._move_exact(node.anchor)
                        self._enqueue_offset_retry_if_eligible(node, next_branch, er, geofence)
                    elif not is_up and node.kind == "switchback" and end_anchor == node.anchor:
                        # A previous slice can stop on the final stair before opening the spine.
                        node.kind = "spine"
                        node.branches.extend(self._spine_branches(next_branch.direction, node.anchor, geofence))
                    continue

                if er in {"reached_target_y", "completed"} and reached_band:
                    next_branch.status = "completed"
                    spine_node = self._create_node(
                        kind="spine",
                        anchor=end_anchor,
                        parent_id=node.node_id,
                        edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                        branches=self._spine_branches(next_branch.direction, end_anchor, geofence),
                    )
                    self._maybe_place_torch_at_node(spine_node)
                elif er in {"time_slice", "completed"} and steps_done > 0:
                    next_branch.status = "completed"
                    if not reached_band:
                        rem_depth = max(1, abs(end_anchor[1] - eff_y))
                        cont_branches = [BranchSpec(next_branch.kind, next_branch.direction, rem_depth, branch_target_y=eff_y)]
                        if not is_up:
                            cont_branches.extend(
                                self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                            )
                        self._create_node(
                            kind="switchback",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=cont_branches,
                        )
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if steps_done >= 2 and reached_band:
                        left_d, _ = LEFT_RIGHT_DIRS[next_branch.direction]
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(left_d, end_anchor, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                    elif steps_done >= 2 and not is_up:
                        sw_branches = self._switchback_branches(next_branch.direction, end_anchor[1], eff_y, end_anchor, geofence)
                        if sw_branches:
                            sw_node = self._create_node(
                                kind="switchback",
                                anchor=end_anchor,
                                parent_id=node.node_id,
                                edge_from_parent=ShaftEdge(next_branch.kind, next_branch.direction, edge_res["waypoints"]),
                                branches=sw_branches,
                            )
                            self._maybe_place_torch_at_node(sw_node)
                        else:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    else:
                        if steps_done > 0:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                        else:
                            self._move_exact(node.anchor)
                        self._enqueue_offset_retry_if_eligible(node, next_branch, er, geofence)

            else:
                # "branch_leaf" or "spine_forward"
                if self._note_fluid_abort(er):
                    next_branch.status = "pruned"
                    next_branch.prune_reason = er
                    if not steps_done and self._retry_dry_junction(node, geofence=geofence, min_hp_ref=min_hp_ref):
                        continue
                    if self._retry_dry_parent_after_level_abort(
                        node, edge_res["waypoints"], geofence=geofence, min_hp_ref=min_hp_ref,
                    ):
                        continue
                    moved, auto_relocations_this_slice = self._force_relocate_on_fluid(
                        geofence=geofence, deadline=deadline, min_hp_ref=min_hp_ref,
                        auto_relocations=auto_relocations_this_slice,
                    )
                    if moved:
                        continue
                    stop_reason = "shaft_exhausted"
                    break

                if er == "quota_reached":
                    next_branch.status = "completed"
                    end_anchor_q = tuple(edge_res["end_anchor"])
                    if next_branch.kind == "spine_forward" and steps_done >= 1 and end_anchor_q != node.anchor:
                        q_sp = self._create_node(
                            kind="spine",
                            anchor=end_anchor_q,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor_q, geofence),
                        )
                        self._maybe_place_torch_at_node(q_sp)
                    elif next_branch.kind == "branch_leaf" and steps_done > 0:
                        if steps_done >= 2:
                            next_branch.completed_waypoints = list(edge_res["waypoints"])
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    stop_reason = "quota_reached"
                    break
                if er in {"hurt", "not_ready", "pickaxe_low"}:
                    end_anchor_s = tuple(edge_res["end_anchor"])
                    if steps_done == 0 and er != "pickaxe_low":
                        next_branch.status = "pending"
                    elif steps_done >= 2 and next_branch.kind == "spine_forward" and end_anchor_s != node.anchor:
                        next_branch.status = "completed"
                        self._create_node(
                            kind="spine",
                            anchor=end_anchor_s,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor_s, geofence),
                        )
                    elif steps_done > 0 and next_branch.kind == "branch_leaf":
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    stop_reason = er
                    break

                if next_branch.kind == "branch_leaf":
                    if er == "time_slice" and steps_done == 0:
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    next_branch.status = "completed" if er in {"completed", "time_slice"} else "pruned"
                    if er in {"completed", "time_slice"} and steps_done >= 2:
                        next_branch.completed_waypoints = list(edge_res["waypoints"])
                    if er not in {"completed", "time_slice"}:
                        next_branch.prune_reason = er
                    if steps_done > 0:
                        self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)
                    if er == "time_slice":
                        stop_reason = "time_slice_progress"
                        break
                else:
                    end_anchor = tuple(edge_res["end_anchor"])
                    if er == "time_slice" and steps_done == 0:
                        next_branch.status = "pending"
                        stop_reason = "time_slice_progress"
                        break
                    if (
                        er in {"completed", "time_slice"}
                        and end_anchor != node.anchor
                        and steps_done >= 1
                    ):
                        next_branch.status = "completed"
                        sp_node = self._create_node(
                            kind="spine",
                            anchor=end_anchor,
                            parent_id=node.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", next_branch.direction, edge_res["waypoints"]),
                            branches=self._spine_branches(next_branch.direction, end_anchor, geofence),
                        )
                        self._maybe_place_torch_at_node(sp_node)
                        if er == "time_slice":
                            stop_reason = "time_slice_progress"
                            break
                    else:
                        next_branch.status = "pruned"
                        next_branch.prune_reason = er
                        if steps_done > 0:
                            self._walk_waypoints(edge_res["waypoints"], reverse=True, min_hp_ref=min_hp_ref)

            if _quota_met():
                stop_reason = "quota_reached"
                break

        inv_after, counts_after = _inventory_snapshot_and_counts(self.bot)
        gained_delta = _positive_inv_delta(counts0, counts_after)
        target_gained = _gained_target_count(counts_after)
        if target_gained >= goal_count:
            stop_reason = "quota_reached"

        returned_collar_on_low = False
        if stop_reason == "pickaxe_low":
            returned_collar_on_low = self._maybe_return_collar_for_resupply(counts_after, min_hp_ref=min_hp_ref)

        self._archive_current_shaft_to_ledger(mark_exhausted=(stop_reason == "shaft_exhausted"))
        self._equip_for_block_tier(1, for_ore=False, inv_snapshot=inv_after)
        st_end = self.bot.status()

        ok = stop_reason in {"quota_reached", "time_slice_progress"}
        if stop_reason == "time_slice_progress" and not gained_delta and st0.get("pos") == st_end.get("pos"):
            self.idle_calls += 1
        else:
            self.idle_calls = 0
        if target_gained <= 0:
            self.dry_calls += 1
        else:
            self.dry_calls = 0
        out_res: dict[str, Any] = {
            "ok": ok,
            "requires_replan": not ok or stop_reason == "hurt",
            "action": act,
            "strategy": self.strategy,
            "target": profile["profile_key"],
            "target_y": self.target_y,
            "goal_count": goal_count,
            "target_gained": target_gained,
            "gained": gained_delta,
            "stop_reason": stop_reason,
            "pos_before": st0.get("pos"),
            "pos_after": st_end.get("pos"),
            "mined_ores": mined_ores,
            "breaches_sealed": breaches_sealed,
            "catch_placed": catch_placed,
            "deferred_added": deferred_added,
            "recrafted_picks": self.recrafted_picks - recrafted_before,
            "tree": self.snapshot(),
        }
        if self.radar_enabled:
            out_res["radar_summary"] = {
                "exposed_count": len(self.radar_targets),
                "ever_seen_count": len(self.ever_seen_radar_ores),
                "locked_cluster": list(self.locked_cluster["rep"]) if self.locked_cluster else None,
                "cluster_size": int(self.locked_cluster["size"]) if self.locked_cluster else 0,
                "mob_nearby": bool(self.locked_cluster.get("mob_nearby", False)) if self.locked_cluster else False,
            }
        if returned_collar_on_low:
            out_res["returned_to_collar_for_resupply"] = True
        if stop_reason == "shaft_exhausted" and self.fluid_abandoned_deep:
            out_res["message"] = (
                "本井连续遇到水或洞穴，已归档回撤路线。请调用 action='climb_out' 优先沿已保存的巷道上爬。"
                "若返回 climb_progress，可从当前站位继续 climb_out。到达地表后再 explore。"
            )
        if stop_reason == "pickaxe_low":
            out_res.update(self._pickaxe_low_recovery(counts_after, st_end.get("pos")))
        if stop_reason == "dig_cell_avoided":
            out_res["message"] = "当前可走分支的挖掘格处于超时冷却。请先处理障碍，或改从同层的安全站位进矿。"
        if stop_reason == "shaft_rejoin_blocked":
            out_res["message"] = "目标层没有可直接接续的已知分支。请从同层安全位置继续探矿，或调用 climb_out 回撤。"
        if stop_reason == "no_progress":
            if self._active_deep_branch_pending():
                out_res["message"] = "已保留当前深度的矿道和待挖分支。请在同层确认安全站位，再继续 explore。"
            else:
                out_res["message"] = (
                    "在原地连续多次既没挖到方块也没推进矿井节点（可能卡在死胡同反复重试），已提前结束本轮而不是空耗时间。"
                    "请先调整站位；有井口记录时用 return_collar 回撤，空间树为空时直接 explore 开新井。"
                )
        if stop_reason == "shaft_exhausted" and not self.fluid_abandoned_deep:
            out_res["message"] = (
                "当前矿井分支与偏移重试均已穷尽；请先移到 16 格以外的安全新点位，再直接 explore。"
                "若仍在地下，有井口记录时用 return_collar，空间树为空时用 climb_out 回撤。"
            )
        if self._utility_blocks_backtrack(stop_reason):
            out_res["message"] = "目标层的矿道受阻。请根据阻挡坐标从同层绕行或清理可安全移除的障碍，再继续 explore。"
        return out_res

