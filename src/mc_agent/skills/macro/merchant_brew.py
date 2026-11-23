"""MacroSkills mixin: merchant_brew (split from legacy library.py)."""

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


class _MerchantBrewMixin:
    def _merchant_trade(
        self, entity_id: int, option: int | None, get_item: str | None, times: int, *, verify_range: bool,
    ) -> dict[str, Any]:
        target = self._window_target(entity_id=entity_id)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 entity_id。"}
        option_idx = None
        needle = None
        if option is not None:
            try:
                option_idx = int(option)
            except (TypeError, ValueError):
                return {"ok": False, "error": "bad_args", "message": "option 必须是整数。"}
        elif get_item:
            needle = resolve_item_alias(get_item)
        else:
            return {"ok": False, "error": "bad_args", "message": "需要 option 或 get_item 之一。"}

        count = max(1, int(times))
        previous_offer = None
        retried = False
        while True:
            if verify_range:
                try:
                    self._prepare_merchant_window(entity_id, approach_limit=32.0)
                except ApiError as err:
                    return self._window_api_error(err)
            try:
                view = self.bot.call("window/view", {"target": target})
            except ApiError as err:
                if err.code != "out_of_reach" or not verify_range or retried:
                    return self._window_api_error(err)
                retried = True
                continue

            options = [o for o in (view.get("options") or []) if isinstance(o, dict)]
            if option_idx is not None:
                chosen = next((o for o in options if int(o.get("index", -1)) == option_idx), None)
                if chosen is None and 0 <= option_idx < len(options):
                    chosen = options[option_idx]
            else:
                matches = [
                    o for o in options if isinstance(o.get("get"), dict)
                    and resolve_item_alias(str(o["get"].get("name") or "")) == needle
                ]
                available = [o for o in matches if o.get("available") is not False]
                chosen = (available or matches or [None])[0]
            if chosen is None:
                return {"ok": False, "error": "no_matching_trade", "message": "没有匹配的交易选项。", "options": options}
            offer = (chosen.get("give"), chosen.get("get"))
            if previous_offer is not None and offer != previous_offer:
                return {"ok": False, "error": "trade_changed", "message": "交易内容已变，请核对新选项。", "options": options}
            idx = int(chosen.get("index", 0))
            if verify_range:
                try:
                    self._prepare_merchant_window(entity_id, approach_limit=0.0)
                except ApiError as err:
                    if err.code != "out_of_reach" or retried:
                        return self._window_api_error(err)
                    previous_offer = offer
                    retried = True
                    continue
            try:
                result = self.bot.call("window/act", {
                    "target": target, "ops": [{"op": "select", "option": idx, "times": count}],
                })
            except ApiError as err:
                failed_to_open = err.code == "out_of_reach" and not err.context.get("done") and "failed_op" not in err.context
                if not failed_to_open or not verify_range or retried:
                    return self._window_api_error(err)
                previous_offer = offer
                retried = True
                continue
            return {"ok": True, "option": idx, "trade": chosen, "done": result.get("done"), "window": result.get("window")}


    def brew_once(
        self,
        ingredient_item: str,
        *,
        bottles: Any = None,
        fuel_item: str | None = None,
        wait: bool = True,
        wait_timeout_s: float = 30.0,
        poll_interval_s: float = 2.0,
        block: str | None = "brewing_stand",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Load a brewing stand (`bottles`/`ingredient`/`fuel`) and optionally poll `brew_time_left`."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}

        bottle_entries = _window_item_entries(bottles) if bottles else []
        ops: list[dict[str, Any]] = []
        for i, entry in enumerate(bottle_entries):
            op: dict[str, Any] = {
                "op": "put",
                "item": entry["item"],
                "count": entry["count"],
                "to": "bottles",
            }
            if "potion" in entry:
                op["potion"] = entry["potion"]
            if "slot" in entry:
                op["slot"] = entry["slot"]
            elif entry["count"] == 1 and len(bottle_entries) > 1:
                op["slot"] = i
            ops.append(op)
        ops.append(
            {"op": "put", "item": resolve_item_alias(ingredient_item), "count": 1, "to": "ingredient"}
        )
        if fuel_item:
            ops.append({"op": "put", "item": resolve_item_alias(fuel_item), "count": 1, "to": "fuel"})

        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            if err.code == "no_window" and x is not None and y is not None and z is not None and block:
                try:
                    got = self.bot.call("get_block", {"pos": [x, y, z]})
                    actual = str((got or {}).get("name") or "").removeprefix("minecraft:")
                except ApiError:
                    actual = ""
                if actual and actual != str(block).removeprefix("minecraft:"):
                    return {
                        "ok": False,
                        "error": "window_target_mismatch",
                        "target": [x, y, z],
                        "expected": block,
                        "actual": actual,
                        "message": (
                            f"目标 {[x, y, z]} 实际是 {actual}，不是 {block}；"
                            "请用 mc_find_blocks(types=['brewing_stand']) 重新定位，"
                            "或省略 x/y/z 让工具自动搜索附近酿造台。不要重复同一坐标。"
                        ),
                    }
            out = self._window_api_error(err)
            if err.code == "missing_item":
                try:
                    out.setdefault("inventory", _inventory_counts(self.bot))
                except ApiError:
                    pass
                out["message"] = (
                    f"{out.get('message') or err.message}；已附 inventory 计数，"
                    "请核对 ingredient/fuel 是否仍在背包，勿原参数重复。"
                )
            return out

        view: dict[str, Any] | None = None
        if wait:
            deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
            delay = 1.0
            while time.monotonic() < deadline:
                time.sleep(delay)
                delay = max(0.5, float(poll_interval_s))
                try:
                    view = self.bot.call("window/view", {"target": target})
                except ApiError as err:
                    return self._window_api_error(err)
                info = view.get("info") or {}
                try:
                    left = float(info.get("brew_time_left") or 0.0)
                except (TypeError, ValueError):
                    left = 0.0
                if left <= 0:
                    break

        out: dict[str, Any] = {
            "ok": True,
            "ops": ops,
            "done": result.get("done"),
            "window": (view or {}).get("window", result.get("window")),
            "sections": (view or {}).get("sections"),
            "info": (view or {}).get("info"),
        }
        return out


    def anvil_operate(
        self,
        left_item: str | dict[str, Any],
        *,
        right_item: str | dict[str, Any] | None = None,
        left_enchanted: bool | None = None,
        right_enchanted: bool | None = None,
        text: str | None = None,
        take: bool = True,
        block: str | None = "anvil",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Single `/window/act` anvil combo: put left/right, optional rename text, take output."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}

        def _build_anvil_put(spec: str | dict[str, Any], slot_name: str, ench_flag: bool | None) -> dict[str, Any]:
            if isinstance(spec, dict):
                op_d: dict[str, Any] = {
                    "op": "put",
                    "item": resolve_item_alias(str(spec.get("item") or spec.get("name") or "")),
                    "to": slot_name,
                }
                for fk in ("enchanted", "custom_name", "slot"):
                    if fk in spec and spec[fk] is not None:
                        op_d[fk] = spec[fk]
                if ench_flag is not None and "enchanted" not in op_d:
                    op_d["enchanted"] = bool(ench_flag)
                return op_d
            op_s: dict[str, Any] = {"op": "put", "item": resolve_item_alias(str(spec)), "to": slot_name}
            if ench_flag is not None:
                op_s["enchanted"] = bool(ench_flag)
            return op_s

        ops: list[dict[str, Any]] = [_build_anvil_put(left_item, "left", left_enchanted)]
        if right_item is not None:
            ops.append(_build_anvil_put(right_item, "right", right_enchanted))
        if text is not None:
            ops.append({"op": "text", "value": str(text)})
        if take:
            ops.append({"op": "take", "from": "output"})
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            err_payload = self._window_api_error(err)
            if err.code == "no_output":
                win = err_payload.get("window") if isinstance(err_payload.get("window"), dict) else {}
                secs = win.get("sections") if isinstance(win.get("sections"), dict) else {}
                left_rows = secs.get("left") or []
                right_rows = secs.get("right") or []
                l0 = left_rows[0] if left_rows and isinstance(left_rows[0], dict) else {}
                r0 = right_rows[0] if right_rows and isinstance(right_rows[0], dict) else {}
                rcost = int((win.get("info") or {}).get("repair_cost") or 0) if isinstance(win.get("info"), dict) else 0
                if l0 and r0 and not l0.get("durability_used") and not r0.get("enchantments"):
                    err_payload["hint"] = (
                        "铁砧无产物(no_output)：左槽物品为满耐久且右槽无附魔可叠加；"
                        "若要修耐久请将受损装备放 left，若要合附魔请用 left_enchanted=True / right_enchanted=True 指定附魔件。"
                    )
                elif l0.get("enchantments") and r0.get("enchantments"):
                    err_payload["hint"] = (
                        "铁砧无产物(no_output)：左右槽附魔可能互斥（如锋利/亡灵杀手/节肢克星互斥）或已达等级上限，或当前经验等级不足。"
                    )
                elif rcost > 0:
                    err_payload["hint"] = f"铁砧无产物或无法取出：所需经验等级 repair_cost={rcost}，请确认当前 XP 等级足够。"
            return err_payload
        return {"ok": True, "ops": ops, "done": result.get("done"), "window": result.get("window")}


    def smithing_apply(
        self,
        template_item: str,
        base_item: str,
        addition_item: str,
        *,
        take: bool = True,
        block: str | None = "smithing_table",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Single `/window/act` smithing combo: template + base + addition -> take output."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}
        ops: list[dict[str, Any]] = [
            {"op": "put", "item": resolve_item_alias(template_item), "to": "template"},
            {"op": "put", "item": resolve_item_alias(base_item), "to": "base"},
            {"op": "put", "item": resolve_item_alias(addition_item), "to": "addition"},
        ]
        if take:
            ops.append({"op": "take", "from": "output"})
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            return self._window_api_error(err)
        return {"ok": True, "ops": ops, "done": result.get("done"), "window": result.get("window")}


    def stonecutter_cut(
        self,
        input_item: str,
        option: int,
        *,
        count: int | None = None,
        take: bool = True,
        block: str | None = "stonecutter",
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict[str, Any]:
        """Single `/window/act` stonecutter combo: put input -> select option -> take output."""
        target = self._window_target(block=block, x=x, y=y, z=z)
        if target is None:
            return {"ok": False, "error": "bad_args", "message": "需要 block 或 (x, y, z)。"}
        try:
            opt = int(option)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_args", "message": "option 必须是整数。"}
        put_op: dict[str, Any] = {"op": "put", "item": resolve_item_alias(input_item), "to": "input"}
        if count is not None:
            put_op["count"] = max(1, int(count))
        ops: list[dict[str, Any]] = [put_op, {"op": "select", "option": opt}]
        if take:
            ops.append({"op": "take", "from": "output"})
        try:
            result = self.bot.call("window/act", {"target": target, "ops": ops})
        except ApiError as err:
            return self._window_api_error(err)
        return {"ok": True, "option": opt, "ops": ops, "done": result.get("done"), "window": result.get("window")}


    def bind_bed_spawn(self, x: int, y: int, z: int) -> dict[str, Any]:
        """Move near a bed and interact reliably day or night to set respawn point."""
        self.bot.call("move_to", {"x": x, "y": y, "z": z, "range": 2.5})
        st = self.bot.status()
        payload: dict[str, Any] = {"pos": [x, y, z]}
        if st.get("is_day"):
            counts = _inventory_counts(self.bot)
            for hand_item in ("stick", "wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "stone_sword", "iron_sword", "bone"):
                if counts.get(hand_item, 0) > 0:
                    payload["item"] = hand_item
                    break
        res = self.bot.call("interact", payload)
        if self.world_memory is not None:
            try:
                self.world_memory.record_bed_spawn([x, y, z], dim=normalize_dimension(st.get("dimension")))
            except Exception:
                pass
        return {"ok": True, "bed_pos": [x, y, z], **res}

