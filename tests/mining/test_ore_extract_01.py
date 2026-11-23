from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

@pytest.mark.parametrize("goal_count", [1, 2])
def test_mining_dfs_stops_an_exposed_vein_at_the_requested_iron_gain(goal_count) -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 16.0, 0.5), surface_y=64,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [120, 131]},
            {"name": "raw_iron", "count": 2, "slot": 10},
        ],
    )
    vein = [(x, 16, 1) for x in range(3)]
    for x, y, z in vein:
        world.blocks[(x, y, 0)] = world.blocks[(x, y + 1, 0)] = "air"
        world.blocks[(x, y, z)] = "iron_ore"

    with BotSession("tok", bot_username="IronDeficit", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills.mining_dfs._init_collar((0, 16, 0), target_y=16, geofence=None, viable_dirs=["east"])
        result = skills.mine_dfs(target="iron", target_y=16, goal_count=goal_count, max_duration_s=15)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == goal_count
    assert world._count("raw_iron") == 2 + goal_count
    assert [pos for pos, name, _ in world.dig_log if name == "iron_ore"] == vein[:goal_count]
    assert all(world.block_at(pos) == "iron_ore" for pos in vein[goal_count:])


def test_mining_dfs_catch_block_prevents_hanging_ore_drop_loss() -> None:
    world = _MockMineWorld(start_pos=(0.5, 74.0, 0.5), surface_y=80)
    world.blocks[(1, 74, -1)] = "air"
    world.blocks[(1, 75, -1)] = "air"
    world.blocks[(2, 75, -1)] = "iron_ore"
    world.blocks[(2, 74, -1)] = "air"
    world.blocks[(2, 73, -1)] = "stone"
    world.blocks[(0, 74, -1)] = "air"
    world.blocks[(0, 75, -1)] = "air"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=74, max_duration_s=15.0)

    assert res["ok"] is True
    assert res["target_gained"] >= 1
    assert any(pt == (2, 74, -1) and item == "cobblestone" for pt, item in world.place_log)
    assert world._count("raw_iron") >= 1


def test_mining_dfs_dual_pickaxe_discipline_and_deferred_diamond_reharvest() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 74.0, 0.5),
        surface_y=80,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [100, 131]},
            {"name": "cobblestone", "count": 32, "slot": 10},
        ],
    )
    world.blocks[(1, 74, 0)] = "air"
    world.blocks[(1, 75, 0)] = "air"
    world.blocks[(2, 75, 0)] = "diamond_ore"
    world.blocks[(2, 74, 0)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res1 = skills.mine_dfs(target="iron", goal_count=1, target_y=74, max_duration_s=15.0)
        assert res1["ok"] is True
        assert world.block_at((2, 75, 0)) == "diamond_ore"
        assert any(d["block"] == "diamond_ore" for d in res1["tree"]["deferred_ores"])

        world.items.append({"name": "iron_pickaxe", "count": 1, "slot": 11, "durability": [250, 250]})
        res2 = skills.mine_dfs(target="diamond", goal_count=1, target_y=74, max_duration_s=15.0)

    assert res2["ok"] is True
    assert res2["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    diamond_digs = [h for pt, bn, h in world.dig_log if bn == "diamond_ore"]
    stone_digs = [h for pt, bn, h in world.dig_log if bn == "stone"]
    assert diamond_digs == ["iron_pickaxe"]
    assert all(h == "stone_pickaxe" for h in stone_digs)


def test_deep_explore_skips_travel_for_deferred_ore_outside_pickup_range(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(
        start_pos=(0.5, -20.0, 0.5), surface_y=64,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]}],
    )
    ore = (-100, -45, 0)
    world.blocks[ore] = "deepslate_diamond_ore"
    with BotSession("tok", bot_username="DeepOreFrontier", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(-39, 25, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 64
        tip = engine._create_node(
            kind="switchback", anchor=(0, -20, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [collar.anchor, (0, -20, 0)]),
            branches=[mining.BranchSpec("stair_down", "east", 34, branch_target_y=-54)],
        )
        engine.deferred_ores[ore] = mining.DeferredOreRecord(ore, "deepslate_diamond_ore", 3, collar.node_id)

        def climb_for_ore(goal, **kwargs):
            world.pos = [-38.5, 18.0, 0.5]
            engine.bot.status()
            clock.now = engine._deadline
            return False, "walk_time_slice"

        def descend(**kwargs):
            world.pos = [1.5, -21.0, 0.5]
            engine.bot.status()
            clock.now = engine._deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_navigate_to_node", climb_for_ore)
        monkeypatch.setattr(engine, "_dig_stair_down_edge", descend)
        result = engine.run(target="diamond", target_y=-54, max_duration_s=32)

    assert result["stop_reason"] == "time_slice_progress"
    assert result["pos_after"][1] == -21
    assert engine.nodes[engine.active_node_id].parent_id == tip.node_id
    assert ore in engine.deferred_ores
    assert not world.dig_log


def test_deep_explore_keeps_pending_descent_before_shallow_deferred_ore(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec, DeferredOreRecord

    clock = _MiningClock()
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    origin = (0, 31, 0)
    ore = (1, 50, 18)
    world = _MockMineWorld(start_pos=(0.5, 31.0, 0.5), surface_y=67)
    world.blocks[origin] = world.blocks[(0, 32, 0)] = "air"
    world.blocks[ore] = "iron_ore"
    with BotSession("tok", bot_username="DeepDeferredOre", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 49, 18), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = 67
        tip = engine._create_node(
            kind="switchback", anchor=origin, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, origin]),
            branches=[BranchSpec("stair_down", "north", 15)],
        )
        engine.active_node_id = tip.node_id
        engine.target_y = 16
        engine.deferred_ores[ore] = DeferredOreRecord(ore, "iron_ore", 2, collar.node_id)

        def navigate_upper_ore(goal, **kwargs):
            pytest.fail(f"deferred ore above the active shaft caused navigation to {goal}")

        def descend(**kwargs):
            world.pos = [0.5, 30.0, -0.5]
            engine.bot.status()
            clock.now = engine._deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_navigate_to_node", navigate_upper_ore)
        monkeypatch.setattr(engine, "_dig_stair_down_edge", descend)
        result = engine.run(target="iron", target_y=16, max_duration_s=32)

    assert result["stop_reason"] == "time_slice_progress"
    assert result["pos_after"][1] == 30
    assert engine.nodes[engine.active_node_id].parent_id == tip.node_id
    assert ore in engine.deferred_ores


def test_mining_dfs_deep_descent_two_stone_two_iron_zero_cobble_finds_diamond() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 70.0, 0.5),
        surface_y=70,
        simulate_best_harvest_tool=True,
        track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "stone_pickaxe", "count": 1, "slot": 10, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 11, "durability": [250, 250]},
            {"name": "iron_pickaxe", "count": 1, "slot": 12, "durability": [250, 250]},
        ],
    )
    world.blocks[(0, 70, -1)] = "tall_grass"
    world.blocks[(0, 71, -1)] = "tall_grass"
    world.blocks[(1, 30, -40)] = "gravel"
    world.blocks[(-1, 30, -40)] = "gravel"
    world.blocks[(0, 29, -40)] = "gravel"
    world.blocks[(0, -54, -125)] = "air"
    world.blocks[(0, -54, -126)] = "lava"
    world.blocks[(-9, -54, -125)] = "deepslate_diamond_ore"
    world.blocks[(-9, -54, -126)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=2, target_y=-54, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2
    assert world._count("diamond") >= 2
    assert world.offhand is not None or any(entry.endswith(":off-hand") for entry in world.equip_log)
    assert res["breaches_sealed"] >= 1
    diamond_digs = [h for _, bn, h in world.dig_log if bn == "deepslate_diamond_ore"]
    assert len(diamond_digs) >= 2
    assert all(h == "iron_pickaxe" for h in diamond_digs)


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("ingots, raw_gained, iron_needed", [(0, 3, 0), (2, 1, 0), (2, 0, 1), (3, 0, 0)])
def test_iron_receipt_reports_first_pickaxe_materials_after_incomplete_slice(
    monkeypatch, interrupted, ingots, raw_gained, iron_needed
) -> None:
    from mc_agent.skills.mining_dfs import _MiningDeadline

    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=64)
    if ingots:
        world._add_item("iron_ingot", ingots)
    with BotSession("tok", bot_username="IronPickaxeMaterials", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def mine_slice(**kwargs):
            engine.bot.status()
            engine.bot.inventory()
            if raw_gained:
                world._add_item("raw_iron", raw_gained)
                engine.bot.inventory()
            if interrupted:
                raise _MiningDeadline
            return {"ok": False, "stop_reason": "movement_not_confirmed", "target_gained": raw_gained}

        monkeypatch.setattr(engine, "_run", mine_slice)
        result = engine.run(target="iron", target_y=16, goal_count=8)

    assert result["target_gained"] == raw_gained
    assert result["goal_met"] is False
    assert result["target_inventory_count"] == raw_gained
    assert result["iron_pickaxe_materials"] == {
        "iron_ingot": ingots, "raw_iron": raw_gained, "additional_iron_needed": iron_needed,
    }


@pytest.mark.parametrize("auto_collected", [0, 1])
def test_dfs_collects_remaining_target_stacks_after_real_ore_dig(monkeypatch, auto_collected) -> None:
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]}],
    )
    for z in range(5):
        for y in (-54, -53):
            world.blocks[(0, y, z)] = "air"
    ore = (1, -54, 0)
    world.blocks[ore] = "deepslate_diamond_ore"
    world.blocks[(2, -54, 3)] = "water"

    def handler(request):
        digging_ore = (
            request.url.path.endswith("/dig")
            and tuple(json.loads(request.content).get("pos", [])) == ore
            and world.block_at(ore) == "deepslate_diamond_ore"
        )
        response = world.handler(request)
        if digging_ore and world.block_at(ore) == "air":
            world._consume_item("diamond", world._count("diamond"))
            if auto_collected:
                world._add_item("diamond", auto_collected)
            world.entities[:] = [
                {"id": 101, "name": "item", "item": "diamondx1", "pos": [0.5, -53.8, 2.5]},
                {"id": 102, "name": "item", "item": "diamondx1", "pos": [0.5, -53.8, 3.5]},
                {"id": 103, "name": "item", "item": "diamondx1", "pos": [2.5, -53.8, 3.5]},
                {"id": 104, "name": "item", "item": "dirtx1", "pos": [0.5, -53.8, 5.5]},
            ]
        return response

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="SplitDiamond", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="diamond", target_y=-54, goal_count=auto_collected + 2)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == auto_collected + 2
    assert result["goal_met"]
    receipts = {row["id"]: row["reason"] for row in result["target_drop_receipts"]}
    assert receipts == {101: "picked_up", 102: "picked_up", 103: "unsafe_route"}
    assert world._count("dirt") == 0


def test_dfs_timeout_receipt_distinguishes_ore_breaks_from_target_pickups(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=22)
    world.blocks[(1, 16, 0)] = "coal_ore"
    world.blocks[(2, 16, 0)] = "iron_ore"
    drop_name_for = world._drop_name_for
    monkeypatch.setattr(world, "_drop_name_for", lambda name: "coal" if name == "coal_ore" else drop_name_for(name))

    with BotSession("tok", bot_username="OreBreakReceipt", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def interrupted(*, deadline, **kwargs):
            engine.bot.status()
            engine.bot.inventory()
            engine.bot.call("dig", {"pos": [1, 16, 0], "collect": True})
            engine.bot.call("dig", {"pos": [2, 16, 0], "collect": False})
            engine.bot.call("dig", {"pos": [3, 16, 0], "collect": False})
            engine.bot.inventory()
            clock.now = deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_run", interrupted)
        result = engine.run(target="iron", goal_count=1)

    assert result["stop_reason"] == "time_slice_progress"
    assert result["dug_count"] == 3
    assert result["ore_breaks"] == [
        {"pos": [1, 16, 0], "block": "coal_ore"},
        {"pos": [2, 16, 0], "block": "iron_ore"},
    ]
    assert result["gained"] == {"coal": 1}
    assert result["target_gained"] == 0


def test_shallow_diamond_y_clamp_hud_mine_dfs_guidance_and_craft_no_result_retry() -> None:
    """Verify LLM shallow diamond Y clamp (-30<y<=16 -> -54), HUD mine_dfs hints, and craft_gear 'no result' retry."""
    # 1) HUD guidance checks
    hint_iron = compute_goal_gap_hint(
        {"stone_pickaxe": 1, "cobblestone": 12},
        task="生存探险",
        target_advancement=None,
    )
    assert "mc_skill_mine_dfs(target='iron')" in hint_iron
    assert "mc_skill_staircase" not in hint_iron

    hint_diamond = compute_goal_gap_hint(
        {"iron_pickaxe": 1, "stone_pickaxe": 1},
        task="从当前状态出发挖到钻石（diamond >= 1）",
        target_advancement=None,
    )
    assert "target_y=-54" in hint_diamond
    assert "mc_skill_mine_dfs" in hint_diamond

    # 2) McToolset shallow diamond target_y=12 clamp + craft_gear "server placed no result" self-heal
    world = _MockMineWorld(
        start_pos=(0.5, -52.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 24, "slot": 11},
            {"name": "crafting_table", "count": 1, "slot": 12},
        ],
    )
    world.blocks[(0, -54, -3)] = "deepslate_diamond_ore"
    world.fail_next_craft_no_result = True

    with BotSession("tok", bot_username="ClampBot", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot)
        # Passing shallow target_y=12 to LLM tool must clamp to -54 and mine the Y=-54 diamond!
        dfs_out = json.loads(ts.mc_skill_mine_dfs(target="diamond", goal_count=1, target_y=12))
        assert dfs_out["ok"] is True
        assert dfs_out["clamped_shallow_diamond_y_from"] == 12
        assert dfs_out["target_y"] == -54
        assert world._count("diamond") >= 1

        # Craft furnace when first `/craft` fails with "craft failed: server placed no result"
        craft_res = ts.skills.craft_gear("furnace", count=1)
        assert craft_res["ok"] is True
        assert world._count("furnace") >= 1


def test_explore_deepens_an_existing_iron_spine_before_more_level_digging(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    anchor = (0, 16, 0)
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=60,
                           inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9}])
    world.blocks[anchor] = world.blocks[(0, 17, 0)] = "air"
    world.blocks[(-3, 13, 0)] = "diamond_ore"

    with BotSession("tok", bot_username="RetargetIronSpine", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(44, 60, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine.target_y = 16
        side_branch = BranchSpec("branch_leaf", "south", 8)
        spine = engine._create_node(
            kind="spine", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "west", [collar.anchor, anchor]),
            branches=[side_branch, BranchSpec("spine_forward", "west", 4)],
        )

        def wrong_layer(*args, **kwargs):
            pytest.fail("continued digging the old iron layer before attempting descent")

        monkeypatch.setattr(engine, "_dig_level_edge", wrong_layer)
        result = engine.run(target="diamond", target_y=-54, goal_count=1, strategy="fishbone_scan")

    assert result["stop_reason"] == "quota_reached"
    assert result["pos_after"][1] < anchor[1]
    assert engine.nodes[collar.node_id] is collar
    assert side_branch.status == "pending"
    assert any(node.parent_id == spine.node_id and node.anchor[1] < 16 for node in engine.nodes.values())


def test_pickaxe_low_hint_tells_model_how_many_stone_picks_it_can_craft() -> None:
    """On pickaxe_low the model must be told the exact recraft headroom, not just 'low'."""
    from mc_agent.skills.mining_dfs import _pickaxe_low_hint, _stone_pickaxe_headroom

    # scan6's real inventory when it stalled at Y=-48: stone is abundant, logs untouched
    inv = {"cobbled_deepslate": 248, "cobblestone": 139, "oak_log": 30, "stick": 2}
    n, stone, sticks = _stone_pickaxe_headroom(inv)
    assert stone == 387
    assert sticks == 2 + 2 * (4 * 30)   # 2 sticks + 30 logs -> 120 planks -> 240 sticks
    assert n == 121                      # stick-limited, stone-limited would be 129

    hint = _pickaxe_low_hint(inv)
    assert "121" in hint
    assert "stone_pickaxe" in hint

    # No wood -> harvest a log before trying to craft again.
    no_wood_hint = _pickaxe_low_hint({"cobblestone": 9})
    assert "mc_skill_harvest_logs(target_logs=1)" in no_wood_hint

    # Sticks are made in batches of four from two planks, including mixed planks.
    for planks, expected in [(1, 0), (2, 4), (3, 4)]:
        assert _stone_pickaxe_headroom({"cobblestone": 18, "acacia_planks": planks}) == (expected // 2, 18, expected)
    assert _stone_pickaxe_headroom({"cobblestone": 18, "acacia_planks": 1, "oak_planks": 1}) == (2, 18, 4)

    # no stone at all -> dig some first
    assert "石料不足" in _pickaxe_low_hint({"oak_log": 8})

    # zero-material edge case must not claim a positive count
    n0, _, _ = _stone_pickaxe_headroom({})
    assert n0 == 0


def test_pickaxe_low_without_stone_or_pickaxe_guides_wooden_pickaxe_first() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 80.0, 0.5), surface_y=80,
        inventory_items=[{"name": "dirt", "count": 1, "slot": 9}],
    )
    with BotSession("tok", bot_username="MiningEmptyPickaxeStock", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", target_y=16, max_duration_s=20)

    assert result["stop_reason"] == "pickaxe_low"
    assert "先用 mc_skill_craft_gear(item='wooden_pickaxe', equip_slot='hand')" in result["message"]
    assert not world.dig_log and not world.craft_log


def test_pickaxe_low_with_one_plank_requests_logs_before_recraft(monkeypatch) -> None:
    world = _MockMineWorld(inventory_items=[
        {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [4, 131]},
        {"name": "acacia_planks", "count": 1, "slot": 10},
        {"name": "crafting_table", "count": 1, "slot": 11},
    ])
    world._add_item("cobblestone", 169)
    world._add_item("cobbled_deepslate", 223)
    crafted = []

    def craft_gear(item, **kwargs):
        crafted.append(item)
        return {"ok": False, "error": "missing_ingredients"}

    with BotSession("tok", bot_username="OnePlank", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        monkeypatch.setattr(skills, "craft_gear", craft_gear)
        result = skills.mine_dfs()

    assert result["stop_reason"] == "pickaxe_low"
    assert "mc_skill_harvest_logs(target_logs=1)" in result["message"]
    assert not crafted


@pytest.mark.parametrize("available_supply", ["raw_iron", "crafting_table", "sticks"])
def test_worn_pickaxe_resupply_preserves_mining_tip(monkeypatch, available_supply) -> None:
    from mc_agent.skills import mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(
        start_pos=(0.5, 16.0, 0.5), surface_y=64, track_durability=True,
        inventory_items=[
            {"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [7, 250]},
            {"name": "cobblestone", "count": 32, "slot": 10},
        ],
    )
    world.blocks[(0, 16, 0)] = world.blocks[(0, 17, 0)] = "air"
    if available_supply == "raw_iron":
        world._add_item("raw_iron")
    if available_supply == "sticks":
        world._add_item("stick", 4)
    with BotSession("tok", bot_username="CollarResupply", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -48), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=(0, 16, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, (0, 16, 0)]),
            branches=[mining.BranchSpec("stair_down", "east", 70)],
        )
        if available_supply == "crafting_table":
            engine.ledger.record_utility("crafting_table", (1, 64, -48))
        return_goals = []
        navigation_origins = []

        def navigate_back(goal_node_id, **kwargs):
            return_goals.append(goal_node_id)
            navigation_origins.append(engine.active_node_id)
            if len(return_goals) == 1:
                world.pos = [0.5, 53.0, -36.5]
                engine.active_node_id = collar.node_id
                engine.bot.status()
                clock.now = engine._deadline
                engine.bot.status()
            world.pos = [0.5, 64.0, -47.5]
            engine.active_node_id = collar.node_id
            return True, None

        monkeypatch.setattr(engine, "_navigate_to_node", navigate_back)
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=32)

        assert result["stop_reason"] == "pickaxe_low"
        if available_supply == "sticks":
            assert result["next_action"] == "return_collar"
            assert result["pos_after"][1] == 53
            tip = engine.nodes[navigation_origins[0]]
            assert tip.anchor[1] <= 16
            assert engine.active_node_id == tip.node_id
            assert any(branch.status == "pending" for branch in tip.branches)
            continued = engine.run(action="return_collar", max_duration_s=32)
            assert continued["stop_reason"] == "returned_to_collar"
            assert engine.active_node_id == tip.node_id
            assert return_goals == [collar.node_id, collar.node_id]
        else:
            assert result["next_action"] == "climb_out"
            assert result["pos_after"][1] <= 16
            assert not return_goals
        assert engine.collar_node_id == collar.node_id


@pytest.mark.parametrize("stone_durability,expected_crafts", [(None, 1), (110, 0), (4, 1)])
def test_descent_uses_owned_stone_pickaxe_without_recrafting_each_step(
    monkeypatch, stone_durability, expected_crafts,
) -> None:
    clock = _MiningClock()
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.monotonic", clock.monotonic)
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", clock.sleep)
    inventory = [
        {"name": "wooden_pickaxe", "count": 1, "slot": 9, "durability": [59, 59]},
        {"name": "cobblestone", "count": 48, "slot": 10},
        {"name": "stick", "count": 32, "slot": 11},
        {"name": "crafting_table", "count": 1, "slot": 12},
    ]
    if stone_durability is not None:
        inventory.append({"name": "stone_pickaxe", "count": 1, "slot": 13, "durability": [stone_durability, 131]})
    if stone_durability == 110:
        inventory.append({"name": "stone_pickaxe", "count": 1, "slot": 14, "durability": [110, 131]})
    world = _MockMineWorld(
        start_pos=(0.5, 24.0, 0.5), surface_y=64, inventory_items=inventory, track_durability=True,
    )
    world.blocks[(0, 24, 0)] = world.blocks[(0, 25, 0)] = "air"
    for x in range(8):
        world.blocks[(x, 24 - x, 1)] = "crafting_table"
    world.blocks[(8, 16, 0)] = "iron_ore"

    with BotSession("tok", bot_username="StoneBurnerDescent", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        result = engine._dig_stair_down_edge(
            direction="east", max_steps=8, target_y=16, target_blocks=("iron_ore",),
            parent_node_id=0, geofence=None, deadline=clock.now + 90, min_hp_ref=[20.0], min_target_tier=2,
        )

    assert result["steps_done"] == 8
    assert world._count("raw_iron") == 1
    assert world.craft_log.count(("stone_pickaxe", 1)) == expected_crafts
    assert engine.recrafted_picks == expected_crafts
    assert all(held == "stone_pickaxe" for _, _, held in world.dig_log), world.dig_log
    assert world._count("wooden_pickaxe") == 1


def test_descent_keeps_using_a_stone_pickaxe_across_budget_slices(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    start = (0, 45, 0)
    world = _MockMineWorld(
        start_pos=(0.5, 45.0, 0.5), surface_y=64, track_durability=True,
        inventory_items=[
            {"name": "wooden_pickaxe", "count": 1, "slot": 9, "durability": [59, 59]},
            {"name": "stone_pickaxe", "count": 1, "slot": 10, "durability": [131, 131]},
            {"name": "cobblestone", "count": 48, "slot": 11},
            {"name": "stick", "count": 32, "slot": 12},
            {"name": "crafting_table", "count": 1, "slot": 13},
        ],
    )
    world.blocks[start] = world.blocks[(0, 46, 0)] = "air"
    world.blocks[(21, 24, 0)] = "iron_ore"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/dig"):
            clock.now += 0.65
        return world.handler(request)

    with BotSession("tok", bot_username="StoneAcrossSlices", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        engine = skills.mining_dfs
        engine._init_collar(start, target_y=24, geofence=None, viable_dirs=["east"])
        kwargs = dict(target="iron", target_y=24, goal_count=1, max_duration_s=32)
        first = skills.mine_dfs(**kwargs)
        second = skills.mine_dfs(**kwargs)

    assert first["stop_reason"] == "time_slice_progress"
    assert first["target_gained"] == 0
    assert second["stop_reason"] == "quota_reached"
    assert second["target_gained"] == world._count("raw_iron") == 1
    assert world._count("stone_pickaxe") == 1
    assert world._count("wooden_pickaxe") == 1
    assert not world.craft_log
    assert all(held == "stone_pickaxe" for _, _, held in world.dig_log)


def test_engine_recrafts_burner_stone_pickaxe_when_durability_low(monkeypatch) -> None:
    """Rock digging must auto-craft a stone burner pick instead of stopping at pickaxe_low."""
    world = _MockMineWorld(
        start_pos=(0.5, 80.0, 0.5),
        surface_y=80,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [40, 131]},
            {"name": "cobblestone", "count": 30, "slot": 10},
            {"name": "oak_log", "count": 4, "slot": 11},
            {"name": "crafting_table", "count": 1, "slot": 12},
        ],
    )
    with BotSession("tok", bot_username="RecraftBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        crafted: list[str] = []

        def fake_craft(item: str, count: int = 1, equip_slot: str | None = None) -> dict[str, Any]:
            crafted.append(item)
            world._add_item(item, count)
            return {"ok": True, "crafted": item, "gained": count}

        monkeypatch.setattr(skills, "craft_gear", fake_craft)
        counts = {"cobblestone": 30, "oak_log": 4, "crafting_table": 1, "stone_pickaxe": 1}
        assert eng._try_recraft_burner_pickaxe(counts) is True
        assert crafted == ["stone_pickaxe"]
        assert eng.recrafted_picks == 1

        # no stone -> must not even attempt a craft
        assert eng._try_recraft_burner_pickaxe({"cobblestone": 1, "oak_log": 4}) is False
        assert crafted == ["stone_pickaxe"]

        # craft failure must not be counted as a recraft
        monkeypatch.setattr(skills, "craft_gear", lambda *a, **k: {"ok": False, "error": "missing_ingredients"})
        assert eng._try_recraft_burner_pickaxe(counts) is False
        assert eng.recrafted_picks == 1
        monkeypatch.setattr(skills, "craft_gear", lambda *a, **k: {"ok": True, "already_owned": True, "gained": 0})
        assert eng._try_recraft_burner_pickaxe(counts) is False
        assert eng.recrafted_picks == 1

        counts["stone_pickaxe"] = world._count("stone_pickaxe")
        monkeypatch.setattr(skills, "craft_gear", lambda *a, **k: {"ok": True, "crafted": "stone_pickaxe", "gained": 1})
        assert eng._try_recraft_burner_pickaxe(counts) is False
        assert eng.recrafted_picks == 1

        # BUG-144: holding a leftover wooden_pickaxe alongside stone_pickaxe must choose stone_pickaxe for tier-1 rock
        # and must never recraft beyond 2 stone pickaxes.
        from mc_agent.skills.mining_dfs import _choose_pickaxe_for_tier
        picks = [
            {"name": "wooden_pickaxe", "tier": 1, "remaining": 50, "in_offhand": False},
            {"name": "stone_pickaxe", "tier": 2, "remaining": 120, "in_offhand": False},
            {"name": "iron_pickaxe", "tier": 3, "remaining": 240, "in_offhand": True},
        ]
        chosen = _choose_pickaxe_for_tier(picks, required_tier=1)
        assert chosen is not None and chosen["name"] == "stone_pickaxe"
        assert eng._should_recraft_burner_pickaxe(chosen, total_rem=410) is False
        monkeypatch.setattr(skills, "craft_gear", fake_craft)
        assert eng._try_recraft_burner_pickaxe({**counts, "stone_pickaxe": 2}) is False


def test_retreat_walls_off_threat_side_before_walking(monkeypatch) -> None:
    """Being hit by an adjacent mob must seal the threat side first (walking away only trades hits)."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = _fake_shaft_engine(MacroSkills(bot).mining_dfs)
        sealed: list[list] = []
        monkeypatch.setattr(
            eng, "_walk_waypoints",
            lambda wps, reverse=False, min_hp_ref=None: (True, None),
        )
        monkeypatch.setattr(
            eng, "_seal_breach",
            lambda coords, cache, allow_corridor=False: (sealed.append(list(coords)) or [list(c) for c in coords]),
        )
        res = eng.retreat_into_shaft(max_hops=1, threat_pos=(0.5, 30.0, 12.5))

    assert res["ok"] is True
    wall = sealed[0]                      # first seal call is the threat-facing wall
    assert len(wall) == 9                 # 3 columns x 3 high
    assert all(c[2] == 1 for c in wall)   # +z side, i.e. toward the threat
    assert res["sealed_first"] == 9


def test_entry_gate_recrafts_before_reporting_pickaxe_low(monkeypatch) -> None:
    """No pickaxe at call entry must trigger an in-place recraft, not an immediate pickaxe_low bail."""
    world = _MockMineWorld(
        start_pos=(0.5, 20.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "cobblestone", "count": 64, "slot": 10},
            {"name": "oak_log", "count": 8, "slot": 11},
        ],
    )
    with BotSession("tok", bot_username="GateBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        calls = []
        monkeypatch.setattr(skills, "craft_gear",
                            lambda item, count=1, equip_slot=None: (calls.append(item) or {"ok": True}))
        res = eng.run(target="iron", goal_count=1, target_y=16, action="explore", max_duration_s=5.0)
    assert calls == ["stone_pickaxe"]          # recraft attempted at the gate
    assert res.get("stop_reason") != "pickaxe_low" or calls

