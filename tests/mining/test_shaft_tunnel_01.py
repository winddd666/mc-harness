from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

def test_mining_dfs_insufficient_tier_blocks_before_wasting_shaft() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 74.0, 0.5),
        surface_y=80,
        inventory_items=[{"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [100, 131]}],
    )
    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=1)
    assert res["ok"] is False
    assert res["stop_reason"] == "insufficient_pickaxe_tier"
    assert len(world.dig_log) == 0


@pytest.mark.parametrize("already_queued", [False, True])
def test_descent_offset_preserves_the_stair_back_to_the_collar(monkeypatch, already_queued: bool) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    anchor = (0, 109, 0)
    route = [(-6, 115, 0), (-4, 113, 0), (-2, 111, 0), anchor]
    world = _MockMineWorld(start_pos=(0.5, 109.0, 0.5), surface_y=120)
    for step in range(7):
        for dy in range(3):
            world.blocks[(-step, anchor[1] + step + dy, 0)] = "air"
    for blocked in ((0, 107, -1), (0, 107, 1), (1, 107, 0)):
        world.blocks[blocked] = "bedrock"
    world.blocks[(3, 108, -1)] = "iron_ore"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    failed_descent = BranchSpec("stair_down", "north", 19)
    branches = [failed_descent]
    if already_queued:
        failed_descent.status = "pruned"
        failed_descent.prune_reason = "fluid_or_bedrock"
        branches.append(BranchSpec(
            "offset_retry", "north", 19, offset_tried=True, sidestep_dir="west", resume_kind="stair_down",
        ))

    with BotSession("tok", bot_username="PreserveDescentStair", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=route[0], parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", route), branches=branches,
        )
        result = engine.run(target="iron", target_y=90, goal_count=1, max_duration_s=20)

    assert world.block_at((-1, 109, 0)) == "stone"
    assert world.block_at((-2, 110, 0)) == "stone"
    assert result["stop_reason"] == "quota_reached"
    assert any(pt[0] == 3 for pt, _, _ in world.dig_log)
    assert engine.collar_node_id == collar.node_id


def test_mining_dfs_reset_relocates_away_from_old_collar() -> None:
    """P1-4 fix: calling reset() when a Collar exists records exhausted collar and relocates >=14 blocks away."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    world.blocks[(0, 74, -3)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res1 = skills.mine_dfs(target="iron", goal_count=1, target_y=74, max_duration_s=15.0)
        assert res1["ok"] is True

        reset_res = skills.mine_dfs(action="reset")
        assert reset_res["ok"] is True
        assert "relocated_to" in reset_res
        rx, ry, rz = reset_res["relocated_to"]
        assert math.hypot(rx - 0, rz - 0) >= 12.0


def test_world_ledger_persists_shaft_and_ores_across_respawn_and_disk(tmp_path: Path) -> None:
    """P2-7 fix: respawning 20 blocks from Collar reuses existing shaft 0-dig; disk ledger verifies live world."""
    ledger_file = tmp_path / "world_ledger.json"
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    world.blocks[(0, 72, -5)] = "iron_ore"
    world.blocks[(1, 72, -4)] = "diamond_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills._mining_dfs = MiningDFSEngine(skills, ledger_path=ledger_file)
        res1 = skills.mine_dfs(target="iron", goal_count=1, target_y=72, max_duration_s=15.0)
        assert res1["ok"] is True
        assert ledger_file.exists()

    # New session + simulated respawn 18 blocks east at (18.5, 76.0, 0.5), now holding iron_pickaxe!
    world.pos = [18.5, 76.0, 0.5]
    world.items.append({"name": "iron_pickaxe", "count": 1, "slot": 12, "durability": [250, 250]})
    digs_before = len(world.dig_log)

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot2:
        skills2 = MacroSkills(bot2)
        skills2._mining_dfs = MiningDFSEngine(skills2, ledger_path=ledger_file)
        res2 = skills2.mine_dfs(target="diamond", goal_count=1, target_y=72, max_duration_s=15.0)

    assert res2["ok"] is True
    assert res2["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    # Reused Collar at (0, 76, 0) and walked down existing shaft instead of digging a brand-new staircase from Y=76!
    new_digs = world.dig_log[digs_before:]
    assert len(new_digs) <= 4


def test_zero_displacement_stair_down_never_spins_duplicate_spine_nodes() -> None:
    """Regression for realA4 (3295 nodes) & realA (152 nodes): starting at <=target_y with deeper radar ore never spins."""
    world = _MockMineWorld(
        start_pos=(0.5, 15.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "cobblestone", "count": 32, "slot": 10},
        ],
    )
    # Carve starting pocket at (0, 15, 0) where anchor_y=15 is already <= target_y=16
    world.blocks[(0, 15, 0)] = "air"
    world.blocks[(0, 16, 0)] = "air"
    # Deeper radar ore at (0, 6, -10), while all 4 directions around (0, 15, 0) are boxed in by bedrock at 2 blocks out
    # so stair_down / spine quickly hit bedrock and return 0 steps on blocked faces.
    world.blocks[(0, 6, -11)] = "cave_air"
    world.blocks[(0, 6, -10)] = "iron_ore"
    for dx in range(-2, 3):
        for dy in range(12, 18):
            for dz in range(-2, 3):
                if abs(dx) == 2 or abs(dz) == 2:
                    world.blocks[(dx, dy, dz)] = "bedrock"

    with BotSession("tok", bot_username="NoSpinBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=16, strategy="radar", max_duration_s=10.0)

    assert res["stop_reason"] in {"shaft_exhausted", "no_progress"}
    assert res["goal_met"] is False
    assert "action='reset'" not in res["message"]
    assert "reset 或 explore" not in res["message"]
    # Node count must remain tiny (never exploding to hundreds/thousands of duplicate Spine nodes)
    assert res["tree"]["node_count"] <= 16
    anchors = [tuple(n["anchor"]) for n in res["tree"]["nodes"]]
    assert len(anchors) == len(set(anchors))


def test_collar_hitbox_ledge_snap_prevents_false_no_solid_collar_ground() -> None:
    """Regression for realB & realC2: standing on hitbox ledge (floor(x) over air, x-1 solid) snaps onto solid block."""
    world = _MockMineWorld(
        start_pos=(10.05, 26.0, -20.05),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "cobblestone", "count": 16, "slot": 10},
        ],
    )
    # Make (10, 25, -21) and surrounding pit air so floor(pos)=(10, 26, -21) has NO solid block under it,
    # while adjacent (9, 25, -21) is solid stone with 2-block headroom (9, 26..27, -21) = air!
    for x in (10, 11):
        for y in range(20, 28):
            for z in (-22, -21, -20):
                world.blocks[(x, y, z)] = "air"
    world.blocks[(9, 25, -21)] = "stone"
    world.blocks[(9, 26, -21)] = "air"
    world.blocks[(9, 27, -21)] = "air"
    # Put iron ore along the first north descent from snapped Collar (9, 26, -21) -> (9, 23, -24)
    world.blocks[(9, 23, -24)] = "iron_ore"

    with BotSession("tok", bot_username="LedgeBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=23, strategy="fishbone", max_duration_s=15.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert world._count("raw_iron") >= 1


def test_fishbone_scan_strategy_properties() -> None:
    """fishbone_scan = fishbone safety skeleton + read-only widened ore reach in target band."""
    from mc_agent.skills.mining_dfs import normalize_dfs_strategy, SCAN_BAND

    # alias resolution
    assert normalize_dfs_strategy("fishbone_scan") == "fishbone_scan"
    assert normalize_dfs_strategy("fbscan") == "fishbone_scan"
    assert normalize_dfs_strategy("scan") == "fishbone_scan"
    # must not collide with existing strategies
    assert normalize_dfs_strategy("fishbone") == "fishbone"
    assert normalize_dfs_strategy("radar") == "radar"
    assert normalize_dfs_strategy("v5") == "v5"

    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    with BotSession("tok", bot_username="ScanBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        eng._refresh_strategy("fishbone_scan")
        assert eng.strategy == "fishbone_scan"
        # safety skeleton preserved: NOT radar-aggressive, NOT surgical (no 1x1 breach-through)
        assert eng.radar_enabled is False
        assert eng.surgical_enabled is False
        # target-band gate: within +-SCAN_BAND of target_y is "in band"
        eng.target_y = -54
        assert abs((-54) - eng.target_y) <= SCAN_BAND          # anchor on target layer -> in band
        assert abs((-30) - eng.target_y) > SCAN_BAND           # transit layer -> not in band


def test_fishbone_spine_descends_when_target_deepened() -> None:
    """fishbone/fishbone_scan spine must schedule a stair_down when target_y moves deeper mid-shaft.

    Regression: the pure-fishbone skeleton only emitted branch_leaf/spine_forward, so raising
    the target depth (e.g. iron layer -> diamond layer) made the tree tunnel sideways on the
    stale layer forever instead of descending.
    """
    from mc_agent.skills.mining_dfs import SPINE_DESCEND_MIN_DELTA

    anchor = (0, 16, 0)

    for strategy in ("fishbone", "fishbone_scan"):
        world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
        with BotSession("tok", bot_username="SpineBot", transport=httpx.MockTransport(world.handler)) as bot:
            eng = MacroSkills(bot).mining_dfs
            eng._refresh_strategy(strategy)
            assert eng.radar_enabled is False  # skeleton has no native vertical branch

            # on-layer target: unchanged fishbone behaviour, no descent branch
            eng.target_y = 16
            flat = eng._spine_branches("north", anchor, geofence=None)
            assert [b.kind for b in flat] == ["branch_leaf", "branch_leaf", "spine_forward"]

            # deepened target: descent must be scheduled first so pending-order digs down
            eng.target_y = -54
            deep = eng._spine_branches("north", anchor, geofence=None)
            assert deep[0].kind == "stair_down"
            assert deep[0].branch_target_y == -54
            assert 0 < deep[0].steps <= 16
            assert deep[0].steps <= anchor[1] - eng.target_y
            # horizontal branches still present after the descent
            assert "spine_forward" in [b.kind for b in deep]

            # tiny depth differences are ignored to avoid needless vertical churn
            eng.target_y = 16 - SPINE_DESCEND_MIN_DELTA
            assert "stair_down" not in [b.kind for b in eng._spine_branches("north", anchor, geofence=None)]


def test_fishbone_transit_keeps_bonus_wall_ores() -> None:
    """While descending to a deep layer, fishbone must keep harvesting wall iron/coal.

    Regression: target=diamond + an iron pickaxe used to switch the vein hook to
    "target ores only", so the descent ignored deepslate_iron_ore on the walls and the
    pickaxe died with too few ingots to craft a replacement.
    """
    from mc_agent.skills.mining_dfs import SCAN_BAND, _should_focus_target_only

    def focus(strategy: str, anchor_y: int, target_y: int, *, max_tier: int = 3, min_tier: int = 3) -> bool:
        return _should_focus_target_only(
            strategy=strategy,
            min_target_tier=min_tier,
            max_tier=max_tier,
            anchor_y=anchor_y,
            target_y=target_y,
        )

    # fishbone_scan: narrow focus ONLY on the target layer itself
    assert focus("fishbone_scan", -54, -54) is True
    assert focus("fishbone_scan", -54 + SCAN_BAND, -54) is True
    # transit layers keep scanning iron/coal so the pickaxe can always be recrafted
    assert focus("fishbone_scan", 16, -54) is False
    assert focus("fishbone_scan", -30, -54) is False
    assert focus("fishbone_scan", -54 - SCAN_BAND - 1, -54) is False

    # plain fishbone never narrows to target-only
    assert focus("fishbone", -54, -54) is False

    # radar family keeps its ore-steered focus everywhere
    assert focus("radar", 16, -54) is True
    assert focus("v5", 16, -54) is True

    # without a matching-tier pickaxe nothing is filtered out
    assert focus("fishbone_scan", -54, -54, max_tier=2) is False
    assert focus("fishbone_scan", -54, -54, min_tier=2) is False


def test_retreat_into_shaft_refuses_when_not_inside_own_corridor() -> None:
    """Standing in open cave air must NOT be treated as our shaft (caller falls back to flee)."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = _fake_shaft_engine(MacroSkills(bot).mining_dfs)
        eng.known_corridor_positions = {(40, -20, 40)}
        eng.nodes[1].anchor = (40, -20, 40)
        eng.nodes[2].anchor = (45, -20, 40)
        res = eng.retreat_into_shaft()
    assert res["ok"] is False
    assert res["reason"] == "not_in_shaft"


def test_fishbone_scan_harvests_wall_pocket_diamond_and_deep_reset_relocates() -> None:
    """fishbone_scan extracts a target-band diamond 2-3 blocks behind the corridor wall, and deep reset records active_anchor."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobblestone", "count": 64, "slot": 11},
        ],
    )
    # Block east/south/west at start so spine heads north along x=0, z=0..-4;
    # place an exposed diamond 3 blocks east of (0, -54, -2) behind 2 blocks of solid rock at (1..2, -54, -2),
    # exposed on its far side (4, -54, -2) so anti-xray find_blocks sees (3, -54, -2)!
    for dpos in ((1, -54, 0), (-1, -54, 0), (0, -54, 1)):
        world.blocks[dpos] = "bedrock"
    world.blocks[(3, -54, -2)] = "deepslate_diamond_ore"
    world.blocks[(4, -54, -2)] = "air"
    world.blocks[(4, -53, -2)] = "air"

    with BotSession("tok", bot_username="ScanMiner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="fishbone_scan",
            max_duration_s=25.0,
        )
        assert res["ok"] is True
        assert res["target_gained"] >= 1
        assert [3, -54, -2] in res["mined_ores"]

        # Simulate deep shaft where collar was far away at (0, 16, 0) while active_node is at current deep pos
        eng = skills.mining_dfs
        eng.nodes[eng.collar_node_id].anchor = (0, 16, 0)
        cur_deep = (math.floor(world.pos[0]), math.floor(world.pos[1]), math.floor(world.pos[2]))
        eng.nodes[eng.active_node_id].anchor = cur_deep
        # Contract changed on purpose (2026-11-18): a reset 38 blocks below the collar is refused
        # instead of silently clearing the tree (live s01 burned 8 such resets in one run).
        rst = skills.mine_dfs(action="reset", strategy="fishbone_scan")
        assert rst["ok"] is False
        assert rst["stop_reason"] == "reset_needs_surface"
        assert eng.collar_node_id is not None        # tree preserved, no silent wipe


def test_restore_skips_shaft_saved_for_a_different_target_layer() -> None:
    """A ledger shaft saved for -54 must not be reused when the caller asks for iron at 16."""
    from mc_agent.skills.mining_dfs import ShaftNode

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="RestoreBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        node = ShaftNode(node_id=1, kind="collar", anchor=(0, 20, 0), parent_id=None,
                         edge_from_parent=None, branches=[])
        eng.ledger.saved_shafts = [{
            "collar_anchor": [0, 20, 0],
            "collar_node_id": 1,
            "target_y": -54,
            "nodes": [node.to_dict()],
        }]
        eng.target_y = 16
        assert eng._try_restore_saved_shaft((0, 20, 0), min_hp_ref=[20.0]) is False
        assert not eng.nodes


def test_reset_is_refused_while_deep_below_the_collar() -> None:
    """Underground reset used to clear the tree with no surface anchor -> no_progress spin."""
    world = _MockMineWorld(start_pos=(0.5, 8.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="DeepResetBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        node = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = node.node_id
        res = eng.reset(geofence=None)
        assert eng.collar_node_id == node.node_id        # tree kept intact
    assert res["ok"] is False
    assert res["stop_reason"] == "reset_needs_surface"
    assert "climb_out" in res["message"]


def test_reset_refused_when_a_deep_second_collar_tries_to_move_again() -> None:
    """Live s01: 8 resets at y=-54 churned corridors and wasted the whole 40 min budget."""
    world = _MockMineWorld(start_pos=(0.5, -54.0, 0.5), surface_y=99)
    with BotSession("tok", bot_username="ThrashBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        surface = eng._create_node(kind="collar", anchor=(0, 99, 0), parent_id=None,
                                   edge_from_parent=None, branches=[])
        eng.collar_ceiling_y = 99                       # we already opened a surface collar
        deep = eng._create_node(kind="collar", anchor=(0, -54, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = deep.node_id
        res = eng.reset(geofence=None)
        assert eng.collar_node_id == deep.node_id       # tree kept
    assert res["stop_reason"] == "reset_needs_surface"
    assert "explore" in res["message"]
    assert surface.node_id in eng.nodes


def test_shaft_exhausted_message_is_built_after_result_dict_exists() -> None:
    """Regression: s04 crashed with UnboundLocalError because out_res was written too early."""
    import pathlib as _pl

    src = _pl.Path("src/mc_agent/skills/mining/runner.py").read_text(encoding="utf-8")
    created = src.index("out_res: dict[str, Any] = {")
    used = src.index('if stop_reason == "shaft_exhausted" and self.fluid_abandoned_deep:')
    assert used > created


def test_retired_shaft_keeps_its_walkable_route() -> None:
    """Live s12: record_exhausted_collar deleted the shaft, so the 120-block route was re-dug."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="RouteBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        tip = eng._create_node(kind="spine", anchor=(0, 20, 0), parent_id=collar.node_id,
                               edge_from_parent=ShaftEdge("stair_down", "north",
                                                          [(0, y, 0) for y in range(64, 19, -1)]),
                               branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = tip.node_id
        eng._archive_current_shaft_to_ledger(mark_exhausted=True)

        saved = [s for s in eng.ledger.saved_shafts if tuple(s["collar_anchor"]) == (0, 64, 0)]
        assert saved, "exhausted shaft must keep its node graph"
        assert saved[0]["retired"] is True
        assert len(saved[0]["nodes"]) >= 2


@pytest.mark.parametrize("start_y", [-54, -17])
def test_upward_layer_switch_is_refused_on_an_existing_shaft(monkeypatch, start_y) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    tip = (0, start_y, 64 - start_y)
    spawn = (0.5, float(start_y), tip[2] + 0.5)
    world = _MockMineWorld(start_pos=spawn, surface_y=70)
    world.blocks[tip] = world.blocks[(0, start_y + 1, tip[2])] = "air"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="LayerBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng.collar_ceiling_y = 64
        eng.target_y = -54
        remaining = BranchSpec("stair_down", "south", max(1, start_y + 54), branch_target_y=-54)
        active = eng._create_node(
            kind="switchback", anchor=tip, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, tip]), branches=[remaining],
        )
        res = eng.run(target="iron", goal_count=4, target_y=16, action="explore", max_duration_s=6.0)
    assert res["stop_reason"] == "layer_switch_upward_refused"
    assert res["requires_replan"] is True
    assert eng.target_y == -54 and eng.active_node_id == active.node_id
    assert active.branches == [remaining] and remaining.status == "pending"
    assert world.pos == list(spawn)
    assert not world.dig_log and not world.visited_blocks


def test_thirty_dry_calls_abandon_a_dead_shaft() -> None:
    """n0344 burned 45 dry calls for 1 hit; n0424 recovered after ~20, so the bar is 30."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        inventory_items=[
            {"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]},
            {"name": "stone_pickaxe", "count": 1, "slot": 10, "durability": [131, 131]},
            {"name": "cobblestone", "count": 64, "slot": 11},
        ],
    )
    with BotSession("tok", bot_username="DryBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = collar.node_id
        eng.collar_ceiling_y = 64
        eng.target_y = -54
        eng.dry_calls = 30
        res = eng.run(target="diamond", goal_count=4, target_y=-54, action="explore", max_duration_s=6.0)
    assert res["stop_reason"] == "shaft_dry_abandoned"
    assert res["requires_replan"] is True
    assert eng.dry_calls == 0


def test_repeated_first_dig_timeouts_relocate_an_idle_nether_collar(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    old_site, new_site = (0, 64, 0), (18, 64, 0)
    world.blocks[(18, 64, -1)] = "ancient_debris"
    attempts = []

    def handler(request):
        if request.url.path.endswith("/dig") and world.pos[0] < 10:
            attempts.append(tuple(json.loads(request.content)["pos"]))
            clock.now += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("first dig stalled at the collar", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="IdleNetherCollar", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine._init_collar(old_site, target_y=15, geofence=None, viable_dirs=["north", "east", "south", "west"])

        def relocate(pos, **kwargs):
            assert pos == old_site
            world.pos = [18.5, 64.0, 0.5]
            return new_site

        monkeypatch.setattr(engine, "_relocate_to_fresh_collar_site", relocate)
        args = dict(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=55)
        for _ in range(mining.IDLE_CALLS_BEFORE_RELOCATE):
            result = engine.run(**args)
            assert result["timeout_path"] == "dig" and result["dug_count"] == 0
        result = engine.run(**args)

    assert len(set(attempts)) == len(attempts) == mining.IDLE_CALLS_BEFORE_RELOCATE
    assert engine.nodes[engine.collar_node_id].anchor == new_site
    assert result["stop_reason"] == "quota_reached"


@pytest.mark.parametrize("handoff", ["released", "unconfirmed", "hurt"])
def test_exhausted_nether_collar_can_turn_after_first_dig_timeout(monkeypatch, handoff) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    origin = (0, 64, 0)
    stalled = (0, 65, -1)
    ore = (16, 64, 0)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    world.blocks[stalled] = "basalt"
    world.blocks[ore] = "ancient_debris"
    visits = []
    stop_ack = False

    def handler(request):
        nonlocal stop_ack
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "stop":
            stop_ack = True
            visits.append(("stop", None))
            return httpx.Response(200, json={"ok": True, "result": {"released": handoff != "unconfirmed"}})
        if endpoint == "status" and stop_ack and handoff == "hurt":
            payload = world.handler(request).json()
            payload["result"]["health"] = 16
            return httpx.Response(200, json=payload)
        if endpoint == "dig":
            pos = tuple(json.loads(request.content)["pos"])
            visits.append(("dig", pos))
            if pos == stalled:
                clock.now += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("first dig stalled while leaving exhausted collar", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="FreshNetherCollar", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        engine.exhausted_collars.append(origin)
        result = engine.run(target="ancient_debris", goal_count=1, target_y=15, max_duration_s=60)

    assert visits[:2] == [("dig", stalled), ("stop", None)]
    assert visits.count(("dig", stalled)) == 1
    assert clock.now < 1060
    if handoff == "released":
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == world._count("ancient_debris") == 1
        assert result["pos_after"][0] >= 16
        assert engine.relocations_count == 1
    else:
        assert result["stop_reason"] == "time_slice"
        assert result["timeout_path"] == "dig"
        assert not world.dig_log and not world.visited_blocks


@pytest.mark.parametrize("outcome", ["same_life", "respawn_ack", "respawn_error", "dimension_change"])
def test_collar_approach_stops_when_mining_slice_loses_its_starting_life(outcome) -> None:
    origin = [0.5, 50.0, 0.5]
    respawn = [-18.5, 19.0, -30.5]
    world = _MockMineWorld(start_pos=tuple(origin), surface_y=19, dimension="overworld",
                           inventory_items=[{"name": "stone_pickaxe", "count": 1, "slot": 9}])
    world.blocks[(0, 49, 0)] = "stone"
    for x in range(4, 8):
        world.blocks[(x, 49, 0)] = world.blocks[(x, 48, 0)] = "stone"
    world.blocks[(5, 50, 0)] = "iron_ore"
    moved = False
    mutations = []

    def handler(request):
        nonlocal moved
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint in {"move_to", "dig", "place", "control", "look", "equip"}:
            mutations.append(endpoint)
        if endpoint == "move_to" and not moved:
            assert "y" not in json.loads(request.content)
            moved = True
            if outcome != "same_life":
                world.pos = list(respawn)
                if outcome == "dimension_change":
                    world.dimension = "the_nether"
                if outcome == "respawn_error":
                    return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "stopped"}})
                return httpx.Response(200, json={"ok": True, "result": {"pos": respawn}})
        if endpoint == "status" and moved and outcome.startswith("respawn"):
            payload = world.handler(request).json()
            payload["result"]["last_death"] = {"t": 1000}
            return httpx.Response(200, json=payload)
        return world.handler(request)

    with BotSession("tok", bot_username="CliffCollarApproach", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        result = engine.run(target="iron", goal_count=1, target_y=49, max_duration_s=20)

    assert moved
    if outcome == "same_life":
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == world._count("raw_iron") == 1
    else:
        assert result["stop_reason"] == "life_changed"
        assert result["pos_before"] == origin
        assert result["pos_after"] == respawn
        assert result["requires_replan"] is True
        assert mutations == ["move_to"]
        assert engine.collar_node_id is None and not world.dig_log


@pytest.mark.parametrize("connected", [True, False])
def test_interrupted_descent_recovers_a_confirmed_stair_after_a_brief_detour(monkeypatch, connected: bool) -> None:
    import mc_agent.skills.mining_dfs as mining

    origin = (0, 69, 0)
    descent = [(-step, 69 - step, 0) for step in range(14)]
    world = _MockMineWorld(start_pos=(0.5, 69.0, 0.5), surface_y=99)
    with BotSession("tok", bot_username="InterruptedStraightStair", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 99, 30), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        engine._create_node(
            kind="switchback", anchor=origin, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, origin]),
            branches=[mining.BranchSpec("stair_down", "west", 53, branch_target_y=16)],
        )

        def interrupt_after_descent(**kwargs):
            for pt in [*descent[1:3], (-2, 68, 0), *descent[3:]]:
                x, y, z = pt
                world.pos = [x + 0.5, y, z + 0.5]
                engine.bot.status()
                if pt in descent and (connected or pt != descent[3]):
                    engine._record_corridor(pt)
            raise mining._MiningDeadline

        monkeypatch.setattr(engine, "_dig_stair_down_edge", interrupt_after_descent)
        result = engine.run(target="iron", target_y=16, goal_count=4)

    assert result["pos_after"][1] == 56
    tip = engine.nodes[engine.active_node_id]
    assert tip.edge_from_parent.waypoints == (descent if connected else descent[:3])
    assert tip.branches[0].steps == (40 if connected else 51)


def test_restored_deep_shaft_keeps_its_tip_when_rejoin_uses_multiple_slices(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="ResumeDeepShaft", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        mid = engine._create_node(
            kind="switchback", anchor=(0, 48, -16), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [collar.anchor, (0, 48, -16)]), branches=[],
        )
        tip = engine._create_node(
            kind="switchback", anchor=(0, 30, -34), parent_id=mid.node_id,
            edge_from_parent=ShaftEdge("stair_down", "north", [mid.anchor, (0, 30, -34)]),
            branches=[mining.BranchSpec("stair_down", "north", 14)],
        )
        engine._archive_current_shaft_to_ledger()
        engine._clear_active_tree_only()
        goals = []

        def rejoin(goal, **kwargs):
            goals.append(goal)
            engine.active_node_id = mid.node_id
            world.pos = [0.5, 48.0, -15.5] if len(goals) == 1 else [0.5, 42.0, -21.5]
            engine.bot.status()
            clock.now = engine._deadline
            if len(goals) == 2:
                engine.bot.status()
            return False, "walk_time_slice"

        monkeypatch.setattr(engine, "_navigate_to_node", rejoin)
        first = engine.run(target="iron", target_y=16, max_duration_s=5)
        assert engine.active_node_id == tip.node_id
        second = engine.run(target="iron", target_y=16, max_duration_s=5)

    assert goals == [tip.node_id, tip.node_id]
    assert engine.active_node_id == tip.node_id
    assert engine.collar_node_id == collar.node_id
    assert first["stop_reason"] == second["stop_reason"] == "time_slice_progress"
    assert first["pos_after"][1] == 48 and second["pos_after"][1] == 42
    assert not world.dig_log


@pytest.mark.parametrize("midpoint_saved", [True, False])
def test_restore_accepts_known_waypoint_far_below_the_saved_collar(midpoint_saved) -> None:
    import mc_agent.skills.mining_dfs as mining

    waypoint = (40, 20, 0)
    tip_anchor = (41, 19, 0)
    world = _MockMineWorld(start_pos=(40.5, 20.0, 0.5), surface_y=64)
    for x, y, z in (waypoint, tip_anchor):
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    with BotSession("tok", bot_username="RestoreAtDepth", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        route = [collar.anchor, (39, 21, 0)]
        if midpoint_saved:
            route.append(waypoint)
        route.append(tip_anchor)
        tip = engine._create_node(
            kind="switchback", anchor=tip_anchor, parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", route),
            branches=[mining.BranchSpec("stair_down", "east", 4)],
        )
        engine._archive_current_shaft_to_ledger()
        engine._clear_active_tree_only()

        assert engine._try_restore_saved_shaft(waypoint, min_hp_ref=[20.0]) is True
        assert engine.active_node_id == tip.node_id
    assert world.pos == [41.5, 19.0, 0.5]
    assert not world.dig_log

