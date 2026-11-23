from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

def test_mining_dfs_collar_switchback_seal_and_spine() -> None:
    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    world.blocks[(0, 74, -4)] = "lava"
    world.blocks[(0, 75, -4)] = "air"
    world.blocks[(0, 76, -4)] = "air"
    for ox in (-3, -4, -5):
        world.blocks[(ox, 74, -3)] = "iron_ore"
    for ox in (3, 4, 5):
        world.blocks[(ox, 74, -3)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=3, target_y=74, max_duration_s=25.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 3
    assert res["goal_met"] is True
    assert res["breaches_sealed"] >= 1
    assert res["tree"]["sealed_count"] >= 1
    node_kinds = {n["kind"] for n in res["tree"]["nodes"]}
    assert "collar" in node_kinds
    assert "switchback" in node_kinds or "spine" in node_kinds


def test_mining_dfs_side_fluid_seals_and_continues_without_false_prune() -> None:
    """P0-1 fix: lateral water adjacent to descent/tunnel is sealed with cobblestone instead of pruning entire direction."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    # Block east/south/west with in-path bedrock so ONLY north can reach target_y=70
    for dpos in ((1, 75, 0), (-1, 75, 0), (0, 75, 1)):
        world.blocks[dpos] = "bedrock"
    # Put 1-block lateral water pocket right beside the north descent at (1, 74, -2)
    world.blocks[(1, 74, -2)] = "water"
    # Target iron ore further down the north staircase at (0, 70, -7)
    world.blocks[(0, 70, -7)] = "iron_ore"
    world.blocks[(0, 71, -7)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=70, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2
    # Lateral water pocket at (1, 74, -2) was plugged with cobblestone!
    assert world.block_at((1, 74, -2)) == "cobblestone"
    assert res["breaches_sealed"] >= 1


def test_mining_dfs_64_block_radar_steers_spine_toward_distant_diamond() -> None:
    """P1-5 fix: 64-block radar detects exposed diamond ore 18 blocks east in cave wall and steers Spine east."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobblestone", "count": 32, "slot": 11},
        ],
    )
    # Small exposed cave pocket 16 blocks east at (16, -54, -4) with exposed deepslate_diamond_ore at (15, -54, -4)
    world.blocks[(16, -54, -4)] = "air"
    world.blocks[(16, -53, -4)] = "air"
    world.blocks[(15, -54, -4)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=1, target_y=-54, max_duration_s=25.0, strategy="radar")

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    # Verify Spine steered east (+X) rather than wasting durability going far west (-X)
    min_dug_x = min((pt[0] for pt, _, _ in world.dig_log), default=0)
    assert min_dug_x >= -4


def test_dual_strategy_fishbone_ignores_distant_cave_while_radar_triages_hostiles_and_extracts_surgically(
    monkeypatch: Any,
) -> None:
    """Compare `fishbone` vs `v5` (bug-fixed v4) vs `radar` (with hostile triage + LocC-v4 surgical parapet)."""
    monkeypatch.delenv("MC_DFS_STRATEGY", raising=False)
    assert normalize_dfs_strategy(None) == "radar"
    monkeypatch.setenv("MC_DFS_STRATEGY", "fishbone")
    assert normalize_dfs_strategy(None) == "fishbone"
    monkeypatch.setenv("MC_DFS_STRATEGY", "v5")
    assert normalize_dfs_strategy(None) == "v5"
    monkeypatch.setenv("MC_DFS_STRATEGY", "radar")
    assert normalize_dfs_strategy(None) == "radar"

    # 1) Conservative `fishbone`: mines corridor-adjacent diamond, completely ignores 15-block distant cave diamond
    fb_world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    # Distant exposed cave diamond 15 blocks east (should NOT be chased by fishbone)
    fb_world.blocks[(16, -54, -2)] = "cave_air"
    fb_world.blocks[(16, -53, -2)] = "cave_air"
    fb_world.blocks[(15, -54, -2)] = "deepslate_diamond_ore"
    # Corridor-adjacent diamond right beside the north Spine at (1, -54, -3)
    fb_world.blocks[(1, -54, -3)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="FishboneBot", transport=httpx.MockTransport(fb_world.handler)) as bot_fb:
        skills_fb = MacroSkills(bot_fb)
        res_fb = skills_fb.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="fishbone",
            max_duration_s=20.0,
        )

    assert res_fb["ok"] is True
    assert res_fb["strategy"] == "fishbone"
    assert res_fb["tree"]["strategy"] == "fishbone"
    assert res_fb["tree"]["radar_targets"] == []
    assert fb_world._count("diamond") >= 1
    assert fb_world.block_at((15, -54, -2)) == "deepslate_diamond_ore"

    # 1.5) Moderate `v5` (bug-fixed original v4):
    #      - Uses 64-block single-point radar to steer east toward (15, -54, -2) when reachable at Y=-54!
    #      - Does NOT perform cluster locking (`locked_cluster is None`) or surgical pocket breaching (`surgical_extractions == 0`).
    v5_world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    v5_world.blocks[(14, -54, -2)] = "cave_air"
    v5_world.blocks[(14, -53, -2)] = "cave_air"
    v5_world.blocks[(13, -54, -2)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="V5Bot", transport=httpx.MockTransport(v5_world.handler)) as bot_v5:
        skills_v5 = MacroSkills(bot_v5)
        res_v5 = skills_v5.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="v5",
            max_duration_s=20.0,
        )

    assert res_v5["ok"] is True
    assert res_v5["strategy"] == "v5"
    assert res_v5["tree"]["strategy"] == "v5"
    assert res_v5["radar_summary"]["ever_seen_count"] >= 1
    assert res_v5["radar_summary"]["locked_cluster"] is None
    assert res_v5["tree"]["surgical_extractions"] == 0
    assert v5_world._count("diamond") >= 1
    assert min((pt[0] for pt, _, _ in v5_world.dig_log), default=0) >= -4

    # 2) Aggressive `radar`:
    #    - West cluster (-10, -54, 0) has 2 diamonds exposed to cave_air (-11, -54, 0) WITH a zombie right inside!
    #    - North-East target (3, -56, -3) replicates LocC-v4 ([3484, -56, 427] from [3481, -54, 424]):
    #      occluded behind 2 solid deepslate blocks & 2m below Y=-54, backed by natural `cave_air` on its far side (x=4),
    #      with 0 hostiles nearby.
    rd_world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    # Dangerous monster-guarded west cluster
    rd_world.blocks[(-11, -54, 0)] = "cave_air"
    rd_world.blocks[(-11, -53, 0)] = "cave_air"
    rd_world.blocks[(-10, -54, 0)] = "deepslate_diamond_ore"
    rd_world.blocks[(-10, -54, -1)] = "deepslate_diamond_ore"
    rd_world.entities.append({"id": 501, "name": "zombie", "type": "hostile", "pos": [-11.5, -54.0, 0.5]})

    # Safe mob-free cave-backed occluded diamond replicating LocC-v4 geometry:
    cave_cells = {(4, -56, -3), (4, -55, -3), (4, -54, -3), (5, -56, -3), (5, -55, -3)}
    for cpt in cave_cells:
        rd_world.blocks[cpt] = "cave_air"
    rd_world.blocks[(3, -56, -3)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="RadarBot", transport=httpx.MockTransport(rd_world.handler)) as bot_rd:
        skills_rd = MacroSkills(bot_rd)
        res_rd = skills_rd.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="radar",
            max_duration_s=25.0,
        )

    assert res_rd["ok"] is True
    assert res_rd["strategy"] == "radar"
    assert rd_world._count("diamond") >= 1
    # Must avoid the zombie-guarded west cluster completely
    assert min((pt[0] for pt, _, _ in rd_world.dig_log), default=0) >= -2
    # Must NEVER step into natural `cave_air` cells at x>=4!
    assert all(vpt[0] < 4 for vpt in rd_world.visited_blocks)


def test_radar_stair_up_homing_reaches_overhead_ore_cluster() -> None:
    """Radar strategy climbs upward via `stair_up` when the locked exposed ore cluster is >=3 blocks above Spine."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    # Overhead diamond cluster at Y=-49 (5 blocks above Y=-54), exposed on its north face to a tiny cave pocket
    world.blocks[(0, -49, -7)] = "cave_air"
    world.blocks[(0, -48, -7)] = "cave_air"
    world.blocks[(0, -49, -6)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="RadarClimber", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=1, target_y=-54, strategy="radar", max_duration_s=25.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    assert any(n.get("edge_kind") == "stair_up" for n in res["tree"]["nodes"])


def test_timed_out_radar_ascent_reports_confirmed_ore_route_progress(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    world.blocks[(0, -54, 0)] = world.blocks[(0, -53, 0)] = "air"

    def handler(request):
        if request.url.path.endswith("/dig") and world.pos[1] == -43:
            raise httpx.ReadTimeout("next step exceeded the slice", request=request)
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="TimedRadarAscent", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        node = engine._create_node(
            kind="spine", anchor=(0, -54, 0), parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("stair_up", "east", 15, branch_target_y=-39)],
        )
        engine.collar_node_id = engine.active_node_id = node.node_id
        engine.target_y = -54
        result = engine.run(target="diamond", target_y=-54, goal_count=16, strategy="radar", max_duration_s=15)

    assert result["pos_after"][1] == -43
    assert result["stop_reason"] == "time_slice_progress"
    assert result["ok"] is True
    assert result["planned_ascent_blocks"] == 11
    assert "progress_lost" not in result


def test_retreat_into_shaft_walks_back_and_seals_behind(monkeypatch) -> None:
    """Threat response must stay inside our own tunnel: back up one node and plug the choke."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = _fake_shaft_engine(MacroSkills(bot).mining_dfs)
        walked: list[tuple[list, bool]] = []
        sealed: list[list] = []
        monkeypatch.setattr(
            eng, "_walk_waypoints",
            lambda wps, reverse=False, min_hp_ref=None: (walked.append((list(wps), reverse)) or (True, None)),
        )
        monkeypatch.setattr(
            eng, "_seal_breach",
            lambda coords, cache, allow_corridor=False: (sealed.append(list(coords)) or [list(c) for c in coords]),
        )

        res = eng.retreat_into_shaft(max_hops=1)

    assert res["ok"] is True
    assert res["mode"] == "retreat_shaft"
    assert res["hops"] == 1
    assert res["sealed"] == 3
    assert eng.active_node_id == 1                    # handed back to the collar node
    assert walked and walked[0][1] is True            # walked the edge in reverse
    assert sealed and sealed[0] == [(0, 30, 0), (0, 31, 0), (0, 32, 0)]  # plugged the branch cross-section


def test_flee_hostiles_prefers_shaft_retreat_over_free_pathfinding(monkeypatch) -> None:
    """flee_hostiles must route into the shaft retreat when the engine has a live tree."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)

        class _StubEngine:
            nodes = {1: object()}

            def retreat_into_shaft(self, **kwargs):
                return {"ok": True, "mode": "retreat_shaft", "hops": 1, "sealed": 2}

        skills._mining_dfs = _StubEngine()
        res = skills.flee_hostiles(distance=14.0)

    assert res["mode"] == "retreat_shaft"
    assert res["hops"] == 1


def test_three_fluid_aborts_mark_shaft_as_water_trap() -> None:
    """Aquifers used to cost whole runs (6 drown deaths): 3 hazard aborts must force a new shaft."""
    world = _MockMineWorld(start_pos=(0.5, 40.0, 0.5), surface_y=40)
    with BotSession("tok", bot_username="WaterBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert eng._note_fluid_abort("fluid_or_bedrock") is False
        assert eng._note_fluid_abort("unsafe_floor_or_cave") is False
        assert eng._note_fluid_abort("fluid_or_bedrock") is True        # 3rd hazard abort

        eng.fluid_aborts = 2
        # neutral outcomes keep the streak (still exploring the same wet layer)
        assert eng._note_fluid_abort("completed") is False
        # a real setback like hurt resets the streak
        assert eng._note_fluid_abort("hurt") is False
        assert eng.fluid_aborts == 0


def test_manual_reset_clears_the_hazard_streak() -> None:
    """Live s03: stale fluid_aborts made reset -> explore return shaft_exhausted instantly, x6."""
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=71)
    with BotSession("tok", bot_username="StreakBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.fluid_aborts = 3
        eng.fluid_abandoned_deep = True
        eng.collar_ceiling_y = 71
        node = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = node.node_id
        res = eng.reset(geofence=None)                 # allowed: we are at the collar level
    assert res["ok"] is True
    assert eng.fluid_aborts == 0
    assert eng.fluid_abandoned_deep is False


def test_drowning_counts_toward_water_trap_abandonment() -> None:
    """Live s12: 3 successful escapes still left fluid_aborts at 0, so the pool was never abandoned."""
    world = _MockMineWorld(start_pos=(0.5, 17.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="WetBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert eng._note_fluid_abort("in_water") is False
        assert eng._note_fluid_abort("fluid_or_bedrock") is False
        assert eng._note_fluid_abort("in_water") is True     # 3rd water-related abort


def test_sealing_cave_breaches_does_not_condemn_a_healthy_shaft() -> None:
    """Live s11: cave-heavy terrain hit 3 seals in a row and the shaft was abandoned at y=54."""
    world = _MockMineWorld(start_pos=(0.5, 54.0, 0.5), surface_y=61)
    with BotSession("tok", bot_username="CaveBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        for _ in range(6):
            assert eng._note_fluid_abort("cave_breach_sealed") is False
        assert eng.fluid_aborts == 0


@pytest.mark.parametrize("condition", ["transient", "persistent", "water", "unsupported", "hurt", "utility"])
def test_level_excavation_retries_one_transient_safe_step(monkeypatch, condition) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    world.blocks[(0, 20, 0)] = world.blocks[(0, 21, 0)] = "air"
    first_step_moves = 0
    health = 20
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    def handler(request):
        nonlocal first_step_moves, health
        if request.url.path.endswith("/move_to") and json.loads(request.content)["x"] == 1.5:
            first_step_moves += 1
            if first_step_moves == 2:
                if condition == "water":
                    world.blocks[(1, 20, 0)] = "water"
                elif condition == "unsupported":
                    world.blocks[(1, 19, 0)] = "air"
                elif condition == "hurt":
                    health = 16
                elif condition == "utility":
                    world.blocks[(1, 20, 0)] = "furnace"
            if first_step_moves <= 2 or condition == "persistent":
                return httpx.Response(200, json={
                    "ok": False, "error": {"code": "path_not_found", "message": "step blocked"},
                })
        response = world.handler(request)
        if request.url.path.endswith("/status"):
            body = response.json()
            body["result"]["health"] = health
            return httpx.Response(200, json=body)
        return response

    with BotSession("tok", bot_username="RetryLevelStep", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        edge = engine._dig_level_edge(
            direction="east", steps=2, target_blocks=(), parent_node_id=0,
            geofence=None, deadline=math.inf, min_hp_ref=[20.0],
        )

    if condition == "transient":
        assert edge["steps_done"] == 2
        assert edge["stop_reason"] == "completed"
        assert first_step_moves == 3
        assert world.pos == [2.5, 20.0, 0.5]
    else:
        assert edge["steps_done"] == 0
        assert edge["stop_reason"] == ("hurt" if condition == "hurt" else "movement_not_confirmed")
        assert first_step_moves == (4 if condition == "persistent" else 2)
        assert world.pos == [0.5, 20.0, 0.5]
        if condition == "utility":
            assert world.block_at((1, 20, 0)) == "furnace"


def test_dfs_budget_includes_entry_observations_and_radar(monkeypatch) -> None:
    """Initialization used to run outside the slice, then get a fresh full deadline."""
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    calls: list[str] = []

    def slow_handler(request):
        calls.append(request.url.path)
        clock.now += 0.4
        return world.handler(request)

    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    with BotSession("tok", bot_username="TimedInit", transport=httpx.MockTransport(slow_handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", max_duration_s=5.0)
    assert clock.now <= 1005.0
    assert result["stop_reason"] == "time_slice"
    assert len(calls) < 15


@pytest.mark.parametrize("released", [False, True])
def test_first_dig_timeout_does_not_block_the_next_safe_descent(monkeypatch, released) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether",
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    stalled = (1, 63, 0)
    world.blocks[(0, 62, -2)] = "ancient_debris"
    attempts = []

    def handler(request):
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"ok": True, "result": {"released": released}})
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content)["pos"]) == stalled:
            attempts.append(clock.now)
            clock.now += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("first dig stalled", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="FirstDigTimeout", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 64, 0), parent_id=None, edge_from_parent=None,
            branches=[mining.BranchSpec("stair_down", direction, 49) for direction in ("east", "north")],
        )
        engine.collar_node_id = collar.node_id
        first = engine.run(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=32)
        if released:
            resumed = first
        else:
            assert first["stop_reason"] == "time_slice"
            assert first["timeout_path"] == "dig"
            assert first["dug_count"] == 0
            assert not world.dig_log
            resumed = engine.run(target="ancient_debris", target_y=15, goal_count=1, max_duration_s=32)

    assert len(attempts) == 1
    assert resumed["stop_reason"] == "quota_reached"
    assert resumed["target_gained"] == 1
    assert engine.collar_node_id == collar.node_id


@pytest.mark.parametrize("dry_relocation", [False, True])
def test_dry_relocation_after_water_preemption_does_not_reenter_the_old_shaft(monkeypatch, dry_relocation) -> None:
    from mc_agent.harness.guardian import ReflexPreempted
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(0.5, 60.0, 0.5), surface_y=64, dimension="overworld")
    old_collar = (0, 60, 0)
    wet_cell = (0, 58, -2)
    new_start = (27, 64, 4) if dry_relocation else (0, 60, 2)
    world.blocks[old_collar] = world.blocks[(0, 61, 0)] = "air"
    world.blocks[(new_start[0], new_start[1] - 1, new_start[2])] = "stone"
    abort = True
    moves = []

    def handler(request):
        nonlocal abort
        if request.url.path.endswith("/dig") and abort:
            abort = False
            raise ReflexPreempted({
                "source": "tool_water_threat",
                "observed_life": ("overworld", None),
                "water_observation": {"pos": list(wet_cell), "in_water": True},
            })
        if request.url.path.endswith("/move_to"):
            body = json.loads(request.content)
            moves.append((math.floor(body["x"]), math.floor(body["z"])))
        return world.handler(request)

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="TryDryEntry", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=old_collar, parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("stair_down", "north", 44)],
        )
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = old_collar[1]
        args = dict(target="iron", target_y=16, goal_count=1, max_duration_s=20)
        with pytest.raises(ReflexPreempted):
            engine.run(**args)
        world.pos[:] = [new_start[0] + 0.5, float(new_start[1]), new_start[2] + 0.5]
        moves.clear()

        if dry_relocation:
            def confirmed_descent(*, parent_node_id, **kwargs):
                assert engine.nodes[parent_node_id].anchor == new_start
                world._add_item("raw_iron", 1)
                return {
                    "steps_done": 0, "end_anchor": list(new_start), "waypoints": [new_start],
                    "stop_reason": "quota_reached", "breaches_sealed": 0, "catch_placed": 0,
                    "deferred_added": 0, "mined_ores": [],
                }
            monkeypatch.setattr(engine, "_dig_stair_down_edge", confirmed_descent)
        result = engine.run(**args)

    assert (old_collar[0], old_collar[2]) not in moves
    assert any(sh["collar_anchor"] == list(old_collar) for sh in engine.ledger.saved_shafts)
    if dry_relocation:
        assert result["target_gained"] == 1
        assert engine.nodes[engine.collar_node_id].anchor == new_start
    else:
        assert result["stop_reason"] == "water_reentry_avoided"
        assert engine.nodes[engine.collar_node_id].anchor == old_collar
        assert not moves


@pytest.mark.parametrize("safe_exit", [True, False])
def test_water_preemption_can_reanchor_after_a_short_dry_move_at_mining_depth(monkeypatch, safe_exit) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    wet_cell, dry_cell = (0, -55, 0), (8, -53, 0)
    world = _MockMineWorld(start_pos=(8.5, -53.0, 0.5), surface_y=64, dimension="overworld")
    world.blocks[wet_cell] = "water"
    world.blocks[dry_cell] = world.blocks[(8, -52, 0)] = "air"
    if safe_exit:
        world.blocks[(9, -53, 0)] = "deepslate_iron_ore"
    else:
        for x, z in ((8, -1), (9, 0), (8, 1)):
            world.blocks[(x, -53, z)] = "water"
    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)

    with BotSession("tok", bot_username="DryDeepEntry", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        old = engine._create_node(
            kind="spine", anchor=wet_cell, parent_id=None, edge_from_parent=None,
            branches=[BranchSpec("spine_forward", "east", 8)],
        )
        engine.collar_node_id = old.node_id
        engine.collar_ceiling_y = 64
        engine.target_y = -50
        assert engine._remember_water_interruption({
            "source": "tool_water_threat",
            "observed_life": ("overworld", None),
            "water_observation": {"pos": list(wet_cell), "in_water": True},
        }, bot.status())

        reset = engine.run(action="reset")
        assert reset["stop_reason"] == "reset_needs_surface"
        assert engine.collar_node_id == old.node_id
        result = engine.run(action="explore", target="iron", target_y=-50, goal_count=1, max_duration_s=20)

    if safe_exit:
        assert result["stop_reason"] == "quota_reached"
        assert result["target_gained"] == world._count("raw_iron") == 1
        assert engine.nodes[engine.collar_node_id].anchor == dry_cell
        assert all(branch.direction != "west" for branch in engine.nodes[engine.collar_node_id].branches)
        assert any(sh["collar_anchor"] == list(wet_cell) for sh in engine.ledger.saved_shafts)
    else:
        assert result["stop_reason"] == "water_reentry_avoided"
        assert engine.nodes[engine.collar_node_id] is old
        assert not world.dig_log
    assert engine.collar_ceiling_y == 64
    assert wet_cell not in world.visited_blocks


def test_backtrack_tries_another_lower_branch_when_the_deepest_is_sealed(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 39.0, 21.5), surface_y=64)
    world.blocks[(0, 37, 23)] = world.blocks[(0, 38, 23)] = "air"
    world.blocks[(1, 36, 23)] = "iron_ore"
    attempted = []
    with BotSession("tok", bot_username="ResumeAfterSealing", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        transit = engine._create_node(
            kind="switchback", anchor=(0, 41, 19), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, (0, 41, 19)]),
            branches=[mining.BranchSpec("stair_down", direction, 25, status="completed") for direction in mining.DIRECTIONS],
        )
        nearby = engine._create_node(
            kind="switchback", anchor=(0, 37, 23), parent_id=transit.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [transit.anchor, (0, 39, 21), (0, 37, 23)]),
            branches=[mining.BranchSpec("stair_down", "east", 21)],
        )
        deepest = engine._create_node(
            kind="switchback", anchor=(0, 15, 45), parent_id=nearby.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [nearby.anchor, (0, 15, 45)]),
            branches=[mining.BranchSpec("stair_down", "south", 35)],
        )
        engine.active_node_id = transit.node_id

        def navigate(goal, **kwargs):
            attempted.append(goal)
            if goal == deepest.node_id:
                return False, "backtrack_blocked"
            assert goal == nearby.node_id
            world.pos = [0.5, 37.0, 23.5]
            engine.active_node_id = goal
            return True, None

        def walk_up(*args, **kwargs):
            pytest.fail("backtracked upward before trying the nearby pending branch")

        monkeypatch.setattr(engine, "_navigate_to_node", navigate)
        monkeypatch.setattr(engine, "_walk_waypoints", walk_up)
        result = engine.run(target="iron", target_y=16, goal_count=1, max_duration_s=32)

    assert attempted == [deepest.node_id, nearby.node_id]
    assert result["stop_reason"] == "quota_reached"
    assert result["pos_after"][1] <= nearby.anchor[1]
    assert engine.collar_node_id == collar.node_id


def test_level_hazard_can_rejoin_a_dry_parent_with_pending_work() -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, -54.0, 2.5), surface_y=64,
                           inventory_items=[{"name": "diamond_pickaxe", "count": 1, "slot": 9}])
    route = [(0, -54, z) for z in range(3)]
    for x, y, z in route:
        world.blocks[(x, y, z)] = world.blocks[(x, y + 1, z)] = "air"
    world.blocks[(0, -55, 4)] = "lava"
    world.blocks[(2, -54, 0)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="DryParentAfterHazard", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(kind="collar", anchor=(0, 64, -118), parent_id=None,
                                     edge_from_parent=None, branches=[])
        engine.collar_node_id = collar.node_id
        engine.collar_ceiling_y = collar.anchor[1]
        junction = engine._create_node(
            kind="spine", anchor=route[0], parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "south", [collar.anchor, route[0]]),
            branches=[mining.BranchSpec("spine_forward", "east", 4)],
        )
        leaf = engine._create_node(
            kind="spine", anchor=route[-1], parent_id=junction.node_id,
            edge_from_parent=ShaftEdge("branch_leaf", "south", route),
            branches=[mining.BranchSpec("branch_leaf", "south", 4)],
        )
        engine.fluid_aborts = mining.FLUID_ABORT_LIMIT - 1
        result = engine.run(target="diamond", target_y=-54, goal_count=1, max_duration_s=32)

    assert leaf.branches[0].prune_reason == "fluid_or_bedrock"
    assert (0, -54, 3) in engine.known_corridor_positions
    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert result["pos_after"][1] == -54
    assert engine.collar_node_id == collar.node_id
    assert collar.anchor not in engine.exhausted_collars


@pytest.mark.parametrize("move", ["_move_exact", "_step_to"])
@pytest.mark.parametrize("unsafe_position", [{"on_ground": False}, {"in_lava": True, "on_ground": True}])
def test_movement_fallback_waits_for_a_dry_grounded_stance(move, unsafe_position, monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    commands = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path in {"move_to", "look", "control"}:
            commands.append(path)
        if path == "move_to":
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found"}})
        response = world.handler(request)
        if path == "status" and commands:
            body = response.json()
            body["result"].update(unsafe_position)
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="DryMovementFallback", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        assert getattr(engine, move)((1, 20, 0)) is False

    assert commands == ["move_to"]

