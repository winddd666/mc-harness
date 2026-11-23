"""McToolset mixin: skills_bridge."""

from __future__ import annotations

from mc_agent.harness.mc_tools._base import (
    copy,
    dataclass,
    replace,
    json,
    math,
    re,
    threading,
    time,
    Any,
    Callable,
    httpx,
    BotSession,
    ApiError,
    AuthError,
    BotNotReadyError,
    DeathLoopCooldownError,
    assess_world_target,
    build_jit_error_hint,
    lookup_kb,
    normalize_dimension,
    resolve_item_alias,
    MacroSkills,
    SafetyEmergency,
    check_and_stabilize,
    compute_advancement_frontier,
    run_combat_skill,
    BED_BLOCK_TYPES,
    BLOCK_MIN_PICKAXE_TIER,
    LOG_TYPES,
    NETHER_STEM_TYPES,
    SURFACE_SCOUT_DIRECTIONS,
    TABLE_RECIPES,
    VILLAGE_LANDMARK_BLOCKS,
    VILLAGE_SURVEY_CLUTTER,
    _PICKUP_INTERRUPTION_CODES,
    _SkillBudgetSession,
    _SkillDeadline,
    _StairDeadline,
    _StairSession,
    _best_pickaxe_in_inv,
    _can_fit_item_in_inventory,
    _compute_empty_slots,
    _expected_drops_for_blocks,
    _inventory_counts,
    _merge_counts,
    _normalize_drop_name,
    best_melee_weapon_in_inv,
    explain_material_craft_error,
    is_passable_sight_block,
    rejected_find_block,
    SAFE_FOOD_PRIORITY,
    VoidGeofence,
    Parameter,
    Tool,
    ToolRegistry,
    WorldMemory,
    default_memory_path_for_bot,
    _ScoutNumberParameter,
    _BLOCK_COLORS,
    _BLOCK_WOODS,
    _FIND_BLOCK_ALIASES,
    _NON_BLOCK_FIND_TYPES,
    _invalid_find_block_query,
    _normalize_find_block_types,
    COMPACT_CORE_TOOLS,
    _PASSABLE_SIGHT_BLOCKS,
    _FailedMoveWaypoint,
    _COMPACT_TOOL_DESC,
    _SELF_EVIDENT_PARAMS,
    _COMPACT_PARAM_DESC,
    _compactify_tool,
    _build_window_target,
)


class _SkillsBridgeMixin:
    def mc_skill_enchant(
        self,
        item: str,
        option: int,
        lapis_count: int | None = None,
        take: bool = True,
        block: str | None = "enchanting_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        verify: bool = True,
        reconnect_on_stale: bool = True,
    ) -> str:
        def _do_enchant() -> dict[str, Any]:
            before = self._item_enchant_state(item) if (verify and take) else None
            result = self.skills.enchant_item_once(
                item,
                option,
                lapis_count=lapis_count,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
            if verify and take and result.get("ok") and before is not None:
                time.sleep(0.5)
                after = self._item_enchant_state(item)
                verified = bool(after is not None and after[1] > before[1])
                result["inventory_verified"] = verified
                if not verified and reconnect_on_stale:
                    try:
                        self._reset_window_session()
                    except Exception as exc:  # noqa: BLE001
                        result["window_reset_error"] = f"{type(exc).__name__}: {exc}"
                        result["requires_replan"] = True
                    else:
                        refreshed = self._item_enchant_state(item)
                        if refreshed is not None and refreshed[1] > before[1]:
                            # The enchant did land; only the client window was stale.
                            result["inventory_verified"] = True
                            result["reconnected_for_sync"] = True
                        else:
                            # Genuine failure: retry exactly once on the fresh session.
                            retry = self.skills.enchant_item_once(
                                item,
                                option,
                                lapis_count=lapis_count,
                                take=take,
                                block=block,
                                x=x,
                                y=y,
                                z=z,
                            )
                            time.sleep(0.5)
                            after_retry = self._item_enchant_state(item)
                            retry_ok = bool(
                                retry.get("ok") and after_retry is not None and after_retry[1] > before[1]
                            )
                            retry["inventory_verified"] = retry_ok
                            retry["retried_after_reconnect"] = True
                            if not retry_ok:
                                retry["stale_window"] = True
                                retry["requires_replan"] = True
                            result = retry
            return result

        return self._run_skill(_do_enchant)


    def _item_enchant_state(self, item: str) -> tuple[int, int] | None:
        """Return (total_count, enchanted_count) for one item, or None if unreadable."""
        try:
            inv = self.bot.call("inventory", {})
        except (ApiError, httpx.HTTPError):
            return None
        want = resolve_item_alias(item)
        total = 0
        enchanted = 0
        for row in inv.get("items") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").removeprefix("minecraft:")
            if name != want:
                continue
            count = int(row.get("count") or 0)
            total += count
            if row.get("enchantments"):
                enchanted += count
        return total, enchanted


    def _reset_window_session(self) -> None:
        """Disconnect + reconnect to clear a stale mineflayer window/inventory snapshot."""
        try:
            self.bot.disconnect()
        except ApiError as exc:
            if exc.code != "not_connected":
                raise
        time.sleep(2.0)
        try:
            self.bot.connect(respect_reconnect_interval=False)
        except ApiError:
            # Strict competition server enforces >=30s reconnect interval; fall back gracefully.
            self.bot.connect(respect_reconnect_interval=True)
        st = self.bot.wait_until_ready(timeout_s=35.0)
        if isinstance(st, dict):
            self._update_status_cache(st)


    def mc_skill_trade(
        self,
        entity_id: int,
        option: int | None = None,
        get_item: str | None = None,
        times: int = 1,
    ) -> str:
        def trade() -> dict[str, Any]:
            budget, imminent = self._clamp_tool_budget(20.0, max_s=20.0)
            if imminent:
                raise ApiError("episode_deadline_imminent", "本集剩余时间不足，交易未开始。")
            return self.skills.merchant_trade(
                entity_id=entity_id, option=option, get_item=get_item, times=times, max_duration_s=budget,
            )

        return self._run_skill(trade)


    def mc_skill_brew(
        self,
        ingredient_item: str,
        bottles: list[dict[str, Any]] | None = None,
        fuel_item: str | None = None,
        wait: bool = True,
        block: str | None = "brewing_stand",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.brew_once(
                ingredient_item,
                bottles=bottles,
                fuel_item=fuel_item,
                wait=wait,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )


    def mc_skill_anvil(
        self,
        left_item: str,
        right_item: str | None = None,
        left_enchanted: bool | None = None,
        right_enchanted: bool | None = None,
        text: str | None = None,
        take: bool = True,
        block: str | None = "anvil",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.anvil_operate(
                left_item,
                right_item=right_item,
                left_enchanted=left_enchanted,
                right_enchanted=right_enchanted,
                text=text,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )


    def mc_skill_smithing(
        self,
        template_item: str,
        base_item: str,
        addition_item: str,
        take: bool = True,
        block: str | None = "smithing_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.smithing_apply(
                template_item,
                base_item,
                addition_item,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )


    def mc_skill_stonecutter(
        self,
        input_item: str,
        option: int,
        count: int | None = None,
        take: bool = True,
        block: str | None = "stonecutter",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.stonecutter_cut(
                input_item,
                option,
                count=count,
                take=take,
                block=block,
                x=x,
                y=y,
                z=z,
            )
        )


    def mc_posture(self, sneak: bool = False, shield: bool = False, jump: bool = False) -> str:
        return self._call_json("posture", {"sneak": sneak, "shield": shield, "jump": jump})


    def mc_dismount(self) -> str:
        return self._call_json("dismount", {})


    def mc_drop(self, item: str, count: int = 1) -> str:
        return self._call_json("drop", {"item": item, "count": max(1, min(64, int(count)))})


    def mc_chat(self, message: str) -> str:
        return self._call_json("chat", {"message": str(message).lstrip("/")[:100]})


    def mc_stop(self) -> str:
        return self._call_json("stop", {})


    def mc_control(
        self,
        forward: bool = False,
        back: bool = False,
        left: bool = False,
        right: bool = False,
        jump: bool = False,
        sneak: bool = False,
        sprint: bool = False,
        yaw: float | None = None,
        pitch: float | None = None,
        hit: int | None = None,
        hit_interval_ms: int | None = None,
        ms: int | None = None,
        wait: bool | None = None,
        stop_on: list[str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {}
        for key, enabled in (
            ("forward", forward),
            ("back", back),
            ("left", left),
            ("right", right),
            ("jump", jump),
            ("sneak", sneak),
            ("sprint", sprint),
        ):
            if enabled:
                payload[key] = True
        if yaw is not None:
            payload["yaw"] = yaw
        if pitch is not None:
            payload["pitch"] = pitch
        if hit is not None:
            payload["hit"] = max(1, min(20, hit))
        if hit_interval_ms is not None:
            payload["hit_interval_ms"] = max(50, min(5000, hit_interval_ms))
        if ms is not None:
            payload["ms"] = max(50, min(3000, ms))
        if wait is not None:
            payload["wait"] = bool(wait)
        if stop_on is not None:
            payload["stop_on"] = stop_on
        return self._call_json("control", payload)


    def mc_look(
        self,
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        yaw: float | None = None,
        pitch: float | None = None,
    ) -> str:
        payload = {}
        for key, val in (
            ("entity_id", entity_id),
            ("x", x),
            ("y", y),
            ("z", z),
            ("yaw", yaw),
            ("pitch", pitch),
        ):
            if val is not None:
                payload[key] = val
        return self._annotate_stale_entity(self._call_json("look", payload), entity_id)


    def mc_advancement_frontier(self, limit: int = 12) -> str:
        limit = max(1, min(50, int(limit)))
        return self._run_skill(lambda: compute_advancement_frontier(self.bot, limit=limit))


    def mc_skill_stabilize(self) -> str:
        return self._run_skill(lambda: check_and_stabilize(self.bot))


    def _cleanup_inventory_after(self, res: Any, *, skills: MacroSkills | None = None) -> Any:
        """BUG-029: enforce retention caps after every mining-style skill."""
        if not isinstance(res, dict):
            return res
        try:
            _, cleaned = (skills if skills is not None else self.skills)._ensure_free_slots(min_free=3)
        except Exception:
            cleaned = []
        if cleaned:
            res["inventory_cleaned"] = [
                {"item": d.get("item"), "count": d.get("count")} for d in cleaned
            ]
        return res


    def mc_skill_harvest_logs(self, target_logs: int = 4, radius: int = 32) -> str:
        target_logs = max(1, min(32, int(target_logs)))
        radius = max(4, min(64, int(radius)))
        budget_s, deadline_imminent = self._clamp_tool_budget(45.0, max_s=45.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "stop_reason": "episode_deadline_imminent",
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过采木。"},
            }, ensure_ascii=False)
        return self._run_skill(
            lambda: self.skills.harvest_logs(
                target_logs=target_logs, radius=radius, max_duration_s=budget_s, cleanup_inventory=True,
            )
        )


    def mc_skill_scout_surface(
        self,
        direction: str = "auto",
        target_types: list[str] | None = None,
        max_duration_s: float | str = 45.0,
        step_dist: float | str = 28.0,
        track_ender_eye: bool = False,
    ) -> str:
        try:
            if any(isinstance(v, bool) for v in (max_duration_s, step_dist)):
                raise ValueError()
            duration, step = float(max_duration_s), float(step_dist)
        except (TypeError, ValueError, OverflowError):
            duration = step = math.nan
        if not math.isfinite(duration) or not math.isfinite(step):
            return self._run_skill(lambda: self.skills.scout_surface(max_duration_s=duration, step_dist=step))
        budget_s, deadline_imminent = self._clamp_tool_budget(duration, max_s=50.0, min_s=0.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "distance_traveled": 0.0,
                "segments_completed": 0,
                "final_pos": None,
                "detected_pois": [],
                "detected_entities": [],
                "stop_reason": "episode_deadline_imminent",
                "requires_replan": True,
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过地表斥候。"},
            }, ensure_ascii=False)
        return self._run_skill(
            lambda: self.skills.scout_surface(
                direction=direction, target_types=target_types, max_duration_s=budget_s, step_dist=step,
                track_ender_eye=track_ender_eye,
            )
        )


    def mc_skill_craft_gear(self, item: str, count: int = 1, equip_slot: str | None = None) -> str:
        count = max(1, min(64, int(count)))

        def craft() -> dict[str, Any]:
            if self._episode_deadline_mono is None:
                return self.skills.craft_gear(item=item, count=count, equip_slot=equip_slot)
            budget_s, deadline_imminent = self._clamp_tool_budget(55.0)
            if deadline_imminent:
                return {
                    "ok": False,
                    "stop_reason": "episode_deadline_imminent",
                    "requires_replan": True,
                    "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过合成。"},
                }
            return self.skills.craft_gear(item=item, count=count, equip_slot=equip_slot, max_duration_s=budget_s)

        return self._run_skill(craft)


    def mc_skill_ensure_items(
        self,
        items: list[dict[str, Any]],
        max_duration_s: int = 150,
    ) -> str:
        dur = max(10, min(600, int(max_duration_s)))
        return self._run_skill(
            lambda: self.skills.ensure_items(
                items,
                max_duration_s=float(dur),
                geofence=self.geofence,
                stop_check=self._stop_check,
            )
        )


    def mc_skill_mine_blocks(self, block_types: list[str], count: int = 3, radius: int = 32) -> str:
        count = max(1, min(32, int(count)))
        radius = max(4, min(64, int(radius)))

        def mine() -> dict[str, Any]:
            budget_s, deadline_imminent = self._clamp_tool_budget(45.0, max_s=45.0)
            if deadline_imminent:
                return {
                    "ok": False,
                    "stop_reason": "episode_deadline_imminent",
                    "requires_replan": True,
                    "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过采矿。"},
                }
            return self.skills.mine_blocks(
                block_types=block_types, count=count, radius=radius, max_duration_s=budget_s,
                skip_unmineable=True, cleanup_inventory=True,
            )

        return self._run_skill(mine)


    def mc_skill_smelt(self, input_item: str, count: int = 1, fuel_item: str | None = None) -> str:
        count = max(1, min(64, int(count)))
        budget_s, deadline_imminent = self._clamp_tool_budget(55.0)
        if deadline_imminent:
            return json.dumps(
                {
                    "ok": False,
                    "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过熔炼。"},
                },
                ensure_ascii=False,
            )
        return self._run_skill(
            lambda: self.skills.smelt_items(
                input_item=input_item, count=count, fuel_item=fuel_item, wait_timeout_s=budget_s
            )
        )


    def mc_skill_collect_drops(
        self, radius: int = 12, max_pickups: int = 8, item_types: list[str] | None = None,
    ) -> str:
        radius = max(2, min(32, int(radius)))
        max_pickups = max(1, min(20, int(max_pickups)))
        if item_types is None:
            return self._run_skill(lambda: self.skills.collect_drops(radius=radius, max_pickups=max_pickups))
        budget, imminent = self._clamp_tool_budget(12.0, max_s=12.0)
        if imminent:
            return json.dumps({"ok": False, "error": "episode_deadline_imminent"}, ensure_ascii=False)
        return self._run_skill(lambda: self.skills.collect_drops(
            radius=radius, max_pickups=max_pickups, item_types=item_types, max_duration_s=budget,
        ))


    def mc_skill_bind_bed(self, x: int, y: int, z: int) -> str:
        return self._run_skill(lambda: self.skills.bind_bed_spawn(x=x, y=y, z=z))


    def mc_skill_unstick(self) -> str:
        return self._run_skill(lambda: self.skills.unstick())


    def mc_skill_bunker(self) -> str:
        return self._run_skill(lambda: self.skills.dig_safety_bunker())


    def mc_skill_flee(self, distance: int = 14) -> str:
        distance = max(8, min(22, int(distance)))
        budget_s, deadline_imminent = self._clamp_tool_budget(35.0, max_s=35.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "stop_reason": "episode_deadline_imminent",
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，未发起长距离撤退。"},
            }, ensure_ascii=False)
        return self._run_skill(
            lambda: self.skills.flee_hostiles(
                distance=float(distance), geofence=self.geofence, max_duration_s=budget_s,
            )
        )


    def mc_skill_combat(
        self,
        mode: str = "auto",
        target_name: str | None = None,
        max_time_s: float = 20.0,
        pillar_height: int = 3,
    ) -> str:
        """Tactical active combat macro backed by `combat_v1` via `BotSessionCombatAdapter`."""
        return self._run_skill(
            lambda: run_combat_skill(
                self.bot,
                mode=mode,
                target_name=target_name,
                max_time_s=float(max_time_s),
                pillar_height=int(pillar_height),
                geofence=self.geofence,
                episode_deadline_mono=self._episode_deadline_mono,
            )
        )


    def _check_direction_geofence(
        self, direction: str, steps: int, action: str, *, status: Callable[[], dict[str, Any]] | None = None
    ) -> str | None:
        if not self.geofence.enabled:
            return None
        try:
            st = (status or self.bot.status)()
        except (_StairDeadline, _SkillDeadline):
            raise
        except Exception:
            return None
        pos = st.get("pos") or []
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return None
        if direction == "pillar":
            blocked = self.geofence.check_point(float(pos[0]), float(pos[2]), action=action)
            return self._record_geofence_block(blocked) if blocked is not None else None
        blocked = self.geofence.check_ray(float(pos[0]), float(pos[2]), direction, steps, action=action)
        if blocked is None:
            return None
        safe_dirs = [
            cand
            for cand in ("north", "east", "south", "west")
            if self.geofence.check_ray(float(pos[0]), float(pos[2]), cand, steps, action=action) is None
        ]
        return self._record_geofence_block(blocked, safe_directions=safe_dirs)


    def _pick_staircase_direction(self, steps: int, *, vertical: str, skills: MacroSkills) -> str:
        try:
            st = skills.bot.status() or {}
        except _StairDeadline:
            raise
        except Exception:
            return "north"
        pos = st.get("pos") or []
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return "north"
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)
        candidates: list[str] = ["north", "east", "south", "west"]
        try:
            probed = (
                skills._probe_viable_up_stair_dirs(bx, by, bz, require_filler_face=True)
                if vertical == "up"
                else skills._probe_viable_down_stair_dirs(bx, by, bz)
            )
            if probed:
                candidates = probed
        except _StairDeadline:
            raise
        except Exception:
            pass
        if self.geofence.enabled:
            for cand in candidates:
                if self.geofence.check_ray(px, pz, cand, steps, action="mc_skill_staircase") is None:
                    return cand
        return candidates[0] if candidates else "north"


    def mc_skill_staircase(
        self,
        direction: str | None = None,
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool | None = None,
    ) -> str:
        started = time.monotonic()
        budget_s, deadline_imminent = self._clamp_tool_budget(45.0, max_s=45.0, reserve_s=3.0)
        deadline = started + budget_s
        episode_budget = {"deadline": deadline} if self._episode_deadline_mono is not None else {}
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "steps_done": 0,
                "stop_reason": "episode_deadline_imminent",
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过阶梯施工。"},
            }, ensure_ascii=False)

        d_norm = str(direction or "").strip().lower()
        v_norm = str(vertical or "down").strip().lower()
        is_pillar = d_norm == "pillar" or v_norm == "pillar"
        original_steps = max(1, int(steps))
        requested_steps = min(25, original_steps)
        # BUG-037: a 45s call cannot finish long requests (models often ask
        # for 40); cap at 10 so the model gets progress instead of another
        # zero/low-progress time_budget.
        steps = min(requested_steps, 12 if is_pillar else 10)
        if is_pillar:
            d_norm = v_norm = "pillar"
        elif d_norm in ("down", "up"):
            if v_norm not in ("down", "up") or (v_norm == "down" and d_norm == "up"):
                v_norm = d_norm
            d_norm = ""
        resolved_stop = (v_norm == "down") if stop_on_ore is None else bool(stop_on_ore)
        preflight = copy(self.skills)
        preflight.bot = _StairSession(self.bot, 45.0, deadline=deadline)

        def _invoke(dir_key: str) -> str:
            blocked = self._check_direction_geofence(
                dir_key, steps, "mc_skill_staircase", status=preflight.bot.status
            )
            if blocked is not None:
                return blocked
            return self._run_skill(
                lambda: self.skills.dig_staircase(
                    direction=dir_key, steps=steps, vertical=v_norm, stop_on_ore=resolved_stop, **episode_budget,
                )
            )

        try:
            direction = d_norm or self._pick_staircase_direction(steps, vertical=v_norm, skills=preflight)
            raw = _invoke(direction)
        except _StairDeadline:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "direction": d_norm or None,
                "vertical": v_norm,
                "steps_done": 0,
                "stop_reason": "time_budget",
                "error": {"code": "time_budget", "message": "阶梯方向或围栏预检已用完预算。"},
            }, ensure_ascii=False)
        if is_pillar:
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(parsed, dict):
            return raw
        if requested_steps != steps:
            parsed["steps_capped_from"] = original_steps
            parsed["steps_clamped"] = True
        # BUG-037 third mode (r21): first step stuck but viable directions
        # are already known; retry once automatically instead of making the
        # model try the opposite direction.
        if (
            parsed.get("stop_reason") == "movement_not_confirmed"
            and int(parsed.get("steps_done") or 0) == 0
            and int(parsed.get("dug_count") or 0) == 0
        ):
            viable = [d for d in (parsed.get("viable_directions") or []) if isinstance(d, str)]
            alt = next((d for d in viable if d != direction), None)
            if alt and time.monotonic() < deadline:
                try:
                    retry_raw = _invoke(alt)
                except _StairDeadline:
                    return json.dumps(parsed, ensure_ascii=False)
                try:
                    retry_parsed = json.loads(retry_raw)
                except (TypeError, ValueError):
                    return retry_raw
                if isinstance(retry_parsed, dict):
                    retry_parsed["auto_retry_direction"] = alt
                    if requested_steps != steps:
                        retry_parsed["steps_capped_from"] = original_steps
                        retry_parsed["steps_clamped"] = True
                    return json.dumps(retry_parsed, ensure_ascii=False)
        return json.dumps(parsed, ensure_ascii=False)


    def mc_skill_tunnel(self, direction: str = "north", steps: int = 4, stop_on_ore: bool = True) -> str:
        direction = str(direction).strip().lower()
        steps = max(1, min(8, int(steps)))

        def tunnel() -> dict[str, Any]:
            budget_s, deadline_imminent = self._clamp_tool_budget(35.0, max_s=35.0)
            if deadline_imminent:
                return {
                    "ok": False, "requires_replan": True, "steps_done": 0,
                    "stop_reason": "episode_deadline_imminent",
                    "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过隧道施工。"},
                }
            session = None
            skills = self.skills
            if self._episode_deadline_mono is not None:
                session = _SkillBudgetSession(self.bot, budget_s)
                skills = copy(self.skills)
                skills.bot = session
            try:
                blocked = self._check_direction_geofence(direction, steps, "mc_skill_tunnel", status=skills.bot.status)
                if blocked is not None:
                    return json.loads(blocked)
                result = (
                    skills.dig_level_tunnel(direction, steps, stop_on_ore, deadline=session.deadline)
                    if session is not None else skills.dig_level_tunnel(direction, steps, stop_on_ore)
                )
            except (_SkillDeadline, httpx.TimeoutException):
                if session is None:
                    raise
                return {
                    "ok": False, "requires_replan": True, "stop_reason": "time_slice",
                    "stop_confirmed": session.stop_pending(),
                    "error": {"code": "time_slice", "message": "隧道预算已用尽。请先核对位置和背包，再重新规划。"},
                }
            if session is None or (not session.timed_out and result.get("stop_reason") != "time_slice"):
                self._cleanup_inventory_after(result, skills=skills)
                if session is not None and session.timed_out:
                    result["cleanup_deadline_hit"] = True
            if session is not None and session.timed_out:
                session.stop_pending()
            return result

        return self._run_skill(tunnel)


    def _collect_observed_mining_drops(self, target: str, budget_s: float) -> dict[str, Any] | None:
        if budget_s <= 0:
            return None
        from ...skills.mining_dfs import _resolve_target_profile

        with self._state_lock:
            observation = self._nearby_drop_observation
            if observation is None or observation[0] != self._last_dimension:
                return None
            dim, seen_items = observation
            wanted = frozenset(_resolve_target_profile(target, None, dimension=dim)["expected_items"])
            if wanted.isdisjoint(seen_items):
                return None
            remaining_items = seen_items - wanted
            self._nearby_drop_observation = (dim, remaining_items) if remaining_items else None

        # Consume each hint once. The picker always obtains fresh coordinates and verifies the route.
        return self.skills._collect_target_drops(wanted, 5, 4, max_duration_s=budget_s, single_attempt=True)


    def mc_skill_mine_dfs(
        self,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        max_duration_s: float = 32.0,
    ) -> str:
        norm_target = str(target or "").strip().lower().removeprefix("minecraft:")
        eff_target_y = target_y
        clamped_from: int | None = None
        if norm_target in {"diamond", "diamond_ore", "deepslate_diamond_ore"} and target_y is not None:
            ty_int = int(target_y)
            if -30 < ty_int <= 16:
                clamped_from = ty_int
                eff_target_y = -54

        eff_duration_s, deadline_imminent = self._clamp_tool_budget(
            max_duration_s, max_s=90.0, min_s=20.0, reserve_s=4.0
        )
        if deadline_imminent:
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": False,
                    "error": {
                        "code": "episode_deadline_imminent",
                        "message": "本集剩余时间不足 6s，已跳过长耗时挖矿以避免超时越界。",
                    },
                },
                ensure_ascii=False,
            )

        def _invoke() -> dict[str, Any]:
            started = time.monotonic()
            recovered = None
            pickup = None
            duration_s = eff_duration_s
            if str(action or "").strip().lower() == "explore":
                pickup = self._collect_observed_mining_drops(target, min(6.0, duration_s - 6.0))
                if pickup is not None:
                    gained = pickup["gained"]
                    target_gained = sum(gained.values())
                    error = pickup.get("error") or {}
                    interrupted = pickup.get("stop_confirmed") is False or error.get("code") in _PICKUP_INTERRUPTION_CODES
                    if target_gained or interrupted:
                        requested = max(1, min(64, int(goal_count)))
                        result = {
                            "ok": not interrupted, "requires_replan": interrupted,
                            "action": "explore", "target": norm_target or "iron", "goal_count": requested,
                            "goal_met": target_gained >= requested, "dug_count": 0,
                            "gained": gained, "target_gained": target_gained,
                            "stop_reason": "pickup_interrupted" if interrupted else "nearby_drops_collected",
                            "pickup_before_mining": pickup,
                        }
                        if interrupted:
                            result["error"] = error
                        elif pickup.get("pos_after"):
                            result["pos_after"] = pickup["pos_after"]
                        return result
                duration_s = eff_duration_s - (time.monotonic() - started)
                recovered = self.skills.collect_nearby_smelt_output(min(5.0, duration_s - 6.0))
                duration_s = eff_duration_s - (time.monotonic() - started)
            res = self.skills.mine_dfs(
                target=target,
                goal_count=goal_count,
                target_y=eff_target_y,
                action=action,
                geofence=self.geofence,
                max_duration_s=duration_s,
            )
            if recovered and isinstance(res, dict):
                res["furnace_recovered"] = recovered
            if clamped_from is not None and isinstance(res, dict):
                res["clamped_shallow_diamond_y_from"] = clamped_from
            if isinstance(res, dict):
                if pickup is not None:
                    res["pickup_before_mining"] = pickup
                # BUG-029: mine_dfs is the main long-mining path and does not
                # call _ensure_free_slots; enforce retention caps here.
                cleanup_budget = min(8.0, started + eff_duration_s + 3.0 - time.monotonic())
                if cleanup_budget > 0:
                    cleaned, cleanup = self.skills.clean_mining_inventory(cleanup_budget)
                    if cleaned:
                        res["inventory_cleaned"] = cleaned
                    if cleanup is not None:
                        res["inventory_cleanup"] = cleanup
            return res

        return self._run_skill(_invoke)


    def mc_kb_lookup(self, query: str, category: str = "all") -> str:
        res = lookup_kb(query, category=category)
        dim, pos, biome = self._cached_env(fetch_if_missing=False)
        eco = assess_world_target(
            query,
            dimension=dim,
            y=(pos[1] if pos is not None else None),
            biome=biome,
            mode="auto",
        )
        if eco.get("known") is True and (pos is not None or eco.get("available") is False or eco.get("verdict") == "ok"):
            res["ecology_check"] = eco
        return json.dumps(res, ensure_ascii=False)

