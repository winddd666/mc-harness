from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

@pytest.mark.parametrize("archived", [False, True])
def test_climb_out_does_not_walk_to_a_collar_below_the_bot(monkeypatch, archived) -> None:
    world = _MockMineWorld(start_pos=(0.5, 21.0, 0.5), surface_y=53)
    steps = []
    with BotSession("tok", bot_username="ClimbPastLowerCollar", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 15, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 53
        if archived:
            engine._archive_current_shaft_to_ledger(mark_exhausted=True)
            engine._clear_active_tree_only()

        def navigate_down(*args, **kwargs):
            pytest.fail("climb_out navigated toward a collar below the starting position")

        def climb_step(**kwargs):
            steps.append(world.pos[1])
            world.pos[1] += 1
            return {"steps_done": 1}

        monkeypatch.setattr(engine, "_navigate_to_node", navigate_down)
        monkeypatch.setattr(engine.skills, "dig_staircase", climb_step)
        result = engine.run(action="climb_out", max_duration_s=32)

    assert result["stop_reason"] == "surface_reached"
    assert result["climb"]["start_y"] == 21
    assert result["climb"]["end_y"] == 35
    assert steps == list(range(21, 35))


def test_climb_out_skips_a_deeper_rejoin_when_off_the_recorded_route(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 25.0, 0.5), surface_y=74)
    lower_anchor = (0, 17, 1)
    move_goals = []
    climb_starts = []

    with BotSession("tok", bot_username="ClimbAboveOldTunnel", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(60, 74, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="spine", anchor=lower_anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "west", [collar.anchor, lower_anchor]), branches=[],
        )

        def move_exact(cell, **kwargs):
            move_goals.append(cell)
            if cell != lower_anchor:
                return False
            x, y, z = cell
            world.pos[:] = [x + 0.5, float(y), z + 0.5]
            return True

        def climb_step(**kwargs):
            climb_starts.append(world.pos[1])
            world.pos[1] += 1
            return {"steps_done": 1}

        monkeypatch.setattr(engine, "_move_exact", move_exact)
        monkeypatch.setattr(engine, "_hop_up_to", lambda *args, **kwargs: False)
        monkeypatch.setattr(engine.skills, "dig_staircase", climb_step)
        result = engine.run(action="climb_out", max_duration_s=32)

    assert lower_anchor not in move_goals
    assert climb_starts and climb_starts[0] == 25
    assert result["stop_reason"] == "surface_reached"


def test_climb_out_honors_the_callers_budget_in_nested_staircase(monkeypatch) -> None:
    """A 5s climb must never turn into the old implicit 90s staircase budget."""
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    requests: list[tuple[float, str, dict]] = []

    def slow_handler(request):
        timeout = request.extensions["timeout"]
        requests.append((clock.now, request.url.path, timeout))
        clock.now += 0.25
        return world.handler(request)

    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    with BotSession("tok", bot_username="TimedClimb", transport=httpx.MockTransport(slow_handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        eng.collar_ceiling_y = 64
        original_call = bot.call
        result = eng.run(action="climb_out", max_duration_s=5.0)
        assert eng.bot is bot and eng.skills is skills
        assert skills.bot is bot and bot.call == original_call
    assert clock.now <= 1005.0
    assert result["stop_reason"] == "time_slice"
    assert requests
    for started, _, timeouts in requests:
        assert 0 < timeouts["read"] <= 1005.0 - started
    # No quarantine reconnect, background retry, or new action starts after cutoff.
    assert all(started < 1005.0 for started, _, _ in requests)
    assert not any(path.endswith(("/connect", "/disconnect")) for _, path, _ in requests)


def test_climb_timeout_with_confirmed_ascent_can_resume(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=40)
    with BotSession("tok", bot_username="TimedAscent", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 41

        def climb_step(*, deadline, **kwargs):
            world.pos[1] += 1
            engine.bot.status()
            if world.pos[1] == 21:
                clock.now = deadline
            return {"steps_done": 1}

        monkeypatch.setattr(engine.skills, "dig_staircase", climb_step)
        first = engine.run(action="climb_out", max_duration_s=5.0)
        second = engine.run(action="climb_out", max_duration_s=5.0)

    assert first["stop_reason"] == "climb_progress"
    assert first["next_action"] == "climb_out"
    assert first["climb"]["end_y"] == second["climb"]["start_y"] == 21
    assert second["stop_reason"] == "surface_reached"


@pytest.mark.parametrize("landing", ["waypoint", "off_route", "stationary"])
def test_climb_timeout_counts_only_confirmed_advance_on_the_recorded_route(monkeypatch, landing) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    height, start_x = 15, 10
    world = _MockMineWorld(start_pos=(start_x + 0.5, float(height), 0.5), surface_y=64)
    floor_route = [(x, height, 0) for x in range(start_x + 1)]
    stair_route = [(0, y, height - y) for y in range(64, height - 1, -1)]
    for x, y, z in [*floor_route, *stair_route, (start_x, height, 1)]:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"

    with BotSession("tok", bot_username="SameLevelRetreat", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=stair_route[0], parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 64
        junction = engine._create_node(kind="spine", anchor=floor_route[0], parent_id=collar.node_id,
                                       edge_from_parent=ShaftEdge("stair_down", "south", stair_route), branches=[])
        engine._create_node(kind="spine", anchor=floor_route[-1], parent_id=junction.node_id,
                            edge_from_parent=ShaftEdge("spine_forward", "east", floor_route), branches=[])
        engine._archive_current_shaft_to_ledger(mark_exhausted=True)
        engine._clear_active_tree_only()
        actual_step = engine._step_to

        def walk_in_slice(cell, **kwargs):
            if landing == "waypoint":
                reached = actual_step(cell, **kwargs)
                clock.now += 2
                return reached
            if landing == "off_route":
                assert actual_step((start_x, height, 1), **kwargs)
            clock.now = engine._deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_step_to", walk_in_slice)
        first = engine.run(action="climb_out", max_duration_s=6.0)
        if landing == "waypoint":
            second = engine.run(action="climb_out", max_duration_s=6.0)
            assert first["pos_before"][0] > first["pos_after"][0] == second["pos_before"][0] > second["pos_after"][0]
            for result in (first, second):
                assert result["ok"] is True and result["requires_replan"] is True
                assert result["stop_reason"] == "climb_progress"
                assert result["next_action"] == "climb_out"
                assert result["climb"]["start_y"] == result["climb"]["end_y"] == height
                assert result["climb"]["reached"] is False
                assert result["climb"]["recorded_route_progress"] is True
        else:
            assert first["stop_reason"] == "time_slice"
            assert first["ok"] is False
            assert not first["climb"].get("recorded_route_progress")

    assert not world.dig_log
    assert engine.ledger.saved_shafts[0]["retired"] is True


@pytest.mark.parametrize("times_out", [False, True])
def test_grounded_climb_progress_does_not_count_as_a_repeated_failure(monkeypatch, times_out) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.tool_registry import Tool
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 61.0, 0.5), surface_y=85)

    with BotSession("tok", bot_username="GroundedClimbContinuation", transport=httpx.MockTransport(world.handler)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0, 61, 0])
        engine = toolset.skills.mining_dfs
        engine.collar_ceiling_y = 85
        if not times_out:
            climb_out = engine._climb_out
            monkeypatch.setattr(engine, "_climb_out", lambda *, deadline: climb_out(deadline=deadline, max_attempts=1))

        def climb_step(*, deadline, **kwargs):
            if clock.now + 10.0 >= deadline:
                clock.now = deadline
                raise mining._MiningDeadline
            world.pos[1] += 1
            clock.now += 10.0
            return {"ok": True, "steps_done": 1}

        monkeypatch.setattr(engine.skills, "dig_staircase", climb_step)
        guard = _RepeatFailureGuard(toolset)
        tool = guard.wrap_tool(Tool(
            name="mc_skill_mine_dfs", description="Mine", parameters={}, handler=toolset.mc_skill_mine_dfs,
        ))
        first = json.loads(tool.handler(action="climb_out", max_duration_s=20))
        second = json.loads(tool.handler(action="climb_out", max_duration_s=20))
        assert not guard._failed_sigs

        def blocked(**kwargs):
            return {"ok": False, "steps_done": 0}

        monkeypatch.setattr(engine.skills, "dig_staircase", blocked)
        monkeypatch.setattr(engine.skills, "dig_level_tunnel", blocked)
        stalled = json.loads(tool.handler(action="climb_out", max_duration_s=20))
        rejected = json.loads(tool.handler(action="climb_out", max_duration_s=20))

    assert not world.dig_log
    assert [r.get("stop_reason") for r in (first, second)] == ["climb_progress", "climb_progress"]
    for result, start_y in ((first, 61), (second, 62)):
        assert result["ok"] is True and result["requires_replan"] is True
        assert result["climb"]["reached"] is False and result["climb"]["climbed"] == 1
        assert result["pos_before"][1] == start_y
        assert result["pos_after"][1] == start_y + 1
    assert stalled["stop_reason"] == "climb_blocked" and stalled["ok"] is False
    assert rejected["error"]["code"] == "repeated_identical_failure_blocked"


def test_climb_timeout_suggests_enough_budget_for_a_long_remaining_ascent(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 48.0, 0.5), surface_y=91)
    with BotSession("tok", bot_username="ClimbBudgetHint", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 91

        def climb_step(*, deadline, **kwargs):
            if clock.now + 4.7 >= deadline:
                clock.now = deadline
                raise mining._MiningDeadline
            world.pos[1] += 1
            clock.now += 4.7
            engine.bot.status()
            return {"steps_done": 1}

        monkeypatch.setattr(engine.skills, "dig_staircase", climb_step)
        first = engine.run(action="climb_out", max_duration_s=32)
        assert first["stop_reason"] == "climb_progress"
        assert first["climb"]["end_y"] == 54
        assert clock.now <= 1032

        retry_with = first["suggested_next"]["retry_with"]
        assert retry_with["max_duration_s"] == 55
        second = engine.run(action="climb_out", **retry_with)
        third = engine.run(action="climb_out", **retry_with)

    assert second["stop_reason"] == "climb_progress"
    assert second["climb"]["end_y"] == 65
    assert third["stop_reason"] == "surface_reached"
    assert third["climb"]["end_y"] == 73


def test_climb_out_confirms_landing_before_reporting_arrival(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 36.0, 0.5), surface_y=55, dimension="the_nether")
    world.blocks[(1, 38, 0)] = world.blocks[(1, 39, 0)] = "air"

    def handler(request):
        response = world.handler(request)
        if request.url.path.endswith("/status") and world.pos[1] == 38.4:
            payload = response.json()
            payload["result"]["on_ground"] = False
            return httpx.Response(200, json=payload)
        return response

    def step(**kwargs):
        world.pos = [1.5, 38.4, 0.5]
        return {"ok": True}

    with BotSession("tok", bot_username="ConfirmClimbLanding", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 55
        monkeypatch.setattr(engine.skills, "dig_staircase", step)
        airborne = engine.run(action="climb_out", max_duration_s=5)
        assert airborne["ok"] is False
        assert airborne["stop_reason"] == "climb_progress"
        assert airborne["climb"]["target_y"] == 39
        assert airborne["climb"]["reached"] is False

        world.pos[1] = 38.0
        landed = engine.run(action="climb_out", max_duration_s=5)

    assert landed["stop_reason"] == "surface_reached"
    assert landed["climb"]["reached"] is True


def test_climb_timeout_keeps_success_when_the_collar_was_already_reached(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 22.0, 0.5), surface_y=41)
    with BotSession("tok", bot_username="TimedButReached", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 41, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]

        def reach_collar(goal, *, deadline, **kwargs):
            world.pos[1] = 41.0
            engine.bot.status()
            clock.now = deadline
            return True, None

        monkeypatch.setattr(engine, "_navigate_to_node", reach_collar)
        result = engine.run(action="climb_out", max_duration_s=5)

    assert result["climb"]["reached"] is True
    assert result["stop_reason"] == "surface_reached"
    assert result["ok"] is True
    assert result["requires_replan"] is False
    assert "suggested_next" not in result


@pytest.mark.parametrize("interrupted", [False, True])
def test_climb_does_not_report_arrival_below_its_starting_height(monkeypatch, interrupted) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 68.0, 0.5), surface_y=81)
    with BotSession("tok", bot_username="RetainClimbHeight", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 81, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]

        def blocked_walk(collar_id, *, deadline, **kwargs):
            world.pos[1] = 67.0
            if interrupted:
                engine.bot.status()
                clock.now = deadline
                raise mining._MiningDeadline
            return False, "backtrack_blocked"

        def restore_height(**kwargs):
            world.pos[1] += 1
            return {"steps_done": 1}

        monkeypatch.setattr(engine, "_walk_to_collar", blocked_walk)
        monkeypatch.setattr(engine.skills, "dig_staircase", restore_height)
        result = engine.run(action="climb_out", max_duration_s=20)

    assert result["climb"]["target_y"] == 65
    if interrupted:
        assert result["stop_reason"] == "time_slice"
        assert result["climb"]["reached"] is False
        assert result["climb"]["end_y"] == 67
    else:
        assert result["stop_reason"] == "surface_reached"
        assert result["climb"]["reached"] is True
        assert result["climb"]["end_y"] == 68
    assert result["climb"]["minimum_y"] == 68


@pytest.mark.parametrize("slice_ends_on_step", [False, True])
def test_climb_receipt_reports_local_height_below_recorded_collar(monkeypatch, slice_ends_on_step) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 22.0, 0.5), surface_y=60)
    with BotSession("tok", bot_username="ClimbLocalHeight", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 41

        def take_step(*, deadline, **kwargs):
            world.pos[1] += 1
            if slice_ends_on_step:
                engine.bot.status()
                clock.now = deadline
                raise mining._MiningDeadline
            return {"steps_done": 1}

        monkeypatch.setattr(engine.skills, "dig_staircase", take_step)
        result = engine.run(action="climb_out", max_duration_s=20)

    assert result["ok"] is True
    assert result["stop_reason"] == "surface_reached"
    assert result["climb"]["reached"] is True
    assert result["climb"]["end_y"] == 23
    assert result["climb"]["target_y"] == 25
    assert result["climb"]["minimum_y"] == 23
    assert result["climb"]["recorded_collar_y"] == 41
    assert result["pos_before"] == [0.5, 22.0, 0.5]
    assert result["pos_after"] == [0.5, 23.0, 0.5]
    assert "已达到本轮回撤高度" in result["message"]
    assert "最低 y=23" in result["message"]
    assert "已爬回地表" not in result["message"]
    assert "mc_skill_staircase(vertical='up')" in result["message"]


@pytest.mark.parametrize(("fallback", "start_z", "expected_direction", "expected_steps"), [
    ("stair", -9.5, "south", 1),
    ("stair", -11.5, "south", 1),
    ("tunnel", -7.5, "north", 2),
    ("tunnel", -11.5, "south", 1),
])
def test_climb_construction_respects_fence_and_allows_inward_recovery(
    monkeypatch, fallback, start_z, expected_direction, expected_steps,
) -> None:
    from mc_agent.skills.mining_dfs import DIRECTIONS

    world = _MockMineWorld(start_pos=(0.5, 22.0, start_z), surface_y=60)
    fence = VoidGeofence(center_x=0.5, center_z=0.5, radius=15.0, margin=5.0)
    moves = []
    attempted = []

    def advance(direction, steps, rise):
        dx, dz = DIRECTIONS[direction]
        world.pos[0] += dx * steps
        world.pos[1] += rise
        world.pos[2] += dz * steps
        moves.append((direction, steps))
        return {"steps_done": steps}

    def staircase(*, direction, steps, vertical, **kwargs):
        attempted.append((direction, vertical))
        if vertical == "pillar" or (fallback == "tunnel" and not moves):
            return {"steps_done": 0}
        return advance(direction, steps, 1)

    def tunnel(*, direction, steps, **kwargs):
        return advance(direction, steps, 0)

    with BotSession("tok", bot_username="ClimbFence", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 41
        monkeypatch.setattr(engine.skills, "dig_staircase", staircase)
        monkeypatch.setattr(engine.skills, "dig_level_tunnel", tunnel)
        result = engine.run(action="climb_out", geofence=fence, max_duration_s=20)

    expected_moves = [(expected_direction, expected_steps)]
    if fallback == "tunnel" or start_z < -9.5:
        expected_moves.append(("south", 1))
    assert moves == expected_moves
    assert result["climb"]["end_y"] == 23
    assert fence.distance_from_center(world.pos[0], world.pos[2]) <= max(
        fence.safe_radius, fence.distance_from_center(0.5, start_z) - 1,
    )
    if start_z < -9.5:
        assert ("pillar", "pillar") not in attempted


@pytest.mark.parametrize("route", ["flat", "timeout", "unsupported"])
def test_climb_at_target_height_returns_inside_fence_at_same_level(monkeypatch, route) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 23.0, -12.5), surface_y=23)
    fence = VoidGeofence(center_x=0.5, center_z=0.5, radius=15.0, margin=5.0)
    if route == "unsupported":
        world.blocks[(0, 22, -12)] = "air"
    destinations = []

    def handler(request):
        if request.url.path.endswith("/move_to"):
            body = json.loads(request.content)
            destinations.append((body["x"], body["y"], body["z"]))
            if route == "timeout" and len(destinations) == 2:
                clock.now += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("second return step stalled", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="ReturnInsideFence", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 41
        args = dict(action="climb_out", geofence=fence, max_duration_s=20)
        result = engine.run(**args)
        if route == "timeout":
            assert result["stop_reason"] == "climb_progress"
            assert result["next_action"] == "climb_out"
            assert result["climb"]["climbed"] == 0
            assert result["climb"]["geofence"]["end_distance"] == 12
            assert clock.now < 1020
            result = engine.run(**args)

    assert result["climb"]["end_y"] == 23
    if route == "unsupported":
        assert result["stop_reason"] == "climb_blocked"
        assert not destinations and not world.dig_log
        assert result["climb"]["geofence"]["end_distance"] == 13
    else:
        assert result["stop_reason"] == "surface_reached"
        assert fence.distance_from_center(world.pos[0], world.pos[2]) == fence.safe_radius
        assert all(x == 0.5 and y == 23 for x, y, _ in destinations)


def test_climb_walks_open_stairs_when_pathfinder_would_time_out(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", lambda duration: setattr(clock, "now", clock.now + duration))
    world = _MockMineWorld(start_pos=(0.5, 12.0, 0.5), surface_y=30,
                           inventory_items=[{"name": "cobblestone", "count": 32, "slot": 9}])
    for x in range(19):
        world.blocks[(x, x + 12, 0)] = world.blocks[(x, x + 13, 0)] = "air"

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "move_to":
            body = json.loads(request.content)
            clock.now += body["timeout"] / 1000
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "stair"}})
        if path == "control":
            body = json.loads(request.content)
            clock.now += body["ms"] / 1000
            if body.get("jump") and body["ms"] >= 500:
                world.pos[0] += 1
                world.pos[1] += 1
        if path == "stop":
            return httpx.Response(200, json={"ok": True, "result": {}})
        return world.handler(request)

    with BotSession("tok", bot_username="FastClimb", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        route = [(x, x + 12, 0) for x in (*range(18, 1, -2), 1)]
        collar = engine._create_node(kind="collar", anchor=route[0], parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(kind="switchback", anchor=route[-1], parent_id=collar.node_id,
                            edge_from_parent=ShaftEdge("stair_down", "west", route), branches=[])

        result = engine.run(action="climb_out", max_duration_s=18)

    assert result["stop_reason"] == "surface_reached"
    assert result["climb"]["method"] == "walk"
    assert result["climb"]["end_y"] == 30
    assert clock.now - 1000.0 < 15
    assert not world.dig_log


def test_climb_does_not_retry_the_blocked_collar_from_the_ledger(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="BlockedRecordedClimb", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        tip = (0, 20, 0)
        engine._create_node(kind="switchback", anchor=tip, parent_id=collar.node_id,
                            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, tip]),
                            branches=[])
        engine._archive_current_shaft_to_ledger()
        attempted = []

        def blocked_route(goal, **kwargs):
            attempted.append(goal)
            return False, "backtrack_blocked"

        def alternate_stair(**kwargs):
            world.pos[1] = 64.0
            return {"steps_done": 1}

        monkeypatch.setattr(engine, "_navigate_to_node", blocked_route)
        monkeypatch.setattr(engine.skills, "dig_staircase", alternate_stair)
        result = engine.run(action="climb_out", max_duration_s=15)

    assert result["stop_reason"] == "surface_reached"
    assert attempted == [collar.node_id]


def test_climb_can_restore_a_retired_exhausted_route_from_the_bottom(monkeypatch) -> None:
    """A deep bot must match the archived route, even 44 blocks below its collar."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="DeepRetired", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng._create_node(kind="spine", anchor=(0, 20, 0), parent_id=collar.node_id,
                         edge_from_parent=ShaftEdge("stair_down", "north", [(0, 64, 0), (0, 20, 0)]),
                         branches=[])
        eng.collar_ceiling_y = 64
        eng._archive_current_shaft_to_ledger(mark_exhausted=True)
        eng._clear_active_tree_only()
        stairs: list[dict] = []

        def nav(goal_node_id, **kwargs):
            if goal_node_id == eng.collar_node_id:
                world.pos[1] = 64.0
            return True, None

        monkeypatch.setattr(eng, "_navigate_to_node", nav)
        monkeypatch.setattr(eng.skills, "dig_staircase", lambda **kw: stairs.append(kw))
        result = eng.run(action="climb_out", max_duration_s=5.0)
    assert result["climb"]["method"] == "walk"
    assert result["climb"]["reached"] is True
    assert not stairs


@pytest.mark.parametrize("finish_at_deadline", [False, True])
def test_multi_slice_climb_detaches_retired_shaft_but_keeps_its_return_route(monkeypatch, finish_at_deadline) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="RetiredReturn", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=(0, 20, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, (0, 20, 0)]),
            branches=[mining.BranchSpec("stair_down", "north", 34)],
        )
        engine._archive_current_shaft_to_ledger(mark_exhausted=True)
        engine._clear_active_tree_only()
        goals = []

        def walk(goal_node_id, *, deadline=None, **kwargs):
            goals.append(goal_node_id)
            assert goal_node_id == collar.node_id
            world.pos[1] = 32.0 if len(goals) == 1 else 64.0
            engine.bot.status()
            if len(goals) == 1 or finish_at_deadline:
                clock.now = deadline
                engine.bot.status()
            return True, None

        monkeypatch.setattr(engine, "_navigate_to_node", walk)
        first = engine.run(action="climb_out", max_duration_s=20)
        assert first["stop_reason"] == "climb_progress"
        assert first["tree"]["node_count"] == 2

        if finish_at_deadline:
            rejected = engine.run(target="iron", target_y=-54, action="explore", max_duration_s=20)
            assert rejected["stop_reason"] == "needs_surface_climb"
            assert rejected["next_action"] == "climb_out"
            assert rejected["tree"]["node_count"] == 2

        second = engine.run(action="climb_out", max_duration_s=20)
        assert second["stop_reason"] == "surface_reached"
        assert second["tree"]["node_count"] == 0
        assert engine.collar_node_id is None
        assert engine.collar_ceiling_y == 64
        assert engine.ledger.saved_shafts[0]["retired"] is True
        assert len(engine.ledger.saved_shafts[0]["nodes"]) == 2

    assert goals == [collar.node_id, collar.node_id]
    assert not world.dig_log


def test_final_stair_movement_failure_tries_level_sidestep_before_returning_to_collar(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    anchor = (0, 18, 0)
    world = _MockMineWorld(start_pos=(0.5, 18.0, 0.5), surface_y=64)
    route = [(0, y, 18 - y) for y in range(50, 17, -1)]
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(3, 18, 0)] = "iron_ore"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    def handler(request):
        if request.url.path.endswith("/move_to"):
            body = json.loads(request.content)
            if (math.floor(body["x"]), math.floor(body["y"]), math.floor(body["z"])) == (0, 17, 1):
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "stair blocked"}})
        return world.handler(request)

    with BotSession("tok", bot_username="FinalStairOffset", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=route[0], parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("stair_down", "west", 34)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 68
        blocked = BranchSpec("stair_down", "south", 5, branch_target_y=16)
        tip = engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", route), branches=[blocked],
        )
        walk = engine._walk_waypoints

        def walk_at_depth(points, **kwargs):
            assert max(y for _, y, _ in points) <= 18
            return walk(points, **kwargs)

        monkeypatch.setattr(engine, "_walk_waypoints", walk_at_depth)
        result = engine.run(target="iron", target_y=16, goal_count=1, strategy="radar")

    assert blocked.prune_reason == "movement_not_confirmed"
    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert result["pos_after"][1] == 18
    assert engine.collar_node_id == collar.node_id
    assert any(branch.kind == "offset_retry" for branch in tip.branches)

