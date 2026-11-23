from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

@pytest.mark.parametrize("interrupted", [False, True])
def test_dfs_reports_accumulated_inventory_alongside_slice_quota(monkeypatch, interrupted) -> None:
    from mc_agent.skills.mining_dfs import _MiningDeadline

    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        inventory_items=[{"name": "diamond", "count": 1, "slot": 9}],
    )
    slice_gains = iter((1, 2))
    with BotSession("tok", bot_username="InventoryGoal", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def mine_slice(**kwargs):
            engine.bot.status()
            engine.bot.inventory()
            gained = next(slice_gains)
            world._add_item("diamond", gained)
            engine.bot.inventory()
            if interrupted:
                raise _MiningDeadline
            return {"stop_reason": "time_slice_progress", "target_gained": gained}

        monkeypatch.setattr(engine, "_run", mine_slice)
        first = engine.run(target="diamond", goal_count=4, target_y=-54)
        second = engine.run(target="diamond", goal_count=4, target_y=-54)

    assert first["target_gained"] == 1
    assert second["target_gained"] == 2
    assert not first["goal_met"] and not second["goal_met"]
    assert first["target_inventory_count"] == 2
    assert first["target_inventory_meets_goal"] is False
    assert second["target_inventory_items"] == {"diamond": 4}
    assert second["target_inventory_count"] == 4
    assert second["target_inventory_meets_goal"] is True


@pytest.mark.parametrize("endpoint", ["get_entities", "move_to"])
@pytest.mark.parametrize("retry_error", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_target_drop_protocol_retry_respects_pickup_budget(monkeypatch, endpoint, retry_error) -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 16.0, 0.5),
        surface_y=64,
        inventory_items=[{"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]}],
    )
    for z in range(3):
        world.blocks[(0, 16, z)] = world.blocks[(0, 17, z)] = "air"
    if endpoint == "move_to":
        world.entities.append({"id": 79, "name": "item", "item": "raw_ironx1", "pos": [0.5, 16.2, 2.5]})
    world.blocks[(1, 16, 0)] = "iron_ore"
    clock = [100.0]
    request_count = 0
    pickup_started = None
    pickup_finished = None
    stop_attempts = 0

    def advance(seconds):
        clock[0] += seconds

    def handler(request):
        nonlocal request_count, pickup_started, pickup_finished, stop_attempts
        if request.url.path.endswith("/stop"):
            stop_attempts += 1
            advance(sum(request.extensions["timeout"].values()))
            if stop_attempts == 1:
                raise httpx.RemoteProtocolError("closed stop connection", request=request)
            pickup_finished = clock[0]
            return httpx.Response(200, json={"ok": True, "result": {"released": True}})
        if (
            request.url.path.endswith(f"/{endpoint}")
            and world._count("raw_iron")
            and request_count < 2
            and (endpoint == "move_to" or json.loads(request.content).get("types") == ["item"])
        ):
            request_count += 1
            if pickup_started is None:
                pickup_started = clock[0]
            advance(sum(request.extensions["timeout"].values()))
            if request_count == 1:
                raise httpx.RemoteProtocolError("closed keep-alive", request=request)
            pickup_finished = clock[0]
            raise retry_error("pickup response missing after retry", request=request)
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", advance)
    with BotSession("tok", bot_username="PickupBudget", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", target_y=16, goal_count=1, max_duration_s=30)

    assert request_count == 2
    assert pickup_finished - pickup_started <= 3.0
    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert result.get("timeout_path") == ("move_to" if endpoint == "move_to" else None)
    assert stop_attempts == (2 if endpoint == "move_to" else 0)


@pytest.mark.parametrize("last_observation", ["inventory", "craft_reply", "lost_reply"])
def test_dfs_timeout_receipt_keeps_confirmed_crafts_from_this_slice(monkeypatch, last_observation) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 45.0, 0.5), surface_y=45, inventory_items=[
        {"name": "wooden_pickaxe", "count": 1, "slot": 9},
        {"name": "cobblestone", "count": 6, "slot": 10},
        {"name": "stick", "count": 4, "slot": 11},
        {"name": "crafting_table", "count": 1, "slot": 12},
    ])
    world.blocks[(1, 45, 0)] = "crafting_table"

    def handler(request: httpx.Request) -> httpx.Response:
        response = world.handler(request)
        if (
            last_observation != "inventory" and request.url.path.endswith("/craft")
            and response.json().get("result", {}).get("crafted") == "stone_pickaxe"
        ):
            clock.now = engine._deadline
            if last_observation == "lost_reply":
                raise httpx.ReadTimeout("craft reply lost", request=request)
        return response

    with BotSession("tok", bot_username="CraftTimeout", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.recrafted_picks = 2

        def interrupted(*, deadline, **kwargs):
            engine.bot.status()
            counts = mining._inventory_counts(engine.bot)
            assert engine._try_recraft_burner_pickaxe(counts)
            clock.now = deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_run", interrupted)
        result = engine.run(target="iron", goal_count=1)

    assert world.craft_log.count(("stone_pickaxe", 1)) == 1
    if last_observation == "inventory":
        assert result["stop_reason"] == "time_slice_progress"
        assert result["recrafted_picks"] == result["gained"]["stone_pickaxe"] == 1
    elif last_observation == "craft_reply":
        assert result["stop_reason"] == "time_slice_progress"
        assert result["recrafted_picks"] == 1
        assert "stone_pickaxe" not in result["gained"]
    else:
        assert result["stop_reason"] == "time_slice"
        assert result["recrafted_picks"] == 0
        assert "stone_pickaxe" not in result["gained"]


@pytest.mark.parametrize(("dimension", "target", "ore", "probe"), [
    ("overworld", "iron", "iron_ore", "status"),
    ("the_nether", "ancient_debris", "ancient_debris", "find_blocks"),
])
def test_first_dfs_probe_timeout_can_recover_in_the_same_slice(monkeypatch, dimension, target, ore, probe) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension=dimension,
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    world.blocks[(1, 64, 0)] = ore
    timed_out = False

    def handler(request):
        nonlocal timed_out
        if request.url.path.endswith(f"/{probe}") and not timed_out:
            timed_out = True
            clock.now += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("initial probe stalled", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="FirstProbeRetry", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target=target, target_y=64, goal_count=1, max_duration_s=32)

    assert timed_out
    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert clock.now <= 1032


@pytest.mark.parametrize("released", [False, True])
def test_mid_slice_dig_timeout_requires_confirmed_action_release(monkeypatch, released) -> None:
    from mc_agent.errors import ApiError
    from mc_agent.skills.mining_dfs import _BudgetSession, _MiningDeadline

    clock = _MiningClock()
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    requests = []

    def handler(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        requests.append(endpoint)
        if endpoint == "stop":
            return httpx.Response(200, json={"ok": True, "result": {"released": released}})
        if len(requests) == 1:
            return httpx.Response(200, json={"ok": True, "result": {"dug": "deepslate"}})
        raise httpx.ReadTimeout("mid-slice dig did not reply", request=request)

    with BotSession("tok", bot_username="DigRelease", transport=httpx.MockTransport(handler)) as bot:
        budget = _BudgetSession(bot, deadline=clock.now + 40)
        budget.call("dig", {"pos": [0, -54, 0]})
        if released:
            with pytest.raises(ApiError, match="dig_timeout") as error:
                budget.call("dig", {"pos": [1, -54, 0]})
            assert error.value.http_status == 408
            assert budget.timeout_path is None
        else:
            with pytest.raises(_MiningDeadline):
                budget.call("dig", {"pos": [1, -54, 0]})
            assert budget.timeout_path == "dig"
            assert budget.timeout_dig_pos == (1, -54, 0)

    assert requests == ["dig", "dig", "stop"]
    assert budget.dug_count == 1


@pytest.mark.parametrize("other_branch_cooling", [False, True])
def test_cooling_leaf_resumes_a_pending_branch_at_the_same_depth(monkeypatch, other_branch_cooling: bool) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(0.5, 15.0, 3.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    route = [(0, 15, z) for z in range(4)]
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(2, 15, 0)] = "ancient_debris"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="CoolingLeafAtDepth", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        engine = skills.mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -49), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        junction = engine._create_node(
            kind="spine", anchor=route[0], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, route[0]]),
            branches=[BranchSpec("spine_forward", "east", 4)],
        )
        cooling = BranchSpec("branch_leaf", "south", 5)
        engine._create_node(
            kind="spine", anchor=route[-1], parent_id=junction.node_id,
            edge_from_parent=ShaftEdge("branch_leaf", "south", route), branches=[cooling],
        )
        stalled = (0, 16, 4)
        skills._avoid_temporarily(stalled, "dig_timeout", 30.0)
        if other_branch_cooling:
            skills._avoid_temporarily((1, 16, 0), "dig_timeout", 30.0)
        result = engine.run(target="ancient_debris", target_y=15, goal_count=1)

    assert result["stop_reason"] == ("dig_cell_avoided" if other_branch_cooling else "quota_reached")
    if other_branch_cooling:
        assert engine.active_node_id == junction.node_id and not world.dig_log
    assert result["pos_after"][1] == 15
    assert engine.nodes[collar.node_id] is collar and cooling.status == "pending"
    assert all(pos != stalled for pos, _, _ in world.dig_log)


@pytest.mark.parametrize("dry_turn", [True, False])
@pytest.mark.parametrize("dug_before_timeout", [False, True])
def test_timeout_at_a_nether_switchback_cools_its_stalled_dig(monkeypatch, dry_turn, dug_before_timeout) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 65.0, 0.5), surface_y=70, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    collar_anchor, junction_anchor = (0, 65, 0), (0, 64, 1)
    for x, y, z in (collar_anchor, junction_anchor):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(0, 66, 1)] = "air"
    world.blocks[(-1, 62, 1)] = "bedrock"
    if dry_turn:
        world.blocks[(2, 62, 1)] = "ancient_debris"
    else:
        world.blocks[(1, 62, 1)] = "bedrock"
    upper_face = (0, 65, 2)
    stalled = (0, 64, 2) if dug_before_timeout else upper_face
    attempts = []

    def handler(request):
        if dug_before_timeout and request.url.path.endswith("/status") and mining._floor_pos(world.pos) == stalled:
            response = world.handler(request).json()
            response["result"]["on_ground"] = False
            return httpx.Response(200, json=response)
        if request.url.path.endswith("/dig"):
            pos = tuple(json.loads(request.content)["pos"])
            if pos == stalled:
                attempts.append(clock.now)
                if dug_before_timeout and len(attempts) == 1:
                    # The recorded switchback is one cell behind the last observed feet.
                    world.pos = [stalled[0] + 0.5, float(stalled[1]), stalled[2] + 0.5]
                    engine.bot.status()
                clock.now += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("dig beside the existing stair stalled", request=request)
            if dug_before_timeout and pos == upper_face:
                clock.sleep(7.0)
        return world.handler(request)

    with BotSession("tok", bot_username="NetherSwitchbackCooldown", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=collar_anchor, parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        branch = mining.BranchSpec("stair_down", "south", 49)
        junction = engine._create_node(
            kind="switchback", anchor=junction_anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar_anchor, junction_anchor]),
            branches=[branch],
        )
        args = dict(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=55)
        first = engine.run(**{**args, "max_duration_s": 20 if dug_before_timeout else 55})
        if dug_before_timeout:
            assert mining._floor_pos(first["pos_after"]) == stalled
            # Start the next slice from the adjacent confirmed stance.
            world.pos = [0.5, float(junction_anchor[1]), 1.5]
        assert engine.skills._is_block_avoided(stalled)
        # The next slice can arrive more than 30 seconds later.
        clock.sleep(31.25)
        second = engine.run(**args)

    assert first["stop_reason"] == "time_slice_progress"
    assert first["dug_count"] == int(dug_before_timeout) and first["dig_target"] == list(stalled)
    assert len(attempts) == 1
    assert engine.nodes[collar.node_id] is collar
    assert engine.nodes[junction.node_id] is junction and branch.status == "pending"
    if dry_turn:
        assert second["stop_reason"] == "quota_reached"
        assert second["target_gained"] == 1
    else:
        assert second["stop_reason"] == "dig_cell_avoided"
        assert second["pos_after"][1] == junction_anchor[1]
        assert [pos for pos, _, _ in world.dig_log] == ([upper_face] if dug_before_timeout else [])


@pytest.mark.parametrize(
    ("start_cell", "recorded_step"),
    [((0, 14, 0), None), ((1, 14, 0), None), ((1, 12, 0), None), ((2, 12, 0), 13), ((2, 12, 0), 12)],
)
def test_descent_resumes_immediately_after_a_dig_timeout_with_progress(monkeypatch, start_cell, recorded_step) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    start_x, start_y, _ = start_cell
    world = _MockMineWorld(start_pos=(start_x + 0.5, float(start_y), 0.5), surface_y=64,
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    for x, y in ((0, 14), (start_x, start_y)):
        world.blocks[(x, y, 0)] = world.blocks[(x, y + 1, 0)] = "air"
    world.blocks[(start_x + 3, start_y - 3, 0)] = "diamond_ore"
    stalled = (start_x + 2, start_y, 0)
    attempts = []

    def handler(request):
        if request.url.path.endswith("/dig"):
            pos = tuple(json.loads(request.content)["pos"])
            if pos[0] == start_x + 1:
                clock.now += 6.0
            if pos == stalled:
                attempts.append(clock.now)
                if len(attempts) == 1:
                    clock.now += request.extensions["timeout"]["read"]
                    raise httpx.ReadTimeout("next stair dig ran out of time", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="ResumeAfterDigTimeout", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 14, 0), parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", "east", 68)],
        )
        engine.collar_node_id = collar.node_id
        if recorded_step is not None:
            world.blocks[(1, recorded_step, 0)] = world.blocks[(1, recorded_step + 1, 0)] = "air"
            engine._record_corridor((1, recorded_step, 0))
        args = dict(target="diamond", target_y=-54, goal_count=1, max_duration_s=32)
        first = engine.run(**args)
        assert first["stop_reason"] == "time_slice_progress"
        assert first["dig_target"] == list(stalled)
        assert first["dug_count"] > 0
        assert first["pos_after"][1] == start_y - 1
        tip = engine.nodes[engine.active_node_id]
        if recorded_step == 12:
            # Only the lower route's head overlaps the intended joining step.
            assert tip is collar
            assert collar.branches[0].status == "pending"
            return
        assert tip.anchor == (start_x + 1, start_y - 1, 0)
        assert tip.parent_id == collar.node_id
        assert tip.edge_from_parent.waypoints[0] == collar.anchor
        assert start_cell in tip.edge_from_parent.waypoints
        if start_x == 2:
            assert tip.edge_from_parent.waypoints == [collar.anchor, (1, 13, 0), start_cell, tip.anchor]

        second = engine.run(**args)

    assert second["stop_reason"] == "quota_reached"
    assert second["target_gained"] == 1
    assert len(attempts) == 2 and attempts[1] - attempts[0] < 30
    assert engine.collar_node_id == collar.node_id


@pytest.mark.parametrize("steps_before_preemption", [0, 6])
def test_descent_resumes_after_guardian_preemption(monkeypatch, steps_before_preemption) -> None:
    from mc_agent.harness.guardian import ReflexPreempted
    from mc_agent.skills.mining_dfs import BranchSpec

    clock = _MiningClock()
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", clock.sleep)
    start = (0, -22, 0)
    world = _MockMineWorld(
        start_pos=(0.5, -22.0, 0.5), surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9}],
    )
    world.blocks[start] = world.blocks[(0, -21, 0)] = "air"
    interrupted = False

    def handler(request):
        nonlocal interrupted
        if (
            request.url.path.endswith("/dig") and not interrupted
            and world.pos[0] == steps_before_preemption + 0.5
        ):
            interrupted = True
            raise ReflexPreempted({"source": "pre_hurt_creeper"})
        return world.handler(request)

    with BotSession("tok", bot_username="ResumeAfterGuardian", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 86), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=start, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, start]),
            branches=[BranchSpec("stair_down", "east", 32)],
        )
        args = dict(target="diamond", target_y=-54, goal_count=1, max_duration_s=32)
        with pytest.raises(ReflexPreempted):
            engine.run(**args)

        tip = engine.nodes[engine.active_node_id]
        assert engine.bot is bot
        assert tip.anchor == (steps_before_preemption, start[1] - steps_before_preemption, 0)
        assert [(b.status, b.steps) for b in tip.branches] == [("pending", 32 - steps_before_preemption)]
        assert engine.ledger.saved_shafts[0]["active_node_id"] == tip.node_id

        world.blocks[(tip.anchor[0] + 2, tip.anchor[1] - 2, 0)] = "diamond_ore"
        resumed = engine.run(**args)

    assert resumed["stop_reason"] == "quota_reached"
    assert resumed["target_gained"] == 1
    assert engine.collar_node_id == collar.node_id


def test_waypoint_interpolation_checks_deadline_between_hops(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    visited: list[tuple[int, int, int]] = []

    def step(cell, *, from_cell, repair_support):
        visited.append(cell)
        clock.now += 1.0
        return True

    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="TimedWalk", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        monkeypatch.setattr(eng, "_step_to", step)
        result = eng._walk_waypoints([(30, 20, 0)], min_hp_ref=[20.0], deadline=1002.0)
    assert result == (False, "walk_time_slice")
    assert visited == [(1, 20, 0), (2, 20, 0)]


@pytest.mark.parametrize(("start_z", "landing_y", "resumable"), [(0, 19, True), (1, 19, True), (1, 10, False)])
def test_budget_cutoff_preserves_partial_edge_for_the_next_call(monkeypatch, start_z, landing_y, resumable) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, start_z + 0.5), surface_y=20)
    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="PartialEdge", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        branch = mining.BranchSpec("stair_down", "east", 8)
        collar = eng._create_node(kind="collar", anchor=(0, 20, 0), parent_id=None,
                                  edge_from_parent=None, branches=[branch])
        eng.collar_node_id = collar.node_id

        def interrupted(**kwargs):
            branch.status = "active"
            eng.bot.status()
            eng._record_corridor((0, 20, start_z))
            world.pos = [1.5, float(landing_y), start_z + 0.5]
            eng.bot.status()
            clock.now = kwargs["deadline"]
            eng.bot.call("dig", {"pos": [2, 18, 0]})

        monkeypatch.setattr(eng, "_run", interrupted)
        result = eng.run(max_duration_s=5.0)
        tip = eng.nodes[eng.active_node_id]
        if resumable:
            assert tip.parent_id == collar.node_id
            assert tip.anchor == (1, landing_y, start_z)
            expected_route = [collar.anchor]
            if start_z:
                expected_route.append((0, 20, start_z))
            assert tip.edge_from_parent.waypoints == [*expected_route, tip.anchor]
            assert tip.branches[0].status == "pending"
            assert tip.branches[0].steps == 7
            assert branch.status == "completed"
        else:
            assert tip is collar
            assert branch.status == "pending"
    assert result["stop_reason"] == "time_slice_progress"
    assert result["ok"] is True
    assert result["goal_met"] is False
    assert result["pos_before"] == [0.5, 20.0, start_z + 0.5]
    assert result["pos_after"] == [1.5, float(landing_y), start_z + 0.5]
    assert not world.dig_log


def test_blocked_descent_backtrack_resumes_the_deepest_pending_node(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 28.0, 0.5), surface_y=64)
    world.blocks[(0, 28, 0)] = world.blocks[(0, 29, 0)] = "air"
    world.blocks[(2, 26, 0)] = world.blocks[(2, 27, 0)] = "air"
    world.blocks[(3, 25, 0)] = "iron_ore"
    with BotSession("tok", bot_username="ResumeDeepAlternative", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -36), parent_id=None,
                                     edge_from_parent=None, branches=[mining.BranchSpec("stair_down", "north", 40)])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=(2, 26, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [collar.anchor, (2, 26, 0)]),
            branches=[mining.BranchSpec("stair_down", "east", 46, branch_target_y=-20)],
        )
        engine._create_node(
            kind="switchback", anchor=(0, 28, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, (0, 28, 0)]),
            branches=[mining.BranchSpec("stair_down", d, 48, status="pruned") for d in mining.DIRECTIONS],
        )

        def blocked_walk(*args, **kwargs):
            world.pos = [2.5, 26.0, 0.5]
            return False, "backtrack_blocked"

        monkeypatch.setattr(engine, "_walk_waypoints", blocked_walk)
        result = engine.run(target="iron", target_y=-20, goal_count=1, max_duration_s=20)

    assert result["stop_reason"] == "quota_reached"
    assert engine.collar_node_id == collar.node_id
    assert result["pos_after"][1] <= 26


def test_no_progress_hint_does_not_recommend_a_redundant_reset(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    monkeypatch.setattr(mining, "MAX_IDLE_ITERATIONS", 0)
    with BotSession("tok", bot_username="IdleHint", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        result = eng.run(target="iron", goal_count=4, target_y=16, max_duration_s=5.0)
    assert result["stop_reason"] == "no_progress"
    assert "reset" not in result["message"]
    assert "空间树为空时直接 explore" in result["message"]
    assert result["goal_met"] is False
    assert result["dug_count"] == len(world.dig_log) == 0


def test_timed_slice_with_confirmed_dig_remains_retryable_at_same_cell(monkeypatch) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.tool_registry import Tool
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="RetryableSlice", transport=httpx.MockTransport(world.handler)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0, 20, 0])
        engine = toolset.skills.mining_dfs
        attempts = 0

        def cut_off_after_dig(*, deadline, **kwargs):
            nonlocal attempts
            attempts += 1
            engine.bot.status()
            engine.bot.call("dig", {"pos": [attempts, 19, 0], "collect": False})
            clock.now = deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_run", cut_off_after_dig)
        tool = _RepeatFailureGuard(toolset).wrap_tool(Tool(
            name="mc_skill_mine_dfs", description="Mine", parameters={},
            handler=toolset.mc_skill_mine_dfs,
        ))
        first = json.loads(tool.handler(target="iron", max_duration_s=20))
        second = json.loads(tool.handler(target="iron", max_duration_s=20))

    assert attempts == 2
    for result in (first, second):
        assert result["stop_reason"] == "time_slice_progress"
        assert result["dug_count"] == 1
        assert result["pos_before"] == result["pos_after"]
        assert result["gained"] == {}
        assert result["goal_met"] is False


def test_timed_slice_credits_only_observed_target_items(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="VerifiedSliceItems", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def cut_off_after_inventory(*, deadline, **kwargs):
            engine.bot.inventory()
            world._add_item("raw_iron")
            engine.bot.inventory()
            world._add_item("raw_iron")
            clock.now = deadline
            engine.bot.inventory()

        monkeypatch.setattr(engine, "_run", cut_off_after_inventory)
        result = engine.run(target="iron", goal_count=2, max_duration_s=5.0)

    assert world._count("raw_iron") == 2
    assert result["target_gained"] == 1
    assert result["gained"] == {"raw_iron": 1}
    assert result["goal_met"] is False


@pytest.mark.parametrize(("goal_x", "expected_xs"), [(3, [5, 4, 3]), (8, [5, 6, 7, 8])])
def test_navigation_resumes_mid_edge_toward_the_requested_node(goal_x, expected_xs, monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(5.5, 15.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="ResumeTowardGoal", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 20, 0), parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        upper = engine._create_node(
            kind="switchback", anchor=(3, 17, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [(i, 20 - i, 0) for i in range(4)]), branches=[],
        )
        lower = engine._create_node(
            kind="switchback", anchor=(8, 12, 0), parent_id=upper.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [(i, 20 - i, 0) for i in (3, 4, 6, 7, 8)]), branches=[],
        )
        traversed = []

        def walk(waypoints, **kwargs):
            traversed.extend(pt[0] for pt in waypoints)
            x, y, z = waypoints[-1]
            world.pos = [x + 0.5, float(y), z + 0.5]
            return True, None

        def reject_shortcut(*args, **kwargs):
            pytest.fail("the bot must resume its recorded edge")

        monkeypatch.setattr(engine, "_walk_waypoints", walk)
        monkeypatch.setattr(engine, "_move_exact", reject_shortcut)
        goal = upper if goal_x == 3 else lower
        assert engine._navigate_to_node(goal.node_id, min_hp_ref=[20.0]) == (True, None)
        assert engine.active_node_id == goal.node_id

    assert traversed == expected_xs

