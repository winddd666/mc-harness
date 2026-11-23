"""MacroSkills mixin: staircase (split from legacy library.py)."""

from __future__ import annotations

from mc_agent.skills.macro._base import (
    copy,
    partial,
    inspect,
    math,
    re,
    time,
    SimpleNamespace,
    Any,
    Callable,
    httpx,
    BotSession,
    ApiError,
    AuthError,
    DeathLoopCooldownError,
    RECIPES_1_21,
    assess_world_target,
    normalize_dimension,
    resolve_item_alias,
    check_and_stabilize,
    material_shortage,
    explain_craft_error,
    MAX_LOCAL_RECOVERY_ATTEMPTS,
    LOG_TYPES,
    NETHER_STEM_TYPES,
    BED_BLOCK_TYPES,
    VILLAGE_LANDMARK_BLOCKS,
    VILLAGE_SURVEY_CLUTTER,
    SURFACE_SCOUT_DIRECTIONS,
    SURFACE_SCOUT_ENTITIES,
    SURFACE_SCOUT_LANDMARKS,
    NETHER_SCOUT_TARGET_BLOCKS,
    NETHER_SCOUT_TARGET_ENTITIES,
    NETHER_TUNNEL_UNSUPPORTED_FLOORS,
    SURFACE_SCOUT_PATH_ERRORS,
    rejected_find_block,
    AXE_PRIORITY,
    PICKAXE_TIERS,
    STAIR_FILLER_BLOCKS,
    BLOCK_MIN_PICKAXE_TIER,
    ORE_COUNTERPARTS,
    EXPECTED_BLOCK_DROPS,
    _expected_drops_for_blocks,
    TABLE_RECIPES,
    _WORKBENCH_OPEN_ERRORS,
    DEFAULT_EQUIP_SLOTS,
    _REUSABLE_CRAFT_ITEMS,
    TOOL_WOOD_NEEDS,
    _inventory_counts_from_raw,
    _inventory_snapshot_and_counts,
    _inventory_counts,
    _window_item_entries,
    _log_to_planks_name,
    _PLANK_LOG_SOURCES,
    _PLANK_SOURCE_LOGS,
    _canonical_plank_recipe,
    _total_planks,
    _plank_equivalent,
    _stick_craft_batches,
    _wood_budget_shortage,
    _best_pickaxe_in_inv,
    MELEE_WEAPONS_PRIORITY,
    SAFE_MELEE_MOBS,
    best_melee_weapon_in_inv,
    PASSABLE_SIGHT_BLOCKS,
    PASSABLE_WALK_BLOCKS,
    NON_WALL_SIGHT_SUFFIXES,
    is_passable_sight_block,
    SHAFT_SEAL_BLOCKS,
    cardinal_bearing,
    _cached_block_name,
    ray_wall_blocker,
    inspect_local_enclosure,
    assess_hostile_occlusion,
    _is_protected_err,
    _TRANSIENT_BLOCK_ERRORS,
    _positive_inv_delta,
    _TRASH_TIER1_ZERO_KEEP,
    _TRASH_TIER2_DECOR_STONES,
    _TRASH_TIER2_LOOSE_BLOCKS,
    _TRASH_TIER3_DIRT,
    _TRASH_TIER4_COBBLE,
    _TRASH_TIER_NETHER,
    _COMMON_GATHER_DROPS,
    _MINING_RETENTION_CAPS,
    _inventory_retention_plan,
    _UNSTACKABLE_ITEMS,
    _CRAFT_SLOT_INPUTS,
    craft_material_shortage,
    _reserved_craft_materials,
    explain_material_craft_error,
    _compute_empty_slots,
    _is_stackable_item,
    _can_fit_item_in_inventory,
    _yaw_toward,
    _CONTAINER_TYPES,
    SMELT_OUTPUTS,
    _validate_smelt_input,
    _merge_counts,
    _clean_block,
    _HIGH_VALUE_DROPS,
    _PICKUP_INTERRUPTION_CODES,
    _MID_VALUE_DROPS,
    _normalize_drop_name,
    _summarize_uncollected_drops,
    _StairDeadline,
    _StairSession,
    _SkillDeadline,
    _SkillBudgetSession,
)


class _StaircaseMixin:
    def _dig_pillar_up(self, steps: int = 4, stop_on_ore: bool = True) -> dict[str, Any]:
        """Dig 1x1 overhead safely and jump-place underfoot blocks to climb straight up."""
        steps = max(1, min(12, int(steps)))
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
        hazard_set = fluids | falling_set
        ore_types = ["iron_ore", "deepslate_iron_ore", "coal_ore", "deepslate_coal_ore", "copper_ore"]
        iron_names = {"iron_ore", "deepslate_iron_ore"}
        filler_candidates = STAIR_FILLER_BLOCKS

        def _bname(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        def _pick_filler() -> str | None:
            counts = _inventory_counts(self.bot)
            for cand in filler_candidates:
                if counts.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(counts.items()):
                if cnt >= 1 and name.endswith("_planks"):
                    return name
            return None

        def _confirm_pillar_surface(cur: list[float]) -> dict[str, Any] | None:
            cx, cy, cz = (math.floor(float(v)) for v in cur)
            below = _bname([cx, cy - 1, cz])
            surface_ground = {"grass_block", "podzol", "mycelium", "sand", "red_sand", "snow_block"}
            if below in surface_ground and all(_bname([cx, cy + dy, cz]) in air_set for dy in (0, 1)):
                return {"floor": below, "pos": list(cur), "basis": "standing_on_surface_ground"}
            try:
                surf = self.bot.call("find_blocks", {"radius": 5, "types": ["grass_block", "podzol", "sand"], "limit": 8})
            except ApiError:
                return None
            same_level = [b for b in surf.get("blocks", []) if b.get("pos") and abs(float(b["pos"][1]) + 1 - cy) <= 1]
            if same_level and all(_bname([cx, cy + dy, cz]) in air_set for dy in range(2, 14)):
                return {"floor": below, "pos": list(cur), "basis": "same_level_surface_and_clear_column"}
            return None

        st0 = self.bot.status()
        pos_before = st0.get("pos") or [0.0, 64.0, 0.0]
        last_death = (st0.get("last_death") or {}).get("t")
        min_hp = st0.get("health")
        steps_done = 0
        dug_count = 0
        placed_count = 0
        stop_reason = "completed"
        blocker_info: dict[str, Any] | None = None
        last_error: dict[str, Any] | None = None
        ores_nearby: list[dict[str, Any]] = []
        surface_evidence: dict[str, Any] | None = None

        try:
            for step_idx in range(steps):
                if isinstance(self.bot, _StairSession):
                    self.bot.step_deadline = min(self.bot.deadline, time.monotonic() + self.bot.per_step_timeout)
                st = self.bot.status()
                if st.get("alive") is False or (st.get("last_death") or {}).get("t") != last_death:
                    stop_reason = "died"
                    break
                if st.get("ready") is False:
                    stop_reason = "not_ready"
                    break
                if st.get("in_water") or st.get("in_lava"):
                    stop_reason = "entered_fluid"
                    break
                hp_now = st.get("health")
                if isinstance(min_hp, (int, float)) and isinstance(hp_now, (int, float)) and hp_now < min_hp:
                    stop_reason = "hurt"
                    break
                if isinstance(hp_now, (int, float)):
                    min_hp = hp_now

                pos = st.get("pos") or pos_before
                px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
                bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

                hit_col: dict[str, Any] | None = None
                for sc in ([bx, by + 1, bz], [bx, by + 2, bz]):
                    sb = _bname(sc)
                    if sb in fluid_or_bedrock:
                        hit_col = {"pos": sc, "block": sb, "relation": "in_path"}
                        stop_reason = "fluid_or_bedrock"
                        break
                if hit_col is not None:
                    blocker_info = hit_col
                    break

                top3_pos = [bx, by + 3, bz]
                top3 = _bname(top3_pos)
                if top3 in fluids:
                    blocker_info = {"pos": top3_pos, "block": top3, "relation": "overhead_fluid"}
                    stop_reason = "fluid_or_bedrock"
                    break
                if top3 in falling_set:
                    blocker_info = {"pos": top3_pos, "block": top3, "relation": "falling_block"}
                    stop_reason = "falling_material_adjacent"
                    break

                dig_abort = False
                for tpos in ([bx, by + 1, bz], [bx, by + 2, bz]):
                    tname = _bname(tpos)
                    if tname in air_set:
                        continue
                    for ax, az in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nb_pos = [tpos[0] + ax, tpos[1], tpos[2] + az]
                        nb_name = _bname(nb_pos)
                        if nb_name in fluids:
                            blocker_info = {"pos": nb_pos, "block": nb_name, "relation": "adjacent_fluid", "target": tpos}
                            stop_reason = "fluid_or_bedrock"
                            dig_abort = True
                            break
                        if nb_name in falling_set:
                            blocker_info = {"pos": nb_pos, "block": nb_name, "relation": "falling_block", "target": tpos}
                            stop_reason = "falling_material_adjacent"
                            dig_abort = True
                            break
                    if dig_abort:
                        break
                    req_tier = BLOCK_MIN_PICKAXE_TIER.get(tname, 0)
                    pick_name, pick_tier = _best_pickaxe_in_inv(_inventory_counts(self.bot))
                    if req_tier > pick_tier:
                        blocker_info = {
                            "pos": tpos,
                            "block": tname,
                            "relation": "insufficient_pickaxe_tier",
                            "required_tier": req_tier,
                            "current_pickaxe": pick_name,
                            "current_tier": pick_tier,
                        }
                        stop_reason = "insufficient_pickaxe_tier"
                        dig_abort = True
                        break
                    if req_tier > 0 or any(tag in tname for tag in ("stone", "ore", "deepslate", "granite", "diorite", "andesite", "tuff")):
                        self._equip_if_present(tuple(PICKAXE_TIERS))
                    try:
                        self.bot.call("dig", {"pos": tpos, "collect": True, "stop_on": ["hurt", "death"]})
                    except ApiError as err:
                        last_error = {"code": err.code, "message": err.message, "pos": tpos}
                        stop_reason = "dig_failed"
                        dig_abort = True
                        break
                    if _bname(tpos) not in air_set:
                        last_error = {"code": "dig_not_confirmed", "message": f"挖掘 {tpos} 后仍非空气", "pos": tpos}
                        stop_reason = "dig_failed"
                        dig_abort = True
                        break
                    dug_count += 1
                if dig_abort:
                    break

                filler = _pick_filler()
                if not filler:
                    stop_reason = "missing_step_support"
                    break

                if max(abs(px - (bx + 0.5)), abs(pz - (bz + 0.5))) > 0.28:
                    try:
                        self.bot.call("move_to", {"x": bx + 0.5, "y": by, "z": bz + 0.5, "range": 0.2, "timeout": 1500})
                    except ApiError:
                        pass

                placed_ok = False
                place_failures = 0
                retry_after = 0.0
                self.bot.call("posture", {"sneak": False, "shield": False, "jump": True})
                try:
                    for attempt in range(48):
                        time.sleep(0.05)
                        st_j = self.bot.status()
                        if st_j.get("alive") is False or st_j.get("ready") is False:
                            stop_reason = "not_ready" if st_j.get("ready") is False else "died"
                            break
                        if time.monotonic() < retry_after:
                            continue
                        jpos = st_j.get("pos") or [bx + 0.5, by, bz + 0.5]
                        if float(jpos[1]) < by + 1.0 and attempt < 2:
                            continue
                        try:
                            self.bot.call("place", {"item": filler, "pos": [bx, by, bz]})
                            placed_ok = True
                            placed_count += 1
                            break
                        except ApiError as perr:
                            if perr.code == "blocked_by_self":
                                continue
                            last_error = {"code": perr.code, "message": perr.message, "pos": [bx, by, bz]}
                            if perr.code in {"place_failed", "no_support"} and place_failures < 2:
                                # r14 portal recovery: the first jump-place was
                                # rejected while airborne, then the next whole
                                # call succeeded. D1/BUG-024: the same path can
                                # answer no_support before the new floor settles.
                                # Wait one jump cycle and retry.
                                place_failures += 1
                                retry_after = time.monotonic() + 0.45
                                continue
                            break
                finally:
                    try:
                        self.bot.call("posture", {"sneak": False, "shield": False, "jump": False})
                    except ApiError:
                        pass
                if not placed_ok:
                    if stop_reason == "completed":
                        stop_reason = "place_failed"
                    break

                settled_up = False
                cur_pos = [bx + 0.5, float(by), bz + 0.5]
                for _ in range(8):
                    time.sleep(0.05)
                    st_after_step = self.bot.status()
                    raw_p = st_after_step.get("pos") or cur_pos
                    cur_pos = [float(raw_p[0]), float(raw_p[1]), float(raw_p[2])]
                    if math.floor(cur_pos[1]) >= by + 1:
                        settled_up = True
                        break
                if not settled_up:
                    stop_reason = "movement_not_confirmed"
                    break

                steps_done += 1
                if isinstance(self.bot, _StairSession):
                    self.bot.progress.update(steps_done=steps_done, dug_count=dug_count, placed_count=placed_count)
                if steps_done >= 2:
                    surface_evidence = _confirm_pillar_surface(cur_pos)
                    if surface_evidence:
                        stop_reason = "reached_surface"
                        break

                if stop_on_ore and (steps_done % 2 == 0 or step_idx == steps - 1):
                    try:
                        found_ores = self.bot.call("find_blocks", {"radius": 8, "types": ore_types, "limit": 10})
                        ores_nearby = found_ores.get("blocks") or []
                        if any(b.get("name") in iron_names for b in ores_nearby):
                            stop_reason = "iron_ore_found"
                            break
                    except ApiError:
                        pass
        except ApiError as exc:
            stop_reason = "api_error"
            last_error = {"code": exc.code, "message": exc.message, **exc.context}

        if isinstance(self.bot, _StairSession):
            self.bot.step_deadline = self.bot.deadline
        st_end = self.bot.status()
        end_pos = st_end.get("pos") or pos_before
        ok = steps_done > 0 and stop_reason in {"completed", "iron_ore_found", "reached_surface"}
        out_pillar: dict[str, Any] = {
            "ok": ok,
            "requires_replan": not ok,
            "direction": "pillar",
            "vertical": "pillar",
            "steps_requested": steps,
            "steps_done": steps_done,
            "dug_count": dug_count,
            "placed_count": placed_count,
            "stop_reason": stop_reason,
            "pos_before": pos_before,
            "pos_after": end_pos,
        }
        if blocker_info is not None:
            out_pillar["blocker"] = blocker_info
        if last_error is not None:
            out_pillar["last_error"] = last_error

        if not ok:
            # A failed jump-place is read back while the bot is still airborne
            # (r12: pos_after y=97.2 for a grounded start at y=96). With no
            # confirmed step the bot falls back to the start cell, so remember
            # the failure there; otherwise the next probe advertises
            # viable_pillar_up=true at the exact cell that just failed.
            record_pos = pos_before if steps_done == 0 else end_pos
            ebx, eby, ebz = (
                math.floor(float(record_pos[0])),
                math.floor(float(record_pos[1])),
                math.floor(float(record_pos[2])),
            )
            self._record_failed_stair_dir((ebx, eby, ebz, "pillar"), "pillar", stop_reason)
            if steps_done > 0 and st_end.get("on_ground") is False:
                # Partial climb: the last confirmed step is one block below
                # the airborne read-back; block retries there as well.
                self._record_failed_stair_dir((ebx, eby - 1, ebz, "pillar"), "pillar", stop_reason)
            probe_cache_p: dict[tuple[int, int, int], str] = {}
            inv_p = _inventory_counts(self.bot)
            up_dirs = self._probe_viable_up_stair_dirs(ebx, eby, ebz, probe_cache=probe_cache_p, inv_counts=inv_p)
            tunnel_dirs = self._probe_viable_tunnel_dirs(ebx, eby, ebz, probe_cache=probe_cache_p, inv_counts=inv_p)
            out_pillar["viable_up_directions"] = up_dirs
            out_pillar["viable_tunnel_directions"] = tunnel_dirs
            detail_p = (
                f"{stop_reason}({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
                if blocker_info
                else (
                    f"{stop_reason}({last_error['code']}@{last_error.get('pos')})"
                    if last_error and last_error.get("code")
                    else stop_reason
                )
            )
            if up_dirs:
                out_pillar["hint"] = (
                    f"原地垂直垫脚直上遇到{detail_p}；实测可改走斜向阶梯上行方向: {','.join(up_dirs)}"
                    f"（请调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6)）。"
                )
            elif tunnel_dirs:
                out_pillar["hint"] = (
                    f"原地垂直垫脚直上遇到{detail_p}；实测可改走水平矿道方向: {','.join(tunnel_dirs)}"
                    f"（请调 mc_skill_tunnel(direction='{tunnel_dirs[0]}', steps=6)）。"
                )
            else:
                out_pillar["hint"] = (
                    f"原地垂直垫脚直上遇到{detail_p}；当前坐标[{ebx},{eby},{ebz}]斜向上行与水平矿道也受阻，"
                    "请调 mc_move_to 退回安全矿道或先用原子工具清理周围障碍。"
                )

        if surface_evidence is not None:
            out_pillar["surface_evidence"] = surface_evidence
        out_pillar["ores_nearby"] = ores_nearby
        return out_pillar


    def dig_staircase(
        self,
        direction: str | None = None,
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool | None = None,
        *,
        deadline: float | None = None,
        per_step_timeout: float = 15.0,
    ) -> dict[str, Any]:
        parent_limits_first_dig = deadline is not None and deadline - time.monotonic() < 40.0
        session = _StairSession(self.bot, 45.0, deadline=deadline, per_step_timeout=per_step_timeout)
        scoped = copy(self)
        scoped.bot = session
        try:
            return scoped._dig_staircase(direction, steps, vertical, stop_on_ore)
        except _StairDeadline:
            parent_budget_spent = parent_limits_first_dig and time.monotonic() >= session.deadline
            cleanup: dict[str, Any] = {}
            try:
                cleanup = self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
            except (ApiError, httpx.HTTPError):
                pass
            if str(vertical).lower() == "pillar" or str(direction).lower() == "pillar":
                try:
                    # The scoped session has expired, so its ordinary finally
                    # cannot clear the persistent jump posture.
                    self.bot.call("posture", {"jump": False, "sneak": False, "shield": False}, timeout_s=1.0, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
            timeout_message = "阶梯总预算或单步等待耗尽；请根据实际位置重新规划。"
            pick_tier = getattr(session, "pickaxe_tier", None)
            low_tier_slow_dig = False
            if session.last_timeout_path == "dig" and not parent_budget_spent:
                timeout_message = (
                    f"单步挖掘未在预算内确认（目标 {session.last_dig_pos}）。"
                    "若目标位于脚下、为黑曜石/高硬度方块，或同一平台连续超时，"
                    "先调 mc_skill_unstick 并离开该处 8~12 格后再挖；不要原地重复同一方向。"
                )
                # BUG-041/r22_long: with a wooden pickaxe every stone staircase
                # step costs ~25s, so the model loops for ~30s per 1 block.
                if isinstance(pick_tier, int) and pick_tier < 2:
                    low_tier_slow_dig = True
                    blk = session.last_dig_block or "硬质方块"
                    tier_label = {0: "无镐/空手", 1: "木镐"}.get(pick_tier, f"tier {pick_tier}")
                    timeout_message = (
                        f"单步挖掘未在预算内确认：当前镐等级 {pick_tier}（{tier_label}）挖 {blk} "
                        f"约需 25s/格，45s 总预算只够 1 格。"
                        "先调 mc_skill_craft_gear(item='stone_pickaxe')（有圆石时），"
                        "或改走泥土/砂砾/沙子等软方块方向，或先 mc_skill_harvest_logs 补木料；"
                        "也可先 mc_skill_unstick 让开该格；不要在硬岩上原地重复同一步梯。"
                    )
                if isinstance(session.last_dig_pos, (list, tuple)) and len(session.last_dig_pos) >= 3:
                    try:
                        self._avoid_temporarily(
                            (int(session.last_dig_pos[0]), int(session.last_dig_pos[1]), int(session.last_dig_pos[2])),
                            "dig_timeout",
                            30.0,
                        )
                    except (TypeError, ValueError):
                        pass
                # BUG-041/r23: zero-progress dig timeout -> cool the whole stand
                # down so the next call must relocate instead of retrying.
                if not session.progress.get("steps_done"):
                    start_cell = getattr(session, "start_cell", None)
                    if isinstance(start_cell, tuple) and len(start_cell) == 3:
                        try:
                            self._record_failed_stair_dir(
                                (int(start_cell[0]), int(start_cell[1]), int(start_cell[2]),
                                 str(vertical or "down").strip().lower()),
                                str(direction or "north").strip().lower(),
                                "dig_timeout",
                                ttl_s=60.0,
                            )
                        except (TypeError, ValueError):
                            pass
            return {
                "ok": False, "requires_replan": True, "stop_reason": "time_budget",
                "error": {"code": "time_budget", "message": timeout_message},
                "direction": direction, "vertical": vertical, "steps_requested": steps,
                **session.progress, "pos_after": session.last_status.get("pos"),
                "timeout_path": session.last_timeout_path,
                "retried_path": session.last_retry_path,
                "dig_target": session.last_dig_pos,
                "dig_block": session.last_dig_block,
                "pickaxe_tier": pick_tier,
                "low_tier_slow_dig": low_tier_slow_dig,
                "action_released": cleanup.get("released", cleanup.get("stopped", False)),
            }


    def _dig_staircase(
        self,
        direction: str | None = None,
        steps: int = 12,
        vertical: str = "down",
        stop_on_ore: bool | None = None,
    ) -> dict[str, Any]:
        """Dig a safe 3-high stepped staircase downward, upward, or straight-up pillar."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        d_key = str(direction or "").strip().lower()
        v_key = str(vertical or "down").strip().lower()
        if stop_on_ore is None:
            stop_on_ore = v_key == "down"
        if v_key == "pillar" or d_key == "pillar":
            return self._dig_pillar_up(steps=steps, stop_on_ore=stop_on_ore)
        if v_key not in {"down", "up"}:
            return {
                "ok": False,
                "error": "bad_args",
                "message": "direction 必须是 north/south/east/west，vertical 必须是 down/up/pillar。",
            }
        if not d_key or d_key in {"down", "up"}:
            chosen_dir = "north"
            try:
                st_probe = self.bot.status() or {}
                p_probe = st_probe.get("pos") or []
                if isinstance(p_probe, (list, tuple)) and len(p_probe) >= 3:
                    px, py, pz = math.floor(float(p_probe[0])), math.floor(float(p_probe[1])), math.floor(float(p_probe[2]))
                    cands = (
                        self._probe_viable_down_stair_dirs(px, py, pz)
                        if v_key == "down"
                        else self._probe_viable_up_stair_dirs(px, py, pz, require_filler_face=True)
                    )
                    if cands:
                        chosen_dir = cands[0]
            except ApiError:
                pass
            d_key = chosen_dir
        if d_key not in dirs:
            return {
                "ok": False,
                "error": "bad_args",
                "message": "direction 必须是 north/south/east/west，vertical 必须是 down/up/pillar。",
            }

        dx, dz = dirs[d_key]
        steps = max(1, min(25, int(steps)))
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        # r14: a fortress/nether portal head cell was repeatedly dug and
        # returned undiggable for all four directions. Treat these as
        # obstacles, not as dig targets.
        undiggable_obstacles = {"nether_portal", "end_portal", "end_gateway", "barrier"}
        non_solid = air_set | fluids | {
            "short_grass",
            "tall_grass",
            "leaf_litter",
            "dandelion",
            "poppy",
            "oak_sapling",
        }
        stop_ore_names = ("iron_ore", "deepslate_iron_ore", "diamond_ore", "deepslate_diamond_ore")
        surface_evidence: dict[str, Any] | None = None
        dry_passable = PASSABLE_WALK_BLOCKS - fluids
        unsafe_ore_neighbors = fluids | {"unknown"}

        def _ore_ray_clear(
            scan: _StairSession,
            eye: tuple[float, float, float],
            ore: tuple[int, int, int],
            target: tuple[float, float, float],
            cache: dict[tuple[int, int, int], str],
        ) -> bool:
            # Probe a point in every voxel crossed by the eye-to-ore ray.
            crossings = {0.0, 1.0}
            for start, end in zip(eye, target):
                crossings.update(
                    (plane - start) / (end - start)
                    for plane in range(math.floor(min(start, end)) + 1, math.ceil(max(start, end)))
                )
            times = sorted(crossings)
            for lo, hi in zip(times, times[1:]):
                mid = (lo + hi) / 2
                cell = tuple(math.floor(start + (end - start) * mid) for start, end in zip(eye, target))
                if cell != ore and _cached_block_name(scan, *cell, cache) not in dry_passable:
                    return False
            return True

        def _scan_actionable_ores(cur: list[float]) -> list[dict[str, Any]]:
            if len(cur) < 3:
                return []
            # Optional ore probes share the step deadline and may be skipped under load.
            scan = _StairSession(
                self.bot.bot, 2.5, deadline=min(self.bot.deadline, self.bot.step_deadline),
            )
            cache: dict[tuple[int, int, int], str] = {}
            nearby: list[dict[str, Any]] = []
            cx, cy, cz = (float(v) for v in cur[:3])
            eye = (cx, cy + 1.62, cz)
            try:
                found = scan.call("find_blocks", {"radius": 4, "types": list(stop_ore_names), "limit": 10})
                for block in found.get("blocks") or []:
                    pos = block.get("pos")
                    name = block.get("name")
                    if name not in stop_ore_names or not isinstance(pos, (list, tuple)) or len(pos) < 3:
                        continue
                    ore = ox, oy, oz = tuple(int(v) for v in pos[:3])
                    center = (ox + 0.5, oy + 0.5, oz + 0.5)
                    distance = math.dist((cx, cy, cz), center)
                    if distance > 4.0 or self._is_block_avoided(ore):
                        continue
                    if (center[0] - cx) * dx + (center[2] - cz) * dz < -1.5:
                        continue
                    if (v_key == "down" and oy > cy + 2.5) or (v_key == "up" and oy < cy - 2.5):
                        continue

                    if not any(
                        _ore_ray_clear(scan, eye, ore, (center[0], oy + height, center[2]), cache)
                        for height in (0.5, 0.2, 0.8)
                    ):
                        continue
                    if _cached_block_name(scan, *ore, cache) != name:
                        continue
                    if any(
                        _cached_block_name(scan, ox + ax, oy + ay, oz + az, cache) in unsafe_ore_neighbors
                        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
                    ):
                        continue
                    nearby.append({**block, "dist": round(distance, 1)})
            except _StairDeadline:
                pass
            return nearby

        def _block_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        def _is_non_solid_support(bname: str) -> bool:
            return bname in non_solid or bname.endswith(("_leaves", "_sapling", "_carpet", "_banner"))

        def _find_adjacent_fluid(
            targets: list[list[int]],
            cache: dict[tuple[int, int, int], str] | None = None,
        ) -> dict[str, Any] | None:
            def _cached_block(pt: tuple[int, int, int]) -> str:
                if cache is not None and pt in cache:
                    return cache[pt]
                val = _block_name([pt[0], pt[1], pt[2]])
                if cache is not None:
                    cache[pt] = val
                return val

            for pos_item in targets:
                px, py, pz = int(pos_item[0]), int(pos_item[1]), int(pos_item[2])
                if _cached_block((px, py, pz)) in air_set:
                    continue
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = (px + ax, py + ay, pz + az)
                    bname = _cached_block(nb)
                    if bname in fluids:
                        return {
                            "pos": [nb[0], nb[1], nb[2]],
                            "block": bname,
                            "relation": "adjacent_fluid",
                            "target": [px, py, pz],
                        }
            return None

        def _equip_best_pick() -> None:
            best_pick, _ = _best_pickaxe_in_inv(_inventory_counts(self.bot))
            if best_pick:
                try:
                    self.bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass

        def _pick_step_filler(exclude: set[str] | None = None) -> str | None:
            excluded = exclude or set()
            counts = _inventory_counts(self.bot)
            for cand in STAIR_FILLER_BLOCKS:
                if cand not in excluded and counts.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(counts.items()):
                if name not in excluded and cnt >= 1 and name.endswith("_planks"):
                    return name
            return None

        def _settled_position(cur: list[float]) -> list[float]:
            for _ in range(6):
                time.sleep(0.1)
                settled = self.bot.status()
                cur = settled.get("pos") or cur
                if settled.get("on_ground") is not False:
                    break
            return [float(cur[0]), float(cur[1]), float(cur[2])]

        def _advance_step(tx: int, ty: int, tz: int, bx: int, by: int, bz: int, *, jump: bool) -> list[float]:
            fx, fy, fz = tx + 0.5, float(ty), tz + 0.5
            try:
                mv = self.bot.call("move_to", {"x": fx, "y": ty, "z": fz, "range": 0.5})
                cur = mv.get("pos") or self.bot.status().get("pos") or [bx + 0.5, float(by), bz + 0.5]
            except ApiError:
                cur = self.bot.status().get("pos") or [bx + 0.5, float(by), bz + 0.5]

            if math.floor(float(cur[0])) == bx and math.floor(float(cur[2])) == bz:
                try:
                    self.bot.call("look", {"x": fx, "y": fy, "z": fz})
                except ApiError:
                    pass
                ctrl_body: dict[str, Any] = {
                    "forward": True,
                    "ms": 550 if jump else 420,
                    "stop_on": ["hurt", "death"],
                }
                if jump:
                    ctrl_body["jump"] = True
                try:
                    ctrl = self.bot.call("control", ctrl_body)
                    cur = ctrl.get("pos") or self.bot.status().get("pos") or cur
                except ApiError:
                    cur = self.bot.status().get("pos") or cur
            # Control results can be captured at the jump apex. Confirm the
            # settled position instead of counting a jump that lands back below.
            return _settled_position(cur)

        def _confirm_surface(cur: list[float]) -> dict[str, Any] | None:
            cx, cy, cz = (math.floor(float(v)) for v in cur)
            below = _block_name([cx, cy - 1, cz])
            surface_ground = {"grass_block", "podzol", "mycelium", "sand", "red_sand", "snow_block"}
            if below in surface_ground and all(_block_name([cx, cy + dy, cz]) in air_set for dy in (0, 1)):
                return {"floor": below, "pos": list(cur), "basis": "standing_on_surface_ground"}
            # Nearby roots/grass above the ceiling do not mean the bot reached
            # that level. Stone steps need a same-level surface and an open sky column.
            surf = self.bot.call("find_blocks", {"radius": 5, "types": ["grass_block", "podzol", "sand"], "limit": 8})
            same_level = [b for b in surf.get("blocks", []) if b.get("pos") and abs(float(b["pos"][1]) + 1 - cy) <= 1]
            if same_level and all(_block_name([cx, cy + dy, cz]) in air_set for dy in range(2, 14)):
                return {"floor": below, "pos": list(cur), "basis": "same_level_surface_and_clear_column"}
            return None

        _equip_best_pick()
        # BUG-041: remember the pickaxe tier so a dig timeout can name the real
        # cause (wooden pickaxe on hard rock costs ~25s per block).
        try:
            self.bot.pickaxe_tier = _best_pickaxe_in_inv(_inventory_counts(self.bot))[1]
        except (ApiError, httpx.HTTPError, TypeError, ValueError):
            self.bot.pickaxe_tier = None
        st0 = self.bot.status()
        pos_before = st0.get("pos") or [0.0, 64.0, 0.0]
        steps_done = 0
        dug_count = 0
        placed_count = 0
        stop_reason = "completed"
        matched_ores: list[dict[str, Any]] = []
        last_error: dict[str, Any] | None = None
        blocker_info: dict[str, Any] | None = None
        b0x, b0y, b0z = (
            math.floor(float(pos_before[0])),
            math.floor(float(pos_before[1])),
            math.floor(float(pos_before[2])),
        )

        if v_key == "down":
            if _block_name([b0x, b0y - 1, b0z]).endswith("_leaves"):
                self.descend_canopy()
                st0 = self.bot.status()
                cur_p = st0.get("pos") or pos_before
                c0x, c0y, c0z = (
                    math.floor(float(cur_p[0])),
                    math.floor(float(cur_p[1])),
                    math.floor(float(cur_p[2])),
                )
                b0x, b0y, b0z = c0x, c0y, c0z
                if _block_name([c0x, c0y - 1, c0z]).endswith("_leaves"):
                    stop_reason = "on_tree_canopy"

        # BUG-041/r23: a zero-progress dig timeout from this exact stand means
        # every direction here re-stalls. Refuse early instead of burning
        # another 45s per attempt.
        _start_cell = (b0x, b0y, b0z)
        try:
            self.bot.start_cell = _start_cell
        except (AttributeError, TypeError):
            pass
        _cooldown = self._active_failed_stair_dirs((b0x, b0y, b0z, v_key))
        if any("dig_timeout" in str(r) for r in _cooldown.values()):
            return {
                "ok": False,
                "requires_replan": True,
                "direction": d_key,
                "vertical": v_key,
                "steps_requested": steps,
                "steps_done": 0,
                "dug_count": 0,
                "stop_reason": "stair_cell_cooldown",
                "pos_before": pos_before,
                "pos_after": pos_before,
                "last_error": {
                    "code": "stair_cell_cooldown",
                    "pos": [b0x, b0y, b0z],
                    "message": "该站位刚发生零进展 dig 超时，已冷却 60s，未再尝试阶梯。",
                },
                "hint": (
                    "刚在同一站位连续 dig 超时（0 进展）。请先用 mc_move_to 水平离开 8~12 格"
                    " 或 mc_skill_unstick 换站位，再重新开阶梯；不要原地换方向重试。"
                ),
            }

        stair_deadline = self.bot.deadline
        fallback_count = 0
        for step_idx in range(steps if stop_reason == "completed" else 0):
            # BUG-037: the first step often needs walk+dig+confirm and was
            # timing out at per_step_timeout with zero progress. Give step 0
            # the remaining total budget; later steps keep the per-step cap.
            self.bot.step_deadline = (
                self.bot.deadline
                if step_idx == 0 and self.bot.per_step_timeout >= 10.0
                else min(self.bot.deadline, time.monotonic() + self.bot.per_step_timeout)
            )
            # BUG-037/r24: step 0 may also stall 20~30s inside the server's dig
            # even with a tier>=2 pickaxe; let that first dig use the budget.
            self.bot.dig_cap = 40.0 if (step_idx == 0 and self.bot.per_step_timeout >= 10.0) else None
            if step_idx > 0 and time.monotonic() >= stair_deadline:
                stop_reason = "time_budget"
                break
            if step_idx > 0 and step_idx % 6 == 0:
                _equip_best_pick()

            st = self.bot.status()
            pos = st.get("pos") or pos_before
            bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
            nx, nz = bx + dx, bz + dz

            # BUG-041/r23: when a dig cell already timed out here, every retry
            # burns another 15~45s on the same unreachable block. Fail fast and
            # tell the model to relocate instead.
            avoided_cell = next(
                (
                    cell
                    for cell in (
                        (bx, by + 1, bz), (bx, by + 2, bz), (bx, by - 1, bz),
                        (nx, by + 1, nz), (nx, by + 2, nz),
                    )
                    if self._is_block_avoided(cell)
                    and (self.temporary_avoidance.get(cell) or {}).get("reason") == "dig_timeout"
                ),
                None,
            )
            if avoided_cell is not None and step_idx == 0:
                stop_reason = "dig_cell_avoided"
                last_error = {
                    "code": "dig_cell_avoided",
                    "pos": list(avoided_cell),
                    "message": "该格刚发生 dig 超时，已临时避开 30s，未再次挖掘。",
                }
                break

            head_here = _block_name([bx, by + 1, bz])
            if head_here in fluid_or_bedrock:
                blocker_info = {"pos": [bx, by + 1, bz], "block": head_here, "relation": "overhead"}
                stop_reason = "fluid_or_bedrock"
                break
            if head_here in undiggable_obstacles:
                blocker_info = {"pos": [bx, by + 1, bz], "block": head_here, "relation": "undiggable"}
                last_error = {"code": "undiggable_block", "block": head_here, "pos": [bx, by + 1, bz]}
                stop_reason = "undiggable_block"
                break
            if head_here not in air_set:
                adj_head = _find_adjacent_fluid([[bx, by + 1, bz]])
                if adj_head is not None:
                    blocker_info = adj_head
                    stop_reason = "fluid_or_bedrock"
                    break
                try:
                    self.bot.call("dig", {"pos": [bx, by + 1, bz], "collect": True, "stop_on": ["death"]})
                    dug_count += 1
                except ApiError as err:
                    last_error = {"code": err.code, "message": err.message, "pos": [bx, by + 1, bz]}
                    stop_reason = "dig_failed"
                    break
            if v_key == "up":
                apex_here = _block_name([bx, by + 2, bz])
                if apex_here in fluid_or_bedrock:
                    blocker_info = {"pos": [bx, by + 2, bz], "block": apex_here, "relation": "overhead_apex"}
                    stop_reason = "fluid_or_bedrock"
                    break
                if apex_here in undiggable_obstacles:
                    blocker_info = {"pos": [bx, by + 2, bz], "block": apex_here, "relation": "overhead_undiggable"}
                    last_error = {"code": "undiggable_block", "block": apex_here, "pos": [bx, by + 2, bz]}
                    stop_reason = "undiggable_block"
                    break
                if apex_here not in air_set:
                    adj_apex = _find_adjacent_fluid([[bx, by + 2, bz]])
                    if adj_apex is not None:
                        blocker_info = adj_apex
                        stop_reason = "fluid_or_bedrock"
                        break
                    try:
                        self.bot.call("dig", {"pos": [bx, by + 2, bz], "collect": True, "stop_on": ["death"]})
                        dug_count += 1
                    except ApiError as err:
                        last_error = {"code": err.code, "message": err.message, "pos": [bx, by + 2, bz]}
                        stop_reason = "dig_failed"
                        break

            if v_key == "down":
                safety_coords = [[nx, by + 2, nz], [nx, by + 1, nz], [nx, by, nz], [nx, by - 1, nz]]
                hit_safety = None
                for sc in safety_coords:
                    sb = _block_name(sc)
                    if sb in fluid_or_bedrock:
                        hit_safety = {"pos": sc, "block": sb, "relation": "in_path"}
                        break
                if hit_safety is not None:
                    blocker_info = hit_safety
                    stop_reason = "fluid_or_bedrock"
                    break
                support_blk = _block_name([nx, by - 2, nz])
                if support_blk in fluids:
                    blocker_info = {"pos": [nx, by - 2, nz], "block": support_blk, "relation": "floor_fluid"}
                    stop_reason = "fluid_or_bedrock"
                    break
                if _is_non_solid_support(support_blk):
                    blocker_info = {"pos": [nx, by - 2, nz], "block": support_blk, "relation": "missing_floor_support"}
                    stop_reason = "cave_or_drop_ahead"
                    break
                down_targets = [[nx, by + 1, nz], [nx, by, nz], [nx, by - 1, nz]]
                adj_down = _find_adjacent_fluid(down_targets, {})
                if adj_down is not None:
                    blocker_info = adj_down
                    stop_reason = "fluid_or_bedrock"
                    break

                dig_failed = False
                for idx, dpos in enumerate(down_targets):
                    dname = _block_name(dpos)
                    if dname in undiggable_obstacles:
                        blocker_info = {"pos": dpos, "block": dname, "relation": "undiggable"}
                        last_error = {"code": "undiggable_block", "block": dname, "pos": dpos}
                        dig_failed = True
                        break
                    if dname not in air_set:
                        try:
                            self.bot.call("dig", {"pos": dpos, "collect": True})
                            dug_count += 1
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": dpos}
                            dig_failed = True
                            break
                        if idx >= 1:
                            for _ in range(2):
                                remaining_block = _block_name(dpos)
                                if remaining_block in air_set:
                                    break
                                if remaining_block in fluid_or_bedrock:
                                    last_error = {"code": "unsafe_refilled_cell", "pos": dpos, "block": remaining_block}
                                    dig_failed = True
                                    break
                                try:
                                    self.bot.last_dig_block = _block_name(dpos)
                                    self.bot.call("dig", {"pos": dpos, "collect": True, "stop_on": ["death"]})
                                    dug_count += 1
                                except ApiError as err:
                                    last_error = {"code": err.code, "message": err.message, "pos": dpos}
                                    dig_failed = True
                                    break
                            if not dig_failed and _block_name(dpos) in {"sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel"}:
                                last_error = {"code": "stair_cell_not_cleared", "pos": dpos}
                                dig_failed = True
                            if dig_failed:
                                break
                if dig_failed:
                    stop_reason = (
                        "undiggable_block"
                        if last_error and last_error.get("code") == "undiggable_block"
                        else "dig_failed"
                    )
                    break

                cur = _advance_step(nx, by - 1, nz, bx, by, bz, jump=False)
                if math.floor(cur[0]) == bx and math.floor(cur[2]) == bz:
                    xs = sorted({math.floor(cur[0] - 0.3), math.floor(cur[0] + 0.3)})
                    zs = sorted({math.floor(cur[2] - 0.3), math.floor(cur[2] + 0.3)})
                    for hx in xs:
                        for hz in zs:
                            for hy in (by, by + 1):
                                hpos = [hx, hy, hz]
                                hb = _block_name(hpos)
                                if hb not in air_set and hb not in fluid_or_bedrock:
                                    try:
                                        self.bot.call("dig", {"pos": hpos, "collect": True, "stop_on": ["death"]})
                                        dug_count += 1
                                    except ApiError:
                                        pass
                    try:
                        mv2 = self.bot.call("move_to", {"x": nx + 0.5, "y": by - 1, "z": nz + 0.5, "range": 0.5})
                        cur = mv2.get("pos") or self.bot.status().get("pos") or cur
                    except ApiError:
                        cur = self.bot.status().get("pos") or cur
                    cur = _settled_position(cur)
                if math.floor(float(cur[1])) != by - 1 or math.dist([float(cur[0]), float(cur[2])], [nx + 0.5, nz + 0.5]) > 0.85:
                    stop_reason = "movement_not_confirmed"
                    break
            else:
                hit_up = None
                for sc in ([bx, by + 2, bz], [nx, by + 1, nz], [nx, by + 2, nz]):
                    sb = _block_name(sc)
                    if sb in fluid_or_bedrock:
                        hit_up = {"pos": sc, "block": sb, "relation": "in_path"}
                        break
                if hit_up is not None:
                    blocker_info = hit_up
                    stop_reason = "fluid_or_bedrock"
                    break
                for sc in ([bx, by + 3, bz], [nx, by + 3, nz]):
                    sb = _block_name(sc)
                    if sb in fluids:
                        hit_up = {"pos": sc, "block": sb, "relation": "overhead_fluid"}
                        break
                if hit_up is not None:
                    blocker_info = hit_up
                    stop_reason = "fluid_or_bedrock"
                    break
                up_targets = [[bx, by + 2, bz], [nx, by + 2, nz], [nx, by + 1, nz]]
                adj_up = _find_adjacent_fluid(up_targets, {})
                if adj_up is not None:
                    blocker_info = adj_up
                    stop_reason = "fluid_or_bedrock"
                    break

                column = (normalize_dimension(st.get("dimension")), bx, bz)
                pillar_count, pillar_expiry = self._pillar_fallbacks.get(column, (0, 0.0))
                face_cells = (
                    (nx, by - 1, nz), (nx + 1, by, nz), (nx - 1, by, nz),
                    (nx, by, nz + 1), (nx, by, nz - 1),
                )
                retry_after = math.ceil(pillar_expiry - time.monotonic())
                if (
                    pillar_count >= 2 and retry_after > 0
                    and _is_non_solid_support(_block_name([nx, by, nz]))
                    and not any(not _is_non_solid_support(_block_name(cell)) for cell in face_cells)
                ):
                    stop_reason = "pillar_fallback_cooldown"
                    last_error = {"code": stop_reason, "pos": [bx, by, bz], "retry_after_s": retry_after}
                    break

                dig_failed = False
                for dpos in up_targets:
                    uname = _block_name(dpos)
                    if uname in undiggable_obstacles:
                        blocker_info = {"pos": dpos, "block": uname, "relation": "undiggable"}
                        last_error = {"code": "undiggable_block", "block": uname, "pos": dpos}
                        dig_failed = True
                        break
                    if uname not in air_set:
                        try:
                            self.bot.last_dig_block = uname
                            self.bot.call("dig", {"pos": dpos, "collect": True})
                            dug_count += 1
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": dpos}
                            dig_failed = True
                            break
                if dig_failed:
                    stop_reason = (
                        "undiggable_block"
                        if last_error and last_error.get("code") == "undiggable_block"
                        else "dig_failed"
                    )
                    break

                if _is_non_solid_support(_block_name([nx, by, nz])):
                    # BUG-024: in a self-dug 1x1 shaft the step cell can have
                    # no solid face at all; place would always answer no_support.
                    # One safe pillar step is the only way to keep climbing.
                    has_place_face = any(
                        not _is_non_solid_support(_block_name(cell)) for cell in (*face_cells, (nx, by + 1, nz))
                    )
                    if not has_place_face:
                        now = time.monotonic()
                        for key, (_, expiry) in list(self._pillar_fallbacks.items()):
                            if expiry <= now:
                                del self._pillar_fallbacks[key]
                        fallback_count, expiry = self._pillar_fallbacks.get(column, (0, 0.0))
                        if fallback_count >= 2:
                            stop_reason = "pillar_fallback_cooldown"
                            last_error = {
                                "code": stop_reason,
                                "pos": [bx, by, bz],
                                "retry_after_s": math.ceil(expiry - now),
                            }
                            break
                        recovery = self._dig_pillar_up(steps=1, stop_on_ore=False)
                        if recovery.get("ok"):
                            steps_done += int(recovery.get("steps_done") or 0)
                            dug_count += int(recovery.get("dug_count") or 0)
                            placed_count += int(recovery.get("placed_count") or 0)
                            stop_reason = "pillar_fallback"
                            fallback_count += 1
                            self._pillar_fallbacks[column] = (fallback_count, time.monotonic() + 120.0)
                        else:
                            last_error = recovery.get("last_error") or {
                                "code": "no_place_face", "pos": [nx, by, nz],
                            }
                            stop_reason = "missing_step_support"
                        break
                    # The client inventory can disagree with the server's
                    # findItem (r13: cobblestone reported but place said none).
                    # Fall through to other solid fillers before giving up.
                    rejected_fillers: set[str] = set()
                    filler = _pick_step_filler()
                    placed_ok = False
                    place_failures = 0
                    while filler:
                        try:
                            self.bot.call("place", {"item": filler, "pos": [nx, by, nz]})
                            placed_count += 1
                            placed_ok = True
                            break
                        except ApiError as err:
                            last_error = {"code": err.code, "message": err.message, "pos": [nx, by, nz]}
                            if err.code == "place_failed" and place_failures < 2:
                                # r14: an up-step filler placement was refused
                                # once while the bot settled, then succeeded on
                                # a later attempt.
                                place_failures += 1
                                time.sleep(0.25)
                                continue
                            if err.code != "missing_item":
                                break
                            rejected_fillers.add(filler)
                            filler = _pick_step_filler(exclude=rejected_fillers)
                    if not placed_ok:
                        stop_reason = "missing_step_support"
                        break

                cur = _advance_step(nx, by + 1, nz, bx, by, bz, jump=True)
                if math.floor(float(cur[1])) != by + 1 or math.dist([float(cur[0]), float(cur[2])], [nx + 0.5, nz + 0.5]) > 0.85:
                    stop_reason = "movement_not_confirmed"
                    break

                steps_done += 1
                self.bot.progress.update(steps_done=steps_done, dug_count=dug_count)
                if step_idx >= 1:
                    surface_evidence = _confirm_surface(cur)
                    if surface_evidence:
                        stop_reason = "reached_surface"
                        break

                if stop_on_ore and steps_done % 3 == 0 and step_idx + 1 < steps:
                    matched_ores = _scan_actionable_ores(cur)
                    if matched_ores:
                        stop_reason = "ore_detected"
                        break
                continue

            steps_done += 1
            self.bot.progress.update(steps_done=steps_done, dug_count=dug_count)
            if stop_on_ore and steps_done % 3 == 0 and step_idx + 1 < steps:
                matched_ores = _scan_actionable_ores(cur)
                if matched_ores:
                    stop_reason = "ore_detected"
                    break

        self.bot.step_deadline = self.bot.deadline
        self.bot.dig_cap = None
        st_end = self.bot.status()
        if stop_on_ore and stop_reason == "completed":
            matched_ores = _scan_actionable_ores(st_end.get("pos") or pos_before)
            if matched_ores:
                stop_reason = "ore_detected"

        out_stair: dict[str, Any] = {
            "ok": stop_reason in {"completed", "ore_detected", "reached_surface", "pillar_fallback"} and (steps_done > 0 or stop_reason == "ore_detected"),
            "requires_replan": stop_reason not in {"completed", "ore_detected", "reached_surface", "pillar_fallback"},
            "direction": d_key,
            "vertical": v_key,
            "steps_requested": steps,
            "steps_done": steps_done,
            "dug_count": dug_count,
            "stop_reason": stop_reason,
            "pos_before": pos_before,
            "pos_after": st_end.get("pos"),
        }
        if matched_ores:
            out_stair["ores_detected"] = [
                {"name": b.get("name"), "pos": b.get("pos"), "dist": b.get("dist")}
                for b in matched_ores[:5]
            ]
            if stop_reason == "ore_detected":
                detected_names = list(dict.fromkeys(str(b.get("name")) for b in matched_ores if b.get("name")))
                if detected_names:
                    out_stair["hint"] = (
                        f"前方阶梯视野内探测到暴露矿脉 {', '.join(detected_names[:3])}；"
                        f"如需开采请调 mc_skill_mine_blocks(block_types={detected_names[:2]}, count=6)；"
                        f"若仅需继续沿 {d_key} 下探而不在此矿停驻，请传 stop_on_ore=False。"
                    )
        if placed_count > 0:
            out_stair["placed_count"] = placed_count
        if blocker_info is not None:
            out_stair["blocker"] = blocker_info
        if last_error is not None:
            out_stair["last_error"] = last_error
        if stop_reason == "pillar_fallback":
            out_stair["fallback"] = "pillar"
            if fallback_count >= 2:
                out_stair["requires_replan"] = True
                out_stair["hint"] = "同一站位已两次自动垫高。请用 mc_move_to 或 mc_skill_unstick 换到有支撑的站位，再规划阶梯。"
            else:
                out_stair["hint"] = "目标台阶没有可放置面，已垫高一格。请从新高度检查有支撑的横向路线。"
        # BUG-099: prevent stone-pickaxe iron runs from sliding past Y=16 into deepslate.
        end_pos = st_end.get("pos") or pos_before
        _stair_pick_tier = getattr(self.bot, "pickaxe_tier", None)
        if (
            v_key == "down"
            and isinstance(end_pos, (list, tuple))
            and len(end_pos) >= 2
            and float(end_pos[1]) <= 16.0
            and isinstance(_stair_pick_tier, int)
            and _stair_pick_tier < 3
            and "hint" not in out_stair
        ):
            out_stair["hint"] = (
                f"当前已达 Y={int(math.floor(float(end_pos[1])))}（铁层深度）且尚无铁镐；"
                "严禁继续 staircase(down) 滑入深板岩，请立即改调 mc_skill_mine_dfs(action='explore', target='iron', target_y=16, goal_count=8) 水平鱼骨采铁。"
            )
        if stop_reason == "dig_cell_avoided":
            out_stair["hint"] = (
                "该格刚发生 dig 超时（同一格已临时避开 30s），本次未再挖掘。"
                "请先用 mc_move_to 水平离开 8~12 格或 mc_skill_unstick 换站位，"
                "再换方向重新开阶梯；不要重复同一坐标/方向。"
            )
        end_pos = st_end.get("pos") or pos_before
        ebx, eby, ebz = math.floor(float(end_pos[0])), math.floor(float(end_pos[1])), math.floor(float(end_pos[2]))
        if stop_reason == "pillar_fallback_cooldown":
            from ..combat_env import CombatEnv

            retry_after = last_error["retry_after_s"]
            reason = f"当前列缺少台阶放置面，自动垫高还需冷却约 {retry_after} 秒。"
            recovery = "请沿已知的实心通道换站位，或调用 mc_skill_unstick 检查安全落脚点。"
            try:
                up_dirs = self._probe_viable_up_stair_dirs(
                    ebx, eby, ebz, exclude_dirs={d_key}, require_filler_face=True,
                )
                if self.geofence:
                    up_dirs = [
                        direction for direction in up_dirs
                        if self.geofence.check_ray(end_pos[0], end_pos[2], direction, 1, action="mc_skill_staircase") is None
                    ]
                out_stair["viable_up_directions"] = up_dirs
                if up_dirs:
                    recovery = f"可先调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=1)。"
                else:
                    terrain = CombatEnv(self.bot, geofence=self.geofence)
                    for ox, oz in dirs.values():
                        x, z = ebx + ox + 0.5, ebz + oz + 0.5
                        yaw = _yaw_toward(end_pos[0], end_pos[2], x, z)
                        route = terrain.probe_retreat(end_pos, yaw, math.hypot(x - end_pos[0], z - end_pos[2]))
                        if route is not None:
                            y, _ = route
                            out_stair["recovery_move"] = {"x": x, "y": y, "z": z, "range": 0.3}
                            recovery = f"可先调 mc_move_to(x={x}, y={y}, z={z}, range=0.3) 转移到有支撑的临近落脚点。"
                            break
            except (ApiError, _StairDeadline):
                pass
            out_stair["message"] = out_stair["hint"] = reason + recovery
            out_stair["last_error"]["message"] = out_stair["message"]
        if stop_reason in {"cave_or_drop_ahead", "fluid_or_bedrock", "dig_failed", "undiggable_block", "movement_not_confirmed", "missing_step_support"}:
            reason_label = (
                f"{stop_reason}:{last_error['code']}"
                if last_error and last_error.get("code")
                else (f"{stop_reason}:{blocker_info['block']}" if blocker_info else stop_reason)
            )
            self._record_failed_stair_dir((ebx, eby, ebz, v_key), d_key, reason_label)
            if steps_done > 0 and dug_count == 0 and (ebx, eby, ebz) != (b0x, b0y, b0z):
                self._record_failed_stair_dir((b0x, b0y, b0z, v_key), d_key, f"dead_end->{reason_label}")

        if v_key == "down" and stop_reason in {"cave_or_drop_ahead", "fluid_or_bedrock", "on_tree_canopy", "dig_failed", "undiggable_block", "movement_not_confirmed"}:
            probe_cache: dict[tuple[int, int, int], str] = {}

            def _probe_block(px: int, py: int, pz: int) -> str:
                pt = (px, py, pz)
                if pt not in probe_cache:
                    probe_cache[pt] = _block_name([px, py, pz])
                return probe_cache[pt]

            viable_dirs: list[str] = []
            direction_status: dict[str, str] = {}
            if stop_reason == "on_tree_canopy":
                for odir in dirs:
                    direction_status[odir] = "blocked(on_tree_canopy)"
            else:
                failed_dirs = self._active_failed_stair_dirs((ebx, eby, ebz, "down"))
                for odir, (odx, odz) in dirs.items():
                    if odir == d_key:
                        if blocker_info:
                            direction_status[odir] = (
                                f"blocked({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
                            )
                        elif last_error:
                            direction_status[odir] = (
                                f"failed({stop_reason}:{last_error.get('code')}@{last_error.get('pos')})"
                            )
                        else:
                            direction_status[odir] = f"failed({stop_reason})"
                        continue
                    if odir in failed_dirs:
                        direction_status[odir] = f"failed_recently({failed_dirs[odir]})"
                        continue
                    try:
                        support_blk = _probe_block(ebx + odx, eby - 2, ebz + odz)
                        if support_blk in fluid_or_bedrock:
                            direction_status[odir] = f"blocked(floor:{support_blk}@[{ebx + odx},{eby - 2},{ebz + odz}])"
                            continue
                        if _is_non_solid_support(support_blk):
                            direction_status[odir] = f"blocked(cave_drop:{support_blk}@[{ebx + odx},{eby - 2},{ebz + odz}])"
                            continue
                        hit_path = None
                        for ky in (2, 1, 0, -1):
                            fb = _probe_block(ebx + odx, eby + ky, ebz + odz)
                            if fb in fluid_or_bedrock:
                                hit_path = f"blocked(in_path:{fb}@[{ebx + odx},{eby + ky},{ebz + odz}])"
                                break
                        if hit_path is not None:
                            direction_status[odir] = hit_path
                            continue
                        adj = _find_adjacent_fluid(
                            [[ebx + odx, eby + 1, ebz + odz], [ebx + odx, eby, ebz + odz], [ebx + odx, eby - 1, ebz + odz]],
                            probe_cache,
                        )
                        if adj is not None:
                            direction_status[odir] = f"blocked(adjacent_fluid:{adj['block']}@{adj['pos']})"
                            continue
                        direction_status[odir] = "viable"
                        viable_dirs.append(odir)
                    except ApiError:
                        direction_status[odir] = "unknown(probe_error)"
                        continue
            out_stair["direction_status"] = direction_status
            out_stair["viable_directions"] = viable_dirs
            if blocker_info:
                detail_desc = f"{stop_reason}({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
            elif last_error and last_error.get("code"):
                detail_desc = f"{stop_reason}({last_error['code']}@{last_error.get('pos')})"
            else:
                detail_desc = stop_reason
            if viable_dirs:
                out_stair["hint"] = (
                    f"前方({d_key})遇到{detail_desc}，禁止继续朝{d_key}重试；"
                    f"实测可换向安全下探: {','.join(viable_dirs)}。"
                )
            else:
                out_stair["all_directions_blocked"] = True
                try:
                    roof_blk = _probe_block(ebx, eby + 2, ebz)
                except ApiError:
                    roof_blk = "air"
                is_underground = (eby < 62) or (roof_blk not in air_set and not is_passable_sight_block(roof_blk))
                if is_underground:
                    inv_now = _inventory_counts(self.bot)
                    tunnel_dirs = self._probe_viable_tunnel_dirs(
                        ebx,
                        eby,
                        ebz,
                        probe_cache=probe_cache,
                        exclude_dirs={d_key} if stop_reason in {"dig_failed", "undiggable_block"} else None,
                        inv_counts=inv_now,
                    )
                    up_dirs = self._probe_viable_up_stair_dirs(
                        ebx,
                        eby,
                        ebz,
                        probe_cache=probe_cache,
                        inv_counts=inv_now,
                    )
                    out_stair["viable_tunnel_directions"] = tunnel_dirs
                    out_stair["viable_up_directions"] = up_dirs
                    if tunnel_dirs:
                        up_hint = (
                            f"mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6)"
                            if up_dirs
                            else "mc_skill_staircase(vertical='up', steps=4)"
                        )
                        out_stair["hint"] = (
                            f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面均无法继续阶梯下探(viable_directions=[])，"
                            f"严禁在当前位置换方向继续调 mc_skill_staircase！请改调 mc_skill_tunnel(direction='{tunnel_dirs[0]}', steps=6) "
                            f"水平开矿道转移后再下探，或调 {up_hint} 沿原矿道退回上层再分支下探！"
                        )
                    elif up_dirs:
                        out_stair["hint"] = (
                            f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面下探与水平通道均已堵死"
                            "(viable_directions=[], viable_tunnel_directions=[])，严禁在原地重试下探或调用 mc_skill_tunnel！"
                            f"请改调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6) 沿安全方向向上退回上层(可选上行方向: {','.join(up_dirs)})！"
                        )
                    else:
                        vpillar = self._probe_viable_pillar_up(
                            ebx,
                            eby,
                            ebz,
                            probe_cache=probe_cache,
                            inv_counts=inv_now,
                        )
                        out_stair["viable_pillar_up"] = vpillar
                        if vpillar:
                            out_stair["hint"] = (
                                f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面下探、水平通道与斜向阶梯上行均受阻"
                                "(viable_directions=[], viable_tunnel_directions=[], viable_up_directions=[])，"
                                "但头顶正上方安全且有垫脚方块(viable_pillar_up=true)，请改调 mc_skill_staircase(vertical='pillar', steps=4) 原地垂直挖顶垫脚直上脱困！"
                            )
                        else:
                            out_stair["hint"] = (
                                f"前方({d_key})遇到{detail_desc}；当前地下坐标[{ebx},{eby},{ebz}]四面下探、水平通道与常规向上阶梯均受阻"
                                "(viable_directions=[], viable_tunnel_directions=[], viable_up_directions=[], viable_pillar_up=false)，"
                                "严禁继续原地循环调用 mc_skill_staircase 或 mc_skill_tunnel！请先调 mc_move_to 沿已挖矿道走出死胡同，或用 mc_get_block 确认安全后以 mc_dig / mc_place 垫脚向上开路脱困！"
                            )
                else:
                    out_stair["hint"] = (
                        f"前方({d_key})遇到{detail_desc}；当前坐标[{ebx},{eby},{ebz}]四面下方均为空洞/悬崖/树冠(viable_directions=[])，原地4个方向全部无法阶梯下探！"
                        "严禁在当前位置换方向继续调 mc_skill_staircase！请先调 mc_skill_unstick 下树/脱困，或调 mc_move_to(只传x,z省略y) 水平走开 12-20 格到平坦实心陆地后再下探！"
                    )
        elif v_key == "up" and stop_reason in {"fluid_or_bedrock", "dig_failed", "undiggable_block", "movement_not_confirmed", "missing_step_support"}:
            probe_cache_up: dict[tuple[int, int, int], str] = {}
            up_inv = _inventory_counts(self.bot)
            up_dirs = self._probe_viable_up_stair_dirs(
                ebx,
                eby,
                ebz,
                probe_cache=probe_cache_up,
                exclude_dirs={d_key},
                inv_counts=up_inv,
            )
            tunnel_dirs = self._probe_viable_tunnel_dirs(
                ebx,
                eby,
                ebz,
                probe_cache=probe_cache_up,
                inv_counts=up_inv,
            )
            out_stair["viable_up_directions"] = up_dirs
            out_stair["viable_tunnel_directions"] = tunnel_dirs
            detail_up = (
                f"{stop_reason}({blocker_info['relation']}:{blocker_info['block']}@{blocker_info['pos']})"
                if blocker_info
                else (
                    f"{stop_reason}({last_error['code']}@{last_error.get('pos')})"
                    if last_error and last_error.get("code")
                    else stop_reason
                )
            )
            if up_dirs:
                out_stair["hint"] = (
                    f"向上({d_key})遇到{detail_up}，禁止继续朝{d_key}上挖；实测可行向上阶梯方向: {','.join(up_dirs)}"
                    f"（请调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=6)）"
                    + (f"，或平推方向: {','.join(tunnel_dirs)}。" if tunnel_dirs else "。")
                )
            elif tunnel_dirs:
                out_stair["hint"] = (
                    f"向上({d_key})遇到{detail_up}，当前位置无安全上挖方向(viable_up_directions=[])；"
                    f"实测可平推绕避方向: {','.join(tunnel_dirs)}（请改调 mc_skill_tunnel(direction='{tunnel_dirs[0]}', steps=6) 后再上探）。"
                )
            else:
                vpillar = self._probe_viable_pillar_up(
                    ebx,
                    eby,
                    ebz,
                    probe_cache=probe_cache_up,
                    inv_counts=up_inv,
                )
                out_stair["viable_pillar_up"] = vpillar
                if vpillar:
                    out_stair["hint"] = (
                        f"向上({d_key})遇到{detail_up}，当前坐标[{ebx},{eby},{ebz}]斜向上挖与水平通道均受阻"
                        "(viable_up_directions=[], viable_tunnel_directions=[])，但头顶正上方安全且有垫脚方块(viable_pillar_up=true)，"
                        "请改调 mc_skill_staircase(vertical='pillar', steps=4) 原地垂直挖顶垫脚直上脱困！"
                    )
                else:
                    out_stair["hint"] = (
                        f"向上({d_key})遇到{detail_up}，当前坐标[{ebx},{eby},{ebz}]上挖与水平通道均受阻"
                        "(viable_up_directions=[], viable_tunnel_directions=[], viable_pillar_up=false)，禁止继续重复调用 mc_skill_staircase/mc_skill_tunnel；"
                        "请先调 mc_move_to 沿既有矿道走出死胡同，或用 mc_get_block 确认安全后以 mc_dig / mc_place 脱困。"
                    )
        elif stop_reason in {"cave_or_drop_ahead", "fluid_or_bedrock", "on_tree_canopy", "undiggable_block"}:
            out_stair["hint"] = f"前方({d_key})遇到{stop_reason}，禁止原地继续朝{d_key}重试，请换其他方向！"
        if surface_evidence is not None:
            out_stair["surface_evidence"] = surface_evidence
        out_stair["ores_nearby"] = matched_ores
        return out_stair


    def _try_seal_shaft_threat(
        self,
        pos0: list[float] | tuple[float, ...],
        enclosure: dict[str, Any],
        threat_pos: list[float] | tuple[float, ...] | None,
        counts: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Inside a carved shaft, seal a 1-wide open corridor facing an active threat instead of fleeing outside."""
        if enclosure.get("structure") != "carved_shaft":
            return None
        if not isinstance(threat_pos, (list, tuple)) or len(threat_pos) < 3:
            return None
        inv_counts = counts if counts is not None else _inventory_counts(self.bot)
        filler = next((b for b in SHAFT_SEAL_BLOCKS if inv_counts.get(b, 0) >= 2), None)
        if filler is None:
            return None

        t_dir = cardinal_bearing(float(threat_pos[0]) - float(pos0[0]), float(threat_pos[2]) - float(pos0[2]))
        open_dirs = enclosure.get("open_dirs") or []
        if t_dir not in open_dirs:
            return None

        dir_vecs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        sdx, sdz = dir_vecs[t_dir]
        px, pz = float(pos0[0]), float(pos0[2])
        h_dist = math.hypot(float(threat_pos[0]) - px, float(threat_pos[2]) - pz)
        pbx, pbz = math.floor(px), math.floor(pz)
        target_col = (pbx + sdx, pbz + sdz)
        overlaps_target = abs(px - (target_col[0] + 0.5)) < 0.75 and abs(pz - (target_col[1] + 0.5)) < 0.75

        if h_dist < 2.1 or overlaps_target:
            opp_vec = (-sdx, -sdz)
            retreat_dir = next((d for d in open_dirs if d != t_dir), None)
            if retreat_dir is not None:
                rdx, rdz = dir_vecs[retreat_dir]
            else:
                rdx, rdz = opp_vec
            try:
                self.bot.call(
                    "control",
                    {
                        "forward": True,
                        "sprint": True,
                        "jump": retreat_dir in (enclosure.get("up_open_dirs") or []),
                        "yaw": _yaw_toward(px, pz, px + rdx * 5.0, pz + rdz * 5.0),
                        "ms": 550,
                        "stop_on": ["death"],
                    },
                )
            except ApiError:
                pass

        st_cur = self.bot.status()
        if st_cur.get("alive") is False:
            return None
        cpos = st_cur.get("pos") or pos0
        cx, cy, cz = float(cpos[0]), float(cpos[1]), float(cpos[2])
        bx, by, bz = math.floor(cx), math.floor(cy), math.floor(cz)
        tx, tz = bx + sdx, bz + sdz
        if abs(cx - (tx + 0.5)) < 0.75 and abs(cz - (tz + 0.5)) < 0.75:
            return None

        b0 = _cached_block_name(self.bot, tx, by, tz, None)
        b1 = _cached_block_name(self.bot, tx, by + 1, tz, None)
        if b0 in PASSABLE_WALK_BLOCKS:
            targets = [[tx, by, tz], [tx, by + 1, tz]]
        elif b1 in PASSABLE_WALK_BLOCKS:
            targets = [[tx, by + 1, tz], [tx, by + 2, tz]]
        else:
            return None

        for cell in targets:
            cur_blk = _cached_block_name(self.bot, cell[0], cell[1], cell[2], None)
            if cur_blk != "unknown" and cur_blk not in PASSABLE_WALK_BLOCKS:
                continue
            try:
                self.bot.call("place", {"item": filler, "pos": cell})
            except ApiError:
                pass

        for cell in targets:
            fresh_blk = _cached_block_name(self.bot, cell[0], cell[1], cell[2], None)
            if fresh_blk == "unknown" or fresh_blk in PASSABLE_WALK_BLOCKS:
                return None

        return {
            "ok": True,
            "mode": "seal_shaft",
            "structure": "carved_shaft",
            "sealed_direction": t_dir,
            "filler": filler,
            "placed_positions": targets,
            "moved_dist": round(math.hypot(cx - float(pos0[0]), cz - float(pos0[2])), 1),
            "pos_before": [round(float(pos0[0]), 1), round(float(pos0[1]), 1), round(float(pos0[2]), 1)],
            "pos_after": cpos,
            "requires_replan": True,
            "note": f"已用 {filler} 封堵探矿穴 {t_dir} 侧来敌通道，保留当前探矿穴无需返回地表。",
        }

