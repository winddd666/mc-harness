"""MacroSkills mixin: combat_portal (split from legacy library.py)."""

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


class _CombatPortalMixin:
    def flee_hostiles(
        self,
        *,
        distance: float = 14.0,
        urgent: bool = False,
        geofence: Any | None = None,
        threat_pos: list[float] | None = None,
        max_duration_s: float | None = None,
    ) -> dict[str, Any]:
        if max_duration_s is None:
            return self._flee_hostiles(distance=distance, urgent=urgent, geofence=geofence, threat_pos=threat_pos)

        from ..mining_dfs import _BudgetSession, _MiningDeadline

        budget = max(0.0, min(35.0, max_duration_s))
        finish_at = time.monotonic() + budget
        session = _BudgetSession(self.bot, finish_at - min(1.0, budget / 4))
        scoped = copy(self)
        scoped.bot = session.session
        engine = self._mining_dfs
        scoped_engine = None
        if engine is not None and engine.nodes:
            # Share the map and seal ledger. Leave the live engine and BotSession
            # wired to their existing callers during the retreat.
            scoped_engine = copy(engine)
            scoped_engine.bot = scoped.bot
            scoped_engine.skills = scoped
            scoped_engine._deadline = session.deadline
            scoped._mining_dfs = scoped_engine

        def failed(code: str, message: str) -> dict[str, Any]:
            return {
                "ok": False, "requires_replan": True, "stop_reason": code,
                "error": {"code": code, "message": message},
                "pos_before": session.first_pos, "pos_after": session.last_status.get("pos"),
            }

        def stop_pending() -> None:
            session.deadline = finish_at
            try:
                session.call("stop", {}, timeout_s=0.25)
            except (_MiningDeadline, ApiError, httpx.HTTPError):
                pass

        try:
            return scoped._flee_hostiles(distance=distance, urgent=urgent, geofence=geofence, threat_pos=threat_pos)
        except _MiningDeadline:
            if session.timeout_path and session.timeout_path not in session._RETRYABLE_PROBES:
                stop_pending()
            result = failed("time_budget", "撤退已达到时间预算，请检查最后确认的位置并选择安全站位。")
            result["deadline_hit"] = True
            result["timeout_path"] = session.timeout_path
            return result
        except ApiError as exc:
            if exc.code in {"not_connected", "action_quarantined", "action_timeout", "death_loop_cooldown"}:
                return failed(exc.code, exc.message)
            raise
        except httpx.TransportError:
            stop_pending()
            return failed("transport_error", "撤退请求中断，请确认当前位置与动作状态。")
        finally:
            if scoped_engine is not None:
                engine.active_node_id = scoped_engine.active_node_id
                engine.dug_during_return = scoped_engine.dug_during_return


    def _flee_hostiles(
        self,
        *,
        distance: float = 14.0,
        urgent: bool = False,
        geofence: Any | None = None,
        threat_pos: list[float] | None = None,
    ) -> dict[str, Any]:
        """Hold/seal inside a carved shaft when safe or sealable, else sprint-jump flee with death-only stop."""
        target_d = max(8.0, min(22.0, float(distance)))
        st0 = self.bot.status()
        if st0.get("alive") is False:
            return {"ok": False, "mode": "flee", "error": "dead", "requires_replan": True}

        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        px, py, pz = float(pos0[0]), float(pos0[1]), float(pos0[2])

        block_cache: dict[tuple[int, int, int], str] = {}
        enclosure = inspect_local_enclosure(self.bot, pos0, block_cache=block_cache)
        threat = assess_hostile_occlusion(
            self.bot,
            pos0,
            radius=18,
            fallback_nearest=st0.get("nearest_hostile"),
            block_cache=block_cache,
        )

        if (
            not urgent
            and enclosure.get("structure") in {"sealed_bunker", "carved_shaft"}
            and threat["complete"]
            and len(threat["unoccluded"]) == 0
            and threat_pos is None
        ):
            return {
                "ok": True,
                "mode": "hold_in_shaft",
                "structure": enclosure["structure"],
                "moved_dist": 0.0,
                "occluded_hostiles": [
                    {
                        "name": o["name"],
                        "dist": o["dist"],
                        "bearing": o["bearing"],
                        "blocker": (o.get("blocker") or {}).get("name"),
                    }
                    for o in threat["occluded"][:3]
                ],
                "pos_before": [round(px, 1), round(py, 1), round(pz, 1)],
                "pos_after": [round(px, 1), round(py, 1), round(pz, 1)],
                "requires_replan": False,
                "note": "当前位于封闭掩体/探矿穴内且周边怪物均被岩层阻隔（无视线接触），原地保留探矿穴，切勿弃坑逃跑。",
            }

        resolved_threat_pos: list[float] | None = None
        if isinstance(threat_pos, (list, tuple)) and len(threat_pos) >= 3:
            resolved_threat_pos = [float(threat_pos[0]), float(threat_pos[1]), float(threat_pos[2])]
        elif threat["unoccluded"]:
            resolved_threat_pos = list(threat["unoccluded"][0]["pos"])

        # Skeleton/creeper at depth: never leave our own tunnel. Wall off the threat side, then
        # retreat along the recorded waypoints instead of free-pathfinding into open cave air.
        eng = self._mining_dfs
        if eng is not None and getattr(eng, "nodes", None):
            retreat = eng.retreat_into_shaft(
                min_hp_ref=[float(st0.get("health") or 20.0)],
                threat_pos=tuple(resolved_threat_pos) if resolved_threat_pos else None,
            )
            if retreat.get("ok"):
                return retreat
        else:
            nh = st0.get("nearest_hostile")
            if isinstance(nh, dict) and isinstance(nh.get("pos"), (list, tuple)) and len(nh["pos"]) >= 3:
                resolved_threat_pos = [float(nh["pos"][0]), float(nh["pos"][1]), float(nh["pos"][2])]
            elif threat["occluded"]:
                resolved_threat_pos = list(threat["occluded"][0]["pos"])

        if enclosure.get("structure") == "carved_shaft" and resolved_threat_pos is not None:
            sealed = self._try_seal_shaft_threat(pos0, enclosure, resolved_threat_pos)
            if sealed is not None:
                return sealed
            target_d = min(target_d, 8.0)

        hx: float | None = resolved_threat_pos[0] if resolved_threat_pos is not None else None
        hz: float | None = resolved_threat_pos[2] if resolved_threat_pos is not None else None

        if hx is not None and hz is not None:
            dx, dz = px - hx, pz - hz
            norm = math.hypot(dx, dz)
            ux, uz = (dx / norm, dz / norm) if norm > 1e-4 else (0.0, -1.0)
        else:
            ux, uz = 0.0, -1.0

        def _rotated_vecs() -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            for deg in (0, 45, -45, 90, -90, 135, -135, 180):
                rad = math.radians(deg)
                c, s = math.cos(rad), math.sin(rad)
                out.append((ux * c - uz * s, ux * s + uz * c))
            return out

        rotated = _rotated_vecs()
        gf_enabled = geofence is not None and bool(getattr(geofence, "enabled", False))
        if gf_enabled:
            allowed = [
                (rx, rz)
                for rx, rz in rotated
                if geofence.check_point(px + rx * target_d, pz + rz * target_d, action="mc_skill_flee") is None
            ]
            if not allowed:
                short_d = min(target_d, 6.0)
                allowed = [
                    (rx, rz)
                    for rx, rz in rotated
                    if geofence.check_point(px + rx * short_d, pz + rz * short_d, action="mc_skill_flee") is None
                ]
            candidates = allowed or rotated
        else:
            candidates = rotated

        rx0, rz0 = candidates[0]
        try:
            self.bot.call(
                "control",
                {
                    "forward": True,
                    "sprint": True,
                    "jump": True,
                    "yaw": _yaw_toward(px, pz, px + rx0 * 10.0, pz + rz0 * 10.0),
                    "ms": 1200,
                    "stop_on": ["death"],
                },
            )
        except ApiError:
            pass

        st_mid = self.bot.status()
        if st_mid.get("alive") is False:
            return {"ok": False, "mode": "flee", "error": "dead", "requires_replan": True}

        mid_pos = st_mid.get("pos") or pos0
        moved = math.hypot(float(mid_pos[0]) - px, float(mid_pos[2]) - pz)
        nh_mid = st_mid.get("nearest_hostile")
        nh_mid_dist = float(nh_mid.get("dist", 99.0)) if isinstance(nh_mid, dict) else 99.0

        if nh_mid_dist < 9.0 or moved < 4.5:
            mv_ok = False
            for rx, rz in candidates[:2]:
                try:
                    self.bot.call(
                        "move_to",
                        {
                            "x": round(px + rx * target_d, 1),
                            "z": round(pz + rz * target_d, 1),
                            "range": 2.0,
                            "stop_on": ["death"],
                            "timeout": 2800 if urgent else 7000,
                        },
                    )
                    mv_ok = True
                    break
                except ApiError:
                    continue
            if not mv_ok:
                rx1, rz1 = candidates[1] if len(candidates) > 1 else candidates[0]
                cur = self.bot.status().get("pos") or mid_pos
                cx, cz = float(cur[0]), float(cur[2])
                try:
                    self.bot.call(
                        "control",
                        {
                            "forward": True,
                            "sprint": True,
                            "jump": True,
                            "yaw": _yaw_toward(cx, cz, cx + rx1 * 10.0, cz + rz1 * 10.0),
                            "ms": 900,
                            "stop_on": ["death"],
                        },
                    )
                except ApiError:
                    pass

        st_end = self.bot.status()
        end_pos = st_end.get("pos") or mid_pos
        moved_dist = round(math.hypot(float(end_pos[0]) - px, float(end_pos[2]) - pz), 1)
        nh_after = st_end.get("nearest_hostile")
        nh_end_dist = float(nh_after.get("dist", 99.0)) if isinstance(nh_after, dict) else 99.0
        # A 7000-block "flee" is a death/respawn teleport, not a successful escape: never report it
        # as ok, or the model believes it got away when in fact it lost its gear.
        respawned = moved_dist > 64.0 or st_end.get("alive") is False
        return {
            "ok": False if respawned else (bool(st_end.get("alive", True)) and (nh_end_dist >= 7.0 or moved_dist >= 4.5)),
            "mode": "respawned" if respawned else "flee",
            "moved_dist": moved_dist,
            "respawned": respawned,
            "nearest_hostile_after": nh_after,
            "pos_before": [round(px, 1), round(py, 1), round(pz, 1)],
            "pos_after": end_pos,
            "requires_replan": True,
            **(
                {"note": "检测到位置瞬移超过 64 格：这是死亡重生，不是逃跑成功。装备已掉落，先确认背包再决定重挖还是重做工具。"}
                if respawned
                else {}
            ),
        }


    def defend_or_flee(
        self,
        *,
        attacker: dict[str, Any] | None = None,
        counts: dict[str, int] | None = None,
        health: float | None = None,
        urgent: bool = False,
        geofence: Any | None = None,
    ) -> dict[str, Any]:
        """Counter-attack a lone unoccluded melee zombie/spider when well-armed, else seal shaft or sprint-flee."""
        if counts is None:
            counts = _inventory_counts(self.bot)
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        if health is None:
            hp = float(st0.get("health") or 0.0)
        else:
            hp = float(health)

        threat = assess_hostile_occlusion(self.bot, pos0, radius=12, fallback_nearest=attacker)
        unoccluded = threat["unoccluded"]
        occluded = threat["occluded"]

        best_weapon = best_melee_weapon_in_inv(counts)
        danger_mobs = {"creeper", "enderman", "warden", "witch"}
        danger_mob_near = any(
            str(e.get("name") or "").removeprefix("minecraft:") in danger_mobs and float(e.get("dist", 99.0)) <= 8.0
            for e in unoccluded
        )
        close_unblocked = [e for e in unoccluded if float(e.get("dist", 99.0)) <= 6.5]

        target: dict[str, Any] | None = None
        target_occluded = False
        att_id = attacker.get("id") if isinstance(attacker, dict) else None
        if att_id is not None:
            match_u = next((e for e in unoccluded if e.get("id") == att_id), None)
            if match_u is not None:
                target = match_u
            else:
                match_o = next((e for e in occluded if e.get("id") == att_id), None)
                if match_o is not None:
                    target = match_o
                    target_occluded = True
                elif not threat["complete"]:
                    target = attacker
        elif len(close_unblocked) == 1:
            target = close_unblocked[0]

        t_id = target.get("id") if isinstance(target, dict) else None
        t_name = str(target.get("name") or "").removeprefix("minecraft:") if isinstance(target, dict) else ""
        t_dist = float(target.get("dist", 3.0)) if isinstance(target, dict) else 99.0
        t_pos = target.get("pos") if isinstance(target, dict) else None

        can_counter = (
            best_weapon is not None
            and hp >= 11.0
            and not danger_mob_near
            and len(close_unblocked) <= 1
            and t_id is not None
            and not target_occluded
            and t_name in SAFE_MELEE_MOBS
            and t_dist <= 5.5
        )
        if can_counter:
            try:
                self.bot.call("equip", {"item": best_weapon, "slot": "hand"})
            except ApiError:
                pass
            try:
                atk_body: dict[str, Any] = {
                    "entity_id": int(t_id),
                    "timeout": 4200 if urgent else 12000,
                    "feedback": True,
                }
                if t_dist <= 3.2:
                    atk_body["chase"] = False
                atk_res = self.bot.call("attack", atk_body)
                st_after = self.bot.status()
                nh_after = st_after.get("nearest_hostile")
                nh_after_dist = float(nh_after.get("dist", 99.0)) if isinstance(nh_after, dict) else 99.0
                killed = bool(atk_res.get("killed")) or not (
                    isinstance(nh_after, dict) and nh_after.get("id") == t_id
                )
                if killed or nh_after_dist >= 6.0:
                    return {
                        "ok": True,
                        "mode": "counter_attack",
                        "weapon": best_weapon,
                        "target": t_name,
                        "entity_id": int(t_id),
                        "killed": killed,
                        "attack": atk_res,
                        "pos_after": st_after.get("pos"),
                        "requires_replan": True,
                    }
            except ApiError:
                pass

        return self.flee_hostiles(
            distance=14.0 if not can_counter else 12.0,
            urgent=urgent,
            geofence=geofence,
            threat_pos=t_pos if isinstance(t_pos, list) else None,
        )


    def emergency_death_loop_shelter(
        self,
        *,
        hold_until_mono: float,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Shelter underground/bunker until the rolling 60s 3-death kick window clears."""
        st0 = self.bot.status()
        pos0 = st0.get("pos") or [0.0, 64.0, 0.0]
        enclosure = inspect_local_enclosure(self.bot, pos0)
        sheltered = enclosure.get("structure") in {"sealed_bunker", "carved_shaft"}
        bunker_res: dict[str, Any] = {"ok": True, "already_sheltered": sheltered, "structure": enclosure.get("structure")}
        flee_res: dict[str, Any] | None = None

        if not sheltered:
            try:
                self.descend_canopy()
            except Exception:
                pass
            bunker_res = self.dig_safety_bunker(urgent=True)
            sheltered = bool(
                bunker_res.get("ok")
                or (bunker_res.get("inside_shaft") and bunker_res.get("roof_placed"))
            )
            if not sheltered:
                flee_res = self.flee_hostiles(distance=16.0, urgent=True)
                bunker_res = self.dig_safety_bunker(urgent=True)
                sheltered = bool(
                    bunker_res.get("ok")
                    or (bunker_res.get("inside_shaft") and bunker_res.get("roof_placed"))
                )

        held_s = 0.0
        if sheltered:
            wait_deadline = min(float(hold_until_mono), time.monotonic() + 55.0)
            started = time.monotonic()
            while True:
                if stop_check is not None and stop_check():
                    raise KeyboardInterrupt("防死亡循环掩体驻留期间收到停止信号")
                rem = wait_deadline - time.monotonic()
                if rem <= 0:
                    break
                time.sleep(min(0.25, rem))
            held_s = round(max(0.0, time.monotonic() - started), 2)

        return {
            "ok": sheltered,
            "sheltered": sheltered,
            "held_s": held_s,
            "bunker": bunker_res,
            "flee": flee_res,
        }


    @property
    def mining_dfs(self) -> Any:
        if self._mining_dfs is None:
            from ..mining_dfs import MiningDFSEngine

            self._mining_dfs = MiningDFSEngine(self)
        return self._mining_dfs


    def mine_dfs(
        self,
        target: str = "iron",
        goal_count: int = 6,
        target_y: int | None = None,
        action: str = "explore",
        geofence: Any = None,
        max_duration_s: float = 32.0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """Run path-compressed MiningDFS underground exploration, vein harvesting, or collar backtrack."""
        previous_reports = self._dfs_storage_reports
        reports: list[dict[str, Any]] = []
        self._dfs_storage_reports = reports
        try:
            result = self.mining_dfs.run(
                target=target,
                goal_count=goal_count,
                target_y=target_y,
                action=action,
                geofence=geofence,
                max_duration_s=max_duration_s,
                strategy=strategy,
            )
        finally:
            self._dfs_storage_reports = previous_reports
        if reports:
            result["inventory_cleanup_during_mining"] = reports
        return result


    def ensure_items(
        self,
        targets: dict[str, int] | list[dict[str, Any]],
        *,
        max_duration_s: float = 180.0,
        equip_slots: dict[str, str] | None = None,
        geofence: Any = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Deterministically resolve objective vanilla recipes, smelting, and harvest tiers with 0 LLM calls."""
        from ..ensure import PhysicalPrerequisiteResolver

        resolver = PhysicalPrerequisiteResolver(self, geofence=geofence, stop_check=stop_check)
        return resolver.ensure_items(targets, max_duration_s=max_duration_s, equip_slots=equip_slots)


    def cast_obsidian(
        self, x: int, y: int, z: int, *, max_duration_s: float = 15.0,
    ) -> dict[str, Any]:
        """Cool a reachable lava source from dry footing and recover the placed water."""
        session = _SkillBudgetSession(self.bot, max_duration_s, single_attempt=True)
        scoped = copy(self)
        scoped.bot = session
        try:
            return scoped._cast_obsidian((x, y, z), session)
        except (AuthError, DeathLoopCooldownError):
            raise
        except (_SkillDeadline, httpx.TimeoutException):
            return {
                **session.progress, "ok": False, "requires_replan": True,
                "error": "obsidian_cast_time_budget", "stop_confirmed": session.stop_pending(),
                "message": "浇铸预算已用尽。请核对岩浆格、浇水格与背包，再决定是否继续。",
            }
        except (ApiError, httpx.HTTPError) as err:
            return {
                **session.progress, "ok": False, "requires_replan": True,
                "error": getattr(err, "code", type(err).__name__), "message": str(err),
            }


    def _cast_obsidian(self, target: tuple[int, int, int], session: _SkillBudgetSession) -> dict[str, Any]:
        from ..combat_env import CombatEnv

        air = {"air", "cave_air", "void_air"}
        fluids = {"lava", "flowing_lava", "water", "flowing_water"}
        rocks = {*SHAFT_SEAL_BLOCKS, "stone", "deepslate", "blackstone", "basalt", "obsidian", "bedrock", "sandstone"}
        neighbors = ((1, 0), (-1, 0), (0, 1), (0, -1))
        x, y, z = target
        progress = session.progress
        progress.update(lava_pos=list(target), phase="preflight")

        def fail(code: str, message: str) -> dict[str, Any]:
            return {**progress, "ok": False, "requires_replan": True, "error": code, "message": message}

        def read(pos: tuple[int, int, int]) -> tuple[str, Any]:
            block = session.call("get_block", {"pos": list(pos)})
            return _clean_block(block.get("name") or block.get("block")), (block.get("properties") or {}).get("level")

        def permitted(pos: tuple[int, int, int]) -> bool:
            return self.geofence is None or not self.geofence.check_point(pos[0] + 0.5, pos[2] + 0.5, action="mc_skill_cast_obsidian")

        status = session.status()
        if not status.get("dimension") or normalize_dimension(status["dimension"]) != "overworld":
            return fail("obsidian_cast_requires_overworld", "请在主世界从岩浆旁的干燥实体平台浇铸。")
        origin = status.get("pos")
        if (
            not isinstance(origin, (list, tuple)) or len(origin) < 3
            or not all(type(v) in (int, float) and math.isfinite(v) for v in origin[:3])
            or status.get("on_ground") is not True or status.get("alive") is False
            or status.get("in_water") or status.get("in_lava")
        ):
            return fail("unsafe_cast_footing", "请站稳在干燥、有支撑的岩浆边缘。")

        def same_station(*, dry: bool) -> bool:
            current = session.status()
            pos = current.get("pos")
            return (
                isinstance(pos, (list, tuple)) and len(pos) >= 3
                and all(type(v) in (int, float) and math.isfinite(v) for v in pos[:3])
                and math.dist(pos[:3], origin[:3]) <= 0.3
                and current.get("dimension") is not None and normalize_dimension(current["dimension"]) == "overworld"
                and current.get("last_death") == status.get("last_death")
                and current.get("alive") is not False and current.get("on_ground") is True
                and not current.get("in_lava") and (not dry or not current.get("in_water"))
            )

        feet = tuple(math.floor(coord) for coord in origin[:3])
        eye = [origin[0], origin[1] + 1.62, origin[2]]
        surface = [x + 0.5, y + 1.0, z + 0.5]
        if y >= feet[1] or math.dist(eye, surface) > 4.0 or feet[::2] == target[::2]:
            return fail("obsidian_cast_out_of_reach", "请从 4 格内的侧边平台对准下方可见岩浆源。")
        if not permitted(target) or not permitted(feet):
            return fail("void_geofence_blocked", "浇铸目标和站位都需要在围栏内。")
        if read((feet[0], feet[1] - 1, feet[2]))[0] not in rocks:
            return fail("unsafe_cast_footing", "脚下支撑未确认，请换到实体石质平台。")

        original, lava_level = read(target)
        if original != "obsidian" and (
            original != "lava" or str(lava_level) != "0" or read((x, y + 1, z))[0] not in air
        ):
            return fail("obsidian_cast_requires_exposed_source", "请选择上方空旷且状态为 level=0 的岩浆源。")
        counts = _inventory_counts(session)
        if _best_pickaxe_in_inv(counts)[1] < PICKAXE_TIERS["diamond_pickaxe"]:
            return fail("tool_tier_insufficient", "请先准备钻石镐或下界合金镐，才能收取黑曜石。")

        if original != "obsidian":
            if counts.get("water_bucket", 0) < 1:
                return fail("missing_water_bucket", "请先用桶盛静止水源。")

            terrain = CombatEnv(session, geofence=self.geofence)
            for dx, dz in neighbors:
                support = (x + dx, y, z + dz)
                water = (support[0], y + 1, support[2])
                top = [support[0] + 0.5, y + 0.99, support[2] + 0.5]
                # The pour must reach a separate top face without stepping toward the lava.
                if support[::2] == feet[::2] or not permitted(support) or math.dist(eye, top) > 3.5:
                    continue
                if read(support)[0] in rocks and read(water)[0] in air and terrain.has_los(origin, top, dst_height=0)["clear"]:
                    break
            else:
                return fail("no_safe_water_support", "岩浆源旁没有可从当前位置点击的干燥实体顶面。请换一个岩浆源或站位。")
            if not same_station(dry=True):
                return fail("unsafe_cast_footing", "探查时站位已变化。请重新站稳并确认目标。")

            progress.update(phase="pour_water", water_pos=list(water), support_pos=list(support))
            session.call("interact", {"pos": list(support), "item": "water_bucket"}, timeout_s=3.0)
            progress["phase"] = "verify_cast"
            placed = False
            for _ in range(5):
                water_name, level = read(water)
                placed = water_name == "water" and str(level) == "0"
                progress["obsidian_confirmed"] = read(target)[0] == "obsidian"
                if placed and progress["obsidian_confirmed"]:
                    break
                time.sleep(min(0.25, max(0.0, session.deadline - time.monotonic())))
            if not placed:
                return fail("water_placement_unconfirmed", "未在预期位置确认新水源。请先检查实际落水位置和桶，避免重复浇水。")

            before_scoop = _inventory_counts(session)
            if before_scoop.get("bucket", 0) < 1 or not same_station(dry=False):
                return fail("water_recovery_deferred", "已确认水源坐标。请站稳并确认手上有空桶后再回收。")
            water_name, level = read(water)
            if water_name != "water" or str(level) != "0":
                return fail("water_recovery_unconfirmed", "水源已变化，请检查原位和水桶。")
            progress["phase"] = "recover_water"
            session.call("interact", {"pos": list(water), "item": "bucket"}, timeout_s=3.0)
            progress["water_recovered"] = False
            for _ in range(3):
                water_name, level = read(water)
                if (water_name != "water" or str(level) != "0") and _inventory_counts(session).get("water_bucket", 0) > before_scoop.get("water_bucket", 0):
                    progress["water_recovered"] = True
                    break
                time.sleep(min(0.2, max(0.0, session.deadline - time.monotonic())))
            if not progress["water_recovered"]:
                return fail("water_recovery_unconfirmed", "水桶未确认回包，请重新观察原位和背包。")

        progress["phase"] = "check_mining_face"
        progress["obsidian_confirmed"] = read(target)[0] == "obsidian"
        if not progress["obsidian_confirmed"]:
            return fail("obsidian_conversion_unconfirmed", "目标格未确认变成黑曜石。请换可见的静止岩浆源。")
        exposed = read((x, y + 1, z))[0] in air and read((x, y + 2, z))[0] in air and read((x, y - 1, z))[0] in rocks
        nearby_fluid = exposed and any(read((x + dx, y, z + dz))[0] in fluids | {""} for dx, dz in neighbors)
        if not exposed or nearby_fluid or not same_station(dry=True):
            return {
                **progress, "ok": True, "mine_ready": False, "requires_replan": True,
                "message": "黑曜石已确认。请等残水退去，核实底部支撑并封住相邻岩浆后再采集。",
            }
        return {
            **progress, "ok": True, "mine_ready": True,
            "next_actions": [{"name": "mc_skill_mine_blocks", "arguments": {"block_types": ["obsidian"], "count": 1, "radius": 4}}],
        }


    def build_nether_portal(
        self, *, enter: bool = True, wait_s: float = 10.0, max_duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Deterministically clear, build a 4x5 obsidian Nether portal, ignite it, and step inside."""
        if max_duration_s is None:
            return self._build_nether_portal(enter, wait_s)
        session = _SkillBudgetSession(self.bot, max_duration_s, single_attempt=True)
        scoped = copy(self)
        scoped.bot = session
        try:
            return scoped._build_nether_portal(enter, wait_s)
        except (_SkillDeadline, httpx.TimeoutException):
            action_released = session.stop_pending()
            return {
                "ok": False, "requires_replan": True, "entered_nether": False,
                **session.progress,
                "error": {
                    "code": "portal_time_budget", "phase": session.progress.get("phase", "preflight"),
                    "message": "建门预算已用尽。请在原门附近核对现有框格后继续。",
                },
                **({"action_released": True} if action_released else {}),
            }


    def _build_nether_portal(self, enter: bool, wait_s: float) -> dict[str, Any]:
        session = self.bot if isinstance(self.bot, _SkillBudgetSession) else None
        progress = session.progress if session is not None else {}
        st = self.bot.status()
        dim = str(st.get("dimension") or "").lower()
        if "nether" in dim:
            return {
                "ok": True,
                "already_in_nether": True,
                "entered_nether": True,
                "dimension": dim,
                "message": "当前已身处下界(the_nether)，无需再搭传送门。",
            }

        entry_detail: dict[str, Any] = {}

        def _walk_into_portal(portal_blocks: list[dict[str, Any]]) -> tuple[bool, str]:
            progress.update(phase="entry", portal_ignited=True)
            # A pathfinder success only confirms its goal radius. Check actual
            # portal contact and use short controls when the portal plane is avoided.
            blocks = sorted(
                (b for b in portal_blocks if isinstance(b, dict)
                 and isinstance(b.get("pos"), (list, tuple)) and len(b["pos"]) >= 3),
                key=lambda b: (float(b["pos"][1]), float(b.get("dist", 99.0))),
            )
            if not blocks:
                entry_detail["error"] = {"code": "portal_not_found", "phase": "entry"}
                return False, dim
            target = [math.floor(float(v)) for v in blocks[0]["pos"][:3]]
            tx, ty, tz = target
            entry_detail["entry_target"] = target
            deadline = time.monotonic() + max(4.0, min(30.0, float(wait_s)))
            if session is not None:
                deadline = min(deadline, session.deadline)
            dim_now = dim

            def _entry_call(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ApiError("portal_entry_timeout", "入门预算耗尽")
                cap = min(float(kwargs.pop("timeout_s", 3.0)), remaining)
                if path == "move_to":
                    body = {**(body or {}), "timeout": max(1, int(max(0.001, cap - 0.2) * 1000))}
                return self.bot.call(path, body, timeout_s=cap, **kwargs)

            def _status() -> dict[str, Any]:
                nonlocal dim_now
                real_bot = session.bot if session is not None else self.bot
                st_now = (_entry_call("status", method="GET", timeout_s=2.0)
                          if isinstance(real_bot, BotSession) else self.bot.status())
                dim_now = str(st_now.get("dimension") or "").lower()
                entry_detail["entry_pos"] = st_now.get("pos")
                return st_now

            try:
                # Break the vegetation/terrain on the nearest face, without
                # touching the frame, portal, or its supporting floor.
                block = _entry_call("get_block", {"pos": target})
                axis = (block.get("properties") or {}).get("axis", "x")
                normal = (0, 1) if axis == "x" else (1, 0)
                cur = _status().get("pos") or st.get("pos") or target
                sign = -1 if (float(cur[0]) - tx - 0.5) * normal[0] + (float(cur[2]) - tz - 0.5) * normal[1] <= 0 else 1
                ax, az = tx + sign * normal[0], tz + sign * normal[1]
                for y_cell in (ty, ty + 1, ty + 2):
                    cell = [ax, y_cell, az]
                    name = str(_entry_call("get_block", {"pos": cell}).get("name") or "").removeprefix("minecraft:")
                    if name in {"water", "lava", "flowing_water", "flowing_lava", "bedrock", "obsidian"}:
                        entry_detail["error"] = {"code": "portal_approach_blocked", "phase": "entry", "pos": cell, "block": name}
                        return False, dim_now
                    if name and name not in {"air", "cave_air", "void_air", "nether_portal", "fire"}:
                        _entry_call("dig", {"pos": cell, "collect": False}, timeout_s=min(3.0, max(0.05, deadline - time.monotonic())))
                try:
                    _entry_call("move_to", {"x": tx + 0.5, "y": ty, "z": tz + 0.5,
                                             "range": 0.2, "timeout": 2500}, timeout_s=3.0)
                except ApiError as err:
                    entry_detail["move_error"] = {"code": err.code, "message": err.message}
                contact_seen = False
                pulses = 0
                approach_recovered = False
                while time.monotonic() < deadline:
                    now = _status()
                    if "nether" in dim_now:
                        return True, dim_now
                    cur = now.get("pos") or []
                    if len(cur) < 3:
                        break
                    cx, cy, cz = map(float, cur[:3])
                    in_portal = abs(cx - tx - 0.5) <= 0.45 and abs(cz - tz - 0.5) <= 0.45 and ty <= cy < ty + 1
                    contact_seen = contact_seen or in_portal
                    normal_distance = abs((cx - tx - 0.5) * normal[0] + (cz - tz - 0.5) * normal[1])
                    lateral_distance = abs((cx - tx - 0.5) * normal[1] + (cz - tz - 0.5) * normal[0])
                    if (not approach_recovered and 1.1 < ty - cy <= 2.1
                            and 0.7 <= normal_distance <= 1.3 and lateral_distance <= 0.6):
                        # r12: pathfinding can fall to a low approach floor two
                        # blocks below the portal. One verified jump-place makes
                        # a one-block stair, without touching its obsidian bar.
                        floor_pos = [math.floor(cx), math.floor(cy) - 1, math.floor(cz)]
                        floor = str(_entry_call("get_block", {"pos": floor_pos}).get("name") or "").removeprefix("minecraft:")
                        if not floor or is_passable_sight_block(floor) or floor in {"lava", "flowing_lava", "magma_block", "cactus"}:
                            entry_detail["error"] = {"code": "portal_approach_unsupported", "phase": "entry", "pos": floor_pos, "block": floor}
                            return False, dim_now
                        approach_recovered = True
                        recovery = self.dig_staircase(vertical="pillar", steps=1, stop_on_ore=False,
                                                      deadline=deadline, per_step_timeout=3.0)
                        entry_detail["entry_recovery"] = {key: recovery.get(key) for key in ("ok", "steps_done", "pos_after", "stop_reason")}
                        if not recovery.get("ok"):
                            entry_detail["error"] = {"code": "portal_approach_step_failed", "phase": "entry", "cause": recovery.get("stop_reason")}
                            return False, dim_now
                        continue
                    if not in_portal and pulses < 8:
                        # Steer only within a single step of the portal floor.
                        if (cy < ty - 1.1 or cy > ty + 0.6) or math.hypot(cx - tx - 0.5, cz - tz - 0.5) > 2.2:
                            break
                        _entry_call("look", {"x": tx + 0.5, "y": cy + 1.62, "z": tz + 0.5}, timeout_s=1.0)
                        _entry_call("control", {"forward": True, "jump": cy < ty - 0.4, "ms": 150, "stop_on": ["hurt", "death"]}, timeout_s=1.0)
                        pulses += 1
                    else:
                        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                entry_detail["error"] = {
                    "code": "portal_transfer_timeout" if contact_seen else "portal_entry_not_reached",
                    "phase": "entry", "target": target, "contact_seen": contact_seen,
                    "message": (
                        "已检测到踏入传送门，但维度切换在预算内未完成；服务器可能延迟。"
                        "请先查询 status.dimension 等待传送，不要重复建门或再次入门。"
                        if contact_seen
                        else "未检测到踏入传送门；请检查门前两格净空、脚下支撑及 entry_pos，不要重复建门。"
                    ),
                }
            except (ApiError, httpx.HTTPError) as err:
                if isinstance(err, httpx.TimeoutException):
                    if session is not None:
                        raise
                    try:
                        self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
                    except (ApiError, httpx.HTTPError):
                        pass
                entry_detail["error"] = {"code": "portal_entry_failed", "phase": "entry", "cause": getattr(err, "code", type(err).__name__)}
            return False, dim_now

        # 0) If a lit nether_portal already exists within 12 blocks, walk straight into it without needing 10 obsidian!
        try:
            existing = self.bot.call("find_blocks", {"types": ["nether_portal"], "radius": 12, "limit": 8})
            existing_blocks = existing.get("blocks") or []
        except ApiError:
            existing_blocks = []
        if existing_blocks:
            if not enter:
                return {
                    "ok": True,
                    "reused_existing_portal": True,
                    "portal_ignited": True,
                    "entered_nether": False,
                    "dimension": dim,
                    "requires_replan": False,
                }
            entered_now, dim_now = _walk_into_portal(existing_blocks)
            return {
                "ok": bool(entered_now),
                "reused_existing_portal": True,
                "portal_ignited": True,
                "entered_nether": entered_now,
                "dimension": dim_now,
                "requires_replan": not entered_now,
                **entry_detail,
            }

        if bool(st.get("in_water")):
            try:
                self.escape_water()
                st = self.bot.status()
            except ApiError:
                pass

        raw_pos = st.get("pos")
        try:
            pos = [float(coord) for coord in raw_pos[:3]] if isinstance(raw_pos, (list, tuple)) else []
        except (TypeError, ValueError):
            pos = []
        if len(pos) != 3 or not all(map(math.isfinite, pos)):
            return {
                "ok": False, "requires_replan": True,
                "error": {"code": "portal_position_unverified", "phase": "preflight", "message": "请先确认当前站位再建门。"},
            }
        px, py, pz = map(math.floor, pos)
        dimension = normalize_dimension(st.get("dimension"))
        remembered = self._unfinished_portal_frames.get(dimension)
        origin = (px - 1, py, pz + 2)
        if remembered is not None:
            ox, oy, oz = remembered
            if abs(py - oy) <= 2 and math.hypot(pos[0] - ox - 1.5, pos[2] - oz - 0.5) <= 6.0:
                origin = remembered
        bx, by, bz = origin
        frame_steps = [
            ((bx + 1, by, bz), True),
            ((bx + 2, by, bz), True),
            ((bx, by, bz), False),
            ((bx + 3, by, bz), False),
            *(((x, y, bz), True) for x in (bx, bx + 3) for y in range(by + 1, by + 4)),
            ((bx, by + 4, bz), False),
            ((bx + 1, by + 4, bz), True),
            ((bx + 2, by + 4, bz), True),
        ]

        def _blk_name(x: int, y: int, z: int) -> str:
            try:
                block = self.bot.call("get_block", {"pos": [x, y, z]})
            except ApiError as err:
                if err.code in _PICKUP_INTERRUPTION_CODES:
                    raise
                return ""
            return _clean_block(block.get("name") or block.get("block"))

        progress.update(portal_origin=list(origin), portal_ignited=False, phase="preflight")
        frame = {cell: _blk_name(*cell) for cell, required in frame_steps if required}
        existing = {cell for cell, name in frame.items() if name == "obsidian"}
        progress["existing_obsidian"] = len(existing)
        progress["placed_obsidian"] = 0
        unknown = [list(cell) for cell, name in frame.items() if not name]
        if unknown:
            return {
                "ok": False, "requires_replan": True, **progress,
                "error": {"code": "portal_frame_unverified", "phase": "preflight", "cells": unknown},
            }

        counts = _inventory_counts(self.bot)
        obs_cnt = counts.get("obsidian", 0)
        fns_cnt = counts.get("flint_and_steel", 0)
        required_obsidian = len(frame) - len(existing)
        if obs_cnt < required_obsidian or fns_cnt < 1:
            return {
                "ok": False, "requires_replan": True, **progress,
                "error": "missing_materials", "required_obsidian": required_obsidian,
                "obsidian": obs_cnt, "flint_and_steel": fns_cnt,
                "message": f"当前门框还缺 {required_obsidian} 格黑曜石，需要一件打火石。背包有 {obs_cnt} 黑曜石、{fns_cnt} 打火石。",
                **({"unfinished_portal_origin": list(remembered)} if remembered and origin != remembered else {}),
            }
        self._unfinished_portal_frames[dimension] = origin
        progress["phase"] = "placement"

        scaffold_candidates = (
            "cobblestone",
            "cobbled_deepslate",
            "dirt",
            "netherrack",
            "stone",
            "oak_planks",
            "spruce_planks",
            "birch_planks",
            "dark_oak_planks",
            "andesite",
            "diorite",
            "granite",
        )
        scaffold_item: str | None = None
        for cand in scaffold_candidates:
            if counts.get(cand, 0) >= 3:
                scaffold_item = cand
                break
        spare_obsidian = obs_cnt - required_obsidian
        if scaffold_item is None and spare_obsidian >= 3:
            scaffold_item = "obsidian"
        if scaffold_item is None:
            for cand in scaffold_candidates:
                if counts.get(cand, 0) >= 1:
                    scaffold_item = cand
                    break
        if scaffold_item is None and spare_obsidian > 0:
            scaffold_item = "obsidian"
        scaffold_left = spare_obsidian if scaffold_item == "obsidian" else counts.get(scaffold_item, 0)

        non_diggable = {
            "air", "cave_air", "void_air", "water", "flowing_water", "lava", "flowing_lava", "fire",
        }
        solid_scaffold_ok = {
            "stone", "cobblestone", "dirt", "grass_block", "deepslate", "cobbled_deepslate",
            "netherrack", "obsidian", "sandstone", "andesite", "diorite", "granite",
            "oak_planks", "spruce_planks", "birch_planks", "dark_oak_planks",
        }

        def _clear_cell(x: int, y: int, z: int) -> None:
            nm = _blk_name(x, y, z)
            if nm and nm not in non_diggable and nm != "obsidian":
                try:
                    self.bot.call("dig", {"pos": [x, y, z]})
                except ApiError:
                    pass

        def _ensure_block(x: int, y: int, z: int, item_name: str, *, support: bool = False) -> str:
            nm = _blk_name(x, y, z)
            if nm == item_name or (support and nm in solid_scaffold_ok):
                return "existing"
            if not nm:
                return "failed"
            if nm not in non_diggable:
                try:
                    self.bot.call("dig", {"pos": [x, y, z]})
                except ApiError:
                    pass
            # Stand near the frame front without colliding with the target block (closer for top row y>=by+3)
            stand_z = float(bz - 1.2) if y >= by + 3 else float(bz - 2.0)
            try:
                self.bot.call("move_to", {"x": bx + 1.5, "y": by, "z": stand_z, "range": 1.5})
            except ApiError:
                pass
            try:
                self.bot.call("place", {"pos": [x, y, z], "item": item_name})
                return "placed"
            except ApiError as err:
                if err.code in _PICKUP_INTERRUPTION_CODES:
                    raise
                if _blk_name(x, y, z) == item_name:
                    return "existing"
                # Retry once after stepping closer to the specific column
                try:
                    self.bot.call("move_to", {"x": float(x), "y": float(by), "z": float(bz - 1.2), "range": 1.2})
                    self.bot.call("place", {"pos": [x, y, z], "item": item_name})
                    return "placed"
                except ApiError as retry_error:
                    if retry_error.code in _PICKUP_INTERRUPTION_CODES:
                        raise
                    return "failed"

        # 1) Clear standing space in front and portal interior
        for cx in (bx + 1, bx + 2):
            for cy in (by + 1, by + 2, by + 3):
                _clear_cell(cx, cy, bz)
            for cy in (by, by + 1):
                _clear_cell(cx, cy, bz - 1)

        # 2) Ensure solid floor beneath bottom bar & corners
        if scaffold_item:
            for fx in range(bx, bx + 4):
                if scaffold_left and _ensure_block(fx, by - 1, bz, scaffold_item, support=True) == "placed":
                    scaffold_left -= 1

        # 3) Build 4x5 frame (bottom bar -> bottom corners -> left/right pillars -> top corner -> top bar)
        failed_cells: list[list[int]] = []
        progress["failed_cells"] = failed_cells
        for cell, required in frame_steps:
            if not required:
                if scaffold_item and scaffold_left and _ensure_block(*cell, scaffold_item, support=True) == "placed":
                    scaffold_left -= 1
                continue
            outcome = _ensure_block(*cell, "obsidian")
            if outcome == "existing":
                existing.add(cell)
            else:
                existing.discard(cell)
                if outcome == "placed":
                    progress["placed_obsidian"] += 1
                else:
                    failed_cells.append(list(cell))
            progress["existing_obsidian"] = len(existing)

        progress["obsidian_consumed"] = max(0, obs_cnt - _inventory_counts(self.bot).get("obsidian", 0))

        # Re-clear interior in case any stray block remains
        for cx in (bx + 1, bx + 2):
            for cy in (by + 1, by + 2, by + 3):
                _clear_cell(cx, cy, bz)

        if failed_cells:
            return {
                "ok": False, "requires_replan": True, **progress,
                "portal_ignited": False, "entered_nether": False,
                "error": {"code": "portal_frame_incomplete", "phase": "placement", "failed_cells": failed_cells},
            }

        # 4) Ignite the bottom interior of the frame
        progress["phase"] = "ignition"
        try:
            self.bot.call("move_to", {"x": bx + 1.5, "y": by, "z": bz - 2.0, "range": 1.8})
        except ApiError:
            pass
        ignited = False
        lit_blocks: list[dict[str, Any]] = []
        for ix in (bx + 1, bx + 2):
            try:
                self.bot.call("interact", {"pos": [ix, by, bz], "item": "flint_and_steel"})
            except ApiError:
                pass
            time.sleep(min(0.5, session._remaining()) if session is not None else 0.5)
            try:
                fb = self.bot.call("find_blocks", {"types": ["nether_portal"], "radius": 8, "limit": 6})
                if fb.get("blocks"):
                    ignited = True
                    lit_blocks = list(fb.get("blocks") or [])
                    break
            except ApiError:
                pass

        entered_nether = False
        dim_after = dim
        if ignited:
            self._unfinished_portal_frames.pop(dimension, None)
        if ignited and enter:
            entered_nether, dim_after = _walk_into_portal(
                lit_blocks or [{"pos": [bx + 1, by + 1, bz], "dist": 1.0}]
            )

        return {
            "ok": bool(ignited and (entered_nether or not enter)),
            **progress,
            "portal_ignited": ignited,
            "entered_nether": entered_nether,
            "dimension": dim_after,
            "requires_replan": not (ignited and (entered_nether or not enter)),
            **({"error": {"code": "portal_ignition_failed", "phase": "ignition"}} if not ignited else entry_detail),
        }

