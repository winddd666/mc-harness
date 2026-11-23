"""MacroSkills mixin: mining_container (split from legacy library.py)."""

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


class _MiningContainerMixin:
    def _mine_blocks(
        self, block_types: list[str], count: int, radius: int, *, skip_unmineable: bool, defer_cleanup: bool = False,
    ) -> dict[str, Any]:
        if not block_types:
            return {"ok": False, "error": "bad_args", "message": "block_types 不能为空。"}
        # Natural terrain has `stone`/`deepslate` rather than `cobblestone`; auto-expand so cobblestone requests work!
        block_types = list(block_types)
        if "cobblestone" in block_types and "stone" not in block_types:
            block_types.append("stone")
        count = max(1, min(32, int(count)))
        radius = max(4, min(64, int(radius)))
        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        best_pick, best_tier = _best_pickaxe_in_inv(counts)
        allowed = [name for name in block_types if BLOCK_MIN_PICKAXE_TIER.get(name, 0) <= best_tier]
        blocked = {name: BLOCK_MIN_PICKAXE_TIER[name] for name in block_types if name not in allowed}
        if blocked and (not allowed or not skip_unmineable):
            req_tier = max(blocked.values())
            tier_names = {1: "wooden_pickaxe(木镐)", 2: "stone_pickaxe(石镐)", 3: "iron_pickaxe(铁镐)", 4: "diamond_pickaxe(钻石镐)"}
            allowed_hint = f"当前允许 {allowed}，可单独请求。" if allowed else ""
            return {
                "ok": False,
                "error": "tool_tier_insufficient",
                "message": f"当前最高 {best_pick or '无镐'}，请求的目标 {list(blocked)} 至少需要 {tier_names.get(req_tier, req_tier)}。{allowed_hint}本次没有执行挖掘。",
                "required_tier": req_tier,
                "current_pickaxe": best_pick,
                "allowed_types": allowed,
                "blocked_types": blocked,
            }
        block_types = allowed
        skipped = {"skipped_unmineable": list(blocked)} if blocked else {}
        if isinstance(self.bot, _SkillBudgetSession):
            self.bot.progress.update(skipped)

        st = self.bot.status()
        raw_yaw = st.get("yaw")
        away_yaw = (float(raw_yaw) + math.pi) if isinstance(raw_yaw, (int, float)) else None
        inv_raw, auto_dropped = self._ensure_free_slots(
            min_free=8,
            inv_snapshot=inv_raw,
            turn_away_yaw=away_yaw,
        )
        if auto_dropped:
            counts = _inventory_counts_from_raw(inv_raw)
            best_pick, best_tier = _best_pickaxe_in_inv(counts)
        auto_dropped_names = frozenset(
            str(d["item"]).removeprefix("minecraft:")
            for d in auto_dropped
            if isinstance(d, dict) and d.get("item")
        )

        if bool(st.get("in_water")):
            esc = self.escape_water()
            if esc.get("in_water"):
                return {
                    "ok": False,
                    "error": "in_water",
                    "message": "角色当前仍在水中，水下挖掘极慢且易溺水中断；请先脱离水体登上干燥地面再挖矿。",
                    "water_escape": esc,
                    **skipped,
                }
            counts = _inventory_counts(self.bot)
            best_pick, _ = _best_pickaxe_in_inv(counts)

        if best_pick:
            try:
                self.bot.call("equip", {"item": best_pick, "slot": "hand"})
            except ApiError:
                pass

        res = self._dig_target_blocks(
            block_types=block_types,
            count=count,
            radius=radius,
            protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
            below_tol=1.5,
            above_tol=2.5,
            inv_before=counts,
            avoid_water=True,
            auto_dropped_names=auto_dropped_names or None,
        )
        if res.get("error") == "protected_region" or res.get("stop_reason") in {"time_budget", "unreachable_targets", "pickup_interrupted"}:
            return {**res, **skipped}

        if (
            int(res.get("dug_count") or 0) == 0
            and res.get("error") == "no_targets_available"
            and not self.temporary_avoidance
        ):
            alt_types = list(
                dict.fromkeys(
                    alt
                    for b in block_types
                    if (alt := ORE_COUNTERPARTS.get(b))
                    and alt not in block_types
                    and BLOCK_MIN_PICKAXE_TIER.get(alt, 0) <= best_tier
                )
            )
            if alt_types:
                alt_res = self._dig_target_blocks(
                    block_types=alt_types,
                    count=count,
                    radius=radius,
                    protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
                    below_tol=1.5,
                    above_tol=2.5,
                    inv_before=counts,
                    avoid_water=True,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if int(alt_res.get("dug_count") or 0) > 0 or alt_res.get("error") != "no_targets_available":
                    res = {**alt_res, "auto_paired_types": alt_types}

        # 若近距（<=10m、同层|dy|<=2）有暴露矿石但因隔墙/寻路失败暂避导致 0 挖掘，先水平破墙再清障重试一次
        has_reach_failure = any(
            rec.get("reason") in {"path_not_found", "out_of_reach"}
            for rec in self.temporary_avoidance.values()
        )
        effective_types = list(res.get("auto_paired_types") or block_types)
        if int(res.get("dug_count") or 0) == 0 and has_reach_failure:
            tunnel_attempt = self._approach_occluded_ore(block_types=effective_types, radius=min(12, radius))
            if tunnel_attempt is not None:
                if int(tunnel_attempt.get("steps_done") or 0) > 0 or tunnel_attempt.get("stop_reason") in {
                    "ore_at_face",
                    "ore_detected",
                }:
                    for pos_k in [
                        k for k, v in self.temporary_avoidance.items()
                        if v.get("reason") in {"path_not_found", "out_of_reach"}
                    ]:
                        del self.temporary_avoidance[pos_k]
                res2 = self._dig_target_blocks(
                    block_types=effective_types,
                    count=count,
                    radius=radius,
                    protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
                    below_tol=1.5,
                    above_tol=2.5,
                    inv_before=counts,
                    avoid_water=True,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if int(res2.get("dug_count") or 0) > 0:
                    return {
                        **res2,
                        **skipped,
                        "pickaxe_used": best_pick,
                        "occluded_tunnel": tunnel_attempt,
                    }
                res["occluded_tunnel"] = tunnel_attempt

        if int(res.get("dug_count") or 0) == 0 and self.temporary_avoidance:
            reach_entries = [
                (pos_k, rec)
                for pos_k, rec in self.temporary_avoidance.items()
                if rec.get("reason") in {"out_of_reach", "path_not_found", "no_path"}
            ]
            if reach_entries:
                try:
                    cur_p = self.bot.status().get("pos") or [0.0, 64.0, 0.0]
                    cx, cy, cz = float(cur_p[0]), float(cur_p[1]), float(cur_p[2])
                    tx, ty, tz = min(
                        (pos_k for pos_k, _ in reach_entries),
                        key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2,
                    )
                    dx, dy, dz = tx - cx, float(ty) - cy, tz - cz
                    bearing = cardinal_bearing(dx, dz)
                    if dy <= -3.0:
                        approach_hint = (
                            f"最近不可达矿石位于[{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})，"
                            f"请朝 {bearing} 调 mc_skill_staircase(direction='{bearing}', vertical='down', steps={min(10, max(3, int(abs(dy) + 1)))}, stop_on_ore=True) 下探接近后再挖！"
                        )
                    elif dy >= 3.0:
                        approach_hint = (
                            f"最近不可达矿石位于[{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})，"
                            f"请朝 {bearing} 调 mc_skill_staircase(direction='{bearing}', vertical='up', steps={min(10, max(3, int(abs(dy) + 1)))}, stop_on_ore=True) 上行接近后再挖！"
                        )
                    else:
                        approach_hint = (
                            f"最近不可达矿石位于[{tx},{ty},{tz}](相对朝向={bearing}, dy={dy:+.1f})，"
                            f"请朝 {bearing} 调 mc_skill_tunnel(direction='{bearing}', steps={min(8, max(2, int(max(abs(dx), abs(dz)) + 1)))}, stop_on_ore=True) 开矿道接近后再挖！"
                        )
                    res["nearest_unreachable_target"] = {
                        "pos": [tx, ty, tz],
                        "bearing": bearing,
                        "dy": round(dy, 1),
                    }
                    res["approach_hint"] = approach_hint
                except Exception:
                    pass

        if (
            int(res.get("dug_count") or 0) == 0
            and res.get("error") == "no_targets_available"
            and "nearest_unreachable_target" not in res
            and self.world_memory is not None
        ):
            from ...harness.world_memory import is_tracked_poi_block

            mem_targets = [b for b in block_types if is_tracked_poi_block(b)]
            if mem_targets:
                mem_recall = self._recall_and_mine_memory_poi(
                    mem_targets=mem_targets,
                    count=count,
                    radius=radius,
                    best_pick=best_pick,
                    inv_before=counts,
                    auto_dropped_names=auto_dropped_names or None,
                )
                if mem_recall is not None:
                    if auto_dropped:
                        mem_recall["auto_dropped"] = auto_dropped
                    return {**mem_recall, **skipped}

        if (
            int(res.get("dug_count") or 0) == 0
            and res.get("error") == "no_targets_available"
            and "nearest_unreachable_target" not in res
        ):
            enriched = self._enrich_ecology_unavailable(res, targets=block_types, radius=radius, counts=counts)
            if enriched is not None:
                res = enriched

        out_mine: dict[str, Any] = {
            **res,
            **skipped,
            "pickaxe_used": best_pick,
        }
        if auto_dropped:
            out_mine["auto_dropped"] = auto_dropped
        # BUG-029: a long mining slice can leave hundreds of low-value blocks
        # even when several slots are still empty; enforce retention caps here.
        if not defer_cleanup:
            try:
                _, cleaned = self._ensure_free_slots(min_free=3)
            except Exception:
                cleaned = []
            if cleaned:
                out_mine["inventory_cleaned"] = cleaned
        return out_mine


    def _recall_and_mine_memory_poi(
        self,
        *,
        mem_targets: list[str],
        count: int,
        radius: int,
        best_pick: str | None,
        inv_before: dict[str, int],
        auto_dropped_names: frozenset[str] | None,
    ) -> dict[str, Any] | None:
        """Recall nearest known non-ore POI from WorldMemory, relay-navigate, lazily prune/cool bad points, and mine."""
        wm = self.world_memory
        if wm is None:
            return None

        def _probe_live(pt: tuple[int, int, int]) -> str | None:
            try:
                b = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
                return str(b.get("name") or b.get("block") or "air").removeprefix("minecraft:")
            except ApiError:
                return None

        target_clean_set = {str(t).removeprefix("minecraft:").strip().lower() for t in mem_targets}
        recalled_log: list[dict[str, Any]] = []

        for _attempt in range(4):
            try:
                st_now = self.bot.status()
            except ApiError:
                break
            cur_pos = st_now.get("pos") or [0.0, 64.0, 0.0]
            dim_now = normalize_dimension(st_now.get("dimension"))
            hits = wm.query_pois(
                mem_targets,
                origin=cur_pos,
                dim=dim_now,
                limit=1,
                verify_fn=_probe_live,
                verify_radius=32.0,
            )
            if not hits:
                break
            cand = hits[0]
            cpos = cand["pos"]
            dist_c = float(cand["dist"])

            if dist_c > 12.0:
                try:
                    self.navigate_segmented(float(cpos[0]) + 0.5, float(cpos[2]) + 0.5, range_m=2.5)
                except ApiError as nav_err:
                    if nav_err.code in {"hurt", "death", "interrupted"}:
                        raise
                    wm.mark_unreachable(cpos, dim=dim_now, reason=nav_err.code, cooldown_s=300.0)
                    recalled_log.append({"pos": cpos, "status": "cooled_unreachable", "reason": nav_err.code})
                    continue

            live_after_nav = _probe_live((int(cpos[0]), int(cpos[1]), int(cpos[2])))
            if live_after_nav is not None and live_after_nav.lower() not in target_clean_set:
                wm.remove_poi(cpos, dim=dim_now)
                recalled_log.append({"pos": cpos, "status": "removed_stale", "actual": live_after_nav})
                continue

            if best_pick:
                try:
                    self.bot.call("equip", {"item": best_pick, "slot": "hand"})
                except ApiError:
                    pass

            dig_res = self._dig_target_blocks(
                block_types=mem_targets,
                count=count,
                radius=max(16, min(32, radius)),
                protected_msg="目标方块处于保护区，请向远离出生点移动后再挖掘。",
                below_tol=2.5,
                above_tol=3.0,
                inv_before=inv_before,
                avoid_water=True,
                auto_dropped_names=auto_dropped_names,
            )
            if int(dig_res.get("dug_count") or 0) > 0 and dig_res.get("ok"):
                wm.remove_poi(cpos, dim=dim_now)
                return {
                    **dig_res,
                    "pickaxe_used": best_pick,
                    "recalled_from_memory": cand,
                    "memory_attempts": recalled_log,
                }

            # Reached vicinity, block still exists, yet digging could not reach it (e.g. walled off by bedrock) -> cool down 300s!
            wm.mark_unreachable(cpos, dim=dim_now, reason=str(dig_res.get("error") or "unreachable"), cooldown_s=300.0)
            recalled_log.append({"pos": cpos, "status": "cooled_dig_blocked", "reason": dig_res.get("error")})

        return None


    def _approach_occluded_ore(self, *, block_types: list[str], radius: int = 12) -> dict[str, Any] | None:
        """当同层近距目标被石墙隔开时，朝主水平轴短距开矿道接近。"""
        try:
            st = self.bot.status()
            if st.get("in_water"):
                return None
            pos = st.get("pos") or [0.0, 64.0, 0.0]
            bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))
            found = self.bot.call("find_blocks", {"radius": max(4, radius), "types": block_types, "limit": 8})
        except Exception:
            return None
        cands = []
        for b in found.get("blocks") or []:
            if not isinstance(b, dict) or not isinstance(b.get("pos"), list) or len(b["pos"]) < 3:
                continue
            pos_t = (int(b["pos"][0]), int(b["pos"][1]), int(b["pos"][2]))
            if pos_t in self.protected_positions:
                continue
            avoid = self.temporary_avoidance.get(pos_t)
            if avoid is not None and avoid.get("reason") not in {"path_not_found", "out_of_reach"}:
                continue
            if abs(pos_t[1] - by) <= 2 and float(b.get("dist", 99.0)) <= float(radius):
                cands.append(b)
        if not cands:
            return None
        nearest = min(cands, key=lambda b: float(b.get("dist", 99.0)))
        tx, _ty, tz = int(nearest["pos"][0]), int(nearest["pos"][1]), int(nearest["pos"][2])
        dx, dz = tx - bx, tz - bz
        if abs(dx) + abs(dz) <= 1:
            return None
        if abs(dx) >= abs(dz):
            direction = "east" if dx > 0 else "west"
            steps = min(4, max(1, abs(dx) - 1))
        else:
            direction = "south" if dz > 0 else "north"
            steps = min(4, max(1, abs(dz) - 1))
        try:
            return self.dig_level_tunnel(direction=direction, steps=steps, stop_on_ore=True)
        except Exception:
            return None


    def _window_target(
        self,
        *,
        block: str | None = None,
        entity_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any] | None:
        if entity_id is not None:
            return {"entity_id": int(entity_id)}
        if x is not None and y is not None and z is not None:
            return {"pos": [int(x), int(y), int(z)]}
        if block:
            return {"block": str(block).removeprefix("minecraft:")}
        return None


    def _current_dim(self, dim: str | None = None) -> str:
        if dim:
            return normalize_dimension(dim)
        try:
            return normalize_dimension(self.bot.status().get("dimension"))
        except ApiError:
            return "overworld"


    def _resolve_nearby_container_pos(self, block: str | None = None, *, radius: int = 16) -> list[int] | None:
        names = [str(block).removeprefix("minecraft:")] if block else list(_CONTAINER_TYPES)
        try:
            found = self.bot.call("find_blocks", {"radius": max(4, min(48, int(radius))), "types": names, "limit": 8})
        except ApiError:
            return None
        blocks = [b for b in (found.get("blocks") or []) if isinstance(b, dict) and isinstance(b.get("pos"), list)]
        if not blocks:
            return None
        nearest = min(blocks, key=lambda b: float(b.get("dist") or 99))
        return [int(v) for v in nearest["pos"][:3]]


    def _container_block_at(self, pos: list[int]) -> str | None:
        try:
            res = self.bot.call("get_block", {"pos": [int(pos[0]), int(pos[1]), int(pos[2])]})
        except ApiError:
            return None
        return str(res.get("name") or res.get("block") or "").removeprefix("minecraft:").strip().lower()


    def _sync_container_window(
        self,
        payload: dict[str, Any] | None,
        *,
        pos: list[int] | None = None,
        block: str | None = None,
        dim: str | None = None,
    ) -> bool:
        """Overwrite `WorldMemory.containers` from any payload carrying a `window` snapshot.

        An empty `sections.container: []` must clear the cached phantom contents (`{}`).
        """
        if not isinstance(payload, dict):
            return False
        window = payload.get("window") if isinstance(payload.get("window"), dict) else payload
        resolved_pos = pos
        if resolved_pos is None:
            raw = payload.get("pos") or (payload.get("target") or {}).get("pos") if isinstance(payload.get("target"), dict) else payload.get("pos")
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                resolved_pos = [int(raw[0]), int(raw[1]), int(raw[2])]
        if resolved_pos is None and block:
            resolved_pos = self._resolve_nearby_container_pos(block)
        mem = self._get_world_memory()
        try:
            synced = bool(
                mem.update_container_from_window(
                    window,
                    fallback_pos=resolved_pos,
                    fallback_block=block,
                    dim=self._current_dim(dim),
                )
            )
        except TypeError:
            synced = bool(mem.update_container_from_window(window, fallback_pos=resolved_pos, fallback_block=block))
        if synced and resolved_pos is not None:
            live = self._container_block_at(resolved_pos)
            if live in ("air", "cave_air", "void_air"):
                mem.invalidate_container(resolved_pos, dim=self._current_dim(dim), block_name=block)
                synced = False
        return synced


    def _window_api_error(
        self,
        err: ApiError,
        *,
        pos: list[int] | None = None,
        block: str | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Self-heal container cache from a failed `window/act` payload (context.window)."""
        ctx = dict(getattr(err, "context", None) or {})
        window = ctx.get("window") if isinstance(ctx.get("window"), dict) else None
        synced = False
        if window is not None:
            synced = self._sync_container_window(window, pos=pos, block=block, dim=dim)
        elif pos is not None and err.code in ("no_window", "not_found", "window_closed"):
            live = self._container_block_at(pos)
            if live in ("air", "cave_air", "void_air"):
                self._get_world_memory().invalidate_container(
                    pos, dim=self._current_dim(dim), block_name=block
                )
        return {
            **ctx,
            "ok": False,
            "error": err.code,
            "message": err.message,
            "failed_op": ctx.get("failed_op"),
            "done": ctx.get("done"),
            "window": window,
            "container_pos": pos,
            "memory_synced": synced,
            "context_keys": sorted(ctx.keys()),
        }


    def container_batch(
        self,
        items: Any,
        *,
        action: str | None = "put",
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Batch `put`/`take` on the `container` section of a chest/barrel window."""
        action_norm = str(action or "").strip().lower()
        if action_norm in ("get", "withdraw", "take_all", "fetch", "grab"):
            action = "take"
        elif action_norm in ("store", "deposit", "stash", "save"):
            action = "put"
        else:
            action = action_norm
        if action not in ("put", "take"):
            return {
                "ok": False, "error": "bad_args",
                "message": "请填写 action，存入用 put，取出用 take。查看箱内物品请用 mc_window_view。",
            }
        if isinstance(items, (list, tuple)):
            for index, row in enumerate(items):
                if isinstance(row, dict) and "items" in row and row.get("count") is None:
                    return {
                        "ok": False, "error": "bad_args", "item_index": index,
                        "message": f"items 列表第 {index + 1} 项请用 count 指定数量。",
                    }
        entries = _window_item_entries(items)
        if not entries:
            return {"ok": False, "error": "bad_args", "message": "请在 items 填写要存取的物品名和数量。查看箱内物品请用 mc_window_view。"}
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "请提供箱子的完整坐标 x、y、z，或用 block 指定附近的容器类型。"}

        ops: list[dict[str, Any]] = []
        for entry in entries:
            op: dict[str, Any] = {
                "op": action,
                "item": entry["item"],
                "count": entry["count"],
                "to" if action == "put" else "from": "container",
            }
            if action == "put":
                for key in ("slot", "potion", "enchanted", "custom_name"):
                    if key in entry:
                        op[key] = entry[key]
            ops.append(op)

        pos = [int(x), int(y), int(z)] if x is not None and y is not None and z is not None else None
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            if pos is None:
                pos = self._resolve_nearby_container_pos(block)
            failure = self._window_api_error(err, pos=pos, block=block, dim=dim)
            done = failure.get("done")
            if isinstance(done, list) and any(
                isinstance(op, dict) and (op.get("put") or op.get("took")) for op in done
            ):
                failure.update(
                    action=action, partial_transfer=True, requires_replan=True,
                    message=f"{err.message}。以 done 列出的转移为准。请复查箱内和背包，再处理剩余项目。",
                )
            return failure

        if pos is None:
            pos = self._resolve_nearby_container_pos(block)
        synced = self._sync_container_window(result, pos=pos, block=block, dim=dim)
        return {
            "ok": True,
            "action": action,
            "ops": ops,
            "done": result.get("done"),
            "window": result.get("window"),
            "container_pos": pos,
            "memory_synced": synced,
        }


    def container_deposit(
        self,
        items: Any,
        *,
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        return self.container_batch(items, action="put", block=block, x=x, y=y, z=z, dim=dim)


    def container_withdraw(
        self,
        items: Any,
        *,
        block: str | None = None,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
        dim: str | None = None,
    ) -> dict[str, Any]:
        return self.container_batch(items, action="take", block=block, x=x, y=y, z=z, dim=dim)


    def probe_unknown_nearby_containers(
        self,
        radius: float = 8.0,
        max_probes: int = 2,
        *,
        dim: str | None = None,
        skip_dungeon: bool = True,
    ) -> dict[str, Any]:
        """Passively build cache entries for nearby chests that are not known yet."""
        mem = self._get_world_memory()
        norm_dim = self._current_dim(dim)
        probed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            found = self.bot.call(
                "find_blocks",
                {"radius": max(4, min(32, int(radius))), "types": ["chest", "barrel", "trapped_chest"], "limit": 8},
            )
        except ApiError as err:
            return {"ok": False, "error": err.code, "probed": [], "skipped": []}
        spawners: list[list[int]] = []
        if skip_dungeon:
            try:
                sp = self.bot.call("find_blocks", {"radius": 16, "types": ["spawner"], "limit": 8})
                spawners = [
                    [int(v) for v in b["pos"][:3]]
                    for b in (sp.get("blocks") or [])
                    if isinstance(b, dict) and isinstance(b.get("pos"), list)
                ]
            except ApiError:
                spawners = []
        for blk in found.get("blocks") or []:
            if len(probed) >= max(1, int(max_probes)):
                break
            if not isinstance(blk, dict) or not isinstance(blk.get("pos"), list):
                continue
            pos = [int(v) for v in blk["pos"][:3]]
            key = (norm_dim, pos[0], pos[1], pos[2])
            if key in getattr(mem, "containers", {}):
                skipped.append({"pos": pos, "reason": "already_cached"})
                continue
            if skip_dungeon and any(
                math.dist(pos, sp) <= 6.0 for sp in spawners
            ):
                skipped.append({"pos": pos, "reason": "dungeon_spawner"})
                continue
            try:
                view = self.bot.call("window/view", {"target": {"pos": pos}})
            except ApiError as err:
                skipped.append({"pos": pos, "reason": f"view:{err.code}"})
                continue
            synced = self._sync_container_window(view, pos=pos, block=_clean_block(blk.get("name")), dim=norm_dim)
            probed.append({"pos": pos, "block": _clean_block(blk.get("name")), "synced": synced})
        return {"ok": bool(probed), "probed": probed, "skipped": skipped, "dim": norm_dim}


    def _protected_smelt_fuels(self) -> set[str]:
        if self.inventory_manager is not None:
            try:
                return set(self.inventory_manager.active_protected())
            except (AttributeError, TypeError):
                pass
        return set()


    def _smelt_fuel_log(self, counts: dict[str, int], input_item: str, *, input_reserved: int = 1) -> str | None:
        from ..inventory_manager import is_safe_fuel

        protected = self._protected_smelt_fuels()
        return next((
            log for log in LOG_TYPES
            if counts.get(log, 0) > (input_reserved if log == input_item else 0)
            and is_safe_fuel(log, protected)
            and is_safe_fuel(_log_to_planks_name(log), protected)
        ), None)


    def _choose_smelt_fuel(
        self,
        *,
        smelt_n: int,
        fuel_item: str | None,
        counts: dict[str, int],
        input_item: str,
    ) -> tuple[str | None, int, float]:
        from ..inventory_manager import SUPPORTED_FUELS, is_safe_fuel

        protected = self._protected_smelt_fuels()
        order = ["coal", "charcoal", "coal_block", "dried_kelp_block", "stick", "bamboo"]
        order += [n for n in SUPPORTED_FUELS if n not in order and n.endswith("_planks")]
        if fuel_item:
            clean = str(fuel_item).removeprefix("minecraft:")
            if is_safe_fuel(clean, protected) and counts.get(clean, 0) > 0:
                order = [clean] + [n for n in order if n != clean]
        for cand in order:
            if not is_safe_fuel(cand, protected):
                continue
            avail = int(counts.get(cand, 0)) - (smelt_n if cand == input_item else 0)
            if avail <= 0:
                continue
            per = float(SUPPORTED_FUELS.get(cand) or (1.5 if cand.endswith("_planks") else 1.5))
            need = max(1, math.ceil(smelt_n / max(0.25, per)))
            if avail < need:
                continue
            return cand, need, per
        # second pass: accept partial fuel (a partial batch still smelts fewer items)
        for cand in order:
            if not is_safe_fuel(cand, protected):
                continue
            avail = int(counts.get(cand, 0)) - (smelt_n if cand == input_item else 0)
            if avail <= 0:
                continue
            per = float(SUPPORTED_FUELS.get(cand) or 1.5)
            return cand, min(avail, max(1, math.ceil(smelt_n / max(0.25, per)))), per
        return None, 0, 0.0


    @staticmethod
    def _first_iron_pick_fuel_stock(
        counts: dict[str, int], input_item: str, smelt_n: int, *, preserve_short_stock: bool = False,
    ) -> tuple[dict[str, int] | None, int | None]:
        pick_tier = _best_pickaxe_in_inv(counts)[1]
        output = SMELT_OUTPUTS.get(input_item)
        preparing_charcoal = output == "charcoal" and pick_tier == PICKAXE_TIERS["stone_pickaxe"]
        if not preparing_charcoal and (output != "iron_ingot" or pick_tier >= PICKAXE_TIERS["iron_pickaxe"]):
            return counts, None
        sticks = counts.get("stick", 0)
        reserved_planks = (0 if counts.get("crafting_table", 0) else 4) + 2 * _stick_craft_batches(2, sticks)
        input_reserved = smelt_n if preparing_charcoal else 0
        spare_planks = _plank_equivalent(counts) - reserved_planks - 4 * input_reserved
        if spare_planks < 0 and not preserve_short_stock:
            if preparing_charcoal:
                return None, reserved_planks
            return counts, None  # Furnace selection cannot preserve an already missing recipe.
        spare_planks = max(0, spare_planks)
        spendable = dict(counts, stick=max(0, sticks - 2))
        for item, amount in counts.items():
            if item in _PLANK_SOURCE_LOGS:
                # The fuel selector subtracts any logs committed as input.
                spendable[item] = min(amount, spare_planks // 4 + (input_reserved if item == input_item else 0))
            elif item.endswith("_planks"):
                spendable[item] = min(amount, spare_planks)
        return spendable, reserved_planks


    def _plan_sync_smelt_fuel(
        self, counts: dict[str, int], input_item: str, smelt_n: int, requested: str | None,
    ) -> tuple[tuple[str | None, int, float], str | None, int | None]:
        spendable, reserved_planks = self._first_iron_pick_fuel_stock(counts, input_item, smelt_n)
        if spendable is None:
            return (None, 0, 0.0), None, reserved_planks
        current = self._choose_smelt_fuel(
            smelt_n=smelt_n, fuel_item=requested, counts=spendable, input_item=input_item,
        )
        if current[1] * current[2] >= smelt_n or (current[0] and reserved_planks is None):
            return current, None, reserved_planks
        charcoal_reserve = reserved_planks is not None and SMELT_OUTPUTS.get(input_item) == "charcoal"
        log = self._smelt_fuel_log(counts, input_item, input_reserved=smelt_n if charcoal_reserve else 1)
        if log is not None and reserved_planks is not None:
            converted = dict(counts)
            converted[log] -= 1
            planks = _log_to_planks_name(log)
            converted[planks] = converted.get(planks, 0) + 4
            after_conversion, _ = self._first_iron_pick_fuel_stock(converted, input_item, smelt_n)
            if after_conversion is None:
                return current, None, reserved_planks
            projected = self._choose_smelt_fuel(
                smelt_n=smelt_n, fuel_item=requested, counts=after_conversion, input_item=input_item,
            )
            if projected[1] * projected[2] <= current[1] * current[2]:
                log = None
        return current, log, reserved_planks

