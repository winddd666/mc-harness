from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("tracking", [False, True])
def test_tracking_ender_eye_stops_before_wide_detour_after_first_hop(tracking) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    backend = _SurfaceScoutBackend()

    def handle(request):
        if request.url.path.endswith("/get_block") and json.loads(request.content)["pos"][0] > 33:
            return backend.ok({"name": "water"})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        registry = tools.registry(profile="compact", extra_tools=("mc_skill_scout_surface",))
        result = json.loads(registry.execute({
            "function": {
                "name": "mc_skill_scout_surface",
                "arguments": json.dumps({"direction": "east", "track_ender_eye": tracking}),
            },
        }))

    if tracking:
        assert len(backend.moves) == 1
        assert result["segments_completed"] == 1
        assert result["distance_traveled"] == 28
        assert result["pos_before"] == [0.5, 64, 0.5]
        assert result["final_pos"] == [28.5, 64, 0.5]
        assert result["stop_reason"] == "remeasure_after_detour"
        assert result["requires_replan"] is True
        assert "needs_new_vantage" not in result
    else:
        assert len(backend.moves) == 5
        assert result["direction"] == "north"


def test_tracking_ender_eye_requests_new_vantage_when_blocked_in_place() -> None:
    backend = _SurfaceScoutBackend()

    def handle(request):
        if request.url.path.endswith("/get_block"):
            return backend.ok({"name": "water"})
        return backend.handle(request)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="southeast", track_ender_eye=True)

    assert backend.moves == []
    assert result["distance_traveled"] == 0
    assert result["stop_reason"] == "remeasure_after_detour"
    assert result["needs_new_vantage"] is True
    assert result["pos_before"] == result["final_pos"]
    assert "先换到" in result["error"]["message"]
    assert "与上一次投点水平相距至少 25 格" in result["error"]["message"]


def test_tracking_ender_eye_stops_after_observed_backward_movement() -> None:
    backend = _SurfaceScoutBackend()

    def move(request, body):
        return backend.arrive(request, {**body, "x": -4.5})

    backend.move = move
    with BotSession("offline", transport=httpx.MockTransport(backend.handle)) as bot:
        result = MacroSkills(bot).scout_surface(direction="east", track_ender_eye=True)

    assert len(backend.moves) == 1
    assert result["stop_reason"] == "remeasure_after_detour"
    assert result["segments_completed"] == 0
    assert result["distance_traveled"] == 5
    assert result["final_pos"] == [-4.5, 64, 0.5]


@pytest.mark.parametrize("entry", ["find_blocks", "scout_surface"])
def test_village_scan_retains_beds_and_hay_amid_path_blocks(entry) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    crowded = [
        {"name": "dirt_path", "pos": [i % 10, 63, i // 10], "dist": 1.0}
        for i in range(200)
    ]
    anchors = [
        {"name": "hay_block", "pos": [2, 64, 0], "dist": 2.1},
        {"name": "yellow_bed", "pos": [3, 64, 0], "dist": 3.1},
    ]
    block_queries = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "find_blocks":
            block_queries.append(body["types"])
            rows = [row for row in [*crowded, *anchors] if row["name"] in body["types"]]
            result = {"blocks": rows[:body["limit"]], "total": len(rows)}
        elif route == "status":
            result = {"pos": [0.5, 64, 0.5], "biome": "plains"}
        elif route == "get_entities":
            result = {"entities": []}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        if entry == "find_blocks":
            out = json.loads(toolset.mc_find_blocks(
                radius=64, types=["dirt_path", "hay_block", "bed"], limit=20, mature=False,
            ))["result"]
            assert out["total"] == 202
            assert out["returned"] == len(out["blocks"]) == 20
            assert out["village_landmarks_prioritized"] is True
            assert ["hay_block"] in block_queries[1:]
            rows = out["blocks"]
        else:
            out = json.loads(toolset.mc_skill_scout_surface())
            assert out["stop_reason"] == "target_found"
            assert out["segments_completed"] == 0
            assert "dirt_path" not in block_queries[0]
            rows = out["detected_pois"]
        assert [row["name"] for row in rows[:2]] == ["hay_block", "yellow_bed"]
        remembered = toolset.world_memory.query_pois(["hay_block", "bed"], origin=[0.5, 64, 0.5], dim="overworld")
        assert {"hay_block", "yellow_bed"} <= {row["name"] for row in remembered}


@pytest.mark.parametrize("item,initial,cap", [("cobblestone", 320, 192), ("netherrack", 200, 128), ("dirt", 200, 64)])
def test_free_slots_enforces_retention_cap_even_with_empty_slots(item, initial, cap) -> None:
    """BUG-029: over-cap blocks were never dropped when >=min_free slots remained."""
    state = {item: initial}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "inventory":
            rows = []
            left = state[item]
            while left > 0:
                rows.append({"name": item, "count": min(64, left)})
                left -= min(64, left)
            return httpx.Response(200, json={"ok": True, "result": {"items": rows}})
        if path == "drop":
            state[item] = max(0, state[item] - int(body.get("count") or 0))
            return httpx.Response(200, json={"ok": True, "result": {}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        inv_after, dropped = skills._ensure_free_slots(min_free=3)
    assert dropped
    assert state[item] == cap
    assert all(map(lambda d: d["item"] == item, dropped))


@pytest.mark.parametrize(
    "episode_budget,cap,slow_action",
    [(None, 35.0, "move_to"), (13.0, 9.0, "place"), (17.0, 13.0, None)],
)
def test_flee_budget_reaches_shaft_waypoints_and_seals(monkeypatch, episode_budget, cap, slow_action) -> None:
    from mc_agent.harness.events import EventSink
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.guardian import GenerationGuard
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.skills.mining_dfs import MiningDFSEngine, ShaftEdge, ShaftNode

    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    pos = [0.5, 30, 2.5]
    moves = []
    placed = []
    actions = []
    start = clock[0]

    def walk_waypoints(engine, waypoints, *, reverse, min_hp_ref):
        assert reverse is True
        x, y, z = waypoints[0]
        engine.bot.call("move_to", {"x": x + 0.5, "y": y, "z": z + 0.5, "timeout": 60_000})
        engine.bot.status()
        return True, None

    monkeypatch.setattr(MiningDFSEngine, "_walk_waypoints", walk_waypoints)

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        timeout = request.extensions["timeout"]["read"]
        assert 0.0 < timeout <= start + cap - clock[0]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": pos[:], "health": 20, "alive": True, "ready": True}
        elif path == "get_block":
            result = {"name": "stone" if body["pos"][1] < 30 else "air"}
        elif path == "get_entities":
            result = {"entities": []}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 64}]}
        elif path == "move_to":
            actions.append(path)
            moves.append([body["x"], body["y"], body["z"]])
            assert 0 < body["timeout"] / 1000 < timeout
            if len(moves) == 2 and slow_action == path:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline slow shaft waypoint", request=request)
            clock[0] += min(1.0, timeout)
            pos[:] = moves[-1]
            result = {"pos": pos[:]}
        elif path == "place":
            actions.append(path)
            if slow_action == path:
                clock[0] += timeout
                raise httpx.ReadTimeout("offline slow shaft seal", request=request)
            placed.append(tuple(body["pos"]))
            result = {"placed": True}
        elif path == "stop":
            actions.append(path)
            result = {"stopped": True, "released": True}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot, geofence=VoidGeofence())
        toolset._episode_deadline_mono = None if episode_budget is None else start + episode_budget
        engine = toolset.skills.mining_dfs
        engine.nodes = {
            1: ShaftNode(1, "collar", (0, 30, 0), None, None, []),
            2: ShaftNode(2, "spine", (0, 30, 1), 1, ShaftEdge("spine_forward", "south", [(0, 30, 0)]), []),
            3: ShaftNode(3, "spine", (0, 30, 2), 2, ShaftEdge("spine_forward", "south", [(0, 30, 1)]), []),
        }
        engine.collar_node_id = 1
        engine.active_node_id = 3
        original_call = bot.call
        guard = GenerationGuard(bot, EventSink(on_event=lambda event: None), checkpoint=lambda: None)
        result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", toolset.mc_skill_flee))
        assert engine.active_node_id == (2 if slow_action else 1)
        assert engine.sealed_positions == set(placed)
        assert toolset.skills.mining_dfs is engine
        assert engine.bot is bot and bot.call == original_call

    assert clock[0] - start <= cap
    assert moves[0] == [0.5, 30, 1.5]
    if slow_action:
        assert result["ok"] is False
        assert result["stop_reason"] == "time_budget"
        assert result["timeout_path"] == slow_action
        assert result["pos_after"] == [0.5, 30, 1.5]
        assert actions[-2:] == [slow_action, "stop"]
    else:
        assert result["ok"] is True
        assert result["mode"] == "retreat_shaft"
        assert result["hops"] == 2
        assert result["sealed"] == 6
        assert "stop" not in actions


@pytest.mark.parametrize(
    "block_read,air_name",
    [
        ({"name": "air"}, "air"),
        ({"name": "minecraft:cave_air"}, "cave_air"),
        ({"block": "void_air"}, "void_air"),
        ({}, None),
        (None, None),
    ],
)
def test_mc_dig_skips_only_confirmed_air_and_rechecks_same_position(block_read, air_name) -> None:
    from mc_agent.harness.mc_tools import McToolset

    pos = [1, 64, 0]
    calls = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append(route)
        assert payload["pos"] == pos
        if route == "get_block":
            if block_read is None:
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_loaded"}})
            return httpx.Response(200, json={"ok": True, "result": block_read})
        assert route == "dig"
        assert payload["collect"] is True
        return httpx.Response(200, json={"ok": True, "result": {"pos": pos}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_dig(*pos))
        if air_name is None:
            assert result["ok"] is True
            assert calls == ["get_block", "dig"]
        else:
            assert result["ok"] is False
            assert result["requires_replan"] is True
            assert result["error"]["code"] == "nothing_to_dig"
            assert result["error"]["block"] == air_name
            assert result["error"]["pos"] == pos
            assert "mc_find_blocks" in result["error"]["message"]
            assert calls == ["get_block"]

        # A fresh read can see a newly placed block at the same coordinate.
        block_read = {"name": "oak_log"}
        calls.clear()
        assert json.loads(toolset.mc_dig(*pos))["ok"] is True
        assert calls == ["get_block", "dig"]


@pytest.mark.parametrize("case", [
    "picked_up", "native", "collect_false", "wrong_item", "too_far", "pit", "move_timeout", "status_timeout", "inventory_timeout",
])
def test_mc_dig_only_adds_a_bounded_pickup_after_confirmed_break(monkeypatch, case) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [20.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    pos = [0.5, 64.0, 0.5]
    target = [1, 65, 0]
    drop = [4.1 if case == "too_far" else 1.5, 62.0 if case == "pit" else 64.0, 0.5]
    calls, picked = [], 0

    def handle(request):
        nonlocal picked
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(route)
        if case == f"{route}_timeout" and route in {"status", "inventory"}:
            clock[0] += sum(request.extensions["timeout"].values())
            raise httpx.ReadTimeout("offline pickup read timed out", request=request)
        if route == "get_block":
            name = "oak_log" if len(calls) == 1 else "stone" if body["pos"][1] < 64 else "air"
            result = {"name": name}
        elif route == "dig":
            assert body == {"pos": target, "collect": case != "collect_false"}
            result = {"dug": "oak_log", "gained": {"oak_log": 1} if case == "native" else {"dirt": 1}}
        elif route == "status":
            result = {"pos": list(pos), "dimension": "overworld"}
        elif route == "get_entities":
            assert body == {"radius": 4, "types": ["item"]}
            item = "spruce_log" if case == "wrong_item" else "oak_log"
            result = {"entities": [{"id": 8, "item": item, "pos": drop}]}
        elif route == "inventory":
            result = {"items": [{"name": "oak_log", "count": picked + 2}]}
        elif route == "move_to":
            assert [body["x"], body["y"], body["z"]] == drop and body["range"] == 0.8
            assert 50 <= body["timeout"] < 1000
            if case == "move_timeout":
                clock[0] += sum(request.extensions["timeout"].values())
                raise httpx.ReadTimeout("offline short pickup timed out", request=request)
            pos[:] = drop
            picked += 1
            result = {"pos": list(pos)}
        elif route == "stop":
            result = {"released": False}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot)
        tools._episode_deadline_mono = 35.0
        result = json.loads(tools.mc_dig(*target, collect=case != "collect_false"))
    assert result["ok"] is True and result["result"]["dug"] == "oak_log"
    assert calls.count("dig") == 1 and clock[0] <= 21.0
    if case == "picked_up":
        assert calls.count("move_to") == 1 and picked == 1
        assert result["result"]["gained"] == {"oak_log": 1, "dirt": 1}
    elif case == "move_timeout":
        assert calls.count("move_to") == calls.count("stop") == 1
        assert result["pickup_after_dig"]["stop_confirmed"] is False
        assert result["requires_replan"] is True
        assert result["result"]["gained"] == {"dirt": 1}
    elif case in {"status_timeout", "inventory_timeout"}:
        assert "move_to" not in calls and "stop" not in calls
        assert result["pickup_after_dig"]["error"]["code"] == "pickup_time_budget"
    else:
        assert "move_to" not in calls
        assert ("get_entities" not in calls) is (case in {"native", "collect_false"})


def test_mc_dig_timeout_temporarily_avoids_same_cell() -> None:
    """r15 P2-21: dig timeout was retried on the same coordinate."""
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": [1000.5, 64.0, 1000.5]}})
        if path == "get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "oak_log"}})
        if path == "dig":
            return httpx.Response(200, json={"ok": False, "error": {"code": "timeout", "message": "dig timed out"}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        result = json.loads(toolset.mc_dig(1000, 64, 1000))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert "15 秒" in result["error"]["message"]
    assert toolset.skills.temporary_avoidance[(1000, 64, 1000)]["reason"] == "dig_timeout"


def test_bucket_interact_rejects_flowing_fluid_before_no_effect_roundtrip() -> None:
    """r13: bucket right-clicks on flowing lava repeated interact_no_effect."""
    from mc_agent.harness.mc_tools import McToolset

    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "get_block":
            return httpx.Response(200, json={"ok": True, "result": {"name": "lava", "properties": {"level": 3}}})
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_interact(x=1, y=64, z=0, item="bucket"))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == "interact_flowing_fluid"
    assert result["error"]["level"] == 3
    assert calls == ["get_block"]


def test_find_blocks_expands_families_and_keeps_valid_part_of_mixed_query() -> None:
    from mc_agent.harness.mc_tools import McToolset

    rejected = {"shield", "sheep", "made_up_block"}
    queries = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        assert path == "find_blocks"
        queries.append(body["types"])
        bad = next((name for name in body["types"] if name in rejected), None)
        if bad:
            return httpx.Response(200, json={"ok": False, "error": {"code": "bad_args", "message": f"unknown block {bad}"}})
        return httpx.Response(200, json={"ok": True, "result": {"blocks": [{"name": "oak_log", "pos": [1, 64, 1]}]}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_find_blocks(types=["minecraft:LOG", "planks", "bed", "wool", "grass", "portal", "nether_rack", "cave", "flowing_water", "shield", "sheep", "made_up_block", "future_valid_block"]))
    assert result["ok"] is True
    assert result["ignored_types"] == ["shield", "sheep", "made_up_block"]
    assert result["result"]["blocks"][0]["name"] == "oak_log"
    final = queries[-1]
    assert {"oak_log", "pale_oak_log", "birch_planks", "bamboo_planks", "white_bed", "black_wool", "short_grass", "tall_grass", "nether_portal", "netherrack", "cave_air", "water", "future_valid_block"} <= set(final)
    assert len(final) == len(set(final))
    assert len(queries) == 2


def test_find_blocks_labels_backend_total_as_truncated_scan_count() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handler(request):
        assert request.url.path.endswith("/find_blocks")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "total": 10000,
                    "blocks": [
                        {"name": "netherrack", "pos": [0, 63, 0], "dist": 1.2},
                        {"name": "netherrack", "pos": [1, 63, 0], "dist": 1.2},
                    ],
                },
            },
        )

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_find_blocks(types=["netherrack"], radius=64, limit=2))
    assert result["result"]["total"] == 10000
    assert result["result"]["returned"] == 2
    assert result["result"]["truncated"] is True
    assert "后端在半径内扫描到的匹配总数" in result["result"]["total_note"]


@pytest.mark.parametrize("types,expected_queries", [(["shield", "leather"], 0), (["made_up_one", "made_up_two"], 2)])
def test_find_blocks_all_unknown_returns_example_without_unfiltered_fallback(types, expected_queries) -> None:
    from mc_agent.harness.mc_tools import McToolset

    queries = []

    def handler(request):
        body = json.loads(request.content)
        assert body["types"]
        queries.append(body["types"])
        return httpx.Response(200, json={"ok": False, "error": {"code": "bad_args", "message": f"unknown block {body['types'][0]}"}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_find_blocks(types=types))
    assert result["ok"] is False
    assert result["ignored_types"] == types
    assert "mc_find_blocks(types=" in result["error"]["message"]
    assert len(queries) == expected_queries


@pytest.mark.parametrize("code,word", [("no_support", "支撑"), ("no_support", "侧袋"), ("blocked_by_self", "3 格"), ("place_failed", "mc_dig")])
def test_place_failure_has_specific_recovery_without_world_mutation(code, word) -> None:
    from mc_agent.errors import ApiError
    from mc_agent.harness.mc_tools import McToolset

    result = json.loads(McToolset._format_api_error("place", {"item": "crafting_table", "pos": [1, 64, 1]}, ApiError(code, "offline place failure")))
    assert result["error"]["code"] == code
    assert word in result["error"]["recovery_hint"]


@pytest.mark.parametrize("observed_distance,age,live_distance,expected_error", [
    (45.0, 0.0, 45.0, "target_out_of_reach"),
    (45.0, 0.0, None, "target_not_visible"),
    (45.0, 0.0, 2.0, None),
    (2.0, 24.5, None, "target_not_visible"),
    (2.0, 19.6, 2.0, None),
    (None, 4.7, None, "target_not_visible"),
])
def test_attack_rechecks_stale_or_distant_targets(monkeypatch, observed_distance, age, live_distance, expected_error) -> None:
    from mc_agent.harness.mc_tools import McToolset

    calls = []
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "get_entities":
            if json.loads(request.content)["radius"] == 28:
                return httpx.Response(200, json={"ok": True, "result": {"entities": []}})
            dist = observed_distance if observed_distance is not None and calls.count(path) == 1 else live_distance
            result = {"entities": [] if dist is None else [{"id": 77, "name": "glow_squid", "type": "animal", "dist": dist, "pos": [0.5, 64, dist]}]}
        elif path == "status":
            result = {"pos": [0.5, 64, 0.5], "nearest_hostile": {"id": 77, "name": "creeper", "dist": 2.7}}
        elif path == "get_block":
            result = {"name": "stone"}
        elif path == "inventory":
            result = {"items": []}
        elif path == "attack":
            result = {"hits": 1, "connected_hits": 1, "killed": False}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        initial = tools.mc_get_entities(radius=64) if observed_distance is not None else tools.mc_status()
        assert json.loads(initial)["ok"] is True
        clock[0] += age
        result = json.loads(tools.mc_attack(entity_id=77))
    assert ("attack" in calls) is (expected_error is None)
    assert result["ok"] is (expected_error is None)
    assert calls.count("get_entities") == 2
    if expected_error is not None:
        assert "inventory" not in calls
        assert result["requires_replan"] is True
        assert result["error"]["code"] == expected_error


@pytest.mark.parametrize("feedback,error_code", [
    ({"hits": 4, "connected_hits": 0}, "attack_zero_hits"),
    ({"hits": 4, "hits_connected": 0}, "attack_zero_hits"),
    ({"hits": 4, "target_hp_delta": 0}, "attack_zero_hits"),
    ({"hits": 4, "target_health_before": 12, "target_health_after": 12}, "attack_zero_hits"),
    ({"hits": 0}, "attack_zero_hits"),
    ({"hits": 4}, "attack_hit_unconfirmed"),
    ({"hits": 4, "target_hp_delta": -2}, "attack_hit_unconfirmed"),
    ({"hits": 4, "target_hp_delta": 2}, "attack_hit_unconfirmed"),
    ({"hits": 0, "connected_hits": 2}, None),
    ({"hits": 0, "target_health_before": 12, "target_health_after": 10}, None),
    ({"hits": 0, "connected_hits": 0, "killed": True}, None),
])
def test_attack_checks_feedback_and_stops_repeating_unverified_swings(feedback, error_code) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.mc_tools import McToolset

    attack_calls = []
    returned = {"killed": False, "ms": 4000, **feedback}

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        if route == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        assert route == "attack"
        attack_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": returned})

    args = {"entity_id": 77, "max_hits": 4, "interval_ms": 350, "timeout_ms": 4000}
    call = {"function": {"name": "mc_attack", "arguments": json.dumps(args)}}
    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot)
        tools.update_block_pos([0.5, 64, 0.5])
        registry = tools.registry().select("mc_attack").map_tools(_RepeatFailureGuard(tools).wrap_tool)
        reply = json.loads(registry.execute(call))
        assert reply["ok"] is (error_code is None)
        if error_code is None:
            assert reply["result"] == returned
            assert reply["hit_confirmed"] is True
        else:
            assert reply["error"]["code"] == error_code
            assert reply["requires_replan"] is True
            assert reply["hit_confirmed"] is (False if error_code == "attack_zero_hits" else None)
            assert reply["result"]["reported_hits"] == returned["hits"]
            assert "hits" not in reply["result"]
            assert all(reply["result"][key] == value for key, value in returned.items() if key != "hits")
            blocked = json.loads(registry.execute(call))
            assert blocked["error"]["code"] == "repeated_identical_failure_blocked"
    assert attack_calls == [{"entity_id": 77, "max_hits": 4, "interval_ms": 350, "timeout": 4000, "feedback": True}]


def test_attack_disappearing_entity_reuses_the_stale_id_hint() -> None:
    from mc_agent.harness.mc_tools import McToolset

    def handle(request):
        if request.url.path.endswith("/inventory"):
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        assert request.url.path.endswith("/attack")
        return httpx.Response(200, json={"ok": False, "error": {"code": "no_such_entity", "message": "gone"}})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = json.loads(McToolset(bot).mc_attack(entity_id=77))
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert "mc_get_entities" in result["error"]["message"]


def test_ender_eye_two_throw_triangulation_and_dive_detection() -> None:
    """Two orthogonal ender_eye throws should triangulate the exact stronghold (X, Z)."""
    from mc_agent.harness.mc_tools import McToolset

    state = {"pos": [0.0, 70.0, 0.0], "vec": [1.0, 0.0], "landed": [12.0, 75.0, 0.0]}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "status":
            return httpx.Response(200, json={"ok": True, "result": {"pos": state["pos"], "dimension": "overworld"}})
        if path == "use_item":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "used": {"name": "ender_eye", "count": 1},
                        "thrown": True,
                        "eye_vector": state["vec"],
                        "landed_at": state["landed"],
                    },
                },
            )
        raise AssertionError(path)

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        ts = McToolset(bot)
        # Throw 1 at (0, 0) pointing east (+X) toward stronghold at (300, 0)
        r1 = json.loads(ts.mc_use_item("ender_eye"))["result"]
        assert r1["scout_direction"] == "east"
        assert r1["suggested_second_throw_direction"] == "south"
        assert "triangulated_stronghold_xz" not in r1

        # Throw 2 at (0, 100) pointing toward (300, 0): vector = (300, -100)
        state["pos"] = [0.0, 70.0, 100.0]
        state["vec"] = [0.9486833, -0.3162278]
        state["landed"] = [11.4, 74.0, 96.2]
        r2 = json.loads(ts.mc_use_item("ender_eye"))["result"]
        assert r2["triangulated_stronghold_xz"] == [300, 0]
        assert abs(r2["triangulated_distance"] - 316.2) < 1.0
        assert "三角定位成功" in r2["scout_hint"]

        # Throw 3 directly over stronghold -> dives underground
        state["pos"] = [300.0, 70.0, 0.0]
        state["vec"] = [0.1, 0.0]
        state["landed"] = [300.5, 65.0, 0.0]
        r3 = json.loads(ts.mc_use_item("ender_eye"))["result"]
        assert r3["eye_went_down"] is True
        assert "mc_skill_staircase" in r3["scout_hint"]

