from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("scene", [
    "view_approach", "view_moved", "view_unconfirmed", "view_far",
    "trade_near", "trade_approach", "trade_fence", "trade_walks_after_view", "trade_open_failure",
    "trade_offer_changed", "trade_partial", "trade_timeout",
])
def test_merchant_window_rechecks_approach_and_never_replays_uncertain_trade(monkeypatch, scene) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    initial_distance = {
        "view_approach": 3.5, "view_unconfirmed": 3.5, "view_far": 6.0, "trade_approach": 12.0, "trade_fence": 12.0,
    }.get(scene, 2.0)
    merchant = [initial_distance, 64.0, 0.0]
    pos = [0.0, 64.0, 0.0]
    bag = {"emerald": 1, "bread": 0}
    calls = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(route)
        if route == "get_entities":
            distance = 12.0 if scene == "trade_approach" else math.dist(merchant, pos)
            result = {"entities": [{"id": 17, "name": "villager", "pos": merchant, "dist": distance}]}
        elif route == "status":
            result = {"pos": list(pos), "alive": True, "on_ground": True}
        elif route == "move_to":
            assert body == {
                "x": merchant[0], "y": merchant[1], "z": merchant[2], "range": 1.5,
                "timeout": 4200 if scene == "trade_approach" else 1800, "stop_on": ["hurt", "death"],
            }
            if scene != "view_unconfirmed":
                pos[0] = merchant[0] - 1.5
            if scene == "view_approach":
                return httpx.Response(200, json={"ok": False, "error": {
                    "code": "movement_not_confirmed", "message": "position receipt incomplete",
                }})
            result = {"pos": pos}
        elif route == "view":
            assert body == {"target": {"entity_id": 17}}
            assert math.dist(merchant, pos) <= 2.5
            first = calls.count("view") == 1
            if first and scene in {"view_moved", "trade_walks_after_view"}:
                merchant[0] = 3.5
                if scene == "view_moved":
                    return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "villager walked away"}})
            output = "apple" if scene == "trade_offer_changed" and not first else "bread"
            result = {"window": "merchant", "options": [
                {"index": 0, "give": [{"name": "emerald", "count": 1}], "get": {"name": output, "count": 6}, "available": True},
            ]}
        elif route == "act":
            assert body == {"target": {"entity_id": 17}, "ops": [{"op": "select", "option": 0, "times": 1}]}
            assert math.dist(merchant, pos) <= 2.5
            if calls.count("act") == 1 and scene in {"trade_open_failure", "trade_offer_changed"}:
                merchant[0] = 3.5
                return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "before opening"}})
            assert bag["emerald"] == 1
            bag.update(emerald=0, bread=6)
            done = [{"op": "select", "times": 1}]
            if scene == "trade_partial":
                return httpx.Response(200, json={"ok": False, "error": {
                    "code": "out_of_reach", "message": "after selection", "failed_op": 0, "done": done,
                }})
            if scene == "trade_timeout":
                clock[0] += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("ambiguous selection", request=request)
            result = {"done": done}
        elif route == "stop":
            result = {"released": True}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence(radius=16 if scene == "trade_fence" else 32))
        raw = tools.mc_window_view(entity_id=17) if scene.startswith("view") else tools.mc_skill_trade(entity_id=17, option=0)
        result = json.loads(raw)

    assert calls.count("move_to") == int(scene in {
        "view_approach", "view_moved", "view_unconfirmed", "trade_approach",
        "trade_walks_after_view", "trade_open_failure", "trade_offer_changed",
    })
    no_trade = scene.startswith("view") or scene == "trade_fence"
    assert calls.count("act") == (0 if no_trade else 2 if scene == "trade_open_failure" else 1)
    if scene in {"view_unconfirmed", "view_far"}:
        assert result["error"]["code"] == "out_of_reach" and "view" not in calls
    elif scene == "trade_fence":
        assert result["error"] == "void_geofence_blocked" and "view" not in calls
    elif scene == "trade_offer_changed":
        assert result["error"] == "trade_changed" and bag == {"emerald": 1, "bread": 0}
    elif scene == "trade_partial":
        assert result["error"] == "out_of_reach" and result["done"] == [{"op": "select", "times": 1}]
        assert bag == {"emerald": 0, "bread": 6}
    elif scene == "trade_timeout":
        assert result["error"]["code"] == "trade_time_budget" and result["error"]["action_released"] is True
        assert calls[-2:] == ["act", "stop"] and bag == {"emerald": 0, "bread": 6}
    else:
        assert result["ok"]
        if scene.startswith("view"):
            assert result["result"]["options"][0]["get"]["name"] == "bread"
        else:
            assert result["done"] == [{"op": "select", "times": 1}] and bag == {"emerald": 0, "bread": 6}


@pytest.mark.parametrize("action, op", [
    ("GET", "take"), (" withdraw ", "take"), ("store", "put"), ("deposit", "put"),
])
def test_skill_container_normalizes_action_aliases(action, op) -> None:
    from mc_agent.harness.mc_tools import McToolset

    sent = []
    pos = [1, 64, 0]

    def handler(request):
        assert request.url.path.endswith("/window/act")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"done": []}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_container(
            [{"item": "iron_ingot", "count": 2}], action=action, x=pos[0], y=pos[1], z=pos[2],
        ))

    assert result["ok"] is True
    assert result["action"] == op
    assert sent == [{"target": {"pos": pos}, "ops": [
        {"op": op, "item": "iron_ingot", "count": 2, "from" if op == "take" else "to": "container"},
    ]}]


@pytest.mark.parametrize("items, options, message", [
    ([], {"action": "take", "block": "chest"}, "物品名和数量"),
    ([{"item": "iron_ingot", "count": 2}], {"action": "take", "x": 1}, "完整坐标"),
    ([{"item": "iron_ingot", "count": 2}], {"action": "inspect", "block": "chest"}, "mc_window_view"),
    ([{"item": "iron_ingot", "count": 2}], {"block": "chest"}, "action"),
    ([{"item": "wheat", "count": 1}, {"item": "coal", "items": 20}], {"action": "store", "block": "chest"}, "count"),
])
def test_skill_container_explains_invalid_requests_without_opening(items, options, message) -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        raise AssertionError(request.url.path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_container(items, **options))

    assert result["ok"] is False
    assert result["error"] == "bad_args"
    assert message in result["message"]


def test_skill_container_registry_requires_an_action() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        raise AssertionError(request.url.path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        registry = McToolset(bot).registry()
        call = {"function": {"name": "mc_skill_container", "arguments": json.dumps({
            "items": [{"item": "iron_ingot", "count": 16}], "x": -6401, "y": 67, "z": -6380,
        })}}
        result = registry.execute(call)

    assert "必填：action, items" in result


def test_skill_container_marks_confirmed_transfers_on_batch_failure() -> None:
    from mc_agent.harness.mc_tools import McToolset

    stored = {}
    calls = []

    def handler(request):
        assert request.url.path.endswith("/window/act")
        ops = json.loads(request.content)["ops"]
        calls.append(ops)
        done = []
        for op in ops:
            if op["item"] == "gold_ingot":
                return httpx.Response(200, json={"ok": False, "error": {
                    "code": "missing_item", "message": "no gold_ingot in inventory", "failed_op": 2, "done": done,
                }})
            stored[op["item"]] = op["count"]
            done.append({"op": "put", "put": op["item"], "count": op["count"], "to": "container"})
        raise AssertionError("batch should fail after the first two transfers")

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_container(
            [{"item": "diamond_pickaxe"}, {"item": "diamond", "count": 4},
             {"item": "gold_ingot", "count": 16}, {"item": "wheat", "count": 24}],
            action="store", x=-7032, y=123, z=5801,
        ))

    assert len(calls) == 1 and stored == {"diamond_pickaxe": 1, "diamond": 4}
    assert result["ok"] is False and result["error"] == "missing_item"
    assert result["partial_transfer"] is True and result["requires_replan"] is True
    assert result["failed_op"] == 2 and {entry["put"]: entry["count"] for entry in result["done"]} == stored
    assert "done" in result["message"] and "复查" in result["message"]


def test_brew_bottle_alias_accepts_water_bottle_shape() -> None:
    from mc_agent.skills.library import _window_item_entries

    entries = _window_item_entries([{"item": "minecraft:water_bottle", "slot": 0}])
    assert entries == [{"item": "potion", "potion": "water", "count": 1, "slot": 0}]


def test_brew_missing_item_reports_inventory_counts() -> None:
    def handler(request):
        path = request.url.path
        if path.endswith("/window/act"):
            return httpx.Response(
                200, json={"ok": False, "error": {"code": "missing_item", "message": "no blaze_powder"}}
            )
        if path.endswith("/inventory"):
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "cobblestone", "count": 5}]}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).brew_once(
            "nether_wart",
            bottles=[{"item": "water_bottle"}],
            fuel_item="blaze_powder",
            block="brewing_stand",
            x=10,
            y=64,
            z=10,
            wait=False,
        )
    assert result["ok"] is False
    assert result["error"] == "missing_item"
    assert result["inventory"]["cobblestone"] == 5
    assert "inventory" in result["message"]


def test_brew_no_window_reports_wrong_block_at_coordinates() -> None:
    """r16 brewing used a coordinate that actually held cobblestone."""
    calls = []

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/window/act"):
            return httpx.Response(
                200, json={"ok": False, "error": {"code": "no_window", "message": "cobblestone did not open a GUI"}}
            )
        if path.endswith("/get_block"):
            return httpx.Response(200, json={"ok": True, "result": {"name": "cobblestone"}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).brew_once(
            "nether_wart",
            bottles=[{"item": "potion", "count": 1}],
            block="brewing_stand",
            x=10,
            y=64,
            z=10,
            wait=False,
        )
    assert result["ok"] is False
    assert result["error"] == "window_target_mismatch"
    assert result["actual"] == "cobblestone"
    assert len(calls) == 2
    assert calls[0].endswith("/window/act")
    assert calls[1].endswith("/get_block")

