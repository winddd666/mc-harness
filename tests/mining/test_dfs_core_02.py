from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

@pytest.mark.parametrize("first_slice_budget", [32.0, 2.16])
def test_descent_cooldown_requires_time_to_dig(monkeypatch, first_slice_budget) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    stalled = (0, 65, 0)
    world.blocks[stalled] = "gravel"
    world.blocks[(0, 62, -2)] = "ancient_debris"
    attempts = []

    def handler(request):
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content)["pos"]) == stalled:
            read_s = request.extensions["timeout"]["read"]
            attempts.append(read_s)
            if len(attempts) == 1 or (first_slice_budget == 32.0 and clock.now < 1030):
                clock.now += read_s
                raise httpx.ReadTimeout("gravel dig stalled", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="SharedDigTimeout", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", direction, 49) for direction in ("east", "north")],
        )
        engine.collar_node_id = collar.node_id
        args = dict(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=32)
        first = engine.run(**{**args, "max_duration_s": first_slice_budget})
        assert first["stop_reason"] == "time_slice"
        resumed = engine.run(**args)
        if first_slice_budget == 32.0:
            assert resumed["stop_reason"] == "dig_cell_avoided"
            assert len(attempts) == 1
            assert all(branch.status == "pending" for branch in collar.branches)
            clock.sleep(mining.DIG_TIMEOUT_COOLDOWN_S + 1)
            resumed = engine.run(**args)
        else:
            assert attempts[0] < 1.0

    assert resumed["stop_reason"] == "quota_reached"
    assert resumed["target_gained"] == 1
    assert len(attempts) == 2
    assert engine.collar_node_id == collar.node_id


def test_resuming_a_completed_descent_opens_branches_at_the_target_layer(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(0.5, 16.0, 3.5), surface_y=64)
    path = [(0, 18, 1), (0, 17, 2), (0, 16, 3)]
    for x, y, z in path:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(1, 16, 3)] = "iron_ore"
    world.blocks[(-1, 16, 3)] = "iron_ore"

    with BotSession("tok", bot_username="ResumeAtTargetLayer", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=path[0], parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        remaining = BranchSpec("stair_down", "south", 1, branch_target_y=16)
        tip = engine._create_node(
            kind="switchback", anchor=path[-1], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", path), branches=[remaining],
        )
        # Require the same-layer branch to dig the ore.
        monkeypatch.setattr(engine, "_run_vein_hook", lambda *args, **kwargs: {
            "mined_coords": [], "deferred_added": 0, "catch_placed": 0, "breaches_sealed": 0,
        })
        result = engine.run(target="iron", target_y=16, goal_count=1)

    assert result["stop_reason"] == "quota_reached"
    assert result["pos_after"][1] == 16
    assert engine.nodes[tip.node_id] is tip and tip.kind == "spine"
    assert remaining.status == "completed"


@pytest.mark.parametrize("start_pos", [(0.5, 48.0, 1.5), (1.5, 47.0, 1.5), (3.5, 46.0, 0.5)])
def test_descent_rejoins_the_saved_node_from_an_offset_stance(monkeypatch, start_pos) -> None:
    import mc_agent.skills.mining_dfs as mining

    anchor = (0, 47, 0)
    world = _MockMineWorld(start_pos=start_pos, surface_y=68)
    world.blocks[anchor] = world.blocks[(0, 48, 0)] = "air"
    x, y, z = mining._floor_pos(start_pos)
    world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(1, 46, 0)] = "iron_ore"
    with BotSession("tok", bot_username="ResumeFromHigherStep", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 68, -21), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        active = engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, anchor]),
            branches=[mining.BranchSpec("stair_down", "east", 31)],
        )
        descend = engine._dig_stair_down_edge

        def dig_from_saved_node(**kwargs):
            assert mining._floor_pos(world.pos) == anchor
            return descend(**kwargs)

        monkeypatch.setattr(engine, "_dig_stair_down_edge", dig_from_saved_node)
        result = engine.run(target="iron", target_y=16, goal_count=1, max_duration_s=32)

    assert result["stop_reason"] == "quota_reached"
    tip = engine.nodes[engine.active_node_id]
    assert tip.parent_id == active.node_id
    assert tip.edge_from_parent.waypoints[0] == anchor
    assert tip.anchor == (1, 46, 0)


@pytest.mark.parametrize("deadline_expires", [False, True])
def test_descent_keeps_confirmed_steps_when_the_bot_moves_upward(monkeypatch, deadline_expires) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    origin = (0, 50, 0)
    descent = [origin, (1, 49, 0), (2, 48, 0)]
    displaced = (2, 63, 0)
    world = _MockMineWorld(start_pos=(0.5, 50.0, 0.5), surface_y=64)
    attempted_from = []
    with BotSession("tok", bot_username="DescentDisplaced", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=origin, parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", "east", 34)],
        )
        engine.collar_node_id = collar.node_id

        def descend(**kwargs):
            attempted_from.append(engine.active_node_id)
            if len(attempted_from) > 1:
                raise mining._MiningDeadline
            for x, y, z in [*descent[1:], displaced]:
                world.pos = [x + 0.5, y, z + 0.5]
                engine.bot.status()
            if deadline_expires:
                raise mining._MiningDeadline
            return {
                "steps_done": 2, "stop_reason": "fluid_or_bedrock",
                "end_anchor": displaced, "waypoints": [*descent, displaced],
                "breaches_sealed": 0, "catch_placed": 0, "deferred_added": 0, "mined_ores": [],
            }

        monkeypatch.setattr(engine, "_dig_stair_down_edge", descend)
        monkeypatch.setattr(engine, "_switchback_branches", lambda *a, **k: [mining.BranchSpec("stair_down", "south", 40)])
        result = engine.run(target="iron", target_y=16, goal_count=1, max_duration_s=32)

    assert result["stop_reason"] == "depth_reset"
    assert attempted_from == [collar.node_id]
    assert result["pos_after"][1] == displaced[1]
    assert [node.anchor for node in engine.nodes.values()] == [origin, descent[-1]]
    tip = engine.nodes[engine.active_node_id]
    assert tip.edge_from_parent.waypoints == descent
    assert [(b.status, b.steps) for b in tip.branches] == [("pending", 32)]


def test_navigation_rejoins_an_edge_instead_of_moving_through_a_wall(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(5.5, 15.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="MidEdge", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 20, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        route = [(i, 20 - i, 0) for i in range(11)]
        eng._create_node(kind="switchback", anchor=(10, 10, 0), parent_id=collar.node_id,
                         edge_from_parent=ShaftEdge("stair_down", "east", route), branches=[])
        walks: list[list] = []

        def walk(points, **kwargs):
            walks.append(points)
            world.pos = [points[-1][0] + 0.5, float(points[-1][1]), points[-1][2] + 0.5]
            return True, None

        monkeypatch.setattr(eng, "_walk_waypoints", walk)
        monkeypatch.setattr(eng, "_move_exact", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct wall move")))
        assert eng._navigate_to_node(collar.node_id, min_hp_ref=[20.0]) == (True, None)
    assert walks[0][:6] == list(reversed(route[:6]))


def test_exhausted_leaf_backtracks_from_the_middle_toward_its_parent(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(0.5, 16.0, 1.5), surface_y=64)
    parent_anchor, tip_anchor = (0, 16, 4), (0, 16, 0)
    for z in range(5):
        world.blocks[(0, 16, z)] = world.blocks[(0, 17, z)] = "air"
    world.blocks[(0, 15, 0)] = "air"
    world.blocks[(2, 16, 4)] = "iron_ore"
    movement_targets = []

    def handler(request):
        if request.url.path.endswith("/move_to"):
            move = json.loads(request.content)
            movement_targets.append((math.floor(move["x"]), math.floor(move["z"])))
        return world.handler(request)

    with BotSession("tok", bot_username="ReturnFromLeafMidpoint", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 52), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        parent = engine._create_node(
            kind="spine", anchor=parent_anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, parent_anchor]),
            branches=[BranchSpec("spine_forward", "east", 4)],
        )
        engine._create_node(
            kind="spine", anchor=tip_anchor, parent_id=parent.node_id,
            edge_from_parent=ShaftEdge("branch_leaf", "north", [parent_anchor, (0, 16, 3), tip_anchor]), branches=[],
        )
        # Keep ore collection in the pending branch itself.
        monkeypatch.setattr(engine, "_run_vein_hook", lambda *args, **kwargs: {
            "mined_coords": [], "deferred_added": 0, "catch_placed": 0, "breaches_sealed": 0,
        })
        result = engine.run(target="iron", target_y=16, goal_count=1)

    assert (tip_anchor[0], tip_anchor[2]) not in movement_targets
    assert result["stop_reason"] == "quota_reached"
    assert result["pos_after"][1] == 16


def test_failed_underground_rejoin_reports_blocker_and_last_position(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(0.5, 39.0, 2.5), surface_y=64)
    blocked = "backtrack_blocked@0,42,0(feet=furnace,head=air,from=0,41,1)"
    with BotSession("tok", bot_username="BlockedRejoinReceipt", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("stair_down", "south", 48)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 67
        tip = engine._create_node(
            kind="switchback", anchor=(0, 42, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, (0, 42, 0)]),
            branches=[BranchSpec("stair_down", "south", 26)],
        )

        def fail_rejoin(node_id, **kwargs):
            assert node_id == tip.node_id
            world.pos[:] = [0.5, 41.0, 1.5]
            engine.bot.status()
            return False, blocked

        monkeypatch.setattr(engine, "_resume_shaft_tip", fail_rejoin)
        result = engine.run(target="iron", target_y=16, goal_count=8, strategy="radar")

    assert result["stop_reason"] == "needs_surface_climb"
    assert result["rejoin_error"] == blocked
    assert result["pos_before"] == [0.5, 39.0, 2.5]
    assert result["pos_after"] == [0.5, 41.0, 1.5]
    assert engine.active_node_id == tip.node_id


@pytest.mark.parametrize("lower_open", [True, False])
def test_rejoin_prefers_a_nearby_route_below_the_current_height(monkeypatch, lower_open: bool) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=65)
    with BotSession("tok", bot_username="PreferLowerJoin", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 65, -30), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        high = engine._create_node(
            kind="switchback", anchor=(0, 29, 5), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, (0, 29, 5)]), branches=[],
        )
        low = engine._create_node(
            kind="spine", anchor=(1, 16, 14), parent_id=high.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [high.anchor, (1, 16, 14)]), branches=[],
        )
        tip = engine._create_node(
            kind="spine", anchor=(4, 16, 17), parent_id=low.node_id,
            edge_from_parent=ShaftEdge("branch_leaf", "east", [low.anchor, (4, 16, 17)]), branches=[],
        )
        attempted = []

        def move(cell, **kwargs):
            attempted.append(cell)
            return cell != low.anchor or lower_open

        def walk(waypoints, **kwargs):
            x, y, z = waypoints[-1]
            world.pos = [x + 0.5, y, z + 0.5]
            return True, None

        monkeypatch.setattr(engine, "_move_exact", move)
        monkeypatch.setattr(engine, "_walk_waypoints", walk)
        result = engine._resume_shaft_tip(tip.node_id, min_hp_ref=[20.0])

    assert result == (True, None) and engine.active_node_id == tip.node_id
    assert attempted == ([low.anchor] if lower_open else [low.anchor, high.anchor])


def test_blocked_spine_rejoin_tries_another_pending_node_at_depth(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    junction_pos, pending_pos, spent_pos = (0, 16, 0), (6, 15, 0), (12, 15, 0)
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    for x, y, z in (junction_pos, pending_pos):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(8, 15, 0)] = "ancient_debris"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="BlockedSpineRejoin", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -48), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        junction = engine._create_node(
            kind="spine", anchor=junction_pos, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, junction_pos]), branches=[],
        )
        pending = engine._create_node(
            kind="spine", anchor=pending_pos, parent_id=junction.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [junction_pos, pending_pos]),
            branches=[BranchSpec("spine_forward", "east", 4)],
        )
        spent = engine._create_node(
            kind="spine", anchor=spent_pos, parent_id=pending.node_id,
            edge_from_parent=ShaftEdge("branch_leaf", "east", [pending_pos, spent_pos]), branches=[],
        )
        attempted = []

        def rejoin(node_id, **kwargs):
            attempted.append(node_id)
            if node_id == spent.node_id:
                engine.active_node_id = junction.node_id
                return False, "backtrack_blocked"
            assert node_id == pending.node_id
            world.pos = [6.5, 15.0, 0.5]
            engine.active_node_id = pending.node_id
            return True, None

        monkeypatch.setattr(engine, "_resume_shaft_tip", rejoin)
        result = engine.run(target="ancient_debris", target_y=15, goal_count=1)

    assert attempted == [spent.node_id, pending.node_id]
    assert result["stop_reason"] == "quota_reached"
    assert engine.nodes[collar.node_id] is collar


def test_completed_one_step_spine_keeps_the_forward_route(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 15.0, 0.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    anchor = (0, 15, 0)
    world.blocks[anchor] = world.blocks[(0, 16, 0)] = "air"
    world.blocks[(3, 15, 0)] = "ancient_debris"

    with BotSession("tok", bot_username="OneStepSpine", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 41, -26), parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", "west", 26)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        node = engine._create_node(
            kind="spine", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, anchor]),
            branches=[mining.BranchSpec("spine_forward", "east", 1)],
        )
        walk = engine._walk_waypoints

        def walk_at_depth(waypoints, *, reverse=False, **kwargs):
            assert not (reverse and waypoints == [anchor, (1, 15, 0)])
            assert all(y <= 15 for _, y, _ in waypoints)
            return walk(waypoints, reverse=reverse, **kwargs)

        monkeypatch.setattr(engine, "_walk_waypoints", walk_at_depth)
        result = engine.run(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=32)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert result["pos_after"][1] == 15
    assert node.branches[0].status == "completed"
    assert any(child.parent_id == node.node_id and child.anchor == (1, 15, 0) for child in engine.nodes.values())


@pytest.mark.parametrize("local_pending", [True, False])
def test_depleted_target_level_does_not_walk_up_to_restart_the_descent(monkeypatch, local_pending) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    descent = [(i, -43 - i, 0) for i in range(12)]
    current = descent[-1]
    side_route = [current, (11, -54, 1), (11, -54, 2)]
    world = _MockMineWorld(
        start_pos=(11.5, -54.0, 0.5), surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9}],
    )
    for x, y, z in [*descent, *side_route]:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    if local_pending:
        world.blocks[(11, -54, 6)] = "deepslate_diamond_ore"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="KeepTargetDepth", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=descent[0], parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("stair_down", "south", 11)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 74
        spent = engine._create_node(
            kind="spine", anchor=current, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", descent), branches=[],
        )
        if local_pending:
            engine._create_node(
                kind="spine", anchor=side_route[-1], parent_id=collar.node_id,
                edge_from_parent=ShaftEdge("stair_down", "east", [*descent, *side_route[1:]]),
                branches=[BranchSpec("spine_forward", "south", 4)],
            )
            engine.active_node_id = spent.node_id
        walk = engine._walk_waypoints

        def walk_at_depth(waypoints, **kwargs):
            assert max(y for _, y, _ in waypoints) <= -46, "unwound the target layer to the old collar"
            return walk(waypoints, **kwargs)

        monkeypatch.setattr(engine, "_walk_waypoints", walk_at_depth)
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=10)

    if local_pending:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == 1
    else:
        assert result["stop_reason"] == "shaft_rejoin_blocked"
        assert not world.dig_log and not world.visited_blocks
    assert result["pos_after"][1] == -54
    assert engine.collar_node_id == collar.node_id
    assert collar.anchor not in engine.exhausted_collars


@pytest.mark.parametrize("target_y", [None, 16])
def test_emerald_dfs_uses_mountain_height_when_no_depth_is_requested(monkeypatch, target_y) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    start_y = 110
    world = _MockMineWorld(
        start_pos=(0.5, float(start_y), 0.5), surface_y=start_y, dimension="overworld",
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9},
            {"name": "iron_pickaxe", "count": 1, "slot": 10},
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/dig", "/move_to")):
            clock.now += 0.5
        return world.handler(request)

    with BotSession("tok", bot_username="EmeraldDepth", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).mine_dfs(
            target="emerald", target_y=target_y, goal_count=1, max_duration_s=10,
        )

    ecology = mining.assess_world_target(("emerald_ore",), dimension="overworld", mode="find")
    assert result["tree"]["target_y"] == (target_y if target_y is not None else ecology["recommended_y"])
    if target_y is None:
        assert world.pos[1] >= start_y
    else:
        assert world.pos[1] < start_y


def test_generic_gold_in_nether_counts_nuggets_and_uses_the_nether_layer(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether")
    world.blocks[(1, 64, 0)] = "nether_gold_ore"
    monkeypatch.setattr(world, "_drop_name_for", lambda name: "gold_nugget" if name == "nether_gold_ore" else name)
    with BotSession("tok", bot_username="NetherGold", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="gold", goal_count=1, max_duration_s=10.0)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_y"] == 64
    assert result["goal_met"] is True
    assert result["gained"]["gold_nugget"] == 1
    assert any(name == "nether_gold_ore" for _, name, _ in world.dig_log)


def test_pathfinder_expiry_leaves_time_for_local_movement(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    sent = []

    def handler(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "move_to":
            server_timeout = json.loads(request.content)["timeout"] / 1000
            read_timeout = request.extensions["timeout"]["read"]
            sent.append((server_timeout, read_timeout))
            if server_timeout + 0.1 > read_timeout:
                clock.now += read_timeout
                raise httpx.ReadTimeout("pathfinder still running", request=request)
            clock.now += server_timeout + 0.1
            return httpx.Response(200, json={"ok": False, "error": {
                "code": "timeout", "message": "pathfinder reached its own timeout",
            }})
        if endpoint == "control":
            world.pos = [1.5, 20.0, 0.5]
        return world.handler(request)

    with BotSession("tok", bot_username="TimedMovementFallback", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def take_step(**kwargs):
            return {"ok": engine._move_exact((1, 20, 0))}

        monkeypatch.setattr(engine, "_run", take_step)
        result = engine.run(max_duration_s=20.0)

    assert len(sent) == 1
    server_timeout, read_timeout = sent[0]
    assert 0 < server_timeout < read_timeout
    assert result["ok"] is True
    assert tuple(world.pos) == (1.5, 20.0, 0.5)
    assert clock.now < 1020.0


@pytest.mark.parametrize(("landing_y", "on_ground"), [(20.0, True), (21.0, False)])
def test_step_requires_settled_arrival_on_requested_layer(landing_y, on_ground, monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "control":
            world.pos = [1.5, landing_y, 0.5]
        response = world.handler(request)
        if path == "status" and world.pos[0] == 1.5:
            body = response.json()
            body["result"]["on_ground"] = on_ground
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="WrongLevelStep", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        monkeypatch.setattr(engine, "_move_exact", lambda cell: False)
        monkeypatch.setattr(engine, "_hop_up_to", lambda cell: False)
        assert engine._step_to((1, 21, 0)) is False

    assert world.pos[0] == 1.5

