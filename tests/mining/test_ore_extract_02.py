from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

def test_entry_gate_recrafts_when_both_stone_pickaxes_are_near_breaking(monkeypatch) -> None:
    clock = _MiningClock()
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", clock.sleep)
    world = _MockMineWorld(
        start_pos=(0.5, 20.0, 0.5), surface_y=64, track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [2, 131]},
            {"name": "stone_pickaxe", "count": 1, "slot": 10, "durability": [3, 131]},
            {"name": "cobblestone", "count": 32, "slot": 11},
            {"name": "stick", "count": 12, "slot": 12},
            {"name": "crafting_table", "count": 1, "slot": 13},
        ],
    )
    world.blocks[(0, 20, 0)] = world.blocks[(0, 21, 0)] = "air"
    world.blocks[(0, 20, 1)] = "crafting_table"
    world.blocks[(2, 18, 0)] = "iron_ore"
    with BotSession("tok", bot_username="WornStoneStock", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills.mining_dfs._init_collar((0, 20, 0), target_y=18, geofence=None, viable_dirs=["east"])
        result = skills.mine_dfs(target="iron", target_y=18, goal_count=1, max_duration_s=30)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == world._count("raw_iron") == 1
    assert world.craft_log == [("stone_pickaxe", 1)]


@pytest.mark.parametrize("height", [-2, 1, 2])
def test_descent_stops_before_entering_pointed_dripstone_column(monkeypatch, height) -> None:
    world = _MockMineWorld(start_pos=(0.5, 26.0, 0.5), surface_y=64)
    world.blocks[(0, 26, 0)] = world.blocks[(0, 27, 0)] = "air"
    hazard = (0, 26 + height, -1)
    world.blocks[hazard] = "pointed_dripstone"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="DripstoneDescent", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        result = engine._dig_stair_down_edge(
            direction="north", max_steps=1, target_y=16, target_blocks=("iron_ore",),
            parent_node_id=0, geofence=None, deadline=math.inf, min_hp_ref=[20.0],
        )

    assert result["steps_done"] == 0
    assert result["stop_reason"] == ("cave_or_drop_ahead" if height == -2 else "falling_material_adjacent")
    assert world.pos == [0.5, 26.0, 0.5]
    assert world.block_at(hazard) == "pointed_dripstone"
    assert not world.dig_log


def test_drop_reachability_requires_standing_next_to_the_ore() -> None:
    """The dig API ignores reach, so the engine must gate on pickup range by itself."""
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=16)
    with BotSession("tok", bot_username="ReachBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        cache = {}

        # face-adjacent ore: always fine
        assert eng._drop_cell_reachable((1, 16, 0), (0, 16, 0), cache) is True

        # 4 blocks away behind solid stone: not collectable
        solid = {(x, 16, 0): "stone" for x in range(-6, 7)}
        assert eng._drop_cell_reachable((4, 16, 0), (0, 16, 0), dict(solid)) is False

        # 2 blocks away but one neighbour is open and one step from us -> we can walk next to it
        cache2 = dict(solid)
        cache2[(3, 16, 0)] = "air"
        assert eng._drop_cell_reachable((4, 16, 0), (2, 16, 0), cache2) is True


@pytest.mark.parametrize("ore, item, y", [("iron_ore", "raw_iron", 16), ("deepslate_diamond_ore", "diamond", -54)])
@pytest.mark.parametrize("route", ["open", "wall", "low_ceiling", "water", "unsupported", "wrong_landing"])
def test_vein_hook_approaches_distant_ore_only_along_supported_corridor(monkeypatch, ore, item, y, route) -> None:
    world = _MockMineWorld(
        start_pos=(0.5, float(y), 0.5), surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [240, 250]}],
        simulate_realistic_reach_and_pockets=True,
    )
    anchor, ore_pt = (0, y, 0), (5, y, 0)
    for x in range(5):
        world.blocks[(x, y, 0)] = "air"
        world.blocks[(x, y + 1, 0)] = "air"
    world.blocks[ore_pt] = ore
    obstruction = {
        "wall": ((2, y, 0), "stone"),
        "low_ceiling": ((2, y + 1, 0), "stone"),
        "water": ((2, y, 0), "water"),
        "unsupported": ((2, y - 1, 0), "air"),
    }.get(route)
    if obstruction:
        pos, block = obstruction
        world.blocks[pos] = block
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    moves = []

    def handler(request):
        if request.url.path.endswith("/move_to"):
            body = json.loads(request.content)
            goal = tuple(math.floor(body[axis]) for axis in ("x", "y", "z"))
            moves.append(goal)
            if route == "wrong_landing":
                world.pos[:] = [3.5, float(y), 0.5]
                if goal != anchor:
                    return httpx.Response(200, json={"ok": True, "result": {"pos": list(world.pos)}})
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content)["pos"]) == ore_pt:
            assert world.pos == [4.5, float(y), 0.5]
        return world.handler(request)

    with BotSession("tok", bot_username="DistantVein", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.strategy = "radar"
        engine.target_y = y
        result = engine._run_vein_hook(
            anchor, target_blocks=(ore,), current_node_id=0, geofence=None, cache={},
        )

    assert world.pos == [0.5, float(y), 0.5]
    if route == "open":
        assert result["mined_coords"] == [list(ore_pt)]
        assert world._count(item) == 1
        assert [pos for pos, _, _ in world.dig_log] == [ore_pt]
        assert moves[0] == (4, y, 0)
    else:
        assert result["mined_coords"] == []
        assert world.block_at(ore_pt) == ore
        assert world._count(item) == 0
        assert not world.dig_log
        assert moves == ([(4, y, 0), anchor] if route == "wrong_landing" else [])


@pytest.mark.parametrize(("side_floor", "pickup"), [
    ("deepslate", "passive"), ("deepslate", "step_down"), ("deepslate", "blocked_step"),
    ("air", "blocked"), ("water", "blocked"),
])
def test_vein_hook_steps_off_a_diamond_ore_floor_before_digging(monkeypatch, side_floor, pickup) -> None:
    anchor, side, ore_pt = (0, -54, 0), (0, -54, 1), (0, -55, 0)
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.6),
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9},
                         {"name": "cobblestone", "count": 32, "slot": 10}],
    )
    for x, y, z in (anchor, side):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(0, -55, 1)] = side_floor
    world.blocks[ore_pt] = "deepslate_diamond_ore"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    digs = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dig"):
            body = json.loads(request.content)
            digs.append((tuple(body["pos"]), list(world.pos), body["collect"]))
            response = world.handler(request)
            if tuple(body["pos"]) == ore_pt and pickup == "blocked_step":
                world.blocks[(0, -53, 0)] = "gravel"
            return response
        if pickup == "passive" and request.url.path.endswith("/inventory") and world.pos == [0.5, -54.0, 1.5]:
            # Simulate the nearby item entering pickup range without moving into the hole.
            for entity in world.entities[:]:
                if entity.get("item") == "diamondx1" and math.dist(entity["pos"], world.pos) < 1.4:
                    world._add_item("diamond", 1)
                    world.entities.remove(entity)
        return world.handler(request)

    with BotSession("tok", bot_username="MineOreFromSide", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.strategy = "radar"
        engine.target_y = -54
        result = engine._run_vein_hook(
            anchor, target_blocks=("deepslate_diamond_ore",), current_node_id=0, geofence=None, cache={},
        )

    if pickup == "blocked_step":
        assert digs == [(ore_pt, [0.5, -54.0, 1.5], False)]
        assert result["mined_coords"] == [] and world._count("diamond") == 0
        assert world.visited_blocks == [side] and not world.place_log
        assert [row["reason"] for row in engine.drop_receipts.values()] == ["unsafe_route"]
    elif side_floor == "deepslate":
        assert digs == [(ore_pt, [0.5, -54.0, 1.5], False)]
        assert result["mined_coords"] == [list(ore_pt)]
        assert world._count("diamond") == 1
        assert world.place_log == [(ore_pt, "cobblestone")]
        assert world.pos == [0.5, -54.0, 0.5]
        if pickup == "step_down":
            assert world.visited_blocks[:4] == [side, ore_pt, side, anchor]
            assert world.visited_blocks.count(ore_pt) == 1
    else:
        assert not digs and not world.visited_blocks and not world.place_log
        assert result["mined_coords"] == [] and world.block_at(ore_pt) == "deepslate_diamond_ore"


@pytest.mark.parametrize("floor", ["deepslate", "water"])
def test_explore_recovers_from_one_block_below_an_ore_branch(monkeypatch, floor) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    anchor, broken_support = (0, -54, 0), (0, -55, 0)
    world = _MockMineWorld(
        start_pos=(0.5, -55.0, 0.6),
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9},
                         {"name": "cobblestone", "count": 32, "slot": 10},
                         {"name": "diamond", "count": 2, "slot": 11}],
    )
    for y in (-55, -54, -53):
        world.blocks[(0, y, 0)] = "air"
    world.blocks[(0, -56, 0)] = floor
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    repaired = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/move_to") and world.pos[1] == -55:
            return httpx.Response(200, json={"ok": False, "error": {"code": "unsafe_landing", "message": "no support"}})
        return world.handler(request)

    with BotSession("tok", bot_username="RejoinDiamondSpine", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 64, 118), parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        engine._create_node(
            kind="spine", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, anchor]),
            branches=[BranchSpec("spine_forward", "north", 1)],
        )

        def pillar_step(**kwargs):
            assert kwargs["vertical"] == "pillar" and kwargs["steps"] == 1
            repaired.append(broken_support)
            world.blocks[broken_support] = "cobblestone"
            world.pos = [0.5, -54.0, 0.5]
            return {"placed_count": 1}

        monkeypatch.setattr(engine.skills, "dig_staircase", pillar_step)
        result = engine.run(target="diamond", target_y=-54, goal_count=4, max_duration_s=5)

    assert result["target_gained"] == 0 and world._count("diamond") == 2
    assert engine.collar_node_id == collar.node_id
    if floor == "deepslate":
        assert repaired == [broken_support]
        assert (0, -54, -1) in world.visited_blocks
    else:
        assert result["stop_reason"] == "cannot_reach_nearest_node"
        assert result["pos_after"][1] == -55
        assert not repaired and not world.dig_log


@pytest.mark.parametrize("block,solid_floor", [("cobblestone", True), ("cobblestone", False), ("furnace", True)])
def test_explore_rejoins_filled_recorded_step_directly_below(monkeypatch, block, solid_floor) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    anchor = (0, 42, 0)
    world = _MockMineWorld(start_pos=(0.5, 43.0, 0.5), surface_y=64)
    world.blocks[anchor] = block
    world.blocks[(0, 43, 0)] = world.blocks[(0, 44, 0)] = "air"
    if not solid_floor:
        world.blocks[(0, 41, 0)] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="FilledStep", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -22), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        node = engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, anchor]),
            branches=[BranchSpec("stair_down", "south", 2)],
        )
        engine.active_node_id = node.node_id
        engine.collar_ceiling_y = 64
        engine.target_y = 16
        result = engine.run(target="iron", target_y=16, goal_count=1, max_duration_s=6)

    if block == "cobblestone" and solid_floor:
        assert any(pos == anchor for pos, _, _ in world.dig_log)
        assert result["pos_after"][1] < 43
        assert result["stop_reason"] != "needs_surface_climb"
    else:
        assert world.block_at(anchor) == block
        assert result["stop_reason"] == "needs_surface_climb"
    assert engine.collar_node_id == collar.node_id


def test_shallower_layer_accepts_new_observed_ore_below_the_requested_level(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    anchor, iron = (0, 22, 0), (3, -2, 0)
    world = _MockMineWorld(start_pos=(0.5, 22.0, 0.5), surface_y=63)
    world.blocks[anchor] = world.blocks[(0, 23, 0)] = "air"
    world.blocks[iron] = "iron_ore"
    world.blocks[(3, -2, 1)] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="ObservedShallowLayerTarget", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.strategy = "radar"
        engine.target_y = -54
        collar = engine._create_node(
            kind="collar", anchor=anchor, parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("stair_down", "north", 76, branch_target_y=-54)],
        )
        engine.collar_node_id = collar.node_id
        assert engine._replan_shallower_layer(
            bot.status(), anchor, target_y=16, target_blocks=("iron_ore", "deepslate_iron_ore"),
            max_tier=2, geofence=None,
        )

        assert engine.target_y == 16
        assert engine.active_node_id == collar.node_id
        assert iron in engine.radar_targets
        assert {branch.branch_target_y for branch in collar.branches} == {-2}
        assert not world.dig_log and not world.visited_blocks

        result = engine.run(target="iron", target_y=16, goal_count=4, strategy="radar", max_duration_s=6)

    assert result["ok"] and world.dig_log
    assert result["pos_after"][1] < 16


def test_repeated_explore_below_new_layer_does_not_resume_stale_saved_descent(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    collar_anchor, tip = (0, 64, 0), (0, -35, 99)
    route = [(0, y, 64 - y) for y in range(64, tip[1] - 1, -1)]
    world = _MockMineWorld(start_pos=(0.5, -35.0, 99.5), surface_y=64)
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="SavedLayerBelow", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=collar_anchor, parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        engine.target_y = 16
        old_edge = ShaftEdge("stair_down", "south", route)
        old_tip = engine._create_node(
            kind="switchback", anchor=tip, parent_id=collar.node_id, edge_from_parent=old_edge,
            branches=[BranchSpec("stair_down", "south", 19, branch_target_y=-54)],
        )
        engine._archive_current_shaft_to_ledger()
        engine._clear_active_tree_only()

        results = [engine.run(target="iron", target_y=16, goal_count=4, max_duration_s=6) for _ in range(2)]

    assert not world.dig_log and not world.visited_blocks
    assert [result["stop_reason"] for result in results] == ["layer_switch_upward_refused"] * 2
    assert engine.nodes[old_tip.node_id].edge_from_parent == old_edge


def test_default_dfs_slice_allows_slow_ore_dig_within_budget(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64)
    ore = (1, 64, 0)
    world.blocks[ore] = "iron_ore"
    dig_timeouts = []

    def handler(request):
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content)["pos"]) == ore:
            timeouts = request.extensions["timeout"]
            dig_timeouts.append((clock.now, timeouts))
            if timeouts["read"] < 8.0:
                clock.now += timeouts["read"]
                raise httpx.ReadTimeout("ore dig still running", request=request)
            clock.now += 8.0
        return world.handler(request)

    with BotSession("tok", bot_username="SlowOreDig", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", target_y=64, goal_count=1)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert clock.now <= 1032.0
    [(started, timeouts)] = dig_timeouts
    assert 2 * sum(timeouts.values()) + 0.15 <= 1031.5 - started + 1e-9


@pytest.mark.parametrize("outcome", ["already_removed", "removed_on_retry", "still_solid"])
def test_mid_slice_dig_timeout_rechecks_block_before_one_retry(monkeypatch, outcome) -> None:
    from mc_agent.skills.mining_dfs import _BudgetSession

    clock = _MiningClock()
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=64)
    first = (0, 16, 1)
    cell = (1, 16, 0)
    attempts = 0
    stops = 0

    def handler(request):
        nonlocal attempts, stops
        if request.url.path.endswith("/stop"):
            stops += 1
            return httpx.Response(200, json={"ok": True, "result": {"released": True}})
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content)["pos"]) == cell:
            attempts += 1
            if attempts == 1 or outcome == "still_solid":
                if outcome == "already_removed":
                    world.blocks[cell] = "air"
                raise httpx.ReadTimeout("dig reply missing", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="DigTimeoutRetry", transport=httpx.MockTransport(handler)) as bot:
        budget = _BudgetSession(bot, deadline=clock.now + 40)
        engine = MacroSkills(budget.session).mining_dfs
        assert budget.call("dig", {"pos": list(first), "collect": False})["dug"]
        ok, reason, _, _ = engine._dig_cell_safely(cell, collect_rock=False, parent_node_id=0, cache={})

    assert attempts == (1 if outcome == "already_removed" else 2)
    assert stops == (2 if outcome == "still_solid" else 1)
    assert ok == (outcome != "still_solid")
    assert (world.block_at(cell) == "air") == ok
    assert reason == (None if ok else "dig_failed")


def test_first_vein_timeout_cools_ore_and_allows_next_slice_to_dig(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]}],
    )
    origin = (0, -54, 0)
    ore = (0, -54, -2)
    blockers = [(0, -54, -1), (1, -54, -2)]
    world.blocks[origin] = world.blocks[(0, -53, 0)] = "air"
    world.blocks[ore] = "deepslate_diamond_ore"
    world.blocks[(0, -53, -2)] = "air"
    attempted = []
    clock = _MiningClock()

    def handler(request):
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content).get("pos", [])) in blockers:
            raise httpx.ReadTimeout("ore blocker did not respond", request=request)
        response = world.handler(request)
        if request.url.path.endswith("/dig") and world.dig_log:
            clock.sleep(16)
        return response

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="VeinDigTimeout", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        engine = skills.mining_dfs
        node = engine._create_node(
            kind="spine", anchor=origin, parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("spine_forward", "east", 2)],
        )
        engine.collar_node_id = engine.active_node_id = node.node_id
        engine.target_y = -54

        def peel_ore(pt, *args, **kwargs):
            assert pt == ore
            blocker = next(pt for pt in blockers if not engine.skills._is_block_avoided(pt))
            attempted.append(blocker)
            engine.bot.call("dig", {"pos": list(blocker), "collect": False})
            pytest.fail("the mock dig must time out")

        monkeypatch.setattr(engine, "_mine_vein_ore_cell", peel_ore)
        first = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=16)
        assert first["timeout_path"] == "dig" and first["dug_count"] == 0
        assert skills._is_block_avoided(blockers[0])
        engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=16)

    assert attempted == [blockers[0]]
    assert any(pos[0] > 0 for pos, _, _ in world.dig_log)
    assert world.block_at(ore) == "deepslate_diamond_ore"


@pytest.mark.parametrize(("start_y", "end_y"), [(28, 20), (28, 45), (-54, -26)])
def test_deep_explore_reports_descent_or_lost_depth_without_raising_budget(monkeypatch, start_y, end_y) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, float(start_y), 0.5), surface_y=64)
    with BotSession("tok", bot_username="DescentReceipt", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def interrupted(*, deadline, **kwargs):
            assert deadline == 1031.5
            engine.bot.status()
            world.pos[1] = end_y
            engine.bot.status()
            clock.now = deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_run", interrupted)
        result = engine.run(target="diamond", target_y=-54, max_duration_s=32)

    assert result["budget_effective_s"] == 32
    assert clock.now <= 1032.0
    assert result["goal_met"] is False
    if end_y > start_y:
        assert result["stop_reason"] == "depth_reset"
        assert result["progress_lost"] is True
        assert result["ok"] is False and result["requires_replan"] is True
    else:
        assert result["stop_reason"] == "time_slice_progress"
        assert result["suggested_next"]["retry_with"]["max_duration_s"] == 60


@pytest.mark.parametrize("prune_reason", ["fluid_or_bedrock", "movement_not_confirmed"])
def test_exhausted_descent_tries_the_other_lateral_route_before_backtracking(monkeypatch, prune_reason) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 28.0, 0.5), surface_y=64)
    world.blocks[(0, 28, 0)] = world.blocks[(0, 29, 0)] = "air"
    world.blocks[(1, 27, 0)] = "bedrock"
    world.blocks[(1, 27, 3)] = "iron_ore"
    with BotSession("tok", bot_username="SecondLateralRoute", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -36), parent_id=None,
                                     edge_from_parent=None, branches=[mining.BranchSpec("stair_down", "east", 40)])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        anchor = (0, 28, 0)
        engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, anchor]),
            branches=[
                mining.BranchSpec("stair_down", "east", 48, status="pruned", prune_reason=prune_reason),
                mining.BranchSpec("stair_down", "south", 48, status="pruned", prune_reason="protected_region"),
                mining.BranchSpec("stair_down", "west", 48, status="pruned", prune_reason="protected_region"),
                mining.BranchSpec("offset_retry", "east", 48, status="pruned", sidestep_dir="north"),
            ],
        )
        monkeypatch.setattr(engine, "_walk_waypoints", lambda *a, **k: pytest.fail("backtracked before trying south"))
        result = engine.run(target="iron", target_y=-20, goal_count=1, max_duration_s=20)

    assert result["stop_reason"] == "quota_reached"
    assert engine.collar_node_id == collar.node_id
    assert result["pos_after"][1] <= 28


@pytest.mark.parametrize("deeper_branch_pending", [True, False])
@pytest.mark.parametrize("from_archive", [False, True])
def test_explore_from_low_waypoint_does_not_rejoin_a_shallow_active_node(
    monkeypatch, deeper_branch_pending, from_archive,
) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 44.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    route = [(0, y, 64 - y) for y in range(64, 16, -1)]
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(1, 16, 47)] = "ancient_debris"
    with BotSession("tok", bot_username="ExploreFromDeepWayPoint", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=route[0], parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", "east", 49)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=route[-1], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", route),
            branches=[mining.BranchSpec("stair_down", "east", 2)] if deeper_branch_pending else [],
        )
        engine.active_node_id = collar.node_id
        engine._record_corridor((0, 20, 44))
        if from_archive:
            engine.target_y = 15
            engine._archive_current_shaft_to_ledger()
            engine._clear_active_tree_only()
        walk = engine._walk_waypoints

        def walk_at_depth(waypoints, **kwargs):
            assert max(y for _, y, _ in waypoints) <= 20
            return walk(waypoints, **kwargs)

        monkeypatch.setattr(engine, "_walk_waypoints", walk_at_depth)
        result = engine.run(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=32)

    if deeper_branch_pending:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == 1
    else:
        assert result["stop_reason"] == "needs_surface_climb"
        assert world.pos == [0.5, 20.0, 44.5]
        assert not world.dig_log
    assert engine.collar_node_id == collar.node_id


@pytest.mark.parametrize("dry_floor", [True, False])
@pytest.mark.parametrize(
    ("collar_pos", "tip_y", "start", "rejoin_attempted"),
    [((-2, 108, 0), 105, (15, 90, 0), False), ((-42, 93, 0), 51, (5, 45, 0), True)],
)
def test_explore_uses_a_verified_descent_when_the_saved_tip_is_above_it(
    monkeypatch, dry_floor, collar_pos, tip_y, start, rejoin_attempted,
) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    x, y, z = start
    world = _MockMineWorld(start_pos=(x + 0.5, float(y), z + 0.5), surface_y=111)
    world.blocks[start] = world.blocks[(x, y + 1, z)] = "air"
    if dry_floor:
        world.blocks[(x, y - 1, z - 1)] = "iron_ore"
    else:
        world.blocks[(x, y - 1, z)] = "water"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="DownFromStaleTip", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=collar_pos, parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 111 if not rejoin_attempted else collar_pos[1]
        tip_pos = (0, tip_y, 0)
        tip = engine._create_node(
            kind="switchback", anchor=tip_pos, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [collar.anchor, tip_pos]),
            branches=[BranchSpec("stair_down", "east", tip_y - 16)],
        )
        attempts = []

        def failed_rejoin(node_id, **kwargs):
            attempts.append(node_id)
            return False, "cannot_reach_nearest_node"

        monkeypatch.setattr(engine, "_resume_shaft_tip", failed_rejoin)
        result = engine.run(target="iron", target_y=16, goal_count=1, max_duration_s=5)

    assert attempts == ([tip.node_id] if rejoin_attempted else [])
    if dry_floor:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == 1
        assert engine.nodes[engine.collar_node_id].anchor == start
        assert any(shaft["collar_anchor"] == list(collar.anchor) for shaft in engine.ledger.saved_shafts)
    else:
        assert result["stop_reason"] == "needs_surface_climb"
        assert engine.active_node_id == tip.node_id
        assert not world.dig_log and not world.visited_blocks
    assert result["pos_after"][1] <= start[1]


@pytest.mark.parametrize("dry_floor", [True, False])
def test_switching_to_deeper_ore_recovers_from_an_unreachable_spine(monkeypatch, dry_floor) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    start = (-3, 8, -1)
    world = _MockMineWorld(
        start_pos=(-2.5, 8.0, -0.5), surface_y=74,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9}],
    )
    world.blocks[start] = world.blocks[(-3, 9, -1)] = "air"
    if dry_floor:
        world.blocks[(-3, 7, -2)] = "diamond_ore"
    else:
        world.blocks[(-3, 7, -1)] = "water"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="RetargetOffSpine", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 74, 65), parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        tip = engine._create_node(
            kind="spine", anchor=(0, 9, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, (0, 9, 0)]),
            branches=[BranchSpec("spine_forward", "west", 3)],
        )
        engine.target_y = 16
        attempts = []

        def blocked_rejoin(node_id, **kwargs):
            attempts.append(node_id)
            return False, "cannot_reach_nearest_node"

        monkeypatch.setattr(engine, "_resume_shaft_tip", blocked_rejoin)
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=5, strategy="radar")

    assert attempts == [tip.node_id]
    if dry_floor:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == 1
        assert engine.nodes[engine.collar_node_id].anchor == start
        assert any(shaft["collar_anchor"] == list(collar.anchor) for shaft in engine.ledger.saved_shafts)
    else:
        assert result["stop_reason"] == "cannot_reach_nearest_node"
        assert engine.collar_node_id == collar.node_id
        assert not world.dig_log and not world.visited_blocks

