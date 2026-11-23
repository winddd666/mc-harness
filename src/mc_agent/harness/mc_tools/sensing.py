"""McToolset mixin: sensing."""

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


class _SensingMixin:
    def rebind_runtime_hooks(
        self,
        *,
        geofence: VoidGeofence | None = None,
        wait_for_cooldown: bool | None = None,
        stop_check: Callable[[], bool] | None = None,
        on_cooldown_wait: Callable[[str, float], None] | None = None,
        episode_deadline_mono: float | None = None,
    ) -> None:
        """Rebind per-episode timeout/cooldown callbacks on a persistent shared self."""
        if geofence is not None:
            self.geofence = geofence
            self.skills.geofence = geofence
        if wait_for_cooldown is not None:
            self._wait_for_cooldown = bool(wait_for_cooldown)
        self._stop_check = stop_check
        self._on_cooldown_wait = on_cooldown_wait
        self._episode_deadline_mono = episode_deadline_mono
        self._geofence_blocked_count = 0
        self._path_not_found_targets.clear()
        self._last_failed_waypoint = None
        self._empty_max_scans.clear()


    def _annotate_scan_ceiling(
        self,
        parsed: dict[str, Any],
        res_obj: dict[str, Any] | None,
        *,
        tool_name: str,
        requested_radius: int,
        effective_radius: int,
        query_types: list[str] | None,
        empty: bool,
        cur_pos: tuple[float, float, float] | list[float] | None,
    ) -> bool:
        """Surface the 64m scan ceiling and break futile radius-doubling loops (long-run Bottleneck #1)."""
        changed = False
        if requested_radius > effective_radius and isinstance(res_obj, dict):
            res_obj["radius_clamped_from"] = requested_radius
            res_obj["radius_effective"] = effective_radius
            res_obj["hint"] = (
                f"{tool_name} 最大扫描半径硬上限为 {effective_radius}m（已将 radius={requested_radius} 钳制为 {effective_radius}m）。"
                "原地继续放大 radius 不会扫描到更远区块；若当前 64m 内无目标，请先移动 >=40 格换区，或直接输出阻断结论切换目标。"
            )
            changed = True
        if not empty or (effective_radius < 48 and requested_radius <= 64):
            return changed
        norm_key = (
            tool_name,
            tuple(sorted(str(t).strip().lower().removeprefix("minecraft:") for t in (query_types or []))),
        )
        pos_xz = (float(cur_pos[0]), float(cur_pos[2])) if cur_pos is not None and len(cur_pos) >= 3 else None
        with self._state_lock:
            prev_count, prev_xz = self._empty_max_scans.get(norm_key, (0, None))
            moved_far = (
                pos_xz is not None
                and prev_xz is not None
                and math.hypot(pos_xz[0] - prev_xz[0], pos_xz[1] - prev_xz[1]) >= 24.0
            )
            count = 1 if moved_far else prev_count + 1
            self._empty_max_scans[norm_key] = (count, pos_xz if pos_xz is not None else prev_xz)
        if count >= 2:
            parsed["requires_replan"] = True
            parsed["scan_exhausted"] = True
            parsed["empty_max_scan_count"] = count
            msg = (
                f"你在当前位置附近已连续 {count} 次执行大半径({effective_radius}m)扫描且结果为 0。"
                "服务端仅加载玩家周围约 64m 区块，严禁继续放大 radius 或原地重复扫描！"
                "请立即移动 >=40 格换区，或直接结束本集汇报目标阻断。"
            )
            parsed["hint"] = msg
            if isinstance(res_obj, dict):
                res_obj["hint"] = msg
            changed = True
        return changed


    def _clamp_tool_budget(
        self,
        requested_s: float,
        *,
        max_s: float = 55.0,
        min_s: float = 5.0,
        reserve_s: float = 4.0,
    ) -> tuple[float, bool]:
        """Clamp long-running skill budgets to [min_s, max_s] and remaining episode deadline."""
        eff = max(float(min_s), min(float(max_s), float(requested_s)))
        if self._episode_deadline_mono is not None:
            rem = self._episode_deadline_mono - time.monotonic() - float(reserve_s)
            if rem <= 2.0:
                return (0.0, True)
            eff = min(eff, max(2.0, rem))
        return (eff, False)


    def _record_geofence_block(
        self,
        blocked: dict[str, Any],
        *,
        safe_directions: list[str] | None = None,
    ) -> str:
        """BUG-048: track per-episode geofence blocks and escalate to requires_replan after 3 hits."""
        with self._state_lock:
            self._geofence_blocked_count += 1
            count = self._geofence_blocked_count
        out = dict(blocked)
        out["geofence_blocked_count"] = count
        if safe_directions is not None:
            out["suggested_safe_directions"] = list(safe_directions)
        if count >= 3:
            out["requires_replan"] = True
            out["hint"] = (
                f"本集已连续 {count} 次触发安全围栏拦截，请放弃当前越界方向/目标，"
                "先调用 mc_status 确认当前位置并向安全中心方向换新目标，不要继续微调越界坐标。"
            )
        return json.dumps(out, ensure_ascii=False)


    def _set_last_event_seq(self, seq: int) -> None:
        with self._state_lock:
            if seq > self._last_event_seq:
                self._last_event_seq = seq
            try:
                setattr(self.bot, "_last_event_seq", self._last_event_seq)
            except Exception:
                pass


    def _reconnect_once(self, *, quarantined: bool = False) -> None:
        with self._reconnect_lock:
            if quarantined and hasattr(self.bot, "recover_quarantine"):
                try:
                    self.bot.recover_quarantine(timeout_s=30.0)
                    return
                except DeathLoopCooldownError:
                    if not (self._wait_for_cooldown and hasattr(self.bot, "ensure_connected_and_ready")):
                        raise
            if self._wait_for_cooldown and hasattr(self.bot, "ensure_connected_and_ready"):
                self.bot.ensure_connected_and_ready(
                    timeout_s=45.0,
                    poll_interval_s=1.0,
                    wait_for_cooldown=True,
                    stop_check=self._stop_check,
                    on_wait=self._on_cooldown_wait,
                )
            else:
                self.bot.wait_until_ready(timeout_s=30.0, poll_interval_s=1.0)


    def _update_status_cache(self, st: dict[str, Any]) -> None:
        pos = st.get("pos")
        hostile = st.get("nearest_hostile")
        hostile_id = hostile.get("id") if isinstance(hostile, dict) else None
        with self._state_lock:
            self._status_hostile_id = hostile_id if type(hostile_id) is int else None
            self._in_water = bool(st.get("in_water") or st.get("submerged"))
            if st.get("dimension"):
                self._last_dimension = normalize_dimension(st.get("dimension"))
            if "biome" in st and st.get("biome") is not None:
                self._last_biome = str(st.get("biome") or "")
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                try:
                    self._last_block_pos = [
                        math.floor(float(pos[0])),
                        math.floor(float(pos[1])),
                        math.floor(float(pos[2])),
                    ]
                except (TypeError, ValueError):
                    pass
        try:
            self.world_memory.observe_status(st)
        except Exception:
            pass


    def _cached_env(self, *, fetch_if_missing: bool = False) -> tuple[str, list[int] | None, str]:
        with self._state_lock:
            dim = self._last_dimension
            pos = list(self._last_block_pos) if self._last_block_pos is not None else None
            biome = self._last_biome
        if fetch_if_missing and pos is None and callable(getattr(self.bot, "status", None)):
            try:
                st = self.bot.status()
                if isinstance(st, dict):
                    self._update_status_cache(st)
                    with self._state_lock:
                        dim = self._last_dimension
                        pos = list(self._last_block_pos) if self._last_block_pos is not None else None
                        biome = self._last_biome
            except (ApiError, httpx.HTTPError, RuntimeError, ValueError):
                pass
        return dim, pos, biome


    def assess_target(
        self,
        target: str | list[str] | tuple[str, ...],
        *,
        mode: str = "auto",
        radius: int = 16,
    ) -> dict[str, Any]:
        """评估当前维度、高度与群系下寻找/挖掘或放置/交互指定目标的可行性。"""
        dim, pos, biome = self._cached_env(fetch_if_missing=True)
        return assess_world_target(
            target,
            dimension=dim,
            y=(pos[1] if pos is not None else None),
            biome=biome,
            radius=radius,
            mode=mode,
        )


    def last_block_pos(self) -> list[int] | None:
        with self._state_lock:
            return list(self._last_block_pos) if self._last_block_pos is not None else None


    def update_block_pos(self, pos: Any) -> None:
        if not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        try:
            blk = [math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))]
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._last_block_pos = blk


    def _is_in_water(self) -> bool:
        with self._state_lock:
            return self._in_water


    def _get_block_name(
        self, x: int, y: int, z: int, *, session: _SkillBudgetSession | None = None, default: str = "air",
    ) -> str:
        res = (session or self.bot).call("get_block", {"pos": [x, y, z]})
        raw = res.get("name") or res.get("block") or default
        return str(raw).removeprefix("minecraft:")


    def _ray_wall_blocker(
        self,
        start_xyz: tuple[float, float, float],
        end_xyz: tuple[float, float, float],
    ) -> dict[str, Any] | None:
        sx, sy, sz = start_xyz[0], start_xyz[1] + 1.62, start_xyz[2]
        ex, ey, ez = end_xyz[0], end_xyz[1] + 1.0, end_xyz[2]
        dist = math.sqrt((ex - sx) ** 2 + (ey - sy) ** 2 + (ez - sz) ** 2)
        if dist < 1.0:
            return None

        bx, by, bz = math.floor(start_xyz[0]), math.floor(start_xyz[1]), math.floor(start_xyz[2])
        hx, hy, hz = math.floor(end_xyz[0]), math.floor(end_xyz[1]), math.floor(end_xyz[2])
        skip_cells = {(bx, by, bz), (bx, by + 1, bz), (hx, hy, hz), (hx, hy + 1, hz)}

        steps = max(2, int(math.ceil(dist * 2.0)))
        visited: set[tuple[int, int, int]] = set()
        probes: list[tuple[int, int, int]] = []
        for idx in range(1, steps):
            t = idx / steps
            cell = (
                math.floor(sx + (ex - sx) * t),
                math.floor(sy + (ey - sy) * t),
                math.floor(sz + (ez - sz) * t),
            )
            if cell in skip_cells or cell in visited:
                continue
            visited.add(cell)
            probes.append(cell)
            if len(probes) >= 18:
                break

        for vx, vy, vz in probes:
            try:
                bname = self._get_block_name(vx, vy, vz)
            except (ApiError, httpx.HTTPError):
                return None
            if not is_passable_sight_block(bname):
                return {"name": bname, "pos": [vx, vy, vz]}
        return None


    def _enrich_status_awareness(self, st: dict[str, Any]) -> None:
        nh = st.get("nearest_hostile")
        pos = st.get("pos")
        if not isinstance(nh, dict) or not isinstance(pos, (list, tuple)) or len(pos) < 3:
            return
        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

        if not st.get("in_water"):
            try:
                for dy in (2, 3):
                    roof_name = self._get_block_name(bx, by + dy, bz)
                    if roof_name not in _PASSABLE_SIGHT_BLOCKS:
                        st["enclosure"] = {
                            "roofed_pocket": True,
                            "ceiling_block": roof_name,
                            "note": "上方有遮盖，但不代表四周封闭；确认无新受伤和无遮挡近敌后，可在掩体内就地合成/熔炼。",
                        }
                        break
            except (ApiError, httpx.HTTPError):
                pass

        # Occlusion is an observation of one ray at one instant, never proof of safety.
        assessment: dict[str, Any] = {
            "hostile_blocked_by_wall": None,
            "safe_in_place": False,
            "assessment_complete": False,
            "nearby_hostiles": [],
            "note": "附近敌情尚未确认；不要把未知实体视为隔墙安全。",
        }
        st["threat_assessment"] = assessment
        try:
            ents = self.bot.call("get_entities", {"radius": 28, "types": ["hostile"]})
        except (ApiError, httpx.HTTPError):
            return
        elist = ents.get("entities") or []
        candidates = []
        for ent in elist:
            if not isinstance(ent, dict):
                continue
            epos = ent.get("pos")
            if not isinstance(epos, (list, tuple)) or len(epos) < 3:
                continue
            try:
                xyz = tuple(float(v) for v in epos[:3])
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in xyz):
                continue
            candidates.append((math.dist((px, py, pz), xyz), ent, xyz))
        candidates.sort(key=lambda row: row[0])

        for distance, ent, xyz in candidates[:5]:
            blocker = self._ray_wall_blocker((px, py, pz), xyz)
            row = {
                "id": ent.get("id"), "name": ent.get("name"),
                "dist": round(distance, 1), "blocked_by_wall": blocker is not None,
            }
            if blocker is not None:
                row["blocking_block"] = blocker
            assessment["nearby_hostiles"].append(row)
            if nh.get("id") is not None and ent.get("id") == nh["id"]:
                assessment["hostile_blocked_by_wall"] = blocker is not None
                assessment["dy"] = round(xyz[1] - py, 1)
                if blocker is not None:
                    assessment["blocking_block"] = blocker

        checked = assessment["nearby_hostiles"]
        assessment["hostiles_observed"] = len(elist)
        assessment["hostiles_checked"] = len(checked)
        assessment["assessment_complete"] = (
            len(checked) == len(elist) and (not nh or assessment["hostile_blocked_by_wall"] is not None)
        )
        assessment["all_checked_hostiles_occluded"] = bool(checked) and all(r["blocked_by_wall"] for r in checked)
        if any(not r["blocked_by_wall"] for r in checked):
            assessment["note"] = "附近有无遮挡或遮挡未确认的怪物；最近一只隔墙不能代表整体安全。优先保持掩体、举盾或封堵，受伤时暂停采集/合成。"
        elif checked:
            assessment["note"] = "已检查怪物的当前射线均有遮挡，可保持现有掩体；仍需留意未检查实体、绕路和新受伤，勿据此认定绝对安全或盲目冲向地表。"


    def _call_json(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        timeout_s: float | None = None,
        session: _SkillBudgetSession | None = None,
    ) -> str:
        call_kw: dict[str, Any] = {"method": method}
        if timeout_s is not None:
            call_kw["timeout_s"] = float(timeout_s)
        try:
            result = (session or self.bot).call(path, body, **call_kw)
        except AuthError as exc:
            raise RuntimeError(f"MC API 会话不可继续：{exc}") from None
        except DeathLoopCooldownError as exc:
            if not self._wait_for_cooldown:
                raise RuntimeError(f"MC API 会话不可继续：{exc}") from None
            if session is not None:
                return self._format_api_error(path, body or {}, exc)
            try:
                self._reconnect_once(quarantined=False)
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except ApiError as exc:
            self.update_block_pos(exc.context.get("pos") or exc.context.get("from"))
            if session is not None or exc.code not in ("not_connected", "action_quarantined"):
                return self._format_api_error(path, body or {}, exc)
            try:
                self._reconnect_once(quarantined=(exc.code == "action_quarantined"))
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.TimeoutException:
            if session is not None:
                raise
            if timeout_s is not None and path == "move_to":
                try:
                    self.bot.call("stop", {}, timeout_s=2.0, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
                return json.dumps(
                    {
                        "ok": False,
                        "requires_replan": True,
                        "error": {
                            "code": "move_timeout",
                            "message": f"move_to 超过 {round(float(timeout_s), 1)}s 预算已中止移动，请缩短步长或更换路径。",
                        },
                    },
                    ensure_ascii=False,
                )
            try:
                self._reconnect_once(quarantined=True)
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError as retry_not_ready:
                return self._format_api_error(path, body or {}, retry_not_ready)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.HTTPError as exc:
            if session is not None:
                raise RuntimeError(f"MC API 网络请求异常（{type(exc).__name__}）：{exc}") from None
            # A timed-out action may still be running server-side. Recover the
            # quarantined session and retry the same request once.
            try:
                self._reconnect_once(quarantined=True)
                result = self.bot.call(path, body, **call_kw)
            except (AuthError, DeathLoopCooldownError) as re_exc:
                raise RuntimeError(f"MC API 会话不可继续：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error(path, body or {}, exc)
            except ApiError as retry_exc:
                self.update_block_pos(retry_exc.context.get("pos") or retry_exc.context.get("from"))
                return self._format_api_error(path, body or {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(
                    f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}"
                ) from None

        if path == "status" and isinstance(result, dict):
            self._update_status_cache(result)
            self._enrich_status_awareness(result)
        elif isinstance(result, dict):
            self.update_block_pos(result.get("pos_after") or result.get("pos"))
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False)


    @staticmethod
    def _format_api_error(action: str, params: dict[str, Any], exc: ApiError) -> str:
        err_obj: dict[str, Any] = {"code": exc.code, "message": exc.message, **exc.context}
        if action == "dig" and str(exc.code).lower() in ("out_of_reach", "path_not_found"):
            target_pos = params.get("pos")
            bot_pos = err_obj.get("pos") or err_obj.get("from")
            if (
                isinstance(target_pos, (list, tuple))
                and len(target_pos) >= 3
                and isinstance(bot_pos, (list, tuple))
                and len(bot_pos) >= 3
            ):
                try:
                    err_obj["dist"] = round(
                        math.dist(
                            [float(target_pos[0]), float(target_pos[1]), float(target_pos[2])],
                            [float(bot_pos[0]), float(bot_pos[1]), float(bot_pos[2])],
                        ),
                        1,
                    )
                except (TypeError, ValueError):
                    pass
        if action == "place" and exc.code in {"no_support", "blocked_by_self", "place_failed", "occupied"}:
            target = params.get("pos")
            if exc.code == "blocked_by_self":
                err_obj["recovery_hint"] = "先用 mc_move_to 到至少 3 格外，设 range=0.5，让出目标格，再放置；也可省略 x/y/z 自动选落点。"
            elif exc.code == "no_support":
                err_obj["recovery_hint"] = (
                    "目标需要相邻实心方块支撑；先用 mc_get_block 检查目标与下方，选择贴着实心面的空格。"
                    "若四周全空，先在旁边 mc_dig 一格侧袋制造参考面，或换一个贴着实心块的坐标，"
                    "也可省略 x/y/z 自动选落点；不要重复同一坐标。"
                )
            else:
                err_obj["recovery_hint"] = (f"先用 mc_get_block 检查目标 {target}；若仍是 sugar_cane、草或其他可清理障碍，先 mc_dig 清空目标格，再 mc_place。不要重复向同一占用格放置。")
        if action == "craft":
            err_obj = explain_material_craft_error(params.get("item", ""), params.get("count", 1), err_obj)
        if action == "use_item" and str(exc.code).lower() == "missing_item" and isinstance(err_obj.get("inventory"), dict):
            inv_map = err_obj["inventory"]
            avail_foods = [
                f
                for f in (*SAFE_FOOD_PRIORITY, "golden_apple", "enchanted_golden_apple")
                if int(inv_map.get(f) or 0) > 0
            ]
            if avail_foods:
                err_obj["kb_hint"] = (
                    f"背包中没有 {params.get('item')}，但已有可食用食物: {avail_foods}；"
                    f"请改传 mc_use_item(item='{avail_foods[0]}')！"
                )
            else:
                err_obj["no_food_in_inventory"] = True
                err_obj["kb_hint"] = (
                    "当前背包内没有任何可食用食物(0食物)！严禁继续调用 mc_use_item 空吃 bread/apple/golden_apple 等不存在的食物！"
                    "请立即停止进食尝试，继续执行挖矿/任务，或待地表安全时猎杀动物获取食物。"
                )
            return json.dumps({"ok": False, "error": err_obj}, ensure_ascii=False)
        hint = build_jit_error_hint(action, params, exc.code, exc.message)
        if (
            action == "craft"
            and str(exc.code).lower() == "missing_ingredients"
            and params.get("item") in ("stick", "crafting_table")
            and isinstance(err_obj.get("inventory"), dict)
        ):
            inv_map = err_obj["inventory"]
            has_planks = any(k.endswith("_planks") and int(v or 0) > 0 for k, v in inv_map.items())
            logs = [k for k, v in inv_map.items() if k.endswith("_log") and int(v or 0) > 0]
            if not has_planks and logs:
                extra = f"背包有原木({','.join(logs)})但木板为0，请先将 {logs[0]} 合成为木板(*_planks)，或直接调 mc_skill_craft_gear！"
                hint = f"{hint} {extra}" if hint else extra
        if hint:
            err_obj["kb_hint"] = hint
        return json.dumps({"ok": False, "error": err_obj}, ensure_ascii=False)


    def _run_skill(self, fn) -> str:
        try:
            result = fn()
        except (AuthError, SafetyEmergency) as exc:
            raise RuntimeError(f"MC 技能触发紧急停机：{exc}") from None
        except DeathLoopCooldownError as exc:
            if not self._wait_for_cooldown:
                raise RuntimeError(f"MC 技能触发紧急停机：{exc}") from None
            try:
                self._reconnect_once(quarantined=False)
                result = fn()
            except (AuthError, DeathLoopCooldownError, SafetyEmergency) as re_exc:
                raise RuntimeError(f"MC 技能触发紧急停机：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error("skill", {}, exc)
            except ApiError as retry_exc:
                return self._format_api_error("skill", {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except ApiError as exc:
            if exc.code not in ("not_connected", "action_quarantined"):
                return self._format_api_error("skill", {}, exc)
            try:
                self._reconnect_once(quarantined=(exc.code == "action_quarantined"))
                result = fn()
            except (AuthError, DeathLoopCooldownError, SafetyEmergency) as re_exc:
                raise RuntimeError(f"MC 技能触发紧急停机：{re_exc}") from None
            except BotNotReadyError:
                return self._format_api_error("skill", {}, exc)
            except ApiError as retry_exc:
                return self._format_api_error("skill", {}, retry_exc)
            except httpx.HTTPError as retry_http_exc:
                raise RuntimeError(f"MC API 网络请求异常（{type(retry_http_exc).__name__}）：{retry_http_exc}") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"MC API 网络请求异常（{type(exc).__name__}）：{exc}") from None

        if isinstance(result, dict):
            self.update_block_pos(result.get("pos_after") or result.get("pos"))
        return json.dumps(result, ensure_ascii=False)


    def mc_status(self) -> str:
        return self._call_json("status", method="GET")


    def mc_inventory(self) -> str:
        raw = self._call_json("inventory", method="GET")
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("ok") is True and isinstance(parsed.get("result"), dict):
                self.bot._last_inventory = parsed["result"]
        except ValueError:
            pass
        return raw


    _CROP_BLOCK_TYPES = frozenset(
        {
            "wheat",
            "carrots",
            "potatoes",
            "beetroots",
            "nether_wart",
            "cocoa",
            "sweet_berry_bush",
            "torchflower_crop",
            "pitcher_crop",
            "melon_stem",
            "pumpkin_stem",
        }
    )


    def _reserve_village_landmarks(
        self, blocks: list[dict[str, Any]], types: list[str], radius: int, limit: int
    ) -> list[dict[str, Any]]:
        rows = [row for row in blocks if isinstance(row, dict)]
        if not any(row.get("name") in VILLAGE_SURVEY_CLUTTER for row in rows):
            return blocks

        groups: dict[str, list[str]] = {}
        for name in types:
            if name in VILLAGE_LANDMARK_BLOCKS:
                key = "bed" if name.endswith("_bed") else name
                groups.setdefault(key, []).append(name)
        if not groups:
            return blocks

        nearest: dict[str, dict[str, Any]] = {}
        budget_s, _ = self._clamp_tool_budget(4.0, max_s=4.0, min_s=0.0, reserve_s=1.0)
        deadline = time.monotonic() + budget_s
        for key, names in groups.items():
            existing = next((row for row in rows if row.get("name") in names), None)
            if existing is not None:
                nearest[key] = existing
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0.15:
                continue
            try:
                found = self.bot.call(
                    "find_blocks", {"radius": radius, "types": names, "limit": 1},
                    timeout_s=min(1.0, remaining / 2), allow_reauth=False,
                )
            except (ApiError, httpx.HTTPError):
                continue
            first = next((
                row for row in (found.get("blocks") or [])
                if isinstance(row, dict) and row.get("name") in names
            ), None)
            if first is not None:
                nearest[key] = first

        reserved = list(nearest.values())
        return (reserved + [row for row in blocks if row not in reserved])[:limit]


    def mc_find_blocks(
        self,
        radius: int = 16,
        types: list[str] | None = None,
        limit: int = 20,
        mature: bool | None = None,
    ) -> str:
        requested_radius = int(radius)
        max_radius = 128 if types else 64
        radius = max(1, min(max_radius, requested_radius))
        limit = max(1, min(60, limit))
        payload: dict[str, Any] = {"radius": radius, "limit": limit}
        ignored: list[str] = []
        if types:
            requested = [str(name).strip().lower().removeprefix("minecraft:") for name in types]
            ignored = list(dict.fromkeys(name for name in requested if name in _NON_BLOCK_FIND_TYPES))
            dim_alias, _, _ = self._cached_env(fetch_if_missing=False)
            types = _normalize_find_block_types(types, dimension=dim_alias)
            if not types:
                return _invalid_find_block_query(ignored or requested)
            payload["types"] = types
        if mature is not None:
            norm_types = [str(t).removeprefix("minecraft:").strip().lower() for t in (types or [])]
            if not norm_types or any(t in self._CROP_BLOCK_TYPES for t in norm_types):
                payload["mature"] = mature
        radius_fallback_done = False
        while True:
            raw = self._call_json("find_blocks", payload)
            try:
                parsed = json.loads(raw)
            except ValueError:
                return raw
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if (
                not radius_fallback_done
                and payload.get("radius", 0) > 64
                and isinstance(err, dict)
                and str(err.get("code") or "") == "bad_args"
                and "radius" in str(err.get("message") or "").lower()
            ):
                # Backward-compat with pre-1.3.20 API servers that still cap radius at 64.
                radius_fallback_done = True
                radius = 64
                payload["radius"] = 64
                continue
            bad_type = (
                rejected_find_block(err.get("code"), str(err.get("message") or ""), types)
                if isinstance(err, dict) else None
            )
            if not bad_type or not types:
                break
            ignored.append(bad_type)
            types = [name for name in types if name != bad_type]
            if not types:
                return _invalid_find_block_query(ignored)
            payload["types"] = types
        if ignored and isinstance(parsed, dict):
            parsed["ignored_types"] = ignored
            raw = json.dumps(parsed, ensure_ascii=False)
        res_obj = parsed.get("result") if isinstance(parsed, dict) else None
        blocks = res_obj.get("blocks") if isinstance(res_obj, dict) else None
        if (parsed.get("ok") is True and isinstance(res_obj, dict)
                and isinstance(res_obj.get("total"), int) and isinstance(blocks, list)):
            # total is the backend scan count, not the number of returned rows.
            # Do not rewrite it; expose the truncation explicitly (P2-9).
            if types and "mature" not in payload and res_obj["total"] > len(blocks):
                prioritized = self._reserve_village_landmarks(blocks, types, radius, limit)
                if prioritized != blocks:
                    blocks = res_obj["blocks"] = prioritized
                    res_obj["village_landmarks_prioritized"] = True
            returned = len(blocks)
            res_obj["returned"] = returned
            if res_obj["total"] > returned:
                res_obj["truncated"] = True
                if res_obj.get("village_landmarks_prioritized"):
                    res_obj["total_note"] = "total 为原始匹配总数；blocks 优先保留村庄地标，最多返回 limit 条。"
                else:
                    res_obj["total_note"] = (
                        "total 为后端在半径内扫描到的匹配总数；blocks 仅返回前 limit 条。"
                        "请缩小 radius/types 或提高 limit（上限 60），不要把 total 当作可访问方块数。"
                    )
            raw = json.dumps(parsed, ensure_ascii=False)
        if parsed.get("ok") is True and isinstance(blocks, list) and blocks:
            dim_obs, _, _ = self._cached_env(fetch_if_missing=False)
            self.world_memory.observe_blocks(blocks, dim=dim_obs)
        if types:
            if parsed.get("ok") is True and isinstance(blocks, list) and len(blocks) == 0:
                dim, pos, biome = self._cached_env(fetch_if_missing=True)
                if pos is not None:
                    def _verify_live(pt: tuple[int, int, int]) -> str | None:
                        try:
                            return self._get_block_name(pt[0], pt[1], pt[2])
                        except Exception:
                            return None

                    mem_hits = self.world_memory.query_pois(
                        types,
                        origin=pos,
                        dim=dim,
                        limit=3,
                        verify_fn=_verify_live,
                        verify_radius=32.0,
                    )
                    if mem_hits:
                        res_obj["memory_blocks"] = mem_hits
                        res_obj["memory_hint"] = (
                            f"当前 {radius}m 内未扫到目标，但记忆库已探明最近 {mem_hits[0]['name']} "
                            f"位于 {mem_hits[0]['pos']}（距此 {mem_hits[0]['dist']}m，{mem_hits[0]['bearing']}），"
                            "可直接调 mc_skill_mine_blocks 或 mc_move_to(x, z) 分段前往。"
                        )
                        return json.dumps(parsed, ensure_ascii=False)
                check = assess_world_target(
                    types,
                    dimension=dim,
                    y=(pos[1] if pos is not None else None),
                    biome=biome,
                    radius=radius,
                    mode="find",
                )
                if check.get("available") is False:
                    res_obj["ecology_check"] = check
                    res_obj["hint"] = check["hint"]
                    return json.dumps(parsed, ensure_ascii=False)
        if isinstance(parsed, dict) and parsed.get("ok") is True and isinstance(res_obj, dict):
            _, cur_pos, _ = self._cached_env(fetch_if_missing=False)
            if self._annotate_scan_ceiling(
                parsed,
                res_obj,
                tool_name="mc_find_blocks",
                requested_radius=requested_radius,
                effective_radius=radius,
                query_types=types,
                empty=(isinstance(blocks, list) and len(blocks) == 0),
                cur_pos=cur_pos,
            ):
                return json.dumps(parsed, ensure_ascii=False)
        return raw


    def mc_get_block(self, x: int, y: int, z: int) -> str:
        return self._call_json("get_block", {"pos": [x, y, z]})


    def mc_get_entities(self, radius: int = 32, types: list[str] | None = None) -> str:
        requested_radius = int(radius)
        radius = max(1, min(64, requested_radius))
        payload: dict[str, Any] = {"radius": radius}
        if types:
            payload["types"] = types
        raw = self._call_json("get_entities", payload)
        try:
            parsed = json.loads(raw)
            res_ent = parsed.get("result") if isinstance(parsed, dict) else None
            ents = (res_ent or {}).get("entities") or []
            dim_ent, pos_ent, _ = self._cached_env(fetch_if_missing=False)
            if isinstance(parsed, dict) and parsed.get("ok") is True:
                nearby_drops = frozenset(
                    _normalize_drop_name(ent.get("item")) for ent in ents
                    if isinstance(ent, dict) and isinstance(ent.get("dist"), (int, float)) and ent["dist"] <= 4.5
                ) - {""}
                with self._state_lock:
                    self._entity_observations = {int(ent["id"]): ent for ent in ents if isinstance(ent, dict) and isinstance(ent.get("id"), int)}
                    self._entity_observed_mono = time.monotonic()
                    self._entity_observed_dimension = dim_ent
                    if nearby_drops or not types or "item" in types:
                        self._nearby_drop_observation = (dim_ent, nearby_drops) if nearby_drops else None
            if ents:
                self.world_memory.observe_entities(ents, dim=dim_ent)
            elif types and isinstance(res_ent, dict) and pos_ent is not None:
                hab_hits = self.world_memory.query_habitats(types, origin=pos_ent, dim=dim_ent, limit=3)
                if hab_hits:
                    res_ent["memory_habitats"] = hab_hits
                    return json.dumps(parsed, ensure_ascii=False)
            near_hostiles = [
                ent
                for ent in ents
                if isinstance(ent, dict)
                and ent.get("type") == "hostile"
                and float(ent.get("dist", 999)) <= 24.0
                and isinstance(ent.get("pos"), (list, tuple))
                and len(ent["pos"]) >= 3
            ][:5]
            if near_hostiles:
                st = self.bot.status()
                spos = st.get("pos") or []
                if isinstance(spos, (list, tuple)) and len(spos) >= 3:
                    start_xyz = (float(spos[0]), float(spos[1]), float(spos[2]))
                    for ent in near_hostiles:
                        epos = ent["pos"]
                        blocker = self._ray_wall_blocker(
                            start_xyz,
                            (float(epos[0]), float(epos[1]), float(epos[2])),
                        )
                        ent["blocked_by_wall"] = blocker is not None
                        if blocker is not None:
                            ent["blocking_block"] = blocker
                    return json.dumps(parsed, ensure_ascii=False)
            if isinstance(parsed, dict) and parsed.get("ok") is True and isinstance(res_ent, dict):
                if self._annotate_scan_ceiling(
                    parsed,
                    res_ent,
                    tool_name="mc_get_entities",
                    requested_radius=requested_radius,
                    effective_radius=radius,
                    query_types=types,
                    empty=(len(ents) == 0),
                    cur_pos=pos_ent,
                ):
                    return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            pass
        return raw


    def mc_events(self, limit: int = 50) -> str:
        limit = max(1, min(200, limit))
        with self._state_lock:
            since = self._last_event_seq
        raw = self._call_json("events", {"since": since, "limit": limit})
        try:
            parsed = json.loads(raw)
            if parsed.get("ok") and isinstance(parsed.get("result"), dict):
                res = parsed["result"]
                evs = res.get("events") or []
                page_max = max(
                    [int(res.get("last_seq") or 0)]
                    + [int(ev.get("seq") or 0) for ev in evs if isinstance(ev, dict)],
                    default=0,
                )
                if page_max > 0:
                    self._set_last_event_seq(page_max)
        except ValueError:
            pass
        return raw


    def sync_event_cursor(self) -> int:
        """翻页消费全部开局前历史事件，避免把旧死亡/受伤当成本轮新事件重放。"""
        for _ in range(50):
            with self._state_lock:
                since = self._last_event_seq
            try:
                res = self.bot.events(since=since, limit=50)
            except AuthError:
                raise
            except DeathLoopCooldownError:
                if self._wait_for_cooldown:
                    break
                raise
            except (ApiError, httpx.HTTPError):
                break
            if not isinstance(res, dict):
                break
            evs = res.get("events") or []
            evs_max = max(
                (int(ev.get("seq") or 0) for ev in evs if isinstance(ev, dict)),
                default=0,
            )
            api_last = int(res.get("last_seq") or 0)
            # Only jump to server-reported last_seq when the current page is final (<50 items);
            # otherwise advance only to the highest seq actually seen on this page so next page isn't skipped.
            page_max = evs_max if len(evs) >= 50 and evs_max > since else max(evs_max, api_last)
            self._set_last_event_seq(page_max)
            if len(evs) < 50 or page_max <= since:
                break
        with self._state_lock:
            return self._last_event_seq


    def drain_notable_events(self) -> str:
        """增量拉取最近事件并提取他人聊天、新解锁进度与受伤/死亡/重生提醒。"""
        notes: list[str] = []
        bot_name = (self.bot.bot_username or "").strip()
        for _ in range(10):
            with self._state_lock:
                since = self._last_event_seq
            try:
                res = self.bot.events(since=since, limit=50)
            except AuthError:
                raise
            except DeathLoopCooldownError:
                if self._wait_for_cooldown:
                    break
                raise
            except (ApiError, httpx.HTTPError):
                break
            if not isinstance(res, dict):
                break
            evs = res.get("events") or []
            evs_max = max(
                (int(ev.get("seq") or 0) for ev in evs if isinstance(ev, dict)),
                default=0,
            )
            api_last = int(res.get("last_seq") or 0)
            # Only jump to server-reported last_seq when the current page is final (<50 items);
            # otherwise advance only to the highest seq actually seen on this page so next page isn't skipped.
            page_max = evs_max if len(evs) >= 50 and evs_max > since else max(evs_max, api_last)
            self._set_last_event_seq(page_max)
            for ev in evs:
                if not isinstance(ev, dict):
                    continue
                ev_seq = ev.get("seq")
                if isinstance(ev_seq, int) and ev_seq <= since:
                    continue
                kind = str(ev.get("type") or ev.get("event") or "").lower()
                if kind == "chat":
                    sender = str(ev.get("username") or ev.get("from") or ev.get("sender") or "")
                    msg = str(ev.get("message") or ev.get("text") or "").strip()
                    if not msg:
                        continue
                    if sender and bot_name and sender == bot_name:
                        continue
                    if not sender and bot_name and msg.startswith(f"{bot_name} "):
                        continue
                    # Drop multiplayer server broadcast noise for other bots/players (advancements, deaths, joins)
                    if not sender or sender.lower() == "unknown":
                        low_msg = msg.lower()
                        if any(
                            m in low_msg
                            for m in (
                                "has made the advancement",
                                "has completed the challenge",
                                "has reached the goal",
                                "was slain by",
                                "was shot by",
                                "was blown up by",
                                "drowned",
                                "fell from a high place",
                                "hit the ground too hard",
                                "tried to swim in lava",
                                "suffocated in a wall",
                                "burned to death",
                                "joined the game",
                                "left the game",
                            )
                        ):
                            continue
                    notes.append(f"[不可信玩家公屏消息 {sender or 'unknown'}: {msg[:120]}]")
                elif kind in ("advancement", "advancements"):
                    adv = ev.get("advancement") or ev.get("id") or ev.get("title") or ev.get("name") or ""
                    if adv:
                        notes.append(f"[新解锁进度: {adv}]")
                elif kind == "hurt":
                    hp = ev.get("health")
                    src = ev.get("source") or ev.get("by") or ""
                    notes.append(f"[受伤 health={hp}{(' by ' + str(src)) if src else ''}]")
                elif kind == "death":
                    rec_fn = getattr(self.bot, "record_death_event", None)
                    if callable(rec_fn):
                        rec_fn(ev_seq if ev_seq is not None else ev.get("t"))
                    cause = ev.get("cause") or ev.get("message") or ""
                    pos = ev.get("pos")
                    drop_pos = ev.get("dropped_items_at") or pos
                    try:
                        self.world_memory.record_death(drop_pos, dim=self._last_dimension)
                    except Exception:
                        pass
                    notes.append(f"[死亡 pos={pos} {cause}]")
                elif kind in ("respawn", "spawn"):
                    notes.append(f"[已重生 pos={ev.get('pos')}]")
            if len(evs) < 50 or page_max <= since:
                break
        return "; ".join(notes)


    def mc_advancements(self, status: str | None = None) -> str:
        payload: dict[str, Any] = {"include_recipes": False}
        if status:
            payload["status"] = status
        return self._call_json("advancements", payload)


    @staticmethod
    def _annotate_stale_entity(raw: str, entity_id: int | None) -> str:
        if entity_id is None:
            return raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if not isinstance(parsed, dict) or parsed.get("ok") is not False:
            return raw
        err = parsed.get("error") if isinstance(parsed.get("error"), dict) else None
        if err is None or err.get("code") != "no_such_entity":
            return raw
        parsed["requires_replan"] = True
        err["message"] = (
            f"实体 {entity_id} 已不可见或消失；请重新调 mc_get_entities 获取最新 id/位置，"
            "不要重复使用旧 id。"
        )
        return json.dumps(parsed, ensure_ascii=False)

