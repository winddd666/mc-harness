"""MacroSkills mixin: scout_nav (split from legacy library.py)."""

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


class _ScoutNavMixin:
    def scout_surface(
        self,
        direction: str = "auto",
        target_types: list[str] | None = None,
        max_duration_s: float = 45.0,
        step_dist: float = 28.0,
        *,
        track_ender_eye: bool = False,
    ) -> dict[str, Any]:
        """Scout in bounded hops, measuring horizontal distance between observed positions."""
        direction = direction.strip().lower()
        result: dict[str, Any] = {
            "ok": False,
            "distance_traveled": 0.0,
            "segments_completed": 0,
            "final_pos": None,
            "biome": None,
            "detected_pois": [],
            "detected_entities": [],
        }
        if direction not in SURFACE_SCOUT_DIRECTIONS and (direction != "auto" or track_ender_eye):
            return {**result, "stop_reason": "invalid_direction", "requires_replan": True}
        if not math.isfinite(max_duration_s) or not math.isfinite(step_dist):
            return {**result, "stop_reason": "invalid_budget_or_step", "requires_replan": True}
        session = _SkillBudgetSession(self.bot, min(50.0, max_duration_s))
        memory = self._get_world_memory()
        observations: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {
            "detected_pois": {}, "detected_entities": {},
        }
        distance = 0.0
        found_target = False
        eye_heading = SURFACE_SCOUT_DIRECTIONS.index(direction) if track_ender_eye else None
        eye_headings = (
            {(eye_heading + offset) % len(SURFACE_SCOUT_DIRECTIONS) for offset in (-1, 0, 1)}
            if eye_heading is not None else None
        )

        def observe() -> dict[str, Any]:
            nonlocal distance
            status = session.call("status", method="GET", timeout_s=2.0, allow_reauth=False)
            pos = status.get("pos")
            prev_pos = result["final_pos"]
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                if prev_pos is not None:
                    distance += math.hypot(pos[0] - prev_pos[0], pos[2] - prev_pos[2])
                result["final_pos"] = list(pos)
                result["biome"] = status.get("biome") or None
                memory.observe_status(status)
            else:
                status["pos"] = None
            return status

        def scan(status: dict[str, Any]) -> bool:
            nonlocal found_target
            dim = normalize_dimension(status.get("dimension"))
            if dim == "overworld" and hasattr(memory, "mark_village_scanned"):
                memory.mark_village_scanned(status["pos"], dim=dim)
            for route, key, receipt_key, types in (
                ("find_blocks", "blocks", "detected_pois", blocks),
                ("get_entities", "entities", "detected_entities", entities),
                ("find_blocks", "blocks", "detected_pois", chests),
                ("find_blocks", "blocks", "detected_pois", paths),
                ("find_blocks", "blocks", "detected_pois", landmarks),
            ):
                if not types or (types is paths and found_target):
                    continue
                query = {"radius": 64, "types": types}
                if key == "blocks":
                    query["limit"] = 20 if types is chests else 6
                while types:
                    try:
                        rows = session.call(route, query, timeout_s=3.0, allow_reauth=False).get(key, [])
                        break
                    except ApiError as exc:
                        rejected = rejected_find_block(exc.code, exc.message, types) if key == "blocks" else None
                        if rejected is None:
                            raise
                        types.remove(rejected)
                        remaining_targets.discard(rejected)
                        result.setdefault("ignored_types", []).append(rejected)
                        if not remaining_targets:
                            raise ApiError(
                                code="invalid_target_types",
                                message="没有可扫描的目标。请使用有效的方块或实体 ID，也可省略目标使用当前维度的默认扫描。",
                            ) from None
                else:
                    continue
                matches = [
                    row for row in rows
                    if isinstance(row, dict) and row.get("name") in types and isinstance(row.get("pos"), (list, tuple))
                    and len(row["pos"]) == 3
                ][:6]
                if key == "blocks":
                    memory.observe_blocks(matches, dim=dim)
                else:
                    memory.observe_entities(matches, dim=dim)
                for row in matches:
                    name = row["name"]
                    height_delta = row["pos"][1] - status["pos"][1]
                    is_target = name in targets and (not default_village_search or abs(height_delta) <= 12)
                    key_pos = (name, *row["pos"]) if name in targets else (name,)
                    receipt = {
                        "name": row["name"],
                        "pos": row["pos"],
                        "dist": round(math.dist(status["pos"], row["pos"]), 1),
                        "biome": row.get("biome"),
                        "observer_biome": status.get("biome"),
                        "observer_pos": status["pos"],
                        **({"height_delta": round(height_delta, 1)} if types is chests else {}),
                        **({"id": row["id"]} if "id" in row else {}),
                    }
                    prior = observations[receipt_key].get(key_pos)
                    if prior is None or receipt["dist"] < prior["dist"]:
                        observations[receipt_key][key_pos] = receipt
                    found_target |= is_target
            return found_target

        def waypoint(heading: int, length: float) -> tuple[float, float]:
            x, _, z = status["pos"]
            angle = heading * math.pi / 4
            return round(x + length * math.cos(angle), 2), round(z + length * math.sin(angle), 2)

        def fence_allows(x: float, z: float) -> bool:
            return self.geofence is None or self.geofence.check_point(x, z, action="mc_skill_scout_surface") is None

        def reach_dry_bank(dry_origin: list[float] | None = None) -> str | None:
            nonlocal status
            from ..combat_env import CombatEnv, SHORE_LAND_BLOCKS

            if status.get("submerged"):
                health_before = status.get("health")
                surfaced = session.call(
                    "control", {"jump": True, "ms": 550, "stop_on": ["hurt", "death"]},
                    timeout_s=1.8, allow_reauth=False,
                )
                status = observe()
                danger = self._scout_danger(status, health_before=health_before, dimension=dimension)
                if danger or surfaced.get("interrupted_by"):
                    return danger or surfaced["interrupted_by"]
                if status.get("submerged"):
                    return "in_water"
            x, y, z = status["pos"]
            shore = dry_origin
            if shore is None or math.hypot(x - shore[0], z - shore[2]) > 8.0 or not fence_allows(shore[0], shore[2]):
                shore = CombatEnv(session, geofence=self.geofence)._find_walkable_shore_target(
                    status["pos"], block_types=SHORE_LAND_BLOCKS, max_radius=10,
                    min_y=math.floor(y) - 2, max_y=math.floor(y) + 1, max_candidates=6,
                )
                session._remaining()
            if shore is None or math.hypot(x - shore[0], z - shore[2]) > 8.0 or not fence_allows(shore[0], shore[2]):
                return "in_water"
            health_before = status.get("health")
            error = self._scout_move(session, shore[0], shore[2], seconds=5.0, range_m=0.5, y=shore[1])
            if error == "move_timeout_unreleased":
                return error
            status = observe()
            danger = self._scout_danger(status, health_before=health_before, dimension=dimension)
            if danger:
                return danger
            if error and error not in SURFACE_SCOUT_PATH_ERRORS:
                return error
            if status.get("in_water") or status.get("submerged") or status.get("on_ground") is False:
                return "in_water"
            result["shore_recoveries"] = result.get("shore_recoveries", 0) + 1
            return "remeasure_after_detour" if track_ender_eye else None

        def advance(heading: int, length: float) -> str:
            nonlocal direction, status
            x, y, z = status["pos"]
            tx, tz = waypoint(heading, length)
            if not fence_allows(tx, tz):
                return "void_geofence_blocked"
            landing_y = None
            if nether_probe is not None:
                if status.get("on_ground") is not True:
                    return "not_grounded"
                route = nether_probe.probe_retreat(status["pos"], _yaw_toward(x, z, tx, tz), math.hypot(tx - x, tz - z))
                session._remaining()
                if route is None:
                    return "unsafe_nether_route"
                landing_y, _ = route
            elif self._scout_route_has_liquid(session, x, y, z, tx - x, tz - z):
                return "liquid_ahead"

            route_key = (dimension, heading)
            previous = self._failed_scout_routes.get(route_key)
            if previous is not None:
                previous_pos, previous_step, expires = previous
                if (
                    time.monotonic() < expires and math.dist((x, y, z), previous_pos) <= 2.0
                    and abs(length - previous_step) <= 0.5
                ):
                    return "scout_route_cooldown"
            health_before = status.get("health")
            move_error = self._scout_move(
                session, tx, tz, seconds=10.0, y=landing_y, range_m=0.5 if nether else 1.5,
            )
            if move_error == "move_timeout_unreleased":
                return move_error
            status = observe()
            danger = self._scout_danger(status, health_before=health_before, dimension=dimension)
            if danger:
                if nether and danger == "nearby_hostile" and scan(status):
                    return "target_found"
                return danger
            if move_error and move_error not in SURFACE_SCOUT_PATH_ERRORS:
                return move_error
            if status.get("in_water") or status.get("submerged"):
                return reach_dry_bank([x, y, z]) or "liquid_ahead"
            px, py, pz = status["pos"]
            if eye_heading is not None:
                angle = eye_heading * math.pi / 4
                forward = (px - x) * math.cos(angle) + (pz - z) * math.sin(angle)
                if forward < -0.5:
                    return "remeasure_after_detour"
            if move_error:
                if move_error in {"path_not_found", "no_path"} and math.dist((x, y, z), (px, py, pz)) < 1.0:
                    self._failed_scout_routes[route_key] = ((x, y, z), length, time.monotonic() + 180.0)
                return move_error
            if nether_probe is not None:
                if status.get("on_ground") is not True or abs(py - landing_y) > 0.75:
                    return "unsafe_nether_landing"
                landing = nether_probe.probe_retreat(status["pos"], 0.0, 0.0)
                session._remaining()
                if landing is None:
                    return "unsafe_nether_landing"
            arrival_tolerance = 0.75 if length == short_nether_hop else 3.0
            if math.hypot(px - tx, pz - tz) > arrival_tolerance:
                return "stalled"
            direction = SURFACE_SCOUT_DIRECTIONS[heading]
            result["segments_completed"] += 1
            return "target_found" if scan(status) else "segment_completed"

        terrain_blocks = {"void_geofence_blocked", "liquid_ahead", "unsafe_nether_route"}
        nether_route_blocks = {"void_geofence_blocked", "unsafe_nether_route"}
        short_nether_hop = 4.0
        detour_reasons = SURFACE_SCOUT_PATH_ERRORS | terrain_blocks | {"scout_route_cooldown"}
        blocked_dirs: list[str] = []

        def record_blocked(heading_idx: int, why: str) -> None:
            dname = SURFACE_SCOUT_DIRECTIONS[heading_idx]
            if dname not in blocked_dirs:
                blocked_dirs.append(dname)
            if (
                not nether
                and why in {"liquid_ahead", "void_geofence_blocked"}
                and status.get("pos") is not None
                and hasattr(memory, "record_exploration_edge")
            ):
                memory.record_exploration_edge(status["pos"], dname, why, dim=dimension)
        try:
            status = observe()
            origin = result["final_pos"]
            dimension = normalize_dimension(status.get("dimension"))
            nether = dimension == "the_nether"
            nether_probe = None
            if nether:
                from ..combat_env import CombatEnv

                nether_probe = CombatEnv(session, geofence=self.geofence)
            default_village_search = not nether and not target_types
            default_blocks = NETHER_SCOUT_TARGET_BLOCKS if nether else tuple(
                name for name in VILLAGE_LANDMARK_BLOCKS if name != "chest"
            )
            default_entities = NETHER_SCOUT_TARGET_ENTITIES if nether else SURFACE_SCOUT_ENTITIES
            requested = [resolve_item_alias(name) for name in (target_types or (*default_blocks, *default_entities))]
            if nether and any(name in (*default_blocks, *default_entities) for name in requested):
                # Bulk terrain can neither crowd out fortress markers nor end a fortress scan.
                requested = [name for name in requested if name != "netherrack"]
            targets = tuple(dict.fromkeys(
                block for name in requested for block in (BED_BLOCK_TYPES if name in {"bed", "beds", "*_bed"} else (name,))
            ))
            remaining_targets = set(targets)
            known_entities = (*SURFACE_SCOUT_ENTITIES, *NETHER_SCOUT_TARGET_ENTITIES)
            blocks = [
                name for name in targets
                if name not in known_entities and name not in VILLAGE_SURVEY_CLUTTER and name != "chest"
            ]
            chests = ["chest"] if not nether or "chest" in targets else []
            paths = ["dirt_path"] if not nether or "dirt_path" in targets else []
            if "farmland" in targets:
                paths.append("farmland")
            if not blocks and not any(name in VILLAGE_SURVEY_CLUTTER or name == "chest" for name in targets):
                blocks = list(default_blocks)
            entities = [name for name in targets if name in known_entities] or list(default_entities)
            landmarks = [name for name in SURFACE_SCOUT_LANDMARKS if name not in blocks]
            step = max(16.0, min(20.0, step_dist)) if nether else max(28.0, min(32.0, step_dist))
            danger = self._scout_danger(status, dimension=dimension)
            if not danger and (status.get("in_water") or status.get("submerged")):
                danger = reach_dry_bank()
            if danger:
                reason = "target_found" if nether and danger == "nearby_hostile" and scan(status) else danger
            elif scan(status):
                reason = "target_found"
            else:
                if direction == "auto":
                    if hasattr(memory, "unexplored_directions_8"):
                        unexplored = memory.unexplored_directions_8(status["pos"], dim=dimension)
                    else:
                        unexplored = memory.unexplored_directions(status["pos"], dim=dimension)
                    preferences = list(dict.fromkeys((*unexplored, *SURFACE_SCOUT_DIRECTIONS)))
                    direction = next((
                        name for name in preferences
                        if fence_allows(*waypoint(SURFACE_SCOUT_DIRECTIONS.index(name), 2 * step if nether else 40.0))
                    ), preferences[0])
                for _ in range(5):
                    health_before = status.get("health")
                    status = observe()
                    danger = self._scout_danger(status, health_before=health_before, dimension=dimension)
                    if not danger and (status.get("in_water") or status.get("submerged")):
                        danger = reach_dry_bank()
                    if danger:
                        reason = "target_found" if nether and danger == "nearby_hostile" and scan(status) else danger
                        break
                    heading = SURFACE_SCOUT_DIRECTIONS.index(direction)
                    reason = advance(heading, step)
                    if reason in detour_reasons:
                        record_blocked(heading, reason)
                    try_short_hop = nether and not track_ender_eye and reason in nether_route_blocks
                    explore_wider = reason in terrain_blocks
                    base_offsets = (-2, 2, -3, 3, -1, 1, 4) if explore_wider else (-1, 1, -2, 2, -3, 3, 4)
                    if not nether and not track_ender_eye and status.get("pos") is not None and hasattr(memory, "score_direction_novelty"):
                        # Keep eligible 45° detours ahead of wider turns until a terrain block permits wider turns (BUG-071).
                        offsets = tuple(
                            sorted(
                                base_offsets,
                                key=lambda off: (
                                    1 if (not explore_wider and abs(off) > 1) else 0,
                                    1 if off == 4 else 0,
                                    memory.score_direction_novelty(
                                        status["pos"],
                                        SURFACE_SCOUT_DIRECTIONS[(heading + off) % len(SURFACE_SCOUT_DIRECTIONS)],
                                        dim=dimension,
                                    ),
                                    base_offsets.index(off),
                                ),
                            )
                        )
                    else:
                        offsets = tuple(sorted(base_offsets, key=lambda off: not explore_wider and abs(off) > 1))
                    for offset in offsets:
                        if reason not in detour_reasons:
                            break
                        if abs(offset) > 1 and not explore_wider:
                            break
                        candidate = (heading + offset) % len(SURFACE_SCOUT_DIRECTIONS)
                        if eye_headings is not None and candidate not in eye_headings:
                            continue
                        reason = advance(candidate, step)
                        if reason in detour_reasons:
                            record_blocked(candidate, reason)
                        try_short_hop &= reason in nether_route_blocks
                        explore_wider |= reason in terrain_blocks
                    if try_short_hop:
                        for offset in (0, *offsets):
                            candidate = (heading + offset) % len(SURFACE_SCOUT_DIRECTIONS)
                            reason = advance(candidate, short_nether_hop)
                            if reason not in nether_route_blocks:
                                break
                        if reason == "segment_completed":
                            reason = "short_nether_hop"
                    if track_ender_eye and reason in detour_reasons:
                        reason = "remeasure_after_detour"
                    if reason != "segment_completed":
                        break
                else:
                    reason = "segment_limit"
        except _SkillDeadline:
            session.stop_pending()
            result["deadline_hit"] = True
            reason = "target_found" if found_target else "time_budget"
        except ApiError as exc:
            reason = exc.code
            result["error"] = {"code": exc.code, "message": exc.message}
        except httpx.HTTPError:
            reason = "transport_error"
        result.update(
            ok=reason == "target_found" or (
                reason in {"segment_limit", "time_budget", "short_nether_hop"} and result["segments_completed"] > 0
            ),
            stop_reason=reason,
            distance_traveled=round(distance, 2),
            pos_after=result["final_pos"],
            direction=direction,
        )
        if blocked_dirs:
            result["blocked_directions"] = blocked_dirs[:4]
        if (
            result["final_pos"] is not None
            and not result.get("ok")
            and normalize_dimension(locals().get("dimension", "overworld")) == "overworld"
            and hasattr(memory, "unexplored_directions_8")
        ):
            rec_dirs = memory.unexplored_directions_8(
                result["final_pos"], dim="overworld", exclude=set(blocked_dirs),
            )
            if rec_dirs:
                result["unexplored_directions"] = rec_dirs[:3]
        if not result["ok"]:
            result["requires_replan"] = True
            result.setdefault("error", {"code": reason})
        if reason == "scout_route_cooldown":
            result["error"]["message"] = "当前位置的候选航线刚刚寻路失败。请先用 mc_skill_unstick 或安全的短距移动换站位，再启动斥候。"
        if reason == "remeasure_after_detour":
            final = result["final_pos"]
            result["pos_before"] = origin
            if math.hypot(final[0] - origin[0], final[2] - origin[2]) < 1.0:
                result["needs_new_vantage"] = True
                message = "原测向点附近未能推进。请先换到可达的开阔干地并确认已离开原处，再重新掷眼。"
            else:
                message = "追眼路线受阻或发生绕行。请重新测向后再按新回执的方向继续。"
            result["error"]["message"] = message + "若用两次投眼定位，请与上一次投点水平相距至少 25 格再投。"
        biome = str(result["biome"] or "").lower()
        if any(part in biome for part in ("ocean", "dark_forest", "jungle")):
            result["biome_hint"] = "当前群系不利于寻村，请改走朝向平原、草原或其他开阔干地的方向。"
        for key, rows in observations.items():
            result[key] = list(rows.values())
        return result


    @staticmethod
    def _scout_danger(
        status: dict[str, Any], *, dimension: str, health_before: float | None = None
    ) -> str | None:
        if normalize_dimension(status.get("dimension")) != dimension:
            return "dimension_changed"
        if status.get("in_lava") or status.get("on_fire"):
            return "hazard"
        health = status.get("health")
        if status.get("alive") is False or (health is not None and health <= 0):
            return "dead"
        if health_before is not None and health is not None and health < health_before:
            return "hurt"
        hostile = status.get("nearest_hostile") or {}
        distance = hostile.get("dist")
        if isinstance(distance, (int, float)) and distance <= 8.0:
            return "nearby_hostile"
        if status.get("ready") is False or not status.get("pos"):
            return "not_ready"
        if status.get("busy"):
            return "busy"
        return None


    @staticmethod
    def _scout_route_has_liquid(
        session: _SkillBudgetSession, x: float, y: float, z: float, dx: float, dz: float
    ) -> bool:
        for fraction in (0.5, 1.0):
            pos = [math.floor(x + dx * fraction), math.floor(y) - 1, math.floor(z + dz * fraction)]
            try:
                block = session.call("get_block", {"pos": pos}, timeout_s=1.5, allow_reauth=False)
            except ApiError as exc:
                if exc.code == "not_loaded":
                    continue
                raise
            if block.get("name") in {"water", "flowing_water", "bubble_column", "lava", "flowing_lava"}:
                return True
        return False


    @staticmethod
    def _scout_move(
        session: _SkillBudgetSession, x: float, z: float, *, seconds: float, range_m: float = 1.5, y: float | None = None
    ) -> str | None:
        # Ask pathfinder to finish before the client timeout so it can release the action.
        request_s = min(seconds + 1.0, session._remaining())
        if request_s <= 1.0:
            raise _SkillDeadline()
        payload = {"x": x, "z": z, "range": range_m, "timeout": int((request_s - 0.75) * 1000), "stop_on": ["hurt", "death"]}
        if y is not None:
            payload["y"] = y
        try:
            moved = session.call(
                "move_to", payload, timeout_s=request_s, allow_reauth=False,
            )
        except _SkillDeadline:
            if not session.pending_action:
                raise
            if not session.stop_pending():
                return "move_timeout_unreleased"
            session.timed_out = False
            return "move_timeout"
        except ApiError as exc:
            if exc.code == "interrupted":
                return str(exc.context.get("cause") or "interrupted")
            if exc.code in SURFACE_SCOUT_PATH_ERRORS:
                return exc.code
            raise
        except httpx.TransportError:
            session.pending_action = True
            session.stop_pending()
            raise
        if moved.get("dimension_changed"):
            return "dimension_changed"
        return moved.get("interrupted_by")


    def navigate_segmented(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        range_m: float = 2.5,
        step_m: float = 24.0,
        geofence: Any = None,
        ignore_hurt: bool = False,
        skip_direct: bool = False,
        hop_timeout_ms: int = 20000,
    ) -> dict[str, Any]:
        """Relay long-distance navigation (>36m) in ~24m segments with ±35 deg detour fallback."""
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        cx, cy, cz = float(pos0[0]), float(pos0[1]), float(pos0[2])
        total_hdist = math.hypot(float(x) - cx, float(z) - cz)
        stop_on = ["death"] if (ignore_hurt or st0.get("in_water")) else ["hurt", "death"]

        if total_hdist <= 36.0:
            payload: dict[str, Any] = {
                "x": float(x),
                "z": float(z),
                "range": range_m,
                "timeout": max(4000, int(hop_timeout_ms)),
                "stop_on": stop_on,
            }
            if y is not None:
                payload["y"] = float(y)
            res = self.bot.call("move_to", payload)
            nav = self._get_terrain_nav()
            nav.note_status()
            return {"ok": True, "segmented": False, "segments_done": 1, "result": res}

        last_err: ApiError = ApiError("path_not_found", f"unable to reach ({x:.1f}, {z:.1f})")
        if not skip_direct:
            # Try direct first in case chunks are already loaded and unobstructed
            try:
                direct_payload: dict[str, Any] = {
                    "x": float(x),
                    "z": float(z),
                    "range": range_m,
                    "stop_on": stop_on,
                    "timeout": 30000,
                }
                if y is not None:
                    direct_payload["y"] = float(y)
                res_d = self.bot.call("move_to", direct_payload, timeout_s=42.0)
                return {"ok": True, "segmented": False, "segments_done": 1, "result": res_d}
            except ApiError as direct_err:
                if direct_err.code in {"hurt", "death", "interrupted"}:
                    raise
                if direct_err.code == "busy":
                    self._get_terrain_nav().release_stuck_action()
                last_err = direct_err
            except Exception as exc:  # noqa: BLE001 - long direct path may outlive the HTTP timeout
                last_err = ApiError(
                    code="timeout",
                    message=f"direct move_to HTTP timeout ({type(exc).__name__}); fall back to segmented hops",
                )

        max_hops = min(18, max(3, int(math.ceil(total_hdist / max(10.0, step_m))) + 3))
        segs_done = 0
        nav = self._get_terrain_nav()
        recoveries: list[dict[str, Any]] = []
        for _ in range(max_hops):
            st_cur = self.bot.status()
            p_cur = st_cur.get("pos") or [cx, cy, cz]
            cx, cy, cz = float(p_cur[0]), float(p_cur[1]), float(p_cur[2])
            rem = math.hypot(float(x) - cx, float(z) - cz)
            if rem <= max(range_m + 1.0, 3.5):
                return {
                    "ok": True,
                    "segmented": True,
                    "segments_done": segs_done,
                    "pos": p_cur,
                    "recoveries": recoveries,
                }

            if rem <= step_m + 4.0:
                hop_targets = [(float(x), float(z), y, range_m)]
            else:
                base_ang = math.atan2(float(z) - cz, float(x) - cx)
                hop_targets = [
                    (cx + math.cos(base_ang + ang_off) * step_m, cz + math.sin(base_ang + ang_off) * step_m, None, 3.0)
                    for ang_off in (0.0, 0.61, -0.61)
                ]

            advanced = False
            for hx, hz, hy, hr in hop_targets:
                if geofence is not None and geofence.check_point(hx, hz, action="navigate_segmented") is not None:
                    continue
                hop_payload: dict[str, Any] = {
                    "x": round(hx, 2),
                    "z": round(hz, 2),
                    "range": hr,
                    "stop_on": stop_on,
                    "timeout": max(4000, int(hop_timeout_ms)),
                }
                if hy is not None:
                    hop_payload["y"] = float(hy)
                try:
                    self.bot.call("move_to", hop_payload)
                    st_after = self.bot.status()
                    p_after = st_after.get("pos") or [cx, cy, cz]
                    progress = math.hypot(float(p_after[0]) - cx, float(p_after[2]) - cz)
                    hop_dist = math.hypot(float(hx) - cx, float(hz) - cz)
                    # A 24m hop that only creeps 1-2m is a stall, not progress: hand it to the
                    # local-recovery state machine instead of burning the whole hop budget.
                    need_m = max(2.0, min(8.0, hop_dist * 0.3))
                    if progress >= need_m or math.hypot(
                        float(x) - float(p_after[0]), float(z) - float(p_after[2])
                    ) <= max(range_m + 1.0, 3.5):
                        segs_done += 1
                        advanced = True
                        break
                except ApiError as hop_err:
                    if hop_err.code in {"hurt", "death", "interrupted"}:
                        raise
                    if hop_err.code == "busy":
                        # a previously hung action can wedge the server lock: free it and retry once
                        nav.release_stuck_action()
                        try:
                            self.bot.call("move_to", hop_payload, timeout_s=40.0)
                        except ApiError as retry_err:
                            if retry_err.code in {"hurt", "death", "interrupted"}:
                                raise
                            last_err = retry_err
                            continue
                        except Exception:  # noqa: BLE001
                            last_err = ApiError(code="timeout", message="hop retry HTTP timeout")
                            continue
                    last_err = hop_err
                    continue
                except Exception as exc:  # noqa: BLE001 - treat a hung hop as a failed hop
                    last_err = ApiError(
                        code="timeout",
                        message=f"hop move_to HTTP timeout ({type(exc).__name__})",
                    )
                    continue
            if not advanced:
                if len(recoveries) < MAX_LOCAL_RECOVERY_ATTEMPTS:
                    rec = nav.recover(x=float(x), z=float(z), y=y, attempt=len(recoveries) + 1)
                    recoveries.append(
                        {
                            "op": rec.get("op"),
                            "diagnosis": rec.get("diagnosis"),
                            "ok": bool(rec.get("ok")),
                            "error": rec.get("error"),
                        }
                    )
                    if rec.get("ok"):
                        segs_done += 1
                        nav.note_status()
                    if rec.get("ok") or len(recoveries) < MAX_LOCAL_RECOVERY_ATTEMPTS:
                        continue
                raise ApiError(
                    code=last_err.code,
                    message=(
                        f"{last_err.message}（navigate_segmented 已完成 {segs_done} 段，"
                        f"局部恢复 {len(recoveries)} 次仍不通）"
                    ),
                    http_status=last_err.http_status,
                    ms=last_err.ms,
                    context={
                        **dict(last_err.context or {}),
                        "segments_done": segs_done,
                        "recoveries": recoveries,
                        "blocked_reason": (recoveries[-1] if recoveries else "no_recovery"),
                        "pos": self.bot.status().get("pos"),
                    },
                )
            nav.note_status()

        st_fin = self.bot.status()
        p_fin = st_fin.get("pos") or [cx, cy, cz]
        if math.hypot(float(x) - float(p_fin[0]), float(z) - float(p_fin[2])) <= max(range_m + 2.0, 4.5):
            return {"ok": True, "segmented": True, "segments_done": segs_done, "pos": p_fin}
        raise last_err

