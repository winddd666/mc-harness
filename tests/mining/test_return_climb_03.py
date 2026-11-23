from tests.mining._common import *  # noqa: F401,F403
from tests.mining._common import _MockMineWorld, _fake_shaft_engine, _MiningClock

def test_climb_remembers_all_blocked_primitives_at_the_same_cell(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    attempts: list[tuple[str, str]] = []
    with BotSession("tok", bot_username="BlockedClimb", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64
        monkeypatch.setattr(eng.skills, "dig_staircase",
                            lambda **kw: (attempts.append(("stair", kw["direction"])) or {"ok": False}))
        monkeypatch.setattr(eng.skills, "dig_level_tunnel",
                            lambda **kw: (attempts.append(("tunnel", kw["direction"])) or {"ok": False}))
        first = eng.run(action="climb_out", max_duration_s=5.0)
        count = len(attempts)
        second = eng.run(action="climb_out", max_duration_s=5.0)
    assert first["stop_reason"] == second["stop_reason"] == "climb_blocked"
    assert count == 9  # four stairs, one pillar, four lateral exits
    assert len(attempts) == count
    assert "可继续 climb_out" not in second["message"]


def test_slow_blocked_climb_tries_a_different_direction_next_slice(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    directions: list[str] = []

    def blocked_stair(**kwargs):
        directions.append(kwargs["direction"])
        clock.now += 4.4
        return {"ok": False, "steps_done": 0}

    with BotSession("tok", bot_username="SlowBlockedClimb", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64
        monkeypatch.setattr(eng.skills, "dig_staircase", blocked_stair)
        first = eng.run(action="climb_out", max_duration_s=5.0)
        second = eng.run(action="climb_out", max_duration_s=5.0)
    assert first["stop_reason"] == second["stop_reason"] == "time_slice"
    assert directions == ["north", "south"]
    assert clock.now < 1010.0


def test_escape_primitive_passes_shared_deadline_to_new_skill_contract(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    received: list[float] = []
    with BotSession("tok", bot_username="DeadlineContract", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs

        def stairs(*, deadline, **kwargs):
            received.append(deadline)
            assert kwargs["steps"] == 1
            return {"ok": False}

        monkeypatch.setattr(eng.skills, "dig_staircase", stairs)
        eng._escape_primitive("dig_staircase", deadline=1234.0, steps=1, direction="north", vertical="up")
    assert received == [1234.0]


@pytest.mark.parametrize("target_y", [60, 16])
def test_explore_only_climbs_toward_a_higher_target(monkeypatch, target_y) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="AutoClimb", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64

        def climb(*, deadline):
            world.pos[1] = 64.0
            return {"reached": True}

        monkeypatch.setattr(eng, "_climb_out", climb)
        result = eng.run(target="iron", target_y=target_y, max_duration_s=10.0)
    if target_y == 60:
        assert result["stop_reason"] != "needs_surface_climb"
        assert result["tree"]["collar_node_id"] is not None
        assert world.dig_log
    else:
        assert result["stop_reason"] == "needs_surface_climb"
        assert result["next_action"] == "climb_out"
        assert result["pos_after"] == [0.5, 20.0, 0.5]
        assert world.pos[1] == 20
        assert not world.dig_log

