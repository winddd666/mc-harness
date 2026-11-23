"""MacroSkills mixin: inventory_station (split from legacy library.py)."""

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


class _InventoryStationMixin:
    def _ensure_free_slots(
        self,
        min_free: int = 3,
        inv_snapshot: dict[str, Any] | None = None,
        *,
        turn_away_yaw: float | None = None,
        station_materials: dict[str, int] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Drop low-value clutter deterministically when inventory is nearly full (< min_free empty slots)."""
        if self._smelt_inventory_reserve:
            station_materials = _merge_counts(dict(station_materials or {}), self._smelt_inventory_reserve)
        station_min_free = min(3, min_free) if station_materials is not None else 0
        materials = station_materials or {}
        inv = inv_snapshot if isinstance(inv_snapshot, dict) else self.bot.inventory()
        if not isinstance(inv, dict):
            return {}, []
        empty_now = _compute_empty_slots(inv)
        mining_deadline = getattr(self.bot, "_mining_deadline", None)
        if empty_now < min_free and mining_deadline is not None:
            return self._stash_native_mining_inventory(inv, min_free, mining_deadline)
        pre_counts: dict[str, int] = {}
        for row in inv.get("items") or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            iname = str(row["name"]).removeprefix("minecraft:")
            if iname in _TRASH_TIER4_COBBLE or iname in _TRASH_TIER_NETHER or iname in _TRASH_TIER3_DIRT:
                pre_counts[iname] = pre_counts.get(iname, 0) + int(row.get("count") or 0)
        pre_over_cap = (
            any(pre_counts.get(n, 0) > 192 for n in _TRASH_TIER4_COBBLE)
            or any(pre_counts.get(n, 0) > 128 for n in _TRASH_TIER_NETHER)
            or any(pre_counts.get(n, 0) > 64 for n in _TRASH_TIER3_DIRT)
        )
        if empty_now >= min_free and not pre_over_cap:
            return inv, []

        max_drop_ops = 12
        dropped: list[dict[str, Any]] = []
        drop_ops = 0
        turned_away = False
        for pass_idx in range(2):
            empty_now = _compute_empty_slots(inv)
            # Preserve ordinary retention unless the workstation still has fewer than three slots.
            compact = pass_idx == 1 and empty_now < station_min_free
            slot_goal = station_min_free if compact else min_free
            rows = [
                r
                for r in (inv.get("items") or [])
                if isinstance(r, dict) and r.get("name") and int(r.get("count") or 0) > 0
            ]
            per_item_counts: dict[str, int] = {}
            per_item_slots: dict[str, list[int]] = {}
            for r in rows:
                iname = str(r["name"]).removeprefix("minecraft:")
                cnt = int(r.get("count") or 0)
                per_item_counts[iname] = per_item_counts.get(iname, 0) + cnt
                per_item_slots.setdefault(iname, []).append(cnt)

            over_cap = (
                any(per_item_counts.get(n, 0) > 192 for n in _TRASH_TIER4_COBBLE)
                or any(per_item_counts.get(n, 0) > 128 for n in _TRASH_TIER_NETHER)
                or any(per_item_counts.get(n, 0) > 64 for n in _TRASH_TIER3_DIRT)
            )
            if (empty_now >= min_free and not over_cap) or drop_ops >= max_drop_ops:
                break

            plan_specs = _inventory_retention_plan(per_item_counts)
            est_free = empty_now
            made_drop = False
            for name, regular_keep in plan_specs:
                keep = min(regular_keep, 64) if compact else regular_keep
                reserved = materials.get(name, 0)
                if reserved:
                    keep = max(keep, 64, reserved)
                is_capped = keep >= regular_keep and name in _MINING_RETENTION_CAPS
                total = per_item_counts.get(name, 0)
                if drop_ops >= max_drop_ops or (est_free >= slot_goal and not (is_capped and total > keep)):
                    if drop_ops >= max_drop_ops:
                        break
                    if est_free >= slot_goal and not over_cap:
                        break
                    continue
                excess = total - keep
                if excess <= 0:
                    continue
                slot_list = per_item_slots.get(name) or [excess]
                smallest_slot = min(slot_list)
                # Avoid tiny remainder drops unless keeping=0 or freeing at least 1 full slot.
                if keep > 0 and excess < smallest_slot and len(slot_list) <= 1:
                    continue
                target_drop = (
                    excess
                    if (keep == 0 or is_capped)
                    else min(excess, max(0, slot_goal - est_free) * 64)
                )
                if keep > 0 and target_drop < smallest_slot:
                    continue
                remaining = target_drop
                while (
                    remaining > 0
                    and drop_ops < max_drop_ops
                    and (est_free < slot_goal or (is_capped and total > keep))
                ):
                    if turn_away_yaw is not None and not turned_away:
                        turned_away = True
                        try:
                            self.bot.call("look", {"yaw": float(turn_away_yaw), "pitch": 0.0})
                        except ApiError:
                            pass
                    batch = min(64, remaining)
                    try:
                        self.bot.call("drop", {"item": name, "count": batch})
                    except ApiError as err:
                        if err.code in ("interrupted", "dead", "disconnected"):
                            return self.bot.inventory(), dropped
                        break
                    drop_ops += 1
                    dropped.append({"item": name, "count": batch})
                    made_drop = True
                    remaining -= batch
                    total -= batch
                    per_item_counts[name] = total
                    slots_now = math.ceil(max(0, total) / 64)
                    if slots_now < len(slot_list):
                        est_free += len(slot_list) - slots_now
                        slot_list = [64] * slots_now
            if not made_drop:
                if pass_idx == 0 and empty_now < station_min_free:
                    continue
                break
            inv = self.bot.inventory()
            if not isinstance(inv, dict):
                return {}, dropped
        return inv, dropped


    def remember_placed_storage(self, item: str, pos: list[int], dim: str) -> None:
        item = _clean_block(item)
        if item in {"chest", "barrel", "trapped_chest"}:
            self._placed_storage[(normalize_dimension(dim), tuple(int(v) for v in pos[:3]))] = item


    def _corridor_side_recess(self, origin: list[float]) -> tuple[list[int] | None, bool]:
        """Locate an enclosed recess beside a through corridor, or identify a closed narrow lane."""
        x, y, z = (math.floor(v) for v in origin[:3])
        if not 0 <= origin[1] - y < 0.1:
            return None, False
        air = {"air", "cave_air", "void_air"}
        solid = {"stone", "deepslate", "dirt", "cobblestone", "cobbled_deepslate", "andesite", "granite", "tuff", "netherrack"}
        cache: dict[tuple[int, int, int], str] = {}

        def block(dx: int, dy: int, dz: int) -> str:
            return _cached_block_name(self.bot, x + dx, y + dy, z + dz, cache)

        for dx, dz in ((1, 0), (0, 1)):
            if not all(block(k * dx, dy, k * dz) in air for k in (-1, 1) for dy in (0, 1)):
                continue
            if not all(block(k * dx, -1, k * dz) in solid for k in (-1, 1)):
                continue
            closed_flanks = 0
            for k in (1, -1):
                sx, sz = k * dz, k * dx
                flank = [block(sx, dy, sz) for dy in (0, 1)]
                if all(name in solid for name in flank):
                    closed_flanks += 1
                    continue
                if any(name not in air for name in flank):
                    continue
                if block(sx, -1, sz) not in solid or block(2 * sx, 0, 2 * sz) not in solid:
                    continue
                if not all(block(sx + bend * dx, 0, sz + bend * dz) in solid for bend in (-1, 1)):
                    continue
                closed_flanks += 1
                if self.geofence and self.geofence.check_point(x + sx, z + sz, action="mc_place"):
                    continue
                return [x + sx, y, z + sz], True
            if closed_flanks == 2:
                return None, True
        return None, False


    def _underground_utility_site(self, status: dict[str, Any]) -> tuple[list[int] | None, bool]:
        pos = status.get("pos")
        if (
            not pos or pos[1] >= 55 or status.get("on_ground") is not True
            or status.get("in_water") or status.get("in_lava")
        ):
            return None, False
        return self._corridor_side_recess(pos)


    @staticmethod
    def _no_safe_utility_position() -> dict[str, Any]:
        return {
            "ok": False, "error": "no_safe_utility_position", "requires_replan": True,
            "message": "矿道两侧没有可用侧格。请到带侧龛的拓宽处放置，保持回程通道畅通。",
        }


    def _storage_blocks_passage(
        self, origin: list[float], site: tuple[int, ...], cache: dict[tuple[int, int, int], str],
    ) -> bool:
        x, y, z = (math.floor(coord) for coord in origin[:3])
        dx, dz = site[0] - x, site[2] - z
        distance = abs(dx) + abs(dz)
        if site[1] != y or (dx and dz) or distance not in (1, 2):
            return False
        dx, dz = dx // distance, dz // distance
        air = {"air", "cave_air", "void_air"}
        open_or_unknown = air | {"unknown"}

        def block(ox: int, oy: int, oz: int) -> str:
            return _cached_block_name(self.bot, x + ox, y + oy, z + oz, cache)

        # A nearby chest in line with two open ends can occupy the return lane.
        open_steps = (-1, *range(1, distance), distance + 1)
        if block(distance * dx, 1, distance * dz) not in air or not all(
            block(k * dx, dy, k * dz) in air for k in open_steps for dy in (0, 1)
        ):
            return False
        return all(
            any(block(k * dz, dy, k * dx) not in open_or_unknown for dy in (0, 1))
            for k in (-1, 1)
        )


    def _stash_mining_clutter(
        self, inv: dict[str, Any], min_free: int, *, status: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        before = _inventory_counts_from_raw(inv)
        free = _compute_empty_slots(inv)
        needed = min_free - free
        planned: dict[str, int] = {}
        for item, keep in _inventory_retention_plan(before):
            excess = before.get(item, 0) - keep
            if excess <= 0 or (needed <= 0 and item not in _MINING_RETENTION_CAPS):
                continue
            count = excess if item in _MINING_RETENTION_CAPS else min(excess, 64 * needed)
            planned[item] = count
            occupied = sum(
                1 for row in inv.get("items") or []
                if _clean_block(row.get("name")) == item and int(row.get("count") or 0) > 0
            )
            needed -= max(0, occupied - math.ceil((before[item] - count) / 64))
        result: dict[str, Any] = {"method": "no_safe_storage", "empty_slots": free}
        if not planned:
            result["method"] = "no_clutter"
            return [], result

        if status is None:
            status = self.bot.status()
        origin = status.get("pos")
        if not origin or status.get("alive") is False or status.get("in_water") or status.get("in_lava"):
            return [], result
        dim = normalize_dimension(status.get("dimension"))
        site = None
        cache: dict[tuple[int, int, int], str] = {}
        for (site_dim, pos), name in sorted(
            self._placed_storage.items(), key=lambda pair: math.dist(origin[:3], pair[0][1]),
        ):
            if site_dim != dim or math.dist(origin[:3], [v + 0.5 for v in pos]) > 2.5:
                continue
            if self.geofence and self.geofence.check_point(pos[0], pos[2], action="mc_place"):
                continue
            candidate = list(pos)
            if self._container_block_at(candidate) != name:
                continue
            if self._storage_blocks_passage(origin, pos, cache):
                result.setdefault("storage_skipped", []).append({"pos": candidate, "reason": "blocks_passage"})
                continue
            actual, attempt = self._verify_mining_storage(
                planned, candidate, dim, status, {**result, "method": "owned_storage", "pos": candidate}, before=before,
            )
            if actual or attempt.get("error") not in {
                "section_full", "container_full", "no_window", "not_found", "out_of_reach", "path_not_found",
            }:
                return actual, attempt
            result.setdefault("storage_skipped", []).append({"pos": candidate, "reason": attempt["error"]})

        carried = next((name for name in ("chest", "barrel") if before.get(name, 0) >= 1), None)
        # A new stack needs an empty slot even when enough planks are in the bag.
        can_craft = _total_planks(before) >= 16 and free > 0
        if carried or can_craft:
            site, _ = self._corridor_side_recess(origin)
            if site is not None and not carried:
                tables = self.bot.call("find_blocks", {"radius": 2, "types": ["crafting_table"], "limit": 1})
                if not tables.get("blocks"):
                    site = None
                else:
                    self.bot.call("craft", {"item": "chest", "count": 1})
                    carried = "chest"
            if site is not None:
                placed = self.bot.call("place", {"item": carried, "pos": site})
                if (placed.get("pos") and placed["pos"] != site) or self._container_block_at(site) != carried:
                    return [], {**result, "error": "storage_placement_unconfirmed"}
                self.remember_placed_storage(carried, site, dim)
                result["method"] = "placed_storage"
        if site is None:
            result["message"] = "附近暂无已确认可用的自有容器，且没有可放置的侧格。请换到有侧格或仓储的位置腾出空位。"
            return [], result

        result["pos"] = site
        return self._verify_mining_storage(planned, site, dim, status, result, before=before)


    @staticmethod
    def _storage_window_counts(view: Any) -> dict[str, int] | None:
        if not isinstance(view, dict):
            return None
        rows = (view.get("sections") or {}).get("container")
        if not isinstance(rows, list):
            return None
        return _inventory_counts_from_raw({"items": rows})


    def _verify_mining_storage(
        self, planned: dict[str, int], site: list[int], dim: str,
        status: dict[str, Any], result: dict[str, Any], *, before: dict[str, int],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target = {"target": {"pos": site}}
        try:
            starting = self._storage_window_counts(self.bot.call("window/view", target))
        except ApiError as error:
            return [], {**result, "error": error.code}
        if starting is None:
            return [], {**result, "error": "storage_window_unconfirmed"}

        session = self.bot
        deadline = session.deadline
        session.deadline = max(time.monotonic(), deadline - 2.0)
        try:
            stored = self.container_batch(planned, action="put", x=site[0], y=site[1], z=site[2], dim=dim)
        except (_SkillDeadline, httpx.TimeoutException):
            session.deadline = deadline
            if session.pending_action and not session.stop_pending():
                return [], {**result, "error": "storage_action_unconfirmed"}
            session.timed_out = False
            stored = {"ok": False, "error": "storage_timeout"}
        finally:
            session.deadline = deadline

        error = stored.get("error")
        if error in {"dead", "death", "disconnected", "not_connected", "interrupted", "hurt"}:
            return [], {**result, "error": error}

        def life_changed() -> bool:
            live = self.bot.status()
            return (
                live.get("alive") is False or live.get("last_death") != status.get("last_death")
                or normalize_dimension(live.get("dimension")) != dim
            )

        if life_changed():
            return [], {**result, "error": "storage_life_changed"}

        done = stored.get("done")
        reported_put = isinstance(done, list) and any(isinstance(op, dict) and op.get("put") for op in done)
        ending = self._storage_window_counts(stored.get("window"))
        if ending is None:
            ending = self._storage_window_counts(self.bot.call("window/view", target))
        inventory = self.bot.inventory()
        if life_changed():
            return [], {**result, "error": "storage_life_changed"}
        if not isinstance(inventory.get("items"), list):
            return [], {**result, "error": "storage_transfer_unconfirmed"}
        bag_removed = _positive_inv_delta(_inventory_counts_from_raw(inventory), before)
        box_added = _positive_inv_delta(starting, ending) if ending is not None else {}
        amounts = {name: min(n, bag_removed.get(name, 0), box_added.get(name, 0)) for name, n in planned.items()}
        actual = [{"item": name, "count": n} for name, n in amounts.items() if n > 0]
        result["empty_slots"] = _compute_empty_slots(inventory)
        if not actual:
            uncertain = reported_put or ending is None or any(name in bag_removed or name in box_added for name in planned)
            result["error"] = "storage_transfer_unconfirmed" if uncertain else (error or "storage_transfer_unconfirmed")
        elif error:
            result["reconciled_after"] = error
        return actual, result


    def _stash_native_mining_inventory(
        self, inv: dict[str, Any], min_free: int, deadline: float,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        budget = min(8.0, deadline - time.monotonic() - 0.5)
        session = _SkillBudgetSession(self.bot, budget, single_attempt=True)
        scoped = copy(self)
        scoped.bot = session
        try:
            stored, result = scoped._stash_mining_clutter(inv, min_free)
        except (_SkillDeadline, httpx.TimeoutException):
            session.stop_pending()
            stored, result = [], {"method": "cleanup_time_budget"}
        except (ApiError, httpx.HTTPError) as error:
            stored, result = [], {"method": "cleanup_failed", "error": getattr(error, "code", type(error).__name__)}
        if self._dfs_storage_reports is not None:
            entry = {**result, "stored": stored}
            if not self._dfs_storage_reports or self._dfs_storage_reports[-1] != entry:
                self._dfs_storage_reports.append(entry)
        latest = getattr(self.bot, "_last_inventory", None) if stored else None
        return latest if isinstance(latest, dict) else inv, stored


    def clean_mining_inventory(self, max_duration_s: float) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        session = _SkillBudgetSession(self.bot, max_duration_s, single_attempt=True)
        scoped = copy(self)
        scoped.bot = session
        try:
            inv = session.inventory()
            counts = _inventory_counts_from_raw(inv)
            over_cap = any(counts.get(item, 0) > cap for item, cap in _MINING_RETENTION_CAPS.items())
            if _compute_empty_slots(inv) >= 3 and not over_cap:
                return [], None
            status = session.status()
            stored, detail = scoped._stash_mining_clutter(inv, 3, status=status)
            if not over_cap or stored or detail.get("method") != "no_safe_storage" or detail.get("error"):
                return stored, detail
            yaw = status.get("yaw")
            if (
                not status.get("pos") or status.get("alive") is False or status.get("on_ground") is False
                or status.get("in_water") or status.get("in_lava")
                or not isinstance(yaw, (int, float)) or not math.isfinite(yaw)
            ):
                return [], detail
            return scoped._drop_excess_mining_clutter(inv, counts, status, detail)
        except (_SkillDeadline, httpx.TimeoutException):
            session.stop_pending()
            return [], {"method": "cleanup_time_budget"}
        except (ApiError, httpx.HTTPError) as error:
            return [], {"method": "cleanup_failed", "error": getattr(error, "code", type(error).__name__)}


    def _drop_excess_mining_clutter(
        self, inv: dict[str, Any], before: dict[str, int], status: dict[str, Any], detail: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        yaw, pitch = float(status["yaw"]), status.get("pitch")
        pitch = float(pitch) if isinstance(pitch, (int, float)) and math.isfinite(pitch) else 0.0
        self.bot.call("look", {"yaw": yaw + math.pi, "pitch": 0.0})
        try:
            after, dropped = self._ensure_free_slots(min_free=3, inv_snapshot=inv)
        finally:
            if not self.bot.timed_out:
                try:
                    self.bot.call("look", {"yaw": yaw, "pitch": pitch})
                except (_SkillDeadline, ApiError, httpx.HTTPError):
                    self.bot.stop_pending()
        receipt = {**detail, "method": "drop_fallback"}
        live = self.bot.status()
        if (
            live.get("alive") is False or live.get("last_death") != status.get("last_death")
            or normalize_dimension(live.get("dimension")) != normalize_dimension(status.get("dimension"))
        ):
            return [], {**receipt, "error": "cleanup_life_changed"}
        if not isinstance(after.get("items"), list):
            return [], {**receipt, "error": "cleanup_unconfirmed"}
        removed = _positive_inv_delta(_inventory_counts_from_raw(after), before)
        requested: dict[str, int] = {}
        for row in dropped:
            requested[row["item"]] = requested.get(row["item"], 0) + row["count"]
        verified = [{"item": name, "count": min(count, removed[name])} for name, count in requested.items() if name in removed]
        receipt["empty_slots"] = _compute_empty_slots(after)
        if verified:
            receipt.pop("message", None)
        else:
            receipt["error"] = "cleanup_unconfirmed"
        return verified, receipt


    def _safe_craft(
        self,
        item: str,
        count: int = 1,
        *,
        inv_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Auto-clear clutter if full, fail fast when still un-fittable at empty_slots=0, else craft."""
        item = _canonical_plank_recipe(item)
        inv_now, dropped = self._ensure_free_slots(
            min_free=3, inv_snapshot=inv_snapshot, station_materials=_reserved_craft_materials(item, count),
        )
        if _compute_empty_slots(inv_now) == 0 and not _can_fit_item_in_inventory(inv_now, item, count):
            raise ApiError(
                code="inventory_full",
                message=(
                    f"背包 36 格已满(empty_slots=0)且无可自动丢弃的杂物，直接合成 {item} x{count} "
                    "会导致 Mineflayer 无法存放产物而触发 30s 超时；请先消耗、放置或丢弃至少 1 格物品再重试。"
                ),
                context={"item": item, "count": count, "empty_slots": 0},
            )
        res = self.bot.call("craft", {"item": item, "count": count})
        if dropped and isinstance(res, dict):
            return {**res, "auto_dropped": dropped}
        return res


    def _record_failed_stair_dir(
        self,
        cell: tuple[int, int, int, str],
        d_key: str,
        reason: str = "failed",
        ttl_s: float = 180.0,
    ) -> None:
        bucket = self.failed_stair_dirs.setdefault(cell, {})
        bucket[d_key] = (time.monotonic() + ttl_s, reason)


    def _active_failed_stair_dirs(self, cell: tuple[int, int, int, str]) -> dict[str, str]:
        bucket = self.failed_stair_dirs.get(cell)
        if not bucket:
            return {}
        now = time.monotonic()
        expired = [k for k, (exp, _) in bucket.items() if exp <= now]
        for k in expired:
            del bucket[k]
        return {k: reason for k, (_, reason) in bucket.items()}


    def _record_failed_tunnel_dir(
        self,
        cell: tuple[int, int, int],
        d_key: str,
        reason: str = "failed",
        ttl_s: float = 180.0,
    ) -> None:
        bucket = self.failed_tunnel_dirs.setdefault(cell, {})
        bucket[d_key] = (time.monotonic() + ttl_s, reason)


    def _active_failed_tunnel_dirs(self, cell: tuple[int, int, int]) -> dict[str, str]:
        bucket = self.failed_tunnel_dirs.get(cell)
        if not bucket:
            return {}
        now = time.monotonic()
        expired = [k for k, (exp, _) in bucket.items() if exp <= now]
        for k in expired:
            del bucket[k]
        return {k: reason for k, (_, reason) in bucket.items()}


    def _probe_cached_block(
        self,
        pt: tuple[int, int, int],
        probe_cache: dict[tuple[int, int, int], str] | None = None,
    ) -> str:
        if probe_cache is not None and pt in probe_cache:
            return probe_cache[pt]
        res = self.bot.call("get_block", {"pos": [pt[0], pt[1], pt[2]]})
        raw = res.get("name") or res.get("block")
        val = str(raw).removeprefix("minecraft:") if raw else "unknown"
        if probe_cache is not None:
            probe_cache[pt] = val
        return val


    def _probe_viable_tunnel_dirs(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        exclude_dirs: set[str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Probe 2-3 steps ahead for safe horizontal tunnel directions matching tunnel.py rules."""
        from .. import tunnel as _tunnel_mod

        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        cache = probe_cache if probe_cache is not None else {}
        counts = inv_counts if inv_counts is not None else _inventory_counts(self.bot)
        pick, pick_tier = _best_pickaxe_in_inv(counts)
        if not pick:
            return []

        air_set = _tunnel_mod.AIR
        fluid_set = _tunnel_mod.FLUID
        ground_or_ore = _tunnel_mod.GROUND | _tunnel_mod.ORE
        falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
        failed_tunnel = self._active_failed_tunnel_dirs((bx, by, bz))
        excluded = set(exclude_dirs or ())

        def _step_safe(tx: int, tz: int) -> tuple[bool, bool]:
            floor = self._probe_cached_block((tx, by - 1, tz), cache)
            if floor not in ground_or_ore:
                return False, False
            faces = [(tx, by, tz), (tx, by + 1, tz)]
            f_names = [self._probe_cached_block(p, cache) for p in faces]
            for bname in f_names:
                if bname not in (air_set | ground_or_ore):
                    return False, False
                if bname not in air_set and BLOCK_MIN_PICKAXE_TIER.get(bname, 1) > pick_tier:
                    return False, False
            # Regardless of whether the face block is air or solid, check all 6 neighbors of both face blocks.
            neighbors = {
                (px + ax, py + ay, pz + az)
                for px, py, pz in faces
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
            }
            for nb in sorted(neighbors):
                nb_name = self._probe_cached_block(nb, cache)
                if nb_name in fluid_set or nb_name in falling_set:
                    return False, False
            has_solid = any(b not in air_set for b in f_names)
            return True, has_solid

        viable: list[str] = []
        for odir, (odx, odz) in dirs.items():
            if odir in excluded or odir in failed_tunnel:
                continue
            try:
                ok1, dig1 = _step_safe(bx + odx, bz + odz)
                if not ok1:
                    continue
                ok2, dig2 = _step_safe(bx + 2 * odx, bz + 2 * odz)
                if not ok2:
                    continue
                if not dig1 and not dig2:
                    ok3, _ = _step_safe(bx + 3 * odx, bz + 3 * odz)
                    if not ok3:
                        continue
                viable.append(odir)
            except ApiError:
                continue
        return viable


    def _probe_viable_up_stair_dirs(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        exclude_dirs: set[str] | None = None,
        inv_counts: dict[str, int] | None = None,
        require_filler_face: bool = False,
    ) -> list[str]:
        """Probe which 4 cardinal directions can safely perform dig_staircase(vertical='up')."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        cache = probe_cache if probe_cache is not None else {}
        counts = inv_counts if inv_counts is not None else _inventory_counts(self.bot)
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        non_solid = air_set | fluids | {
            "short_grass",
            "tall_grass",
            "leaf_litter",
            "dandelion",
            "poppy",
            "oak_sapling",
        }
        no_support = non_solid | {"unknown", "nether_portal", "end_portal", "end_gateway", "barrier"}
        has_filler = any(
            counts.get(c, 0) >= 1
            for c in STAIR_FILLER_BLOCKS
        ) or any(cnt >= 1 and name.endswith("_planks") for name, cnt in counts.items())

        def _non_air_has_adj_fluid(cells: list[tuple[int, int, int]]) -> bool:
            for cx, cy, cz in cells:
                if self._probe_cached_block((cx, cy, cz), cache) in air_set:
                    continue
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    if self._probe_cached_block((cx + ax, cy + ay, cz + az), cache) in fluids:
                        return True
            return False

        try:
            head_here = self._probe_cached_block((bx, by + 1, bz), cache)
            if head_here in fluid_or_bedrock:
                return []
            if _non_air_has_adj_fluid([(bx, by + 1, bz), (bx, by + 2, bz)]):
                return []
            if self._probe_cached_block((bx, by + 2, bz), cache) in fluid_or_bedrock:
                return []
            if self._probe_cached_block((bx, by + 3, bz), cache) in fluids:
                return []
        except ApiError:
            return []

        failed_up = self._active_failed_stair_dirs((bx, by, bz, "up"))
        excluded = set(exclude_dirs or ())
        viable_up: list[str] = []
        for odir, (odx, odz) in dirs.items():
            if odir in excluded or odir in failed_up:
                continue
            nx, nz = bx + odx, bz + odz
            try:
                if any(self._probe_cached_block(p, cache) in fluid_or_bedrock for p in ((nx, by + 1, nz), (nx, by + 2, nz))):
                    continue
                if self._probe_cached_block((nx, by + 3, nz), cache) in fluids:
                    continue
                if _non_air_has_adj_fluid([(nx, by + 1, nz), (nx, by + 2, nz)]):
                    continue
                step_sup = self._probe_cached_block((nx, by, nz), cache)
                if step_sup in fluid_or_bedrock or step_sup == "unknown":
                    continue
                is_non_solid = step_sup in non_solid or step_sup.endswith(("_leaves", "_sapling", "_carpet", "_banner"))
                if is_non_solid and not has_filler:
                    continue
                if is_non_solid and require_filler_face:
                    # The space above the filler is dug out before placement.
                    faces = ((nx, by - 1, nz), (nx + 1, by, nz), (nx - 1, by, nz), (nx, by, nz + 1), (nx, by, nz - 1))
                    if not any(
                        name not in no_support and not name.endswith(("_leaves", "_sapling", "_carpet", "_banner"))
                        for name in (self._probe_cached_block(cell, cache) for cell in faces)
                    ):
                        continue
                viable_up.append(odir)
            except ApiError:
                continue
        return viable_up


    def _probe_viable_down_stair_dirs(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        exclude_dirs: set[str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Probe which 4 cardinal directions can safely perform dig_staircase(vertical='down')."""
        dirs = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        cache = probe_cache if probe_cache is not None else {}
        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        non_solid = air_set | fluids | {
            "short_grass",
            "tall_grass",
            "leaf_litter",
            "dandelion",
            "poppy",
            "oak_sapling",
        }
        def _non_air_has_adj_fluid(cells: list[tuple[int, int, int]]) -> bool:
            for cx, cy, cz in cells:
                if self._probe_cached_block((cx, cy, cz), cache) in air_set:
                    continue
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    if self._probe_cached_block((cx + ax, cy + ay, cz + az), cache) in fluids:
                        return True
            return False

        failed_down = self._active_failed_stair_dirs((bx, by, bz, "down"))
        excluded = set(exclude_dirs or ())
        viable_down: list[str] = []
        for odir, (odx, odz) in dirs.items():
            if odir in excluded or odir in failed_down:
                continue
            nx, nz = bx + odx, bz + odz
            try:
                dig_cells = [(nx, by + 1, nz), (nx, by, nz), (nx, by - 1, nz)]
                if any(self._probe_cached_block(p, cache) in fluid_or_bedrock for p in dig_cells):
                    continue
                if self._probe_cached_block((nx, by + 2, nz), cache) in fluids:
                    continue
                if _non_air_has_adj_fluid(dig_cells):
                    continue
                step_sup = self._probe_cached_block((nx, by - 2, nz), cache)
                if step_sup in fluid_or_bedrock:
                    continue
                is_non_solid = step_sup in non_solid or step_sup.endswith(("_leaves", "_sapling", "_carpet", "_banner"))
                if is_non_solid:
                    continue
                viable_down.append(odir)
            except ApiError:
                continue
        return viable_down


    def _enrich_ecology_unavailable(
        self,
        res: dict[str, Any],
        *,
        targets: list[str] | tuple[str, ...],
        radius: int,
        counts: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Enrich an empty local search with world ecology diagnostics and staircase directions."""
        if not callable(getattr(self.bot, "status", None)):
            return None
        try:
            st = self.bot.status() or {}
        except Exception:
            return None
        dim = normalize_dimension(st.get("dimension"))
        biome = str(st.get("biome") or "")
        pos = st.get("pos")
        has_pos = isinstance(pos, (list, tuple)) and len(pos) >= 3
        px, py, pz = ([int(math.floor(float(v))) for v in pos[:3]] if has_pos else [0, 64, 0])
        check = assess_world_target(
            list(targets),
            dimension=dim,
            y=(py if has_pos else None),
            biome=biome,
            radius=radius,
            mode="find",
        )
        if check.get("available") is not False:
            return None

        out: dict[str, Any] = {
            **res,
            "requires_replan": True,
            "ecology_check": check,
        }
        if has_pos:
            out["pos"] = [px, py, pz]
        if check.get("family_id") == "surface_trees" and check.get("verdict") == "below_generation_band":
            out["underground_no_trees"] = True

        action_hint = ""
        dir_needed = check.get("direction_needed")
        probe_cache: dict[tuple[int, int, int], str] = {}
        if has_pos and dir_needed == "up":
            up_dirs = self._probe_viable_up_stair_dirs(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
            vpillar = False if up_dirs else self._probe_viable_pillar_up(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
            out["viable_up_directions"] = up_dirs
            out["viable_pillar_up"] = vpillar
            rec_y = int(check.get("recommended_y") or 66)
            steps_up = min(20, max(12, rec_y - py))
            if up_dirs:
                action_hint = (
                    f"请先调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps={steps_up}) "
                    f"向上返回目标高度带(可选上行: {','.join(up_dirs)})。"
                )
            elif vpillar:
                action_hint = f"请先调 mc_skill_staircase(vertical='pillar', steps={min(10, steps_up)}) 原地垫脚直上脱困。"
            else:
                action_hint = "请先调 mc_skill_staircase(vertical='up', steps=16) 或 mc_skill_unstick 脱困。"
        elif has_pos and dir_needed == "down":
            down_dirs = self._probe_viable_down_stair_dirs(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
            out["viable_down_directions"] = down_dirs
            rec_y = int(check.get("recommended_y") or 0)
            steps_down = min(16, max(6, py - rec_y))
            if down_dirs:
                action_hint = (
                    f"请调 mc_skill_staircase(direction='{down_dirs[0]}', vertical='down', steps={steps_down}, stop_on_ore=True) "
                    f"斜向下探接近 Y={rec_y}(可选下探: {','.join(down_dirs)})。"
                )
            else:
                action_hint = f"请调 mc_skill_staircase(vertical='down', steps={steps_down}, stop_on_ore=True) 下探至 Y={rec_y} 附近。"

        out["message"] = f"{check['hint']} {action_hint}".strip()
        return out


    def _probe_viable_pillar_up(
        self,
        bx: int,
        by: int,
        bz: int,
        *,
        probe_cache: dict[tuple[int, int, int], str] | None = None,
        inv_counts: dict[str, int] | None = None,
    ) -> bool:
        """Probe whether 1x1 straight-up pillar climbing (dig overhead + jump + place underfoot) is safe."""
        if "pillar" in self._active_failed_stair_dirs((bx, by, bz, "pillar")):
            return False
        cache = probe_cache if probe_cache is not None else {}
        counts = inv_counts if inv_counts is not None else _inventory_counts(self.bot)
        has_filler = any(
            counts.get(c, 0) >= 1
            for c in ("cobblestone", "cobbled_deepslate", "dirt", "andesite", "diorite", "granite", "tuff", "stone")
        ) or any(cnt >= 1 and name.endswith("_planks") for name, cnt in counts.items())
        if not has_filler:
            return False

        air_set = {"air", "cave_air", "void_air"}
        fluids = {"water", "lava", "flowing_water", "flowing_lava"}
        fluid_or_bedrock = fluids | {"bedrock"}
        falling_set = {"gravel", "sand", "red_sand", "suspicious_gravel", "suspicious_sand"}
        hazard_set = fluids | falling_set
        _, pick_tier = _best_pickaxe_in_inv(counts)

        try:
            for cy in (by + 1, by + 2):
                bname = self._probe_cached_block((bx, cy, bz), cache)
                if bname in fluid_or_bedrock or bname == "unknown":
                    return False
            top3 = self._probe_cached_block((bx, by + 3, bz), cache)
            if top3 in hazard_set:
                return False
            overhead = self._probe_cached_block((bx, by + 2, bz), cache)
            if overhead not in air_set:
                req_tier = BLOCK_MIN_PICKAXE_TIER.get(overhead, 0)
                if req_tier > pick_tier:
                    return False
                for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    nb = self._probe_cached_block((bx + ax, by + 2 + ay, bz + az), cache)
                    if nb in hazard_set:
                        return False
            return True
        except ApiError:
            return False


    def _avoid_temporarily(self, pos: tuple[int, int, int], reason: str, seconds: float = 15.0) -> None:
        self.temporary_avoidance[pos] = {"reason": reason, "until": time.monotonic() + seconds}


    def _is_block_avoided(self, pos: tuple[int, int, int]) -> bool:
        record = self.temporary_avoidance.get(pos)
        if record and record["until"] <= time.monotonic():
            del self.temporary_avoidance[pos]
            record = None
        return pos in self.protected_positions or record is not None


    def _avoidance_summary(self) -> dict[str, Any]:
        for pos in list(self.temporary_avoidance):
            self._is_block_avoided(pos)
        return {
            "protected_blacklisted": len(self.protected_positions),
            "temporarily_avoided": len(self.temporary_avoidance),
            "temporary_reasons": sorted({r["reason"] for r in self.temporary_avoidance.values()}),
        }


    def _block_info(self, pos_xyz: list[int]) -> tuple[str, bool]:
        res = self.bot.call("get_block", {"pos": pos_xyz})
        raw = res.get("name") or res.get("block") or "air"
        return str(raw).removeprefix("minecraft:"), bool(res.get("diggable", False))


    def _is_water_covered(self, pos: tuple[int, int, int]) -> bool:
        water_names = {"water", "flowing_water"}
        probes = (
            (pos[0], pos[1] + 1, pos[2]),
            (pos[0], pos[1] + 2, pos[2]),
            (pos[0] + 1, pos[1], pos[2]),
            (pos[0] - 1, pos[1], pos[2]),
            (pos[0], pos[1], pos[2] + 1),
            (pos[0], pos[1], pos[2] - 1),
        )
        for pt in probes:
            try:
                name, _ = self._block_info(list(pt))
            except ApiError:
                continue
            if name in water_names:
                return True
        return False


    def _equip_if_present(self, candidates: tuple[str, ...], slot: str = "hand") -> str | None:
        counts = _inventory_counts(self.bot)
        for item in candidates:
            if counts.get(item, 0) > 0:
                try:
                    self.bot.call("equip", {"item": item, "slot": slot})
                    return item
                except ApiError:
                    return None
        return None


    def _ensure_planks_and_sticks(self, planks_needed: int, sticks_needed: int) -> dict[str, int]:
        counts = _inventory_counts(self.bot)
        # Convert sticks shortage -> additional planks needed (2 planks -> 4 sticks)
        extra_stick_crafts = _stick_craft_batches(sticks_needed, counts.get("stick", 0))
        total_planks_needed = planks_needed + extra_stick_crafts * 2

        if _total_planks(counts) < total_planks_needed:
            # Prefer the usual wood families with unstripped stock, then use stripped stock.
            sources = sorted(_PLANK_LOG_SOURCES.items(), key=lambda pair: counts.get(pair[1][0], 0) <= 0)
            for plank_name, log_names in sources:
                logs_to_craft = math.ceil((total_planks_needed - _total_planks(counts)) / 4)
                if logs_to_craft <= 0:
                    break
                use_logs = min(sum(counts.get(log, 0) for log in log_names), logs_to_craft)
                if use_logs <= 0:
                    continue
                try:
                    self._safe_craft(plank_name, use_logs * 4)
                except ApiError as err:
                    if err.code == "inventory_full":
                        raise
                else:
                    counts = _inventory_counts(self.bot)

        if sticks_needed > counts.get("stick", 0) and _total_planks(counts) >= 2:
            need_crafts = _stick_craft_batches(sticks_needed, counts.get("stick", 0))
            try:
                self._safe_craft("stick", need_crafts * 4)
            except ApiError as err:
                if err.code == "inventory_full":
                    raise
            counts = _inventory_counts(self.bot)

        return counts


    def _carve_and_place_table(
        self,
        px: int,
        py: int,
        pz: int,
        item: str = "crafting_table",
    ) -> dict[str, Any] | None:
        """Carve a 1-block wall pocket over a solid floor and place a utility block there."""
        air_set = {"air", "cave_air", "void_air"}
        fluid_or_bedrock = {"water", "lava", "flowing_water", "flowing_lava", "bedrock"}
        non_solid = air_set | fluid_or_bedrock | {"short_grass", "tall_grass", "leaf_litter"}

        def _blk_name(pos_xyz: list[int]) -> str:
            res = self.bot.call("get_block", {"pos": pos_xyz})
            return str(res.get("name") or res.get("block") or "air").removeprefix("minecraft:")

        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            floor_pos = [px + dx, py - 1, pz + dz]
            wall_pos = [px + dx, py, pz + dz]
            try:
                if _blk_name(floor_pos) in non_solid:
                    continue
                w_name = _blk_name(wall_pos)
                if w_name in fluid_or_bedrock or w_name == "crafting_table":
                    continue
                if w_name not in air_set:
                    if w_name in BLOCK_MIN_PICKAXE_TIER or "stone" in w_name or "deepslate" in w_name:
                        self._equip_if_present(tuple(PICKAXE_TIERS))
                    self.bot.call("dig", {"pos": wall_pos, "collect": True, "stop_on": ["death"]})
                placed = self.bot.call("place", {"item": item, "pos": wall_pos})
                return {"ok": True, "reused": False, "carved_pocket": wall_pos, **placed}
            except ApiError:
                continue
        return None


    def _record_utility_in_ledger(self, block_name: str, pos: Any) -> None:
        if self._mining_dfs is not None and isinstance(pos, (list, tuple)) and len(pos) >= 3:
            self._mining_dfs.ledger.record_utility(block_name, [int(pos[0]), int(pos[1]), int(pos[2])])


    def _navigate_to_distant_table(self, table_pos: list[int], *, cur_hp: float = 20.0) -> bool:
        """Navigate along active MiningDFS waypoints (if any) and move_to reach a placed crafting_table."""
        tx, ty, tz = int(table_pos[0]), int(table_pos[1]), int(table_pos[2])
        if self._mining_dfs is not None and self._mining_dfs.nodes:
            nearest_nid = min(
                self._mining_dfs.nodes.keys(),
                key=lambda nid: math.dist(self._mining_dfs.nodes[nid].anchor, (tx, ty, tz)),
            )
            if math.dist(self._mining_dfs.nodes[nearest_nid].anchor, (tx, ty, tz)) <= 16.0:
                try:
                    self._mining_dfs._navigate_to_node(nearest_nid, min_hp_ref=[cur_hp])
                except ApiError:
                    pass
        try:
            st_mid = self.bot.status()
            p_mid = st_mid.get("pos") or [9999.0, 9999.0, 9999.0]
            if math.dist((float(p_mid[0]), float(p_mid[1]), float(p_mid[2])), (tx + 0.5, ty + 0.5, tz + 0.5)) <= 3.8:
                return True
        except ApiError:
            pass
        try:
            self.bot.call("move_to", {"x": tx + 0.5, "y": ty, "z": tz + 0.5, "range": 2.2})
        except ApiError:
            try:
                self.bot.call("move_to", {"x": tx + 0.5, "z": tz + 0.5, "range": 2.2})
            except ApiError:
                pass
        try:
            st_after = self.bot.status()
            p_after = st_after.get("pos") or [9999.0, 9999.0, 9999.0]
            return math.dist((float(p_after[0]), float(p_after[1]), float(p_after[2])), (tx + 0.5, ty + 0.5, tz + 0.5)) <= 4.8
        except ApiError:
            return False


    @staticmethod
    def _has_non_wood_table_recipe_ready(target_item: str | None, counts: dict[str, int]) -> bool:
        if not target_item:
            return False
        cobble = counts.get("cobblestone", 0) + counts.get("cobbled_deepslate", 0) + counts.get("blackstone", 0)
        sticks = counts.get("stick", 0)
        reqs: dict[str, bool] = {
            "stone_pickaxe": cobble >= 3 and sticks >= 2,
            "stone_axe": cobble >= 3 and sticks >= 2,
            "stone_sword": cobble >= 2 and sticks >= 1,
            "stone_shovel": cobble >= 1 and sticks >= 2,
            "stone_hoe": cobble >= 2 and sticks >= 2,
            "furnace": cobble >= 8,
            "iron_pickaxe": counts.get("iron_ingot", 0) >= 3 and sticks >= 2,
            "iron_axe": counts.get("iron_ingot", 0) >= 3 and sticks >= 2,
            "iron_sword": counts.get("iron_ingot", 0) >= 2 and sticks >= 1,
            "iron_shovel": counts.get("iron_ingot", 0) >= 1 and sticks >= 2,
            "golden_boots": counts.get("gold_ingot", 0) >= 4,
            "diamond_pickaxe": counts.get("diamond", 0) >= 3 and sticks >= 2,
            "diamond_sword": counts.get("diamond", 0) >= 2 and sticks >= 1,
        }
        return bool(reqs.get(target_item, False))


    def _reserve_crafting_table_cleanup(self, y: int) -> None:
        if y < 55 and isinstance(self.bot, _SkillBudgetSession):
            # Keep six seconds to verify, break and pick up the table before the stop reserve.
            self.bot.deadline = min(self.bot.deadline, self.bot.finish_at - 7.0)


    def _remember_crafting_table_placement(
        self, pos: Any, temporary_tables: list[list[int]] | None
    ) -> None:
        if temporary_tables is not None and isinstance(pos, (list, tuple)) and len(pos) >= 3 and pos[1] < 55:
            cell = [int(v) for v in pos[:3]]
            if cell not in temporary_tables:
                temporary_tables.append(cell)
        else:
            self._record_utility_in_ledger("crafting_table", pos)


    def _place_nether_workbench(
        self, status: dict[str, Any], side: list[int] | None, narrow_lane: bool,
        temporary_tables: list[list[int]] | None,
    ) -> dict[str, Any]:
        from ..combat_env import HAZARD_BLOCK_TYPES, _hitbox_columns, _is_solid_ground

        origin = status.get("pos")
        if (not origin or status.get("on_ground") is not True or status.get("alive") is False
                or status.get("in_water") or status.get("in_lava")):
            return self._no_safe_utility_position()
        px, py, pz = map(math.floor, origin[:3])
        occupied = _hitbox_columns(origin[0], origin[2])
        air = {"air", "cave_air", "void_air"}
        cache: dict[tuple[int, int, int], str] = {}

        def name(x: int, y: int, z: int) -> str:
            return _cached_block_name(self.bot, x, y, z, cache)

        if narrow_lane:
            candidates = [side] if side else []
        else:
            offsets = (
                (0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (-1, 1), (1, -1), (1, 1), (0, -2), (0, 2), (-2, 0), (2, 0),
            )
            candidates = [[px + dx, py + dy, pz + dz] for dy in (0, -1, 1) for dx, dz in offsets]
        before_count = None
        for pos in candidates:
            x, y, z = pos
            if (x, z) in occupied or (self.geofence and self.geofence.check_point(x + 0.5, z + 0.5, action="mc_place")):
                continue
            floor = name(x, y - 1, z)
            if floor == "unknown" or floor in HAZARD_BLOCK_TYPES or not _is_solid_ground(floor):
                continue
            if name(x, y, z) not in air or name(x, y + 1, z) not in air:
                continue
            if before_count is None:
                before_count = _inventory_counts(self.bot).get("crafting_table", 0)
            try:
                placed = self.bot.call("place", {"item": "crafting_table", "pos": pos})
            except ApiError as err:
                if err.code in {"no_support", "blocked_by_self", "occupied"}:
                    continue
                if err.code != "place_failed":
                    raise
                observed = _cached_block_name(self.bot, x, y, z)
                if observed == "crafting_table":
                    placed = {"pos": pos}
                elif observed in air and before_count and _inventory_counts(self.bot).get("crafting_table", 0) == before_count:
                    continue
                else:
                    return {
                        "ok": False, "error": "workbench_placement_unconfirmed", "requires_replan": True,
                        "pos": pos, "message": "放置未确认，请先核对工作台坐标和背包。",
                    }
            actual = placed.get("pos") or pos
            if actual != pos or _cached_block_name(self.bot, x, y, z) != "crafting_table":
                return {
                    "ok": False, "error": "workbench_placement_unconfirmed", "requires_replan": True,
                    "pos": actual, "message": "工作台放置回执未与方块位置对上，请先核对现场和背包。",
                }
            self._remember_crafting_table_placement(actual, temporary_tables)
            return {"ok": True, "reused": False, "pos": pos, "dimension": "the_nether"}
        return self._no_safe_utility_position()


    def _step_to_nether_workbench(self, table: Any) -> bool:
        from ..combat_env import CombatEnv

        status = self.bot.status()
        origin = status.get("pos")
        if (
            not isinstance(table, (list, tuple)) or len(table) != 3 or not origin
            or status.get("on_ground") is not True or status.get("alive") is False
            or status.get("in_water") or status.get("in_lava")
            or normalize_dimension(status.get("dimension")) != "the_nether"
        ):
            return False
        x, y, z = map(int, table)
        if math.dist(origin[:3], (x + 0.5, y + 0.5, z + 0.5)) > 4.0:
            return False
        if _cached_block_name(self.bot, x, y, z) != "crafting_table":
            return False

        terrain = CombatEnv(self.bot, geofence=self.geofence)
        landings = [
            (x + dx + 0.5, z + dz + 0.5) for dx in (-1, 0, 1) for dz in (-1, 0, 1) if dx or dz
        ]
        landings.sort(key=lambda p: math.hypot(p[0] - origin[0], p[1] - origin[2]))
        for tx, tz in landings:
            distance = math.hypot(tx - origin[0], tz - origin[2])
            if not 0.5 <= distance <= 2.0:
                continue
            route = terrain.probe_retreat(origin, _yaw_toward(origin[0], origin[2], tx, tz), distance)
            if route is None or abs(route[0] - y) > 1:
                continue
            destination = (tx, route[0], tz)
            if not terrain.has_los(destination, (x + 0.5, y, z + 0.5), dst_height=0.85)["clear"]:
                continue
            try:
                moved = self.bot.call("move_to", {
                    "x": tx, "y": route[0], "z": tz, "range": 0.4, "timeout": 1500, "stop_on": ["hurt", "death"],
                }, timeout_s=2.0)
            except ApiError as err:
                if err.code in {"no_path", "path_not_found", "out_of_reach"}:
                    return False
                raise
            if moved.get("interrupted_by"):
                raise ApiError("interrupted", "接近工作台时移动中断。")
            after = self.bot.status()
            reached = after.get("pos")
            if after.get("alive") is False or after.get("last_death") != status.get("last_death"):
                raise ApiError("interrupted", "接近工作台时存活状态发生变化。")
            return bool(
                reached and math.dist(reached[:3], destination) <= 0.6 and after.get("on_ground") is True
                and not after.get("in_water") and not after.get("in_lava")
                and normalize_dimension(after.get("dimension")) == "the_nether"
                and _cached_block_name(self.bot, x, y, z) == "crafting_table"
            )
        return False


    def ensure_crafting_table(
        self,
        *,
        force_recarve: bool = False,
        target_item: str | None = None,
        temporary_tables: list[list[int]] | None = None,
        reserved_planks: int = 0,
    ) -> dict[str, Any]:
        """Reuse a reachable table or craft and place one while preserving reserved recipe planks."""
        st = self.bot.status()
        if bool(st.get("in_water")):
            self.escape_water()
            st = self.bot.status()
        px, py, pz = [int(math.floor(v)) for v in (st.get("pos") or [0, 64, 0])]
        cur_hp = float(st.get("health") if isinstance(st.get("health"), (int, float)) else 20.0)
        dim = normalize_dimension(st.get("dimension"))

        if not force_recarve:
            found = self.bot.call("find_blocks", {"radius": 8, "types": ["crafting_table"], "limit": 5})
            blocks = [
                b
                for b in (found.get("blocks") or [])
                if b.get("pos")
                and abs(int(b["pos"][1]) - py) <= 2
                and float(b.get("dist", 99)) <= 4.5
            ]
            if blocks:
                nearest = min(blocks, key=lambda b: float(b.get("dist", 999)))
                pos = nearest.get("pos")
                self._record_utility_in_ledger("crafting_table", pos)
                if dim == "the_nether":
                    center = [coord + 0.5 for coord in pos]
                    if math.dist((st.get("pos") or [px, py, pz])[:3], center) <= 3.2 or self._step_to_nether_workbench(pos):
                        return {"ok": True, "reused": True, "pos": pos, "dimension": dim}
                elif float(nearest.get("dist", 99)) > 3.2 and pos:
                    try:
                        self.bot.call("move_to", {"x": pos[0], "y": pos[1], "z": pos[2], "range": 2.0})
                        status_after_move = self.bot.status()
                        reached = status_after_move.get("pos")
                        table_center = [pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5]
                        if isinstance(reached, (list, tuple)) and len(reached) >= 3:
                            if math.dist(reached[:3], table_center) <= 3.2:
                                return {"ok": True, "reused": True, "pos": pos}
                            st = status_after_move
                            px, py, pz = map(math.floor, reached[:3])
                    except ApiError:
                        pass
                else:
                    return {"ok": True, "reused": True, "pos": pos}

        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        if counts.get("crafting_table", 0) < 1:
            required_planks = 4 + reserved_planks
            recipe_item = target_item or "crafting_table"
            wood_shortage = (
                _wood_budget_shortage(recipe_item, counts, required_planks)
                if reserved_planks else None
            )
            if wood_shortage is None:
                inv_raw, _ = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
                try:
                    counts = self._ensure_planks_and_sticks(required_planks, 0)
                except ApiError as err:
                    return {"ok": False, "error": err.code, "requires_replan": True, "message": err.message, **err.context}
                if reserved_planks:
                    wood_shortage = _wood_budget_shortage(recipe_item, counts, required_planks, count_logs=False)
            if wood_shortage is not None or _total_planks(counts) < 4:
                # Out of wood to build a new crafting_table: check wider 32-block radius & WorldLedger for an already-placed table!
                distant_candidates: list[list[int]] = []
                if not force_recarve:
                    try:
                        wide_found = self.bot.call("find_blocks", {"radius": 32, "types": ["crafting_table"], "limit": 8})
                        for wb in sorted(wide_found.get("blocks") or [], key=lambda b: float(b.get("dist", 999))):
                            wpos = wb.get("pos")
                            if isinstance(wpos, list) and len(wpos) >= 3:
                                pt_list = [int(wpos[0]), int(wpos[1]), int(wpos[2])]
                                self._record_utility_in_ledger("crafting_table", pt_list)
                                if pt_list not in distant_candidates:
                                    distant_candidates.append(pt_list)
                    except ApiError:
                        pass
                    if self.world_memory is not None:
                        wm_hits = self.world_memory.query_pois(
                            ["crafting_table"],
                            origin=(px, py, pz),
                            dim=normalize_dimension(st.get("dimension")),
                            limit=2,
                        )
                        for wh in wm_hits:
                            if wh["dist"] <= 48.0 and wh["pos"] not in distant_candidates:
                                distant_candidates.append(list(wh["pos"]))
                    if self._mining_dfs is not None:
                        ledger_tbl = self._mining_dfs.ledger.nearest_utility("crafting_table", (px, py, pz), max_dist=48.0)
                        if ledger_tbl is not None and list(ledger_tbl) not in distant_candidates:
                            try:
                                b_chk = self.bot.call("get_block", {"pos": list(ledger_tbl)})
                                if str(b_chk.get("name") or b_chk.get("block") or "").removeprefix("minecraft:") == "crafting_table":
                                    distant_candidates.append(list(ledger_tbl))
                            except ApiError:
                                pass

                for cand_pos in distant_candidates[:2]:
                    if dim == "the_nether":
                        if self._step_to_nether_workbench(cand_pos):
                            return {"ok": True, "reused": True, "pos": cand_pos, "dimension": dim}
                    elif self._navigate_to_distant_table(cand_pos, cur_hp=cur_hp):
                        return {"ok": True, "reused": True, "pos": cand_pos}

                if wood_shortage is not None:
                    return wood_shortage

                if distant_candidates:
                    tpos0 = distant_candidates[0]
                    dy_table = int(tpos0[1]) - int(py)
                    approach_msg = (
                        f"该工作台位于上方 {tpos0}（高差 +{dy_table} 格，直接 mc_move_to 不可达），请先调 mc_skill_mine_dfs(action='climb_out') 爬回地表后再靠近合成。"
                        if dy_table >= 8
                        else "无需回地表砍树，请先调 mc_skill_mine_dfs(action='return_collar') 或 mc_move_to 靠近该工作台后再合成。"
                    )
                    if dim == "the_nether":
                        approach_msg = "请先确认干燥、有支撑的近处站位，再接近旧台或补足 4 块木板就地制作。"
                    return {
                        "ok": False,
                        "error": "need_crafting_table",
                        "requires_replan": True,
                        "pos": [px, py, pz],
                        "nearest_crafting_table": tpos0,
                        "message": f"包内无富余木板新建工作台，但附近已放置有现成 crafting_table 位于 {tpos0}；{approach_msg}",
                    }

                if self._has_non_wood_table_recipe_ready(target_item, counts):
                    return {
                        "ok": False,
                        "error": "need_crafting_table",
                        "requires_replan": True,
                        "pos": [px, py, pz],
                        "item": target_item,
                        "message": (
                            f"合成 {target_item} 的原料已在背包中齐备，但 32 格内未发现已放置的 crafting_table 且包内无 4 块木板新建工作台；"
                            "请返回之前放有 crafting_table 的井口/基地合成，或补充 1 块原木制作工作台。"
                        ),
                    }

                biome = str(st.get("biome") or "")
                wood_targets = list(NETHER_STEM_TYPES) if dim == "the_nether" else list(LOG_TYPES)
                base_err: dict[str, Any] = {
                    "ok": False,
                    "error": "missing_ingredients",
                    "message": (
                        f"合成工作台需要至少 4 块木板或 1 块原木；当前位于下界({dim})，请先采集 {', '.join(NETHER_STEM_TYPES)} 菌柄合成木板。"
                        if dim == "the_nether"
                        else "合成工作台需要至少 4 块木板或 1 块原木。"
                    ),
                }
                wood_check = assess_world_target(wood_targets, dimension=dim, y=py, biome=biome, mode="find")
                if not wood_check.get("available"):
                    probe_cache: dict[tuple[int, int, int], str] = {}
                    up_dirs = (
                        self._probe_viable_up_stair_dirs(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
                        if wood_check.get("direction_needed") == "up"
                        else []
                    )
                    vpillar = (
                        False
                        if up_dirs or wood_check.get("direction_needed") != "up"
                        else self._probe_viable_pillar_up(px, py, pz, probe_cache=probe_cache, inv_counts=counts)
                    )
                    up_hint = (
                        f"请先调 mc_skill_staircase(direction='{up_dirs[0]}', vertical='up', steps=16) 返回目标高度带"
                        if up_dirs
                        else (
                            "请先调 mc_skill_staircase(vertical='pillar', steps=8) 原地垫脚升层"
                            if vpillar
                            else ""
                        )
                    )
                    return {
                        "ok": False,
                        "error": "missing_ingredients",
                        "requires_replan": True,
                        "pos": [px, py, pz],
                        "ecology_check": wood_check,
                        "underground_no_wood": wood_check.get("verdict") == "below_generation_band",
                        "viable_up_directions": up_dirs,
                        "viable_pillar_up": vpillar,
                        "message": f"合成工作台需要至少 4 块木板或 1 块原木；{wood_check['hint']} {up_hint}".strip(),
                    }
                return base_err
            try:
                self._safe_craft("crafting_table", 1)
            except ApiError as err:
                return {"ok": False, "error": err.code, "requires_replan": True, "message": err.message, **err.context}

        if dim == "the_nether":
            st = self.bot.status()
            if normalize_dimension(st.get("dimension")) != dim:
                return self._no_safe_utility_position()
            if st.get("pos"):
                py = math.floor(st["pos"][1])
        # Leave time to clear a newly placed underground table after crafting.
        if temporary_tables is not None:
            self._reserve_crafting_table_cleanup(py)
        side, narrow_lane = self._underground_utility_site(st)
        if dim == "the_nether":
            return self._place_nether_workbench(st, side, narrow_lane, temporary_tables)
        if narrow_lane:
            if side is None:
                return self._no_safe_utility_position()
            placed = self.bot.call("place", {"item": "crafting_table", "pos": side})
            self._remember_crafting_table_placement(placed.get("pos") or side, temporary_tables)
            return {"ok": True, "reused": False, "pos": side, **placed}
        try:
            placed = self.bot.call("place", {"item": "crafting_table"})
            self._remember_crafting_table_placement(placed.get("pos"), temporary_tables)
            return {"ok": True, "reused": False, **placed}
        except ApiError as err:
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 1, 0)):
                pos = [px + dx, py + dy, pz + dz]
                try:
                    placed = self.bot.call("place", {"item": "crafting_table", "pos": pos})
                    self._remember_crafting_table_placement(placed.get("pos") or pos, temporary_tables)
                    return {"ok": True, "reused": False, **placed}
                except ApiError:
                    continue
            pocket = self._carve_and_place_table(px, py, pz)
            if pocket is not None:
                self._remember_crafting_table_placement(
                    pocket.get("pos") or pocket.get("carved_pocket"), temporary_tables,
                )
                return pocket
            return {"ok": False, "error": err.code, "message": err.message}

