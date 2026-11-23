from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

def test_collect_drops_smelt_charcoal_reservation_and_toolset_registration() -> None:
    from mc_agent.harness.mc_tools import McToolset

    window_ops: list[dict] = []
    crafted: list[dict] = []
    inv_items: list[dict] = [
        {"name": "oak_log", "count": 4, "slot": 36},
        {"name": "white_bed", "count": 1, "slot": 37},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inv_items
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if path == "/status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 64.0, 0.0], "health": 20, "food": 20, "is_day": True}})
        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": list(inv_items)}})
        if path == "/find_blocks":
            return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "furnace", "pos": [1, 64, 0], "dist": 1.2}]}})
        if path == "/craft":
            crafted.append(body)
            if body.get("item") == "oak_planks":
                inv_items = [
                    {"name": "oak_log", "count": 3, "slot": 36},
                    {"name": "oak_planks", "count": 4, "slot": 38},
                ]
            return httpx.Response(200, json={"ok": True, "result": {"crafted": body["item"], "gained": body.get("count", 1)}})
        if path == "/window/act":
            window_ops.append(body)
            done = [{"took": {"charcoal": 3}}] if body["ops"][0]["op"] == "take" else True
            return httpx.Response(200, json={"ok": True, "result": {"done": done}})
        if path == "/window/view":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"sections": {"input": [], "output": [{"name": "charcoal", "count": 3}]}, "info": {"burning": False}}},
            )
        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": [{"id": 501, "type": "item", "item": "cobblestonex2", "pos": [2.0, 64.0, 0.0]}]}})
        if path == "/move_to":
            if body.get("entity_id") == 501:
                inv_items = [*inv_items, {"name": "cobblestone", "count": 2, "slot": 39}]
                return httpx.Response(200, json={"ok": True, "result": {"pos": [2.0, 64.0, 0.0]}})
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.0, 64.0, 0.0]}})
        raise AssertionError(f"Unexpected path: {path}")

    bot = BotSession("tok", bot_username="5Ease_Bot", transport=httpx.MockTransport(handler))
    skills = MacroSkills(bot)
    smelt_res = skills.smelt_items("oak_log", count=3)
    assert smelt_res["ok"] is True
    assert smelt_res["fuel"]["item"] == "oak_planks"
    assert smelt_res["fuel"]["count"] == 2
    assert crafted and crafted[0]["item"] == "oak_planks"
    assert window_ops[0]["ops"][0] == {"op": "put", "item": "oak_log", "count": 3, "to": "input"}
    assert window_ops[0]["ops"][1] == {"op": "put", "item": "oak_planks", "count": 2, "to": "fuel"}

    drops_res = skills.collect_drops()
    assert drops_res["ok"] is True
    assert drops_res["gained"].get("cobblestone") == 2

    names = {s["function"]["name"] for s in McToolset(bot).registry().schemas()}
    assert "mc_skill_collect_drops" in names
    assert "mc_skill_bind_bed" in names
    assert "mc_skill_unstick" in names


@pytest.mark.parametrize("options,observed,source,expected_source,item", [
    ({"block": "furnace"}, None, None, "output", "iron_ingot"),
    ({"block": "minecraft:furnace"}, None, None, "output", "iron_ingot"),
    ({"block": "furnace"}, None, "input", "input", "raw_iron"),
    ({"block": "furnace"}, None, "fuel", "fuel", "coal"),
    ({"block": "chest"}, None, None, None, "iron_ingot"),
    ({"block": "furnace", "x": 1, "y": 64, "z": 0}, "furnace", None, "output", "iron_ingot"),
    ({"x": 1, "y": 64, "z": 0}, "minecraft:furnace", None, "output", "iron_ingot"),
    ({"block": "furnace", "x": 1, "y": 64, "z": 0}, "chest", None, None, "iron_ingot"),
    ({"x": 1, "y": 64, "z": 0}, "unknown", None, None, "iron_ingot"),
    ({"x": 1, "y": 64, "z": 0}, None, "input", "input", "raw_iron"),
    ({"x": 1, "y": 64, "z": 0}, None, "fuel", "fuel", "coal"),
])
def test_window_act_defaults_confirmed_furnace_take_to_output(options, observed, source, expected_source, item) -> None:
    from mc_agent.harness.mc_tools import McToolset

    sections = {"input": {"raw_iron": 4}, "fuel": {"coal": 4}, "output": {"iron_ingot": 6}}
    bag = {}
    probes = []
    take = {"op": "take", "item": item, "count": 4, "to": "player"}
    if source is not None:
        take["from"] = source
    request_op = dict(take)

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/get_block"):
            assert observed is not None and body == {"pos": [1, 64, 0]}
            probes.append(body["pos"])
            if observed == "unknown":
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_loaded", "message": "unknown"}})
            return httpx.Response(200, json={"ok": True, "result": {"name": observed}})
        assert request.url.path.endswith("/window/act")
        target = {"pos": [1, 64, 0]} if "x" in options else {"block": options["block"]}
        assert body["target"] == target
        expected_op = {**request_op, "from": expected_source} if expected_source else request_op
        assert body["ops"] == [expected_op]
        if expected_source is None:
            return httpx.Response(200, json={"ok": False, "error": {"code": "bad_section", "done": []}})
        op = body["ops"][0]
        sections[op["from"]][item] -= op["count"]
        bag[item] = op["count"]
        return httpx.Response(200, json={"ok": True, "result": {"done": [{"took": dict(bag)}]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_window_act(ops=[take], **options))

    assert take == request_op
    assert probes == ([[1, 64, 0]] if observed is not None else [])
    if expected_source is None:
        assert result["error"]["code"] == "bad_section" and not bag
    else:
        assert result["result"]["done"] == [{"took": {item: 4}}]
        assert bag == {item: 4}
        assert sections["output"]["iron_ingot"] == (2 if expected_source == "output" else 6)


@pytest.mark.parametrize("entry,input_item,output_item", [
    ("tool", "coal", None), ("tool", "minecraft:coal", None), ("start", "coal", None),
    ("tool", "raw_iron", "iron_ingot"), ("tool", "coal_ore", "coal"),
])
def test_smelt_coal_is_fuel_and_valid_recipes_still_transfer(entry, input_item, output_item) -> None:
    from mc_agent.harness.mc_tools import McToolset

    furnace = [1, 64, 0]
    before = {"coal": 16, "raw_iron": 2, "coal_ore": 2}
    bag = dict(before)
    ready = 0
    calls = []
    operations = []

    def handler(request):
        nonlocal ready
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(route)
        if route == "inventory":
            result = {"items": [{"name": name, "count": count} for name, count in bag.items() if count > 0]}
        elif route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "dimension": "overworld"}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": furnace, "dist": 1.0}]}
        elif route == "view":
            assert body["target"]["pos"] == furnace
            result = {"sections": {"input": [], "output": [{"name": output_item, "count": ready}] if ready else []}}
        elif route == "act":
            assert body["target"]["pos"] == furnace
            operations.extend(body["ops"])
            if body["ops"][0]["op"] == "put":
                material, fuel = body["ops"]
                assert material["to"] == "input" and fuel["to"] == "fuel" and fuel["item"] == "coal"
                bag["coal"] -= fuel["count"]
                if output_item:
                    bag[material["item"]] -= material["count"]
                    ready = material["count"]
                result = {}
            else:
                assert body["ops"] == [{"op": "take", "from": "output", "count": ready}]
                bag[output_item] = bag.get(output_item, 0) + ready
                result = {"done": [{"took": {output_item: ready}}]}
                ready = 0
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        if entry == "start":
            result = tools.skills.smelt_start(input_item, count=10, fuel_item="coal")
        else:
            result = json.loads(tools.mc_skill_smelt(input_item, count=2 if output_item else 10, fuel_item="coal"))

    if output_item is None:
        assert result["ok"] is False and result["error"] == "not_smeltable"
        assert "coal" in result["message"] and "fuel_item" in result["message"]
        assert bag == before and not calls
    else:
        assert result["ok"] is True and result["collected"] == [{"name": output_item, "count": 2}]
        assert operations == [
            {"op": "put", "item": input_item, "count": 2, "to": "input"},
            {"op": "put", "item": "coal", "count": 1, "to": "fuel"},
            {"op": "take", "from": "output", "count": 2},
        ]
        assert bag[input_item] == 0 and ready == 0
        assert bag["coal"] == (17 if output_item == "coal" else 15)
        if output_item != "coal":
            assert bag[output_item] == 2


@pytest.mark.parametrize("initial_cobble", [32, 8])
def test_smelt_recounts_cobblestone_after_crafting_its_furnace(monkeypatch, initial_cobble) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    furnace = [1, 64, 0]
    bag = {"cobblestone": initial_cobble, "coal": 5}
    loaded = 0
    output = 0
    operations = []

    def handler(request):
        nonlocal loaded, output
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            result = {"items": [{"name": name, "count": n} for name, n in bag.items() if n > 0]}
        elif path == "status":
            result = {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "dimension": "overworld"}
        elif path == "find_blocks":
            table = {"pos": [0, 64, 1], "dist": 1.0}
            result = {"blocks": [table] if body["types"] == ["crafting_table"] else []}
        elif path == "craft":
            assert body == {"item": "furnace", "count": 1}
            bag["cobblestone"] -= 8
            bag["furnace"] = 1
            result = {"crafted": "furnace", "gained": 1}
        elif path == "place":
            assert body == {"item": "furnace"} and bag.pop("furnace") == 1
            result = {"pos": furnace}
        elif path == "act":
            assert body["target"]["pos"] == furnace
            operations.extend(body["ops"])
            if body["ops"][0]["op"] == "put":
                material, fuel = body["ops"]
                assert 0 < material["count"] <= bag["cobblestone"]
                bag["cobblestone"] -= material["count"]
                bag["coal"] -= fuel["count"]
                output = 4
                loaded = material["count"] - output
                result = {}
            else:
                bag["stone"] = output
                result = {"done": [{"took": {"stone": output}}]}
                output = 0
        elif path == "view":
            result = {"sections": {
                "input": [{"name": "cobblestone", "count": loaded}],
                "output": [{"name": "stone", "count": output}],
            }, "info": {"burning": True}}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_skill_smelt("cobblestone", count=initial_cobble, fuel_item="coal"))
        pending = toolset.world_memory.get_in_conversion_counts()

    if initial_cobble == 8:
        assert result["error"] == "missing_ingredients"
        assert not operations and not pending and bag["coal"] == 5
    else:
        assert result["error"] == "partial_smelt" and result["requested_count"] == 24
        assert operations == [
            {"op": "put", "item": "cobblestone", "count": 24, "to": "input"},
            {"op": "put", "item": "coal", "count": 3, "to": "fuel"},
            {"op": "take", "from": "output", "count": 4},
        ]
        assert pending == {"stone": 20} and loaded == 20 and bag == {"cobblestone": 0, "coal": 2, "stone": 4}


@pytest.mark.parametrize("requested_fuel,pending", [(None, False), ("coal", False), ("coal", True)])
def test_smelt_checks_fuel_and_pending_batch_before_preparing_furnace(requested_fuel, pending) -> None:
    """P2-28/r20_long: bare 'no fuel' left ds_1_bare stuck in bootstrap_iron_age."""

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status" and pending:
            return httpx.Response(200, json={"ok": True, "result": {"pos": [0.5, 58.0, 0.5], "dimension": "overworld"}})
        assert path == "inventory"
        return httpx.Response(200, json={"ok": True, "result": {"items": [
            {"name": "raw_iron", "count": 3, "slot": 36}, {"name": "cobblestone", "count": 8, "slot": 37},
        ]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        if pending:
            skills._get_world_memory().register_conversion_job(
                pos=[1, 58, 0], input_item="raw_iron", output_item="iron_ingot", count=2,
                dim="overworld", kind="sync_furnace",
            )
        result = skills.smelt_items("raw_iron", count=3, fuel_item=requested_fuel)
    assert result["ok"] is False
    if pending:
        assert result["error"] == "pending_smelt_batch"
        assert result["furnace_pos"] == [1, 58, 0] and result["pending_count"] == 2
        return
    assert result["error"] == "missing_fuel"
    assert result["requires_replan"] is True
    assert result["input"] == "raw_iron" and result["furnace_pos"] is None
    assert result["collected"] == []
    assert result["next_action"] == "mc_skill_harvest_logs(target_logs=2)"
    assert result["inventory"] == {"raw_iron": 3, "cobblestone": 8}
    assert "mc_skill_harvest_logs" in result["message"]
    assert "取出的成品为 0" in result["message"]


@pytest.mark.parametrize("requested,stock,protected,expected", [
    ("coal", {"oak_planks": 2}, (), ("oak_planks", 2)),
    ("cobblestone", {"cobblestone": 64, "oak_planks": 5}, (), ("oak_planks", 2)),
    ("minecraft:charcoal", {"coal": 1, "charcoal": 1}, (), ("charcoal", 1)),
    ("wooden_pickaxe", {"wooden_pickaxe": 1, "shield": 1, "blaze_rod": 1}, (), None),
    ("oak_planks", {"oak_planks": 5, "oak_log": 1}, ("oak_planks",), None),
])
def test_sync_smelt_validates_fuel_before_load(requested, stock, protected, expected) -> None:
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.skills.inventory_manager import InventoryManager

    bag = {"raw_iron": 3, **stock}
    furnace = [1, 64, 0]
    calls = []
    ready = 0

    def handler(request):
        nonlocal ready
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(route)
        if route == "inventory":
            result = {"items": [{"name": name, "count": count} for name, count in bag.items() if count]}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": furnace, "dist": 1.0}]}
        elif route == "status":
            result = {"pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "dimension": "overworld"}
        elif route == "view":
            result = {"sections": {"input": [], "output": [{"name": "iron_ingot", "count": ready}]}}
        elif route == "act":
            assert body["target"]["pos"] == furnace
            if body["ops"][0]["op"] == "put":
                fuel, fuel_count = expected
                assert body["ops"] == [
                    {"op": "put", "item": "raw_iron", "count": 3, "to": "input"},
                    {"op": "put", "item": fuel, "count": fuel_count, "to": "fuel"},
                ]
                bag["raw_iron"] -= 3
                bag[fuel] -= fuel_count
                ready = 3
                result = {}
            else:
                assert body["ops"] == [{"op": "take", "from": "output", "count": 3}]
                bag["iron_ingot"] = ready
                result = {"done": [{"took": {"iron_ingot": ready}}]}
                ready = 0
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        tools.skills.inventory_manager = inventory = InventoryManager(bot)
        with inventory.protect(protected):
            outcome = json.loads(tools.mc_skill_smelt("raw_iron", count=3, fuel_item=requested))

    if expected is None:
        assert outcome["error"] == "missing_fuel" and outcome["collected"] == []
        assert calls == ["inventory"] and bag == {"raw_iron": 3, **stock}
    else:
        fuel, fuel_count = expected
        assert outcome["ok"] is True
        assert outcome["collected"] == [{"name": "iron_ingot", "count": 3}]
        assert bag == {**stock, "raw_iron": 0, "iron_ingot": 3, fuel: stock[fuel] - fuel_count}
        requested = requested.removeprefix("minecraft:")
        if fuel == requested:
            assert "fuel_fallback" not in outcome
        else:
            assert outcome["fuel_fallback"] == {"requested": requested, "selected": fuel}


@pytest.mark.parametrize("stock,requested,expected,sticks_used_before_load", [
    ({"oak_log": 2, "oak_planks": 1, "crafting_table": 2}, "oak_log", ("oak_planks", 2), False),
    ({"oak_log": 2, "oak_planks": 1, "crafting_table": 2, "stick": 2}, "oak_log", ("oak_log", 2), False),
    ({"oak_log": 2, "oak_planks": 1, "crafting_table": 2, "stick": 2}, "oak_log", ("oak_planks", 2), True),
    ({"oak_planks": 6, "stick": 2}, "oak_planks", ("oak_planks", 2), False),
    ({"oak_planks": 4, "stick": 2, "coal": 1}, "oak_planks", ("coal", 1), False),
    ({"oak_planks": 4, "stick": 2}, "oak_planks", None, False),
    ({"crafting_table": 1, "stick": 2}, "stick", None, False),
    ({"oak_log": 2, "oak_planks": 1, "crafting_table": 2, "iron_pickaxe": 1}, "oak_log", ("oak_log", 2), False),
])
def test_sync_iron_smelt_retains_first_pick_materials(stock, requested, expected, sticks_used_before_load) -> None:
    from mc_agent.harness.mc_tools import McToolset

    bag = {"raw_iron": 3, **stock}
    furnace = [1, 56, 0]
    table = [0, 56, 1]
    table_present = False
    eligible_output = 0
    writes = []

    def handler(request):
        nonlocal eligible_output, table_present
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": item, "count": n} for item, n in bag.items() if n]}
        elif route == "status":
            result = {"pos": [0.5, 56.0, 0.5], "dimension": "overworld", "health": 20, "food": 20}
        elif route == "find_blocks":
            types = body["types"]
            if types == ["furnace"] and sticks_used_before_load:
                bag["stick"] = 0
            found = furnace if types == ["furnace"] else table if table_present else None
            result = {"blocks": [{"pos": found, "dist": 1}] if found else []}
        elif route == "view":
            assert body["target"]["pos"] == furnace
            result = {"sections": {"input": [], "output": [{"name": "iron_ingot", "count": eligible_output}]}}
        elif route == "act":
            assert body["target"]["pos"] == furnace
            ops = body["ops"]
            writes.append(ops)
            if ops[0]["op"] == "put":
                fuel, amount = expected
                assert ops == [
                    {"op": "put", "item": "raw_iron", "count": 3, "to": "input"},
                    {"op": "put", "item": fuel, "count": amount, "to": "fuel"},
                ]
                assert bag[fuel] >= amount
                bag["raw_iron"] -= 3
                bag[fuel] -= amount
                eligible_output = 3
                result = {}
            else:
                assert ops == [{"op": "take", "from": "output", "count": eligible_output}]
                bag["iron_ingot"] = eligible_output
                result = {"done": [{"took": {"iron_ingot": eligible_output}}]}
                eligible_output = 0
        elif route == "craft":
            item, count = body["item"], body["count"]
            writes.append(item)
            if item == "oak_planks":
                assert count == 4 and bag["oak_log"] > 0
                bag["oak_log"] -= 1
            elif item == "stick":
                assert count == 4 and bag["oak_planks"] >= 2
                bag["oak_planks"] -= 2
            elif item == "crafting_table":
                assert count == 1 and bag["oak_planks"] >= 4
                bag["oak_planks"] -= 4
            else:
                assert item == "iron_pickaxe" and count == 1 and table_present
                assert bag["iron_ingot"] >= 3 and bag["stick"] >= 2
                bag["iron_ingot"] -= 3
                bag["stick"] -= 2
            bag[item] = bag.get(item, 0) + count
            result = {"crafted": item, "gained": count}
        elif route == "place":
            assert body["item"] == "crafting_table" and bag["crafting_table"] > 0
            writes.append(route)
            bag["crafting_table"] -= 1
            table_present = True
            result = {"pos": table}
        elif route == "equip":
            result = {}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        smelt = json.loads(tools.mc_skill_smelt("raw_iron", count=3, fuel_item=requested))
        if expected is None:
            assert smelt["error"] == "missing_fuel" and smelt["collected"] == []
            assert smelt["reserved_for_iron_pickaxe"] == {
                "stick": 2, "plank_equivalent": 0 if "crafting_table" in stock else 4,
            }
            assert writes == [] and bag == {"raw_iron": 3, **stock}
            return
        assert smelt["ok"] and bag["iron_ingot"] == 3
        if "iron_pickaxe" not in stock:
            pick = json.loads(tools.mc_skill_craft_gear("iron_pickaxe"))
            assert pick["ok"] and bag["iron_pickaxe"] == 1


@pytest.mark.parametrize("stock,count,requested,expected", [
    ({"birch_log": 1, "birch_planks": 1}, 1, None, ("reserved_materials", 0)),
    ({"birch_log": 1, "birch_planks": 1, "coal": 1}, 1, "coal", ("reserved_materials", 0)),
    ({"birch_log": 2, "birch_planks": 1}, 2, None, ("reserved_materials", 1)),
    ({"birch_log": 2}, 1, "stick", ("missing_fuel", 0)),
    ({"birch_log": 2, "birch_planks": 1}, 1, "stick", ("birch_planks", 1)),
    ({"birch_log": 3, "birch_planks": 1}, 1, "birch_log", ("birch_log", 1)),
    ({"birch_log": 3}, 1, None, ("birch_planks", 1)),
    ({"birch_log": 1, "crafting_table": 1, "coal": 1}, 1, None, ("coal", 1)),
])
def test_charcoal_checks_both_input_and_fuel_against_first_iron_materials(stock, count, requested, expected) -> None:
    from mc_agent.harness.mc_tools import McToolset

    inventory = {"stone_pickaxe": 1, "stick": 2, "cobblestone": 12, **stock}
    bag = dict(inventory)
    furnace = [233459, 55, 246410]
    routes = []
    loaded = ready = 0

    def handler(request):
        nonlocal loaded, ready
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        routes.append(route)
        if route == "inventory":
            result = {"items": [{"name": item, "count": n} for item, n in bag.items() if n]}
        elif route == "status":
            result = {"pos": [233458.5, 55.0, 246410.5], "dimension": "overworld", "health": 20, "food": 20}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": furnace, "dist": 1}]}
        elif route == "craft":
            assert body == {"item": "birch_planks", "count": 4}
            assert expected == ("birch_planks", 1) and bag.get("birch_planks", 0) == 0
            bag["birch_log"] -= 1
            bag["birch_planks"] = 4
            result = {"crafted": "birch_planks", "gained": 4}
        elif route == "view":
            assert body["target"]["pos"] == furnace
            sections = {"input": [], "output": [{"name": "charcoal", "count": ready}]}
            result = {"sections": sections, "info": {"burning": False}}
        elif route == "act":
            assert body["target"]["pos"] == furnace
            ops = body["ops"]
            if ops[0]["op"] == "put":
                fuel, fuel_count = expected
                assert ops == [
                    {"op": "put", "item": "birch_log", "count": count, "to": "input"},
                    {"op": "put", "item": fuel, "count": fuel_count, "to": "fuel"},
                ]
                assert loaded == 0
                bag["birch_log"] -= count
                assert bag[fuel] >= fuel_count
                bag[fuel] -= fuel_count
                loaded = ready = count
                result = {}
            else:
                assert ops == [{"op": "take", "from": "output", "count": loaded}]
                bag["charcoal"] = ready
                result = {"done": [{"took": {"charcoal": ready}}]}
                ready = 0
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_smelt("birch_log", count=count, fuel_item=requested))

    status, amount = expected
    if status in {"missing_fuel", "reserved_materials"}:
        assert result["error"] == status and result["collected"] == []
        if status == "reserved_materials":
            assert result["max_charcoal_input_after_reserve"] == amount
        assert routes == ["inventory"] and bag == inventory
    else:
        assert result["ok"] and result["fuel"] == {"item": status, "count": amount}
        assert bag["charcoal"] == count and bag["stick"] == 2
        assert bag.get("crafting_table", 0) or 4 * bag.get("birch_log", 0) + bag.get("birch_planks", 0) >= 4


@pytest.mark.parametrize("episode_budget,cap", [(None, 55.0), (14.0, 10.0)])
def test_skill_smelt_collects_partial_output_within_tool_budget(monkeypatch, episode_budget, cap) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    operations = []

    def handler(request):
        route = request.url.path
        body = json.loads(request.content) if request.content else {}
        timeout = request.extensions["timeout"]["read"]
        if route.endswith("/inventory"):
            result = {"items": [{"name": "raw_copper", "count": 7}, {"name": "coal", "count": 1}]}
        elif route.endswith("/find_blocks"):
            assert timeout <= cap
            clock[0] += 2.0
            result = {"blocks": [{"name": "furnace", "pos": [1, 64, 0], "dist": 1.0}]}
        elif route.endswith("/status"):
            result = {"pos": [0.5, 64, 0.5], "health": 20, "food": 20}
        elif route.endswith("/window/view"):
            result = {
                "sections": {"input": [{"name": "raw_copper", "count": 5}], "output": [{"name": "copper_ingot", "count": 2}]},
                "info": {"burning": True},
            }
        elif route.endswith("/window/act"):
            operations.extend(body["ops"])
            result = {"done": [{"took": {"copper_ingot": 2}}]} if body["ops"][0]["op"] == "take" else {}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset._episode_deadline_mono = episode_budget
        outcome = json.loads(toolset.mc_skill_smelt("raw_copper", count=7))

    assert clock[0] <= cap
    assert outcome["ok"] is False
    assert outcome["error"] == "partial_smelt"
    assert outcome["requires_replan"] is True
    assert outcome["collected"] == [{"name": "copper_ingot", "count": 2}]
    assert operations[-1] == {"op": "take", "from": "output", "count": 2}


@pytest.mark.parametrize("state,ready,taken,after,stock,refill", [
    ("processing", 4, 4, (2, 1, 0, True), {}, None),
    ("output_ready", 4, 4, (0, 0, 2, False), {}, None),
    ("needs_fuel", 1, 1, (2, 0, 0, False), {"stick": 6, "crafting_table": 1}, ("stick", 4)),
    ("needs_fuel", 1, 1, (2, 0, 0, False), {"stick": 3, "crafting_table": 1}, None),
    ("needs_fuel", 1, 1, (2, 0, 0, False), {"stick": 5}, None),
    ("needs_fuel", 1, 1, (2, 0, 0, False), {"coal": 1, "stick": 2}, ("coal", 1)),
    ("needs_fuel", 1, 1, (2, 0, 0, False), None, None),
    ("check_furnace", 2, 2, None, {}, None),
    ("output_ready", 4, 0, (2, 1, 4, True), {}, None),
])
def test_partial_iron_smelt_reports_last_furnace_state_and_confirmed_pick_material(
    monkeypatch, state, ready, taken, after, stock, refill,
) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    furnace = [219410, 16, 152022]
    fuel = "oak_planks" if state == "needs_fuel" else "coal"
    load_count = 3 if state == "needs_fuel" else 6
    bag = {"raw_iron": load_count, fuel: 1}
    writes = []

    def window(input_count, fuel_count, output_count, burning):
        return {
            "window": "furnace",
            "sections": {
                slot: [{"name": item, "count": count}] if count else []
                for slot, item, count in (
                    ("input", "raw_iron", input_count), ("fuel", fuel, fuel_count), ("output", "iron_ingot", output_count),
                )
            },
            "info": {"burning": burning} if burning is not None else {},
        }

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            if len(writes) == 2 and stock is None:
                raise httpx.ReadTimeout("offline refill inventory unavailable", request=request)
            result = {"items": [{"name": item, "count": n} for item, n in bag.items() if n]}
        elif route == "status":
            result = {"pos": [219409.5, 16.0, 152022.5], "health": 20, "food": 20}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": furnace, "dist": 1}]}
        elif route == "view":
            assert body["target"] == {"pos": furnace}
            before_burning = None if after is None else state != "needs_fuel"
            result = window(load_count - ready, int(before_burning is True), ready, before_burning)
        elif route == "act":
            assert body["target"] == {"pos": furnace}
            writes.append(body["ops"])
            if body["ops"][0]["op"] == "put":
                assert body["ops"] == [
                    {"op": "put", "item": "raw_iron", "count": load_count, "to": "input"},
                    {"op": "put", "item": fuel, "count": 1, "to": "fuel"},
                ]
                bag["raw_iron"] = bag[fuel] = 0
                result = {}
            else:
                assert body["ops"] == [{"op": "take", "from": "output", "count": ready}]
                bag["iron_ingot"] = taken
                bag.update(stock or {})
                result = {"done": [{"took": {"iron_ingot": taken}}] if taken else []}
                if after is not None:
                    result["window"] = window(*after)
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        tools._episode_deadline_mono = 14.0
        result = json.loads(tools.mc_skill_smelt("raw_iron", count=load_count, fuel_item=fuel))
        assert tools.world_memory.get_in_conversion_counts() == {"iron_ingot": load_count - taken}

    assert result["error"] == "partial_smelt" and result["furnace_state"] == state
    assert result["elapsed_s"] == round(clock[0], 2) <= 10
    assert len(writes) == 2 and bag["iron_ingot"] == taken and bag["raw_iron"] == 0
    assert result["collected"] == ([{"name": "iron_ingot", "count": taken}] if taken else [])
    assert result.get("enough_for_iron_pickaxe", False) is (taken >= 3)
    if taken >= 3:
        assert "首把铁镐" in result["message"]
    if after is not None:
        assert result["last_furnace_observation"] == {
            **dict(zip(("input", "fuel", "output", "burning"), after)), "phase": "after_take",
            **({"input_item": "raw_iron"} if after[0] else {}),
        }
    else:
        assert result["last_furnace_observation"]["phase"] == "before_take"
    target = {"block": "furnace", "x": furnace[0], "y": furnace[1], "z": furnace[2]}
    expected_actions = [{"name": "mc_window_view", "arguments": target}]
    if refill:
        item, count = refill
        expected_actions.append({
            "name": "mc_window_act",
            "arguments": {**target, "ops": [{"op": "put", "item": item, "count": count, "to": "fuel"}]},
        })
        assert "仍停火、燃料槽空且原料仍为 raw_iron ×2" in result["message"]
    elif state == "needs_fuel":
        assert "燃料足够烧完余料" in result["message"]
        assert "先补煤或额外木料" in result["message"]
    expected_actions.append({
        "name": "mc_window_act", "arguments": {**target, "ops": [{"op": "take", "from": "output"}]},
    })
    assert result["next_actions"] == expected_actions
    assert all(bag[item] == count for item, count in (stock or {}).items())

