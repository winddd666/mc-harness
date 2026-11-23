"""MacroSkills mixin: escape_unstick (split from legacy library.py)."""

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


class _EscapeUnstickMixin:
    def escape_water(self) -> dict[str, Any]:
        """Float to the water/lava surface, carve an upper notch over a solid shoreline step if needed, and hop onto dry land."""
        st = self.bot.status()
        if not bool(st.get("in_water")) and not bool(st.get("in_lava")):
            return {
                "ok": True,
                "in_water": False,
                "in_lava": False,
                "already_dry": True,
                "pos_after": st.get("pos"),
                "dug": [],
                "control": {},
            }

        if not bool(st.get("jumping")):
            try:
                self.bot.call(
                    "posture",
                    {
                        "sneak": False,
                        "shield": False,
                        "jump": True,
                    },
                )
            except ApiError:
                pass

        water_names = {"water", "flowing_water"}
        fluids = {"water", "flowing_water", "lava", "flowing_lava"}
        passable = {
            "air",
            "cave_air",
            "void_air",
            "short_grass",
            "tall_grass",
            "fern",
            "large_fern",
            "leaf_litter",
            "snow",
            "vine",
            "dead_bush",
            "dandelion",
            "poppy",
        }
        dug: list[dict[str, Any]] = []
        mv_res: dict[str, Any] = {}
        pick_equipped = False

        def _try_dig_above(cx: int, cy: int, cz: int) -> bool:
            nonlocal pick_equipped
            if self._is_block_avoided((cx, cy, cz)):
                return False
            try:
                bname, diggable = self._block_info([cx, cy, cz])
            except ApiError:
                return False
            if bname in passable:
                return True
            if bname in fluids or bname == "bedrock" or not diggable:
                return False
            if not pick_equipped and (
                bname in BLOCK_MIN_PICKAXE_TIER or "stone" in bname or "ore" in bname or "deepslate" in bname
            ):
                pick_equipped = self._equip_if_present(tuple(PICKAXE_TIERS)) is not None
            try:
                self.bot.call("dig", {"pos": [cx, cy, cz], "collect": True, "stop_on": ["death"]})
                dug.append({"pos": [cx, cy, cz], "block": bname})
                return True
            except ApiError as err:
                if _is_protected_err(err):
                    self.protected_positions.add((cx, cy, cz))
                else:
                    self._avoid_temporarily((cx, cy, cz), err.code)
                return False

        def _step_onto_adjacent_bank() -> tuple[dict[str, Any], dict[str, Any]]:
            nonlocal mv_res
            cur = self.bot.status()
            pos = cur.get("pos") or [0.0, 64.0, 0.0]
            px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
            bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

            # Float to water surface layer wy if currently submerged
            wy = by
            for _ in range(4):
                try:
                    above_name, _ = self._block_info([bx, wy + 1, bz])
                except ApiError:
                    break
                if above_name in fluids:
                    try:
                        self.bot.call("control", {"jump": True, "ms": 450, "stop_on": ["death"]})
                    except ApiError:
                        pass
                    wy += 1
                else:
                    break

            cur = self.bot.status()
            pos = cur.get("pos") or [px, float(wy), pz]
            px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
            bx, bz = math.floor(px), math.floor(pz)
            wy = max(wy, math.floor(py))

            # Clear any non-fluid roof/wall overlapping the bot above water surface (never dig wy itself)
            xs = sorted({math.floor(px - 0.35), bx, math.floor(px + 0.35)})
            zs = sorted({math.floor(pz - 0.35), bz, math.floor(pz + 0.35)})
            for cy in (wy + 1, wy + 2):
                for cx in xs:
                    for cz in zs:
                        _try_dig_above(cx, cy, cz)

            # Inspect 4 cardinal banks around (bx, wy, bz), preserving solid step (ex, wy, ez)
            candidates: list[tuple[int, int, int, int, str, str]] = []
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ex, ez = bx + dx, bz + dz
                try:
                    bm1, _ = self._block_info([ex, wy - 1, ez])
                    b0, _ = self._block_info([ex, wy, ez])
                    b1, d1 = self._block_info([ex, wy + 1, ez])
                    b2, d2 = self._block_info([ex, wy + 2, ez])
                except ApiError:
                    continue
                if b1 in (fluids | {"bedrock"}) or b2 in (fluids | {"bedrock"}):
                    continue
                is_step_a = b0 not in (passable | fluids)
                is_step_b = b0 in passable and bm1 not in (passable | fluids)
                if not (is_step_a or is_step_b):
                    continue
                need_dig = (0 if b1 in passable else 1) + (0 if b2 in passable else 1)
                if b1 not in passable and (not d1 or self._is_block_avoided((ex, wy + 1, ez))):
                    continue
                if b2 not in passable and (not d2 or self._is_block_avoided((ex, wy + 2, ez))):
                    continue
                candidates.append((need_dig, 0 if is_step_a else 1, ex, ez, b1, b2))

            candidates.sort(key=lambda item: (item[0], item[1]))
            after_cur = cur
            for _, _, ex, ez, b1, b2 in candidates:
                if b1 not in passable and not _try_dig_above(ex, wy + 1, ez):
                    continue
                if b2 not in passable and not _try_dig_above(ex, wy + 2, ez):
                    continue
                cur2 = self.bot.status()
                p2 = cur2.get("pos") or [px, float(wy), pz]
                yaw = _yaw_toward(float(p2[0]), float(p2[2]), ex + 0.5, ez + 0.5)
                try:
                    mv_res = self.bot.call(
                        "control",
                        {"forward": True, "jump": True, "yaw": yaw, "ms": 1150, "stop_on": ["death"]},
                    )
                except ApiError as err:
                    mv_res = {"error": err.code, "message": err.message}
                after_cur = self.bot.status()
                if not bool(after_cur.get("in_water")) and not bool(after_cur.get("in_lava")):
                    return mv_res, after_cur
            return mv_res, after_cur

        mv_res, st_after = _step_onto_adjacent_bank()

        if bool(st_after.get("in_water")) or bool(st_after.get("in_lava")):
            pos_now = st_after.get("pos") or st.get("pos") or [0.0, 64.0, 0.0]
            by_now = math.floor(float(pos_now[1]))
            shore_blocks: list[dict[str, Any]] = []
            try:
                found = self.bot.call(
                    "find_blocks",
                    {
                        "radius": 16,
                        "types": [
                            "grass_block",
                            "dirt",
                            "sand",
                            "gravel",
                            "stone",
                            "cobblestone",
                            "andesite",
                            "diorite",
                            "granite",
                            "clay",
                            "mud",
                            "netherrack",
                            "blackstone",
                            "basalt",
                            "soul_sand",
                            "soul_soil",
                            "nether_bricks",
                            "obsidian",
                        ],
                        "limit": 30,
                    },
                )
                shore_blocks = list(found.get("blocks") or [])
            except ApiError:
                shore_blocks = []

            shore_blocks.sort(key=lambda b: float(b.get("dist", 99)))
            dry_targets: list[list[int]] = []
            for blk in shore_blocks:
                bpos = blk.get("pos")
                if not isinstance(bpos, list) or len(bpos) < 3:
                    continue
                if abs(int(bpos[1]) - by_now) > 3:
                    continue
                if not self._is_water_covered((int(bpos[0]), int(bpos[1]), int(bpos[2]))):
                    dry_targets.append([int(bpos[0]), int(bpos[1]), int(bpos[2])])
                    if len(dry_targets) >= 3:
                        break

            for tx, _ty, tz in dry_targets:
                try:
                    mv_res = self.bot.call(
                        "move_to",
                        {"x": tx + 0.5, "z": tz + 0.5, "range": 1.2, "stop_on": ["death"]},
                    )
                except ApiError:
                    cur3 = self.bot.status()
                    p3 = cur3.get("pos") or pos_now
                    yaw3 = _yaw_toward(float(p3[0]), float(p3[2]), tx + 0.5, tz + 0.5)
                    try:
                        mv_res = self.bot.call(
                            "control",
                            {"forward": True, "jump": True, "yaw": yaw3, "ms": 1800, "stop_on": ["death"]},
                        )
                    except ApiError as err:
                        mv_res = {"error": err.code, "message": err.message}
                st_after = self.bot.status()
                if not bool(st_after.get("in_water")):
                    break
                mv_res, st_after = _step_onto_adjacent_bank()
                if not bool(st_after.get("in_water")):
                    break

        st_after = self.bot.status()
        still_in_water = bool(st_after.get("in_water"))
        if not still_in_water and bool(st_after.get("jumping")):
            try:
                self.bot.call(
                    "posture",
                    {
                        "sneak": bool(st_after.get("sneaking")),
                        "shield": bool(st_after.get("blocking")),
                        "jump": False,
                    },
                )
            except ApiError:
                pass
            st_after = self.bot.status()
            still_in_water = bool(st_after.get("in_water"))

        return {
            "ok": not still_in_water,
            "in_water": still_in_water,
            "pos_after": st_after.get("pos"),
            "dug": dug,
            "control": mv_res,
        }


    def descend_canopy(self) -> dict[str, Any]:
        """Remove one canopy block at a time after confirming support below it."""
        from ..combat_env import HAZARD_BLOCK_TYPES, _is_solid_ground

        def read_block(x: int, y: int, z: int) -> tuple[str, bool]:
            block = self.bot.call("get_block", {"pos": [x, y, z]})
            name = block.get("name") or block.get("block") or ""
            return str(name).removeprefix("minecraft:"), bool(block.get("diggable"))

        st = self.bot.status()
        pos_before = st.get("pos")
        if (
            not isinstance(pos_before, (tuple, list)) or len(pos_before) < 3
            or st.get("on_ground") is False or st.get("alive") is False or st.get("ready") is False
            or st.get("in_water") or st.get("in_lava")
        ):
            return {"ok": False, "on_canopy": False}
        bx, by, bz = (math.floor(float(p)) for p in pos_before[:3])
        try:
            under, diggable = read_block(bx, by - 1, bz)
        except ApiError:
            return {"ok": False, "on_canopy": False}
        if not under.endswith("_leaves"):
            return {"ok": False, "on_canopy": False, "below": under}

        dug: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            "ok": False, "on_canopy": True, "dug": dug, "pos_before": pos_before, "pos_after": pos_before,
        }
        if isinstance(self.bot, _SkillBudgetSession):
            self.bot.progress["canopy"] = result
        for _ in range(10):
            if not diggable or not under.endswith(("_leaves", "_log")):
                break
            landing_y = None
            for floor_y in range(by - 2, by - 7, -1):
                try:
                    block, _ = read_block(bx, floor_y, bz)
                except ApiError:
                    break
                if not block or block == "unknown" or block in HAZARD_BLOCK_TYPES:
                    break
                # Leaves are solid. Land on the next layer before removing it.
                if _is_solid_ground(block):
                    landing_y = floor_y + 1
                    break
            if landing_y is None:
                result["reason"] = "unsafe_drop"
                break
            target = [bx, by - 1, bz]
            try:
                self.bot.call("dig", {"pos": target, "collect": True, "stop_on": ["hurt", "death"]})
            except ApiError:
                break
            dug.append({"pos": target, "block": under})
            for _ in range(3):
                time.sleep(0.25)
                after = self.bot.status()
                pos = after.get("pos")
                if pos:
                    result["pos_after"] = pos
                if (
                    not pos or after.get("alive") is False or after.get("ready") is False
                    or after.get("dimension") != st.get("dimension") or after.get("last_death") != st.get("last_death")
                    or after.get("in_water") or after.get("in_lava")
                    or (math.floor(pos[0]), math.floor(pos[2])) != (bx, bz)
                ):
                    result.update(ok=False, reason="descent_interrupted")
                    return result
                if abs(float(pos[1]) - landing_y) <= 0.2 and after.get("on_ground") is not False:
                    break
            else:
                result.update(ok=False, reason="descent_not_confirmed")
                break
            result["ok"] = True
            by = landing_y
            try:
                under, diggable = read_block(bx, by - 1, bz)
            except ApiError:
                break
        return result


    def unstick(self, *, max_duration_s: float = 45.0) -> dict[str, Any]:
        """Clear blocks overlapping the bot's 0.6x1.8 floor()-aligned hitbox and step out of pits/corners, canopy, or water.

        All internal get_block/dig/control steps share one hard deadline
        (default 45s) so a stuck server action can never turn this into a
        150~270s call (r22/r23/r24).
        """
        started = time.monotonic()
        budget_s = max(5.0, float(max_duration_s))
        deadline = started + budget_s
        params = inspect.signature(self.bot.call).parameters
        supports_timeout = "timeout_s" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

        class _UnstickDeadline(Exception):
            pass

        timed_out = False

        def _call(
            path: str, body: dict[str, Any] | None = None, cap: float = 3.0, **kwargs: Any
        ) -> dict[str, Any]:
            nonlocal timed_out
            remaining = deadline - time.monotonic()
            if timed_out or remaining <= 0.2:
                timed_out = True
                raise _UnstickDeadline()
            timeout = min(remaining, cap)
            try:
                if supports_timeout:
                    return self.bot.call(path, body, timeout_s=timeout, **kwargs)
                return self.bot.call(path, body)
            except httpx.TimeoutException:
                timed_out = True
                raise _UnstickDeadline() from None

        def _finish(payload: dict[str, Any]) -> dict[str, Any]:
            payload["budget_s"] = round(budget_s, 2)
            if timed_out:
                payload["deadline_hit"] = True
                payload["timed_out"] = True
                payload["requires_replan"] = True
                payload["hint"] = (
                    f"unstick 在 {payload['budget_s']}s 硬预算内未完成（已做{len(payload.get('dug') or [])}步挖除）。"
                    "请改用 mc_move_to(x,z) 短距离 8~12 格换站位、mc_skill_staircase 或 mc_control 短脉冲脱困，"
                    "不要连续重复调用 unstick。"
                )
                try:
                    self.bot.call("stop", {}, timeout_s=1.0, allow_reauth=False)
                except (ApiError, httpx.HTTPError):
                    pass
            payload["elapsed_s"] = round(time.monotonic() - started, 2)
            return payload

        st = self.bot.status()
        pos_before = st.get("pos") or [0.0, 64.0, 0.0]
        px, py, pz = float(pos_before[0]), float(pos_before[1]), float(pos_before[2])
        bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)

        xs = sorted({math.floor(px - 0.38), bx, math.floor(px + 0.38)})
        zs = sorted({math.floor(pz - 0.38), bz, math.floor(pz + 0.38)})
        if bool(st.get("in_water")):
            # Leave time for stop if a water probe or movement exhausts the budget.
            deadline -= 1.0
            bot = self.bot

            class _WaterSession:
                def __init__(self) -> None:
                    self.last_status = st

                def call(
                    self,
                    path: str,
                    body: dict[str, Any] | None = None,
                    *,
                    timeout_s: float | None = None,
                    **kwargs: Any,
                ) -> dict[str, Any]:
                    cap = 15.0 if path == "dig" else 6.0 if path == "move_to" else 3.0
                    if timeout_s is not None:
                        cap = min(cap, float(timeout_s))
                    return _call(path, body, cap=cap, **kwargs)

                def status(self) -> dict[str, Any]:
                    self.last_status = _call("status", method="GET") if isinstance(bot, BotSession) else bot.status()
                    return self.last_status

                def inventory(self) -> dict[str, Any]:
                    return _call("inventory", method="GET") if isinstance(bot, BotSession) else bot.inventory()

            water_session = _WaterSession()
            scoped = copy(self)
            scoped.bot = water_session
            try:
                esc = scoped.escape_water()
                timed_out = time.monotonic() >= deadline
            except _UnstickDeadline:
                esc = {
                    "ok": False,
                    "in_water": bool(water_session.last_status.get("in_water", True)),
                    "pos_after": water_session.last_status.get("pos") or pos_before,
                }
            escaped_water = bool(esc.get("ok"))
            result = {
                "ok": escaped_water,
                "foot_block_before": [bx, by, bz],
                "hitbox_xs": xs,
                "hitbox_zs": zs,
                "pos_before": pos_before,
                "pos_after": esc.get("pos_after", pos_before),
                "dug": esc.get("dug", []),
                "control": esc.get("control", {}),
                "escaped_water": escaped_water,
                "in_water": esc.get("in_water", False),
            }
            if not escaped_water and not timed_out:
                result["requires_replan"] = True
                result["error"] = {
                    "code": "water_escape_failed",
                    "message": "未能离开水体。请用 mc_status 确认位置，再朝可达岸边短移或向上脱困。",
                }
            return _finish(result)

        canopy_session = _SkillBudgetSession(self.bot, deadline - time.monotonic(), single_attempt=True)
        scoped = copy(self)
        scoped.bot = canopy_session
        try:
            canopy = scoped.descend_canopy()
        except (_SkillDeadline, httpx.TimeoutException) as error:
            transport = error.__cause__ if isinstance(error, _SkillDeadline) else error
            if isinstance(transport, httpx.TransportError) and time.monotonic() < canopy_session.deadline:
                # The actor guard records the failed endpoint and any confirmed dig progress.
                raise transport
            canopy = canopy_session.progress.get("canopy") or {}
            timed_out = True
        if timed_out or (canopy.get("on_canopy") and canopy.get("dug")):
            descended = canopy.get("ok") is True and not timed_out
            result = {
                "ok": descended,
                "descended_canopy": descended,
                "foot_block_before": [bx, by, bz],
                "pos_before": pos_before,
                "pos_after": canopy.get("pos_after", pos_before),
                "dug": canopy.get("dug", []),
            }
            if not descended and not timed_out:
                result.update(
                    requires_replan=True,
                    error={
                        "code": canopy.get("reason") or "canopy_descent_unconfirmed",
                        "message": "树冠下撤未确认安全落脚。请先核对当前站位，再选择有支撑的路线。",
                    },
                )
            return _finish(result)

        skip_names = {"air", "cave_air", "void_air", "water", "lava", "bedrock"}
        dug: list[dict[str, Any]] = []
        pick_equipped = False

        def _try_dig_if_solid(cx: int, cy: int, cz: int, *, retries: int = 1) -> None:
            nonlocal pick_equipped, timed_out
            for _attempt in range(retries):
                if timed_out:
                    return
                try:
                    blk = _call("get_block", {"pos": [cx, cy, cz]}, cap=3.0)
                except _UnstickDeadline:
                    return
                except ApiError:
                    return
                bname = str(blk.get("name") or "air")
                if bname in skip_names or not blk.get("diggable", False):
                    return
                if self._is_block_avoided((cx, cy, cz)):
                    return
                if not pick_equipped and (
                    bname in BLOCK_MIN_PICKAXE_TIER or "stone" in bname or "ore" in bname or "deepslate" in bname
                ):
                    try:
                        pick_equipped = self._equip_if_present(tuple(PICKAXE_TIERS)) is not None
                    except (ApiError, httpx.HTTPError):
                        pick_equipped = False
                try:
                    _call("dig", {"pos": [cx, cy, cz], "collect": True, "stop_on": ["death"]}, cap=15.0)
                    dug.append({"pos": [cx, cy, cz], "block": bname})
                except _UnstickDeadline:
                    self._avoid_temporarily((cx, cy, cz), "dig_timeout", 30.0)
                    timed_out = True
                    return
                except ApiError as err:
                    if _is_protected_err(err):
                        self.protected_positions.add((cx, cy, cz))
                    else:
                        self._avoid_temporarily((cx, cy, cz), err.code)
                    return

        # 1. Clear full horizontal hitbox footprint at head (by+1) and feet (by), plus overhead (by+2)
        for cy in (by + 1, by):
            for cx in xs:
                for cz in zs:
                    _try_dig_if_solid(cx, cy, cz, retries=2)
        _try_dig_if_solid(bx, by + 2, bz, retries=2)

        # Probe each exit against the same support rules as the Guardian.
        from ..combat_env import CombatEnv

        def _read_status() -> dict[str, Any]:
            if isinstance(self.bot, BotSession):
                return _call("status", method="GET")
            return self.bot.status()

        mv_res: dict[str, Any] = {}
        exit_status: dict[str, Any] = {}
        exit_move = None
        if not timed_out:
            try:
                exit_status = _read_status()
            except (_UnstickDeadline, ApiError, httpx.HTTPError):
                pass
        exit_pos = exit_status.get("pos")
        if (
            exit_pos and not timed_out and exit_status.get("on_ground") is not False
            and exit_status.get("alive") is not False and exit_status.get("ready") is not False
            and not exit_status.get("in_lava") and not exit_status.get("in_water")
        ):
            projected_clear: set[tuple[int, int, int]] = set()

            def _probe_call(path: str, body: dict[str, Any]) -> dict[str, Any]:
                block = _call(path, body)
                p = tuple(body["pos"])
                if p in projected_clear and block.get("diggable") and not self._is_block_avoided(p):
                    name = str(block.get("name") or "").removeprefix("minecraft:")
                    if name and name not in skip_names | {"flowing_water", "flowing_lava"}:
                        return {**block, "name": "air"}
                return block

            probe = CombatEnv(SimpleNamespace(call=_probe_call), geofence=self.geofence)
            fx, fy, fz = math.floor(exit_pos[0]), math.floor(exit_pos[1]), math.floor(exit_pos[2])
            for dx, dz, yaw in ((1, 0, -math.pi / 2), (0, -1, 0.0), (-1, 0, math.pi / 2), (0, 1, math.pi)):
                if timed_out:
                    break
                route = probe.probe_retreat(exit_pos, yaw, 2.2)
                if route is None and not timed_out:
                    cells = [(fx + dx, y, fz + dz) for y in (fy + 1, fy + 2, fy)]
                    projected_clear.update(cells)
                    probe.invalidate_block_cache()
                    can_clear_exit = probe.probe_retreat(exit_pos, yaw, 2.2) is not None
                    projected_clear.clear()
                    probe.invalidate_block_cache()
                    if can_clear_exit and not timed_out:
                        for cell in cells:
                            _try_dig_if_solid(*cell)
                        route = probe.probe_retreat(exit_pos, yaw, 2.2)
                if route is not None:
                    exit_move = {"forward": True, "jump": route[1], "yaw": yaw, "ms": 350, "stop_on": []}
                    break

        if exit_move is not None and not timed_out:
            try:
                mv_res = _call("control", exit_move, cap=3.0)
            except _UnstickDeadline:
                mv_res = {"error": "deadline", "message": "control skipped: unstick budget exhausted"}
            except ApiError as err:
                mv_res = {"error": err.code, "message": err.message}

        st_after = exit_status
        if exit_move is not None and not timed_out:
            try:
                st_after = _read_status()
            except (_UnstickDeadline, ApiError, httpx.HTTPError):
                pass
        result = {
            "ok": bool(dug) or exit_move is not None and not timed_out,
            "foot_block_before": [bx, by, bz],
            "hitbox_xs": xs,
            "hitbox_zs": zs,
            "pos_before": pos_before,
            "pos_after": st_after.get("pos") or mv_res.get("pos") or pos_before,
            "dug": dug,
            "control": mv_res,
        }
        if exit_move is None and not timed_out:
            result["ok"] = False
            result["requires_replan"] = True
            result["error"] = {
                "code": "unsafe_landing",
                "message": "四个方向都未确认安全落脚点。请检查当前位置，再选择有支撑的路线。",
            }
        return _finish(result)


    def dig_safety_bunker(self, *, urgent: bool = False) -> dict[str, Any]:
        """Dig a 1x3 vertical shaft under the bot and seal the top to block night mobs."""
        roof_candidates = ("dirt", "cobblestone", "cobbled_deepslate", "andesite", "diorite", "granite")

        def _pick_roof_item(inv: dict[str, int]) -> str | None:
            for cand in roof_candidates:
                if inv.get(cand, 0) >= 1:
                    return cand
            for name, cnt in sorted(inv.items()):
                if cnt >= 1 and name.endswith("_planks"):
                    return name
            return None

        counts = _inventory_counts(self.bot) if urgent else None
        if counts is not None and _pick_roof_item(counts) is None:
            return {"ok": False, "reason": "no_roof_block"}

        st = self.bot.status()
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))

        fluid_or_bedrock = {"water", "lava", "flowing_water", "flowing_lava", "bedrock"}
        non_solid_floor = {"air", "cave_air", "void_air", "water", "lava", "flowing_water", "flowing_lava"}

        def _block_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            raw = res.get("name") or res.get("block") or "air"
            return str(raw).removeprefix("minecraft:")

        floor_pos = [bx, by - 4, bz]
        floor_block = _block_name(floor_pos)
        if floor_block in non_solid_floor:
            return {
                "ok": False,
                "reason": "unsafe_floor",
                "floor_pos": floor_pos,
                "floor_block": floor_block,
            }

        shaft_positions = [[bx, by - 1, bz], [bx, by - 2, bz], [bx, by - 3, bz]]
        shaft_blocks: list[tuple[list[int], str]] = []
        for s_pos in shaft_positions:
            b_name = _block_name(s_pos)
            if b_name in fluid_or_bedrock:
                return {
                    "ok": False,
                    "reason": "unsafe_shaft_block",
                    "block_pos": s_pos,
                    "block": b_name,
                }
            shaft_blocks.append((s_pos, b_name))

        if urgent:
            if st.get("on_ground") is not True:
                return {"ok": False, "reason": "not_grounded"}
            # Never open a fluid pocket or an existing cave beside an emergency shaft.
            for cy in (by - 1, by - 2, by - 3):
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbor = [bx + dx, cy, bz + dz]
                    name = _block_name(neighbor)
                    if name in non_solid_floor:
                        return {"ok": False, "reason": "unsafe_open_side", "block_pos": neighbor, "block": name}

        if counts is None:
            counts = _inventory_counts(self.bot)
        crafted_planks: str | None = None
        if _pick_roof_item(counts) is None:
            for log_name in (*LOG_TYPES, *NETHER_STEM_TYPES):
                if counts.get(log_name, 0) >= 1:
                    planks_name = _log_to_planks_name(log_name)
                    try:
                        self.bot.call("craft", {"item": planks_name, "count": 4})
                        crafted_planks = planks_name
                    except ApiError as exc:
                        return {
                            "ok": False,
                            "reason": "no_roof_block_craft_failed",
                            "error": exc.code,
                            "message": exc.message,
                        }
                    counts = _inventory_counts(self.bot)
                    break
            if _pick_roof_item(counts) is None:
                return {"ok": False, "reason": "no_roof_block"}

        # A bot straddling the edge remains supported by its neighbor when
        # the nominal foot block is dug. Center it before opening the shaft.
        body_t = (st.get("last_death") or {}).get("t")
        for _ in range(5):
            actual = self.bot.status()
            cur = actual.get("pos") or pos
            if (math.floor(cur[0]), math.floor(cur[1]), math.floor(cur[2])) != (bx, by, bz) or (actual.get("last_death") or {}).get("t") != body_t:
                return {"ok": False, "requires_replan": True, "reason": "position_changed_before_dig", "pos_after": cur}
            if max(abs(cur[0] - bx - 0.5), abs(cur[2] - bz - 0.5)) <= 0.16:
                break
            try:
                result = self.bot.call("control", {
                    "forward": True, "sneak": True,
                    "yaw": _yaw_toward(cur[0], cur[2], bx + 0.5, bz + 0.5),
                    "ms": 120, "stop_on": ["hurt", "death"],
                })
                if result.get("interrupted_by"):
                    return {"ok": False, "requires_replan": True, "reason": "centering_interrupted", "interrupted_by": result["interrupted_by"]}
                time.sleep(0.1)
            except ApiError as exc:
                return {"ok": False, "requires_replan": True, "reason": "centering_failed", "error": exc.code, "message": exc.message}
        else:
            return {"ok": False, "requires_replan": True, "reason": "centering_not_confirmed", "pos_after": self.bot.status().get("pos")}

        if urgent and any(name in BLOCK_MIN_PICKAXE_TIER for _, name in shaft_blocks):
            best, tier = _best_pickaxe_in_inv(counts)
            if best is None or any(BLOCK_MIN_PICKAXE_TIER.get(name, 0) > tier for _, name in shaft_blocks):
                return {"ok": False, "reason": "missing_shaft_tool"}
            self.bot.call("equip", {"item": best, "slot": "hand"})

        dug: list[dict[str, Any]] = []
        for s_pos, b_name in shaft_blocks:
            if b_name in {"air", "cave_air", "void_air"}:
                continue
            try:
                # An urgent bunker already has roof material. Chasing drops
                # between each dig can add eight seconds per block.
                self.bot.call("dig", {"pos": s_pos, "collect": not urgent, "stop_on": ["hurt", "death"]})
                dug.append({"pos": s_pos, "block": b_name})
                # The dig response is not evidence the bot fell into its hole.
                # Never close the roof over an empty shaft next to the bot.
                for _ in range(10):
                    fell = self.bot.status()
                    fallen_pos = fell.get("pos") or pos
                    if fell.get("alive") is False or (fell.get("last_death") or {}).get("t") != body_t:
                        break
                    if abs(float(fallen_pos[1]) - s_pos[1]) <= 0.2 and max(abs(fallen_pos[0] - bx - 0.5), abs(fallen_pos[2] - bz - 0.5)) <= 0.2 and fell.get("on_ground") is not False:
                        break
                    time.sleep(0.1)
                if fell.get("alive") is False or (fell.get("last_death") or {}).get("t") != body_t or abs(float(fallen_pos[1]) - s_pos[1]) > 0.2 or max(abs(fallen_pos[0] - bx - 0.5), abs(fallen_pos[2] - bz - 0.5)) > 0.2:
                    return {"ok": False, "requires_replan": True, "reason": "shaft_entry_not_confirmed", "dug": dug, "pos_after": fallen_pos}
            except ApiError as exc:
                return {
                    "ok": False,
                    "reason": "protected" if _is_protected_err(exc) else "dig_failed",
                    "failed_pos": s_pos,
                    "error": exc.code,
                    "message": exc.message,
                    "dug": dug,
                }

        counts_after = _inventory_counts(self.bot)
        roof_item = _pick_roof_item(counts_after)
        roof_pos = [bx, by - 1, bz]
        roof_placed = False
        roof_error: dict[str, str] | None = None
        if roof_item:
            try:
                self.bot.call("place", {"item": roof_item, "pos": roof_pos})
                roof_placed = _block_name(roof_pos) == roof_item
            except ApiError as exc:
                roof_error = {"code": exc.code, "message": exc.message}

        st_after = self.bot.status()
        final_pos = st_after.get("pos") or pos
        inside = abs(float(final_pos[1]) - (by - 3)) <= 0.2 and max(abs(final_pos[0] - bx - 0.5), abs(final_pos[2] - bz - 0.5)) <= 0.2
        return {
            "ok": roof_placed and inside,
            "requires_replan": not (roof_placed and inside),
            "inside_shaft": inside,
            "origin": [bx, by, bz],
            "floor_pos": floor_pos,
            "floor_block": floor_block,
            "crafted_planks": crafted_planks,
            "dug": dug,
            "roof_pos": roof_pos,
            "roof_item": roof_item,
            "roof_placed": roof_placed,
            "roof_error": roof_error,
            "pos_after": st_after.get("pos"),
        }


    def dig_cover_pocket(self) -> dict[str, Any]:
        """Step into a 1x2 wall pocket and seal it behind us to break a shooter's line of sight.

        Live s03: a skeleton in an open cave put 6 arrows into a fleeing bot (20 -> 0 hp).
        Running away in open air only extends the firing line; a sealed pocket ends it.
        """
        fluid_or_bedrock = {"water", "lava", "flowing_water", "flowing_lava", "bedrock"}
        _solid_pref = ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite", "granite", "netherrack")

        def _blk(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        counts = _inventory_counts(self.bot)
        filler = next((name for name in _solid_pref if counts.get(name, 0) >= 2), None)
        if filler is None:
            filler = next((name for name, cnt in sorted(counts.items())
                           if cnt >= 2 and name.endswith(("_planks", "_log", "_stem"))), None)
        if filler is None:
            return {"ok": False, "requires_replan": True, "reason": "no_filler_block"}

        st = self.bot.status()
        pos = st.get("pos") or [0.0, 64.0, 0.0]
        bx, by, bz = math.floor(float(pos[0])), math.floor(float(pos[1])), math.floor(float(pos[2]))

        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            feet = [bx + dx, by, bz + dz]
            head = [bx + dx, by + 1, bz + dz]
            back_deep = [bx + dx * 2, by, bz + dz * 2]
            try:
                feet_name, head_name, deep_name = _blk(feet), _blk(head), _blk(back_deep)
            except ApiError:
                continue
            if feet_name in fluid_or_bedrock or head_name in fluid_or_bedrock or deep_name in fluid_or_bedrock:
                continue
            if feet_name == "air" or head_name == "air" or "bedrock" in (feet_name, head_name):
                continue
            try:
                self._equip_if_present(tuple(PICKAXE_TIERS))
                # Deliberately do NOT stop on "hurt": an arrow landing mid-dig is exactly the
                # situation we are escaping, and aborting here would leave us still exposed.
                for cell in (feet, head):
                    self.bot.call("dig", {"pos": cell, "collect": True, "stop_on": ["death"]})
                self.bot.call("move_to", {
                    "x": feet[0] + 0.5, "y": feet[1], "z": feet[2] + 0.5,
                    "range": 0.8, "timeout": 2500, "stop_on": ["death"],
                })
                sealed = []
                for cell in (feet, head):
                    try:
                        self.bot.call("place", {"item": filler, "pos": cell})
                        sealed.append(_blk(cell) == filler)
                    except ApiError:
                        sealed.append(False)
                st_now = self.bot.status()
                return {
                    "ok": all(sealed),
                    "requires_replan": not all(sealed),
                    "mode": "cover_pocket",
                    "direction": [dx, dz],
                    "pocket": [feet, head],
                    "filler": filler,
                    "sealed": sealed,
                    "pos_after": st_now.get("pos"),
                }
            except ApiError:
                continue
        return {"ok": False, "requires_replan": True, "reason": "no_solid_wall"}


    def dig_level_tunnel(
        self, direction: str = "north", steps: int = 4, stop_on_ore: bool = True, *, deadline: float | None = None,
    ) -> dict[str, Any]:
        from ..tunnel import dig_level_tunnel

        res = dig_level_tunnel(self.bot, direction, steps, stop_on_ore, deadline=deadline)
        try:
            return self._describe_tunnel_result(direction, res)
        except _SkillDeadline:
            res["routing_probe_deadline_hit"] = True
            return res


    def _describe_tunnel_result(self, direction: str, res: dict[str, Any]) -> dict[str, Any]:
        from ..tunnel import AIR

        d_key = str(direction).strip().lower()
        stop_reason = str(res.get("stop_reason") or "")
        if stop_reason == "unsafe_or_unknown_floor":
            blocker = res.get("blocker") or {}
            floor = blocker.get("block")
            if floor in AIR or floor in NETHER_TUNNEL_UNSUPPORTED_FLOORS:
                try:
                    dimension = normalize_dimension(_SkillBudgetSession(self.bot, 2.0).status().get("dimension"))
                except (_SkillDeadline, ApiError, httpx.HTTPError):
                    dimension = None
                if dimension == "the_nether":
                    if floor in AIR:
                        res["stop_reason"] = "bridge_gap_ahead"
                        res["hint"] = "前方脚下是空隙。请沿有支撑的路线改向，或退回上一处安全落脚点。"
                    else:
                        res["stop_reason"] = "nether_tunnel_floor_unsupported"
                        res["hint"] = f"水平矿道当前不支持以 {floor} 为支撑。请改走已确认安全的实心通路，或换方向侦察。"
                    return res
        terrain_failures = {
            "unsafe_or_unknown_floor",
            "fluid_adjacent",
            "falling_material_adjacent",
            "unsupported_face_block",
            "insufficient_pickaxe_tier",
            "dig_not_confirmed",
            "support_changed",
            "movement_not_confirmed",
        }
        if res.get("ok") is False and stop_reason in terrain_failures:
            def _floor3(raw_pos: Any) -> tuple[int, int, int] | None:
                if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) < 3:
                    return None
                try:
                    return (
                        math.floor(float(raw_pos[0])),
                        math.floor(float(raw_pos[1])),
                        math.floor(float(raw_pos[2])),
                    )
                except (TypeError, ValueError):
                    return None

            start_cell = _floor3(res.get("pos_before"))
            end_cell = _floor3(res.get("pos_after")) or start_cell
            blk = res.get("blocker")
            reason_label = (
                f"{stop_reason}:{blk.get('block')}"
                if isinstance(blk, dict) and blk.get("block")
                else stop_reason
            )
            if end_cell is not None:
                if d_key in {"north", "south", "east", "west"}:
                    self._record_failed_tunnel_dir(end_cell, d_key, reason_label)
                    if not res.get("dug") and start_cell is not None and start_cell != end_cell:
                        self._record_failed_tunnel_dir(start_cell, d_key, f"dead_end->{reason_label}")
                inv_after = res.get("inventory_after") if isinstance(res.get("inventory_after"), dict) else None
                probe_cache: dict[tuple[int, int, int], str] = {}
                vtun = self._probe_viable_tunnel_dirs(
                    end_cell[0],
                    end_cell[1],
                    end_cell[2],
                    probe_cache=probe_cache,
                    exclude_dirs={d_key} if d_key in {"north", "south", "east", "west"} else None,
                    inv_counts=inv_after,
                )
                vup = self._probe_viable_up_stair_dirs(
                    end_cell[0],
                    end_cell[1],
                    end_cell[2],
                    probe_cache=probe_cache,
                    inv_counts=inv_after,
                )
                res["viable_tunnel_directions"] = vtun
                res["viable_up_directions"] = vup
                orig_hint = str(res.get("hint") or "").strip()
                base_hint = orig_hint.rstrip("。")
                for suffix_phrase in ("，请换方向", "或换方向"):
                    if base_hint.endswith(suffix_phrase):
                        base_hint = base_hint[: -len(suffix_phrase)]
                if vtun:
                    up_opt = (
                        f"；也可改调 mc_skill_staircase(direction='{vup[0]}', vertical='up', steps=6) 向上升层"
                        if vup
                        else ""
                    )
                    res["hint"] = (
                        f"{base_hint}；当前坐标{list(end_cell)}实测可行水平开挖方向: {','.join(vtun)}"
                        f"（可改调 mc_skill_tunnel(direction='{vtun[0]}', steps=6)）{up_opt}。"
                    ).lstrip("；")
                elif vup:
                    res["hint"] = (
                        f"{base_hint}；当前坐标{list(end_cell)}水平四向均无法继续平推(viable_tunnel_directions=[])，"
                        f"严禁继续原地调用 mc_skill_tunnel！请改调 mc_skill_staircase(direction='{vup[0]}', vertical='up', steps=6) 向上退回上层再分支(可选上行方向: {','.join(vup)})。"
                    ).lstrip("；")
                else:
                    vpillar = self._probe_viable_pillar_up(
                        end_cell[0],
                        end_cell[1],
                        end_cell[2],
                        probe_cache=probe_cache,
                        inv_counts=inv_after,
                    )
                    res["viable_pillar_up"] = vpillar
                    if vpillar:
                        res["hint"] = (
                            f"{base_hint}；当前坐标{list(end_cell)}水平四向与斜向阶梯上行均受阻"
                            "(viable_tunnel_directions=[], viable_up_directions=[])，但头顶正上方安全且有垫脚方块(viable_pillar_up=true)，"
                            "请改调 mc_skill_staircase(vertical='pillar', steps=4) 原地垂直挖顶垫脚直上脱困！"
                        ).lstrip("；")
                    else:
                        res["hint"] = (
                            f"{base_hint}；当前坐标{list(end_cell)}水平通道与常规向上阶梯均受阻(viable_tunnel_directions=[], viable_up_directions=[], viable_pillar_up=false)，"
                            "严禁继续原地重复调用 mc_skill_tunnel/mc_skill_staircase；请调 mc_move_to 沿已挖矿道退回或确认头顶安全后用 mc_dig/mc_place 脱困。"
                        ).lstrip("；")

            head_order = (
                "ok",
                "requires_replan",
                "direction",
                "steps_requested",
                "steps_done",
                "stop_reason",
                "blocker",
                "last_error",
                "viable_tunnel_directions",
                "viable_up_directions",
                "viable_pillar_up",
                "hint",
            )
            ordered: dict[str, Any] = {}
            for k in head_order:
                if k in res:
                    ordered[k] = res[k]
            for k, v in res.items():
                if k not in ordered:
                    ordered[k] = v
            return ordered
        return res

