from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

def test_mining_dfs_zero_dig_return_collar_and_backtrack_after_branch_exhausted() -> None:
    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    world.blocks[(0, 76, -5)] = "iron_ore"
    world.blocks[(0, 77, -5)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res1 = skills.mine_dfs(target="iron", goal_count=2, target_y=76, max_duration_s=20.0)
        assert res1["ok"] is True
        digs_before_return = len(world.dig_log)

        res_ret = skills.mine_dfs(action="return_collar")
        digs_after_return = len(world.dig_log)

    assert res_ret["ok"] is True
    assert res_ret["stop_reason"] == "returned_to_collar"
    assert res_ret["dug_during_return"] == 0
    assert digs_after_return == digs_before_return
    assert tuple(int(v // 1) for v in world.pos) == (0, 80, 0)


@pytest.mark.parametrize(("with_progress", "expire_during_status"), [(False, True), (True, True), (True, False)])
def test_return_collar_timeout_reports_confirmed_approach(monkeypatch, with_progress, expire_during_status) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(6.5, 58.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="ResumeReturn", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        route = [(0, 64, 0), (2, 62, 0), (4, 60, 0), (6, 58, 0)]
        collar = engine._create_node(kind="collar", anchor=route[0], parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine._create_node(kind="switchback", anchor=route[-1], parent_id=collar.node_id,
                            edge_from_parent=ShaftEdge("stair_down", "east", route), branches=[])
        walked = []
        interrupted = False

        def step(cell, *, from_cell, repair_support):
            nonlocal interrupted
            if with_progress or interrupted:
                x, y, z = cell
                world.pos = [x + 0.5, float(y), z + 0.5]
                walked.append(cell)
                engine.bot.status()
            if not interrupted and (not with_progress or cell == route[2]):
                interrupted = True
                clock.now = engine._deadline
                if expire_during_status:
                    engine.bot.status()
            return True

        monkeypatch.setattr(engine, "_step_to", step)
        first = engine.run(action="return_collar", max_duration_s=5)
        if with_progress:
            assert first["stop_reason"] == "return_collar_progress"
            assert first["next_action"] == "return_collar"
            assert first["requires_replan"] is False
            assert first["pos_after"][1] == 60
        else:
            assert first["stop_reason"] == "time_slice"
            assert first["requires_replan"] is True
        assert first["ok"] is False

        second = engine.run(action="return_collar", max_duration_s=5)

    assert second["stop_reason"] == "returned_to_collar"
    assert walked == [(x, 64 - x, 0) for x in range(5, -1, -1)]
    assert not world.dig_log


def test_blocked_return_with_confirmed_ascent_does_not_count_as_zero_progress(monkeypatch) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.tool_registry import Tool

    start = [0.5, 24.0, 0.5]
    higher_stance = [2.5, 27.0, 0.5]
    world = _MockMineWorld(start_pos=tuple(start), surface_y=59)
    blocked = "backtrack_blocked@3,29,0(feet=air,head=air,from=1,26,0)"
    attempts = []

    with BotSession("tok", bot_username="BlockedReturnProgress", transport=httpx.MockTransport(world.handler)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0, 24, 0])
        engine = toolset.skills.mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(32, 59, 0), parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id

        def stop_at_blocked_waypoint(collar_id, **kwargs):
            assert collar_id == collar.node_id
            attempts.append(world.pos)
            world.pos = higher_stance.copy()
            return False, blocked

        monkeypatch.setattr(engine, "_walk_to_collar", stop_at_blocked_waypoint)
        guard = _RepeatFailureGuard(toolset)
        tool = guard.wrap_tool(Tool(
            name="mc_skill_mine_dfs", description="Mine", parameters={}, handler=toolset.mc_skill_mine_dfs,
        ))
        advanced = json.loads(tool.handler(action="return_collar"))
        assert not guard._failed_sigs
        stalled = json.loads(tool.handler(action="return_collar"))
        rejected = json.loads(tool.handler(action="return_collar"))

    assert attempts == [start, higher_stance]
    assert advanced["ok"] is True and advanced["requires_replan"] is True
    assert advanced["stop_reason"] == "return_collar_progress"
    assert advanced["collar_reached"] is False
    assert advanced["rejoin_error"] == blocked
    assert advanced["pos_before"] == start and advanced["pos_after"] == higher_stance
    assert stalled["ok"] is False and stalled["stop_reason"] == blocked
    assert stalled["pos_before"] == stalled["pos_after"] == higher_stance
    assert rejected["error"]["code"] == "repeated_identical_failure_blocked"


def test_underground_resupply_collar_return_craft_gear_table_reuse_and_mc_craft_auto_place() -> None:
    """P2-8, P2-9, P2-10 fix: pickaxe_low returns to Collar; craft_gear reuses distant table; mc_craft auto-places backpack table."""
    world = _MockMineWorld(
        start_pos=(0.5, 20.0, 0.5),
        surface_y=70,
        track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [28, 131]},
            {"name": "cobblestone", "count": 32, "slot": 10},
            {"name": "stick", "count": 4, "slot": 11},
        ],
    )
    # Carve initial Collar room at (0, 20, 0) with an existing placed crafting_table at (1, 20, 0)
    for x in (-1, 0, 1):
        for y in (20, 21):
            world.blocks[(x, y, 0)] = "air"
    world.blocks[(1, 20, 0)] = "crafting_table"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        # Descend until stone_pickaxe hits redline (<=12) -> should auto-return 0-dig to Collar (0, 20, 0)!
        res_low = skills.mine_dfs(target="iron", goal_count=5, target_y=0, max_duration_s=20.0)
        assert res_low["stop_reason"] == "pickaxe_low"
        assert res_low.get("returned_to_collar_for_resupply") is True
        assert math.dist(world.pos, (0.5, 20.0, 0.5)) <= 1.5

        # Even if bot walked back down to bottom of dug shaft (~5 blocks away -> >4.6m from table),
        # craft_gear("stone_pickaxe") must navigate back to (1, 20, 0) and craft without oak_log ecology error!
        deep_nodes = [n for n in skills.mining_dfs.nodes.values() if n["anchor"][1] < 18] if False else [
            n for n in skills.mining_dfs.nodes.values() if n.anchor[1] < 18
        ]
        assert deep_nodes
        bottom = min(deep_nodes, key=lambda n: n.anchor[1])
        skills.mining_dfs._navigate_to_node(bottom.node_id, min_hp_ref=[20.0])
        assert math.dist(world.pos, (1.5, 20.5, 0.5)) > 4.6

        cg = skills.craft_gear("stone_pickaxe", count=1)
        assert cg["ok"] is True
        assert "ecology_check" not in cg

        # Also verify mc_craft auto-places a crafting_table from backpack when no table is placed nearby!
        world.blocks[(1, 20, 0)] = "stone"  # remove old table
        world._add_item("crafting_table", 1)
        world._add_item("stick", 2)
        world.blocks[(1, 20, 0)] = "air"
        world.blocks[(1, 21, 0)] = "air"
        ts = McToolset(bot)
        raw_mc_craft = json.loads(ts.mc_craft("stone_pickaxe", 1))
        assert raw_mc_craft["ok"] is True
        assert raw_mc_craft["result"]["crafted"] == "stone_pickaxe"


@pytest.mark.parametrize(
    "anchor_y, ore_x, ore_y, climb_first",
    [(-54, 27, -35, False), (-54, 7, -35, True), (-54, 27, -49, True), (-64, 27, -45, True)],
)
def test_radar_near_target_depth_defers_a_long_climb_to_distant_ore(anchor_y, ore_x, ore_y, climb_first) -> None:
    world = _MockMineWorld(start_pos=(0.5, float(anchor_y), 0.5), surface_y=64)
    with BotSession("tok", bot_username="RadarDepthPlan", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.strategy = "radar"
        engine.target_y = -54
        ore = (ore_x, ore_y, 0)
        engine.locked_cluster = {"rep": ore, "size": 1, "mob_nearby": False}
        branches = engine._spine_branches("east", (0, anchor_y, 0), None)

    kinds = [branch.kind for branch in branches]
    assert kinds.count("stair_up") == 1
    stair = branches[0 if climb_first else -1]
    assert stair.kind == "stair_up"
    assert stair.branch_target_y == ore_y
    assert [branch.direction for branch in branches if branch.kind != "stair_up"] == ["east", "north", "south"]


@pytest.mark.parametrize("intermediate", ["not_attempted", "collected_diamond", "approached_cluster"])
@pytest.mark.parametrize("first_interrupted", [False, True])
def test_radar_locked_cluster_tracks_dry_budget_exits(monkeypatch, intermediate, first_interrupted) -> None:
    from mc_agent.skills.mining_dfs import _MiningDeadline

    world = _MockMineWorld(start_pos=(0.5, -54.0, 0.5), surface_y=64)
    representative = (0, -53, 4)
    phases = iter(("first_dry", intermediate, "dry"))
    dig_positions = iter(((1, -54, 0), (2, -54, 0), (3, -54, 0)))
    with BotSession("tok", bot_username="RadarDryBudget", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.locked_cluster = {"rep": representative, "size": 1, "mob_nearby": False}
        engine.radar_targets = [representative]

        def mine_slice(**kwargs):
            phase = next(phases)
            engine.bot.status()
            engine.bot.inventory()
            if phase != "not_attempted":
                assert engine.bot.call("dig", {"pos": list(next(dig_positions)), "collect": False})["dug"]
            if phase == "collected_diamond":
                world._add_item("diamond", 1)
                engine.bot.inventory()
            if phase == "approached_cluster":
                world.pos = [0.5, -54.0, 3.5]
                engine.bot.status()
            if phase == "first_dry" and not first_interrupted:
                return {"target_gained": 0, "tree": engine.snapshot()}
            raise _MiningDeadline

        monkeypatch.setattr(engine, "_run", mine_slice)
        first = engine.run(target="diamond", goal_count=4, target_y=-54)
        assert first["target_gained"] == 0
        assert engine.locked_cluster is not None
        assert engine._locked_rep_dry_calls == 1

        intervening = engine.run(target="diamond", goal_count=4, target_y=-54)
        assert intervening["target_gained"] == (1 if intermediate == "collected_diamond" else 0)
        assert engine._locked_rep_dry_calls == (1 if intermediate == "not_attempted" else 0)
        last = engine.run(target="diamond", goal_count=4, target_y=-54)

    should_evict = intermediate == "not_attempted"
    assert last.get("radar_evicted_cluster") == (list(representative) if should_evict else None)
    assert ("locked_cluster" not in last["tree"]) == should_evict
    assert engine._is_ore_unreachable(representative) == should_evict
    assert (engine.locked_cluster is None) == should_evict
    assert (representative not in engine.radar_targets) == should_evict


def test_fishbone_spine_climbs_when_target_raised() -> None:
    """Symmetric gap: a shaft that overshot below target_y must be able to stair_up back to it."""
    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    with BotSession("tok", bot_username="ClimbBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng._refresh_strategy("fishbone")

        anchor = (0, -54, 0)
        eng.target_y = -54
        assert not [b for b in eng._spine_branches("north", anchor, geofence=None) if b.kind == "stair_up"]

        # diamond layer -> iron layer: must climb, otherwise the skeleton stays buried forever
        eng.target_y = 16
        deep = eng._spine_branches("north", anchor, geofence=None)
        assert deep[0].kind == "stair_up"
        assert deep[0].branch_target_y == 16
        assert 0 < deep[0].steps <= 16
        assert deep[0].steps <= 16 - anchor[1]


def test_deep_pickaxe_low_with_no_wood_requests_climb_before_harvesting() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, -53.0, 0.5), surface_y=64,
        inventory_items=[{"name": "cobbled_deepslate", "count": 64, "slot": 9}],
    )
    with BotSession("tok", bot_username="WoodlessUnderground", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.collar_ceiling_y = 64
        result = engine.run(target="iron", max_duration_s=10)

    assert result["stop_reason"] == "pickaxe_low"
    assert result["next_action"] == "climb_out"
    assert "y>=48" in result["message"]
    assert result["message"].index("climb_out") < result["message"].index("mc_skill_harvest_logs")
    assert result["pos_after"] == [0.5, -53.0, 0.5]
    assert world.pos == [0.5, -53.0, 0.5]
    assert not world.dig_log


def test_fluid_trap_deep_underground_does_not_try_surface_relocation() -> None:
    """Live s03: 3 cave breeches at y=8 -> relocation found no surface stand -> orphaned tree."""
    import time as _time

    world = _MockMineWorld(start_pos=(0.5, 8.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="DeepBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        node = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = node.node_id
        moved, auto = eng._force_relocate_on_fluid(
            geofence=None, deadline=_time.monotonic() + 100.0,
            min_hp_ref=[20.0], auto_relocations=0,
        )
    assert (moved, auto) == (False, 0)
    assert eng.fluid_abandoned_deep is True
    assert eng.collar_node_id is None                     # archived, must climb out first


def test_deep_empty_tree_asks_for_a_climb_instead_of_opening_fake_collar() -> None:
    """Live s01/s03: a deep "collar" at y=-54 made later resets look legitimate forever."""
    world = _MockMineWorld(start_pos=(0.5, 8.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="DeepCollarBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64                     # we did open a surface collar earlier
        eng.idle_calls = 3
        res = eng.run(target="iron", goal_count=4, target_y=16, action="explore", max_duration_s=6.0)
    assert res["stop_reason"] == "needs_surface_climb"
    assert res["requires_replan"] is True
    assert eng.collar_node_id is None                 # no fake underground collar planted


def test_climb_out_action_reports_progress_and_target(monkeypatch) -> None:
    """One call must own probe+repeat; the model should not spend a turn per blocked direction."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="ClimbBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64                     # target = 48 (ceiling-16)
        calls: list[str] = []

        def fake_status():
            return {"pos": [0.5, float(world.pos[1]), 0.5], "health": 20.0, "food": 20.0, "alive": True}

        def fake_stair(direction=None, steps=0, vertical="down", stop_on_ore=None):
            if direction in {"north", "south", "east", "west"}:
                calls.append(direction)
                world.pos = [world.pos[0], min(64.0, world.pos[1] + 4.0), world.pos[2]]
                return {"ok": True, "steps_done": 4}
            return {"ok": False}

        monkeypatch.setattr(eng.bot, "status", fake_status)
        monkeypatch.setattr(eng.skills, "dig_staircase", fake_stair)
        res = eng.run(action="climb_out", target_y=16, max_duration_s=20.0)

    assert res["action"] == "climb_out"
    assert res["climb"]["target_y"] == 48
    assert res["climb"]["reached"] is True
    assert res["climb"]["climbed"] >= 28
    assert calls


def test_climb_out_prefers_walking_the_shaft_over_new_stairs(monkeypatch) -> None:
    """Walking our own corridor must win over re-digging stairs (s12: 27 turns of staircase)."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="WalkHomeBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        tip = eng._create_node(kind="spine", anchor=(0, 20, 0), parent_id=collar.node_id,
                               edge_from_parent=ShaftEdge("stair_down", "north", [(0, 40, 0)]),
                               branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = tip.node_id
        eng.collar_ceiling_y = 64

        def fake_status():
            return {"pos": [0.5, float(world.pos[1]), 0.5], "health": 20.0, "food": 20.0, "alive": True}

        def fake_nav(goal_node_id, *, min_hp_ref, deadline, allow_lower_rejoin):
            assert allow_lower_rejoin is False
            world.pos = [0.5, 64.0, 0.5]          # walk all the way home
            eng.active_node_id = goal_node_id
            return True, None

        stairs: list[str] = []
        monkeypatch.setattr(eng.bot, "status", fake_status)
        monkeypatch.setattr(eng, "_navigate_to_node", fake_nav)
        monkeypatch.setattr(eng.skills, "dig_staircase",
                            lambda **kw: (stairs.append(kw.get("direction")), {"ok": True})[1])
        res = eng.run(action="climb_out", target_y=16, max_duration_s=20.0)

    assert res["climb"]["method"] == "walk"
    assert res["climb"]["reached"] is True
    assert res["tree"]["active_node_id"] == tip.node_id
    assert stairs == []                            # never dug a single stair


@pytest.mark.parametrize("retired", [False, True])
def test_climb_out_rejoins_a_completed_side_branch_before_cutting_stairs(monkeypatch, retired) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(9.5, 20.0, 0.5), surface_y=40)
    stair = [(0, y, y - 20) for y in range(40, 19, -1)]
    leaf = [(x, 20, 0) for x in range(10)]
    cached_ore = (200, -54, 0)

    def short_moves_only(request):
        if request.url.path.endswith("/get_block") and tuple(json.loads(request.content)["pos"]) == cached_ore:
            pytest.fail("checked an unrelated cached ore during climb_out")
        if request.url.path.endswith("/move_to"):
            args = json.loads(request.content)
            goal = (args["x"], args.get("y", world.pos[1]), args["z"])
            if math.dist(world.pos, goal) > 1.5:
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "path_not_found", "message": "use the corridor"},
                })
        return world.handler(request)

    for x, y, z in [*stair, *leaf]:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    with BotSession("tok", bot_username="ClimbFromLeaf", transport=httpx.MockTransport(short_moves_only)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=stair[0], parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="spine", anchor=leaf[0], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", stair),
            branches=[BranchSpec("branch_leaf", "east", 9, status="completed", completed_waypoints=leaf)],
        )
        if retired:
            engine._archive_current_shaft_to_ledger(mark_exhausted=True)
            engine._clear_active_tree_only()
            engine.ledger.discovered_ores[cached_ore] = {
                "block": "deepslate_diamond_ore", "required_tier": 3, "from_disk": True,
            }
        monkeypatch.setattr(engine.skills, "dig_staircase", lambda **kw: pytest.fail("re-dug the return route"))
        result = engine.run(action="climb_out", max_duration_s=20)

    assert result["stop_reason"] == "surface_reached"
    assert result["climb"]["method"] == "walk"
    assert not world.dig_log


@pytest.mark.parametrize("condition", ["dry", "adjacent_water", "furnace", "unconfirmed_support"])
def test_return_collar_repairs_the_adjacent_recorded_step_from_one_block_below(monkeypatch, condition) -> None:
    start, tip, collar_anchor = (0, -33, 1), (0, -32, 0), (0, -31, -1)
    support = (0, -33, 0)
    world = _MockMineWorld(start_pos=(0.5, -33.0, 1.3), surface_y=64)
    for x, y, z in (start, tip, collar_anchor):
        for dy in range(3):
            world.blocks[(x, y + dy, z)] = "air"
    world.blocks[support] = "air"
    if condition == "adjacent_water":
        world.blocks[(1, -32, 0)] = "water"
    elif condition == "furnace":
        world.blocks[(0, -31, 0)] = "furnace"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    placement_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/place"):
            placement_requests.append(tuple(json.loads(request.content)["pos"]))
            if condition == "unconfirmed_support":
                return httpx.Response(200, json={"ok": True, "result": {}})
        return world.handler(request)

    with BotSession("tok", bot_username="JoinRecordedStair", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=collar_anchor, parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine._create_node(
            kind="switchback", anchor=tip, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar_anchor, tip]), branches=[],
        )
        result = engine.run(action="return_collar")

    if condition == "dry":
        assert result["stop_reason"] == "returned_to_collar"
        assert world.place_log == [(support, "cobblestone")]
        assert tip in world.visited_blocks
        assert world.pos == [0.5, -31.0, -0.5]
    else:
        assert result["stop_reason"] == "cannot_reach_nearest_node"
        assert not world.place_log
        assert world.pos == [0.5, -33.0, 1.3]
    assert placement_requests == ([support] if condition in {"dry", "unconfirmed_support"} else [])
    assert not world.dig_log


@pytest.mark.parametrize("steps_between_waypoints", [1, 2])
def test_return_repairs_missing_support_under_an_open_recorded_stair(monkeypatch, steps_between_waypoints) -> None:
    lower = (steps_between_waypoints, 19, 0)
    upper = (0, 19 + steps_between_waypoints, 0)
    support = (lower[0] - 1, 19, 0)
    world = _MockMineWorld(start_pos=(support[0] + 0.5, 19.0, 0.5), surface_y=40)
    world.blocks[support] = "air"
    for step in range(steps_between_waypoints + 1):
        for dy in range(3):
            world.blocks[(lower[0] - step, lower[1] + step + dy, 0)] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    def handler(request):
        if request.url.path.endswith("/place"):
            assert tuple(math.floor(v) for v in world.pos) == lower
        if request.url.path.endswith("/move_to") and json.loads(request.content).get("y", 0) > world.pos[1] + 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "stair gap"}})
        return world.handler(request)

    with BotSession("tok", bot_username="RepairStairSupport", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        result = engine._walk_waypoints([upper, lower], reverse=True, min_hp_ref=[20.0])

    assert result == (True, None)
    assert world.pos == [0.5, float(upper[1]), 0.5]
    assert world.place_log == [(support, "cobblestone")]
    assert not world.dig_log


@pytest.mark.parametrize(
    "height, start_offset, recorded_lower, lands_below_waypoint, horizontal_offset",
    [
        (1, 0, True, False, 0), (2, 0, True, False, 0), (2, 1, True, False, 0),
        (1, 0, False, False, 0), (3, 0, True, True, 0),
        (2, 0, True, False, 1), (2, 1, True, False, 1),
    ],
)
def test_return_jump_rejoins_a_recorded_vertical_gap(
    monkeypatch, height, start_offset, recorded_lower, lands_below_waypoint, horizontal_offset,
) -> None:
    start_x, start_z = (1, -1) if lands_below_waypoint else (0, 0)
    start_y = 19 + start_offset
    world = _MockMineWorld(start_pos=(start_x + 0.5, float(start_y), start_z + 0.5), surface_y=40)
    lower, upper = (start_x, 19, start_z), (horizontal_offset, 19 + height, 0)
    support_y = upper[1] - 1 if lands_below_waypoint else start_y
    for y in range(support_y, upper[1] + 2):
        world.blocks[(0, y, 0)] = "air"
    world.blocks[(start_x, start_y, start_z)] = "air"
    world.blocks[(start_x, start_y + 1, start_z)] = "air"
    world.blocks[upper] = world.blocks[(upper[0], upper[1] + 1, upper[2])] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    jumping = False
    jump_base = 0

    def handler(request):
        nonlocal jumping, jump_base
        path = request.url.path.rsplit("/", 1)[-1]
        if lands_below_waypoint and path in {"move_to", "control"}:
            world.pos[:] = [0.5, float(support_y), 0.5]
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "landed below waypoint"}})
        if horizontal_offset and path == "move_to" and json.loads(request.content)["y"] > world.pos[1] + 1:
            return httpx.Response(200, json={"ok": False, "error": {"code": "unsafe_landing"}})
        if path == "posture":
            wants_jump = bool(json.loads(request.content)["jump"])
            if wants_jump and not jumping:
                jump_base = math.floor(world.pos[1])
                world.pos[1] = jump_base + 1.1
            elif jumping and not wants_jump:
                placed = world.block_at((0, jump_base, 0)) == "cobblestone"
                world.pos[1] = float(jump_base + placed)
            jumping = wants_jump
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path == "place":
            assert jumping
        response = world.handler(request)
        if path == "status" and jumping:
            body = response.json()
            body["result"]["on_ground"] = False
            return httpx.Response(200, json=body)
        return response

    with BotSession("tok", bot_username="VerticalReturn", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=upper, parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        if recorded_lower:
            engine._create_node(
                kind="switchback", anchor=lower, parent_id=collar.node_id,
                edge_from_parent=ShaftEdge("stair_down", "north", [upper, lower]), branches=[],
            )
        mining_node_id = engine.active_node_id
        result = engine.run(action="return_collar")

    assert result["ok"] is True
    assert world.pos == [upper[0] + 0.5, float(upper[1]), 0.5]
    assert world.place_log == [((0, y, 0), "cobblestone") for y in range(support_y, upper[1] - horizontal_offset)]
    assert engine.active_node_id == mining_node_id
    assert not world.dig_log
    assert not jumping


@pytest.mark.parametrize(("stairs", "start_step", "down_direction"), [
    ([(0, -41, 0), (1, -40, 0), (2, -39, 0), (3, -39, 0), (4, -38, 0)], 0, "west"),
    ([(0, 26, 0), (0, 26, -1), (0, 27, -2), (0, 28, -3)], 0, "south"),
    ([(0, 26, 0), (0, 26, -1), (0, 27, -2), (0, 28, -3)], 1, "south"),
])
def test_return_collar_follows_a_recorded_stair_with_a_flat_step(monkeypatch, stairs, start_step, down_direction) -> None:
    waypoints = [stairs[0], stairs[-1]]
    start_x, start_y, start_z = stairs[start_step]
    world = _MockMineWorld(start_pos=(start_x + 0.5, float(start_y), start_z + 0.5), surface_y=64)
    for x, y, z in stairs:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"

    def handler(request):
        if request.url.path.endswith("/move_to"):
            args = json.loads(request.content)
            if "x" in args and math.hypot(args["x"] - world.pos[0], args["z"] - world.pos[2]) > 1.0:
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "stair gap"}})
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="MixedStairReturn", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=waypoints[-1], parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine._create_node(
            kind="switchback", anchor=waypoints[0], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", down_direction, waypoints[::-1]), branches=[],
        )
        result = engine.run(action="return_collar")

    assert result["ok"] is True
    goal_x, goal_y, goal_z = waypoints[-1]
    assert world.pos == [goal_x + 0.5, float(goal_y), goal_z + 0.5]
    assert stairs[-2] in world.visited_blocks
    assert not any(step in world.visited_blocks for step in stairs[:start_step])
    assert not world.dig_log and not world.place_log


@pytest.mark.parametrize("start_case", ["node", "edge", "saved"])
def test_shallower_layer_replans_above_the_new_target_without_losing_return_route(monkeypatch, start_case) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    start, collar_anchor = (0, 22, 42), (0, 64, 0)
    tip = (0, 15, 49) if start_case == "edge" else start
    route = [(0, y, 64 - y) for y in range(64, tip[1] - 1, -1)]
    world = _MockMineWorld(start_pos=(0.5, 22.0, 42.5), surface_y=64)
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="ShallowerMiningPlan", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=collar_anchor, parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 64
        engine.target_y = -54
        old_edge = ShaftEdge("stair_down", "south", route)
        old_tip = engine._create_node(
            kind="switchback", anchor=tip, parent_id=collar.node_id, edge_from_parent=old_edge,
            branches=[BranchSpec("stair_down", "south", tip[1] + 54, branch_target_y=-54)],
        )
        if start_case == "saved":
            # Old snapshots can already report the new nominal layer while preserving the old plan.
            engine.target_y = 16
            engine._archive_current_shaft_to_ledger()
            engine._clear_active_tree_only()
        targets = []
        dig_down = engine._dig_stair_down_edge

        def record_descent(*, target_y, **kwargs):
            targets.append(target_y)
            return dig_down(target_y=target_y, **kwargs)

        monkeypatch.setattr(engine, "_dig_stair_down_edge", record_descent)
        first = engine.run(target="iron", target_y=16, goal_count=4, max_duration_s=6)
        second = engine.run(target="iron", target_y=16, goal_count=4, max_duration_s=6)

    assert targets and set(targets) == {16}
    assert first["pos_before"][1] == start[1]
    assert min(y for _, y, _ in world.visited_blocks) >= 16
    assert first["pos_after"][1] >= 16 and second["pos_after"][1] >= 16
    assert engine.collar_node_id == collar.node_id
    assert engine.nodes[old_tip.node_id].edge_from_parent == old_edge
    assert world.block_at((0, 22, 41)) == "stone"


def test_climb_target_overshoots_the_explore_threshold() -> None:
    """Live n0332: target==ceiling-20 made climb "reach" exactly where explore still refuses."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="BoundaryBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64
        climb = eng._climb_out(deadline=0.0)          # no time: just check the computed target
    assert climb["target_y"] == 48                    # > 64-20=44, so explore will accept


@pytest.mark.parametrize(("walk_duration", "walk_error", "expected_stop"), [
    (5, "backtrack_blocked", "surface_reached"),
    (23, "backtrack_blocked", "climb_progress"),
    (5, "hurt", "climb_progress"),
])
def test_climb_out_continues_after_partial_walk_with_budget(monkeypatch, walk_duration, walk_error, expected_stop) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=43)
    walked_from = []
    stair_starts = []

    def walk_to_collar(collar_id, **kwargs):
        walked_from.append(world.pos[1])
        if len(walked_from) == 1:
            world.pos[1] += 3
            clock.now += walk_duration
        return False, walk_error

    def stair_step(**kwargs):
        stair_starts.append(world.pos[1])
        world.pos[1] += 1
        clock.now += 1
        return {"ok": False, "stop_reason": "time_budget", "steps_done": 1}

    with BotSession("tok", bot_username="PartialClimb", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 43, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        monkeypatch.setattr(engine, "_walk_to_collar", walk_to_collar)
        monkeypatch.setattr(engine.skills, "dig_staircase", stair_step)
        result = engine.run(action="climb_out", max_duration_s=32)

    assert result["stop_reason"] == expected_stop
    if expected_stop == "surface_reached":
        assert walked_from == [20, 23]
        assert stair_starts == [23, 24]
        assert result["climb"]["end_y"] == 25
    else:
        assert walked_from == [20]
        assert not stair_starts
        assert result["climb"]["end_y"] == 23

