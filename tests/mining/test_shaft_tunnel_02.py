from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

@pytest.mark.parametrize("dry_turn", [True, False])
def test_exhausted_deep_node_tries_local_descent_and_keeps_its_shaft(monkeypatch, dry_turn) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 28.0, 0.5), surface_y=64)
    world.blocks[(0, 28, 0)] = world.blocks[(0, 29, 0)] = "air"
    world.blocks[(0, 27, 1)] = "bedrock"
    world.blocks[(-1, 27, 0)] = "iron_ore" if dry_turn else "bedrock"
    walks = []

    def blocked_walk(waypoints, **kwargs):
        walks.append(waypoints)
        return False, "backtrack_blocked"

    with BotSession("tok", bot_username="DeepNodeRecovery", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 64, -36), parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", "east", 40)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        route = [(0, 64 - step, -36 + step) for step in range(37)]
        engine._create_node(
            kind="switchback", anchor=route[-1], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", route),
            branches=[mining.BranchSpec("stair_down", "east", 40, status="pruned")],
        )
        monkeypatch.setattr(engine, "_walk_waypoints", blocked_walk)
        monkeypatch.setattr(engine, "_relocate_to_fresh_collar_site", lambda *a, **k: pytest.fail("abandoned the deep shaft"))
        result = engine.run(target="iron", target_y=-20, goal_count=1, max_duration_s=20)

    assert engine.collar_node_id == collar.node_id
    if dry_turn:
        assert result["stop_reason"] == "quota_reached"
        assert not walks
    else:
        assert result["stop_reason"] == "backtrack_blocked"
        assert walks == [route]


@pytest.mark.parametrize("local_branch_available", [True, False])
def test_blocked_target_level_backtrack_preserves_the_shaft(monkeypatch, local_branch_available) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, -54.0, 0.5), surface_y=64,
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    for x in (0, 2):
        world.blocks[(x, -54, 0)] = world.blocks[(x, -53, 0)] = "air"
    world.blocks[(4, -54, 0)] = "deepslate_diamond_ore"
    with BotSession("tok", bot_username="BlockedAtTargetDepth", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 64, -120), parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", "west", 118)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        junction = engine._create_node(
            kind="spine", anchor=(0, -54, -2), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, (0, -54, -2)]), branches=[],
        )
        if local_branch_available:
            engine._create_node(
                kind="spine", anchor=(2, -54, 0), parent_id=junction.node_id,
                edge_from_parent=ShaftEdge("spine_forward", "east", [junction.anchor, (2, -54, 0)]),
                branches=[mining.BranchSpec("spine_forward", "east", 4)],
            )
        spent = engine._create_node(
            kind="spine", anchor=(0, -54, 0), parent_id=junction.node_id,
            edge_from_parent=ShaftEdge("branch_leaf", "south", [junction.anchor, (0, -54, 0)]), branches=[],
        )
        relocations = []

        def blocked_walk(waypoints, *, reverse=False, **kwargs):
            assert reverse and waypoints == spent.edge_from_parent.waypoints
            world.pos = [2.5, -54.0, 0.5]
            return False, "backtrack_blocked"

        def interrupted_relocation(*args, **kwargs):
            relocations.append(world.pos)
            world.pos = [3.5, -54.0, 0.5]
            engine.bot.status()
            clock.now = engine._deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_walk_waypoints", blocked_walk)
        monkeypatch.setattr(engine, "_relocate_to_fresh_collar_site", interrupted_relocation)
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=32)

    if local_branch_available:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == 1
        assert not relocations
    else:
        assert result["stop_reason"] == "time_slice_progress"
        assert len(relocations) == 1
    assert result["pos_after"][1] == -54
    assert engine.collar_node_id == collar.node_id
    assert spent.node_id in engine.nodes


def test_explore_rejoins_a_recorded_stair_far_from_its_collar(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 11.0, 53.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    stairs = [(0, y, 64 - y) for y in range(64, 3, -1)]
    for x, y, z in stairs:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(1, 3, 60)] = "ancient_debris"
    with BotSession("tok", bot_username="FarFromCollar", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=stairs[0], parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=stairs[-1], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", stairs[::2]),
            branches=[mining.BranchSpec("stair_down", "east", 2)],
        )

        def restore_unrelated_shaft(*args, **kwargs):
            pytest.fail("discarded the active route while standing on one of its steps")

        monkeypatch.setattr(engine, "_try_restore_saved_shaft", restore_unrelated_shaft)
        result = engine.run(target="ancient_debris", target_y=3, goal_count=1, max_duration_s=32)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert result["pos_after"][1] <= 4
    assert engine.collar_node_id == collar.node_id


@pytest.mark.parametrize("rejoinable", [True, False])
def test_explore_keeps_a_far_collar_when_displaced_near_its_deep_tip(monkeypatch, rejoinable: bool) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    collar_anchor = (0, 75, 0)
    tip_anchor = (-2, 21, -50)
    displaced = (-8, 15, -53)
    world = _MockMineWorld(start_pos=(-7.5, 15.0, -52.5), surface_y=75)
    for x, y, z in (displaced, tip_anchor):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(-3, 21, -50)] = "iron_ore"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="RejoinFarDeepTip", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=collar_anchor, parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar_anchor[1]
        tip = engine._create_node(
            kind="switchback", anchor=tip_anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar_anchor, (0, 25, -50), tip_anchor]),
            branches=[BranchSpec("stair_down", "west", 75)],
        )
        move_exact = engine._move_exact
        attempted_rejoins = []

        def move_to_tip(cell, **kwargs):
            if tuple(math.floor(v) for v in world.pos) == displaced:
                attempted_rejoins.append(cell)
                if not rejoinable:
                    return False
            return move_exact(cell, **kwargs)

        def open_new_collar(*args, **kwargs):
            pytest.fail("opened a new underground collar beside the recorded descent")

        monkeypatch.setattr(engine, "_move_exact", move_to_tip)
        monkeypatch.setattr(engine, "_ensure_solid_collar_anchor", open_new_collar)
        result = engine.run(target="iron", target_y=-54, goal_count=1, max_duration_s=20)

    assert attempted_rejoins == [tip_anchor]
    assert engine.nodes[collar.node_id] is collar
    if rejoinable:
        assert result["stop_reason"] == "quota_reached"
    else:
        assert result["stop_reason"] == "needs_surface_climb"
        assert engine.nodes[tip.node_id] is tip
        assert not world.dig_log


@pytest.mark.parametrize(
    ("floor", "adjacent", "face", "reason"),
    [
        ("obsidian", "air", "air", "completed"),
        ("basalt", "air", "air", "completed"),
        ("stone", "air", "air", "completed"),
        ("air", "air", "air", "bridge_gap_ahead"),
        ("lava", "air", "air", "unsafe_or_unknown_floor"),
        ("obsidian", "lava", "air", "fluid_adjacent"),
        ("obsidian", "gravel", "air", "falling_material_adjacent"),
        ("obsidian", "air", "obsidian", "unsupported_face_block"),
        ("basalt", "lava", "air", "fluid_adjacent"),
        ("basalt", "air", "basalt", "unsupported_face_block"),
        ("polished_blackstone_bricks", "air", "air", "completed"),
        ("polished_blackstone_bricks", "lava", "air", "fluid_adjacent"),
        ("polished_blackstone_bricks", "air", "polished_blackstone_bricks", "unsupported_face_block"),
        ("nether_bricks", "air", "air", "completed"),
        ("nether_bricks", "lava", "air", "fluid_adjacent"),
        ("nether_bricks", "air", "nether_bricks", "unsupported_face_block"),
    ],
)
def test_level_tunnel_leaves_nether_platform_on_verified_support(floor, adjacent, face, reason) -> None:
    world = _MockMineWorld(start_pos=(1856.5, 64.0, 1808.5), surface_y=64, dimension="the_nether")
    world.blocks[(1857, 63, 1808)] = floor
    world.blocks[(1857, 64, 1807)] = adjacent
    world.blocks[(1857, 64, 1808)] = face

    with BotSession("tok", bot_username="NetherTunnel", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).dig_level_tunnel("east", 1)

    moved = reason == "completed"
    assert result["stop_reason"] == reason
    assert result["steps_done"] == int(moved)
    assert result["pos_before"] == [1856.5, 64.0, 1808.5]
    assert result["pos_after"] == [1857.5 if moved else 1856.5, 64.0, 1808.5]
    assert not world.dig_log


@pytest.mark.parametrize("floor", ["obsidian", "basalt", "polished_blackstone_bricks", "nether_bricks"])
def test_level_tunnel_stops_if_support_disappears_after_digging(floor) -> None:
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether")
    world.blocks[(1, 63, 0)] = floor
    world.blocks[(1, 64, 0)] = "stone"

    def remove_support_after_dig(request: httpx.Request) -> httpx.Response:
        response = world.handler(request)
        if request.url.path.endswith("/dig") and world.dig_log:
            world.blocks[(1, 63, 0)] = "air"
        return response

    with BotSession("tok", bot_username="NetherTunnel", transport=httpx.MockTransport(remove_support_after_dig)) as bot:
        result = MacroSkills(bot).dig_level_tunnel("east", 1)

    assert result["stop_reason"] == "support_changed"
    assert result["steps_done"] == 0
    assert result["pos_before"] == result["pos_after"] == [0.5, 64.0, 0.5]
    assert [pos for pos, _, _ in world.dig_log] == [(1, 64, 0)]


def test_level_tunnel_deadline_covers_each_probe_and_skips_final_io(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining
    from mc_agent.skills.tunnel import dig_level_tunnel

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=21)
    calls: list[float] = []

    def slow_handler(request):
        calls.append(clock.now)
        clock.now += 0.2
        return world.handler(request)

    with BotSession("tok", bot_username="TimedTunnel", transport=httpx.MockTransport(slow_handler)) as bot:
        result = dig_level_tunnel(bot, "east", 8, deadline=1001.0)
        before_expired_call = len(calls)
        expired = dig_level_tunnel(bot, "east", 8, deadline=999.0)
    assert result["ok"] is False and result["requires_replan"] is True
    assert result["stop_reason"] == expired["stop_reason"] == "time_slice"
    assert clock.now <= 1001.01
    assert all(t < 1001.0 for t in calls)
    assert not world.dig_log
    assert len(calls) == before_expired_call


@pytest.mark.parametrize("local_collar", [False, True])
def test_deep_idle_slice_keeps_the_shaft_and_tries_its_other_branch(monkeypatch, local_collar) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=64,
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    anchor = (0, 16, 0)
    world.blocks[anchor] = world.blocks[(0, 17, 0)] = "air"
    world.blocks[(0, 14, -2)] = "diamond_ore"
    with BotSession("tok", bot_username="DeepIdleBranch", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar_pos = (0, 16, 1) if local_collar else (0, 64, 48)
        edge_kind = "spine_forward" if local_collar else "stair_down"
        collar = engine._create_node(kind="collar", anchor=collar_pos, parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 64
        tip = engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge(edge_kind, "north", [collar.anchor, anchor]),
            branches=[mining.BranchSpec("stair_down", direction, 70) for direction in ("east", "north")],
        )
        dig_stair = engine._dig_stair_down_edge
        relocation_starts = []

        def dig_edge(*, direction, **kwargs):
            if direction == "east":
                assert engine.bot.call("dig", {"pos": [1, 16, 0], "collect": False})["dug"]
                clock.now += mining.MAX_IDLE_SECONDS
                return {
                    "steps_done": 0, "stop_reason": "movement_not_confirmed", "breaches_sealed": 0,
                    "catch_placed": 0, "deferred_added": 0, "mined_ores": [],
                    "waypoints": [anchor], "end_anchor": anchor,
                }
            return dig_stair(direction=direction, **kwargs)

        def no_new_site(pos, **kwargs):
            relocation_starts.append(pos)
            return pos

        monkeypatch.setattr(engine, "_dig_stair_down_edge", dig_edge)
        monkeypatch.setattr(engine, "_relocate_to_fresh_collar_site", no_new_site)
        args = dict(target="diamond", target_y=-54, goal_count=1, max_duration_s=55)
        first = engine.run(**args)
        assert first["stop_reason"] == "no_progress"
        assert first["pos_before"] == first["pos_after"]
        assert first["gained"] == {} and first["target_gained"] == 0
        assert first["dug_count"] == len(world.dig_log) == 1
        assert engine.active_node_id == tip.node_id
        assert tip.branches[1].status == "pending"
        assert collar.anchor not in engine.exhausted_collars
        assert "同层" in first["message"]

        second = engine.run(**args)

    assert second["stop_reason"] == "quota_reached"
    assert second["target_gained"] == 1
    assert engine.collar_node_id == collar.node_id
    assert not relocation_starts


def test_collar_at_nether_target_layer_can_mine_over_bedrock() -> None:
    world = _MockMineWorld(start_pos=(0.5, 5.0, 0.5), surface_y=5, dimension="the_nether")
    for x in range(-9, 10):
        for z in range(-9, 10):
            world.blocks[(x, 4, z)] = "bedrock"
    world.blocks[(1, 5, 0)] = "nether_quartz_ore"
    with BotSession("tok", bot_username="BedrockFloor", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="quartz", target_y=5, goal_count=1, max_duration_s=5.0)
    assert result["goal_met"] is True
    assert world._count("nether_quartz_ore") == 1
    assert all(name != "bedrock" for _, name, _ in world.dig_log)


def test_collar_reports_when_solid_station_is_outside_geofence() -> None:
    world = _MockMineWorld(start_pos=(0.5, 70.0, 0.5), surface_y=70)
    fence = VoidGeofence(center_x=100.0, center_z=100.0, radius=15.0, margin=5.0)
    with BotSession("tok", bot_username="FenceAtCollar", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", geofence=fence, max_duration_s=5.0)
    assert result["stop_reason"] == "void_geofence_blocked"
    assert result["error"]["code"] == "void_geofence_blocked"
    assert result["tree"]["node_count"] == 0
    assert not world.dig_log


@pytest.mark.parametrize("has_safe_offset", [False, True])
def test_explore_does_not_open_a_fake_collar_after_a_disconnected_move(monkeypatch, has_safe_offset) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(60.5, 20.0, 0.5), surface_y=20)
    for dx, dz in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        world.blocks[(60 + dx, 19, dz)] = "bedrock"
    if has_safe_offset:
        world.blocks[(61, 19, 0)] = "stone"
        world.blocks[(61, 18, 0)] = "air"
        world.blocks[(61, 17, 0)] = "air"
    with BotSession("tok", bot_username="BlockedCollar", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        old_collar = eng._create_node(
            kind="collar", anchor=(0, 20, 0), parent_id=None, edge_from_parent=None,
            branches=[BranchSpec(kind="stair_down", direction="east", steps=4)],
        )
        eng.collar_node_id = old_collar.node_id
        monkeypatch.setattr(eng, "_find_solid_collar_candidates", lambda *args, **kwargs: [])
        result = eng.run(target="iron", target_y=16, max_duration_s=5.0)
    assert result["stop_reason"] == "no_viable_collar_direction"
    assert result["collar_blockers"]["north"] == {
        "code": "fluid_or_bedrock", "pos": [60, 19, -1], "block": "bedrock", "relation": "in_path",
    }
    assert set(result["collar_blockers"]) == {"north", "east", "south", "west"}
    if has_safe_offset:
        assert result["collar_blockers"]["east"]["code"] == "missing_support"
        assert result["suggested_recovery_pos"] == [61.5, 20, 0.5]
        assert "[61.5, 20, 0.5]" in result["message"]
    else:
        assert "suggested_recovery_pos" not in result
        assert "另一处实心站位" in result["message"]
    assert result["tree"]["node_count"] == 0
    assert world.pos == [60.5, 20.0, 0.5]
    assert not world.dig_log


@pytest.mark.parametrize("dz", [-1, 1], ids=["north", "south"])
@pytest.mark.parametrize("dy", [-1, 1], ids=["step_down", "hop_up"])
def test_stair_control_aims_toward_the_landing(dz, dy, monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=30)
    start = (0, 20, 0)
    target = (0, 20 + dy, dz)
    for x, y, z in (start, target):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    for y in range(-18, 23):
        world.blocks[(0, y, -dz)] = "air"
    controls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path in {"move_to", "look"}:
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found"}})
        if path == "control":
            body = json.loads(request.content)
            controls.append(body)
            x = math.floor(world.pos[0] - math.sin(body["yaw"]))
            z = math.floor(world.pos[2] - math.cos(body["yaw"]))
            y = max(20, target[1])
            while world.block_at((x, y - 1, z)) == "air":
                y -= 1
            world.pos = [x + 0.5, float(y), z + 0.5]
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="StairControlHeading", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        assert engine._step_to(target, from_cell=start) is True

    assert world.pos == [0.5, float(target[1]), dz + 0.5]
    assert len(controls) == 1

