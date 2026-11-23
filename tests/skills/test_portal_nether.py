from tests.skills._common import *  # noqa: F401,F403
from tests.skills._common import _SurfaceScoutBackend

@pytest.mark.parametrize("axis", ["x", "z"])
def test_portal_entry_clears_approach_and_confirms_dimension(monkeypatch, axis) -> None:
    # Reproduce r11: the frame is already lit, move_to reports success while
    # still outside (one block below the portal), then a jump enters the plane.
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    target = [0, 65, 2]
    approach = [0, 65, 1] if axis == "x" else [-1, 65, 2]
    state = {"pos": [0.5, 64.0, 1.5] if axis == "x" else [-0.5, 64.0, 2.5], "contact": None}
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append((path, body))
        if path == "status":
            assert request.method == "GET"
            transferred = state["contact"] is not None and clock[0] - state["contact"] >= 4
            result = {"pos": state["pos"], "dimension": "the_nether" if transferred else "overworld"}
        elif path == "find_blocks":
            # Deliberately give the top row first.
            result = {"blocks": [{"pos": [0, 67, 2]}, {"pos": target}]}
        elif path == "get_block":
            result = {"name": "nether_portal", "properties": {"axis": axis}} if body["pos"] == target else {"name": "wildflowers" if body["pos"] == approach else "air"}
        elif path == "control":
            assert body["jump"] is True
            state["pos"] = [0.5, 65.0, 2.5]
            state["contact"] = clock[0]
            result = {"pos": state["pos"]}
        elif path in {"move_to", "dig", "look"}:
            result = {"pos": state["pos"]}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(wait_s=8)
    assert result["ok"] is True
    assert result["entered_nether"] is True
    assert result["entry_target"] == target
    assert [body["pos"] for path, body in calls if path == "dig"] == [approach]
    assert sum(path == "control" for path, _ in calls) == 1
    assert not any(path in {"place", "inventory"} for path, _ in calls)


@pytest.mark.parametrize("mode,code", [("blocked", "portal_approach_blocked"), ("wrong_elevation", "portal_entry_not_reached"), ("no_transfer", "portal_transfer_timeout")])
def test_portal_entry_returns_recoverable_failure(monkeypatch, mode, code) -> None:
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    controls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            result = {"pos": [0.5, 70 if mode == "wrong_elevation" else 65, 2.5], "dimension": "overworld"}
        elif path == "find_blocks":
            result = {"blocks": [{"pos": [0, 65, 2]}]}
        elif path == "get_block":
            result = {"name": "nether_portal" if body["pos"] == [0, 65, 2] else "lava" if mode == "blocked" else "air"}
        elif path == "move_to":
            result = {}  # A successful response is not evidence of portal contact.
        elif path == "control":
            controls.append(body)
            result = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(wait_s=4)
    assert result["ok"] is False
    assert result["requires_replan"] is True
    assert result["error"]["code"] == code
    if mode == "no_transfer":
        assert result["error"]["contact_seen"] is True
        assert "服务器可能延迟" in result["error"]["message"]
    assert controls == []


def test_staircase_down_stops_before_digging_nether_portal() -> None:
    """r14: the head cell was a nether_portal, so all four directions failed undiggable."""
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 30.0, 0.5], "on_ground": True}
        elif path == "inventory":
            result = {"items": []}
        elif path == "get_block":
            result = {"name": "nether_portal" if body["pos"] == [0, 31, 0] else "air"}
        elif path == "find_blocks":
            result = {"blocks": []}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).dig_staircase(direction="north", vertical="down", steps=1)
    assert result["ok"] is False
    assert result["stop_reason"] == "undiggable_block"
    assert result["last_error"]["block"] == "nether_portal"
    assert "dig" not in calls


def test_stair_session_leaves_time_for_obsidian_dig(monkeypatch) -> None:
    """r14: an 8s dig cap made every fortress obsidian step time out."""
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    dig_timeouts = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        assert path == "dig"
        dig_timeouts.append(request.extensions["timeout"]["read"])
        clock[0] += 9.5
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        session = library._StairSession(bot, 45.0)
        session.call("dig", {"pos": [0, 0, 0]})
    assert session.per_step_timeout == pytest.approx(15.0)
    assert dig_timeouts[0] >= 9.5
    assert session.progress["dug_count"] == 1


@pytest.mark.parametrize("place_fails,code", [(True, "portal_frame_incomplete"), (False, "portal_ignition_failed")])
def test_portal_reports_construction_phase_failures(monkeypatch, place_fails, code) -> None:
    import mc_agent.skills.library as library

    monkeypatch.setattr(library.time, "sleep", lambda _: None)
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append(path)
        if path == "status":
            result = {"pos": [0.5, 64, 0.5], "dimension": "overworld"}
        elif path == "inventory":
            result = {"items": [{"name": "obsidian", "count": 10}, {"name": "flint_and_steel", "count": 1}, {"name": "cobblestone", "count": 12}]}
        elif path == "find_blocks":
            result = {"blocks": []}
        elif path == "get_block":
            result = {"name": "air"}
        elif path == "place" and body["item"] == "obsidian" and place_fails:
            return httpx.Response(200, json={"ok": False, "error": {"code": "place_failed", "message": "obstructed"}})
        elif path in {"place", "move_to", "interact"}:
            result = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(enter=False)
    assert result["ok"] is False
    assert result["error"]["code"] == code
    if place_fails:
        assert result["failed_cells"]
        assert "interact" not in calls


@pytest.mark.parametrize("scene", ["retry", "changed", "far", "fresh", "timeout", "no_position", "late_reconnect"])
def test_portal_recounts_the_same_frame_before_resuming_within_budget(monkeypatch, scene) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    origin = [9, 64, 22]
    position = [10.5, 64.0, 20.5]
    occupied = {}
    blocked = {(12, 66, 22), (12, 67, 22), (10, 68, 22), (11, 68, 22)}
    if scene in {"fresh", "timeout"}:
        blocked.clear()
    bag = {"obsidian": 10, "flint_and_steel": 1}
    actions = []
    lit = False
    timeout_pending = scene == "timeout"

    def handler(request):
        nonlocal lit, timeout_pending
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        actions.append((route, body))
        if route == "status":
            if scene == "late_reconnect":
                return httpx.Response(200, json={"ok": False, "error": {"code": "not_connected", "message": "offline fixture"}})
            result = {"pos": list(position) if scene != "no_position" else None, "dimension": "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": item, "count": n} for item, n in bag.items() if n]}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": [10, 65, 22], "name": "nether_portal"}] if lit else []}
        elif route == "get_block":
            pt = tuple(body["pos"])
            result = {"name": occupied.get(pt, "stone" if pt[1] == 63 else "air")}
        elif route == "dig":
            occupied.pop(tuple(body["pos"]), None)
            result = {}
        elif route == "move_to":
            result = {"pos": list(position)}
        elif route == "place":
            pt = tuple(body["pos"])
            assert body["item"] == "obsidian" and pt[2] == origin[2] and bag["obsidian"] > 0
            if pt in blocked:
                return httpx.Response(200, json={"ok": False, "error": {"code": "place_failed", "message": "blocked"}})
            assert pt not in occupied
            occupied[pt] = "obsidian"
            bag["obsidian"] -= 1
            if timeout_pending:
                timeout_pending = False
                clock[0] += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("placement reply lost", request=request)
            result = {"placed": "obsidian", "pos": list(pt)}
        elif route == "stop":
            result = {"released": True}
        elif route == "interact":
            assert body["item"] == "flint_and_steel" and len(occupied) == 10
            lit = True
            result = {}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        tools = McToolset(bot)
        if scene == "late_reconnect":
            tools._episode_deadline_mono = 9.0
            monkeypatch.setattr(tools, "_reconnect_once", lambda **_: clock.__setitem__(0, 6.0))
            result = json.loads(tools.mc_skill_build_portal(enter=False))
            assert result["error"]["code"] == "episode_deadline_imminent"
            assert [name for name, _ in actions] == ["status"]
            return
        if scene == "timeout":
            tools._episode_deadline_mono = 3.0
            imminent = json.loads(tools.mc_skill_build_portal(enter=False))
            assert imminent["error"]["code"] == "episode_deadline_imminent" and actions == []
            tools._episode_deadline_mono = 8.0
        first = json.loads(tools.mc_skill_build_portal(enter=False))
        if scene == "no_position":
            assert first["error"]["code"] == "portal_position_unverified"
            assert [name for name, _ in actions] == ["status", "find_blocks"]
            assert bag["obsidian"] == 10
            return
        assert first["portal_origin"] == origin and first["existing_obsidian"] == 0
        if scene == "fresh":
            assert first["ok"] and first["placed_obsidian"] == first["obsidian_consumed"] == 10
            assert bag["obsidian"] == 0
            return
        if scene == "timeout":
            assert first["error"]["code"] == "portal_time_budget" and first["action_released"] is True
            assert first["placed_obsidian"] == 0 and bag["obsidian"] == 9 and clock[0] <= 4
            assert [name for name, _ in actions if name in {"place", "interact", "stop"}] == ["place", "stop"]
            tools._episode_deadline_mono = None
        else:
            assert first["error"]["code"] == "portal_frame_incomplete"
            assert set(map(tuple, first["failed_cells"])) == blocked
            assert first["placed_obsidian"] == first["obsidian_consumed"] == 6 and bag["obsidian"] == 4
        if scene == "far":
            position[:] = [31.5, 64.0, 28.5]
            writes_before = len([name for name, _ in actions if name in {"dig", "place", "interact"}])
            distant = json.loads(tools.mc_skill_build_portal(enter=False))
            assert distant["error"] == "missing_materials" and distant["required_obsidian"] == 10
            assert distant["unfinished_portal_origin"] == origin
            assert len([name for name, _ in actions if name in {"dig", "place", "interact"}]) == writes_before

        blocked.clear()
        position[:] = [12.5, 64.0, 21.5]
        if scene == "changed":
            del occupied[(10, 64, 22)]
        first_writes = len([name for name, _ in actions if name in {"dig", "place", "interact"}])
        second = json.loads(tools.mc_skill_build_portal(enter=False))
        assert second["portal_origin"] == origin
        if scene == "changed":
            assert second["error"] == "missing_materials" and second["required_obsidian"] == 5
            assert second["existing_obsidian"] == 5 and bag["obsidian"] == 4
            assert len([name for name, _ in actions if name in {"dig", "place", "interact"}]) == first_writes
        else:
            expected = 9 if scene == "timeout" else 4
            assert second["ok"] and second["existing_obsidian"] == 10 - expected
            assert second["placed_obsidian"] == second["obsidian_consumed"] == expected
            assert bag["obsidian"] == 0 and len(occupied) == 10
            before_reuse = len(actions)
            reused = json.loads(tools.mc_skill_build_portal(enter=False))
            assert reused["ok"] and reused["reused_existing_portal"]
            assert [name for name, _ in actions[before_reuse:]] == ["status", "find_blocks"]


@pytest.mark.parametrize("hazard,target", [("lava", "nether_bricks"), ("magma_block", "blaze"), ("cave_air", "nether_bricks")])
def test_nether_scout_detours_unsafe_floor_and_stops_at_fortress_evidence(hazard, target) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    pos = [0.5, 64, 0.5]
    moves = []
    probes = []
    queries = []

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            hostile = {"type": "blaze", "dist": 4} if moves and target == "blaze" else None
            return _SurfaceScoutBackend.ok({
                "pos": pos, "dimension": "the_nether", "biome": "nether_wastes",
                "on_ground": True, "health": 20, "nearest_hostile": hostile,
            })
        if route == "get_block":
            x, y, z = body["pos"]
            name = "air" if y > 63 else "netherrack"
            if (x, y, z) == (4, 63, 0):
                name = hazard
                probes.append(name)
            return _SurfaceScoutBackend.ok({"name": name})
        if route in {"find_blocks", "get_entities"}:
            queries.append(body["types"])
            rows = [{"name": target, "pos": [1, 64, -23]}] if moves and target in body["types"] else []
            return _SurfaceScoutBackend.ok({"blocks" if route == "find_blocks" else "entities": rows})
        if route == "move_to":
            moves.append(body)
            pos[:] = [body["x"], body["y"], body["z"]]
            return _SurfaceScoutBackend.ok({"pos": pos})
        raise AssertionError(route)

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        registry = tools.registry(profile="compact", extra_tools=("mc_skill_scout_surface",))
        args = {"direction": "east"}
        if target == "blaze":
            args["target_types"] = ["netherrack", "blaze"]
        result = json.loads(registry.execute({
            "function": {"name": "mc_skill_scout_surface", "arguments": json.dumps(args)},
        }))

    assert probes == [hazard]
    assert len(moves) == 1
    assert moves[0]["x"] == 0.5 and moves[0]["z"] == -19.5 and moves[0]["y"] == 64
    assert result["stop_reason"] == "target_found"
    expect = result["detected_entities"] if target == "blaze" else result["detected_pois"]
    assert any(row["name"] == target for row in expect)
    assert all("netherrack" not in types for types in queries)


@pytest.mark.parametrize("magma_on_short_route", [False, True])
def test_nether_scout_falls_back_to_a_safe_short_hop(magma_on_short_route) -> None:
    pos = [0.5, 64, 0.5]
    moves = []

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            return _SurfaceScoutBackend.ok({
                "pos": pos, "dimension": "the_nether", "on_ground": True, "health": 20,
            })
        if route == "get_block":
            x, y, z = body["pos"]
            floor = "air"
            if y == 63 and 0 <= x <= 5 and z == 0:
                floor = "magma_block" if magma_on_short_route and x == 2 else "netherrack"
            return _SurfaceScoutBackend.ok({"name": floor})
        if route in {"find_blocks", "get_entities"}:
            return _SurfaceScoutBackend.ok({"blocks" if route == "find_blocks" else "entities": []})
        assert route == "move_to"
        moves.append(body)
        pos[:] = [body["x"], body["y"], body["z"]]
        return _SurfaceScoutBackend.ok({"pos": pos})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        receipt = MacroSkills(bot).scout_surface(direction="east", step_dist=16)

    if magma_on_short_route:
        assert moves == []
        assert receipt["stop_reason"] == "unsafe_nether_route"
    else:
        assert len(moves) == 1
        assert [moves[0][key] for key in ("x", "y", "z")] == [4.5, 64, 0.5]
        assert receipt["ok"] is True
        assert receipt["stop_reason"] == "short_nether_hop"
        assert receipt["distance_traveled"] == 4


@pytest.mark.parametrize("floor,reason", [("cave_air", "bridge_gap_ahead"), ("netherrack", "nether_tunnel_floor_unsupported")])
def test_nether_tunnel_reports_the_actual_floor_blocker(floor, reason) -> None:
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.harness.mc_tools import McToolset

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        responses = {
            "status": {
                "pos": [0.5, 64, 0.5], "dimension": "the_nether", "on_ground": True,
                "health": 20, "held": "stone_pickaxe",
            },
            "inventory": {"items": [{"name": "stone_pickaxe", "count": 1, "slot": 0}]},
            "get_block": {"name": floor},
        }
        return _SurfaceScoutBackend.ok(responses[route])

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        tools = McToolset(bot, geofence=VoidGeofence())
        result = json.loads(tools.mc_skill_tunnel(direction="east", steps=2))

    assert result["stop_reason"] == reason
    assert result["requires_replan"] is True
    assert result["blocker"] == {"block": floor, "pos": [1, 63, 0], "relation": "unsafe_floor"}
    assert result["steps_done"] == 0
    assert "改" in result["hint"]


@pytest.mark.parametrize("scene", [
    "far_lava_bridge", "place_no_effect", "place_uncertain", "reused_step", "replacement", "fresh_step",
    "stalled", "partial", "owned", "worn", "missing_gold",
])
def test_nether_gold_boots_use_supported_workbench_or_equipped_pair(scene) -> None:
    from mc_agent.harness.mc_tools import McToolset

    pos = [0.5, 64.0, 0.5]
    home_floor = 63
    local = [0, 64, 1]
    alternate = [-1, 64, 0]
    table = list(local) if scene in {"reused_step", "replacement"} else [12, 84, 0]
    added = None
    bag = {"gold_ingot": 3 if scene == "missing_gold" else 20, "crafting_table": 1}
    if scene == "owned":
        bag["golden_boots"] = 1
    armor = {"feet": {"name": "golden_boots"}} if scene == "worn" else {}
    actions = []
    craft_attempts = 0

    def handle(request):
        nonlocal added, pos, craft_attempts
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "inventory":
            result = {"items": [{"name": name, "count": count} for name, count in bag.items() if count], "armor": armor}
        elif route == "status":
            result = {"pos": pos, "health": 20, "dimension": "the_nether", "on_ground": True}
        elif route == "find_blocks":
            result = {"blocks": [{"pos": table, "dist": math.dist(pos, table)}]}
        elif route == "get_block":
            block = body["pos"]
            if block in (table, added):
                name = "crafting_table"
            elif block == [0, home_floor, -1]:
                name = "lava"
            else:
                name = "nether_bricks" if block[1] == home_floor else "air"
            result = {"name": name}
        elif route == "place":
            point = body["pos"]
            actions.append((route, point))
            assert point != [0, 64, -1] and bag["crafting_table"] == 1
            if scene == "far_lava_bridge" and point == local:
                return httpx.Response(200, json={"ok": False, "error": {"code": "no_support"}})
            if scene in {"place_no_effect", "place_uncertain"} and point == local:
                if scene == "place_uncertain":
                    bag["crafting_table"] -= 1
                return httpx.Response(200, json={"ok": False, "error": {"code": "place_failed"}})
            if scene == "replacement":
                assert point != table and math.dist(pos, [point[0] + 0.5, point[1], point[2] + 0.5]) <= 2.0
            else:
                assert point == (alternate if scene in {"far_lava_bridge", "place_no_effect"} else local)
            bag["crafting_table"] -= 1
            added = point
            result = {"pos": point}
        elif route == "move_to":
            actions.append((route, body))
            assert body["range"] == 0.4 and body["timeout"] == 1500 and body["stop_on"] == ["hurt", "death"]
            if scene != "stalled":
                pos = [body["x"], body["y"], body["z"]]
            result = {"pos": pos}
        elif route == "craft":
            actions.append((route, body))
            assert body == {"item": "golden_boots", "count": 1}
            craft_attempts += 1
            before_window = scene in {"reused_step", "fresh_step", "stalled", "partial"} and craft_attempts == 1
            if before_window or (scene == "replacement" and craft_attempts <= 2):
                error = {"code": "no_window", "table": added or table}
                if scene == "partial":
                    error["gained"] = {"golden_boots": 1}
                return httpx.Response(200, json={"ok": False, "error": error})
            assert added is not None or scene == "reused_step"
            bag["gold_ingot"] -= 4
            bag["golden_boots"] = 1
            result = {"crafted": "golden_boots", "gained": 1}
        elif route == "equip":
            actions.append((route, body))
            assert body == {"item": "golden_boots", "slot": "feet"}
            assert bag.pop("golden_boots") == 1
            armor["feet"] = {"name": "golden_boots"}
            result = {}
        else:
            raise AssertionError(route)  # Recovery must never dig the original table or its platform.
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        result = json.loads(McToolset(bot).mc_skill_craft_gear("golden_boots", equip_slot="feet" if scene == "owned" else None))

    expected = {
        "far_lava_bridge": ["place", "place", "craft", "equip"],
        "place_no_effect": ["place", "place", "craft", "equip"], "place_uncertain": ["place"],
        "reused_step": ["craft", "move_to", "craft", "equip"],
        "replacement": ["craft", "move_to", "craft", "place", "craft", "equip"],
        "fresh_step": ["place", "craft", "move_to", "craft", "equip"],
        "stalled": ["place", "craft", "move_to"], "partial": ["place", "craft"],
        "owned": ["equip"], "worn": [], "missing_gold": [],
    }
    assert [route for route, _ in actions] == expected[scene]
    if scene in {"stalled", "partial", "missing_gold", "place_uncertain"}:
        assert not result["ok"] and result["requires_replan"]
        if scene == "missing_gold":
            assert result["missing_materials"] == {"gold_ingot": 1}
        elif scene == "place_uncertain":
            assert result["error"] == "workbench_placement_unconfirmed" and bag["gold_ingot"] == 20
        else:
            assert result["error"] == "no_window" and bag["gold_ingot"] == 20
    else:
        assert result["ok"] and result["equipped_slot"] == "feet" and armor["feet"]["name"] == "golden_boots"
        assert bag["gold_ingot"] == (20 if scene in {"owned", "worn"} else 16)


@pytest.mark.parametrize("case,expected_error", [
    ("cast_and_collect", None), ("already_obsidian", None), ("adjacent_lava", None),
    ("flowing", "obsidian_cast_requires_exposed_source"),
    ("nether", "obsidian_cast_requires_overworld"),
    ("no_diamond_pick", "tool_tier_insufficient"),
    ("unsupported", "no_safe_water_support"),
    ("pour_unconfirmed", "water_placement_unconfirmed"),
    ("drifted", "water_recovery_deferred"),
    ("scoop_unconfirmed", "water_recovery_unconfirmed"),
    ("not_converted", "obsidian_conversion_unconfirmed"),
    ("pour_timeout", "obsidian_cast_time_budget"),
])
def test_obsidian_cast_confirms_source_recovers_water_and_hands_off_to_mining(monkeypatch, case, expected_error) -> None:
    from mc_agent.harness.mc_tools import McToolset

    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    lava, bank, water = (2, 64, 0), (1, 64, 0), (1, 65, 0)
    pos = [0.5, 66.0, 0.5]
    blocks = {lava: "obsidian" if case == "already_obsidian" else "lava", (0, 65, 0): "stone", (2, 63, 0): "stone"}
    if case != "unsupported":
        blocks[bank] = "stone"
    if case == "adjacent_lava":
        blocks[(3, 64, 0)] = "lava"
    bag = {"water_bucket": 1, "iron_pickaxe" if case == "no_diamond_pick" else "diamond_pickaxe": 1}
    actions = []

    def handle(request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if route == "status":
            current = [1.0, 66.0, 0.5] if case == "drifted" and water in blocks else pos
            result = {"pos": current, "on_ground": True, "dimension": "the_nether" if case == "nether" else "overworld"}
        elif route == "inventory":
            result = {"items": [{"name": name, "count": count} for name, count in bag.items() if count]}
        elif route == "get_block":
            target = tuple(body["pos"])
            name = blocks.get(target, "air")
            result = {"name": name, "properties": {"level": 3 if case == "flowing" else 0} if name in {"water", "lava"} else {}}
        elif route == "interact":
            actions.append((route, body))
            if body["item"] == "water_bucket":
                assert tuple(body["pos"]) == bank
                if case == "pour_timeout":
                    clock[0] += sum(request.extensions["timeout"].values())
                    raise httpx.ReadTimeout("offline pour reply lost", request=request)
                if case != "pour_unconfirmed":
                    bag["water_bucket"], bag["bucket"] = 0, 1
                    if case != "not_converted":
                        blocks[lava] = "obsidian"
                    blocks[water] = "water"
            else:
                assert body == {"pos": list(water), "item": "bucket"} and blocks[water] == "water"
                if case != "scoop_unconfirmed":
                    del blocks[water]
                    bag["water_bucket"], bag["bucket"] = 1, 0
            result = {"changed": True}
        elif route == "stop":
            actions.append((route, body))
            result = {"released": False}
        elif route == "equip":
            assert body == {"item": "diamond_pickaxe", "slot": "hand"}
            result = {}
        elif route == "find_blocks":
            result = {"blocks": [{"name": "obsidian", "pos": list(lava), "dist": 2.9}]} if blocks[lava] == "obsidian" else {"blocks": []}
        elif route == "dig":
            actions.append((route, body))
            assert case in {"cast_and_collect", "already_obsidian"} and tuple(body["pos"]) == lava
            assert blocks.pop(lava) == "obsidian" and water not in blocks
            bag["obsidian"] = 1
            result = {"dug": "obsidian", "gained": {"obsidian": 1}}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handle)) as bot:
        registry = McToolset(bot).registry(profile="compact", extra_tools=("mc_skill_build_portal",))
        call = {"name": "mc_skill_cast_obsidian", "arguments": json.dumps(dict(zip("xyz", lava)))}
        reply = json.loads(registry.execute({"function": call}))
        assert reply["ok"] is (expected_error is None)
        if expected_error is not None:
            assert reply["error"] == expected_error
        elif case == "adjacent_lava":
            assert reply["obsidian_confirmed"] and reply["water_recovered"]
            assert not reply["mine_ready"] and "next_actions" not in reply
        else:
            assert reply["mine_ready"] and reply["obsidian_confirmed"]
            assert reply.get("water_recovered", False) is (case == "cast_and_collect")
            action, = reply["next_actions"]
            action["arguments"] = json.dumps(action["arguments"])
            collected = json.loads(registry.execute({"function": action}))
            assert collected["ok"] and collected["target_gained"] == {"obsidian": 1} and bag["obsidian"] == 1

    verbs = [route for route, _ in actions]
    if case in {"cast_and_collect", "adjacent_lava", "not_converted"}:
        assert verbs[:2] == ["interact", "interact"] and bag["water_bucket"] == 1
    elif case == "already_obsidian":
        assert verbs == ["dig"]
    elif case in {"pour_unconfirmed", "drifted"}:
        assert verbs == ["interact"]
    elif case == "scoop_unconfirmed":
        assert verbs == ["interact", "interact"] and not reply["water_recovered"]
    elif case == "pour_timeout":
        assert verbs == ["interact", "stop"] and reply["stop_confirmed"] is False
    else:
        assert not actions
    assert clock[0] <= 25.0


def test_find_blocks_uses_nether_stems_for_generic_log() -> None:
    from mc_agent.harness.mc_tools import McToolset

    queries = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        assert path == "find_blocks"
        queries.append(body["types"])
        return httpx.Response(200, json={"ok": True, "result": {"total": 0, "blocks": []}})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0, 64, 0])
        toolset._last_dimension = "the_nether"
        result = json.loads(toolset.mc_find_blocks(types=["log", "planks", "stone"]))
    assert result["ok"] is True
    assert queries == [["crimson_stem", "warped_stem", "crimson_planks", "warped_planks", "stone"]]


@pytest.mark.parametrize("floor", ["stone", "water"])
def test_portal_entry_builds_one_safe_step_from_two_blocks_below(monkeypatch, floor) -> None:
    import mc_agent.skills.library as library

    clock = [0.0]
    monkeypatch.setattr(library.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(library.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    pos = [0.5, 64.0, 1.5]
    contact = [None]
    placed = []
    jumping = [False]

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if path == "status":
            nether = contact[0] is not None and clock[0] - contact[0] >= 4
            result = {"pos": list(pos), "dimension": "the_nether" if nether else "overworld", "alive": True, "ready": True, "on_ground": not jumping[0]}
        elif path == "find_blocks":
            result = {"blocks": [{"pos": [0, 66, 2], "name": "nether_portal"}]}
        elif path == "get_block":
            result = {"name": "nether_portal", "properties": {"axis": "x"}} if body["pos"] == [0, 66, 2] else {"name": floor if body["pos"][1] <= 63 else "air"}
        elif path == "inventory":
            result = {"items": [{"name": "cobblestone", "count": 8}]}
        elif path == "posture":
            jumping[0] = body["jump"]
            pos[1] = 65.1 if body["jump"] else 65.0
            result = {}
        elif path == "place":
            placed.append(body)
            result = {"pos": body["pos"]}
        elif path == "control":
            assert body["jump"] is True
            pos[:] = [0.5, 66.0, 2.5]
            contact[0] = clock[0]
            result = {"pos": list(pos)}
        elif path in {"move_to", "look"}:
            result = {"pos": list(pos)}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("offline", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).build_nether_portal(wait_s=8)
    if floor == "stone":
        assert result["ok"] is True
        assert result["entered_nether"] is True
        assert result["entry_recovery"]["steps_done"] == 1
        assert placed == [{"item": "cobblestone", "pos": [0, 64, 1]}]
    else:
        assert result["ok"] is False
        assert result["error"]["code"] == "portal_approach_unsupported"
        assert placed == []
        assert contact[0] is None
    assert clock[0] <= 8

