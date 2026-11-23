from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

@pytest.mark.parametrize("floor", ["stone", "water", "air"])
@pytest.mark.parametrize(("start", "spine_y", "old_target_y"), [((5, 14, 0), 16, 16), ((-8, -24, 0), -21, -54)])
def test_diamond_explore_resumes_below_an_unreachable_spine(monkeypatch, floor, start, spine_y, old_target_y) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    spine_anchor = (0, spine_y, 0)
    world = _MockMineWorld(
        start_pos=(start[0] + 0.4, float(start[1]), 0.5), surface_y=63,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9}],
    )
    for x, y, z in (start, spine_anchor):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(start[0], start[1] - 1, 0)] = floor
    world.blocks[(start[0], start[1] - 1, -1)] = "diamond_ore"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    rejected_moves = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/move_to"):
            move = json.loads(request.content)
            if move.get("y") == spine_anchor[1]:
                rejected_moves.append((move["x"], move["y"], move["z"]))
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "path_not_found", "message": "blocked"},
                })
        return world.handler(request)

    with BotSession("tok", bot_username="ContinueBelowSpine", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 63, -47), parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        spine = engine._create_node(
            kind="spine", anchor=spine_anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, spine_anchor]),
            branches=[BranchSpec("spine_forward", "east", 5), BranchSpec("branch_leaf", "north", 6)],
        )
        engine.target_y = old_target_y
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=20, strategy="radar")

    assert rejected_moves == [(0.5, float(spine_y), 0.5)]
    assert result["pos_after"][1] <= start[1]
    if floor == "stone":
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == world._count("diamond") == 1
        assert engine.nodes[engine.collar_node_id].anchor == start
        saved = next(shaft for shaft in engine.ledger.saved_shafts if shaft["collar_anchor"] == list(collar.anchor))
        assert not saved["retired"]
        assert engine.collar_ceiling_y == collar.anchor[1]
    else:
        assert result["stop_reason"] == "needs_surface_climb"
        assert result["rejoin_error"] == "cannot_reach_nearest_node"
        assert engine.nodes[engine.active_node_id] is spine
        assert engine.nodes[engine.collar_node_id] is collar
        assert not world.dig_log and not world.place_log and not world.visited_blocks


@pytest.mark.parametrize("from_archive", [False, True])
def test_explore_rejoins_a_sidestep_between_saved_waypoints(monkeypatch, from_archive) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    sidestep_start = (0, -23, 87)
    mid_sidestep = (1, -23, 87)
    sidestep_end = (3, -23, 87)
    tip_anchor = (3, -54, 118)
    world = _MockMineWorld(
        start_pos=(1.5, -23, 87.5), surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9}],
    )
    with BotSession("tok", bot_username="ResumeSidestep", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        stair = engine._create_node(
            kind="switchback", anchor=sidestep_start, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, sidestep_start]),
            branches=[],
        )
        turn = engine._create_node(
            kind="switchback", anchor=sidestep_end, parent_id=stair.node_id,
            edge_from_parent=ShaftEdge("offset_sidestep", "east", [sidestep_start, sidestep_end]),
            branches=[],
        )
        tip = engine._create_node(
            kind="spine", anchor=tip_anchor, parent_id=turn.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [sidestep_end, tip_anchor]),
            branches=[mining.BranchSpec("spine_forward", "east", 4)],
        )
        engine.target_y = -54
        if from_archive:
            engine._archive_current_shaft_to_ledger()
            engine._clear_active_tree_only()
        walked = []

        def walk(waypoints, **kwargs):
            walked.append(waypoints)
            x, y, z = waypoints[-1]
            world.pos = [x + 0.5, y, z + 0.5]
            engine.bot.status()
            if waypoints[-1] == tip_anchor:
                clock.now = engine._deadline
                return False, "walk_time_slice"
            return True, None

        def open_new_collar(*args, **kwargs):
            pytest.fail("opened a new collar while standing on a saved sidestep")

        monkeypatch.setattr(engine, "_walk_waypoints", walk)
        monkeypatch.setattr(engine, "_ensure_solid_collar_anchor", open_new_collar)
        result = engine.run(target="diamond", target_y=-54, max_duration_s=32)

    assert walked == [[mid_sidestep, sidestep_end], [sidestep_end, tip_anchor]]
    assert result["stop_reason"] == "time_slice_progress"
    assert result["pos_after"][1] == -54
    assert engine.collar_node_id == collar.node_id
    assert engine.active_node_id == tip.node_id
    assert not world.dig_log


def test_exhausted_transit_node_uses_its_deeper_pending_branch_before_ascending(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec, DIRECTIONS

    world = _MockMineWorld(start_pos=(0.5, 28.0, 0.5), surface_y=64)
    route = [(0, 28, 0), (1, 27, 0), (2, 26, 0)]
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(3, 25, 0)] = "iron_ore"
    with BotSession("tok", bot_username="DeeperPendingBranch", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -36), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        transit = engine._create_node(
            kind="switchback", anchor=route[0], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, route[0]]),
            branches=[BranchSpec("stair_down", d, 46, status="completed") for d in DIRECTIONS],
        )
        engine._create_node(
            kind="switchback", anchor=route[-1], parent_id=transit.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", route),
            branches=[BranchSpec("stair_down", "east", 46, branch_target_y=-20)],
        )
        engine.active_node_id = transit.node_id
        walk = engine._walk_waypoints

        def walk_at_depth(points, *, reverse=False, **kwargs):
            assert not reverse, "ascended despite a deeper pending branch"
            return walk(points, **kwargs)

        monkeypatch.setattr(engine, "_walk_waypoints", walk_at_depth)
        result = engine.run(target="iron", target_y=-20, goal_count=1, max_duration_s=15)

    assert result["stop_reason"] == "quota_reached"
    assert result["pos_after"][1] <= 26
    assert engine.collar_node_id == collar.node_id


@pytest.mark.parametrize(("budget", "retry_budget"), [(32.0, 47), (90.0, None)])
def test_nested_http_timeout_stops_the_action_and_restores_shared_sessions(monkeypatch, budget, retry_budget) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/dig"):
            raise httpx.ReadTimeout("offline simulated stalled dig", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="TimedHTTP", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs

        def stalled(**kwargs):
            eng.bot.status()
            eng.skills.bot.call("dig", {"pos": [1, 20, 0]})

        monkeypatch.setattr(eng, "_run", stalled)
        result = eng.run(max_duration_s=budget)
        assert eng.bot is bot and eng.skills is skills and skills.bot is bot
        assert bot._recovering_quarantine is False
    assert result["stop_reason"] == "time_slice"
    assert result["ok"] is False and result["requires_replan"] is True
    assert "原地用相同参数重试" in result["message"]
    next_steps = result["suggested_next"]
    assert "安全站位" in next_steps["if_blocked"]
    assert next_steps.get("retry_with") == ({"max_duration_s": retry_budget} if retry_budget else None)
    assert [p.rsplit("/", 1)[-1] for p in calls] == ["status", "dig", "stop"]


def test_partial_slice_success_reports_goal_unmet_without_claiming_ore(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="PartialGoal", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs

        def partial_edge(**kwargs):
            return {"steps_done": 0, "stop_reason": "time_slice", "breaches_sealed": 0,
                    "catch_placed": 0, "deferred_added": 0, "mined_ores": [],
                    "waypoints": [(0, 20, 0)], "end_anchor": [0, 20, 0]}

        monkeypatch.setattr(eng, "_dig_stair_down_edge", partial_edge)
        result = eng.run(target="iron", goal_count=4, target_y=16, max_duration_s=5.0)
    assert result["ok"] is True
    assert result["stop_reason"] == "time_slice_progress"
    assert result["goal_met"] is False
    assert result["target_gained"] == 0


@pytest.mark.parametrize(("dimension", "expected_dimension", "available"), [
    ("minecraft:the_nether", "the_nether", {"ancient_debris": 15, "quartz": 48, "gold": 64}),
    ("world_the_end", "the_end", {}),
])
def test_dfs_rejects_diamond_outside_overworld_before_scanning_or_digging(
    dimension, expected_dimension, available,
) -> None:
    world = _MockMineWorld(start_pos=(0.5, 52.0, 0.5), surface_y=64, dimension=dimension)
    endpoints = []

    def handler(request):
        endpoints.append(request.url.path.rsplit("/", 1)[-1])
        return world.handler(request)

    with BotSession("tok", bot_username="BadDimensionOre", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.mine_dfs(target="diamond", target_y=15, max_duration_s=60)
        assert skills.mining_dfs.target_y == 16

    assert result["stop_reason"] == "bad_target_for_dimension"
    assert result["ok"] is False and result["requires_replan"] is True
    assert result["dimension"] == expected_dimension
    assert result["allowed_dimensions"] == ["overworld"]
    assert result["available_targets"] == available
    assert result["tree"]["node_count"] == 0
    assert endpoints == ["status"]


def test_dfs_rejects_negative_nether_target_y_before_starting_the_slice() -> None:
    world = _MockMineWorld(start_pos=(1856.5, 65.0, 1805.6), surface_y=65, dimension="the_nether")
    endpoints = []

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        endpoints.append(endpoint)
        if endpoint != "status":
            pytest.fail(f"Started work for an invalid Nether depth: {endpoint}")
        return world.handler(request)

    with BotSession("tok", bot_username="BadNetherDepth", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.mine_dfs(target="gold", target_y=-50, goal_count=10, max_duration_s=32)
        assert skills.mining_dfs.target_y == 16

    assert result["stop_reason"] == "bad_target_y_for_dimension"
    assert result["ok"] is False and result["requires_replan"] is True
    assert result["dimension"] == "the_nether"
    assert result["target_y"] == -50
    assert result["minimum_y"] == 0
    assert result["suggested_target_y"] == 64
    assert result["tree"]["node_count"] == 0
    assert endpoints == ["status"]

