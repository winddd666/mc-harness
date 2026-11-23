from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("placement", ["new", "reused", "rejected", "timed_out", "unreleased", "surface"])
def test_craft_gear_clears_its_own_table_from_a_narrow_mine(monkeypatch, placement) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    y = 64 if placement == "surface" else -54
    table = [1, y, 0]
    table_present = placement == "reused"
    calls = []

    def handler(request):
        nonlocal table_present
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        timeout = request.extensions["timeout"]["read"]
        assert timeout <= 26.0 - clock[0] + 1e-9
        calls.append(route)
        if route == "inventory":
            result = {"items": [
                {"name": "cobbled_deepslate", "count": 3},
                {"name": "stick", "count": 2},
                {"name": "crafting_table", "count": int(not table_present)},
            ]}
        elif route == "status":
            result = {"pos": [0.5, y, 0.5], "dimension": "overworld", "health": 20}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": table, "dist": 1}]} if table_present else {"blocks": []}
        elif route == "place":
            assert not table_present
            table_present = True
            result = {"placed": "crafting_table", "pos": table}
        elif route == "craft":
            assert table_present
            if placement in {"timed_out", "unreleased"}:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline slow craft", request=request)
            if placement == "rejected":
                return httpx.Response(200, json={"ok": False, "error": {"code": "unknown_item", "message": "rejected"}})
            result = {"crafted": "stone_pickaxe", "gained": {"stone_pickaxe": 1}}
        elif route == "stop":
            result = {"released": placement != "unreleased"}
        elif route == "equip":
            assert table_present is (placement in {"reused", "surface"})
            result = {"equipped": "stone_pickaxe"}
        elif route == "get_block":
            assert body["pos"] == table
            result = {"name": "crafting_table" if table_present else "air"}
        elif route == "dig":
            assert body["pos"] == table and body["collect"] is True
            assert table_present
            table_present = False
            result = {"dug": "crafting_table", "gained": {"crafting_table": 1}}
        else:
            raise AssertionError(route)
        clock[0] += min(0.1, timeout)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset._episode_deadline_mono = 30.0
        result = json.loads(toolset.mc_skill_craft_gear("stone_pickaxe"))

    assert result["ok"] is (placement not in {"rejected", "timed_out", "unreleased"})
    assert table_present is (placement in {"reused", "surface", "unreleased"})
    if placement in {"reused", "surface", "unreleased"}:
        assert "dig" not in calls
    else:
        assert result["crafting_table_cleanup"] == [{"pos": table, "removed": True, "recovered": 1}]
        verify = calls.index("get_block")
        assert calls[verify:verify + 2] == ["get_block", "dig"]
    if placement == "timed_out":
        assert calls[-3:] == ["stop", "get_block", "dig"]
        assert result["stop_reason"] == "time_budget"
    elif placement == "unreleased":
        assert calls[-1] == "stop"
        assert result["crafting_table_cleanup"] == [{
            "pos": table, "removed": False, "recovered": 0, "error": "cleanup_deadline_or_pending",
        }]
    assert clock[0] <= 26.0


@pytest.mark.parametrize("bag,table,count,shortage", [
    ({"oak_log": 2}, "absent", 1, (9, 8)),
    ({"oak_planks": 6, "stick": 4}, "absent", 1, (7, 6)),
    ({"oak_log": 2, "stick": 2}, "absent", 1, None),
    ({"oak_planks": 5}, "nearby", 1, None),
    ({"oak_planks": 5, "crafting_table": 1}, "absent", 1, None),
    ({"oak_log": 3}, "absent", 1, None),
    ({"oak_log": 3, "stick": 4}, "absent", 3, (15, 12)),
    ({"oak_planks": 5}, "no_window", 1, (7, 3)),
    ({"oak_planks": 4, "oak_log": 1, "stick": 2}, "no_window", 1, None),
])
def test_wooden_pickaxe_budgets_the_table_sticks_and_pick_before_prep(bag, table, count, shortage) -> None:
    from mc_agent.harness.mc_tools import McToolset

    initial = dict(bag)
    bag = dict(bag)
    table_pos = [1, 64, 0]
    present = table != "absent"
    local_placed = False
    writes = []

    def handler(request):
        nonlocal present, local_placed
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "health": 20}
        elif route == "inventory":
            result = {"items": [{"name": name, "count": n} for name, n in bag.items() if n > 0]}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": table_pos, "dist": 1.0}] if present else []}
        elif route == "place":
            writes.append("place")
            assert body["item"] == "crafting_table" and not local_placed and bag["crafting_table"] == 1
            bag["crafting_table"] -= 1
            present = True
            local_placed = True
            result = {"placed": "crafting_table", "pos": table_pos}
        elif route == "craft":
            item, amount = body["item"], body["count"]
            writes.append(item)
            if item == "wooden_pickaxe" and table == "no_window" and not local_placed:
                return httpx.Response(200, json={"ok": False, "error": {"code": "no_window"}})
            if item == "oak_planks":
                ingredient, cost = "oak_log", amount // 4
            elif item == "stick":
                ingredient, cost = "oak_planks", amount // 4 * 2
            elif item == "crafting_table":
                ingredient, cost = "oak_planks", amount * 4
            else:
                assert item == "wooden_pickaxe" and present
                ingredient, cost = "oak_planks", amount * 3
                if bag.get("stick", 0) < amount * 2 or bag.get(ingredient, 0) < cost:
                    return httpx.Response(200, json={"ok": False, "error": {
                        "code": "missing_ingredients", "message": "server placed no result",
                    }})
                bag["stick"] -= amount * 2
            assert bag.get(ingredient, 0) >= cost
            bag[ingredient] -= cost
            bag[item] = bag.get(item, 0) + amount
            result = {"crafted": item, "gained": amount}
        elif route == "equip":
            result = {"held": {"name": body["item"]}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_craft_gear("wooden_pickaxe", count=count))

    if shortage is None:
        assert result["ok"] and bag["wooden_pickaxe"] == count
        assert writes.count("crafting_table") == int(table != "nearby" and not initial.get("crafting_table"))
        assert writes.count("stick") == int(initial.get("stick", 0) < count * 2)
    else:
        needed, available = shortage
        assert result["error"] == "missing_ingredients" and result["requires_replan"]
        assert result["required_materials"] == {"any_planks": needed}
        assert result["available_materials"] == {"any_planks": available}
        assert result["missing_materials"] == {"any_planks": needed - available}
        assert result["logs_needed"] == 1 and "1 根原木" in result["message"]
        if table == "no_window":
            assert writes == ["stick", "wooden_pickaxe"]
        else:
            assert writes == [] and bag == initial


@pytest.mark.parametrize("mode,wood,base_log,planks", [
    ("ensure", {"stripped_spruce_log": 2}, "spruce_log", "spruce_planks"),
    ("ensure", {"stripped_warped_stem": 2}, "warped_stem", "warped_planks"),
    ("ensure", {"crimson_stem": 2}, "crimson_stem", "crimson_planks"),
    ("gear", {"stripped_spruce_log": 2}, "spruce_log", "spruce_planks"),
    ("explicit", {"stripped_oak_log": 2}, "oak_log", "oak_planks"),
    ("mixed", {"spruce_log": 1, "stripped_spruce_log": 1}, "spruce_log", "spruce_planks"),
])
def test_stripped_wood_prepares_planks_sticks_and_stone_pickaxe(mode, wood, base_log, planks) -> None:
    bag = {**wood, "cobblestone": 7}
    crafts = []
    table = [1, 64, 0]

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": name, "count": n} for name, n in bag.items() if n > 0]}
        elif route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "dimension": "overworld", "health": 20, "food": 20}
        elif route == "find_blocks":
            assert body["types"] == ["crafting_table"]
            result = {"blocks": [{"pos": table, "dist": 1.0}]}
        elif route == "craft":
            item, count = body["item"], body["count"]
            crafts.append((item, count))
            if item == planks:
                batches = count // 4
                assert count == batches * 4
                for log in (base_log, f"stripped_{base_log}"):
                    used = min(bag.get(log, 0), batches)
                    if used:
                        bag[log] -= used
                        batches -= used
                assert batches == 0
            elif item == "stick":
                assert count == 4 and bag[planks] >= 2
                bag[planks] -= 2
            elif item == "stone_pickaxe":
                assert count == 1 and bag["cobblestone"] >= 3 and bag["stick"] >= 2
                bag["cobblestone"] -= 3
                bag["stick"] -= 2
            else:
                return httpx.Response(200, json={"ok": False, "error": {
                    "code": "bad_args", "message": f"unknown item {item}",
                }})
            bag[item] = bag.get(item, 0) + count
            result = {"crafted": item, "gained": count}
        elif route == "equip":
            result = {"held": {"name": body["item"]}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        if mode == "ensure":
            result = skills.ensure_items({"stone_pickaxe": 1})
        else:
            if mode == "explicit":
                assert skills.craft_gear(planks, 4)["ok"] is True
            if mode == "mixed":
                assert skills._ensure_planks_and_sticks(6, 2)[planks] == 6
            result = skills.craft_gear("stone_pickaxe")

    expected_planks = 8 if mode == "mixed" else 4
    assert result["ok"] is True
    assert crafts == [(planks, expected_planks), ("stick", 4), ("stone_pickaxe", 1)]
    assert bag[planks] == expected_planks - 2 and bag["stick"] == 2
    assert bag["cobblestone"] == 4 and bag["stone_pickaxe"] == 1
    assert sum(bag[log] for log in wood) == sum(wood.values()) - expected_planks // 4


@pytest.mark.parametrize("wood,planks,fits", [
    ({"spruce_log": 1}, "spruce_planks", True),
    ({"stripped_spruce_log": 1}, "spruce_planks", True),
    ({"stripped_warped_stem": 1}, "warped_planks", True),
    ({"stripped_spruce_log": 2}, "spruce_planks", False),
    ({"spruce_log": 1, "stripped_spruce_log": 2}, "spruce_planks", False),
])
def test_plank_craft_at_full_inventory_requires_a_consumable_input_slot(wood, planks, fits) -> None:
    bag = dict(wood)
    picks = [{"name": "stone_pickaxe", "count": 1}] * (36 - len(wood))
    crafts = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "inventory":
            result = {"items": picks + [{"name": name, "count": n} for name, n in bag.items()]}
            return httpx.Response(200, json={"ok": True, "result": result})
        assert route == "craft" and fits
        body = json.loads(request.content)
        crafts.append(body)
        assert bag.pop(next(iter(wood))) == 1
        bag[planks] = 4
        return httpx.Response(200, json={"ok": True, "result": {"crafted": planks, "gained": 4}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).craft_gear(planks, 4)

    assert result["ok"] is fits
    if fits:
        assert crafts == [{"item": planks, "count": 4}] and bag == {planks: 4}
    else:
        assert result["error"] == "inventory_full" and not crafts and bag == wood


def test_unstick_sends_stop_and_reports_deadline_hit(monkeypatch) -> None:
    """r22/r23/r24: unstick could run 150~270s; budget must cap it."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    calls: list[str] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "control" and body.get("yaw") == 0.0:
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "in_water": False}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": "stone", "diggable": True}
        elif path == "dig":
            clock[0] += min(20.0, float(request.extensions["timeout"]["read"]))
            result = {}
        elif path == "control":
            result = {"pos": [0.5, 64.0, 0.5], "moved": 0.0}
        elif path == "stop":
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills.descend_canopy = lambda **kwargs: {"on_canopy": False}
        result = skills.unstick(max_duration_s=45.0)
    assert result["deadline_hit"] is True
    assert result["elapsed_s"] <= 45.5
    assert result["budget_s"] == pytest.approx(45.0)
    assert "不要连续重复调用 unstick" in result["hint"]
    assert "stop" in calls


def test_unstick_shares_deadline_with_water_escape(monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    calls = []
    pos = [-7824.7, 110.2, -2771.3]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "status":
            result = {"pos": pos, "in_water": True, "jumping": True}
        elif path == "get_block":
            timeout = request.extensions["timeout"]["read"]
            clock[0] += min(2.5, timeout)
            if timeout < 2.5:
                raise httpx.ReadTimeout("water probe exceeded the remaining budget", request=request)
            result = {"name": "water", "diggable": False}
        elif path == "control":
            result = {"pos": pos}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "stop":
            clock[0] += 0.25
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_skill_unstick())
        assert toolset.skills.bot is bot

    assert result["ok"] is False
    assert result["in_water"] is True
    assert result["escaped_water"] is False
    assert result["pos_after"] == pos
    assert result["deadline_hit"] is True
    assert result["requires_replan"] is True
    assert clock[0] <= 45.0
    assert calls[-1] == "stop"


def test_unstick_reports_failure_when_still_in_water() -> None:
    from mc_agent.harness.mc_tools import McToolset

    pos = [-10945.7, 62.3, -11027.2]
    replies = {
        "status": {"pos": pos, "in_water": True, "jumping": True},
        "get_block": {"name": "water", "diggable": False},
        "control": {"pos": pos},
        "find_blocks": {"blocks": []},
    }

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"ok": True, "result": replies[route]})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_unstick())

    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "water_escape_failed"
    assert result["in_water"] is True
    assert result["escaped_water"] is False
    assert result["pos_after"] == pos
    assert "deadline_hit" not in result


@pytest.mark.parametrize("terrain", [
    "layers", "deep_drop", "unknown", "lava", "no_landing", "timeout", "early_read_timeout", "early_dig_timeout",
])
def test_unstick_descends_canopy_only_with_confirmed_support(monkeypatch, terrain) -> None:
    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    standing = [0.5, 71.0, 0.5]
    blocks = {70: "spruce_leaves", 63: "dirt"}
    if terrain in {"layers", "no_landing", "timeout", "early_dig_timeout"}:
        blocks.update({y: "spruce_leaves" for y in range(65, 70)})
        blocks[64] = "spruce_log"
    if terrain in {"unknown", "lava"}:
        blocks[68] = "dirt"
    if terrain == "lava":
        blocks[69] = "lava"
    digs = []
    stops = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": standing, "on_ground": True, "alive": True, "ready": True, "dimension": "overworld"}
        elif route == "get_block":
            if terrain == "early_read_timeout":
                raise httpx.ReadTimeout("canopy support read failed", request=request)
            x, y, z = body["pos"]
            name = blocks.get(y, "air") if (x, z) == (0, 0) else "air"
            result = {"name": name, "diggable": name.endswith(("_leaves", "_log"))}
            if terrain == "unknown" and (x, y, z) == (0, 69, 0):
                result = {}
        elif route == "dig":
            x, y, z = body["pos"]
            digs.append((x, y, z))
            assert y == int(standing[1]) - 1
            if terrain == "early_dig_timeout":
                raise httpx.ReadTimeout("canopy dig reply lost", request=request)
            if terrain == "timeout":
                clock[0] += sum(request.extensions["timeout"].values())
                raise httpx.ReadTimeout("slow canopy dig", request=request)
            removed = blocks.pop(y)
            if terrain != "no_landing":
                standing[1] = max(blocks) + 1.0
            result = {"dug": removed}
        elif route == "stop":
            stops.append(body)
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        if terrain in {"early_read_timeout", "early_dig_timeout"}:
            with pytest.raises(httpx.ReadTimeout):
                skills.unstick(max_duration_s=10)
            assert len(digs) == (1 if terrain == "early_dig_timeout" else 0)
            assert stops == []
            return
        result = skills.unstick(max_duration_s=10)

    if terrain == "layers":
        assert digs == [(0, y, 0) for y in range(70, 63, -1)]
        assert result["ok"] is True
        assert result["descended_canopy"] is True
        assert result["pos_after"] == [0.5, 64.0, 0.5]
    else:
        assert result["ok"] is False
        assert result["requires_replan"] is True
        assert len(digs) == (1 if terrain in {"no_landing", "timeout"} else 0)
        if terrain == "no_landing":
            assert result["error"]["code"] == "descent_not_confirmed"
    assert len(stops) == (1 if terrain == "timeout" else 0)
    if terrain == "timeout":
        assert result["deadline_hit"] is True
        assert result["elapsed_s"] <= 10


@pytest.mark.parametrize("north_has_support", [True, False])
def test_unstick_uses_another_direction_when_east_has_no_landing(north_has_support) -> None:
    from mc_agent.harness.mc_tools import McToolset

    moves = []
    origin = [0.5, 64.0, 0.5]

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": origin, "on_ground": True, "ready": True, "alive": True}
        elif route == "get_block":
            x, y, z = body["pos"]
            ground = y == 63 and x == 0 and (z == 0 or (north_has_support and z < 0))
            result = {"name": "stone" if ground else "air", "diggable": ground}
        elif route == "control":
            moves.append(body)
            result = {"pos": [0.5, 64.0, -0.5], "moved": 1.0}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_unstick())

    assert result["ok"] is north_has_support
    if north_has_support:
        assert len(moves) == 1
        assert moves[0]["yaw"] == pytest.approx(0.0)
        assert moves[0]["jump"] is False
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert result["requires_replan"] is True
        assert moves == []


def test_unstick_fast_path_has_no_deadline_flag() -> None:
    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "in_water": False}
        elif path == "get_block":
            y = json.loads(request.content)["pos"][1]
            result = {"name": "stone" if y == 63 else "air", "diggable": False}
        elif path == "control":
            result = {"pos": [1.5, 64.0, 0.5], "moved": 1.0}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).unstick()
    assert result["ok"] is True
    assert "deadline_hit" not in result
    assert result["elapsed_s"] >= 0.0


@pytest.mark.parametrize("item,counts,missing", [
    ("stone_pickaxe", {"cobblestone": 1, "oak_log": 2}, {"cobblestone": 2, "stick": 2}),
    ("stone_shovel", {"stick": 2}, {"cobblestone": 1}),
    ("shield", {"oak_planks": 6}, {"iron_ingot": 1}),
    ("stone_bricks", {"stone": 1}, {"stone": 3}),
])
def test_craft_gear_missing_materials_fail_before_any_mutation(item, counts, missing) -> None:
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        assert path == "inventory"
        return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": name, "count": count} for name, count in counts.items()]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).craft_gear(item)
    assert result["error"] == "missing_ingredients"
    assert result["requires_replan"] is True
    assert result["missing_materials"] == missing
    # r24/D1: 7 of 8 real failures had message=None, forcing the model to
    # parse the maps itself.
    assert result["message"]
    assert item in result["message"]
    assert "缺少材料" in result["message"]
    assert calls == ["inventory"]


def test_recipe_preflight_handles_stone_alternatives_and_batch_yield() -> None:
    from mc_agent.skills.library import craft_material_shortage

    stone = craft_material_shortage("stone_pickaxe", 1, {"cobblestone": 1, "cobbled_deepslate": 1, "blackstone": 1, "stick": 2})
    assert stone["missing_materials"] == {}
    assert stone["available_materials"]["cobblestone"] == 3
    torch = craft_material_shortage("torch", 5, {"charcoal": 2, "stick": 2})
    assert torch["missing_materials"] == {}
    assert torch["required_materials"] == {"coal_or_charcoal": 2, "stick": 2}
    bricks = craft_material_shortage("stone_bricks", 5, {"stone": 7})
    assert bricks["missing_materials"] == {"stone": 1}
    assert craft_material_shortage("unknown_new_recipe", 1, {}) is None


@pytest.mark.parametrize("lose_material,code", [(False, "server_recipe_rejected"), (True, "missing_ingredients")])
@pytest.mark.parametrize("retry", [False, True])
def test_craft_no_result_distinguishes_fresh_shortage_after_table_recovery(lose_material, code, retry) -> None:
    crafted = []
    tables = []
    counts = {"cobblestone": 4, "stick": 2, "oak_planks": 3}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            result = {"items": [{"name": name, "count": count} for name, count in counts.items()], "empty_slots": 30}
            return httpx.Response(200, json={"ok": True, "result": result})
        assert path == "craft"
        crafted.append(body)
        if retry and len(crafted) == 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "no_window"}})
        if lose_material:
            counts["cobblestone"] = 1
        return httpx.Response(200, json={"ok": False, "error": {"code": "missing_ingredients", "message": "could not craft stone_pickaxe: server placed no result"}})

    def prepare_table(*, force_recarve=False, **kwargs):
        tables.append(force_recarve)
        return {"ok": True, "reused": not force_recarve}

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        skills.ensure_crafting_table = prepare_table
        result = skills.craft_gear("stone_pickaxe")
    assert result["error"] == code
    assert result["requires_replan"] is True
    assert tables == ([False, True] if retry else [False])
    assert crafted == [{"item": "stone_pickaxe", "count": 1}] * (2 if retry else 1)
    assert result["missing_materials"] == ({"cobblestone": 2} if lose_material else {})


def test_atomic_craft_error_reports_actual_materials_instead_of_example_variant() -> None:
    from mc_agent.errors import ApiError
    from mc_agent.harness.mc_tools import McToolset

    err = ApiError("missing_ingredients", "server placed no result", context={"example_recipe_needs": {"cobbled_deepslate": 3, "stick": 2}, "inventory": {"cobblestone": 1, "stick": 2}})
    result = json.loads(McToolset._format_api_error("craft", {"item": "stone_pickaxe", "count": 1}, err))
    assert result["error"]["missing_materials"] == {"cobblestone": 2}
    assert "example_recipe_needs" not in result["error"]
    assert result["error"]["server_recipe_example"]["cobbled_deepslate"] == 3

