from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

def test_mining_dfs_respects_void_geofence() -> None:
    world = _MockMineWorld(start_pos=(0.5, 80.0, -7.5), surface_y=80)
    gf = VoidGeofence(center_x=0.0, center_z=0.0, radius=15.0, margin=5.0)
    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=3, target_y=74, geofence=gf, max_duration_s=10.0)
    for pt, _, _ in world.dig_log:
        assert math.hypot(pt[0] + 0.5, pt[2] + 0.5) <= gf.safe_radius + 1.5
    assert "tree" in res


def test_mining_dfs_canopy_spawn_seeks_solid_ground_anchor() -> None:
    """LocA / LocB fix: spawning on oak_leaves canopy with 0 cobblestone descends to solid ground before Collar."""
    world = _MockMineWorld(
        start_pos=(0.5, 75.0, 0.5),
        surface_y=70,
        track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
        ],
    )
    # Leave a three-block drop from the last leaf layer to solid ground.
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            for y in range(72, 75):
                world.blocks[(dx, y, dz)] = "oak_leaves"
    # Place target iron ore underground at Y=64 along north descent from ground Y=70
    world.blocks[(0, 64, -7)] = "iron_ore"
    world.blocks[(1, 64, -7)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=64, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2
    collar_node = next(n for n in res["tree"]["nodes"] if n["kind"] == "collar")
    assert collar_node["anchor"][1] == 70  # Collar anchored on real ground Y=70, NOT canopy Y=75!
    # Digging oak_leaves must not consume pickaxe durability
    leaf_dig_tools = [tool for _, bn, tool in world.dig_log if bn == "oak_leaves"]
    assert leaf_dig_tools
    assert all(t is None for t in leaf_dig_tools)


def test_mining_dfs_does_not_dig_a_canopy_drop_of_four_blocks() -> None:
    start = (0.5, 67.0, 0.5)
    footing = (0, 66, 0)
    world = _MockMineWorld(start_pos=start, surface_y=63)
    world.blocks[footing] = "birch_leaves"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/move_to", "/control")):
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
        return world.handler(request)

    with BotSession("tok", bot_username="CanopyDrop", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        result = engine.run(target="iron", target_y=16, goal_count=1, max_duration_s=5)

    assert result["stop_reason"] == "no_solid_collar_ground"
    assert world.block_at(footing) == "birch_leaves"
    assert world.pos == list(start) and not world.dig_log
    assert engine.collar_node_id is None


def test_mining_dfs_crosses_self_excavated_cavity_with_bridge_block() -> None:
    """P0-2 fix: crossing a previously dug tunnel (even after reset) places a bridge block and does not prune."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    # Block east/south/west so descent must go north
    for dpos in ((1, 75, 0), (-1, 75, 0), (0, 75, 1)):
        world.blocks[dpos] = "bedrock"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        # Pre-carve a cross-tunnel at z=-3, y=71..72 (which corresponds to support_pt=(0, 72, -3) at step 3)
        for x in (-2, -1, 0, 1, 2):
            for y in (71, 72):
                world.blocks[(x, y, -3)] = "air"
                skills.mining_dfs._record_excavated((x, y, -3))
        # Simulate tree reset (excavated_positions must survive reset!)
        skills.mining_dfs._clear_active_tree_only()

        world.blocks[(0, 70, -7)] = "iron_ore"
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=70, max_duration_s=20.0)
        digs_before_ret = len(world.dig_log)
        ret = skills.mine_dfs(action="return_collar")
        digs_after_ret = len(world.dig_log)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["tree"]["bridge_blocks_placed"] >= 1
    assert world.block_at((0, 72, -3)) == "cobblestone"
    assert ret["ok"] is True
    assert ret["dug_during_return"] == 0
    assert digs_after_ret == digs_before_ret


def test_mining_dfs_offset_sidestep_retry_bypasses_local_obstruction() -> None:
    """P1-6 fix: narrow 1-block bedrock bumpers on all 4 Collar faces trigger 3-block lateral offset retry."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    # Block immediate 1st stair step in all 4 cardinal directions with narrow 1-block bedrock
    for dpos in ((0, 75, -1), (0, 75, 1), (1, 75, 0), (-1, 75, 0)):
        world.blocks[dpos] = "bedrock"
    # Parallel lane 3 blocks west (-3, y, z) is completely open stone with iron ore at (-3, 72, -5)!
    world.blocks[(-3, 72, -5)] = "iron_ore"
    world.blocks[(-3, 73, -5)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=72, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2


@pytest.mark.parametrize("interrupted", [False, True])
def test_offset_route_with_a_changed_height_is_not_recorded_as_level(monkeypatch, interrupted: bool) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec, _MiningDeadline

    anchor = (0, 54, 0)
    world = _MockMineWorld(start_pos=(0.5, 54.0, 0.5), surface_y=72, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    world.blocks[anchor] = world.blocks[(0, 55, 0)] = "air"

    with BotSession("tok", bot_username="OffsetHeightChange", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 72, -18), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        branch = BranchSpec("offset_retry", "north", 39, sidestep_dir="east", resume_kind="stair_down")
        junction = engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, anchor]), branches=[branch],
        )

        def displaced_offset(**kwargs):
            world.pos = [3.5, 53.0, 0.5]
            if interrupted:
                engine.bot.status()
                raise _MiningDeadline
            return {"steps_done": 3, "end_anchor": (3, 53, 0), "stop_reason": "time_slice",
                    "waypoints": [anchor, (3, 53, 0)], "breaches_sealed": 0, "catch_placed": 0,
                    "deferred_added": 0, "mined_ores": []}

        monkeypatch.setattr(engine, "_dig_level_edge", displaced_offset)
        result = engine.run(target="ancient_debris", target_y=15, goal_count=1)

    assert result["stop_reason"] == ("time_slice_progress" if interrupted else "movement_not_confirmed")
    assert result["pos_after"][1] == 53
    assert engine.active_node_id == junction.node_id
    assert len(engine.nodes) == 2
    assert branch.status == "pending"


@pytest.mark.parametrize("steps", [1, 2])
def test_interrupted_sidestep_only_promotes_a_confirmed_offset(monkeypatch, steps: int) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec, _MiningDeadline

    anchor = (0, 54, 0)
    world = _MockMineWorld(start_pos=(0.5, 54.0, 0.5), surface_y=72, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    for x in range(3):
        for y in (54, 55):
            world.blocks[(x, y, 0)] = "air"

    with BotSession("tok", bot_username="InterruptedOffset", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 72, -18), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        branch = BranchSpec("offset_retry", "north", 39, sidestep_dir="east", resume_kind="stair_down",
                            offset_tried=True, branch_target_y=15)
        junction = engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, anchor]), branches=[branch],
        )
        starts = []

        def cut_offset(**kwargs):
            starts.append(tuple(math.floor(v) for v in world.pos))
            for step in range(1, steps + 1):
                world.pos[0] = step + 0.5
                engine.bot.status()
            raise _MiningDeadline

        monkeypatch.setattr(engine, "_dig_level_edge", cut_offset)
        result = engine.run(target="ancient_debris", target_y=15, goal_count=1)
        assert result["stop_reason"] == "time_slice_progress"
        if steps == 1:
            assert branch.status == "pending" and engine.active_node_id == junction.node_id
            engine.run(target="ancient_debris", target_y=15, goal_count=1)
            assert starts == [anchor, anchor]
        else:
            assert branch.status == "completed"
            tip = engine.nodes[engine.active_node_id]
            assert tip.kind == "switchback" and tip.parent_id == junction.node_id
            assert tip.edge_from_parent == ShaftEdge("offset_sidestep", "east", [anchor, (1, 54, 0), (2, 54, 0)])
            assert tip.branches == [BranchSpec("stair_down", "north", 39, offset_tried=True, branch_target_y=15)]


def test_repeated_walk_to_same_mining_stance_counts_as_dry_after_first_approach(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 67.0, 0.5), surface_y=70)
    representative = (0, 16, 0)
    end_heights = iter((59, 59, 56, 56, 56))
    stalled_cells = iter((x, 55, 1) for x in range(5))

    def handler(request):
        if request.url.path.endswith("/dig"):
            raise httpx.ReadTimeout("dig stalled at the mining stance", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="RepeatedStance", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.locked_cluster = {"rep": representative, "size": 6, "mob_nearby": False}
        engine.radar_targets = [representative]

        def mine_slice(**kwargs):
            engine.bot.status()
            engine.bot.inventory()
            world.pos[:] = [0.5, float(next(end_heights)), 0.5]
            engine.bot.status()
            engine.bot.call("dig", {"pos": list(next(stalled_cells)), "collect": True})

        monkeypatch.setattr(engine, "_run", mine_slice)
        for dry_calls in (0, 1, 0, 1, 0):
            world.pos[:] = [0.5, 67.0, 0.5]
            result = engine.run(target="iron", target_y=16, goal_count=8, strategy="radar", max_duration_s=90)
            assert result["target_gained"] == result["dug_count"] == 0
            assert result["timeout_path"] == "dig"
            assert engine._locked_rep_dry_calls == dry_calls

    assert result["radar_evicted_cluster"] == list(representative)
    assert "locked_cluster" not in result["tree"]


@pytest.mark.parametrize("remaining_drop", ["open", "wall", "falling", "water"])
def test_partial_target_pickup_checks_remaining_drops(monkeypatch, remaining_drop) -> None:
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[{"name": "diamond", "count": 1, "slot": 9}],
    )
    for z in range(4):
        for y in (-54, -53):
            world.blocks[(0, y, z)] = "air"
    drop_pos = [0.5, -53.8, 3.5]
    if remaining_drop == "wall":
        world.blocks[(0, -54, 2)] = "deepslate"
    if remaining_drop in {"falling", "water"}:
        drop_pos[1] = -50.2
    if remaining_drop == "water":
        world.blocks[(0, -54, 2)] = "water"
    drop = {"id": 91, "name": "item", "item": "diamondx1", "pos": drop_pos}
    world.entities.extend([drop, {"id": 92, "name": "item", "item": "dirtx1", "pos": [-3.5, -54.0, 0.5]}])
    if remaining_drop == "open":
        world.entities.append({"id": 93, "name": "item", "item": "diamondx1", "pos": [0.5, -54.0, -4.5]})
    scan_count = 0

    def handler(request):
        nonlocal scan_count
        if request.url.path.endswith("/get_entities"):
            scan_count += 1
            if scan_count == 2 and remaining_drop in {"falling", "water"}:
                drop_pos[1] = -53.8 if remaining_drop == "falling" else -52.2
            if scan_count == 2 and remaining_drop == "open":
                world.entities[:] = [entity for entity in world.entities if entity["id"] != 93]
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="PartialDrop", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        assert engine._collect_and_verify_target_drop(
            expected_drops={"diamond"}, before_count=0, ore_pt=(0, -54, 3),
            stand_y=-54, cache={},
        )

    can_pick_up = remaining_drop in {"open", "falling"}
    assert scan_count >= 1
    assert world._count("diamond") == (2 if can_pick_up else 1)
    assert world._count("dirt") == 0
    assert tuple(world.pos) == (0.5, -54.0, 0.5)
    assert not world.dig_log
    if not can_pick_up:
        assert not world.visited_blocks
    assert engine.drop_receipts[91]["item_pos"] == drop_pos
    assert engine.drop_receipts[91]["reason"] == {
        "open": "picked_up", "wall": "unsafe_route", "falling": "picked_up", "water": "height_difference",
    }[remaining_drop]
    assert 92 not in engine.drop_receipts
    if remaining_drop == "open":
        assert engine.drop_receipts[93]["reason"] == "pickup_unconfirmed"
        assert engine.drop_receipts[91]["picked_count"] == 1
        assert engine.drop_receipts[91]["trigger_ore_pos"] == [0, -54, 3]


def test_verified_target_drop_rejects_cobble_trash_and_opens_1x1_pocket_base() -> None:
    """Regression for realA4 2252.5s: cobble pickup is NOT counted as ore; stuck upper 1x1 wall drop opens base block."""
    world = _MockMineWorld(
        start_pos=(0.5, 74.0, 0.5),
        surface_y=80,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "cobblestone", "count": 16, "slot": 10},
        ],
    )
    # Open the starting cell and adjoining passage beside the elevated ore pocket.
    for z in (0, -1):
        for y in (74, 75):
            world.blocks[(0, y, z)] = "air"
    world.blocks[(1, 75, -1)] = "iron_ore"
    world.blocks[(1, 74, -1)] = "stone"
    world.stuck_pocket_ores.add((1, 75, -1))

    with BotSession("tok", bot_username="DropVerifyBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=74, strategy="fishbone", max_duration_s=15.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 1
    assert world._count("raw_iron") >= 1
    assert res["ore_breaks"] == [{"pos": [1, 75, -1], "block": "iron_ore"}]
    assert any(row["item"] == "raw_iron" and row["reason"] == "picked_up" for row in res["target_drop_receipts"])
    # Confirm lower pocket block (1, 74, -1) was opened so the bot could actually collect the trapped raw_iron!
    assert any(pt == (1, 74, -1) for pt, _, _ in world.dig_log)


def test_flee_reports_respawn_instead_of_fake_success(monkeypatch) -> None:
    """A 7000-block 'flee' is a death respawn; it must not look like a successful escape."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="FleeBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        # status(): first call = pre-flee, last call = teleported to world spawn after death
        calls = {"n": 0}

        def fake_status():
            calls["n"] += 1
            if calls["n"] <= 2:
                return {"pos": [0.5, 30.0, 0.5], "health": 12.0, "alive": True, "nearest_hostile": {"name": "zombie", "dist": 3.0}}
            return {"pos": [-152.0, 69.0, -379.0], "health": 20.0, "alive": True, "nearest_hostile": None}

        monkeypatch.setattr(skills.bot, "status", fake_status)
        monkeypatch.setattr(skills.bot, "inventory", lambda *a, **k: {"items": [], "offhand": {}})
        monkeypatch.setattr(skills.bot, "call", lambda *a, **k: {"ok": True})
        res = skills.flee_hostiles(distance=14.0)

    assert res["mode"] == "respawned"
    assert res["ok"] is False
    assert res["moved_dist"] > 64
    assert "死亡重生" in res["note"]


@pytest.mark.parametrize("dry_detour", [True, False])
def test_deep_descent_can_turn_away_from_a_fluid_abort(monkeypatch, dry_detour) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 55.0, 0.5), surface_y=80)
    world.blocks[(0, 55, 0)] = world.blocks[(0, 56, 0)] = "air"
    water = (2, 53, 0)
    world.blocks[water] = "water"
    ore = (1, 53, -1)
    if dry_detour:
        world.blocks[ore] = "iron_ore"
    else:
        world.blocks[(1, 53, -2)] = "water"
    world.blocks[(1, 53, 1)] = "bedrock"

    with BotSession("tok", bot_username="DeepDescentDetour", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 80, -25), parent_id=None, edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng.collar_ceiling_y = collar.anchor[1]
        route = [(0, 80 - step, -25 + step) for step in range(26)]
        node = eng._create_node(
            kind="switchback", anchor=route[-1], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", route),
            branches=[mining.BranchSpec("stair_down", "east", 40, branch_target_y=16)],
        )
        eng.fluid_aborts = mining.FLUID_ABORT_LIMIT - 1
        result = eng.run(target="iron", target_y=16, goal_count=1, max_duration_s=20.0)

    assert world.blocks[water] == "water"
    assert not any(pos == water for pos, _, _ in world.dig_log)
    if dry_detour:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == 1
        assert eng.collar_node_id == collar.node_id
        turn = next(n for n in eng.nodes.values() if n.anchor == (1, 54, 0))
        assert turn.parent_id == node.node_id
        assert turn.edge_from_parent.waypoints == [(0, 55, 0), (1, 54, 0)]
    else:
        assert result["stop_reason"] == "shaft_exhausted"
        assert eng.fluid_abandoned_deep is True
        assert eng.collar_node_id is None
        assert "climb_out" in result["message"]
        assert world.block_at(ore) == "stone"
        [saved] = eng.ledger.saved_shafts
        assert saved["retired"] is True
        assert saved["nodes"][-1]["anchor"] == [1, 54, 0]


def test_reset_on_empty_tree_is_refused_not_a_free_ok() -> None:
    """Live s07/s08: empty-tree reset returned ok:true and the model pressed it 250+ times."""
    world = _MockMineWorld(start_pos=(0.5, 62.0, 0.5), surface_y=62)
    with BotSession("tok", bot_username="EmptyResetBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert not eng.nodes
        res = eng.reset(geofence=None)
    assert res["ok"] is False
    assert res["stop_reason"] == "nothing_to_reset"
    assert "explore" in res["message"]


@pytest.mark.parametrize("floor_collapses", [False, True])
def test_descent_reads_the_landing_support_again_after_excavation(monkeypatch, floor_collapses) -> None:
    anchor, step, floor = (0, 14, 0), (0, 13, -1), (0, 12, -1)
    world = _MockMineWorld(start_pos=(0.5, 14.0, 0.5), surface_y=64)
    world.blocks[anchor] = world.blocks[(0, 15, 0)] = "air"
    moves = []
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "move_to":
            args = json.loads(request.content)
            moves.append((math.floor(args["x"]), math.floor(args["y"]), math.floor(args["z"])))
        reply = world.handler(request)
        if endpoint == "dig" and tuple(json.loads(request.content)["pos"]) == step and floor_collapses:
            assert world.block_at(step) == "air"
            world.blocks[floor] = "air"
        return reply

    with BotSession("tok", bot_username="DescentLandingSupport", transport=httpx.MockTransport(handle)) as bot:
        result = MacroSkills(bot).mining_dfs._dig_stair_down_edge(
            direction="north", max_steps=1, target_y=-54, target_blocks=("diamond_ore",),
            parent_node_id=0, geofence=None, deadline=math.inf, min_hp_ref=[20.0], min_target_tier=3,
        )

    assert world.block_at(step) == "air"
    if floor_collapses:
        assert not moves
        assert result["steps_done"] == 0
        assert result["stop_reason"] == "unsafe_floor_or_cave"
        assert result["end_anchor"] == anchor
    else:
        assert moves == [step]
        assert result["steps_done"] == 1
        assert result["end_anchor"] == step


def test_fluid_abort_keeps_a_dry_deep_junction_with_another_branch() -> None:
    from mc_agent.skills.mining_dfs import BranchSpec, FLUID_ABORT_LIMIT

    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5), surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9},
                         {"name": "cobblestone", "count": 32, "slot": 10}],
    )
    anchor = (0, -54, 0)
    world.blocks[anchor] = world.blocks[(0, -53, 0)] = "air"
    world.blocks[(-1, -55, 0)] = "lava"
    world.blocks[(3, -54, 0)] = "deepslate_diamond_ore"
    with BotSession("tok", bot_username="DryDeepJunction", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="spine", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, anchor]),
            branches=[BranchSpec("branch_leaf", direction, 4) for direction in ("west", "east")],
        )
        engine.target_y = -54
        engine.fluid_aborts = FLUID_ABORT_LIMIT - 1
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=15)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert result["pos_after"][1] == -54
    assert engine.collar_node_id == collar.node_id
    assert world.blocks[(-1, -55, 0)] == "lava"


@pytest.mark.parametrize("block", ["crafting_table", "furnace"])
@pytest.mark.parametrize("alternate_branch", [False, True])
@pytest.mark.parametrize("backtrack", [False, True])
def test_target_depth_rejoin_preserves_utility_blocking_the_corridor(
    monkeypatch, block, alternate_branch, backtrack,
) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 6.5 if backtrack else 0.5),
        surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]}],
    )
    route = [(0, -54, z) for z in range(7)]
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    utility = route[2]
    world.blocks[utility] = block
    if alternate_branch:
        if backtrack:
            world.blocks[(1, -54, 6)] = world.blocks[(1, -53, 6)] = "air"
            world.blocks[(2, -54, 6)] = "deepslate_diamond_ore"
        else:
            world.blocks[(-1, -54, 0)] = "deepslate_diamond_ore"

    def handler(request):
        if request.url.path.endswith("/move_to"):
            args = json.loads(request.content)
            goal_z = args.get("z", world.pos[2])
            if (goal_z - 2.5) * (world.pos[2] - 2.5) <= 0 and world.block_at(utility) == block:
                return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": block}})
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="UtilityBlocksDeepCorridor", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -55), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        junction = engine._create_node(
            kind="spine", anchor=route[0], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, route[0]]),
            branches=[BranchSpec("spine_forward", "west", 4)] if alternate_branch or backtrack else [],
        )
        tip = engine._create_node(
            kind="spine", anchor=route[-1], parent_id=junction.node_id,
            edge_from_parent=ShaftEdge("spine_forward", "south", route),
            branches=[] if backtrack else [BranchSpec("spine_forward", "east", 4)],
        )
        if backtrack and alternate_branch:
            engine._create_node(
                kind="spine", anchor=(1, -54, 6), parent_id=tip.node_id,
                edge_from_parent=ShaftEdge("spine_forward", "east", [tip.anchor, (1, -54, 6)]),
                branches=[BranchSpec("spine_forward", "east", 4)],
            )
        engine.sealed_positions.add(utility)  # Previous seal at this coordinate no longer exists.
        engine.active_node_id = tip.node_id
        engine.collar_ceiling_y = 64
        engine.target_y = -54
        result = engine.run(target="diamond", target_y=-54, goal_count=1)

    assert world.block_at(utility) == block
    assert all(pos != utility for pos, _, _ in world.dig_log)
    assert engine.collar_node_id == collar.node_id and tip.node_id in engine.nodes
    assert result["pos_after"][1] == -54
    if alternate_branch:
        assert result["stop_reason"] == "quota_reached"
    elif backtrack:
        assert result["stop_reason"].startswith("backtrack_blocked@")
        assert f"feet={block}" in result["stop_reason"]
        assert "同层绕行" in result["message"]
    else:
        assert result["stop_reason"] == "shaft_rejoin_blocked"
        assert f"feet={block}" in result["rejoin_error"]


def test_corridor_repair_digs_fill_but_refuses_flooded_cells() -> None:
    """A filled-in corridor cell must be re-opened instead of abandoning the route."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    world.blocks[(0, 20, -1)] = "stone"
    world.blocks[(0, 20, -3)] = "water"
    with BotSession("tok", bot_username="RepairBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert eng._clear_route_obstruction((0, 20, -1)) >= 1      # solid fill -> dug
        assert eng._clear_route_obstruction((0, 20, -3)) == -1     # flooded -> give up


@pytest.mark.parametrize("condition", ["dry", "flat_landing", "missing_support", "adjacent_water", "airborne", "hurt"])
def test_descent_rejoins_excavated_step_after_overshoot(monkeypatch, condition) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(0.5, 63.4, 0.6), surface_y=64)
    world.blocks[(0, 63, 0)] = "air"
    for z in (-2, -3):
        for y in (61, 62, 63):
            world.blocks[(0, y, z)] = "air"
        if condition == "flat_landing":
            world.blocks[(0, 61, z)] = "stone"
    can_rejoin = condition in {"dry", "flat_landing"}
    moves = []
    displaced = False
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal displaced
        path = request.url.path
        if path.endswith("/move_to"):
            args = json.loads(request.content)
            moves.append((args["x"], args["y"], args["z"]))
            if len(moves) <= 2:
                if len(moves) == 2:
                    world.pos = [0.5, 62.0 if condition == "flat_landing" else 61.8, -2.7]
                    displaced = True
                    if condition == "missing_support":
                        world.blocks[(0, 60, -2)] = "air"
                    elif condition == "adjacent_water":
                        world.blocks[(1, 61, -2)] = "water"
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "path_not_found", "message": "missed planned stair"},
                })
            if can_rejoin and moves[-1] == (0.5, 62.0, -0.5):
                world.blocks[(0, 63, -2)] = "iron_ore"
        response = world.handler(request)
        if displaced and path.endswith("/status") and condition in {"airborne", "hurt"}:
            body = response.json()
            if condition == "airborne":
                body["result"]["on_ground"] = False
            else:
                body["result"]["health"] = 18
            return httpx.Response(200, json=body)
        return response

    with BotSession("tok", bot_username="DescentOvershoot", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        if can_rejoin:
            collar = engine._create_node(
                kind="collar", anchor=(0, 63, 0), parent_id=None, edge_from_parent=None,
                branches=[BranchSpec("stair_down", "north", 2)],
            )
            engine.collar_node_id = collar.node_id
            engine.collar_ceiling_y = 63
            result = engine.run(action="explore", target="iron", target_y=61, goal_count=1, max_duration_s=20)
        else:
            edge = engine._dig_stair_down_edge(
                direction="north", max_steps=2, target_y=61, target_blocks=(), parent_node_id=0,
                geofence=None, deadline=math.inf, min_hp_ref=[20.0],
            )

    if can_rejoin:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == world._count("raw_iron") == 1
        assert engine.nodes[engine.active_node_id].edge_from_parent.waypoints == [(0, 63, 0), (0, 61, -2)]
        rejoin_y = 62.0 if condition == "flat_landing" else 61.0
        assert moves[2:4] == [(0.5, rejoin_y, -1.5), (0.5, 62.0, -0.5)]
        assert (0.5, 63.0, 0.5) not in moves
        assert world.pos == [0.5, 61.0, -1.5]
    else:
        assert edge["steps_done"] == 0
        assert edge["stop_reason"] == ("hurt" if condition == "hurt" else "movement_not_confirmed")
        assert len(moves) == 2


@pytest.mark.parametrize("condition", ["dry", "water", "furnace", "hurt", "blocked_waypoint"])
def test_waypoint_rejoin_uses_supported_side_path_around_a_hole(monkeypatch, condition) -> None:
    world = _MockMineWorld(start_pos=(0.5, 35.0, 0.5), surface_y=64)
    for x in range(8):
        for z in (0, 1):
            world.blocks[(x, 35, z)] = world.blocks[(x, 36, z)] = "air"
    world.blocks[(4, 34, 0)] = "air"
    if condition in {"water", "furnace"}:
        world.blocks[(4, 35, 1)] = condition
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/move_to"):
            move = json.loads(request.content)
            if world.pos[2] == 0.5 and world.pos[0] < 4 and move["z"] == 0.5 and move["x"] >= 4.5:
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "unsafe_landing", "message": "missing stair support"},
                })
        response = world.handler(request)
        if condition == "hurt" and request.url.path.endswith("/status") and world.pos[2] == 1.5:
            payload = response.json()
            payload["result"]["health"] = 18
            return httpx.Response(200, json=payload)
        return response

    with BotSession("tok", bot_username="SideRejoin", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        waypoints = [(4, 35, 0), (7, 35, 0)] if condition == "blocked_waypoint" else [(7, 35, 0)]
        result, reason = engine._walk_waypoints(waypoints, min_hp_ref=[20.0])

    if condition in {"dry", "blocked_waypoint"}:
        assert result is True and reason is None
        assert world.pos == [7.5, 35.0, 0.5]
        assert (3, 35, 1) in world.visited_blocks
    elif condition == "hurt":
        assert (result, reason) == (False, "hurt")
        assert world.pos == [3.5, 35.0, 1.5]
    else:
        assert not result and reason.startswith("backtrack_blocked@")
        assert world.pos == [3.5, 35.0, 0.5]
        assert all(z == 0 for _, _, z in world.visited_blocks)
    assert not world.dig_log and not world.place_log


def test_waypoint_walk_interpolates_long_hops() -> None:
    """A 35-block climb must be walked cell by cell; one move_exact over it always fails."""
    from mc_agent.skills.mining_dfs import _interpolate_cells

    hops = _interpolate_cells((0, 63, 0), (0, 28, 0))
    assert len(hops) == 36
    assert hops[0] == (0, 63, 0) and hops[-1] == (0, 28, 0)
    assert all(abs(hops[i + 1][1] - hops[i][1]) == 1 for i in range(len(hops) - 1))


def test_mine_dfs_api_failure_keeps_its_structured_code() -> None:
    from mc_agent.errors import ApiError

    def handler(request):
        raise ApiError("not_loaded", "chunk unavailable")

    with BotSession("tok", bot_username="MiningApiFailure", transport=httpx.MockTransport(handler)) as bot:
        result = json.loads(McToolset(bot).mc_skill_mine_dfs(target="iron"))

    assert result["ok"] is False
    assert result["error"]["code"] == "not_loaded"

