"""MacroSkills mixin: smelting (split from legacy library.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mc_agent.skills.library import MacroSkills

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


class _SmeltingMixin:
    def smelt_start(
        self,
        input_item: str,
        count: int = 1,
        fuel_item: str | None = None,
        *,
        allow_multi_furnace: bool = True,
        per_item_s: float = 10.0,
    ) -> dict[str, Any]:
        """Non-blocking furnace start: split the batch across 2-3 parallel furnaces and return at once."""
        clean_input, error = _validate_smelt_input(input_item)
        if error is not None:
            return error
        if not clean_input:
            return {"ok": False, "error": "bad_args", "message": "input_item 不能为空。"}
        output_item = SMELT_OUTPUTS.get(clean_input, clean_input)
        counts = _inventory_counts(self.bot)
        avail_in = int(counts.get(clean_input, 0))
        if avail_in < 1:
            return {"ok": False, "error": "missing_ingredients", "message": f"背包没有 {clean_input}。"}
        smelt_n = min(max(1, int(count)), avail_in)
        self._ensure_free_slots(min_free=3)
        counts = _inventory_counts(self.bot)

        if fuel_item:
            from ..inventory_manager import is_safe_fuel

            protected_fuel = set()
            if self.inventory_manager is not None:
                try:
                    protected_fuel = set(self.inventory_manager.active_protected())
                except (AttributeError, TypeError):
                    protected_fuel = set()
            requested = str(fuel_item).removeprefix("minecraft:").strip().lower()
            if not is_safe_fuel(requested, protected_fuel):
                return {
                    "ok": False,
                    "error": "missing_fuel",
                    "message": (
                        f"拒绝把 {requested} 当燃料：安全白名单仅允许煤/木炭/煤块/干海带块/竹/木棍/未保护木板，"
                        "严禁烧床、木工具、工作台、箱子、船、弓弩、盾牌、烈焰棒。"
                    ),
                }
        fuel, fuel_total, _per_fuel = self._choose_smelt_fuel(
            smelt_n=smelt_n, fuel_item=fuel_item, counts=counts, input_item=clean_input
        )
        if not fuel or fuel_total < 1:
            return {
                "ok": False,
                "error": "missing_fuel",
                "message": "背包没有安全燃料（煤/木炭/煤块/干海带块/竹/木棍/木板），禁止烧床/工具/工作台/船/烈焰棒。",
            }

        desired_k = min(3, max(1, smelt_n // 6)) if (allow_multi_furnace and smelt_n >= 8) else 1
        st = self.bot.status()
        try:
            found = self.bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": 6})
        except ApiError:
            found = {}
        existing: list[list[int]] = []
        for blk in found.get("blocks") or []:
            if isinstance(blk, dict) and isinstance(blk.get("pos"), list):
                pos = [int(v) for v in blk["pos"][:3]]
                if pos not in existing:
                    existing.append(pos)
        existing.sort(key=lambda p: math.dist(p, st.get("pos") or [0, 64, 0]))

        furnaces: list[dict[str, Any]] = []
        placed_new: list[list[int]] = []
        try:
            for pos in existing[:desired_k]:
                furnaces.append({"pos": pos, "source": "existing"})
            need_new = max(0, desired_k - len(furnaces))
            if need_new > 0:
                # keep >=16 cobblestone after crafting furnaces
                cobble = int(counts.get("cobblestone", 0))
                can_craft = min(need_new, max(0, (cobble - 16) // 8))
                if can_craft > 0:
                    try:
                        self._safe_craft("furnace", can_craft)
                    except ApiError as craft_err:
                        if craft_err.code == "need_crafting_table":
                            # craft_gear builds/borrows a crafting table (live bot usually has planks)
                            try:
                                cg = self.craft_gear("furnace", can_craft)
                                if not isinstance(cg, dict) or not cg.get("ok"):
                                    can_craft = 0
                            except ApiError:
                                can_craft = 0
                        else:
                            can_craft = 0
                owned_furnaces = int(_inventory_counts(self.bot).get("furnace", 0))
                to_place = min(need_new, owned_furnaces)
                for _ in range(to_place):
                    pos = self._place_furnace_near(placed_new)
                    if pos is None:
                        break
                    placed_new.append(pos)
                    furnaces.append({"pos": pos, "source": "placed"})
            if not furnaces:
                return {
                    "ok": False,
                    "error": "need_furnace",
                    "message": "附近没有熔炉，且圆石不足（扣合炉消耗后需保底 >=16 圆石）无法补造。",
                }

            k = len(furnaces)
            base = smelt_n // k
            extra = smelt_n % k
            shares = [base + (1 if i < extra else 0) for i in range(k)]
            jobs: list[dict[str, Any]] = []
            now = time.time()
            remaining_fuel = fuel_total
            for idx, fur in enumerate(furnaces):
                part = shares[idx]
                if part <= 0:
                    continue
                per = float(_per_fuel or 1.5)
                fuel_need = max(1, math.ceil(part / max(0.25, per)))
                fuel_need = min(fuel_need, max(0, remaining_fuel))
                if fuel_need < 1:
                    continue
                remaining_fuel -= fuel_need
                try:
                    self.bot.call(
                        "window/act",
                        {
                            "target": {"pos": fur["pos"]},
                            "ops": [
                                {"op": "put", "item": clean_input, "count": part, "to": "input"},
                                {"op": "put", "item": fuel, "count": fuel_need, "to": "fuel"},
                            ],
                        },
                    )
                except ApiError as err:
                    jobs.append(
                        {
                            "pos": fur["pos"],
                            "error": err.code,
                            "message": err.message,
                            "input_count": part,
                        }
                    )
                    continue
                ready_at = now + per_item_s * part + 5.0
                job = self._get_world_memory().register_conversion_job(
                    pos=fur["pos"],
                    input_item=clean_input,
                    output_item=output_item,
                    count=part,
                    dim=self._current_dim(),
                    furnaces=1,
                    per_item_s=per_item_s,
                    ready_at=ready_at,
                )
                jobs.append(
                    {
                        "pos": fur["pos"],
                        "source": fur["source"],
                        "job_id": job["id"],
                        "input_count": part,
                        "fuel": {"item": fuel, "count": fuel_need},
                        "ready_at": ready_at,
                        "estimated_wait_s": round(per_item_s * part + 5.0, 1),
                    }
                )
        except ApiError as err:
            if err.code in ("hurt", "death", "interrupted"):
                raise
            return {"ok": False, "error": err.code, "message": err.message}

        ok_jobs = [j for j in jobs if "job_id" in j]
        total_ready = max((j["ready_at"] for j in ok_jobs), default=time.time())
        return {
            "ok": bool(ok_jobs),
            "input": clean_input,
            "output_item": output_item,
            "requested_count": smelt_n,
            "furnace_count": len(ok_jobs),
            "furnaces": ok_jobs,
            "failures": [j for j in jobs if "job_id" not in j],
            "fuel": {"item": fuel, "total": fuel_total},
            "estimated_wait_s": round(max((j["estimated_wait_s"] for j in ok_jobs), default=0.0), 1),
            "ready_at": total_ready,
            "in_conversion_counts": self._get_world_memory().get_in_conversion_counts(),
            "non_blocking": True,
        }


    def _place_furnace_near(self, placed: list[list[int]]) -> list[int] | None:
        st = self.bot.status()
        p = st.get("pos") or [0, 64, 0]
        bx, by, bz = math.floor(float(p[0])), math.floor(float(p[1])), math.floor(float(p[2]))
        offsets = [(1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (0, 2), (-2, 0), (0, -2)]
        for dx, dz in offsets:
            cell = [bx + dx, by, bz + dz]
            if cell in placed:
                continue
            try:
                below = self.bot.call("get_block", {"pos": [cell[0], cell[1] - 1, cell[2]]})
                here = self.bot.call("get_block", {"pos": cell})
            except ApiError:
                continue
            below_name = str(below.get("name") or "air").removeprefix("minecraft:")
            here_name = str(here.get("name") or "air").removeprefix("minecraft:")
            if below_name in ("air", "water", "lava") or here_name not in ("air", "cave_air"):
                continue
            try:
                res = self.bot.call("place", {"item": "furnace", "pos": cell})
                pos = res.get("pos") if isinstance(res, dict) else None
                return [int(v) for v in (pos or cell)[:3]]
            except ApiError:
                continue
        return None


    def smelt_collect(
        self,
        furnaces: Any = None,
        furnace_pos: Any = None,
        *,
        wait_remaining_s: float = 15.0,
        dim: str | None = None,
    ) -> dict[str, Any]:
        """Collect finished furnace outputs by coordinate and settle the conversion ledger."""
        mem = self._get_world_memory()
        norm_dim = self._current_dim(dim)
        targets: list[dict[str, Any]] = []
        raw_positions: list[list[int]] = []
        if furnace_pos is not None:
            raw_positions.append([int(v) for v in list(furnace_pos)[:3]])
        if isinstance(furnaces, (list, tuple)):
            for row in furnaces:
                if isinstance(row, dict) and isinstance(row.get("pos"), (list, tuple)):
                    raw_positions.append([int(v) for v in row["pos"][:3]])
                elif isinstance(row, (list, tuple)) and len(row) >= 3:
                    raw_positions.append([int(v) for v in row[:3]])
        jobs = mem.get_conversion_jobs(dim=norm_dim)
        if not raw_positions:
            raw_positions = [[int(v) for v in j["pos"][:3]] for j in jobs]
        for pos in {tuple(p): p for p in raw_positions}.values():
            job = next(
                (
                    j
                    for j in jobs
                    if [int(v) for v in j.get("pos", [])[:3]] == pos
                ),
                None,
            )
            targets.append({"pos": pos, "job": job})
        if not targets:
            return {"ok": True, "collected": [], "in_conversion_counts": {}, "reason": "no_jobs"}

        wall_deadline = time.time() + max(0.0, float(wait_remaining_s))
        collected: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for tgt in targets:
            pos = tgt["pos"]
            job = tgt["job"]
            try:
                self.bot.call("move_to", {"x": pos[0] + 0.5, "z": pos[2] + 0.5, "range": 2.5, "stop_on": ["death"]})
            except Exception:  # noqa: BLE001
                pass
            # short wait for tail-end smelting (never blocks longer than wait_remaining_s)
            take_ops: list[dict[str, Any]] = []
            got: dict[str, int] = {}
            while True:
                try:
                    view = self.bot.call("window/view", {"target": {"pos": pos}})
                except (ApiError, httpx.HTTPError) as err:
                    code = str(getattr(err, "code", type(err).__name__))
                    message = str(getattr(err, "message", err))
                    failures.append({"pos": pos, "error": code, "message": message})
                    # Furnace was broken/replaced: write the job off so the ledger cannot go stale.
                    if code in ("no_window", "not_found", "blocked", "cannot_interact") and job is not None:
                        try:
                            block_name = self._container_block_at(pos)
                        except Exception:  # noqa: BLE001
                            block_name = None
                        if block_name != "furnace":
                            mem.complete_conversion_job(
                                job.get("id"), collected=int(job.get("remaining") or 0)
                            )
                    break
                sections = view.get("sections") or {}
                outputs = [o for o in (sections.get("output") or []) if int(o.get("count") or 0) > 0]
                inputs_left = sum(int(x.get("count") or 0) for x in (sections.get("input") or []))
                burning = bool((view.get("info") or {}).get("burning"))
                out_total = sum(int(o.get("count") or 0) for o in outputs)
                job_remaining = int((job or {}).get("remaining") or 0)
                # Only take when the batch is really finished (or the caller's wait budget expired):
                # a mid-batch "not burning" gap must not trigger a premature partial take.
                finished = inputs_left == 0 and not burning
                complete_enough = job_remaining > 0 and out_total >= job_remaining and inputs_left == 0
                if outputs and (finished or complete_enough):
                    for out in outputs:
                        cnt = int(out.get("count") or 0)
                        name = _clean_block(out.get("name") or ((job or {}).get("output_item") or ""))
                        take_ops.append({"op": "take", "from": "output", "count": cnt})
                        got[name] = got.get(name, 0) + cnt
                    break
                if not burning and inputs_left == 0 and not outputs:
                    break
                if time.time() >= wall_deadline:
                    if outputs and (inputs_left == 0 or out_total >= job_remaining):
                        for out in outputs:
                            cnt = int(out.get("count") or 0)
                            name = _clean_block(out.get("name") or "")
                            take_ops.append({"op": "take", "from": "output", "count": cnt})
                            got[name] = got.get(name, 0) + cnt
                    break
                time.sleep(1.0)
            if not take_ops:
                if job is not None:
                    failures.append({"pos": pos, "error": "no_output_yet", "job_id": job.get("id")})
                continue
            try:
                inv_now, dropped = self._ensure_free_slots(min_free=2)
            except (ApiError, httpx.HTTPError) as err:
                failures.append({
                    "pos": pos,
                    "error": str(getattr(err, "code", type(err).__name__)),
                    "message": str(getattr(err, "message", err)),
                    "got": got,
                })
                continue
            if dropped:
                got.setdefault("_auto_dropped", 0)
            try:
                request = {"target": {"pos": pos}, "ops": take_ops}
                if job is not None and job.get("kind") == "sync_furnace":
                    taken, _ = self._take_sync_furnace_output(self.bot, request, got, inv_now, pos, norm_dim)
                else:
                    res = self.bot.call("window/act", request)
                    taken = self._furnace_take_receipt(self.bot, res, got, inv_now, legacy_preview=True)
            except (ApiError, httpx.HTTPError) as err:
                failures.append({
                    "pos": pos,
                    "error": str(getattr(err, "code", type(err).__name__)),
                    "message": str(getattr(err, "message", err)),
                    "got": got,
                })
                continue
            if not taken:
                failures.append({"pos": pos, "error": "no_output_taken", "job_id": (job or {}).get("id")})
                continue
            self._settle_furnace_output(pos, norm_dim, taken)
            collected.append({"pos": pos, "took": taken, "job_id": (job or {}).get("id")})

        return {
            "ok": bool(collected) or not failures,
            "collected": collected,
            "failures": failures,
            "in_conversion_counts": mem.get_in_conversion_counts(),
            "jobs_remaining": len(mem.get_conversion_jobs(dim=norm_dim)),
        }


    @staticmethod
    def _furnace_take_receipt(
        bot: Any, result: dict[str, Any], preview: dict[str, int], inventory_before: dict[str, Any],
        *, legacy_preview: bool = False,
    ) -> dict[str, int]:
        done = result.get("done")
        receipts = [
            step["took"] for step in done
            if isinstance(step, dict) and isinstance(step.get("took"), dict)
        ] if isinstance(done, list) else []
        if not receipts:
            if legacy_preview and done != []:
                return preview
            gained = _positive_inv_delta(_inventory_counts_from_raw(inventory_before), _inventory_counts(bot))
            return {item: min(count, gained[item]) for item, count in preview.items() if count > 0 and item in gained}
        taken: dict[str, int] = {}
        for receipt in receipts:
            _merge_counts(taken, {_clean_block(name): int(n) for name, n in receipt.items()})
        return taken


    def _take_sync_furnace_output(
        self, bot: Any, request: dict[str, Any], preview: dict[str, int], inventory_before: dict[str, Any],
        pos: list[int] | None, dim: str | None,
    ) -> tuple[dict[str, int], dict[str, Any] | None]:
        counts_before = _inventory_counts_from_raw(inventory_before)
        keys = {
            (dim, tuple(pos), item): count for item, count in preview.items() if count > 0
        } if pos is not None and dim is not None else {}
        for key, count in keys.items():
            if key in self._pending_furnace_takes:
                reconciled = self._reconcile_furnace_take(key, count, inventory_before)
                if reconciled is None or reconciled:
                    return reconciled or {}, None
        for key, count in keys.items():
            self._pending_furnace_takes[key] = (counts_before.get(key[2], 0), count)
        result = bot.call("window/act", request)
        taken = self._furnace_take_receipt(bot, result, preview, inventory_before)
        if taken:
            for key in keys:
                self._pending_furnace_takes.pop(key, None)
        window = result.get("window")
        return taken, window if isinstance(window, dict) else None


    def _reconcile_furnace_take(
        self, key: tuple[str, tuple[int, ...], str], output_count: int, inventory: dict[str, Any],
    ) -> dict[str, int] | None:
        """Return None when a previous take is still ambiguous, or {} when it can be retried."""
        previous_inventory, previous_output = self._pending_furnace_takes[key]
        item = key[2]
        inventory_gain = _inventory_counts_from_raw(inventory).get(item, 0) - previous_inventory
        output_loss = previous_output - output_count
        if inventory_gain > 0 and output_loss > 0:
            del self._pending_furnace_takes[key]
            return {item: min(inventory_gain, output_loss, previous_output)}
        if inventory_gain == 0 and output_loss == 0:
            del self._pending_furnace_takes[key]
            return {}
        return None


    def _settle_furnace_output(self, pos: list[int], dim: str, taken: dict[str, int]) -> None:
        mem = self._get_world_memory()
        unbooked = dict(taken)
        for job in mem.get_conversion_jobs(dim=dim):
            if job["pos"] != pos:
                continue
            item = job["output_item"]
            credited = min(job["remaining"], unbooked.get(item, 0))
            if credited > 0:
                mem.complete_conversion_job(job["id"], collected=credited)
                unbooked[item] -= credited


    def record_window_furnace_take(self, pos: list[int], receipt: dict[str, Any]) -> None:
        """Credit confirmed manual output transfers at a tracked furnace."""
        window = receipt.get("window")
        done = receipt.get("done")
        if not isinstance(window, dict) or _clean_block(window.get("window")) != "furnace" or not isinstance(done, list):
            return
        taken: dict[str, int] = {}
        for step in done:
            if not isinstance(step, dict) or step.get("op") != "take" or step.get("from") != "output":
                continue
            items = step.get("took")
            if isinstance(items, dict):
                _merge_counts(taken, {_clean_block(item): n for item, n in items.items() if type(n) is int and n > 0})
        if not taken or not any(
            job["pos"] == pos and job["output_item"] in taken
            for job in self._get_world_memory().get_conversion_jobs()
        ):
            return
        try:
            dimension = self.bot.call("status", method="GET", timeout_s=1.0).get("dimension")
        except (ApiError, httpx.HTTPError):
            return
        if not dimension:
            return
        dim = normalize_dimension(dimension)
        self._settle_furnace_output(pos, dim, taken)
        for item, amount in taken.items():
            key = (dim, tuple(pos), item)
            if key in self._pending_furnace_takes:
                inventory_before, output_before = self._pending_furnace_takes[key]
                # A later reconciliation must only credit the earlier unconfirmed transfer.
                self._pending_furnace_takes[key] = (inventory_before + amount, output_before)


    def _reclaim_empty_temporary_furnace(
        self, session: _SkillBudgetSession, pos: list[int], dim: str,
    ) -> dict[str, Any] | None:
        key = (dim, tuple(pos))
        if key not in self._temporary_furnaces or any(
            job["pos"] == pos for job in self._get_world_memory().get_conversion_jobs(dim=dim)
        ):
            return None
        if session.deadline - time.monotonic() < 2.0:
            return None
        try:
            block = session.call("get_block", {"pos": pos})
            name = _clean_block(block.get("name") or block.get("block"))
            if name != "furnace":
                if name:
                    self._temporary_furnaces.discard(key)
                return None
            if str((block.get("properties") or {}).get("lit")).lower() == "true":
                return None
            view = session.call("window/view", {"target": {"pos": pos}})
            sections = view.get("sections") or {}
            if view.get("window") != "furnace" or (view.get("info") or {}).get("burning") is not False:
                return None
            if any(sections.get(slot) != [] for slot in ("input", "fuel", "output")):
                return None
            inv = session.inventory()
            _, pick_tier = _best_pickaxe_in_inv(_inventory_counts_from_raw(inv))
            if pick_tier == 0 or not _can_fit_item_in_inventory(inv, "furnace", 1, check_craft_inputs=False):
                return None
            dug = session.call("dig", {"pos": pos, "collect": True, "stop_on": ["hurt", "death"]})
        except (_SkillDeadline, httpx.TimeoutException):
            session.stop_pending()
            return {"pos": pos, "removed": False, "recovered": 0, "error": "cleanup_unconfirmed"}
        except (ApiError, httpx.HTTPError) as error:
            return {"pos": pos, "removed": False, "recovered": 0, "error": getattr(error, "code", type(error).__name__)}
        removed = _clean_block(dug.get("dug")) == "furnace"
        if removed:
            self._temporary_furnaces.discard(key)
            self._get_world_memory().remove_poi(pos, dim=dim)
        return {"pos": pos, "removed": removed, "recovered": int((dug.get("gained") or {}).get("furnace", 0))}


    def collect_nearby_smelt_output(self, max_duration_s: float) -> dict[str, Any] | None:
        """Recover output or reclaim an empty temporary furnace without walking."""
        jobs = [j for j in self._get_world_memory().get_conversion_jobs() if j["kind"] == "sync_furnace"]
        if self._pending_furnace_takes:
            active_keys = {(j["dim"], tuple(j["pos"]), j["output_item"]) for j in jobs}
            for key in self._pending_furnace_takes.keys() - active_keys:
                del self._pending_furnace_takes[key]
        if (not jobs and not self._temporary_furnaces) or max_duration_s < 3.0:
            return None

        session = _SkillBudgetSession(self.bot, max_duration_s)
        try:
            status = session.call("status", method="GET", timeout_s=1.0)
            origin = status.get("pos")
            if not origin or status.get("in_water") or status.get("in_lava"):
                return None
            dim = normalize_dimension(status.get("dimension"))
            furnace_origin = [n - 0.5 for n in origin[:3]]
            candidates: dict[tuple[int, ...], dict[str, Any] | None] = {}
            for job in jobs:
                if job["dim"] == dim and math.dist(furnace_origin, job["pos"]) <= 3.0:
                    candidates.setdefault(tuple(job["pos"]), job)
            for site_dim, site_pos in self._temporary_furnaces:
                if site_dim == dim and math.dist(furnace_origin, site_pos) <= 3.0:
                    candidates.setdefault(site_pos, None)
            nearest = min(
                candidates, key=lambda p: (candidates[p] is None, math.dist(furnace_origin, p)), default=None,
            )
            if nearest is None:
                return None
            pos = list(nearest)
            job = candidates[nearest]
            if not job:
                cleanup = self._reclaim_empty_temporary_furnace(session, pos, dim)
                return {"pos": pos, "furnace_cleanup": cleanup} if cleanup else None

            item = job["output_item"]
            target = {"pos": pos}
            view = session.call("window/view", {"target": target})
            output = (view.get("sections") or {}).get("output")
            if not isinstance(output, list):
                return None
            count = sum(int(row.get("count") or 0) for row in output if _clean_block(row.get("name")) == item)
            key = (dim, tuple(pos), item)
            pending = key in self._pending_furnace_takes
            if count <= 0 and not pending:
                return None
            inventory_before = session.inventory()
            taken = self._reconcile_furnace_take(key, count, inventory_before) if pending else {}
            if taken is None:
                return None
            if not taken:
                if count <= 0 or not _can_fit_item_in_inventory(inventory_before, item, count, check_craft_inputs=False):
                    return None
                request = {"target": target, "ops": [{"op": "take", "from": "output", "item": item, "count": count}]}
                taken, _ = self._take_sync_furnace_output(session, request, {item: count}, inventory_before, pos, dim)
        except (_SkillDeadline, httpx.TimeoutException):
            session.stop_pending()
            return None
        except (ApiError, httpx.HTTPError) as err:
            if isinstance(err, ApiError) and err.code in ("hurt", "death", "interrupted"):
                raise
            return None

        if not taken:
            return None
        self._settle_furnace_output(pos, dim, taken)
        recovered = {"pos": pos, "took": taken}
        cleanup = self._reclaim_empty_temporary_furnace(session, pos, dim)
        if cleanup:
            recovered["furnace_cleanup"] = cleanup
        return recovered


    def _pending_sync_smelt_batch(
        self, pos: list[int] | None, dim: str | None, input_item: str,
    ) -> dict[str, Any] | None:
        if pos is None or dim is None:
            return None
        item = _clean_block(input_item)
        pending = sum(
            j["remaining"] for j in self._get_world_memory().get_conversion_jobs(dim=dim)
            if j["kind"] == "sync_furnace" and j["pos"] == pos and j["input_item"] == item
        )
        if pending <= 0:
            return None
        return {
            "ok": False, "error": "pending_smelt_batch", "requires_replan": True,
            "furnace_pos": pos, "pending_count": pending, "output_item": SMELT_OUTPUTS.get(item),
            **self._furnace_collection_guidance(pos),
        }


    def _nearby_pending_sync_smelt_batch(self, input_item: str) -> dict[str, Any] | None:
        jobs = [
            job for job in self._get_world_memory().get_conversion_jobs()
            if job["kind"] == "sync_furnace" and job["input_item"] == input_item and job["remaining"] > 0
        ]
        if not jobs:
            return None
        status = self.bot.status()
        dim = normalize_dimension(status.get("dimension"))
        origin = status.get("pos")
        if not origin:
            return None
        nearby = min(
            ((math.dist(origin[:3], job["pos"]), job["pos"]) for job in jobs if job["dim"] == dim),
            default=None,
        )
        if nearby is None or nearby[0] > 4.0:
            return None
        return self._pending_sync_smelt_batch(nearby[1], dim, input_item)


    def _record_sync_furnace_load(
        self, pos: list[int] | None, dim: str | None, item: str, result: dict[str, Any], fallback_count: int
    ) -> None:
        item = _clean_block(item)
        output = SMELT_OUTPUTS.get(item)
        if pos is None or dim is None or output is None:
            return
        done = result.get("done")
        receipts = [
            int(op["count"]) for op in done
            if isinstance(op, dict) and op.get("to") == "input"
            and _clean_block(op.get("put")) == item and op.get("count") is not None
        ] if isinstance(done, list) else []
        loaded = sum(receipts) if receipts else fallback_count
        if loaded > 0:
            self._get_world_memory().register_conversion_job(
                pos=pos, input_item=item, output_item=output, count=loaded, dim=dim, kind="sync_furnace",
            )


    def smelt_items(
        self,
        input_item: str,
        count: int = 1,
        fuel_item: str | None = None,
        wait_timeout_s: float = 95.0,
    ) -> dict[str, Any]:
        """Load, poll and collect from a furnace within one budget."""
        input_item, error = _validate_smelt_input(input_item)
        if error is not None:
            return error
        started = time.monotonic()
        session = _SkillBudgetSession(self.bot, wait_timeout_s)
        scoped = copy(self)
        scoped.bot = session
        scoped.world_memory = self._get_world_memory()
        scoped._smelt_inventory_reserve = {input_item: max(1, count)}
        try:
            result = scoped._smelt_items(input_item, count, fuel_item, session)
        except (_SkillDeadline, httpx.TimeoutException):
            session.stop_pending()
            if "fuel" not in session.progress:
                return {
                    **session.progress,
                    "ok": False,
                    "error": "smelt_timeout",
                    "requires_replan": True,
                    "message": "熔炼准备未在预算内确认。请先用 mc_window_view 检查炉内原料和燃料，再决定是否补料。",
                }
            result = self._partial_smelt(session.progress)
        if result.get("error") == "partial_smelt":
            result["elapsed_s"] = round(time.monotonic() - started, 2)
        return result


    @staticmethod
    def _furnace_collection_guidance(pos: list[int] | None) -> dict[str, Any]:
        if pos is None:
            return {
                "message": "炉位未确认。先用 mc_window_view 核对原炉位置，再用 mc_window_act 从 output 槽取成品。",
            }
        target = {"block": "furnace", "x": pos[0], "y": pos[1], "z": pos[2]}
        return {
            "message": "先查看这座炉。output 有成品时执行第二个调用取货，等本批结清再投入新原料。",
            "next_actions": [
                {"name": "mc_window_view", "arguments": target},
                {"name": "mc_window_act", "arguments": {**target, "ops": [{"op": "take", "from": "output"}]}},
            ],
        }


    @staticmethod
    def _observe_smelt_furnace(progress: dict[str, Any], window: dict[str, Any], *, phase: str) -> None:
        sections = window.get("sections") or {}
        observed: dict[str, Any] = {}
        for slot in ("input", "fuel", "output"):
            rows = sections.get(slot)
            if isinstance(rows, list):
                observed[slot] = sum(int(row.get("count") or 0) for row in rows if isinstance(row, dict))
                if slot == "input" and len(rows) == 1 and isinstance(rows[0], dict):
                    input_item = _clean_block(rows[0].get("name"))
                    if input_item:
                        observed["input_item"] = input_item
        burning = (window.get("info") or {}).get("burning")
        if isinstance(burning, bool):
            observed["burning"] = burning
        if observed:
            progress["last_furnace_observation"] = {**observed, "phase": phase}


    def _partial_smelt(self, progress: dict[str, Any], session: _SkillBudgetSession | None = None) -> dict[str, Any]:
        furnace = progress.get("last_furnace_observation") or {}
        guidance = self._furnace_collection_guidance(progress.get("furnace_pos"))
        state = "check_furnace"
        if furnace.get("phase") == "after_take" and furnace.get("output", 0) > 0:
            state = "output_ready"
            guidance["message"] = "最近一次取货回执显示炉内仍有成品。先复读原炉，再从 output 槽取出。"
        elif furnace.get("input", 0) > 0:
            if furnace.get("burning") is False and furnace.get("fuel") == 0:
                state = "needs_fuel"
                refill = self._plan_furnace_refill(progress, furnace, session)
                if refill is None:
                    guidance["message"] = (
                        "原炉仍有原料，最近一次炉窗显示停火且燃料槽空。"
                        "复读原炉，确认可用燃料足够烧完余料后只补 fuel 槽。"
                        "若不足，请先补煤或额外木料，保留首把铁镐所需材料。"
                        "已有成品可先取，随后在 output 再有成品时取货。"
                    )
                else:
                    item, count = refill
                    target = guidance["next_actions"][0]["arguments"]
                    guidance["next_actions"].insert(1, {
                        "name": "mc_window_act",
                        "arguments": {**target, "ops": [{"op": "put", "item": item, "count": count, "to": "fuel"}]},
                    })
                    guidance["message"] = (
                        f"先复读原炉。仅在仍停火、燃料槽空且原料仍为 {furnace['input_item']} ×{furnace['input']} 时，"
                        f"用第二个调用只补 fuel 槽（{item} ×{count}）。确认 output 有成品时再执行取货。"
                    )
            elif furnace.get("burning") is True:
                state = "processing"
                guidance["message"] = "原炉最近仍在烧剩余原料。可先做其它动作，之后复读并从 output 槽取成品。"
        iron_collected = sum(row["count"] for row in progress.get("collected", []) if row["name"] == "iron_ingot")
        if SMELT_OUTPUTS.get(progress.get("input")) == "iron_ingot" and iron_collected >= 3:
            guidance["enough_for_iron_pickaxe"] = True
            guidance["message"] = (
                f"已确认取出 {iron_collected} 个铁锭。若目标是首把铁镐，备齐木棍和工作台即可先合成。"
                f"{guidance['message']}"
            )
        return {
            **progress,
            "ok": False,
            "error": "partial_smelt",
            "requires_replan": True,
            "furnace_state": state,
            **guidance,
        }


    def _plan_furnace_refill(
        self, progress: dict[str, Any], furnace: dict[str, Any], session: _SkillBudgetSession | None,
    ) -> tuple[str, int] | None:
        input_item = progress.get("input")
        if (
            session is None or progress.get("furnace_pos") is None
            or not input_item or furnace.get("input_item") != input_item
        ):
            return None
        budget = min(1.0, session.deadline - time.monotonic())
        read = _SkillBudgetSession(session, budget, single_attempt=True)
        try:
            inventory = read.call("inventory", method="GET")
        except (_SkillDeadline, ApiError, httpx.HTTPError):
            return None
        counts = _inventory_counts_from_raw(inventory)
        spendable, _ = self._first_iron_pick_fuel_stock(counts, input_item, 0, preserve_short_stock=True)
        # The rest of this batch is already in the furnace, outside the inventory snapshot.
        remaining = furnace["input"]
        one_slot = {item: min(count, 64) for item, count in (spendable or {}).items()}
        item, count, per_item = self._choose_smelt_fuel(
            smelt_n=remaining, fuel_item=(progress.get("fuel") or {}).get("item"), counts=one_slot, input_item="",
        )
        return (item, count) if item and count * per_item >= remaining else None


    @staticmethod
    def _missing_smelt_fuel(
        input_item: str, counts: dict[str, int], furnace_pos: list[int] | None = None,
        *, reserved_planks: int | None = None, smelt_count: int = 0,
    ) -> dict[str, Any]:
        failure = {
            "ok": False, "requires_replan": True, "error": "missing_fuel",
            "input": input_item, "furnace_pos": furnace_pos, "collected": [], "inventory": counts,
        }
        if reserved_planks is not None:
            reserve = {"stick": min(2, counts.get("stick", 0)), "plank_equivalent": reserved_planks}
            if SMELT_OUTPUTS.get(input_item) == "charcoal":
                available = min(counts.get(input_item, 0), max(0, (_plank_equivalent(counts) - reserved_planks) // 4))
                if smelt_count > available:
                    return {
                        **failure,
                        "error": "reserved_materials",
                        "reserved_for_iron_pickaxe": reserve,
                        "max_charcoal_input_after_reserve": available,
                        "message": (
                            f"预留首把铁镐与工作台的木料后，最多可投 {available} 根原木烧木炭。"
                            "请优先用煤，或补足额外木料再烧木炭。"
                        ),
                    }
            return {
                **failure,
                "reserved_for_iron_pickaxe": reserve,
                "next_action": "mc_skill_mine_blocks(block_types=['coal_ore'], count=3)",
                "message": "扣除首把铁镐和工作台所需木料后没有可用燃料。请补煤、木炭或额外木料再熔炼。",
            }
        return {
            **failure,
            "next_action": "mc_skill_harvest_logs(target_logs=2)",
            "message": (
                "缺少本批可用燃料。本次停在燃料预检，取出的成品为 0。"
                "请先调 mc_skill_harvest_logs(target_logs=2, radius=32) 砍 2 个原木（自动转木板作燃料），"
                "或用 mc_skill_mine_blocks(block_types=['coal_ore'], count=3) 采煤；"
                "拿到燃料后再用相同 input_item 调用 mc_skill_smelt。不要无燃料重复调用。"
            ),
        }


    def _smelt_items(
        self, input_item: str, count: int, fuel_item: str | None, session: _SkillBudgetSession
    ) -> dict[str, Any]:
        inv_raw, counts = _inventory_snapshot_and_counts(self.bot)
        if counts.get(input_item, 0) < 1:
            return {"ok": False, "error": "missing_ingredients", "message": f"背包没有 {input_item}。"}
        fuel_item = _clean_block(fuel_item) or None
        smelt_n = min(max(1, count), counts[input_item])
        fuel, fuel_log, reserved_planks = self._plan_sync_smelt_fuel(counts, input_item, smelt_n, fuel_item)
        if fuel[0] is None and fuel_log is None:
            return self._nearby_pending_sync_smelt_batch(input_item) or self._missing_smelt_fuel(
                input_item, counts, reserved_planks=reserved_planks, smelt_count=smelt_n,
            )
        inv_raw, pre_dropped = self._ensure_free_slots(min_free=5, inv_snapshot=inv_raw)
        if pre_dropped:
            counts = _inventory_counts_from_raw(inv_raw)

        # Locate or build+place furnace
        found = self.bot.call("find_blocks", {"radius": 8, "types": ["furnace"], "limit": 5})
        furnaces = found.get("blocks") or []
        furnace_pos = None
        if furnaces:
            nearest = min(furnaces, key=lambda b: float(b.get("dist", 99)))
            furnace_pos = nearest["pos"]
            if float(nearest.get("dist", 99)) > 4.0:
                try:
                    self.bot.call("move_to", {"x": furnace_pos[0], "y": furnace_pos[1], "z": furnace_pos[2], "range": 2.5})
                except ApiError:
                    furnace_pos = None
        try:
            furnace_status = self.bot.status()
            dim = normalize_dimension(furnace_status.get("dimension"))
        except ApiError:
            furnace_status = {}
            dim = None
        if furnace_pos is None:
            if counts.get("furnace", 0) < 1:
                cg = self.craft_gear("furnace", 1)
                if not cg.get("ok"):
                    return cg
            side, narrow_lane = self._underground_utility_site(furnace_status)
            if narrow_lane and side is None:
                return self._no_safe_utility_position()
            payload = {"item": "furnace"}
            if side is not None:
                payload["pos"] = side
            try:
                placed = self.bot.call("place", payload)
                furnace_pos = placed.get("pos") or side
            except ApiError as err:
                if narrow_lane:
                    return {"ok": False, "error": err.code, "message": err.message, **err.context}
                fpos = furnace_status.get("pos") or self.bot.status().get("pos") or [0, 64, 0]
                fx, fy, fz = math.floor(float(fpos[0])), math.floor(float(fpos[1])), math.floor(float(fpos[2]))
                for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (1, 1, 0)):
                    try:
                        placed = self.bot.call("place", {"item": "furnace", "pos": [fx + dx, fy + dy, fz + dz]})
                        furnace_pos = placed.get("pos") or [fx + dx, fy + dy, fz + dz]
                        break
                    except ApiError:
                        continue
                if furnace_pos is None:
                    pocket = self._carve_and_place_table(fx, fy, fz, item="furnace")
                    if pocket is not None:
                        furnace_pos = pocket.get("pos") or pocket.get("carved_pocket")
                    else:
                        return {"ok": False, "error": err.code, "message": err.message, **err.context}
            if furnace_pos and dim is not None and int(furnace_pos[1]) < 55:
                self._temporary_furnaces.add((dim, tuple(int(v) for v in furnace_pos[:3])))

        pending_batch = self._pending_sync_smelt_batch(furnace_pos, dim, input_item)
        if pending_batch:
            return pending_batch
        target = {"pos": furnace_pos} if furnace_pos else {"block": "furnace"}
        counts = _inventory_counts(self.bot)
        # Preparing the furnace can consume the same material the caller wants to smelt.
        smelt_n = min(max(1, count), counts.get(input_item, 0))
        if smelt_n < 1:
            return {
                "ok": False, "error": "missing_ingredients", "requires_replan": True,
                "input": input_item, "furnace_pos": furnace_pos,
                "message": f"准备熔炉后背包已没有 {input_item}，请补足原料再烧制。",
            }

        fuel, fuel_log, reserved_planks = self._plan_sync_smelt_fuel(counts, input_item, smelt_n, fuel_item)
        if fuel_log is not None:
            preferred_fuel = fuel_item
            try:
                self._safe_craft(_log_to_planks_name(fuel_log), 4)
            except ApiError as err:
                if err.code in _PICKUP_INTERRUPTION_CODES:
                    raise
                preferred_fuel = fuel_log if fuel[0] is None else fuel_item
            counts = _inventory_counts(self.bot)
            smelt_n = min(smelt_n, counts.get(input_item, 0))
            spendable, reserved_planks = self._first_iron_pick_fuel_stock(counts, input_item, smelt_n)
            fuel = self._choose_smelt_fuel(
                smelt_n=smelt_n, fuel_item=preferred_fuel, counts=spendable, input_item=input_item,
            ) if smelt_n > 0 and spendable is not None else (None, 0, 0.0)
        chosen_fuel, fuel_count, _ = fuel
        if not chosen_fuel or fuel_count < 1:
            return self._missing_smelt_fuel(
                input_item, counts, furnace_pos, reserved_planks=reserved_planks, smelt_count=smelt_n,
            )

        collected: list[dict[str, Any]] = []
        session.progress.update(furnace_pos=furnace_pos, input=input_item, requested_count=smelt_n, collected=collected)
        if fuel_item and chosen_fuel != fuel_item:
            session.progress["fuel_fallback"] = {"requested": fuel_item, "selected": chosen_fuel}
        load_ops = [
            {"op": "put", "item": input_item, "count": smelt_n, "to": "input"},
            {"op": "put", "item": chosen_fuel, "count": fuel_count, "to": "fuel"},
        ]
        try:
            try:
                load_result = self.bot.call("window/act", {"target": target, "ops": load_ops})
            except ApiError as exc:
                if exc.code != "no_window":
                    raise
                # An explicit open failure occurs before any inventory operations.
                alternate = min(
                    (row for row in furnaces if row.get("pos") and row["pos"] != furnace_pos),
                    key=lambda row: float(row.get("dist", 99)),
                    default=None,
                )
                if alternate is None or float(alternate.get("dist", 99)) > 4.0:
                    raise
                furnace_pos = alternate["pos"]
                pending_batch = self._pending_sync_smelt_batch(furnace_pos, dim, input_item)
                if pending_batch:
                    return pending_batch
                target = {"pos": furnace_pos}
                session.progress["furnace_pos"] = furnace_pos
                load_result = self.bot.call("window/act", {"target": target, "ops": load_ops})
        except ApiError as exc:
            # Fuel placement can fail after the input transfer already committed.
            self._record_sync_furnace_load(furnace_pos, dim, input_item, exc.context, 0)
            if exc.context.get("done") and furnace_pos is not None:
                exc.context.setdefault("furnace_pos", furnace_pos)
            raise
        self._record_sync_furnace_load(furnace_pos, dim, input_item, load_result, smelt_n)

        session.progress["fuel"] = {"item": chosen_fuel, "count": fuel_count}
        # Reserve time to reopen the furnace and take confirmed output.
        finish_at = session.deadline
        poll_until = finish_at - min(6.0, max(0.0, (finish_at - time.monotonic()) / 2))
        session.deadline = poll_until
        try:
            while time.monotonic() < poll_until:
                check_and_stabilize(self.bot, raise_on_low_health=False)
                view = self.bot.call("window/view", {"target": target})
                self._observe_smelt_furnace(session.progress, view, phase="poll")
                sections = view.get("sections") or {}
                inputs_left = sum(int(x.get("count") or 0) for x in (sections.get("input") or []))
                out_total = sum(int(x.get("count") or 0) for x in (sections.get("output") or []))
                if out_total >= smelt_n or (inputs_left == 0 and not (view.get("info") or {}).get("burning")):
                    break
                time.sleep(min(2.5, max(0.0, poll_until - time.monotonic())))
        except _SkillDeadline:
            if session.pending_action:
                raise
            session.timed_out = False
        finally:
            session.deadline = finish_at

        view = self.bot.call("window/view", {"target": target})
        self._observe_smelt_furnace(session.progress, view, phase="before_take")
        outputs_to_take = [
            o for o in ((view.get("sections") or {}).get("output") or []) if int(o.get("count") or 0) > 0
        ]
        if outputs_to_take:
            inv_take, take_dropped = self._ensure_free_slots(min_free=3)
            if take_dropped:
                pre_dropped.extend(take_dropped)
            for out_item in outputs_to_take:
                cnt = int(out_item.get("count") or 0)
                out_name = str(out_item.get("name") or "").removeprefix("minecraft:")
                if _compute_empty_slots(inv_take) == 0 and not _can_fit_item_in_inventory(
                    inv_take, out_name, cnt, check_craft_inputs=False
                ):
                    return {
                        "ok": False,
                        "error": "inventory_full",
                        "requires_replan": True,
                        "furnace_pos": furnace_pos,
                        "pending_output": {"name": out_name, "count": cnt},
                        "message": (
                            f"熔炉产物 {out_name} x{cnt} 已就绪，但背包 36 格已满(empty_slots=0)且无法自动清出空槽；"
                            "请先消耗、放置或丢弃至少 1 格物品再取出，切勿重复开炉强行取物触发 30s 超时。"
                        ),
                    }
        for out_item in outputs_to_take:
            cnt = int(out_item.get("count") or 0)
            request = {"target": target, "ops": [{"op": "take", "from": "output", "count": cnt}]}
            taken, window_after_take = self._take_sync_furnace_output(
                self.bot, request, {_clean_block(out_item.get("name")): cnt}, inv_take, furnace_pos, dim,
            )
            if window_after_take is not None:
                self._observe_smelt_furnace(session.progress, window_after_take, phase="after_take")
            collected.extend({"name": item, "count": n} for item, n in taken.items())
            if furnace_pos is not None and dim is not None:
                self._settle_furnace_output(furnace_pos, dim, taken)

        if pre_dropped:
            session.progress["auto_dropped"] = pre_dropped
        expected = SMELT_OUTPUTS.get(_clean_block(input_item))
        if sum(row["count"] for row in collected if expected is None or row["name"] == expected) < smelt_n:
            return self._partial_smelt(session.progress, session)
        if furnace_pos is not None and dim is not None:
            cleanup = self._reclaim_empty_temporary_furnace(session, furnace_pos, dim)
            if cleanup:
                session.progress["furnace_cleanup"] = cleanup
        return {"ok": True, **session.progress}


    def enchant_item_once(
        self,
        item: str,
        option: int,
        *,
        lapis_count: int | None = None,
        take: bool = True,
        block: str | None = "enchanting_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """One enchanting-table combo in a single `/window/act` call.

        Order is fixed by the API: put item -> put lapis -> select option -> take item.
        `option` is the API option index (0-based); lapis cost is `option + 1` when omitted.

        Upstream 1.3.14 note: repeated calls can leave mineflayer's window inventory
        stale even though the server applied the enchant. Prefer the tool-level
        `mc_skill_enchant` wrapper, which verifies `/inventory` and reconnects once.
        """
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}
        try:
            opt = int(option)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_args", "message": "option 必须是整数。"}
        if opt < 0:
            return {"ok": False, "error": "bad_args", "message": "option 从 0 开始。"}
        lapis = opt + 1 if lapis_count is None else int(lapis_count)
        ops: list[dict[str, Any]] = [
            {"op": "put", "item": resolve_item_alias(item), "to": "item"},
            {"op": "put", "item": "lapis_lazuli", "count": max(1, lapis), "to": "lapis"},
            {"op": "select", "option": opt},
        ]
        if take:
            ops.append({"op": "take", "from": "item"})

        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            return self._window_api_error(err)
        return {"ok": True, "option": opt, "ops": ops, "done": result.get("done"), "window": result.get("window")}


    def _observe_window_entity(self, entity_id: int) -> dict[str, Any] | None:
        rows = self.bot.call("get_entities", {"radius": 32}, timeout_s=2.0).get("entities") or []
        return next((row for row in rows if isinstance(row, dict) and row.get("id") == entity_id), None)


    @staticmethod
    def _is_merchant_entity(entity: dict[str, Any]) -> bool:
        return _clean_block(entity.get("name") or entity.get("type")) in {"villager", "wandering_trader"}


    def _prepare_merchant_window(
        self, entity_id: int, observed: dict[str, Any] | None = None, *, approach_limit: float = 4.0,
    ) -> None:
        def position(value: Any) -> list[float]:
            if not isinstance(value, (list, tuple)) or len(value) < 3 or not all(
                isinstance(coord, (int, float)) and math.isfinite(coord) for coord in value[:3]
            ):
                raise ApiError("merchant_position_unverified", "未确认角色与村民的当前坐标，请重新观测。")
            return list(value[:3])

        def verify(entity: dict[str, Any] | None) -> tuple[float, list[float]]:
            if entity is None:
                raise ApiError("target_not_visible", "村民已不在可观察范围，请重新查找其实体 ID。")
            if not self._is_merchant_entity(entity):
                raise ApiError("not_a_merchant", "当前实体不是可交易的村民。")
            pos = position(entity.get("pos"))
            return math.dist(position(self.bot.status().get("pos")), pos), pos

        dist, pos = verify(observed if observed is not None else self._observe_window_entity(entity_id))
        if dist <= 2.5:
            return
        if dist <= approach_limit:
            blocked = self.geofence.check_point(pos[0], pos[2], action="mc_skill_trade") if self.geofence else None
            if blocked is not None:
                detail = blocked["error"]
                raise ApiError(detail["code"], detail["message"], context=detail)
            timeout_ms, timeout_s = (1800, 2.5) if dist <= 4.0 else (4200, 5.0)
            try:
                moved = self.bot.call(
                    "move_to",
                    {"x": pos[0], "y": pos[1], "z": pos[2], "range": 1.5, "timeout": timeout_ms, "stop_on": ["hurt", "death"]},
                    timeout_s=timeout_s,
                )
            except ApiError as err:
                if err.code not in {"movement_not_confirmed", "path_not_found", "no_path"}:
                    raise
            else:
                if moved.get("interrupted_by"):
                    raise ApiError("interrupted", "靠近村民时动作中断。")
            dist, pos = verify(self._observe_window_entity(entity_id))
            if dist <= 2.5:
                return
        raise ApiError(
            "out_of_reach", "请沿安全路线靠近该村民至 2.5 米内，再打开交易窗口。",
            context={"entity_id": entity_id, "dist": round(dist, 2), "pos": pos},
        )


    def _with_merchant_budget(
        self, action: Callable[[MacroSkills], dict[str, Any]], max_duration_s: float,
    ) -> dict[str, Any]:
        session = _SkillBudgetSession(self.bot, max_duration_s, single_attempt=True)
        scoped = copy(self)
        scoped.bot = session
        try:
            return action(scoped)
        except (_SkillDeadline, httpx.TimeoutException):
            action_released = session.stop_pending()
            raise ApiError(
                "trade_time_budget", "交易窗口操作已到预算上限，请先核对背包和村民交易状态。",
                context={"action_released": action_released},
            ) from None


    def merchant_window_view(
        self, entity_id: int, observed: dict[str, Any] | None, *, max_duration_s: float,
    ) -> dict[str, Any]:
        target = {"entity_id": entity_id}

        def view(scoped: MacroSkills) -> dict[str, Any]:
            scoped._prepare_merchant_window(entity_id, observed)
            try:
                result = scoped.bot.call("window/view", {"target": target})
            except ApiError as err:
                if err.code != "out_of_reach":
                    raise
                scoped._prepare_merchant_window(entity_id)
                result = scoped.bot.call("window/view", {"target": target})
            return {"ok": True, "result": result}

        return self._with_merchant_budget(view, max_duration_s)


    def merchant_trade(
        self,
        *,
        entity_id: int,
        option: int | None = None,
        get_item: str | None = None,
        times: int = 1,
        dim: str | None = None,
        max_duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Read merchant `options`, then run `select(option, times)` in one `/window/act` call."""
        if max_duration_s is not None:
            return self._with_merchant_budget(
                lambda scoped: scoped._merchant_trade(entity_id, option, get_item, times, verify_range=True),
                max_duration_s,
            )
        return self._merchant_trade(entity_id, option, get_item, times, verify_range=False)

