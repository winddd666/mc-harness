from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("retry", ["same_footing", "different_footprint", "different_layer", "repaired", "expired", "wet"])
def test_dfs_reuses_an_unsafe_landing_only_for_the_observed_support_blocks(monkeypatch, retry):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    missing = [431712, 43 if retry == "wet" else 42, 384306]
    x, z = 431712.9, 384306.9
    target = [x, 43.96 if retry == "wet" else 43.1, z]
    probes = []
    moves = []
    repaired = False

    class MineBot:
        def call(self, action, body=None, **kwargs):
            nonlocal repaired
            if action == "status":
                return {"ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1}}
            if action == "get_block":
                pos = body["pos"]
                probes.append(pos)
                if retry == "wet" and pos == [431712, 44, 384306]:
                    return {"name": "water" if clock[0] > 100.0 else "air"}
                return {"name": "air" if pos == missing and not repaired else "stone"}
            if action == "place":
                assert body["pos"] == missing
                repaired = True
            if action == "move_to":
                moves.append([body[axis] for axis in ("x", "y", "z")])
            return {"ok": True}

    bot = MineBot()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)
    if retry == "wet":
        guard._mining_water_reentry_site = ((431712, 44, 384306), ("overworld", {"t": 1}))

    def move(pos):
        return bot.call("move_to", dict(zip(("x", "y", "z"), pos), range=0.35))

    def invoke():
        nonlocal repaired
        with pytest.raises(ApiError) as denied:
            move(target)
        assert denied.value.code == "unsafe_landing"
        clock[0] += 0.5
        if retry == "same_footing":
            with pytest.raises(ApiError) as denied_same:
                move([x, 43.2, z])
            assert denied_same.value.context["target"] == [x, 43.2, z]
            clock[0] += 0.5
            return json.dumps(move([x, 43.3, z]))
        if retry == "repaired":
            bot.call("place", {"pos": missing, "item": "cobblestone"})
        elif retry == "expired":
            clock[0] += 1.51
            repaired = True
        destination = {
            "different_footprint": [431713.5, 43.2, z],
            "different_layer": [x, 44.1, z],
            "wet": [x, 44.0, z],
        }.get(retry, [x, 43.3, z])
        return json.dumps(move(destination))

    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", invoke))
    blocked = [event for event in events.events if event["type"] == "guardian.mining_landing_blocked"]
    if retry == "same_footing":
        assert result["error"]["target"] == [x, 43.3, z] and not moves
        assert [event.get("probe_reused", False) for event in blocked] == [False, True, True]
        assert probes == [missing]
    elif retry == "wet":
        assert result["error"]["code"] == "mining_water_reentry" and not moves
        wet = [431712, 44, 384306]
        assert probes == [missing, wet, missing, wet]
        assert [event["reason"] for event in blocked] == ["unsafe_landing", "mining_water_reentry"]
        assert not any(event.get("probe_reused") for event in blocked)
    else:
        assert result["ok"] and len(moves) == 1 and len(probes) > 1


@pytest.mark.parametrize("terrain", ["cliff", "lava", "flat", "wall", "read_timeout", "repaired", "falling"])
def test_dfs_checks_collar_relocation_before_dispatch_and_before_retrying_exactly(terrain):
    origin = [354983.4, 50, 367623.5]
    target = {"x": 354983.5, "z": 367621.5, "range": 1.2, "stop_on": ["hurt", "death"]}
    status = {"pos": origin, "health": 20, "ready": True, "alive": True, "on_ground": terrain != "falling",
              "dimension": "overworld", "in_water": False, "in_lava": False, "velocity": [0, -16, 0]}
    dispatched = []
    repaired = False

    class CollarBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                x, y, z = body["pos"]
                ahead = x == 354983 and z <= 367622
                if ahead and terrain == "wall" and y == 51:
                    return {"name": "stone"}
                if ahead and y == 49 and terrain == "read_timeout":
                    raise httpx.ReadTimeout("collar floor read timed out")
                if ahead and y == 49 and terrain == "lava":
                    return {"name": "lava"}
                if ahead and 45 <= y <= 49 and terrain in {"cliff", "repaired"} and not repaired:
                    return {"name": "air"}
                return {"name": "stone" if y in {49, 18} else "air"}
            if action in {"move_to", "control"}:
                dispatched.append((action, body))
            return {"ok": True}

    bot = CollarBot()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)

    def attempt():
        nonlocal repaired

        def dispatch(action, body):
            try:
                bot.call(action, body)
            except ApiError as err:
                return err.code
            return None

        loose = dispatch("move_to", target)
        if terrain == "repaired":
            repaired = True
        exact = dispatch("move_to", {**target, "y": 19, "range": 0.35})
        fallback = dispatch("control", {"forward": True, "ms": 420})
        status["on_ground"] = True
        alternate = dispatch("move_to", {"x": 354984.5, "y": 50, "z": 367623.5, "range": 0.3})
        return json.dumps({"loose": loose, "exact": exact, "fallback": fallback, "alternate": alternate})

    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", attempt))
    denied = {
        "cliff": "unsafe_landing", "lava": "unsafe_landing", "repaired": "unsafe_landing",
        "read_timeout": "mining_footing_unavailable", "falling": "unsafe_landing",
    }.get(terrain)
    paired_denial = None if terrain == "repaired" else denied
    assert result == {"loose": denied, "exact": paired_denial, "fallback": paired_denial, "alternate": None}
    assert [action for action, _ in dispatched] == (
        ["move_to"] if paired_denial else ["move_to", "control", "move_to"] if denied
        else ["move_to", "move_to", "control", "move_to"]
    )
    if denied:
        event, = [e for e in events.events if e["type"] == "guardian.mining_collar_blocked"]
        assert event["reason"] == denied and event["pos"] == origin


@pytest.mark.parametrize("floor", ["air", "lava", "unavailable"])
def test_bug116_dfs_rejects_dangerous_waypoint_and_keeps_mining(monkeypatch, tmp_path, floor):
    from mc_agent.skills.mining_dfs import MiningDFSEngine

    origin = (475204, 53, 487553)
    rejected = (475204, 53, 487554)
    alternate = (475205, 53, 487553)
    ore = [475206, 53, 487553]
    status = {"pos": [origin[0] + 0.5, origin[1], origin[2] + 0.5], "health": 20,
              "on_ground": True, "ready": True, "alive": True, "in_water": False, "in_lava": False}
    moves = []
    digs = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                if body["pos"] == [rejected[0], rejected[1] - 1, rejected[2]]:
                    if floor == "unavailable":
                        raise httpx.ReadTimeout("short footing read expired")
                    return {"name": floor}
                return {"name": "stone"}
            if action in {"move_to", "control"}:
                moves.append((action, body))
                if action == "move_to":
                    status["pos"] = [body[axis] for axis in ("x", "y", "z")]
            if action == "dig":
                digs.append(body["pos"])
                return {"dug": "iron_ore", "gained": {"raw_iron": 1}}
            return {"ok": True}

        def status(self):
            return self.call("status", method="GET")

    bot = ShaftBot()
    dfs = MiningDFSEngine(MacroSkills(bot), ledger_path=tmp_path / "ledger.json")
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)

    def explore_with_alternate(**kwargs):
        assert not dfs._step_to(rejected, from_cell=origin)
        assert dfs._step_to(alternate, from_cell=origin)
        dig = dfs.bot.call("dig", {"pos": ore, "collect": True})
        return {"ok": True, "target_gained": dig["gained"]["raw_iron"], **dig}

    monkeypatch.setattr(dfs, "_run", explore_with_alternate)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", lambda: json.dumps(dfs.run(goal_count=1))))
    assert result["goal_met"] and result["gained"] == {"raw_iron": 1} and digs == [ore]
    assert [(action, body.get("x"), body.get("z")) for action, body in moves] == [
        ("move_to", alternate[0] + 0.5, alternate[2] + 0.5),
    ]
    rejected_events = [event for event in events.events if event["type"] == "guardian.mining_landing_blocked"]
    reason = "mining_footing_unavailable" if floor == "unavailable" else "unsafe_landing"
    assert rejected_events and {event["reason"] for event in rejected_events} == {reason}


def test_bug116_dfs_reroutes_when_guard_footing_reads_time_out(monkeypatch, tmp_path):
    from mc_agent.skills.mining_dfs import MiningDFSEngine
    from tests.mining._common import _MockMineWorld

    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80, dimension="overworld")
    world.blocks.update({(0, 74, -4): "lava", (0, 75, -4): "air", (0, 76, -4): "air"})
    for x in (-3, -4, -5, 3, 4, 5):
        world.blocks[(x, 74, -3)] = "iron_ore"

    rejected_landing = [0.5, 79, -0.5]
    checking_rejected_landing = False
    read_timeouts = []
    dispatched_moves = []

    def handler(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint in {"get_block", "move_to"}:
            body = json.loads(request.content)
            if endpoint == "get_block" and checking_rejected_landing and body["pos"] == [0, 78, -1]:
                read_timeouts.append(body["pos"])
                raise httpx.ReadTimeout("support probe timed out", request=request)
            if endpoint == "move_to":
                dispatched_moves.append([body[axis] for axis in ("x", "y", "z")])
        return world.handler(request)

    events = EventSink(lambda _: None)
    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(handler)) as bot:
        guard = GenerationGuard(bot, events, checkpoint=lambda: None)
        verify = guard._verify_mining_landing

        def verify_with_fault_scope(target):
            nonlocal checking_rejected_landing
            checking_rejected_landing = target == rejected_landing
            try:
                return verify(target)
            finally:
                checking_rejected_landing = False

        monkeypatch.setattr(guard, "_verify_mining_landing", verify_with_fault_scope)
        dfs = MiningDFSEngine(MacroSkills(bot), ledger_path=tmp_path / "ledger.json")
        result = json.loads(guard.run_tool_with_reflex(
            "mc_skill_mine_dfs",
            lambda: json.dumps(dfs.run(
                target="iron", goal_count=3, target_y=74, max_duration_s=20, strategy="fishbone_scan",
            )),
        ))

    rejected = [event for event in events.events if event["type"] == "guardian.mining_landing_blocked"]
    assert len(read_timeouts) >= 2 and rejected
    assert all(
        event["target"] == rejected_landing and event["reason"] == "mining_footing_unavailable"
        and not event.get("probe_reused")
        for event in rejected
    )
    assert dispatched_moves and rejected_landing not in dispatched_moves
    assert result["ok"] and result["goal_met"] and result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == world._count("raw_iron") == 3

