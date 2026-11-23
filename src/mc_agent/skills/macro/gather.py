"""MacroSkills mixin: gather (split from legacy library.py)."""

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


class _GatherMixin:
    def _collect_nearby_items(
        self,
        *,
        bot_y: float,
        radius: int = 8,
        max_pickups: int = 5,
        fallback_coord: bool = True,
        target_items: set[str] | frozenset[str] | None = None,
        exclude_items: set[str] | frozenset[str] | None = None,
        verify_targets: bool | None = None,
    ) -> dict[str, Any]:
        """Record bounded approach attempts and confirm targeted pickup with inventory reads."""
        if target_items and (verify_targets is True or (verify_targets is None and target_items & _HIGH_VALUE_DROPS)):
            return self._collect_target_drops(target_items, radius, max_pickups)
        attempts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            ents = self.bot.call("get_entities", {"radius": radius, "types": ["item"]})
        except ApiError as err:
            return {"attempts": [], "skipped": [], "error": {"code": err.code, "message": err.message, **err.context}}
        cached_inv = getattr(self.bot, "_last_inventory", None)
        low_slots = isinstance(cached_inv, dict) and _compute_empty_slots(cached_inv) <= 6
        trash_all = frozenset(
            {
                *_TRASH_TIER1_ZERO_KEEP,
                *_TRASH_TIER2_DECOR_STONES,
                *_TRASH_TIER2_LOOSE_BLOCKS,
                *_TRASH_TIER3_DIRT,
                *_TRASH_TIER4_COBBLE,
            }
        )
        candidates: list[tuple[tuple[int, float], dict[str, Any]]] = []
        for idx, ent in enumerate(ents.get("entities") or []):
            pos = ent.get("pos") or []
            row: dict[str, Any] = {"id": ent.get("id"), "item": ent.get("item"), "item_pos": pos}
            if len(pos) < 3 or ent.get("id") is None:
                skipped.append({**row, "reason": "invalid_entity"})
                continue
            norm = _normalize_drop_name(ent.get("item"))
            in_target = bool(target_items and norm and norm in target_items)
            if exclude_items and norm and norm in exclude_items and not in_target:
                skipped.append({**row, "reason": "excluded_auto_dropped"})
                continue
            if target_items and norm and not in_target:
                skipped.append({**row, "reason": "non_target_item"})
                continue
            if low_slots and norm in _TRASH_TIER1_ZERO_KEEP and not in_target:
                skipped.append({**row, "reason": "ignored_clutter_low_slots"})
                continue
            if abs(float(pos[1]) - bot_y) > 4.5:
                skipped.append({**row, "reason": "height_difference", "height_difference": float(pos[1]) - bot_y})
                continue
            if in_target or norm in _HIGH_VALUE_DROPS:
                tier = 0
            elif norm in _MID_VALUE_DROPS or norm.endswith(("_log", "_stem")):
                tier = 1
            elif norm in trash_all:
                tier = 3
            else:
                tier = 2
            dist_val = float(ent.get("dist") if ent.get("dist") is not None else idx)
            candidates.append(((tier, dist_val), row))

        candidates.sort(key=lambda pair: pair[0])
        for _rank_key, row in candidates[:max_pickups]:
            pos = row["item_pos"]
            attempts.append(row)
            mpos: list[float] = []
            try:
                mv = self.bot.call("move_to", {"entity_id": row["id"], "range": 0.8, "timeout": 4000})
                mpos = mv.get("pos") or []
                row["pos"] = mpos
            except ApiError as err:
                row["approach_error"] = {"code": err.code, "message": err.message, **err.context}
                if err.code in ("interrupted", "dead", "disconnected"):
                    return {"attempts": attempts, "skipped": skipped, "error": row["approach_error"]}
            # The current API clamps entity goals to >=2 blocks. A successful
            # move may still be outside pickup reach, including vertically.
            needs_closer = len(mpos) < 3 or math.dist(mpos[:3], pos[:3]) > 1.0
            if fallback_coord and needs_closer and abs(float(pos[1]) - bot_y) <= 2.5:
                try:
                    mv = self.bot.call("move_to", {"x": float(pos[0]), "y": float(pos[1]), "z": float(pos[2]),
                                                    "range": 0.3, "timeout": 4000})
                    row["pos"] = mv.get("pos") or []
                except ApiError as err:
                    row["closer_error"] = {"code": err.code, "message": err.message, **err.context}
                    if err.code in ("interrupted", "dead", "disconnected"):
                        return {"attempts": attempts, "skipped": skipped, "error": row["closer_error"]}
            cur_pos = row.get("pos") or mpos
            if fallback_coord and len(cur_pos) >= 3:
                dy_item = float(pos[1]) - float(cur_pos[1])
                hdist_item = math.hypot(float(pos[0]) - float(cur_pos[0]), float(pos[2]) - float(cur_pos[2]))
                if abs(dy_item) <= 0.75 and 1.05 < math.dist(cur_pos[:3], pos[:3]) <= 2.35:
                    try:
                        ctrl = self.bot.call(
                            "control",
                            {
                                "forward": True,
                                "sneak": True,
                                "yaw": _yaw_toward(
                                    float(cur_pos[0]), float(cur_pos[2]), float(pos[0]), float(pos[2])
                                ),
                                "ms": 380,
                                "stop_on": ["hurt", "death"],
                            },
                        )
                        if isinstance(ctrl.get("pos"), list) and len(ctrl["pos"]) >= 3:
                            row["pos"] = ctrl["pos"]
                    except ApiError as err:
                        row["closer_error"] = {"code": err.code, "message": err.message, **err.context}
                        if err.code in ("interrupted", "dead", "disconnected"):
                            return {"attempts": attempts, "skipped": skipped, "error": row["closer_error"]}
                elif 0.75 < dy_item <= 1.85 and hdist_item <= 2.4:
                    try:
                        ctrl = self.bot.call(
                            "control",
                            {
                                "forward": True,
                                "jump": True,
                                "yaw": _yaw_toward(
                                    float(cur_pos[0]), float(cur_pos[2]), float(pos[0]), float(pos[2])
                                ),
                                "ms": 420,
                                "stop_on": ["hurt", "death"],
                            },
                        )
                        if isinstance(ctrl.get("pos"), list) and len(ctrl["pos"]) >= 3:
                            row["pos"] = ctrl["pos"]
                    except ApiError as err:
                        row["closer_error"] = {"code": err.code, "message": err.message, **err.context}
                        if err.code in ("interrupted", "dead", "disconnected"):
                            return {"attempts": attempts, "skipped": skipped, "error": row["closer_error"]}
            if row.get("pos"):
                row["distance_after"] = round(math.dist(row["pos"][:3], pos[:3]), 2)
                time.sleep(0.25)  # Allow server inventory packets to arrive before verification.
        return {"attempts": attempts, "skipped": skipped}


    def _collect_target_drops(
        self, target_items: set[str] | frozenset[str], radius: float, max_pickups: int, *,
        max_duration_s: float = 6.0, single_attempt: bool = False, dig_pickup: bool = False,
    ) -> dict[str, Any]:
        """Attempt each nearby target on a verified short route and retain unresolved positions."""
        from ..combat_env import CombatEnv

        started = time.monotonic()
        outer_deadline = getattr(self.bot, "_mining_deadline", started + 9.0)
        if isinstance(self.bot, _SkillBudgetSession):
            outer_deadline = self.bot.deadline
        budget = min(6.0, max_duration_s, outer_deadline - started - 0.5)
        session = _SkillBudgetSession(self.bot, budget, bound_transport_retries=True, single_attempt=single_attempt)
        terrain = CombatEnv(session, geofence=self.geofence)
        attempts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        gained: dict[str, int] = {}
        result: dict[str, Any] = {"attempts": attempts, "skipped": skipped, "gained": gained}
        for key, expires in list(self._drop_retry_after.items()):
            if expires <= started:
                del self._drop_retry_after[key]
        handled: set[int] = set()

        def read(path: str) -> dict[str, Any]:
            if dig_pickup and isinstance(self.bot, _SkillBudgetSession):
                return session.call(path, method="GET")
            return getattr(session, path)()

        def scan() -> dict[int, dict[str, Any]]:
            entities = session.call("get_entities", {"radius": min(math.ceil(radius), 6), "types": ["item"]})
            return {
                e["id"]: e for e in entities.get("entities") or []
                if isinstance(e.get("id"), int) and isinstance(e.get("pos"), list) and len(e["pos"]) >= 3
            }

        try:
            status = read("status")
            entities = scan()
            while entities and len(attempts if dig_pickup else handled) < max_pickups:
                origin = status.get("pos")
                if not origin or status.get("in_water") or status.get("in_lava"):
                    break
                if dig_pickup:
                    result["pos_after"] = origin
                pending = []
                for entity_id, ent in entities.items():
                    if entity_id in handled:
                        continue
                    if _normalize_drop_name(ent.get("item")) in target_items:
                        pending.append(ent)
                    else:
                        skipped.append({"id": entity_id, "item": ent.get("item"), "item_pos": ent["pos"], "reason": "non_target_item"})
                if not pending:
                    break
                ent = min(pending, key=lambda e: math.dist(origin[:3], e["pos"][:3]))
                entity_id, pos = ent["id"], ent["pos"][:3]
                handled.add(entity_id)
                item = _normalize_drop_name(ent.get("item"))
                row = {"id": entity_id, "item": ent.get("item"), "item_pos": pos}
                dy = pos[1] - origin[1]
                key = (
                    normalize_dimension(status.get("dimension")), entity_id,
                    tuple(math.floor(v) for v in origin[:3]),
                    tuple(math.floor(v * 2) for v in pos),
                )
                reason = None
                if key in self._drop_retry_after:
                    reason = "retry_cooldown"
                elif abs(dy) > (1.0 if dig_pickup else 1.25):
                    row["height_difference"] = dy
                    reason = "height_difference"
                elif math.dist(origin[:3], pos) > min(4.5, radius):
                    reason = "outside_short_pickup_range"
                else:
                    yaw = _yaw_toward(origin[0], origin[2], pos[0], pos[2])
                    route = terrain.probe_retreat(origin, yaw, math.hypot(pos[0] - origin[0], pos[2] - origin[2]))
                    if route is None or abs(route[0] - pos[1]) > 0.95:
                        reason = "unsafe_route"
                if reason:
                    skipped.append({**row, "reason": reason})
                    if reason != "retry_cooldown":
                        self._drop_retry_after[key] = time.monotonic() + 20.0
                    continue

                attempts.append(row)
                before = _inventory_counts_from_raw(read("inventory"))
                remaining = session.deadline - time.monotonic()
                if remaining < (0.35 if dig_pickup else 0.5):
                    raise _SkillDeadline()
                move_s = min(1.6, remaining - (0.15 if dig_pickup else 0.35))
                moved: dict[str, Any] = {}
                try:
                    moved = session.call("move_to", {
                        "x": pos[0], "y": route[0], "z": pos[2], "range": 0.8 if dig_pickup else 0.25,
                        "timeout": int(move_s * 1000), "stop_on": ["hurt", "death"],
                    }, timeout_s=move_s + 0.2)
                    if dig_pickup and moved.get("interrupted_by"):
                        result["error"] = {"code": "interrupted", "message": "拾取途中移动被中断。"}
                        break
                except ApiError as err:
                    row["approach_error"] = {"code": err.code, "message": err.message}
                    if err.code in _PICKUP_INTERRUPTION_CODES:
                        result["error"] = row["approach_error"]
                        break
                time.sleep(min(0.05 if dig_pickup else 0.2, max(0.0, session.deadline - time.monotonic())))
                if dig_pickup:
                    row["pos"] = moved.get("pos") or origin
                    result["pos_after"] = row["pos"]
                    visible = None
                else:
                    status = session.status()
                    entities = scan()
                    visible = entities.get(entity_id)
                    row["pos"] = status.get("pos")
                after = _inventory_counts_from_raw(read("inventory"))
                gain = max(0, after.get(item, 0) - before.get(item, 0))
                row["pickup_inventory_gain"] = gain
                if gain:
                    gained[item] = gained.get(item, 0) + gain
                    result["pos_after"] = row["pos"]
                if dig_pickup:
                    if not gain:
                        row["reason"] = "pickup_unconfirmed"
                        self._drop_retry_after[key] = time.monotonic() + 20.0
                    if len(attempts) >= max_pickups:
                        break
                    status = read("status")
                    entities = scan()
                    continue
                if visible is not None:
                    row["item_pos"] = visible["pos"][:3]
                if row["pos"]:
                    row["distance_after"] = round(math.dist(row["pos"][:3], row["item_pos"]), 2)
                label = str(row["item"] or "")
                suffix = label.rsplit("x", 1)[-1]
                stack_count = int(suffix) if suffix.isdigit() else 1
                row["picked_up"] = min(gain, stack_count) if visible is None else 0
                if not row["picked_up"]:
                    row["reason"] = "target_still_visible" if visible is not None else "pickup_unconfirmed"
                    self._drop_retry_after[key] = time.monotonic() + 20.0
        except (_SkillDeadline, httpx.TimeoutException):
            if session.pending_action:
                result["stop_confirmed"] = session.stop_pending()
            result["error"] = {"code": "pickup_time_budget", "message": "拾取预算已用尽。"}
        except (ApiError, httpx.HTTPError) as err:
            result["error"] = {"code": getattr(err, "code", type(err).__name__), "message": str(err)}
        # A refreshed scan can see the same irrelevant entities several times.
        result["skipped"] = list({row["id"]: row for row in skipped}.values())
        return result


    def collect_drops(
        self, radius: int = 12, max_pickups: int = 8, *,
        item_types: list[str] | None = None, max_duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Collect nearby ground item entities and report actual inventory delta."""
        wanted = None if item_types is None else frozenset(_clean_block(item) for item in item_types) - {""}
        if wanted is not None and not wanted:
            return {"ok": False, "error": "bad_args", "message": "item_types 需要至少一个物品名。"}
        if wanted is None and max_duration_s is None:
            return self._collect_drops(radius, max_pickups, wanted)
        session = _SkillBudgetSession(
            self.bot, 12.0 if max_duration_s is None else max_duration_s, bound_transport_retries=True,
        )
        scoped = copy(self)
        scoped.bot = session
        try:
            return scoped._collect_drops(radius, max_pickups, wanted)
        except (_SkillDeadline, httpx.TimeoutException):
            session.stop_pending()
            return {"ok": False, "error": "pickup_time_budget", "message": "拾取预算已用尽，请核对背包。"}


    def _collect_drops(
        self, radius: int, max_pickups: int, wanted: frozenset[str] | None,
    ) -> dict[str, Any]:
        inv_raw, inv_before = _inventory_snapshot_and_counts(self.bot)
        inv_raw, auto_dropped = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
        if auto_dropped:
            inv_before = _inventory_counts_from_raw(inv_raw)
        st = self.bot.status()
        bot_y = float((st.get("pos") or [0, 64, 0])[1])
        approaches = self._collect_nearby_items(
            bot_y=bot_y,
            radius=max(2, min(32, radius)),
            max_pickups=max(1, min(20, max_pickups)),
            fallback_coord=True,
            target_items=wanted,
            verify_targets=wanted is not None,
        )
        inv_after = _inventory_counts(self.bot)
        gained = _positive_inv_delta(inv_before, inv_after)
        if wanted is not None:
            gained = {item: n for item, n in gained.items() if item in wanted}
        state_after = self.bot.status()
        attempts = approaches["attempts"]
        out: dict[str, Any] = {
            "ok": bool(gained) and not approaches.get("error"),
            "attempted_pickups": len(attempts),
            "attempts": attempts,
            "skipped": approaches["skipped"],
            "moved_to": [row for row in attempts if row.get("pos")],
            "gained": gained,
            "inventory": inv_after,
            "pos_after": state_after.get("pos"),
            "health": state_after.get("health"),
        }
        if auto_dropped:
            out["auto_dropped"] = auto_dropped
        if approaches.get("error"):
            out["error"] = approaches["error"]["code"]
            out["details"] = approaches["error"]
        elif not gained:
            out["error"] = "no_drops_collected" if attempts else ("drops_unreachable" if approaches["skipped"] else "no_drops_found")
            base_msg = "未确认任何物品入包；走近成功不代表拾取成功。请检查高差、障碍与实际背包，改变站位或路线后再试，勿据此继续合成。"
            drop_summary = _summarize_uncollected_drops(
                state_after.get("pos") or st.get("pos"), attempts, approaches["skipped"]
            )
            if drop_summary is not None:
                out["nearest_unreachable_drop"] = drop_summary["nearest"]
                out["approach_hint"] = drop_summary["approach_hint"]
                out["message"] = f"{base_msg} {drop_summary['approach_hint']}"
            else:
                out["message"] = base_msg
        return out


    def _gather_with_budget(
        self, max_duration_s: float, gather: Callable[[MacroSkills], dict[str, Any]], *, cleanup_inventory: bool = False,
    ) -> dict[str, Any]:
        session = _SkillBudgetSession(self.bot, min(45.0, float(max_duration_s)))
        session.progress = {"dug_count": 0, "gained": {}}
        scoped = copy(self)
        scoped.bot = session
        try:
            result = gather(scoped)
        except (_SkillDeadline, httpx.TimeoutException):
            session.timed_out = True
            result = {}
        if session.timed_out and result.get("stop_reason") != "time_budget":
            session.stop_pending()
            return {
                **session.progress,
                **result,
                "ok": False,
                "requires_replan": True,
                "stop_reason": "time_budget",
                "message": "本轮采集已到时间预算。请按 gained 核对入包产物，再换近处可达目标或减少数量。",
            }
        if cleanup_inventory and result.get("stop_reason") not in {
            "time_budget", "pickup_interrupted", "approach_interrupted",
        }:
            try:
                _, cleaned = scoped._ensure_free_slots(min_free=3)
            except _SkillDeadline:
                session.stop_pending()
                result["cleanup_deadline_hit"] = True
            except (ApiError, httpx.HTTPError):
                pass
            else:
                if cleaned:
                    result["inventory_cleaned"] = cleaned
        return result


    def _reachable_log_column(
        self,
        target: dict[str, Any],
        candidates: list[dict[str, Any]],
        standing: list[float],
        count: int,
        deadline: float,
    ) -> list[dict[str, Any]]:
        x, y, z = target["pos"]
        by_pos = {tuple(row["pos"]): row for row in candidates if row.get("name") == target.get("name")}
        column = [target]
        for dy in range(1, min(count, 4)):
            next_log = by_pos.get((x, y + dy, z))
            if next_log is None:
                break
            column.append(next_log)
        if len(column) == 1:
            return column

        def reachable(pos: list[float]) -> bool:
            # The dig endpoint skips pathfinding when the block is already within four blocks.
            return (math.floor(pos[0]), math.floor(pos[2])) != (x, z) and all(
                math.dist(pos, row["pos"]) <= 4.0 for row in column
            )

        if reachable(standing):
            return column
        tx, tz = x + 0.5, z + 0.5
        if self.geofence and self.geofence.check_point(tx, tz, action="mc_skill_harvest_logs"):
            return [target]
        remaining = min(8.0, deadline - time.monotonic())
        if remaining <= 2.0:
            return [target]
        try:
            moved = self.bot.call(
                "move_to",
                {"x": tx, "z": tz, "range": 1.5, "timeout": int((remaining - 0.5) * 1000), "stop_on": ["hurt", "death"]},
                timeout_s=remaining,
            )
        except ApiError as exc:
            if exc.code in _TRANSIENT_BLOCK_ERRORS:
                return [target]
            raise
        if moved.get("interrupted_by"):
            raise ApiError(code="interrupted", message="接近树干时移动被中断。")
        pos = moved.get("pos") or self.bot.status().get("pos")
        return column if isinstance(pos, list) and len(pos) == 3 and reachable(pos) else [target]


    def _approach_elevated_log(
        self, session: _SkillBudgetSession, before: dict[str, Any], log: list[int],
    ) -> dict[str, Any]:
        """Take one verified step toward a high log without digging a route."""
        from ..combat_env import CombatEnv, aim_yaw_pitch, yaw_to_unit_xz

        origin = before["pos"]
        result = {"log": log, "pos_before": origin, "moved": False}

        def safe_stance(state: dict[str, Any]) -> bool:
            return (
                state.get("pos") is not None and state.get("on_ground") is True
                and state.get("ready") is not False and state.get("alive") is not False
                and not state.get("in_water") and not state.get("in_lava")
                and state.get("dimension") == before.get("dimension")
                and state.get("last_death") == before.get("last_death")
            )

        if not safe_stance(before) or before.get("busy"):
            return {**result, "reason": "unsafe_stance"}
        log_center = [log[0] + 0.5, log[1], log[2] + 0.5]
        yaw, _ = aim_yaw_pitch(origin, log_center)
        distance = math.hypot(log_center[0] - origin[0], log_center[2] - origin[2])
        probe = _SkillBudgetSession(session, min(4.0, session._remaining()), single_attempt=True)
        terrain = CombatEnv(probe, geofence=self.geofence)
        for stride in (min(2.5, distance - 0.75), 1.25):
            if stride < 1.0 or stride > distance - 0.75:
                continue
            for angle in (0, -35, 35):
                if probe.timed_out or probe.deadline <= time.monotonic() or session._remaining() <= 2.5:
                    return {**result, "reason": "insufficient_time"}
                heading = yaw + math.radians(angle)
                route = terrain.probe_retreat(origin, heading, stride)
                if route is None or route[0] < math.floor(origin[1] + 0.05):
                    continue
                ux, uz = yaw_to_unit_xz(heading)
                x, z = origin[0] + ux * stride, origin[2] + uz * stride
                current = session.status()
                if not safe_stance(current) or current.get("busy") or math.dist(current["pos"], origin) > 0.2:
                    return {**result, "reason": "stance_changed", "interrupted": True}
                hop = _SkillBudgetSession(session, min(4.5, session._remaining()), single_attempt=True)
                error = self._scout_move(hop, x, z, y=route[0], seconds=2.5, range_m=0.25)
                if error and error not in SURFACE_SCOUT_PATH_ERRORS:
                    return {**result, "reason": error, "interrupted": True}
                after = session.status()
                if not safe_stance(after):
                    return {**result, "reason": "stance_changed", "interrupted": True}
                pos = after["pos"]
                moved = (
                    math.dist(pos, [x, route[0], z]) <= 0.55
                    and math.dist(pos, log_center) < math.dist(origin, log_center) - 0.4
                )
                return {**result, "pos_after": pos, "moved": moved, **({} if moved else {"reason": error or "move_not_confirmed"})}
        return {**result, "reason": "unsafe_landing"}


    def _dig_target_blocks(
        self,
        *,
        block_types: list[str],
        count: int,
        radius: int,
        protected_msg: str,
        below_tol: float,
        above_tol: float,
        inv_before: dict[str, int] | None = None,
        avoid_water: bool = False,
        auto_dropped_names: set[str] | frozenset[str] | None = None,
        approach_elevated_logs: bool = False,
    ) -> dict[str, Any]:
        """Shared candidate scan, vertical reachability ranking, blacklist handling, and drop rescue."""
        expected_drops = _expected_drops_for_blocks(block_types)
        harvesting_logs = all(name in LOG_TYPES or name in NETHER_STEM_TYPES for name in block_types)
        common_gather = bool(expected_drops) and expected_drops <= _COMMON_GATHER_DROPS
        batch_size = 3 if harvesting_logs else 2

        def _target_gain_sum(gain_map: dict[str, int]) -> int:
            return sum(
                int(v)
                for k, v in gain_map.items()
                if str(k).removeprefix("minecraft:") in expected_drops and int(v) > 0
            )

        gained: dict[str, int] = {}

        def _merge_confirmed_inventory_gain() -> None:
            if inv_before is not None:
                for name, amount in _positive_inv_delta(inv_before, _inventory_counts(self.bot)).items():
                    gained[name] = max(int(gained.get(name) or 0), int(amount))

        dug_count = 0
        protected_hits = 0
        water_skipped = 0
        last_drop_scan: dict[str, Any] | None = None
        last_bot_pos: list[float] | None = None
        session = self.bot if isinstance(self.bot, _SkillBudgetSession) else None
        mine_deadline = session.deadline if session else time.monotonic() + 45.0
        if session:
            session.progress.update(requested_count=count, dug_count=0, gained=gained)
        batch_start_dug = batch_start_gain = 0
        broken_positions: set[tuple[int, int, int]] = set()
        prefer_reachable = False
        pickup_interrupted = False

        def collect_common_batch() -> None:
            nonlocal last_drop_scan, last_bot_pos, pickup_interrupted, prefer_reachable
            batch_count = dug_count - batch_start_dug
            if _target_gain_sum(gained) - batch_start_gain >= batch_count:
                return
            _merge_confirmed_inventory_gain()
            if _target_gain_sum(gained) - batch_start_gain >= batch_count:
                return
            last_drop_scan = self._collect_target_drops(
                expected_drops, 4.5, 2, max_duration_s=min(3.0, mine_deadline - time.monotonic()),
                single_attempt=True, dig_pickup=True,
            )
            last_bot_pos = last_drop_scan.get("pos_after") or last_bot_pos
            _merge_counts(gained, last_drop_scan["gained"])
            if last_drop_scan["attempts"]:
                trunk_queue.clear()
            error = last_drop_scan.get("error") or {}
            pickup_interrupted = last_drop_scan.get("stop_confirmed") is False or error.get("code") in _PICKUP_INTERRUPTION_CODES
            prefer_reachable = _target_gain_sum(gained) == batch_start_gain

        deadline_hit = False
        consecutive_failures = 0
        unreachable_targets = False
        log_approach: dict[str, Any] | None = None
        trunk_queue: list[dict[str, Any]] = []
        try:
            dig_call_params = inspect.signature(self.bot.call).parameters
            dig_supports_timeout = "timeout_s" in dig_call_params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in dig_call_params.values()
            )
        except (TypeError, ValueError):
            dig_supports_timeout = False

        for _ in range(count * 3):
            if dug_count >= count or (harvesting_logs and _target_gain_sum(gained) >= count):
                break
            if time.monotonic() >= mine_deadline:
                deadline_hit = True
                break
            if trunk_queue:
                target = trunk_queue.pop(0)
            else:
                for apos in list(self.temporary_avoidance):
                    self._is_block_avoided(apos)
                scan_limit = min(60, 30 + len(self.temporary_avoidance))
                found = self.bot.call(
                    "find_blocks",
                    {"radius": max(4, min(64, radius)), "types": block_types, "limit": scan_limit},
                )
                candidates = [
                    b
                    for b in (found.get("blocks") or [])
                    if not self._is_block_avoided(tuple(b.get("pos") or ()))
                ]
                if not candidates:
                    break
                st = self.bot.status()
                standing = st.get("pos") or [0, 64, 0]
                bot_y = float(standing[1])
                feet_y = math.floor(bot_y)
                if (
                    approach_elevated_logs and session is not None and st.get("pos")
                    and normalize_dimension(st.get("dimension")) == "overworld"
                    and all(b["pos"][1] > bot_y + above_tol and math.dist(standing, b["pos"]) > 4.0 for b in candidates)
                ):
                    if dug_count == 0 and log_approach is None:
                        nearest = min(candidates, key=lambda b: math.dist(standing, b["pos"]))
                        log_approach = self._approach_elevated_log(session, st, nearest["pos"])
                        if log_approach["moved"]:
                            continue
                    break
                candidates.sort(
                    key=lambda b: (
                        (
                            max(0.0, (bot_y - below_tol) - float(b["pos"][1]))
                            + max(0.0, float(b["pos"][1]) - (bot_y + above_tol))
                        )
                        * 4.5
                        + float(b.get("dist", 99))
                        + (4.5 * abs(float(b["pos"][1]) - feet_y) if harvesting_logs else 0.0)
                    )
                )
                target = None
                for cand in candidates:
                    cpos = tuple(cand["pos"])
                    if prefer_reachable and (
                        cpos in broken_positions or not feet_y <= cpos[1] <= feet_y + 1 or math.dist(standing, cpos) > 4.0
                    ):
                        continue
                    if avoid_water and float(cand["pos"][1]) < bot_y - 0.5 and self._is_water_covered(cpos):
                        self._avoid_temporarily(cpos, "water_covered", 30.0)
                        water_skipped += 1
                        continue
                    target = cand
                    break
                if target is None:
                    break
                if harvesting_logs and count - dug_count > 1:
                    trunk_queue = self._reachable_log_column(target, candidates, standing, count - dug_count, mine_deadline)[1:]
            pos = tuple(target["pos"])
            remaining = mine_deadline - time.monotonic()
            if remaining <= 1.0:
                deadline_hit = True
                break
            # A single un-timed dig can eat the whole slice (r12 obsidian:
            # 4 blocks / 300s). Keep a hard slice cap, but do not starve each
            # dig by dividing the remainder by the remaining blocks (r14:
            # normal stone/log digs need >8s under fleet load). The 15s floor
            # still leaves the total bounded by mine_deadline.
            per_dig = max(15.0, min(30.0, remaining / max(1, count - dug_count)))
            # Through-wall pre-seal & catch-pad for cave-side / pool-side ores:
            # 1) plug adjacent water/lava cells with filler blocks before digging the ore;
            # 2) place a catch block directly beneath suspended ores so drops never fall into ravines/lava.
            t_name = str(target.get("name") or "").removeprefix("minecraft:")
            if not harvesting_logs and (t_name.endswith("_ore") or t_name in {"obsidian", "crying_obsidian"}) and remaining > 4.0:
                try:
                    inv_seal = _inventory_counts(self.bot)
                    filler = next(
                        (
                            b
                            for b in ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "granite", "diorite", "tuff", "netherrack")
                            if inv_seal.get(b, 0) > 0
                        ),
                        None,
                    )
                    if filler is not None:
                        bpos_now = self.bot.status().get("pos") or [pos[0], pos[1], pos[2]]
                        bx_now, by_now, bz_now = math.floor(float(bpos_now[0])), math.floor(float(bpos_now[1])), math.floor(float(bpos_now[2]))
                        bot_cells = {(bx_now, by_now, bz_now), (bx_now, by_now + 1, bz_now)}
                        for dx, dy, dz in ((0, -1, 0), (0, 1, 0), (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
                            nb = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                            if nb in bot_cells:
                                continue
                            if math.dist(bpos_now, [nb[0] + 0.5, nb[1] + 0.5, nb[2] + 0.5]) > 4.5:
                                continue
                            blk_r = self.bot.call("get_block", {"x": nb[0], "y": nb[1], "z": nb[2]})
                            nb_name = str(blk_r.get("name") or blk_r.get("block") or "").removeprefix("minecraft:")
                            is_fluid = nb_name in {"water", "lava", "flowing_water", "flowing_lava"}
                            is_catch_air = (dx, dy, dz) == (0, -1, 0) and nb_name in {"air", "cave_air", "void_air"}
                            if is_fluid or is_catch_air:
                                try:
                                    self.bot.call("place", {"item": filler, "pos": [nb[0], nb[1], nb[2]]})
                                except ApiError:
                                    pass
                except (ApiError, _SkillDeadline, Exception):
                    pass
            try:
                batch_end = common_gather and dug_count + 1 - batch_start_dug >= batch_size
                dig_body = {"pos": list(pos), "collect": not trunk_queue or batch_end}
                res = (
                    self.bot.call("dig", dig_body, timeout_s=min(remaining, per_dig))
                    if dig_supports_timeout
                    else self.bot.call("dig", dig_body)
                )
                dug_count += 1
                if common_gather:
                    broken_positions.add(pos)
                consecutive_failures = 0
                if session:
                    session.progress["dug_count"] = dug_count
                for k, v in (res.get("gained") or {}).items():
                    gained[k] = gained.get(k, 0) + int(v)
                if avoid_water and bool(self.bot.status().get("in_water")):
                    esc = self.escape_water()
                    if esc.get("in_water"):
                        break
                if batch_end:
                    collect_common_batch()
                    if pickup_interrupted:
                        break
                    batch_start_dug, batch_start_gain = dug_count, _target_gain_sum(gained)
                elif not common_gather and not trunk_queue and dug_count >= 2 and _target_gain_sum(gained) == 0:
                    last_bot_pos = self.bot.status().get("pos") if harvesting_logs else st.get("pos")
                    last_drop_scan = self._collect_nearby_items(
                        bot_y=float(last_bot_pos[1]) if last_bot_pos else bot_y,
                        radius=12,
                        target_items=expected_drops,
                        exclude_items=auto_dropped_names,
                        verify_targets=False,
                    )
                    if inv_before is not None:
                        _merge_confirmed_inventory_gain()
                        if _target_gain_sum(gained) > 0:
                            continue
                    break
            except ApiError as err:
                trunk_queue.clear()
                if _is_protected_err(err):
                    self.protected_positions.add(pos)
                    protected_hits += 1
                    if protected_hits >= 3 and dug_count == 0:
                        return {
                            "ok": False,
                            "error": "protected_region",
                            "message": protected_msg,
                            **self._avoidance_summary(),
                        }
                elif err.code in _TRANSIENT_BLOCK_ERRORS:
                    avoid_secs = (
                        90.0
                        if err.code == "out_of_reach" and float(pos[1]) > bot_y + above_tol
                        else 15.0
                    )
                    if harvesting_logs and err.code in {"path_not_found", "no_path", "out_of_reach"}:
                        avoid_secs = 60.0
                    self._avoid_temporarily(pos, err.code, seconds=avoid_secs)
                    consecutive_failures += 1
                    if harvesting_logs and consecutive_failures >= 4:
                        unreachable_targets = True
                        break
                elif err.code == "interrupted":
                    self._avoid_temporarily(pos, err.code, 3.0)
                    esc = None
                    if bool(self.bot.status().get("in_water")):
                        esc = self.escape_water()
                    out_err: dict[str, Any] = {
                        "ok": False,
                        "requires_replan": True,
                        "dug_count": dug_count,
                        "gained": gained,
                        "partial_error": {"code": err.code, "message": err.message, **err.context},
                        **self._avoidance_summary(),
                    }
                    if esc is not None:
                        out_err["water_escape"] = esc
                    return out_err
                else:
                    return {
                        "ok": False,
                        "requires_replan": True,
                        "dug_count": dug_count,
                        "gained": gained,
                        "partial_error": {"code": err.code, "message": err.message, **err.context},
                    }
            except httpx.TimeoutException as err:
                # Do not let one server-side dig outlive the slice. Release the
                # pending action before returning the confirmed partial result.
                self._avoid_temporarily(pos, "dig_timeout", 30.0)
                if session:
                    session.stop_pending()
                else:
                    try:
                        self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
                    except (ApiError, httpx.HTTPError):
                        pass
                return {
                    "ok": False,
                    "requires_replan": True,
                    "stop_reason": "time_budget",
                    "dig_timeout_s": round(min(remaining, per_dig), 2),
                    "dug_count": dug_count,
                    "gained": gained,
                    "partial_error": {"code": "mine_timeout", "message": "单次挖掘未在切片预算内确认；已停止并保留已入包产物。"},
                    "message": "本轮挖掘预算已用尽，单次 dig 超时；请换到目标旁 2 格内或降低 count 后重试，勿等待同一调用完成。",
                    **self._avoidance_summary(),
                }

        if common_gather and not (deadline_hit or unreachable_targets or pickup_interrupted) and dug_count > batch_start_dug:
            collect_common_batch()
        if not (common_gather or deadline_hit or unreachable_targets) and dug_count > 0 and _target_gain_sum(gained) == 0:
            if last_drop_scan is None:
                st = self.bot.status()
                last_bot_pos = st.get("pos")
                bot_y = float((last_bot_pos or [0, 64, 0])[1])
                last_drop_scan = self._collect_nearby_items(
                    bot_y=bot_y,
                    radius=12,
                    target_items=expected_drops,
                    exclude_items=auto_dropped_names,
                    verify_targets=False,
                )
            _merge_confirmed_inventory_gain()
        if (
            not common_gather and harvesting_logs and dug_count >= count
            and 0 < _target_gain_sum(gained) < count
            and not deadline_hit and inv_before is not None
            and mine_deadline - time.monotonic() > 2.0
        ):
            _merge_confirmed_inventory_gain()
            if _target_gain_sum(gained) < count and last_drop_scan is None:
                last_bot_pos = self.bot.status().get("pos")
                last_drop_scan = self._collect_nearby_items(
                    bot_y=float(last_bot_pos[1]) if last_bot_pos else bot_y,
                    radius=8,
                    max_pickups=2,
                    target_items=expected_drops,
                    exclude_items=auto_dropped_names,
                    verify_targets=False,
                )
                _merge_confirmed_inventory_gain()

        if dug_count == 0 and water_skipped > 0:
            recovery = (
                "请到干燥且有支撑的位置，再调 mc_skill_mine_dfs(action='explore', target='iron')。"
                if set(block_types) <= {"iron_ore", "deepslate_iron_ore"}
                else "请换到干燥且有支撑的位置，寻找同层或露出的同类方块，再调用 mc_skill_mine_blocks。"
            )
            return {
                "ok": False,
                "error": "underwater_targets_skipped",
                "message": f"已跳过 {water_skipped} 处水下目标，暂避 30 秒。{recovery}",
                "dug_count": 0,
                "gained": gained,
                "water_skipped": water_skipped,
                **self._avoidance_summary(),
            }

        target_gained = {
            k: int(v)
            for k, v in gained.items()
            if str(k).removeprefix("minecraft:") in expected_drops and int(v) > 0
        }
        incidental_gained = {
            k: int(v)
            for k, v in gained.items()
            if str(k).removeprefix("minecraft:") not in expected_drops and int(v) > 0
        }
        collected = sum(target_gained.values())

        out: dict[str, Any] = {
            "ok": collected >= count if harvesting_logs else dug_count >= count and bool(target_gained),
            "requested_count": count,
            "dug_count": dug_count,
            "gained": gained,
            "target_gained": target_gained,
            "water_skipped": water_skipped,
            **self._avoidance_summary(),
        }
        if incidental_gained:
            out["incidental_gained"] = incidental_gained
        if dug_count > 0 and not target_gained:
            out["error"] = "no_drops_collected"
            out["requires_replan"] = True
            drop_summary = _summarize_uncollected_drops(
                last_bot_pos,
                (last_drop_scan or {}).get("attempts") or [],
                (last_drop_scan or {}).get("skipped") or [],
                gathering=True,
            )
            inc_note = (
                f"（途中仅误吸无关杂物 {incidental_gained}，目标预期产物 {sorted(expected_drops)} 均未入包）"
                if incidental_gained
                else ""
            )
            if drop_summary is not None:
                out["nearby_drops"] = drop_summary["items"]
                out["nearest_unreachable_drop"] = drop_summary["nearest"]
                out["approach_hint"] = drop_summary["approach_hint"]
                out["message"] = f"连续挖掘 {dug_count} 格但目标掉落物未入包！{inc_note}{drop_summary['approach_hint']}"
            else:
                out["message"] = (
                    f"连续挖掘 {dug_count} 格但目标掉落物均未拾取{inc_note}（可能掉入深坑、被地形卡住或工具等级不匹配），"
                    "已停止继续损耗耐久，请移动到安全平层/斜坡挖掘或用 mc_get_entities 检查地面物品。"
                )
        elif not out["ok"]:
            out["error"] = "partial_collection" if dug_count else "no_targets_available"
            if (
                dug_count == 0
                and not self.protected_positions
                and not self.temporary_avoidance
            ):
                out["message"] = (
                    f"半径 {radius} 格内未扫描到任何匹配目标方块（{', '.join(block_types)}），"
                    "请先水平移动或使用 mc_skill_staircase 改变海拔后再探测，勿原地仅修改 count 重试。"
                )
            elif harvesting_logs and dug_count >= count:
                out["partial_progress"] = True
                out["message"] = f"已砍 {dug_count} 格，确认 {collected}/{count} 根原木入包。请按已入包数量决定下一步。"
            else:
                out["message"] = f"本次只挖掘 {dug_count}/{count} 格；请按 gained 检查实际入包量。临时避让与保护区分别报告，改变站位或等待冷却后可重试临时失败。"
            out["requires_replan"] = True
        if unreachable_targets:
            out["stop_reason"] = "unreachable_targets"
            out["error"] = "unreachable_targets"
            out["requires_replan"] = True
            out["message"] = (
                "已连续 4 次未能挖到选中的原木。请用 mc_move_to 换到附近树干旁或更换站位，再采集低处原木。"
            )
        if log_approach is not None:
            out["log_approach"] = log_approach
            if dug_count == 0:
                interrupted = log_approach.get("interrupted")
                out.update(
                    error="interrupted" if interrupted else "unreachable_targets",
                    stop_reason="approach_interrupted" if interrupted else "unreachable_targets",
                    message=(
                        "接近高处原木时动作被中断，请先确认当前站位。"
                        if interrupted else "高处原木仍不可达。请换到有支撑的较高站位，或寻找低处树干。"
                    ),
                )
        if pickup_interrupted:
            out.update(ok=False, stop_reason="pickup_interrupted", requires_replan=True)
            out["pickup"] = last_drop_scan
            out["message"] = "拾取动作被中断。请先复核当前位置和背包。"
        if deadline_hit:
            out["stop_reason"] = "time_budget"
            out["requires_replan"] = True
            out["message"] = (
                f"本轮挖掘预算已用尽，仅完成 {dug_count}/{count} 格；已保留入包产物，"
                "请换到目标旁 2 格内或降低 count 后重试，勿等待同一调用完成。"
            )
        return out


    def harvest_logs(
        self, target_logs: int = 4, radius: int = 32, *, max_duration_s: float = 45.0,
        cleanup_inventory: bool = False,
    ) -> dict[str, Any]:
        """Gather logs or nether stems within a shared skill budget."""
        result = self._gather_with_budget(
            max_duration_s, lambda skills: skills._harvest_logs(target_logs, radius),
            cleanup_inventory=cleanup_inventory,
        )
        if result.get("stop_reason") == "time_budget":
            logs = {
                name: amount for name, amount in (result.get("gained") or {}).items()
                if name.removeprefix("minecraft:") in (*LOG_TYPES, *NETHER_STEM_TYPES)
            }
            collected = sum(logs.values())
            if collected >= 4:
                result.update(
                    ok=True, partial_progress=True, requires_replan=False, target_gained=logs,
                    message=f"采木时间已到，已确认 {collected} 根原木入包。可直接用已有木料制作。",
                )
                result.pop("error", None)
        return result


    def _harvest_logs(self, target_logs: int, radius: int) -> dict[str, Any]:
        target_logs = max(1, min(32, int(target_logs)))
        radius = max(4, min(64, int(radius)))
        st0: dict[str, Any] = {}
        if callable(getattr(self.bot, "status", None)):
            try:
                st0 = self.bot.status() or {}
            except Exception:
                st0 = {}
        raw_yaw = st0.get("yaw")
        away_yaw = (float(raw_yaw) + math.pi) if isinstance(raw_yaw, (int, float)) else None
        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        inv_raw, auto_dropped = self._ensure_free_slots(
            min_free=8,
            inv_snapshot=inv_raw,
            turn_away_yaw=away_yaw,
        )
        if auto_dropped:
            counts = _inventory_counts_from_raw(inv_raw)
        auto_dropped_names = frozenset(
            str(d["item"]).removeprefix("minecraft:")
            for d in auto_dropped
            if isinstance(d, dict) and d.get("item")
        )
        equipped = self._equip_if_present(AXE_PRIORITY)
        dim = normalize_dimension(st0.get("dimension")) if st0 else "overworld"
        wood_targets = list(NETHER_STEM_TYPES) if dim == "the_nether" else list(LOG_TYPES)
        res = self._dig_target_blocks(
            block_types=wood_targets,
            count=target_logs,
            radius=radius,
            protected_msg="周围原木被服务端明确拒绝为 protected_region，请离开该保护区域后再砍树。",
            below_tol=1.5,
            above_tol=2.5,
            inv_before=counts,
            auto_dropped_names=auto_dropped_names or None,
            approach_elevated_logs=True,
        )
        if res.get("error") == "protected_region":
            return res
        if (
            res.get("stop_reason") != "time_budget"
            and res.get("dug_count", 0) == 0
            and res.get("error") == "no_targets_available"
        ):
            enriched = self._enrich_ecology_unavailable(res, targets=wood_targets, radius=radius, counts=counts)
            if enriched is not None:
                return {**enriched, "equipped_axe": equipped}
            if radius < 32:
                retry_res = self._dig_target_blocks(
                    block_types=wood_targets,
                    count=target_logs,
                    radius=32,
                    protected_msg="周围原木被服务端明确拒绝为 protected_region，请离开该保护区域后再砍树。",
                    below_tol=1.5,
                    above_tol=2.5,
                    inv_before=counts,
                    auto_dropped_names=auto_dropped_names or None,
                    approach_elevated_logs=True,
                )
                if retry_res.get("error") == "protected_region":
                    return retry_res
                res = retry_res

        if (
            res.get("stop_reason") not in {"time_budget", "unreachable_targets", "approach_interrupted"}
            and int(res.get("dug_count") or 0) == 0
            and self.temporary_avoidance
        ):
            reach_entries = [
                (pos_k, rec)
                for pos_k, rec in self.temporary_avoidance.items()
                if rec.get("reason") in {"out_of_reach", "path_not_found", "no_path"}
            ]
            if reach_entries:
                try:
                    cur_p = (self.bot.status() or {}).get("pos") or [0.0, 64.0, 0.0]
                    cx, cy, cz = float(cur_p[0]), float(cur_p[1]), float(cur_p[2])
                    px, py, pz = math.floor(cx), math.floor(cy), math.floor(cz)
                    tx, ty, tz = min(
                        (pos_k for pos_k, _ in reach_entries),
                        key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2 + (p[2] - cz) ** 2,
                    )
                    dx, dy, dz = tx - cx, float(ty) - cy, tz - cz
                    bearing = cardinal_bearing(dx, dz)
                    probe_cache: dict[tuple[int, int, int], str] = {}
                    up_dirs = self._probe_viable_up_stair_dirs(
                        px,
                        py,
                        pz,
                        probe_cache=probe_cache,
                        inv_counts=counts,
                    )
                    vpillar = (
                        False
                        if up_dirs
                        else self._probe_viable_pillar_up(
                            px,
                            py,
                            pz,
                            probe_cache=probe_cache,
                            inv_counts=counts,
                        )
                    )
                    best_up = bearing if bearing in up_dirs else (up_dirs[0] if up_dirs else bearing)
                    ux, uz = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}.get(best_up, (0, -1))
                    esc_x, esc_z = px + ux * 24, pz + uz * 24
                    steps_up = max(3, min(8, int(abs(dy) + 1)))
                    if dy >= 2.0:
                        if up_dirs:
                            approach_hint = (
                                f"当前处于低洼/凹坑(y={py})或周围原木仅剩高处残冠（最近原木位于[{tx},{ty},{tz}], 相对朝向={bearing}, dy={dy:+.1f}），"
                                f"直接砍树或原地 mc_skill_unstick 无法够到！请立即调用 "
                                f"mc_skill_staircase(direction='{best_up}', vertical='up', steps={steps_up}) 爬出低坑登顶平层，"
                                f"或调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格寻找整棵新树；"
                                "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                            )
                        elif vpillar:
                            approach_hint = (
                                f"周围原木仅剩高处目标（最近位于[{tx},{ty},{tz}], dy={dy:+.1f}）且四向阶梯受阻，"
                                f"请先用 mc_skill_staircase(direction='pillar', vertical='pillar', steps={min(6, steps_up)}) 垫脚升层，"
                                f"或调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格寻找整棵新树；"
                                "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                            )
                        else:
                            approach_hint = (
                                f"周围原木均为高处残冠或地形阻挡不可达（最近位于[{tx},{ty},{tz}], 相对朝向={bearing}, dy={dy:+.1f}），"
                                f"请调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格以上换一片树林；"
                                "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                            )
                    else:
                        approach_hint = (
                            f"周围原木均被地形/树干遮挡或超距不可达（最近位于[{tx},{ty},{tz}], 相对朝向={bearing}, dy={dy:+.1f}），"
                            f"请朝 {best_up} 调 mc_skill_staircase(direction='{best_up}', vertical='up', steps=4) 跨越地形，"
                            f"或调用 mc_move_to(x={esc_x}, z={esc_z})(省略y) 水平转移24格换一片树林；"
                            "严禁原地重复 mc_skill_harvest_logs 或 mc_skill_unstick！"
                        )
                    res["requires_replan"] = True
                    res["nearest_unreachable_target"] = {
                        "pos": [tx, ty, tz],
                        "bearing": bearing,
                        "dy": round(dy, 1),
                    }
                    res["viable_up_directions"] = up_dirs
                    res["viable_pillar_up"] = vpillar
                    res["approach_hint"] = approach_hint
                    res["message"] = approach_hint
                except Exception:
                    pass
        out_logs: dict[str, Any] = {
            **res,
            "equipped_axe": equipped,
        }
        if auto_dropped:
            out_logs["auto_dropped"] = auto_dropped
        return out_logs


    def _attach_shortage_memory_hint(self, out_short: dict[str, Any], missing_keys: Any) -> dict[str, Any]:
        if self.world_memory is not None:
            try:
                st_m = self.bot.status()
                mem_tip = self.world_memory.shortage_memory_hint(
                    missing_keys,
                    origin=st_m.get("pos"),
                    dim=normalize_dimension(st_m.get("dimension")),
                )
                if mem_tip:
                    out_short["memory_hint"] = mem_tip
                    if "message" in out_short:
                        out_short["message"] = f"{out_short['message']}（{mem_tip}）"
            except Exception:
                pass
        return out_short

