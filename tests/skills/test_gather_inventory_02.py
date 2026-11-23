from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("pickup", ["collected", "partial", "unsafe", "gone", "unreleased", "stale", "after_hostile"])
def test_mine_dfs_checks_observed_target_drops_before_digging(monkeypatch, pickup) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    position = [0.5, 64.0, 0.5]
    drops = [
        {"id": 7, "item": "raw_ironx2", "pos": [0.95, 64.0, 0.5], "dist": 0.45},
        {"id": 8, "item": "cobblestonex4", "pos": [0.5, 64.0, 1.5], "dist": 1.0},
    ]
    actions = []
    owned = 0

    def handler(request):
        nonlocal owned
        route = request.url.path.rsplit("/", 1)[-1]
        actions.append(route)
        body = json.loads(request.content) if request.content else {}
        if route == "get_entities":
            if pickup == "gone" and actions.count("get_entities") == 2:
                clock[0] += 0.75
            result = {"entities": [] if body.get("types") == ["hostile"] else drops}
        elif route == "status":
            result = {"pos": position, "dimension": "overworld"}
        elif route == "get_block":
            y = body["pos"][1]
            result = {"name": "stone" if y < 64 else "water" if pickup == "unsafe" else "air"}
        elif route == "move_to":
            assert [body["x"], body["y"], body["z"]] == drops[0]["pos"]
            if pickup == "unreleased":
                clock[0] += sum(request.extensions["timeout"].values())
                raise httpx.RemoteProtocolError("offline move response lost", request=request)
            position[:] = drops[0]["pos"]
            if pickup == "partial":
                drops[0]["item"] = "raw_ironx1"
                owned = 1
            else:
                drops.pop(0)
                owned = 2
            result = {"pos": position}
        elif route == "stop":
            clock[0] += sum(request.extensions["timeout"].values())
            result = {"released": False}
        elif route == "inventory":
            result = {"items": [{"name": "raw_iron", "count": owned}] if owned else []}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    def mine(**kwargs):
        actions.append("dfs")
        refreshed = pickup == "gone" and actions.count("dfs") == 1
        assert kwargs["max_duration_s"] == pytest.approx(31.25 if refreshed else 32)
        return {"ok": True, "dug_count": 1}

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        tools.skills.mine_dfs = mine
        tools.mc_get_entities(radius=6, types=["item"])
        if pickup == "after_hostile":
            clock[0] += 2.0
            tools.mc_get_entities(radius=6, types=["hostile"])
            clock[0] += 1.8
        else:
            clock[0] += 6.001 if pickup == "stale" else 3.8
        if pickup == "gone":
            drops.pop(0)
        result = json.loads(tools.mc_skill_mine_dfs(target="iron", goal_count=2))
        if pickup == "gone":
            repeated = json.loads(tools.mc_skill_mine_dfs(target="iron", goal_count=2))
            assert "pickup_before_mining" not in repeated

    if pickup in {"collected", "partial", "stale", "after_hostile"}:
        expected = 1 if pickup == "partial" else 2
        assert "dfs" not in actions
        assert result["ok"] is True and result["goal_met"] is (expected == 2) and result["dug_count"] == 0
        assert result["gained"] == {"raw_iron": expected} and result["target_gained"] == expected
        attempt = result["pickup_before_mining"]["attempts"][0]
        assert attempt["pickup_inventory_gain"] == expected
        assert attempt["picked_up"] == (0 if pickup == "partial" else 2)
    elif pickup == "unreleased":
        assert "dfs" not in actions and actions.count("move_to") == 1 and actions.count("stop") == 1
        assert result["ok"] is False and result["pickup_before_mining"]["stop_confirmed"] is False
        assert clock[0] <= 109.8
    else:
        assert "dfs" in actions and "move_to" not in actions
        assert actions.count("get_entities") == 2 and actions.index("dfs") > 1
        skips = {row["id"]: row["reason"] for row in result["pickup_before_mining"]["skipped"]}
        assert skips[8] == "non_target_item"
        if pickup == "unsafe":
            assert skips[7] == "unsafe_route"


def test_collect_drops_honors_explicit_budget_without_item_filter(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handler(request):
        assert request.url.path.endswith("/inventory")
        timeout = request.extensions["timeout"]["read"]
        assert 0 < timeout <= 5.0
        clock[0] += timeout
        raise httpx.ReadTimeout("offline slow inventory", request=request)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).collect_drops(radius=8, max_duration_s=6.0)

    assert result["error"] == "pickup_time_budget"
    assert clock[0] <= 106.0


@pytest.mark.parametrize("item_types", [None, ["diamond"]])
def test_collect_drops_accounts_for_the_protocol_retry_in_its_budget(monkeypatch, item_types) -> None:
    clock = [100.0]
    requests = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def handler(request):
        assert request.url.path.endswith("/inventory")
        attempt_cost = sum(request.extensions["timeout"].values())
        requests.append(attempt_cost)
        clock[0] += min(4.95, attempt_cost - 0.01) if len(requests) == 1 else attempt_cost
        if len(requests) == 1:
            raise httpx.RemoteProtocolError("offline keepalive closed", request=request)
        raise httpx.ReadTimeout("offline retry stalled", request=request)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).collect_drops(item_types=item_types, max_duration_s=6.0)

    assert result["error"] == "pickup_time_budget"
    assert len(requests) == 2
    assert sum(requests) + 0.15 <= 5.0 + 1e-9
    assert clock[0] <= 106.0


@pytest.mark.parametrize("fault_path, protocol_twice, released", [
    ("status", False, True), ("move_to", False, True),
    ("move_to", True, True), ("move_to", True, False),
])
def test_dfs_drop_recovery_shares_short_deadline_with_native_budget(monkeypatch, fault_path, protocol_twice, released) -> None:
    from mc_agent.skills.mining_dfs import _BudgetSession, _MiningDeadline

    clock = [100.0]
    faults = []
    stops = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == fault_path and len(faults) < 2:
            timeout = request.extensions["timeout"]
            faults.append(sum(timeout.values()))
            clock[0] += min(4.95, timeout["read"] - 0.01) if len(faults) == 1 else faults[-1]
            if len(faults) == 1 or protocol_twice:
                raise httpx.RemoteProtocolError("offline keepalive closed", request=request)
            raise httpx.ReadTimeout("offline retry stalled", request=request)
        if path == "get_block":
            result = {"name": "stone" if body["pos"][1] == 63 else "air"}
        elif path == "stop":
            stops.append(clock[0])
            clock[0] += sum(request.extensions["timeout"].values())
            result = {"released": released}
        else:
            result = {
                "status": {"pos": [0.5, 64.0, 0.5], "dimension": "overworld"},
                "get_entities": {"entities": [{"id": 1, "item": "diamond", "pos": [1.5, 64.0, 0.5]}]},
                "inventory": {"items": []},
            }[path]
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        dfs = _BudgetSession(bot, deadline=130.0)
        dfs.dug_count = 1
        def pickup():
            return MacroSkills(dfs.session)._collect_nearby_items(
                bot_y=64.0, radius=6, max_pickups=2, target_items={"diamond"},
            )

        if released:
            result = pickup()
            assert result["error"]["code"] == "pickup_time_budget"
            assert dfs.timeout_path is None
            assert dfs.session.status()["pos"] == [0.5, 64.0, 0.5]
        else:
            with pytest.raises(_MiningDeadline):
                pickup()
            assert dfs.timeout_path == "move_to"
        assert len(faults) == 2 and sum(faults) + 0.15 <= 5.0 + 1e-9
        assert clock[0] <= 106.0
        assert len(stops) == int(fault_path == "move_to")
        assert dfs.deadline == 130.0


@pytest.mark.parametrize("used,gained,ok", [({}, {"lava_bucket": 1}, True), ({"bucket": 1}, {}, True), ({"bucket": 1}, {"lava_bucket": 1}, True), ({}, {}, False)])
def test_bucket_interact_inventory_delta_overrides_stale_block_snapshot(used, gained, ok) -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            result = {"pos": [0.5, 64, 0.5]}
        elif path == "get_block":
            result = {"name": "lava", "properties": {"level": 0}}
        elif path == "interact":
            result = {"before": "lava", "after": "lava", "changed": False, "used": used, "gained": gained}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_interact(x=1, y=64, z=0, item="bucket"))
    assert result["ok"] is ok
    if not ok:
        assert result["error"]["code"] == "interact_no_effect"
    else:
        assert result["result"]["gained"] == gained
        assert "error" not in result


@pytest.mark.parametrize("task", ["iron_boots", "furnaces", "raw_iron", "sand", "sand_new_furnace"])
def test_full_station_inventory_uses_excess_blocks_without_spending_reserved_inputs(monkeypatch, task) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    constructor = task in {"iron_boots", "furnaces"}
    item = {"furnaces": "furnace", "sand_new_furnace": "sand"}.get(task, task)
    output = {"raw_iron": "iron_ingot", "sand": "glass"}.get(item, item)
    bag = {"cobblestone": 192, "cobbled_deepslate": 192, "netherrack": 128, "dirt": 64}
    bag.update({"sand": 64, "coal": 3} if item == "sand" else {"raw_iron": 8, "coal": 3})
    if task == "iron_boots":
        bag["iron_ingot"] = 8
    filler_count = 36 - sum(math.ceil(count / 64) for count in bag.values())
    filler = [{"name": "iron_pickaxe", "count": 1} for _ in range(filler_count)]
    drops, writes = [], []
    loaded = 0
    placed = task != "sand_new_furnace"
    armor = {}

    def empty_slots():
        return 36 - len(filler) - sum(math.ceil(count / 64) for count in bag.values())

    def handle(request):
        nonlocal loaded, placed
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            rows = [
                {"name": name, "count": min(64, rest)}
                for name, count in bag.items() for rest in range(count, 0, -64)
            ]
            result = {"items": filler + rows, "empty_slots": empty_slots(), "armor": armor}
        elif route == "status":
            result = {"pos": [0.5, 44.0, 0.5], "dimension": "overworld", "health": 20, "food": 20}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": [1, 44, 0], "dist": 1}] if placed or body["types"] != ["furnace"] else []}
        elif route == "drop":
            drops.append(body)
            bag[body["item"]] -= body["count"]
            result = {}
        elif route == "craft":
            if empty_slots() < 3:
                return httpx.Response(200, json={"ok": False, "error": {"code": "inventory_full"}})
            writes.append(body)
            count = body["count"]
            crafted = body["item"]
            material, per = ("cobblestone", 8) if crafted == "furnace" else ("iron_ingot", 4)
            assert bag[material] >= count * per
            bag[material] -= count * per
            bag[crafted] = count
            result = {"crafted": crafted, "gained": count}
        elif route == "equip":
            assert body["item"] == "iron_boots"
            armor[body["slot"]] = {"name": body["item"]}
            bag.pop("iron_boots")
            result = {}
        elif route == "place":
            assert task == "sand_new_furnace" and body["item"] == "furnace"
            assert bag.pop("furnace") == 1
            placed = True
            result = {"pos": [1, 44, 0]}
        elif route == "get_block":
            assert task == "sand_new_furnace" and placed
            result = {"name": "furnace", "properties": {"lit": True}}
        elif route == "act":
            ops = body["ops"]
            writes.extend(ops)
            if ops[0]["op"] == "put":
                assert empty_slots() >= 3
                assert ops[0]["item"] == item and ops[0]["count"] == 3
                for op in ops:
                    bag[op["item"]] -= op["count"]
                    assert bag[op["item"]] >= 0
                loaded = 3
                result = {}
            else:
                assert ops == [{"op": "take", "from": "output", "count": 3}]
                bag[output] = loaded
                result = {"done": [{"took": {output: loaded}}]}
                loaded = 0
        elif route == "view":
            result = {"sections": {"input": [], "output": [{"name": output, "count": loaded}]}, "info": {"burning": False}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    assert empty_slots() == 0
    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot)
        result = json.loads(
            tools.mc_skill_craft_gear(item, count=16 if task == "furnaces" else 1)
            if constructor else tools.mc_skill_smelt(item, count=3, fuel_item="coal")
        )
    assert result["ok"] is True
    if task == "iron_boots":
        assert result["equipped_item"]["name"] == output
    else:
        assert bag[output] == (16 if task == "furnaces" else 3)
    assert len(writes) == (1 if constructor else 4 if task == "sand_new_furnace" else 3)
    assert sum(drop["count"] for drop in drops) == 192
    assert all(drop["item"] in {"cobblestone", "cobbled_deepslate", "netherrack"} for drop in drops)
    if item == "sand":
        assert bag["sand"] == 61


@pytest.mark.parametrize("drop_confirmed", [True, False])
@pytest.mark.parametrize("empty_slots", [0, 2, 8])
def test_mine_dfs_triggers_retention_cleanup(drop_confirmed, empty_slots) -> None:
    from mc_agent.harness.mc_tools import McToolset

    near_capacity = empty_slots < 3
    block, original, cap, dimension = (
        ("netherrack", 576, 128, "the_nether") if near_capacity else ("cobblestone", 300, 192, "overworld")
    )
    state = {block: original}
    protected = [{"name": "diamond_pickaxe", "count": 1}] * (36 - math.ceil(original / 64) - empty_slots)
    looks = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            rows = [
                {"name": block, "count": min(64, state[block] - offset)}
                for offset in range(0, state[block], 64)
            ] + protected
            return httpx.Response(200, json={"ok": True, "result": {"items": rows}})
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {
                "pos": [0.5, 30.0, 0.5], "yaw": 1.0, "pitch": 0.4, "dimension": dimension, "on_ground": True,
            }})
        if path == "look":
            looks.append(body)
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "drop":
            assert looks == [{"yaw": 1.0 + math.pi, "pitch": 0.0}]
            assert body["item"] == block
            if drop_confirmed:
                state[block] -= int(body.get("count") or 0)
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset.skills.mine_dfs = lambda **kwargs: {"ok": True, "dug_count": 1}
        result = json.loads(toolset.mc_skill_mine_dfs(target="stone", goal_count=1))
    assert result["inventory_cleanup"]["method"] == "drop_fallback"
    assert looks[-1] == {"yaw": 1.0, "pitch": 0.4}
    if drop_confirmed:
        assert result["inventory_cleaned"] == [{"item": block, "count": original - cap}]
        assert state[block] == cap
    else:
        assert "inventory_cleaned" not in result
        assert result["inventory_cleanup"]["error"] == "cleanup_unconfirmed"
        assert state[block] == original


@pytest.mark.parametrize("mode,spare", [
    *[(mode, 0) for mode in (
        "owned", "carried", "crafted", "unsafe", "dead", "no_transfer", "timeout", "unconfirmed_place",
        "late_death", "lost_put_reply", "unreleased", "action_timeout",
    )],
    ("owned", 8), ("lost_put_reply", 8), ("unreleased", 8),
    ("no_bag_debit", 8), ("no_box_credit", 8),
])
def test_mine_dfs_stashes_clutter_off_the_return_lane(mode, spare, monkeypatch) -> None:
    from mc_agent.harness.mc_tools import McToolset

    origin = [0.5, 30.0, 0.5]
    side = [1, 30, 0]
    cobble = 311 if spare else 384
    excess = cobble - 192
    bag_chest = mode in {"carried", "unconfirmed_place"}
    plank_stacks = ["oak_planks", "birch_planks"] if mode == "crafted" else []
    protected_count = 30 - int(bag_chest) - len(plank_stacks) - int(mode == "crafted") - spare
    chest = mode not in {"carried", "crafted", "unconfirmed_place"}
    died = False
    stored = 0
    picked = bool(spare)
    actions = []
    clock = [100.0]
    if mode in {"lost_put_reply", "action_timeout"}:
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def handler(request):
        nonlocal cobble, bag_chest, chest, stored, picked, died
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        actions.append(route)
        if route == "inventory":
            if mode == "late_death" and stored:
                died = True
            stones = [{"name": "cobblestone", "count": min(64, cobble - n)} for n in range(0, cobble, 64)]
            protected = [{"name": "stone_pickaxe", "count": 1}] * protected_count
            items = [] if died else stones + protected + [{"name": name, "count": 8} for name in plank_stacks]
            if bag_chest:
                items.append({"name": "chest", "count": 1})
            if picked:
                items.append({"name": "diamond", "count": 1})
            result = {"items": items}
        elif route == "status":
            result = {"pos": origin, "dimension": "overworld", "health": 20, "last_death": int(died)}
        elif route == "get_block":
            x, y, z = body["pos"]
            if [x, y, z] == side and chest:
                name = "chest"
            elif x == 0 and y in (30, 31):
                name = "air"
            elif x == 1 and z == 0 and y in (30, 31) and mode in {"carried", "crafted", "unconfirmed_place"}:
                name = "air"
            else:
                name = "stone"
            result = {"name": name}
        elif route == "find_blocks":
            assert mode == "crafted" and body["types"] == ["crafting_table"]
            result = {"blocks": [{"name": "crafting_table", "pos": [-1, 30, 0], "dist": 1}]}
        elif route == "craft":
            assert mode == "crafted" and body == {"item": "chest", "count": 1}
            plank_stacks.pop(0)
            bag_chest = True
            result = {"crafted": "chest", "gained": 1}
        elif route == "place":
            assert body == {"item": "chest", "pos": side} and bag_chest
            if mode == "unconfirmed_place":
                return httpx.Response(200, json={"ok": True, "result": {}})
            bag_chest = False
            chest = True
            result = {"pos": side, "placed": "chest"}
        elif route == "view":
            assert body["target"]["pos"] == side
            result = {"window": "generic_9x3", "sections": {
                "container": [{"name": "cobblestone", "count": stored}] if stored else [],
            }}
        elif route == "stop":
            result = {"released": mode != "unreleased"}
        elif route == "act":
            assert body["target"]["pos"] == side and chest and mode != "unsafe"
            if mode == "dead":
                died = True
                return httpx.Response(200, json={"ok": False, "error": {
                    "code": "dead", "message": "died during transfer", "done": [],
                }})
            ops = body["ops"]
            assert all(op["op"] == "put" and op["item"] == "cobblestone" for op in ops)
            if mode not in {"no_transfer", "no_box_credit"}:
                stored += sum(op["count"] for op in ops)
            if mode not in {"no_transfer", "no_bag_debit"}:
                cobble -= sum(op["count"] for op in ops)
            if mode == "timeout":
                raise httpx.ReadTimeout("offline lost storage receipt", request=request)
            if mode in {"lost_put_reply", "unreleased"}:
                raise httpx.RemoteProtocolError("offline lost reply after deposit", request=request)
            if mode == "action_timeout":
                clock[0] += sum(request.extensions["timeout"].values())
                return httpx.Response(200, json={"ok": False, "error": {
                    "code": "action_timeout", "message": "deposit executed before action timeout",
                }})
            result = {"window": {"window": "generic_9x3", "sections": {
                "container": [{"name": "cobblestone", "count": stored}],
            }}}
            if mode in {"no_bag_debit", "no_box_credit"}:
                result["done"] = [{"put": "cobblestone", "to": "container", "count": excess}]
        elif route in {"disconnect", "connect"} and mode == "action_timeout":
            clock[0] += 5.0
            result = {}
        elif route == "move_to":
            assert body["x"] == 0.5 and body["y"] == 30
            origin[2] = body["z"]
            picked = picked or body["z"] == -1.5
            result = {"pos": list(origin)}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        if mode not in {"carried", "crafted", "unsafe", "unconfirmed_place"}:
            tools.skills.remember_placed_storage("chest", side, "overworld")
        tools.skills.mine_dfs = lambda **kwargs: {"ok": True, "dug_count": 1}
        outcome = json.loads(tools.mc_skill_mine_dfs(target="diamond"))
        if mode not in {"unsafe", "dead", "no_transfer", "unconfirmed_place", "late_death", "unreleased", "no_bag_debit", "no_box_credit"}:
            bot.call("move_to", {"x": 0.5, "y": 30, "z": -1.5})
            bot.call("move_to", {"x": 0.5, "y": 30, "z": 0.5})
            assert any(item["name"] == "diamond" for item in bot.inventory()["items"])

    if mode == "unsafe":
        assert outcome["inventory_cleanup"]["method"] == "no_safe_storage"
        assert "act" not in actions and "place" not in actions
        assert cobble == 384
    elif mode == "unconfirmed_place":
        assert not outcome.get("inventory_cleaned") and "act" not in actions
        assert outcome["inventory_cleanup"]["error"] == "storage_placement_unconfirmed"
    elif mode in {"dead", "no_transfer"}:
        assert not outcome.get("inventory_cleaned") and stored == 0
        assert outcome["inventory_cleanup"]["error"] == ("dead" if mode == "dead" else "storage_transfer_unconfirmed")
    elif mode == "late_death":
        assert not outcome.get("inventory_cleaned") and stored == 192
        assert outcome["inventory_cleanup"]["error"] == "storage_life_changed"
        assert outcome["inventory_cleanup"]["empty_slots"] == 0
    elif mode == "unreleased":
        assert not outcome.get("inventory_cleaned") and stored == excess
        assert outcome["inventory_cleanup"]["error"] == "storage_action_unconfirmed"
        assert actions.count("view") == actions.count("act") == actions.count("stop") == 1
    elif mode in {"no_bag_debit", "no_box_credit"}:
        assert not outcome.get("inventory_cleaned") and actions.count("act") == 1
        assert outcome["inventory_cleanup"]["error"] == "storage_transfer_unconfirmed"
        assert "move_to" not in actions
    else:
        assert outcome["inventory_cleanup"]["method"] == ("placed_storage" if mode in {"carried", "crafted"} else "owned_storage")
        assert outcome["inventory_cleanup"]["empty_slots"] >= 3
        assert outcome["inventory_cleaned"] == [{"item": "cobblestone", "count": excess}]
        assert cobble == 192 and stored == excess
        if mode == "crafted":
            assert plank_stacks == ["birch_planks"]
        if mode in {"lost_put_reply", "action_timeout"}:
            assert actions.count("act") == 1 and actions.count("stop") == 1
            assert actions.index("act") < actions.index("stop") < actions.index("move_to")
            assert "disconnect" not in actions and "connect" not in actions
            assert clock[0] <= 108.0
    assert "drop" not in actions


@pytest.mark.parametrize("fault", ["passage", "passage_far", "full"])
def test_mine_cleanup_skips_passage_storage_and_tries_a_backup(fault) -> None:
    from mc_agent.harness.mc_tools import McToolset

    first = {"passage": [0, 30, 1], "passage_far": [0, 30, 2], "full": [1, 30, 0]}[fault]
    backup = [-1, 30, 0]
    cobble = 384
    stored = 0
    opened = []
    transfers = []

    def handler(request):
        nonlocal cobble, stored
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            result = {"pos": [0.5, 30.0, 0.5], "dimension": "overworld", "last_death": 0}
        elif route == "inventory":
            items = [{"name": "cobblestone", "count": 64}] * (cobble // 64)
            items += [{"name": "stone_pickaxe", "count": 1}] * 30
            result = {"items": items}
        elif route == "get_block":
            x, y, z = body["pos"]
            if body["pos"] == first or (fault == "full" and body["pos"] == backup):
                name = "chest"
            else:
                name = "air" if x == 0 and y in (30, 31) else "stone"
            result = {"name": name}
        elif route in {"view", "act"}:
            site = body["target"]["pos"]
            if route == "view":
                opened.append(site)
                result = {"sections": {"container": [{"name": "cobblestone", "count": stored}] if stored else []}}
            else:
                transfers.append(site)
                if site == first:
                    return httpx.Response(200, json={"ok": False, "error": {
                        "code": "section_full", "message": "container is full", "done": [],
                        "window": {"sections": {"container": []}},
                    }})
                amount = body["ops"][0]["count"]
                stored += amount
                cobble -= amount
                result = {"done": [{"to": "container", "put": "cobblestone", "count": amount}]}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        tools.skills.remember_placed_storage("chest", first, "overworld")
        if fault == "full":
            tools.skills.remember_placed_storage("chest", backup, "overworld")
        tools.skills.mine_dfs = lambda **kwargs: {"ok": True, "dug_count": 1}
        result = json.loads(tools.mc_skill_mine_dfs(target="diamond"))

    if fault != "full":
        assert not opened and not transfers and cobble == 384
        assert result["inventory_cleanup"]["storage_skipped"] == [{"pos": first, "reason": "blocks_passage"}]
    else:
        assert transfers == [first, backup] and cobble == stored == 192
        assert result["inventory_cleanup"]["pos"] == backup
        assert result["inventory_cleanup"]["storage_skipped"] == [{"pos": first, "reason": "section_full"}]


@pytest.mark.parametrize("lost_reply", [False, True])
def test_native_dfs_stashes_full_inventory_before_collecting_ore(monkeypatch, lost_reply) -> None:
    from mc_agent.skills.mining_dfs import _BudgetSession

    monkeypatch.setattr(time, "sleep", lambda _: None)
    side = [0, 30, 1]
    position = [0.5, 30.0, 0.5]
    cobble = 384
    stored = 0
    diamond = 0
    actions = []

    def handler(request):
        nonlocal cobble, stored, diamond
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        actions.append(route)
        if route == "status":
            result = {"pos": position, "dimension": "overworld", "last_death": 0}
        elif route == "inventory":
            items = [{"name": "cobblestone", "count": 64}] * (cobble // 64)
            items += [{"name": "iron_pickaxe", "count": 1}] * 30
            if diamond:
                items.append({"name": "diamond", "count": diamond})
            result = {"items": items}
        elif route == "get_block":
            x, y, z = body["pos"]
            name = "chest" if body["pos"] == side else ("air" if y >= 30 and z == 0 else "stone")
            result = {"name": name}
        elif route == "view":
            assert body["target"]["pos"] == side
            result = {"sections": {"container": [{"name": "cobblestone", "count": stored}] if stored else []}}
        elif route == "act":
            assert body["target"]["pos"] == side
            count = body["ops"][0]["count"]
            stored += count
            cobble -= count
            if lost_reply:
                raise httpx.RemoteProtocolError("offline lost reply after deposit", request=request)
            result = {"done": [{"to": "container", "put": "cobblestone", "count": count}]}
        elif route == "stop":
            result = {"released": True}
        elif route == "get_entities":
            result = {"entities": [] if diamond else [{"id": 11, "item": "diamond", "pos": [1.5, 30.0, 0.5]}]}
        elif route == "move_to":
            assert cobble + stored == 384 and stored == 192
            position[:] = [body["x"], body["y"], body["z"]]
            diamond = 1
            result = {"pos": position}
        elif route == "drop":
            cobble -= body["count"]
            result = {}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        dfs = _BudgetSession(bot, deadline=time.monotonic() + 30)
        dfs.dug_count = 1
        skills = MacroSkills(dfs.session)
        skills.remember_placed_storage("chest", side, "overworld")
        confirmed = skills.mining_dfs._collect_and_verify_target_drop(
            expected_drops={"diamond"}, before_count=0, ore_pt=(1, 30, 0), stand_y=30, cache={},
        )

    assert confirmed is True and diamond == 1
    assert cobble == stored == 192
    assert actions.count("act") == 1 and actions.count("stop") == int(lost_reply)
    assert "drop" not in actions and actions.index("act") < actions.index("move_to")

