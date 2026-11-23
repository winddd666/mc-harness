"""McToolset mixin: actions."""

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


class _ActionsMixin:
    def mc_move_to(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        entity_id: int | None = None,
        range: float = 2.0,
        ignore_hurt: bool = False,
        allow_partial: bool = False,
    ) -> str:
        range = max(0.0, min(16.0, float(range)))
        budgeted = self._episode_deadline_mono is not None
        payload: dict[str, Any] = {"range": range}
        if allow_partial:
            payload["allow_partial"] = True
        geofence_clamped_info: dict[str, Any] | None = None
        if x is not None and z is not None:
            entity_id = None
            orig_x, orig_z = float(x), float(z)
            clamped = self.geofence.clamped_move_target(orig_x, orig_z)
            if clamped is not None:
                x, z = clamped[0], clamped[1]
                geofence_clamped_info = {
                    "geofence_clamped": True,
                    "geofence_original_xz": [round(orig_x, 1), round(orig_z, 1)],
                    "geofence_clamped_xz": [round(float(x), 1), round(float(z), 1)],
                }
            else:
                blocked = self.geofence.check_point(orig_x, orig_z, action="mc_move_to")
                if blocked is not None:
                    return self._record_geofence_block(blocked)
            payload["x"] = x
            payload["z"] = z
            if y is not None:
                yf = float(y)
                last_y = float(self._last_block_pos[1]) if self._last_block_pos and len(self._last_block_pos) >= 3 else None
                bogus_zero_y = (yf == 0.0) and (last_y is None or abs(last_y) >= 16.0)
                if not (-64.0 <= yf <= 320.0):
                    return json.dumps(
                        {
                            "ok": False,
                            "requires_replan": True,
                            "error": {
                                "code": "bad_args",
                                "message": (
                                    f"y={yf:g} 超出主世界/下界合法高度(-64..320)，疑似把 z 填进了 y。"
                                    "请检查坐标后重传 x/z（y 可省略让寻路决定），不要用同一参数重试。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                if not bogus_zero_y:
                    payload["y"] = yf
        elif entity_id is not None:
            payload["entity_id"] = entity_id
        else:
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "需要提供 (x, z) 坐标或 entity_id"}},
                ensure_ascii=False,
            )
        # 水中自动保护：防止溺水扣血打断自己的游上岸动作
        if ignore_hurt or self._is_in_water():
            payload["stop_on"] = ["death"]
        # r12: a stale action lease made /move_to block ~101s before returning
        # busy. /status already exposes the lease owner, so probe it cheaply and
        # fail fast instead of queueing a long path behind an unrelated action.
        origin_pos = None
        try:
            pre = self.bot.call("status", timeout_s=2.0)
            if isinstance(pre, dict):
                origin_pos = pre.get("pos")
                self._update_status_cache(pre)
                busy_action = str(pre.get("busy") or "")
                if busy_action:
                    return json.dumps(
                        {
                            "ok": False,
                            "requires_replan": True,
                            "error": {
                                "code": "action_busy",
                                "action": busy_action,
                                "message": (
                                    f"动作锁仍被 {busy_action} 占用，未提交 move_to，避免阻塞等待。"
                                    "请先处理当前动作（受伤先脱险/等待其完成），1~2 秒后重试坐标；"
                                    "不要用同一参数连续重试。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
        except (ApiError, httpx.HTTPError, RuntimeError, ValueError):
            pass
        eff_move_timeout, move_deadline_imminent = self._clamp_tool_budget(
            25.0, max_s=25.0, min_s=3.0, reserve_s=3.0
        )
        if move_deadline_imminent:
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": False,
                    "error": {
                        "code": "episode_deadline_imminent",
                        "message": "本集剩余时间不足 5s，已跳过 move_to 以避免超时越界。",
                    },
                },
                ensure_ascii=False,
            )
        waypoint = None
        # Budgeted episodes return after one hop so the actor can reassess terrain.
        if (
            budgeted and x is not None and z is not None
            and isinstance(origin_pos, (list, tuple)) and len(origin_pos) >= 3
            and all(isinstance(origin_pos[i], (int, float)) and math.isfinite(origin_pos[i]) for i in (0, 2))
        ):
            dx, dz = float(x) - origin_pos[0], float(z) - origin_pos[2]
            distance = math.hypot(dx, dz)
            if math.isfinite(distance) and distance > 36.0:
                waypoint = [origin_pos[0] + 32.0 * dx / distance, origin_pos[2] + 32.0 * dz / distance]
                blocked = self.geofence.check_point(*waypoint, action="mc_move_to")
                if blocked is not None:
                    return self._record_geofence_block(blocked)
                payload["x"], payload["z"] = waypoint
                payload.pop("y", None)

        failed = self._last_failed_waypoint
        if (
            budgeted and waypoint is None and failed is not None and x is not None and z is not None
            and "y" not in payload and isinstance(origin_pos, (list, tuple))
            and tuple(origin_pos[:3]) == failed.origin and self._last_dimension == failed.dimension
            and time.monotonic() < failed.expires_at
            and math.hypot(float(x) - failed.x, float(z) - failed.z) + range <= failed.range_m
            and math.floor(float(x)) == math.floor(failed.x) and math.floor(float(z)) == math.floor(failed.z)
        ):
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {
                    "code": "move_route_cooldown",
                    "message": "当前站位刚尝试过这个航点，缩小范围仍在同一失败目标内。请先换方向或换站位。",
                },
                **(geofence_clamped_info or {}),
            }, ensure_ascii=False)

        raw = self._call_json("move_to", payload, timeout_s=eff_move_timeout)
        try:
            first = json.loads(raw)
            if first.get("ok") is False and (first.get("error") or {}).get("code") == "busy":
                # /stop waits for the preceding lease to finish. Retry only
                # after the server confirms release; never spin on busy.
                stopped = self.bot.call("stop", {}, timeout_s=5.5)
                if stopped.get("released") is True or (stopped.get("stopped") is True and stopped.get("action") is None):
                    retry_timeout, retry_imminent = self._clamp_tool_budget(
                        15.0, max_s=15.0, min_s=3.0, reserve_s=3.0
                    )
                    if not retry_imminent:
                        raw = self._call_json("move_to", payload, timeout_s=retry_timeout)
        except (ValueError, ApiError, httpx.HTTPError):
            pass
        if waypoint is not None:
            return self._move_waypoint_receipt(
                raw, waypoint, [float(x), float(z)], geofence_clamped_info,
                origin=tuple(origin_pos[:3]), range_m=range,
            )
        if x is not None and z is not None:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
                err_code = str(err_obj.get("code") or "")
                if err_code == "busy":
                    parsed["requires_replan"] = True
                    err_obj["message"] = (
                        f"动作锁仍被 {err_obj.get('action') or '其他动作'} 占用，move_to 未执行；"
                        "请先脱困或等待 1~2 秒后重试，不要连续提交同一路径。"
                    )
                    if geofence_clamped_info is not None:
                        parsed.update(geofence_clamped_info)
                    return json.dumps(parsed, ensure_ascii=False)
                st_mv: dict[str, Any] | None = None
                if not (budgeted and err_code == "move_timeout"):
                    try:
                        st_mv = self.bot.call("status", method="GET", timeout_s=1.0) if budgeted else self.bot.status()
                        self._update_status_cache(st_mv)
                    except Exception:
                        pass
                if err_code == "movement_not_confirmed":
                    pos_now = st_mv.get("pos") if isinstance(st_mv, dict) else None
                    err_msg = str(err_obj.get("message") or "")
                    server_says_in_range = ("已在目标" in err_msg) or ("within range" in err_msg.lower())
                    dist_now: float | None = None
                    if isinstance(pos_now, (list, tuple)) and len(pos_now) >= 3:
                        if y is not None:
                            dist_now = math.dist(
                                [float(x), float(y), float(z)],
                                [float(pos_now[0]), float(pos_now[1]), float(pos_now[2])],
                            )
                        else:
                            dist_now = math.hypot(float(x) - float(pos_now[0]), float(z) - float(pos_now[2]))
                    if server_says_in_range or (dist_now is not None and dist_now <= float(range) + 0.05):
                        if pos_now is None:
                            pos_now = [x, y, z]
                        if dist_now is None:
                            dist_now = 0.0
                        return json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "already_in_range": True,
                                    "pos": list(pos_now[:3]),
                                    "range": range,
                                    "distance": round(dist_now, 2),
                                    "hint": (
                                        "已在目标 range 内，本次移动视为完成。"
                                        "若要挪动站位，请指定至少 3 格外的坐标并设 range=0.5，"
                                        "或直接调 mc_place 不传坐标。"
                                    ),
                                },
                            },
                            ensure_ascii=False,
                        )
                dim_m, cur_p, _ = self._cached_env(fetch_if_missing=not budgeted)
                dist_m = math.hypot(float(x) - cur_p[0], float(z) - cur_p[2]) if cur_p is not None else 0.0
                if not budgeted and dist_m > 36.0 and err_code in {"path_not_found", "no_path", "timeout", "action_timeout", "stalled"}:
                    try:
                        seg_res = self.skills.navigate_segmented(
                            float(x),
                            float(z),
                            y=y,
                            range_m=range,
                            geofence=self.geofence,
                            ignore_hurt=(ignore_hurt or self._is_in_water()),
                            skip_direct=True,
                        )
                        if seg_res.get("ok"):
                            self.update_block_pos(seg_res.get("pos") or (seg_res.get("result") or {}).get("pos"))
                            return json.dumps({"ok": True, "result": seg_res}, ensure_ascii=False)
                    except ApiError:
                        pass
                # If moving onto a remembered POI failed with path_not_found, cool down that POI for 300s
                if err_code in {"path_not_found", "no_path"}:
                    tx_f, tz_f = float(x), float(z)
                    tx_i, tz_i = math.floor(tx_f), math.floor(tz_f)
                    for (edim, ex, ey, ez) in list(self.world_memory.pois.keys()):
                        if edim == dim_m and abs(ex - tx_i) <= 1 and abs(ez - tz_i) <= 1 and (y is None or abs(ey - math.floor(float(y))) <= 2):
                            self.world_memory.mark_unreachable((ex, ey, ez), dim=dim_m, reason=err_code, cooldown_s=300.0)
                    # BUG-058: guide short-range dig_toward and escalate repeated path_not_found to requires_replan
                    total_hits, near_hits = self._record_move_path_failure(tx_f, tz_f)
                    parsed["path_not_found_count"] = total_hits
                    if cur_p is not None and dist_m <= 12.0:
                        parsed["suggested"] = {
                            "mode": "dig_toward",
                            "target": [
                                round(tx_f, 1),
                                round(float(y), 1) if y is not None else None,
                                round(tz_f, 1),
                            ],
                            "distance": round(dist_m, 1),
                        }
                        parsed["hint"] = (
                            f"目标距当前位置仅 {round(dist_m, 1)}m 但寻路不通（可能隔墙/悬崖/门框阻挡），"
                            "请改用 mc_dig / mc_skill_tunnel / mc_skill_staircase 挖通或垫高，不要重复调用 mc_move_to。"
                        )
                    if near_hits >= 2 or total_hits >= 3:
                        parsed["requires_replan"] = True
                        parsed["hint"] = (
                            f"本集已 {total_hits} 次遇到 path_not_found（近邻目标重复 {near_hits} 次），"
                            "该路径不可达。请立即放弃此坐标，改挖通路径（mc_skill_tunnel/mc_dig）或换可达目标。"
                        )
                    raw = json.dumps(parsed, ensure_ascii=False)
        if entity_id is not None:
            try:
                parsed_ent = json.loads(raw)
                err_ent = parsed_ent.get("error") if isinstance(parsed_ent.get("error"), dict) else {}
                msg_ent = str(err_ent.get("message") or "")
                if (isinstance(parsed_ent, dict) and parsed_ent.get("ok") is False
                        and err_ent.get("code") == "movement_not_confirmed"
                        and (("已在目标" in msg_ent) or ("within range" in msg_ent.lower()))):
                    pos_ent = parsed_ent.get("pos_after") or err_ent.get("pos") or [None, None, None]
                    return json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "already_in_range": True,
                                "entity_id": entity_id,
                                "pos": pos_ent,
                                "range": range,
                                "hint": "服务端确认已在目标 range 内，本次移动视为完成。",
                            },
                        },
                        ensure_ascii=False,
                    )
            except (TypeError, ValueError):
                pass
        out_raw = self._annotate_stale_entity(raw, entity_id)
        if geofence_clamped_info is not None:
            try:
                parsed_out = json.loads(out_raw)
                if isinstance(parsed_out, dict):
                    parsed_out.update(geofence_clamped_info)
                    out_raw = json.dumps(parsed_out, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        return out_raw


    def _record_move_path_failure(self, x: float, z: float) -> tuple[int, int]:
        with self._state_lock:
            near_hits = 1 + sum(
                math.hypot(x - previous_x, z - previous_z) <= 8.0
                for previous_x, previous_z in self._path_not_found_targets
            )
            self._path_not_found_targets.append((x, z))
            return len(self._path_not_found_targets), near_hits


    def _move_waypoint_receipt(
        self, raw: str, waypoint: list[float], target: list[float], geofence_info: dict[str, Any] | None,
        *, origin: tuple[float, ...], range_m: float,
    ) -> str:
        dimension = self._last_dimension
        self._last_failed_waypoint = None
        receipt = json.loads(raw)
        result = receipt.get("result") or {}
        error = receipt.get("error") or {}
        pos = result.get("pos") or result.get("pos_after") or error.get("pos")
        if receipt.get("ok") is False:
            receipt["requires_replan"] = True
        if pos is None or receipt.get("ok") is False:
            try:
                status = self.bot.call("status", method="GET", timeout_s=1.0)
                self._update_status_cache(status)
                pos = status.get("pos") or pos
            except (ApiError, httpx.HTTPError):
                pass
        remaining = None
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            remaining = round(math.hypot(target[0] - float(pos[0]), target[1] - float(pos[2])), 1)
        hint = "本次执行途中航点。请根据当前位置和剩余距离继续分段移动或换方向。"
        if receipt.get("ok") is False and error.get("code") in {"path_not_found", "no_path"}:
            if isinstance(pos, (list, tuple)) and tuple(pos[:3]) == origin and self._last_dimension == dimension:
                self._last_failed_waypoint = _FailedMoveWaypoint(
                    dimension, origin, waypoint[0], waypoint[1], range_m, time.monotonic() + 5.0,
                )
            total_hits, near_hits = self._record_move_path_failure(*target)
            receipt["path_not_found_count"] = total_hits
            hint = (
                f"途中航点无可达路径，本集已遇到 {total_hits} 次无路（近邻目标重复 {near_hits} 次）。"
                "请根据当前位置选择其他方向的可达落脚点，再接近最终目标。"
            )
        receipt["result"] = {
            **result,
            "partial_move": True,
            "waypoint_xz": waypoint,
            "target_xz": target,
            "pos": pos,
            "remaining_dist": remaining,
            "hint": hint,
        }
        if geofence_info:
            receipt.update(geofence_info)
        return json.dumps(receipt, ensure_ascii=False)


    def mc_dig(self, x: int, y: int, z: int, collect: bool = True) -> str:
        blocked = self.geofence.check_point(float(x), float(z), action="mc_dig")
        if blocked is not None:
            return self._record_geofence_block(blocked)
        if self._episode_deadline_mono is None:
            return self._dig_block(x, y, z, collect)
        budget_s, deadline_imminent = self._clamp_tool_budget(55.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False,
                "stop_reason": "episode_deadline_imminent",
                "requires_replan": True,
                "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，已跳过挖掘。"},
            }, ensure_ascii=False)
        session = _SkillBudgetSession(self.bot, budget_s)
        try:
            return self._dig_block(x, y, z, collect, session=session)
        except (_SkillDeadline, httpx.TimeoutException):
            stop_confirmed = session.stop_pending()
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "stop_reason": "time_budget",
                "stop_confirmed": stop_confirmed,
                "error": {"code": "time_budget", "message": "挖掘预算已用尽。请核对目标方块和背包后再行动。"},
            }, ensure_ascii=False)


    def _dig_block(
        self, x: int, y: int, z: int, collect: bool, *, session: _SkillBudgetSession | None = None,
    ) -> str:
        bot = session or self.bot
        try:
            bname = self._get_block_name(x, y, z, session=session, default="")
        except _SkillDeadline:
            raise
        except Exception:
            bname = ""
        if bname in {"air", "cave_air", "void_air"}:
            return json.dumps({
                "ok": False,
                "requires_replan": True,
                "error": {
                    "code": "nothing_to_dig",
                    "pos": [x, y, z],
                    "block": bname,
                    "message": f"目标格 {[x, y, z]} 已确认是空气。请用 mc_find_blocks 重新扫描并选择方块。",
                },
            }, ensure_ascii=False)
        req_tier = BLOCK_MIN_PICKAXE_TIER.get(bname, 0)
        if req_tier > 0:
            try:
                inv = bot.inventory()
                counts: dict[str, int] = {}
                for row in inv.get("items") or []:
                    iname = row.get("name")
                    icnt = int(row.get("count") or 0)
                    if iname and icnt > 0:
                        counts[iname] = counts.get(iname, 0) + icnt
                best_pick, best_tier = _best_pickaxe_in_inv(counts)
                held_name = (inv.get("held") or {}).get("name") if isinstance(inv.get("held"), dict) else None
            except _SkillDeadline:
                raise
            except Exception:
                best_pick, best_tier, held_name = None, 0, None
            if best_tier < req_tier:
                return json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "tool_tier_insufficient",
                            "message": (
                                f"禁止用当前工具强挖 {bname}（会直接碎裂无掉落物）！"
                                f"至少需要等级 {req_tier} 的镐，当前背包最高仅 {best_pick or '无镐'}，请先合成对应镐。"
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
            if best_pick and held_name != best_pick:
                try:
                    bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass
        raw_dig = self._call_json("dig", {"pos": [x, y, z], "collect": collect}, session=session)
        try:
            parsed_dig = json.loads(raw_dig)
            if isinstance(parsed_dig, dict) and parsed_dig.get("ok") is True:
                # Broken block is now air: drop both the container cache and its POI entry.
                self.world_memory.invalidate_container(
                    [x, y, z], dim=self._last_dimension, block_name=bname or None
                )
                self.world_memory.remove_poi([x, y, z], dim=self._last_dimension)
                if collect:
                    pickup = self._collect_after_atomic_dig(parsed_dig.get("result"), bname, session)
                    if pickup is not None:
                        parsed_dig["pickup_after_dig"] = pickup
                        if pickup.get("stop_confirmed") is False:
                            parsed_dig["requires_replan"] = True
                        raw_dig = json.dumps(parsed_dig, ensure_ascii=False)
            elif isinstance(parsed_dig, dict) and parsed_dig.get("ok") is False:
                err = parsed_dig.get("error") if isinstance(parsed_dig.get("error"), dict) else {}
                if err.get("code") == "timeout":
                    # P2-21/r15: a timed-out cell was immediately retried by the
                    # model. Avoid it briefly so the next scan picks another.
                    try:
                        self.skills._avoid_temporarily((x, y, z), "dig_timeout", 15.0)
                    except Exception:
                        pass
                    parsed_dig["requires_replan"] = True
                    err["message"] = (
                        f"挖掘 {[x, y, z]} 在预算内未确认；已临时避开该格 15 秒。"
                        "请换相邻格、先 mc_skill_unstick/靠近，或改用 mc_skill_mine_blocks 重新扫描；"
                        "不要立即重试同一坐标。"
                    )
                    raw_dig = json.dumps(parsed_dig, ensure_ascii=False)
        except Exception:
            pass
        return raw_dig


    def _collect_after_atomic_dig(
        self, result: Any, block_name: str, session: _SkillBudgetSession | None,
    ) -> dict[str, Any] | None:
        if not isinstance(result, dict) or not result.get("dug"):
            return None
        dug = result["dug"] if isinstance(result["dug"], str) else block_name
        expected = _expected_drops_for_blocks([dug])
        gained = result.get("gained") or {}
        if not expected or any(_normalize_drop_name(item) in expected and amount > 0 for item, amount in gained.items()):
            return None
        picker = copy(self.skills) if session is not None else self.skills
        if session is not None:
            picker.bot = session
        pickup = picker._collect_target_drops(expected, 3.5, 1, max_duration_s=1.0, single_attempt=True, dig_pickup=True)
        if pickup["gained"]:
            result["gained"] = _merge_counts(dict(gained), pickup["gained"])
        self.update_block_pos(pickup.get("pos_after"))
        return pickup if pickup["attempts"] or pickup.get("error") else None


    def _check_dimension_physics(self, item: str | None) -> str | None:
        if not item:
            return None
        dim, pos, biome = self._cached_env(fetch_if_missing=False)
        probe = assess_world_target(item, dimension=dim, y=(pos[1] if pos is not None else None), biome=biome, mode="place")
        if probe.get("known") is True and pos is None:
            dim, pos, biome = self._cached_env(fetch_if_missing=True)
            probe = assess_world_target(item, dimension=dim, y=(pos[1] if pos is not None else None), biome=biome, mode="place")
        if probe.get("available") is False:
            return json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_dimension_physics",
                        "message": probe["hint"],
                        "ecology_check": probe,
                    },
                },
                ensure_ascii=False,
            )
        return None


    def mc_place(
        self,
        item: str,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        physics_block = self._check_dimension_physics(item)
        if physics_block is not None:
            return physics_block
        payload: dict[str, Any] = {"item": item}
        if x is not None and y is not None and z is not None:
            blocked = self.geofence.check_point(float(x), float(z), action="mc_place")
            if blocked is not None:
                return self._record_geofence_block(blocked)
            payload["pos"] = [x, y, z]
        elif any(v is not None for v in (x, y, z)):
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "放置坐标需同时提供 x, y, z，或全部省略自动选落点"}},
                ensure_ascii=False,
            )
        raw_place = self._call_json("place", payload)
        try:
            parsed_pl = json.loads(raw_place)
            if isinstance(parsed_pl, dict) and parsed_pl.get("ok") is True:
                res_pl = parsed_pl.get("result") if isinstance(parsed_pl.get("result"), dict) else {}
                p_xyz = res_pl.get("pos") or ([x, y, z] if (x is not None and y is not None and z is not None) else None)
                if isinstance(p_xyz, (list, tuple)) and len(p_xyz) >= 3:
                    pos = [int(p_xyz[0]), int(p_xyz[1]), int(p_xyz[2])]
                    self.world_memory.record_poi(item, pos, dim=self._last_dimension)
                    self.skills.remember_placed_storage(item, pos, self._last_dimension)
        except Exception:
            pass
        return raw_place


    def mc_craft(self, item: str, count: int = 1) -> str:
        cnt = max(1, min(64, int(count)))
        resolved = resolve_item_alias(item)
        cached_inv = getattr(self.bot, "_last_inventory", None)
        if isinstance(cached_inv, dict) and _compute_empty_slots(cached_inv) < 2:
            inv_now, _ = self.skills._ensure_free_slots(min_free=2)
            if _compute_empty_slots(inv_now) == 0 and not _can_fit_item_in_inventory(inv_now, item, cnt):
                return json.dumps(
                    {
                        "ok": False,
                        "requires_replan": True,
                        "error": {
                            "code": "inventory_full",
                            "item": item,
                            "count": cnt,
                            "empty_slots": 0,
                            "message": (
                                f"背包 36 格已满(empty_slots=0)且无法自动腾出空槽容纳 {item} x{cnt}；"
                                "直接调 craft 会触发 30s 超时卡死，请先消耗、放置方块或丢弃不需要的物品后再合成。"
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
        raw = self._call_json("craft", {"item": item, "count": cnt})
        if resolved in TABLE_RECIPES:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and not parsed.get("ok"):
                err_obj = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
                err_code = str(err_obj.get("code") or "")
                if err_code in {"need_crafting_table", "no_window", "path_not_found", "out_of_reach"}:
                    try:
                        counts_now = _inventory_counts(self.bot)
                    except ApiError:
                        counts_now = {}
                    if counts_now.get("crafting_table", 0) >= 1:
                        tbl_res = self.skills.ensure_crafting_table(
                            force_recarve=(err_code in {"no_window", "path_not_found", "out_of_reach"}),
                            target_item=resolved,
                        )
                        if tbl_res.get("ok"):
                            return self._call_json("craft", {"item": item, "count": cnt})
                    elif err_code in {"no_window", "path_not_found"} and isinstance(err_obj.get("table"), list) and len(err_obj["table"]) >= 3:
                        tpos = [int(err_obj["table"][0]), int(err_obj["table"][1]), int(err_obj["table"][2])]
                        self.skills._record_utility_in_ledger("crafting_table", tpos)
                        if self.skills._navigate_to_distant_table(tpos):
                            return self._call_json("craft", {"item": item, "count": cnt})
        return raw


    def mc_equip(self, item: str, slot: str = "hand") -> str:
        norm_item = str(item or "").removeprefix("minecraft:").strip()
        norm_slot = "torso" if slot == "chest" else str(slot or "hand")
        raw = self._call_json("equip", {"item": norm_item, "slot": norm_slot})
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            err = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            if err.get("code") == "missing_item":
                try:
                    inv = self.bot.inventory()
                    offhand = inv.get("offhand") if isinstance(inv, dict) else None
                    off_name = str(offhand.get("name") or "").removeprefix("minecraft:") if isinstance(offhand, dict) else ""
                    if off_name and off_name == norm_item:
                        if norm_slot == "off-hand":
                            return json.dumps(
                                {"ok": True, "already_equipped": True, "item": norm_item, "slot": "off-hand"},
                                ensure_ascii=False,
                            )
                        parsed["hint"] = (
                            f"{norm_item} 当前已装备在副手(off-hand slot 45)，服务端 /equip 仅查主背包槽位；"
                            "请先将盾牌/方块等其他物品装备到 off-hand 腾出该物品，或改用背包内其他工具。"
                        )
                        return json.dumps(parsed, ensure_ascii=False)
                except Exception:
                    pass
        return raw


    def mc_interact(
        self,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        entity_id: int | None = None,
        item: str | None = None,
    ) -> str:
        item_name = str(item or "").removeprefix("minecraft:")
        if entity_id is None and item_name == "fishing_rod":
            return json.dumps(
                {
                    "ok": False,
                    "requires_replan": True,
                    "error": {
                        "code": "use_item_api",
                        "message": (
                            "钓鱼需要从岸边向开阔水面抛竿。请选择水面坐标，"
                            "调用 mc_use_item(item='fishing_rod', x=..., y=..., z=...) 抛竿并等待收竿。"
                        ),
                    },
                },
                ensure_ascii=False,
            )
        physics_block = self._check_dimension_physics(item)
        if physics_block is not None:
            return physics_block
        payload: dict[str, Any] = {}
        if entity_id is not None:
            payload["entity_id"] = entity_id
        elif x is not None and y is not None and z is not None:
            payload["pos"] = [x, y, z]
        else:
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "interact 必须提供 (x, y, z) 方块坐标或 entity_id"}},
                ensure_ascii=False,
            )
        if item:
            payload["item"] = item
        is_bucket = item in {"bucket", "water_bucket", "lava_bucket"}
        if is_bucket and x is not None and y is not None and z is not None:
            try:
                blk = self.bot.call("get_block", {"pos": [x, y, z]}, timeout_s=2.0)
                if isinstance(blk, dict):
                    bname = str(blk.get("name") or "").removeprefix("minecraft:")
                    level = (blk.get("properties") or {}).get("level")
                    if bname in {"lava", "water", "flowing_lava", "flowing_water"} and level is not None:
                        if int(level) > 0:
                            return json.dumps(
                                {
                                    "ok": False,
                                    "requires_replan": True,
                                    "error": {
                                        "code": "interact_flowing_fluid",
                                        "target": [x, y, z],
                                        "block": bname,
                                        "level": int(level),
                                        "message": (
                                            f"目标 {bname} 是流动流体(level={int(level)})，桶只能舀源头(level=0)。"
                                            "请沿流体上游/中心寻找源头，或先用方块封堵流向；不要对同一格重复右键。"
                                        ),
                                    },
                                },
                                ensure_ascii=False,
                            )
            except (ApiError, httpx.HTTPError, TypeError, ValueError):
                pass
            try:
                st = getattr(self, "_last_status", None)
                if not isinstance(st, dict) or not st.get("pos"):
                    st = self.bot.status()
                pos = st.get("pos") if isinstance(st, dict) else None
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    bot_y = float(pos[1])
                    if abs(float(y) - bot_y) >= 5.0:
                        return json.dumps(
                            {
                                "ok": False,
                                "requires_replan": True,
                                "error": {
                                    "code": "out_of_reach_vertical",
                                    "message": (
                                        f"目标方块 y={y} 与当前高度 y={bot_y:.1f} 相差 {abs(float(y) - bot_y):.1f} 格，"
                                        "直接右键桶会寻路卡死；请先用 mc_skill_staircase 移动到接近目标高度后再调 mc_interact。"
                                    ),
                                },
                            },
                            ensure_ascii=False,
                        )
            except Exception:
                pass
        raw = self._call_json("interact", payload)
        is_bottle = "pos" in payload and item_name == "glass_bottle"
        if not (is_bucket or is_bottle):
            return raw
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            return raw
        res_obj = parsed.get("result")
        if not isinstance(res_obj, dict):
            return raw

        used = res_obj.get("used")
        gained = res_obj.get("gained")
        if is_bucket and res_obj.get("changed") is False and not used and not gained:
            message = "交互未产生方块变化(changed=false)：目标可能是流动流体而非静止源头(level=0)，或视线/距离受阻，桶内状态未变化！请靠近静止源头(level=0) 3 格内重试。"
        elif is_bottle and str(res_obj.get("block") or "").removeprefix("minecraft:") in {"water", "flowing_water"}:
            if (
                isinstance(used, dict) and used.get("glass_bottle")
                and isinstance(gained, dict) and (gained.get("potion") or gained.get("water_bottle"))
            ):
                return raw
            message = (
                "交互回执未确认玻璃瓶装水。请用 mc_inventory 确认水药瓶是否入包。"
                "若没有，换一个 3 格内可达的静止水源（level=0）。"
            )
        else:
            return raw

        parsed["ok"] = False
        parsed["requires_replan"] = True
        parsed["error"] = {"code": "interact_no_effect", "message": message}
        return json.dumps(parsed, ensure_ascii=False)


    def mc_use_item(
        self,
        item: str,
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        ms: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"item": item}
        if entity_id is not None:
            payload["target"] = {"entity_id": entity_id}
        elif x is not None and y is not None and z is not None:
            payload["target"] = {"pos": [x, y, z]}
        if ms is not None:
            payload["ms"] = max(50, min(10000, int(ms)))
        raw = self._call_json("use_item", payload)
        if resolve_item_alias(item) != "ender_eye":
            return raw

        receipt = json.loads(raw)
        result = receipt.get("result")
        if receipt.get("ok") is not True or not isinstance(result, dict) or result.get("thrown") is not True:
            return raw

        vector = result.get("eye_vector")
        direction = None
        ux = uz = 0.0
        has_valid_vec = False
        if isinstance(vector, list) and len(vector) == 2 and all(
            type(value) in (int, float) and -math.inf < value < math.inf for value in vector
        ):
            dx, dz = float(vector[0]), float(vector[1])
            norm = math.hypot(dx, dz)
            if norm > 1e-6:
                ux, uz = dx / norm, dz / norm
                has_valid_vec = True
                sector = math.floor((math.atan2(uz, ux) + math.pi / 8) / (math.pi / 4))
                direction = SURFACE_SCOUT_DIRECTIONS[sector % len(SURFACE_SCOUT_DIRECTIONS)]
        result["scout_direction"] = direction
        # 0-LLM two-eye stronghold triangulation
        origin_x = origin_y = origin_z = None
        try:
            st = self.bot.status()
            pos = st.get("pos") if isinstance(st, dict) else None
            if isinstance(pos, list) and len(pos) >= 3:
                origin_x, origin_y, origin_z = float(pos[0]), float(pos[1]), float(pos[2])
        except Exception:
            pass
        landed = result.get("landed_at")
        if origin_x is None and isinstance(landed, list) and len(landed) >= 3:
            origin_x, origin_y, origin_z = float(landed[0]), float(landed[1]), float(landed[2])

        eye_went_down = (
            origin_y is not None
            and isinstance(landed, list)
            and len(landed) >= 2
            and float(landed[1]) <= origin_y - 2.0
        )
        if eye_went_down:
            result["eye_went_down"] = True

        triangulated_xz: list[int] | None = None
        triangulated_dist: float | None = None
        triangulated_dir: str | None = None
        if has_valid_vec and origin_x is not None and origin_z is not None:
            throws = getattr(self, "_ender_eye_throws", None)
            if not isinstance(throws, list):
                throws = []
                self._ender_eye_throws = throws
            # Solve best intersection against prior throws with sufficient baseline (>=25m) and non-collinear angle
            best_det = 0.0
            for px, pz, pvx, pvz in throws:
                baseline = math.hypot(origin_x - px, origin_z - pz)
                if baseline < 25.0:
                    continue
                det = pvz * ux - pvx * uz
                if abs(det) < 0.06 or abs(det) <= best_det:
                    continue
                t1 = ((origin_z - pz) * ux - (origin_x - px) * uz) / det
                t2 = ((origin_z - pz) * pvx - (origin_x - px) * pvz) / det
                if t1 <= 0.0 or t2 <= 0.0:
                    continue
                tx = origin_x + t2 * ux
                tz = origin_z + t2 * uz
                dist = math.hypot(tx - origin_x, tz - origin_z)
                if dist > 3000.0:
                    continue
                best_det = abs(det)
                triangulated_xz = [round(tx), round(tz)]
                triangulated_dist = round(dist, 1)
                t_sec = math.floor((math.atan2(tz - origin_z, tx - origin_x) + math.pi / 8) / (math.pi / 4))
                triangulated_dir = SURFACE_SCOUT_DIRECTIONS[t_sec % len(SURFACE_SCOUT_DIRECTIONS)]
            # Keep bounded history of recent throws
            if not throws or math.hypot(origin_x - throws[-1][0], origin_z - throws[-1][1]) >= 8.0:
                throws.append((origin_x, origin_z, ux, uz))
                if len(throws) > 12:
                    del throws[:-12]

        if eye_went_down:
            result["scout_hint"] = (
                "末影之眼已向下钻地！要塞就在脚下附近，立即停止掷眼并连续调用 "
                "mc_skill_staircase(vertical='down', steps=16) 下挖至 Y=10~-10 寻找 stone_bricks / end_portal_frame！"
            )
        elif triangulated_xz is not None and triangulated_dist is not None:
            self._triangulated_stronghold_xz = list(triangulated_xz)
            result["triangulated_stronghold_xz"] = triangulated_xz
            result["triangulated_distance"] = triangulated_dist
            result["triangulated_direction"] = triangulated_dir
            result["scout_hint"] = (
                f"【两眼三角定位成功！】要塞估算坐标为 {triangulated_xz}（距当前约 {triangulated_dist}m，方向 '{triangulated_dir}'）。"
                f"请直接朝 '{triangulated_dir}' 前往该坐标上方（无需中途再掷眼！），抵达后垂直阶梯下挖至 Y=10~-10 寻找 stone_bricks / end_portal_frame！"
            )
        elif direction and origin_x is not None and origin_z is not None:
            perp_x, perp_z = -uz, ux
            p_sec = math.floor((math.atan2(perp_z, perp_x) + math.pi / 8) / (math.pi / 4))
            perp_dir = SURFACE_SCOUT_DIRECTIONS[p_sec % len(SURFACE_SCOUT_DIRECTIONS)]
            suggested_xz = [round(origin_x + perp_x * 75.0), round(origin_z + perp_z * 75.0)]
            result["suggested_second_throw_direction"] = perp_dir
            result["suggested_second_throw_xz"] = suggested_xz
            result["scout_hint"] = (
                f"已记录第 1 颗末影之眼射线（主方向 '{direction}'）。推荐先沿垂直方向 '{perp_dir}' 侧移 60~80 格（约 {suggested_xz}）"
                f"再掷第 2 颗眼，系统将自动三角定位算出要塞精确坐标；也可直接调用 mc_skill_scout_surface(direction='{direction}', track_ender_eye=True)。"
            )
        else:
            result["scout_hint"] = (
                f"可直接调用 mc_skill_scout_surface(direction='{direction}', track_ender_eye=True)。位移后请重新测向。"
                if direction else "本次掷眼没有获得有效的水平向量。请在开阔处重新测向后再决定方向。"
            )
        return json.dumps(receipt, ensure_ascii=False)


    @staticmethod
    def _attack_hit_confirmed(result: dict[str, Any]) -> bool | None:
        """Return None when the receipt only reports swings or ambiguous feedback."""
        if result.get("killed") is True:
            return True
        for key in ("connected_hits", "hits_connected"):
            count = result.get(key)
            if type(count) in (int, float) and count >= 0 and math.isfinite(count):
                return count > 0
        before, after = result.get("target_health_before"), result.get("target_health_after")
        if all(type(value) in (int, float) and value >= 0 and math.isfinite(value) for value in (before, after)):
            return after < before
        delta = result.get("target_hp_delta")
        if type(delta) in (int, float) and math.isfinite(delta):
            # A delta alone does not establish which direction represents damage.
            return False if delta == 0 else None
        swings = result.get("hits")
        return False if type(swings) in (int, float) and swings == 0 else None


    def mc_attack(
        self,
        entity_id: int,
        max_hits: int | None = None,
        interval_ms: int | None = None,
        timeout_ms: int = 20000,
    ) -> str:
        with self._state_lock:
            seen = self._entity_observations.get(entity_id)
            seen_at = self._entity_observed_mono
            observed_context = seen_at is not None or self._status_hostile_id == entity_id
            current_dimension = self._entity_observed_dimension == self._last_dimension
        distance = seen.get("dist") if seen is not None else None
        if observed_context and (
            seen_at is None or time.monotonic() - seen_at > 3.0 or not current_dimension
            or not isinstance(distance, (int, float)) or distance > 32.0
        ):
            try:
                live = self.bot.call("get_entities", {"radius": 64}, timeout_s=2.0).get("entities") or []
                target = next((ent for ent in live if isinstance(ent, dict) and ent.get("id") == entity_id), None)
                if target is None or (isinstance(target.get("dist"), (int, float)) and target["dist"] > 32.0):
                    message = (
                        "目标仍超出近战追击范围。请确认安全路线并靠近，或换附近目标。"
                        if target else f"实体 {entity_id} 已不可见。请重新调 mc_get_entities 获取最新 id 和位置。"
                    )
                    return json.dumps({
                        "ok": False, "requires_replan": True,
                        "error": {"code": "target_out_of_reach" if target else "target_not_visible", "entity_id": entity_id,
                                  "dist": target.get("dist") if target else None,
                                  "message": message},
                    }, ensure_ascii=False)
            except (ApiError, httpx.HTTPError):
                pass
        try:
            inv = self.bot.inventory()
            counts: dict[str, int] = {}
            for row in inv.get("items") or []:
                iname = row.get("name")
                icnt = int(row.get("count") or 0)
                if iname and icnt > 0:
                    counts[iname] = counts.get(iname, 0) + icnt
            best_w = best_melee_weapon_in_inv(counts)
            held_name = (inv.get("held") or {}).get("name") if isinstance(inv.get("held"), dict) else None
            if best_w and held_name != best_w:
                self.bot.call("equip", {"item": best_w, "slot": "hand"})
        except (ApiError, KeyError, TypeError):
            pass
        timeout_ms = max(1000, min(60000, int(timeout_ms)))
        payload = {"entity_id": entity_id, "timeout": timeout_ms, "feedback": True}
        if max_hits is not None:
            payload["max_hits"] = max(1, min(100, int(max_hits)))
        if interval_ms is not None:
            payload["interval_ms"] = max(50, min(5000, int(interval_ms)))
        raw = self._annotate_stale_entity(self._call_json("attack", payload), entity_id)
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(parsed, dict) or parsed.get("ok") is not True or not isinstance(parsed.get("result"), dict):
            return raw
        res = parsed["result"]
        confirmed = self._attack_hit_confirmed(res)
        parsed["hit_confirmed"] = confirmed
        if confirmed is not True:
            # The actor's repeat guard treats result.hits as confirmed material progress.
            if "hits" in res:
                res["reported_hits"] = res.pop("hits")
            message = (
                "本次攻击未取得有效命中或扣血。请重新观测目标位置，再选安全的进攻路线或更换动作。"
                if confirmed is False else
                "服务端回执无法确认实际命中或扣血。请先用 mc_get_entities 复核目标，再决定下一步。"
            )
            parsed.update(ok=False, requires_replan=True, error={
                "code": "attack_zero_hits" if confirmed is False else "attack_hit_unconfirmed",
                "entity_id": entity_id,
                "ms": res.get("ms"),
                "message": message,
            })
        return json.dumps(parsed, ensure_ascii=False)


    def mc_shoot(
        self,
        weapon: str = "bow",
        entity_id: int | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        charge_ms: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"weapon": weapon}
        if entity_id is not None:
            payload["entity_id"] = entity_id
        elif x is not None and y is not None and z is not None:
            payload["pos"] = [x, y, z]
        else:
            return json.dumps(
                {"ok": False, "error": {"code": "bad_args", "message": "shoot 需要提供 entity_id 或 (x, y, z)"}},
                ensure_ascii=False,
            )
        if charge_ms is not None:
            payload["charge_ms"] = max(200, min(5000, int(charge_ms)))
        return self._call_json("shoot", payload)


    def mc_window_view(
        self,
        block: str | None = None,
        entity_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        target = _build_window_target(block, entity_id, x, y, z)
        if isinstance(target, str):
            return target
        if entity_id is not None:
            # BUG-027: the backend's goNear can silently fail, then activate
            # from 12m away and report a misleading no_window. Pre-check here.
            try:
                ent = self.skills._observe_window_entity(entity_id)
                if ent is None:
                    return json.dumps(
                        {
                            "ok": False, "requires_replan": True,
                            "error": {
                                "code": "target_not_visible", "entity_id": entity_id,
                                "message": (
                                    f"实体 {entity_id} 在 32m 内不可见；请重新 mc_get_entities 获取最新 id，"
                                    "或先 mc_move_to 到目标附近再开窗。不要重复使用旧 id。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                dist = ent.get("dist")
                if isinstance(dist, (int, float)) and float(dist) > 4.0:
                    epos = ent.get("pos")
                    move_hint = ""
                    if isinstance(epos, (list, tuple)) and len(epos) >= 3:
                        move_hint = f"先调 mc_move_to(x={epos[0]}, z={epos[2]}, range=2) 靠近，再 mc_window_view。"
                    return json.dumps(
                        {
                            "ok": False, "requires_replan": True,
                            "error": {
                                "code": "out_of_reach", "entity_id": entity_id,
                                "dist": round(float(dist), 1), "pos": epos,
                                "message": (
                                    f"目标距离 {float(dist):.1f}m，超过开窗交互范围(<=4m)。"
                                    f"{move_hint}不要在 12m 外重复开窗，后端不会先走近。"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                if self.skills._is_merchant_entity(ent):
                    def view_merchant() -> dict[str, Any]:
                        nonlocal ent
                        budget, imminent = self._clamp_tool_budget(14.0, max_s=14.0)
                        if imminent:
                            raise ApiError("episode_deadline_imminent", "本集剩余时间不足，交易窗口未打开。")
                        observed, ent = ent, None
                        return self.skills.merchant_window_view(entity_id, observed, max_duration_s=budget)

                    return self._run_skill(view_merchant)
            except (ApiError, httpx.HTTPError, TypeError, ValueError):
                pass
        raw = self._call_json("window/view", {"target": target})
        self._sync_window_payload(
            raw,
            block=block,
            pos=[x, y, z] if (x is not None and y is not None and z is not None) else None,
            entity_id=entity_id,
        )
        return raw


    def mc_window_act(
        self,
        ops: list[dict[str, Any]],
        block: str | None = None,
        entity_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        target = _build_window_target(block, entity_id, x, y, z)
        if isinstance(target, str):
            return target
        missing_source = [
            isinstance(op, dict) and op.get("op") == "take" and op.get("from") is None
            for op in ops
        ] if isinstance(ops, list) else []
        furnace = target.get("block") in ("furnace", "minecraft:furnace")
        if any(missing_source) and "pos" in target:
            try:
                observed = self.bot.call("get_block", {"pos": target["pos"]}, timeout_s=1.0, allow_reauth=False)
                name = str(observed.get("name") or observed.get("block") or "").removeprefix("minecraft:")
                furnace = name == "furnace"
            except (ApiError, httpx.HTTPError):
                pass
        if furnace and any(missing_source):
            ops = [
                {**op, "from": "output"} if missing else op
                for op, missing in zip(ops, missing_source)
            ]
        raw = self._call_json("window/act", {"target": target, "ops": ops})
        if "pos" in target:
            outcome = json.loads(raw)
            receipt = outcome.get("result") if outcome.get("ok") else outcome.get("error")
            if isinstance(receipt, dict):
                self.skills.record_window_furnace_take(target["pos"], receipt)
        self._sync_window_payload(
            raw,
            block=block,
            pos=[x, y, z] if (x is not None and y is not None and z is not None) else None,
            entity_id=entity_id,
        )
        return raw


    def _sync_window_payload(
        self,
        raw: str,
        *,
        block: str | None = None,
        pos: list[int] | None = None,
        entity_id: int | None = None,
    ) -> None:
        """Sync WorldMemory.containers from window payloads on BOTH ok and error branches."""
        if block == "furnace" or entity_id is not None:
            return
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(parsed, dict):
            return
        res = parsed.get("result") if isinstance(parsed.get("result"), dict) else None
        err = parsed.get("error") if isinstance(parsed.get("error"), dict) else None
        payload: dict[str, Any] | None = None
        if res is not None:
            inner = res.get("window")
            if isinstance(inner, dict):
                payload = inner
                if not payload.get("pos") and isinstance(res.get("pos"), (list, tuple)):
                    payload = {**payload, "pos": res["pos"]}
            elif isinstance(inner, str):
                payload = res
        elif err is not None and isinstance(err.get("window"), dict):
            payload = err["window"]
        if payload is None:
            if err is not None and pos is not None and err.get("code") in {"no_window", "not_found", "window_closed"}:
                try:
                    live = str(self._get_block_name(*pos) or "").removeprefix("minecraft:")
                except Exception:
                    live = ""
                if live in {"air", "cave_air", "void_air", ""}:
                    self.world_memory.invalidate_container(pos, dim=self._last_dimension, block_name=block)
            return
        fb_pos = pos
        if fb_pos is None:
            target_info = payload.get("target") if isinstance(payload.get("target"), dict) else {}
            raw_pos = payload.get("pos") or target_info.get("pos")
            if isinstance(raw_pos, (list, tuple)) and len(raw_pos) >= 3:
                fb_pos = [int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2])]
            else:
                fb_pos = self.skills._resolve_nearby_container_pos(block)
        try:
            self.world_memory.update_container_from_window(
                payload,
                fallback_pos=fb_pos,
                fallback_block=block,
                dim=self._last_dimension,
            )
        except Exception:
            pass
        if fb_pos is not None:
            try:
                live = str(self._get_block_name(*fb_pos) or "").removeprefix("minecraft:")
            except Exception:
                live = ""
            if live in {"air", "cave_air", "void_air"}:
                self.world_memory.invalidate_container(fb_pos, dim=self._last_dimension, block_name=block)


    def mc_skill_container(
        self,
        items: list[dict[str, Any]],
        action: str | None = None,
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return self._run_skill(
            lambda: self.skills.container_batch(
                items,
                action=action,
                block=block,
                x=x,
                y=y,
                z=z,
                dim=self._last_dimension,
            )
        )


    def mc_skill_cast_obsidian(self, x: int, y: int, z: int) -> str:
        budget, deadline_imminent = self._clamp_tool_budget(15.0, max_s=15.0)
        if deadline_imminent:
            return json.dumps({
                "ok": False, "requires_replan": True,
                "error": "episode_deadline_imminent", "message": "本集剩余时间不足，浇铸尚未开始。",
            }, ensure_ascii=False)
        return self._run_skill(lambda: self.skills.cast_obsidian(x, y, z, max_duration_s=budget))


    def mc_skill_build_portal(self, enter: bool = True, wait_s: float = 8.0) -> str:
        def build() -> dict[str, Any]:
            budget, deadline_imminent = self._clamp_tool_budget(55.0)
            if deadline_imminent:
                return {
                    "ok": False, "requires_replan": True,
                    "error": {"code": "episode_deadline_imminent", "message": "本集剩余时间不足，建门未开始。"},
                }
            return self.skills.build_nether_portal(enter=bool(enter), wait_s=float(wait_s), max_duration_s=budget)

        return self._run_skill(build)

