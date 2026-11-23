"""MacroSkills mixin: crafting (split from legacy library.py)."""

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


class _CraftingMixin:
    def craft_gear(
        self,
        item: str,
        count: int = 1,
        equip_slot: str | None = None,
        *,
        max_duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Auto-craft intermediate planks/sticks, ensure crafting_table when needed, craft item, and optionally equip."""
        scoped = self
        owns_budget = max_duration_s is not None
        if max_duration_s is not None:
            scoped = copy(self)
            scoped.bot = _SkillBudgetSession(self.bot, max_duration_s)
        session = scoped.bot if isinstance(scoped.bot, _SkillBudgetSession) else None
        deadline = session.deadline if session else None
        temporary_tables: list[list[int]] = []
        try:
            result = scoped._craft_gear(item, count, temporary_tables)
        except (_SkillDeadline, httpx.TimeoutException):
            if not owns_budget:
                raise
            result = {
                "ok": False,
                "error": "time_budget",
                "stop_reason": "time_budget",
                "requires_replan": True,
                "message": "合成预算已用尽。请核对背包中的产物和中间材料，再决定是否重试。",
            }
        finally:
            if session is not None:
                session.deadline = deadline

        requested_item = resolve_item_alias(item)
        inventory = result.get("inventory")
        if (
            result.get("error") == "missing_ingredients"
            and requested_item in _REUSABLE_CRAFT_ITEMS
            and isinstance(inventory, dict)
        ):
            owned = int(inventory.get(requested_item, 0))
            if owned >= max(1, min(64, count)):
                result = {
                    "ok": True, "already_owned": True, "item": requested_item, "owned_count": owned,
                    "gained": 0, "inventory": inventory, "message": f"已持有 {requested_item}×{owned}，本次合成增加 0 件。",
                }
                if not equip_slot:
                    try:
                        snapshot = scoped.bot.inventory()
                    except (ApiError, _SkillDeadline, httpx.HTTPError):
                        snapshot = {}
                    rows = [*(snapshot.get("items") or []), *(snapshot.get("armor") or {}).values(), snapshot.get("offhand")]
                    owned_items = [row for row in rows if isinstance(row, dict) and _clean_block(row.get("name")) == requested_item]
                    if owned_items:
                        result["owned_items"] = owned_items

        craft_timed_out = session is not None and session.timed_out
        if session is not None and owns_budget and craft_timed_out:
            pending = session.pending_action
            released = session.stop_pending()
            if temporary_tables and (not pending or released):
                session.timed_out = False
        interruptions = ("death", "hurt", "interrupted", "not_connected", "action_quarantined")
        cleanup = []
        if temporary_tables:
            blocked = result.get("error") if result.get("error") in interruptions else None
            if session is not None and session.timed_out:
                blocked = blocked or "cleanup_deadline_or_pending"
            if blocked:
                cleanup = [{"pos": pos, "removed": False, "recovered": 0, "error": blocked} for pos in temporary_tables]
            else:
                cleanup = scoped._recover_crafting_tables(temporary_tables)
            result["crafting_table_cleanup"] = cleanup

        if result.get("ok"):
            equip_item = resolve_item_alias(str(result.get("crafted") or item))
            already_owned = result.get("already_owned") is True
            slot = equip_slot
            if not slot and (not already_owned or equip_item == "golden_boots"):
                slot = DEFAULT_EQUIP_SLOTS.get(equip_item)
            result["equipped_slot"] = None
            blocked = next((row["error"] for row in cleanup if row.get("error") in interruptions), None)
            if slot:
                if blocked:
                    result["equip_error"] = {"code": blocked, "message": "装备请求因动作中断而未执行。"}
                else:
                    result.update(scoped._equip_crafted_gear(equip_item, slot, already_owned=already_owned))
                if already_owned and result.get("equip_error"):
                    result.update(ok=False, requires_replan=True)
        if session is not None and owns_budget:
            if session.timed_out:
                session.stop_pending()
            if (craft_timed_out or session.timed_out) and result.get("stop_reason") != "time_budget":
                result["deadline_hit"] = True
        return result


    def _equip_crafted_gear(self, item: str, slot: str, *, already_owned: bool) -> dict[str, Any]:
        slot = {"chest": "torso", "offhand": "off-hand"}.get(slot, slot)

        def equipped_item() -> dict[str, Any]:
            inv = self.bot.inventory()
            if slot in {"hand", "off-hand"}:
                row = inv.get("held" if slot == "hand" else "offhand")
            else:
                armor = inv.get("armor") or {}
                row = (armor.get("chest") or armor.get("torso")) if slot == "torso" else armor.get(slot)
            return row if isinstance(row, dict) else {"name": row} if isinstance(row, str) else {}

        try:
            observed = equipped_item() if already_owned else {}
            if _clean_block(observed.get("name")) != item:
                self.bot.call("equip", {"item": item, "slot": slot})
                observed = equipped_item()
        except (ApiError, _SkillDeadline, httpx.HTTPError) as err:
            code = "time_budget" if isinstance(err, (_SkillDeadline, httpx.TimeoutException)) else getattr(err, "code", type(err).__name__)
            return {"equip_error": {"code": code, "message": f"无法确认 {item} 已装备到 {slot}。"}}
        if _clean_block(observed.get("name")) != item:
            return {"equip_error": {"code": "equip_unconfirmed", "message": f"复查 {slot} 时未见 {item}。"}}
        durability = observed.get("durability")
        if isinstance(durability, (list, tuple)) and len(durability) >= 2:
            remaining, maximum = durability[:2]
        else:
            remaining, maximum = observed.get("durability_remaining"), observed.get("max_durability")
        worn = isinstance(remaining, (int, float)) and isinstance(maximum, (int, float)) and remaining < maximum
        used = (observed.get("durability_used"), observed.get("damage"))
        if not already_owned and (worn or any(isinstance(value, (int, float)) and value > 0 for value in used)):
            return {
                "equipped_item": observed,
                "equip_error": {"code": "equip_unconfirmed", "message": f"{slot} 仍是已磨损的 {item}，请核对新制品。"},
            }
        return {"equipped_slot": slot, "equipped_item": observed}


    def _recover_crafting_tables(self, positions: list[list[int]]) -> list[dict[str, Any]]:
        cleanup: list[dict[str, Any]] = []
        for pos in positions:
            row: dict[str, Any] = {"pos": pos, "removed": False, "recovered": 0}
            cleanup.append(row)
            try:
                block = self.bot.call("get_block", {"pos": pos})
                name = _clean_block(block.get("name") or block.get("block"))
                if name != "crafting_table":
                    row["removed"] = name in {"air", "cave_air", "void_air"}
                    continue
                dug = self.bot.call("dig", {"pos": pos, "collect": True, "stop_on": ["death"]})
                row["removed"] = _clean_block(dug.get("dug")) == "crafting_table"
                row["recovered"] = int((dug.get("gained") or {}).get("crafting_table", 0))
            except ApiError as err:
                row["error"] = err.code
                if err.code in ("death", "hurt", "interrupted", "not_connected", "action_quarantined"):
                    break
            except (_SkillDeadline, httpx.HTTPError):
                row["error"] = "cleanup_unconfirmed"
                break
        return cleanup


    def _classify_craft_no_result(
        self, item: str, count: int, prepared: dict[str, int], message: str,
    ) -> dict[str, Any]:
        try:
            counts = _inventory_counts(self.bot)
        except ApiError:
            counts = prepared
        plan = craft_material_shortage(item, count, counts)
        return {
            "ok": False, "requires_replan": True,
            "error": "missing_ingredients" if plan and plan["missing_materials"] else "server_recipe_rejected",
            "message": message, "inventory": counts,
            **(plan or {}),
        }


    def _recover_nether_craft(
        self, item: str, count: int, table: dict[str, Any], error: ApiError,
        temporary_tables: list[list[int]], reserved_planks: int,
    ) -> dict[str, Any]:
        if any(error.context.get(key) for key in ("done", "gained", "crafted")):
            raise error
        if self._step_to_nether_workbench(table.get("pos") or error.context.get("table")):
            try:
                return self._safe_craft(item, count)
            except ApiError as err:
                if err.code not in _WORKBENCH_OPEN_ERRORS or any(err.context.get(key) for key in ("done", "gained", "crafted")):
                    raise
                error = err
        if not table.get("reused"):
            raise error
        replacement = self.ensure_crafting_table(
            force_recarve=True, target_item=item, temporary_tables=temporary_tables, reserved_planks=reserved_planks,
        )
        return self._safe_craft(item, count) if replacement.get("ok") else replacement


    def _craft_gear(self, item: str, count: int, temporary_tables: list[list[int]]) -> dict[str, Any]:
        item = _canonical_plank_recipe(resolve_item_alias(item))
        count = max(1, min(64, count))
        inv_raw, initial = _inventory_snapshot_and_counts(self.bot)
        if item == "golden_boots" and count == 1 and initial.get(item, 0) > 0:
            return {"ok": True, "already_owned": True, "item": item, "owned_count": initial[item], "gained": 0}
        if item in {"planks", "any_planks"} or (
            item == "oak_planks" and not any(initial.get(log, 0) > 0 for log in _PLANK_LOG_SOURCES[item])
        ):
            for k_log, v_log in initial.items():
                if v_log > 0 and k_log.endswith(("_log", "_stem")):
                    item = _log_to_planks_name(k_log)
                    break
        plan = craft_material_shortage(item, count, initial)
        if plan and plan["missing_materials"]:
            # Only non-consumable / true singleton utility items should short-circuit as already_owned;
            # durability tools (*_pickaxe, *_sword) and buckets must report missing_ingredients when
            # crafting a replacement (e.g., after mine_dfs returns pickaxe_low).
            _ALREADY_OWNED_GEAR = frozenset({
                "flint_and_steel", "shield", "shears", "compass",
                "lodestone", "enchanting_table", "anvil", "brewing_stand", "crafting_table",
            })
            if item in _ALREADY_OWNED_GEAR and int(initial.get(item, 0)) >= count:
                return {
                    "ok": True,
                    "already_owned": True,
                    "crafted": item,
                    "gained": 0,
                    "owned_count": int(initial.get(item, 0)),
                    "inventory": initial,
                    "message": f"背包中已有 {item}×{int(initial.get(item, 0))}，无需重复合成；请直接继续下一步。",
                }
            # Logs/planks and sticks can be prepared automatically, but
            # absent stone/ingots/wool cannot be repaired by rebuilding a table.
            non_wood_missing = {k: v for k, v in plan["missing_materials"].items() if k not in {"stick", "any_planks"}}
            if non_wood_missing:
                missing_desc = "、".join(
                    f"{name} 需 {int(plan['required_materials'].get(name, amt))}"
                    f"（现有 {int(plan['available_materials'].get(name, 0))}，缺 {amt}）"
                    for name, amt in plan["missing_materials"].items()
                )
                return self._attach_shortage_memory_hint(
                    {
                        "ok": False,
                        "error": "missing_ingredients",
                        "requires_replan": True,
                        "inventory": initial,
                        **plan,
                        "message": (
                            f"合成 {item} x{count} 缺少材料：{missing_desc}。"
                            "请先用 mc_skill_harvest_logs 补木料 / mc_skill_mine_blocks 补石料后再合成，"
                            "或改合成本轮材料足够的替代工具；不要原参数重复调用。"
                        ),
                    },
                    non_wood_missing.keys(),
                )
        planks_per, sticks_per = TOOL_WOOD_NEEDS.get(item, (0, 0))
        planks_needed = math.ceil(count / 4) * 2 if item == "stick" else planks_per * count
        sticks_needed = math.ceil(count / 4) if item == "torch" else sticks_per * count
        table_res = None
        if item == "wooden_pickaxe":
            reserved_planks = planks_needed + 2 * _stick_craft_batches(sticks_needed, initial.get("stick", 0))
            wood_shortage = _wood_budget_shortage(item, initial, reserved_planks)
            if wood_shortage is not None:
                return wood_shortage
            table_res = self.ensure_crafting_table(
                target_item=item, temporary_tables=temporary_tables, reserved_planks=reserved_planks,
            )
            if not table_res.get("ok"):
                return table_res
            inv_raw = None  # Preparing a table can change the inventory.

        station_materials = _reserved_craft_materials(item, count)
        inv_raw, pre_dropped = self._ensure_free_slots(
            min_free=5, inv_snapshot=inv_raw, station_materials=station_materials,
        )
        # Only pre-craft sticks/planks up front if we might need them; let server check exact 3x3 items.
        try:
            prepared = self._ensure_planks_and_sticks(planks_needed, sticks_needed)
        except ApiError as err:
            return {
                "ok": False,
                "error": err.code,
                "requires_replan": True,
                "message": err.message,
                **err.context,
            }
        if item == "stick" and _total_planks(prepared) < planks_needed:
            return {
                "ok": False,
                "error": "missing_ingredients",
                "requires_replan": True,
                "inventory": prepared,
                "missing_materials": {"planks_or_log": planks_needed - _total_planks(prepared)},
                "message": (
                    f"合成 {count} 个 stick 需要至少 {planks_needed} 块木板（可用原木自动合成），"
                    f"当前可用木板仅 {_total_planks(prepared)}。"
                ),
            }
        plan = craft_material_shortage(item, count, prepared)
        if plan and plan["missing_materials"]:
            missing = plan["missing_materials"]
            shortage_desc = "、".join(
                f"{name} 需 {int(plan['required_materials'].get(name, amt))}（现有 {int(plan['available_materials'].get(name, 0))}，缺 {amt}）"
                for name, amt in missing.items()
            )
            return self._attach_shortage_memory_hint(
                {
                    "ok": False,
                    "error": "missing_ingredients",
                    "requires_replan": True,
                    "inventory": prepared,
                    **plan,
                    "message": (
                        f"合成 {item} x{count} 缺少材料：{shortage_desc}。"
                        "请先用 mc_skill_harvest_logs 补木料 / mc_skill_mine_blocks 补石料后再合成，"
                        "或改合成本轮材料足够的替代工具；不要原参数重复调用。"
                    ),
                },
                plan["missing_materials"].keys(),
            )

        if item in TABLE_RECIPES and table_res is None:
            table_res = self.ensure_crafting_table(target_item=item, temporary_tables=temporary_tables)
            if not table_res.get("ok"):
                return table_res
            # Re-ensure planks if crafting table consumed some
            if planks_per > 0:
                try:
                    self._ensure_planks_and_sticks(planks_per * count, sticks_needed)
                except ApiError as err:
                    return {
                        "ok": False,
                        "error": err.code,
                        "requires_replan": True,
                        "message": err.message,
                        **err.context,
                    }

        try:
            craft_res = self._safe_craft(item, count)
        except ApiError as err:
            if err.code == "missing_ingredients" and "server placed no result" in err.message:
                return self._classify_craft_no_result(item, count, prepared, err.message)
            if err.code == "inventory_full":
                return {
                    "ok": False,
                    "error": err.code,
                    "requires_replan": True,
                    "message": err.message,
                    **err.context,
                }
            if item in TABLE_RECIPES and (
                "windowOpen" in err.message
                or "server placed no result" in err.message
                or err.code in _WORKBENCH_OPEN_ERRORS
            ):
                try:
                    if "server placed no result" in err.message:
                        self._ensure_free_slots(min_free=6, station_materials=station_materials)
                    reserve = planks_needed if item == "wooden_pickaxe" else 0
                    if table_res and table_res.get("dimension") == "the_nether" and err.code in _WORKBENCH_OPEN_ERRORS:
                        craft_res = self._recover_nether_craft(item, count, table_res, err, temporary_tables, reserve)
                        if craft_res.get("ok") is False:
                            return craft_res
                    else:
                        if table_res and table_res.get("reused"):
                            new_table = self.ensure_crafting_table(
                                force_recarve=True, target_item=item, temporary_tables=temporary_tables, reserved_planks=reserve,
                            )
                            if not new_table.get("ok"):
                                return new_table
                        craft_res = self._safe_craft(item, count)
                except ApiError as retry_err:
                    if retry_err.code == "missing_ingredients" and "server placed no result" in retry_err.message:
                        return self._classify_craft_no_result(item, count, prepared, retry_err.message)
                    nether_workbench = table_res and table_res.get("dimension") == "the_nether"
                    return {
                        "ok": False,
                        "error": retry_err.code,
                        "requires_replan": retry_err.code == "inventory_full" or bool(nether_workbench),
                        "message": retry_err.message,
                        **retry_err.context,
                    }
            else:
                return {
                    "ok": False,
                    "error": err.code,
                    "message": err.message,
                    **{k: v for k, v in explain_craft_error(item, count, {"code": err.code, **err.context}).items() if k != "code"},
                }

        out_craft: dict[str, Any] = {"ok": True, **craft_res}
        if pre_dropped:
            merged_drops = list(pre_dropped)
            if isinstance(out_craft.get("auto_dropped"), list):
                merged_drops.extend(out_craft["auto_dropped"])
            out_craft["auto_dropped"] = merged_drops
        return out_craft


    def mine_blocks(
        self,
        block_types: list[str],
        count: int = 3,
        radius: int = 32,
        *,
        max_duration_s: float = 45.0,
        skip_unmineable: bool = False,
        cleanup_inventory: bool = False,
    ) -> dict[str, Any]:
        """Validate pickaxe tier and gather blocks within a shared skill budget."""
        return self._gather_with_budget(
            max_duration_s,
            lambda skills: skills._mine_blocks(
                block_types, count, radius, skip_unmineable=skip_unmineable, defer_cleanup=cleanup_inventory,
            ),
            cleanup_inventory=cleanup_inventory,
        )

