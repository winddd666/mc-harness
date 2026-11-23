from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("recovery", [
    "landed", "unreleased", "still_wet", "respawned", "missing_water_pos",
    "jump_lands", "jump_stays_airborne", "jump_reenters_water", "jump_respawned", "jump_threat",
])
def test_first_mining_submersion_names_the_water_site_only_after_safe_handoff(monkeypatch, recovery):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    wet = [309368.5, 61, 301962.5]
    bank = [309369.4, 64, 301962.5]
    status = {
        "pos": wet, "in_water": True, "submerged": True, "on_ground": False, "health": 20,
        "oxygen": 17, "dimension": "overworld", "last_death": {"t": 1}, "ready": True, "alive": True,
    }

    class CoastBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if recovery == "jump_lands" and clock[0] >= 1000.49:
                    status.update(pos=bank, on_ground=True)
                if clock[0] >= 1000.09:
                    if recovery == "jump_reenters_water":
                        status.update(in_water=True, submerged=True)
                    elif recovery == "jump_respawned":
                        status.update(pos=bank, on_ground=True, last_death={"t": 2})
                    elif recovery == "jump_threat":
                        status["nearest_hostile"] = {"name": "drowned", "id": 65, "dist": 3.0}
                return dict(status)
            if action == "events":
                return {"events": []}
            return {"ok": True, "items": []}

    def shore(*_args, **_kwargs):
        if recovery == "still_wet":
            return {"ok": False, "in_water": True}
        if recovery == "respawned":
            status["last_death"] = {"t": 2}
        jumping = recovery.startswith("jump_")
        pos = [bank[0], bank[1] + 0.4, bank[2]] if jumping else bank
        status.update(pos=pos, in_water=False, submerged=False, on_ground=not jumping)
        return {
            "ok": True, "in_water": False, "pos_after": bank,
            "actions": ["move_to_shore", "shore_lip_jump"] if jumping else ["move_to_shore"],
        }

    guard = GenerationGuard(CoastBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    water = {**status, "reason": "sustained_submersion", "submerged_for_s": 2.05}
    if recovery == "missing_water_pos":
        water.pop("pos")
    receipt = guard.handle_preemption({
        "source": "tool_water_threat", "observed_life": ("overworld", {"t": 1}), "water_observation": water,
        "action_released": recovery != "unreleased",
    }, interrupted_name="mc_skill_mine_blocks")

    if recovery in {"landed", "jump_lands"}:
        assert receipt["water_reentry_site"] == [309368, 61, 301962]
        assert "[309368, 61, 301962]" in receipt["message"]
        assert "沿干地换一个采矿入口" in receipt["message"]
    else:
        assert "water_reentry_site" not in receipt
    assert receipt.get("shore_landing_confirmed_after_jump", False) is (recovery == "jump_lands")
    if recovery == "jump_respawned":
        assert receipt["reflex_mode"] == "life_unavailable"
    if recovery.startswith("jump_"):
        assert 1000.09 <= clock[0] <= 1000.7
        if recovery in {"jump_threat", "jump_reenters_water", "jump_respawned"}:
            assert clock[0] < 1000.2


@pytest.mark.parametrize("site", [
    "water_at_feet", "water_at_head", "drained", "another_floor", "first_life",
    "new_life", "new_dimension", "missing_death_marker", "probe_timeout", "adjacent_water", "adjacent_dry",
])
def test_bug009_dfs_checks_a_confirmed_water_reentry_site_before_moving(monkeypatch, tmp_path, site):
    from mc_agent.skills.mining_dfs import MiningDFSEngine

    wet = (352084, -55, 357038)
    head = (wet[0], wet[1] + 1, wet[2])
    adjacent = (wet[0], wet[1] - 1, wet[2] - 1)
    wet_pos = [352084.6, -55, 357038.5]
    bank = [352085.5, -53, 357037.5]
    status = {
        "pos": wet_pos, "health": 20, "oxygen": 17, "ready": True, "alive": True, "on_ground": True,
        "in_water": True, "submerged": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    if site == "first_life":
        status.pop("last_death")
    dispatched = []
    blocks_read = []

    class MineBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                cell = tuple(body["pos"])
                blocks_read.append(cell)
                if cell == adjacent and site == "adjacent_water":
                    return {"name": "flowing_water"}
                if cell in {wet, head}:
                    if site == "probe_timeout":
                        raise httpx.ReadTimeout("water voxel probe timed out")
                    if site == "drained":
                        return {"name": "air"}
                    fluid = cell == (head if site == "water_at_head" else wet)
                    return {"name": "flowing_water" if fluid else "air"}
                return {"name": "stone"}
            if action in {"move_to", "control"}:
                dispatched.append((action, body))
                if action == "move_to":
                    status["pos"] = [body[axis] for axis in ("x", "y", "z")]
            return {"ok": True, "items": []}

        def status(self):
            return self.call("status", method="GET")

    def escape(*_args, **_kwargs):
        status.update(pos=bank, in_water=False, submerged=False)
        return {"ok": True, "in_water": False, "pos_after": bank}

    bot = MineBot()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", escape)
    receipt = guard.handle_preemption({
        "source": "tool_water_threat", "observed_life": ("overworld", status.get("last_death")), "action_released": True,
        "water_observation": {**status, "reason": "sustained_submersion", "submerged_for_s": 2.132},
    }, interrupted_name="mc_skill_mine_dfs")
    assert receipt["water_reentry_site"] == list(wet)

    if site == "new_life":
        status["last_death"] = {"t": 2}
    if site == "new_dimension":
        status["dimension"] = "the_nether"
    if site == "missing_death_marker":
        status.pop("last_death")
    if site == "water_at_head":
        status["pos"] = [352093.4, -53, 357037.5]
    target = (wet[0], wet[1] + 4, wet[2]) if site == "another_floor" else wet
    if site in {"adjacent_water", "adjacent_dry"}:
        target = adjacent
    alternate = (352086, -53, 357037)
    dfs = MiningDFSEngine(MacroSkills(bot), ledger_path=tmp_path / "ledger.json")

    def attempt():
        return json.dumps({"first": dfs._step_to(target), "alternate": dfs._step_to(alternate)})

    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", attempt))
    unconfirmed = site in {"probe_timeout", "missing_death_marker"}
    blocked = unconfirmed or site in {"water_at_feet", "water_at_head", "first_life", "adjacent_water"}
    assert result == {"first": not blocked, "alternate": True}
    expected_targets = [alternate] if blocked else [target, alternate]
    assert [(action, body.get("x"), body.get("y"), body.get("z")) for action, body in dispatched] == [
        ("move_to", x + 0.5, y, z + 0.5) for x, y, z in expected_targets
    ]
    denials = [event for event in events.events if event["type"] == "guardian.mining_landing_blocked"]
    if blocked:
        reason = "mining_footing_unavailable" if unconfirmed else "mining_water_reentry"
        assert denials and all(event["reason"] == reason and event["water_reentry_site"] == list(wet) for event in denials)
    else:
        assert denials == []
    probe_target = adjacent if site in {"adjacent_water", "adjacent_dry"} else wet
    assert (probe_target in blocks_read) is (site not in {"another_floor", "new_life", "new_dimension", "missing_death_marker"})


@pytest.mark.parametrize("scene", ["flowing_water", "dry", "new_life", "remote"])
def test_w19_dfs_preserves_a_verified_water_barrier_near_the_previous_pool(scene):
    wet = (219030, 32, 172181)
    target = [219030, 34, 172179] if scene != "remote" else [219030, 34, 172174]
    adjacent_water = [target[0], target[1], target[2] + 1]
    status = {
        "pos": [219031.7, 35, 172181.7], "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 2 if scene == "new_life" else 1},
    }
    digs = []
    reads = []
    events = []

    class MineBot:
        def call(self, endpoint, body=None, **_kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "get_block":
                cell = body["pos"]
                reads.append(cell)
                return {"name": "stone" if scene == "dry" or cell != adjacent_water else "flowing_water"}
            if endpoint == "dig":
                digs.append(body["pos"])
                return {"ok": True, "dug": "stone", "gained": {"cobblestone": 1}}
            return {"ok": True}

    bot = MineBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None)
    guard._mining_water_reentry_site = (wet, ("overworld", {"t": 1}))
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", lambda: json.dumps(
        bot.call("dig", {"pos": target, "collect": True}),
    )))
    if scene == "flowing_water":
        assert digs == [] and result["error"]["code"] == "mining_water_reentry"
        denial, = [event for event in events if event["type"] == "guardian.mining_water_dig_blocked"]
        assert denial["target"] == target and denial["water_reentry_site"] == list(wet)
    else:
        assert digs == [target] and result["gained"] == {"cobblestone": 1}
    assert (adjacent_water in reads) is (scene in {"flowing_water", "dry"})


@pytest.mark.parametrize("oxygen, reason", [(20, None), (400, None), (20, "drowned"), (400, "hurt")])
def test_bug009_mining_keeps_wading_until_there_is_a_water_threat(monkeypatch, oxygen, reason):
    import threading

    finished = threading.Event()
    samples = []
    calls = []
    status = {
        "pos": [41012.5, 63, 11034.4], "health": 20, "oxygen": oxygen,
        "ready": True, "alive": True, "on_ground": True, "in_water": True, "submerged": False,
        "dimension": "overworld",
    }
    drowned = {"id": 45, "name": "drowned", "dist": 4.0, "pos": [41016.5, 63, 11034.4]}

    class WadingMiner:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "dig":
                calls.append(endpoint)
                assert finished.wait(3.0)
                return {"ok": True, "broken": 1}
            if endpoint == "stop":
                calls.append(endpoint)
                finished.set()
            if endpoint == "status":
                if not finished.is_set():
                    samples.append(dict(status))
                    if len(samples) == 2:
                        if reason == "drowned":
                            status["nearest_hostile"] = drowned
                        elif reason == "hurt":
                            status["health"] = 19.33
                        else:
                            finished.set()
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            if endpoint == "inventory":
                return {"items": []}
            return {"ok": True}

    bot = WadingMiner()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)

    def shore(*args, **kwargs):
        calls.append("surface")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    result = json.loads(guard.run_tool_with_reflex(
        "mc_skill_mine_blocks", lambda: json.dumps(bot.call("dig", {"pos": [41012, 63, 11035], "stop_on": ["death"]})),
    ))
    assert len(samples) == 2
    if reason is None:
        assert calls == ["dig"]
        assert result == {"ok": True, "broken": 1}
        assert not events.events
    else:
        assert calls == ["dig", "stop", "surface"]
        assert result["reflex_mode"] == "water_surface"
        event = next(event for event in events.events if event["type"] == "guardian.preempted_tool")
        observed = event["water_observation"]
        assert observed["reason"] == reason and observed["oxygen"] == oxygen
        assert observed["in_water"] is True and observed["submerged"] is False and observed["on_ground"] is True
        assert observed["health"] == (19.33 if reason == "hurt" else 20)
        assert observed["ready"] is True and observed["alive"] is True and observed["dimension"] == "overworld"


@pytest.mark.parametrize("retry, expected_bank", [
    ("same_life", "known"),
    ("bank_flooded", "alternate"),
    ("new_life", "alternate"),
    ("after_window", "alternate"),
])
def test_bug098_repeated_mining_water_preemption_prefers_verified_dry_tunnel(monkeypatch, retry, expected_bank):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    wet = [10.5, 22, 10.5]
    known_bank = [9.5, 22, 10.5]
    alternate_bank = [12.5, 22, 10.5]
    status = {
        "pos": wet, "health": 20, "oxygen": 110, "dimension": "overworld", "last_death": {"t": 1},
        "ready": True, "alive": True, "in_water": True, "submerged": True, "on_ground": False,
    }
    moves = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                y = body["pos"][1]
                return {"name": "stone" if y == 21 else "water" if y == 22 and retry == "bank_flooded" else "air"}
            if action == "move_to":
                goal = [body[key] for key in ("x", "y", "z")]
                moves.append(goal)
                status.update(pos=goal, in_water=False, submerged=False, on_ground=True)
            if action == "events":
                return {"events": []}
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "_find_walkable_shore_target", lambda *_a, **_kw: alternate_bank)
    trigger = {"source": "tool_water_threat", "primary_type": "water"}
    first = guard.handle_preemption(trigger, interrupted_name="mc_skill_mine_dfs")
    assert first["reflex_mode"] == "water_surface" and "矿道重复进水" not in first["message"]

    clock[0] += 61.0 if retry == "after_window" else 5.0
    if retry == "new_life":
        status["last_death"] = {"t": 2}
    guard.combat_env.remember_dry_bank({**status, "pos": known_bank})
    status.update(pos=wet, in_water=True, submerged=True, on_ground=False)
    second = guard.handle_preemption(trigger, interrupted_name="mc_skill_unstick")

    assert second["reflex_mode"] == "water_surface" and second["status_after"]["in_water"] is False
    assert moves == [alternate_bank, known_bank if expected_bank == "known" else alternate_bank]
    assert ("避开水格 (10, 22, 10)" in second["message"]) is (retry in {"same_life", "bank_flooded"})


@pytest.mark.parametrize("retry", ["same_shore", "only_bank", "new_life", "after_window", "different_shore"])
def test_surface_mining_water_reentry_uses_a_different_verified_bank(monkeypatch, retry):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    wet = [47984.6, 62.6, 54980.6]
    banks = [(47983.5, 63.0, 54980.5), (47986.5, 63.0, 54981.5), (47981.5, 63.0, 54979.5)]
    available = banks[:1] if retry == "only_bank" else banks
    supports = {(math.floor(x), math.floor(y) - 1, math.floor(z)) for x, y, z in available}
    status = {"pos": wet, "health": 20, "oxygen": 300, "dimension": "overworld", "last_death": {"t": 1},
              "ready": True, "alive": True, "in_water": True, "submerged": False, "on_ground": False}
    moves = []

    class CoastBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                return {"name": "stone" if tuple(body["pos"]) in supports else "air"}
            if action == "move_to":
                goal = tuple(body[axis] for axis in ("x", "y", "z"))
                assert goal in available
                moves.append(goal)
                status.update(pos=list(goal), in_water=False, on_ground=True)
            return {"ok": True}

    def find_bank(_pos, *, excluded_shores, **_kwargs):
        return next((list(bank) for bank in available if bank not in excluded_shores), None)

    guard = GenerationGuard(CoastBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "_find_walkable_shore_target", find_bank)
    trigger = {"source": "tool_water_threat", "primary_type": "water"}
    attempts = 3 if retry == "same_shore" else 2
    for i in range(attempts):
        if i and retry == "new_life":
            status["last_death"] = {"t": 2}
        water_pos = [wet[0] + 8.0, *wet[1:]] if i and retry == "different_shore" else wet
        status.update(pos=water_pos, in_water=True, on_ground=False)
        receipt = guard.handle_preemption(trigger, interrupted_name="mc_skill_mine_blocks")
        assert receipt["status_after"]["on_ground"] is True
        assert ("海岸重复进水" in receipt["message"]) is bool(i and retry in {"same_shore", "only_bank"})
        clock[0] += 61.0 if retry == "after_window" else 5.0

    assert moves == (banks if retry == "same_shore" else [banks[0], banks[0]])


@pytest.mark.parametrize("landing, expected_error", [
    ("shore", None),
    ("settled", None),
    ("respawned", "respawned"),
    ("unready", "shore_landing_unconfirmed"),
    ("airborne", "shore_landing_unconfirmed"),
    ("reentered", "water_escape_failed"),
    ("wet", "water_escape_failed"),
    ("status_timeout", "shore_landing_unconfirmed"),
])
def test_unstick_water_success_requires_confirmed_same_life_landing(landing, expected_error):
    status = {
        "pos": [-7420.7, 61.5, 2631.7], "health": 12, "oxygen": 300, "last_death": {"t": 1},
        "dimension": "overworld", "ready": True, "alive": True, "on_ground": False, "in_water": True,
    }
    finished = False

    class SwimBot:
        def call(self, action, body=None, **kwargs):
            assert action == "status"
            if finished and landing == "status_timeout":
                raise httpx.ReadTimeout("landing status unavailable")
            if finished and kwargs.get("timeout_s") == 0.25:
                if landing == "settled":
                    status.update(pos=[-7422.5, 63.0, 2630.5], on_ground=True)
                elif landing == "reentered":
                    status.update(pos=[-7422.5, 62.4, 2630.5], in_water=True)
            return dict(status)

    bot = SwimBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def unstick():
        nonlocal finished
        start = bot.call("status", method="GET")
        assert start["in_water"]
        status.update(pos=[-7422.5, 64, 2630.5], in_water=False, on_ground=True)
        if landing == "respawned":
            status.update(pos=[-7421.5, 64.1, 2805.5], ready=False, last_death={"t": 2})
        elif landing == "unready":
            status["ready"] = False
        elif landing in {"airborne", "settled", "reentered"}:
            status["on_ground"] = False
        elif landing == "wet":
            status["in_water"] = True
        finished = True
        return json.dumps({"ok": True, "escaped_water": True, "in_water": False, "pos_after": status["pos"]})

    result = json.loads(guard.run_tool_with_reflex("mc_skill_unstick", unstick))

    assert result["ok"] is (expected_error is None)
    assert result["escaped_water"] is result["ok"]
    if landing == "settled":
        assert result["pos_after"] == [-7422.5, 63.0, 2630.5]
    if expected_error:
        assert result["requires_replan"] is True and result["error"]["code"] == expected_error
        assert result.get("respawned", False) is (landing == "respawned")


@pytest.mark.parametrize(("tool_name", "distance", "wet", "should_preempt"), [
    ("mc_skill_scout_surface", 4.0, True, True),
    ("mc_skill_scout_surface", 8.0, True, False),
    ("mc_move_to", 9.0, True, True),
    ("mc_move_to", 11.0, True, False),
    ("mc_move_to", 9.0, False, False),
    ("pre_ensure", 9.5, True, True),
    ("pre_ensure", 11.1, True, False),
    ("pre_ensure", 9.5, False, False),
])
def test_movement_hands_off_water_near_drowned_before_damage(monkeypatch, tool_name, distance, wet, should_preempt):
    import threading

    moving = threading.Event()
    observed = threading.Event()
    stopped = threading.Event()
    status = {
        "pos": [-11052.6, 64, 11027], "health": 20, "alive": True, "ready": True, "on_ground": True,
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": distance, "pos": [-11048.6, 62, 11027]},
    }
    calls = []

    class MovingBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if moving.is_set():
                    observed.set()
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "move_to":
                calls.append(action)
                status.update(in_water=wet, on_ground=not wet)
                moving.set()
                if should_preempt:
                    assert stopped.wait(2.0), "movement continued near a drowned after entering water"
                else:
                    assert observed.wait(2.0)
            if action == "stop":
                calls.append(action)
                stopped.set()
                return {"ok": True, "released": True}
            return {"ok": True}

    bot = MovingBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda *_a, **_kw: pytest.fail("direct shore recovery expected"))

    def reach_shore(pos, *, keep_shield, shield_target_id):
        assert shield_target_id == 44 and pos == [-11048.6, 62, 11027]
        calls.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    def move():
        return json.dumps(bot.call("move_to", {"x": -11042, "z": 11027, "stop_on": ["death"]}))

    if tool_name == "pre_ensure":
        try:
            with guard._intercept_bot_calls(enable_creeper_watcher=True):
                result = json.loads(move())
        except ReflexPreempted as preempt:
            result = guard.handle_preemption(preempt.trigger, interrupted_name=tool_name)
    else:
        result = json.loads(guard.run_tool_with_reflex(tool_name, move))
    if should_preempt:
        assert calls == ["move_to", "stop", "shore"]
        assert result["reflex_mode"] == "water_surface" and not result["status_after"]["in_water"]
        assert result["status_after"]["health"] == 20
        assert result["water_risk"] == "drowned" and result["action_released"] is True
        assert guard._water_hurt_streak == 0
    else:
        assert calls == ["move_to"] and result["ok"] is True


@pytest.mark.parametrize("scene", ["still_submerged", "busy_after_move", "low_oxygen_in_flight", "head_above", "dry", "surface"])
def test_w19_underground_move_hands_off_drowning_before_a_followup_call(monkeypatch, scene):
    import threading
    from mc_agent.harness.mc_tools import McToolset

    origin = [219032.4, 35.3, 172181.6]
    landing = [219029.5, 34.2 if scene != "surface" else 63.2, 172182.5]
    bank = [219031.7, 35, 172181.7]
    status = {
        "pos": origin, "health": 20, "oxygen": 20, "ready": True, "alive": True,
        "on_ground": True, "in_water": False, "submerged": False, "dimension": "overworld",
    }
    blocked = scene in {"still_submerged", "busy_after_move", "low_oxygen_in_flight"}
    needs_stop = scene in {"busy_after_move", "low_oxygen_in_flight"}
    stopped = threading.Event()
    actions = []

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "status":
            result = dict(status)
        elif endpoint == "move_to":
            actions.append(endpoint)
            status.update(
                pos=landing, in_water=scene != "dry", submerged=scene not in {"dry", "head_above"},
                on_ground=scene == "dry", oxygen=7 if scene == "low_oxygen_in_flight" else 17,
            )
            if scene == "low_oxygen_in_flight":
                assert stopped.wait(2.0), "underground movement was not stopped before damage"
            if scene == "busy_after_move":
                status["busy"] = "move_to"
            result = {"pos": landing}
        elif endpoint == "stop":
            actions.append(endpoint)
            status.pop("busy", None)
            stopped.set()
            result = {"released": True}
        elif endpoint == "inventory":
            result = {"items": []}
        elif endpoint == "events":
            result = {"events": []}
        else:
            pytest.fail(f"Unexpected movement endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": result})

    def reach_shore(_threat, *, surface_first, **_kwargs):
        assert surface_first and status["health"] == 20
        actions.append("shore")
        status.update(pos=bank, in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False, "pos_after": bank}

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
        tools = McToolset(bot)
        result = json.loads(guard.run_tool_with_reflex(
            "mc_move_to", lambda: tools.mc_move_to(x=219029.6, z=172183.1, range=1),
        ))

    if blocked:
        assert result["stop_reason"] == "tool_water_threat" and result["reflex_mode"] == "water_surface"
        assert result["water_risk"] == ("low_oxygen" if scene == "low_oxygen_in_flight" else "underground_move_submerged")
        assert result["pos_at_preemption"] == landing and result["pos_after"] == bank
        assert actions == ["move_to", *(["stop"] if needs_stop else []), "shore"]
        assert result.get("action_released") is (True if needs_stop else None)
    else:
        assert result == {"ok": True, "result": {"pos": landing}}
        assert actions == ["move_to"]


@pytest.mark.parametrize("scene", [
    "known", "unknown", "exposed", "close_creeper", "ambiguous", "changed_life", "still_airborne",
    "target_missing", "creeper_during_check", "player_attributed", "grounded_origin", "step_to_cover", "unsupported",
])
def test_critical_dry_trident_hit_checks_cover_after_zero_movement_retreat(monkeypatch, scene):
    from mc_agent.skills.combat_env import CombatEnv

    landed = [328069.3, 73, 333225.0]
    drowned = {"id": 902140, "name": "drowned", "pos": [328075.1, 73, 333225.5], "dist": 5.8}
    creeper = {"id": 42, "name": "creeper", "pos": [328068.5, 73, 333220.5], "dist": 4.6}
    known_target = scene in {"known", "target_missing"}
    origin = [328068.3, 73, 333225.0] if scene == "grounded_origin" else [328069.3, 73.6, 333225.0]
    status = {
        "pos": origin, "dimension": "overworld", "health": 1.833, "food": 17,
        "ready": True, "alive": True, "on_ground": scene == "grounded_origin", "in_water": False, "blocking": False,
        "last_death": {"t": 1}, "nearest_hostile": drowned if known_target else None,
    }
    hit = {"seq": 1, "type": "hurt", "source": "trident"}
    if known_target:
        hit["source_entity"] = drowned
    if scene == "player_attributed":
        hit["source_entity"] = {**drowned, "id": 99, "name": "player"}
    scans = []
    attempted = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                scans.append(status["pos"])
                entities = [{**drowned, "id": 91}] if scene == "target_missing" else [drowned]
                if scene == "ambiguous":
                    entities.append({**drowned, "id": 93, "pos": [328077.1, 73, 333225.5], "dist": 7.9})
                if scene == "close_creeper" or (scene == "creeper_during_check" and len(scans) > 1):
                    entities.append(creeper)
                return {"entities": entities}
            if action == "get_block":
                x, y, z = body["pos"]
                support = y == 72 and x == 328069 and z in (333224, 333225) and scene != "unsupported"
                wall = x == 328072 and 73 <= y <= 76 and scene != "exposed"
                if scene == "step_to_cover":
                    support = y == 72 and x in (328068, 328069) and 333221 <= z <= 333226
                    wall = wall and z <= 333224
                return {"name": "stone" if support or wall else "air"}
            if action == "move_to" and scene == "step_to_cover":
                attempted.append("move")
                status["pos"] = [body["x"], body["y"], body["z"]]
                return {"ok": True}
            pytest.fail(f"Unexpected action during landing cover check: {action}")

    def dig_cover(*_args, **_kwargs):
        attempted.append("dig")
        return {"ok": False, "reason": "no_filler_block"}

    def flee(*_args, **_kwargs):
        attempted.append("flee")
        if scene != "still_airborne":
            status.update(pos=landed, on_ground=True, nearest_hostile=None)
        if scene == "changed_life":
            status["last_death"] = {"t": 2}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0, "pos_after": status["pos"]}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", dig_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    actions = guard.poll()["actions"]
    covers = [action for action in actions if action["action"] == "natural_cover_after_retreat"]

    if scene in {"changed_life", "still_airborne"}:
        assert attempted == ["flee"]
        assert not covers and not scans
    elif scene == "player_attributed":
        assert attempted == ["flee", "dig"]
        assert not covers and not scans
    elif scene == "step_to_cover":
        assert attempted == ["flee", "dig", "move"]
        assert len(covers) == 1 and covers[0]["ok"]
        assert covers[0]["result"]["pos_after"] == status["pos"] != landed
        assert scans == [landed, status["pos"]]
    else:
        assert attempted == (["dig", "flee"] if scene == "grounded_origin" else ["flee", "dig"])
        assert len(covers) == 1 and covers[0]["ok"] is (scene in {"known", "unknown", "grounded_origin"})
        if covers[0]["ok"]:
            assert covers[0]["result"]["pos_after"] == landed
        after_retreat = [landed, landed] if covers[0]["ok"] or scene == "creeper_during_check" else [landed]
        assert scans == ([origin] + after_retreat if scene == "grounded_origin" else after_retreat)


@pytest.mark.parametrize("followup", ["stalled", "progress", "new_life"])
def test_water_escape_reselects_a_shore_after_a_stalled_path(followup, monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv

    blocked_bank = [0.5, 64, -4.5]
    alternative_bank = [3.5, 63, 0.5]
    status = {
        "pos": [0.5, 62.8, 0.5], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    attempts = []

    class CreekBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "move_to":
                bank = [body[key] for key in ("x", "y", "z")]
                attempts.append(bank)
                if len(attempts) == 1:
                    raise ApiError("path_not_found", "bank is unreachable")
                status.update(pos=bank, in_water=False, on_ground=True)
            if action == "control" and body.get("forward") and len(attempts) == 1:
                if followup == "progress":
                    status["pos"] = [0.5, 63.2, -2.0]
                else:
                    raise ApiError("blocked", "cannot clear the lip")
            return {"ok": True}

    env = CombatEnv(CreekBot())

    def choose_shore(_pos, *, excluded_shores, **_kwargs):
        return alternative_bank if tuple(blocked_bank) in excluded_shores else blocked_bank

    monkeypatch.setattr(env, "_find_walkable_shore_target", choose_shore)
    monkeypatch.setattr(env, "_dry_bank_is_walkable", lambda _pos: True)
    assert env.escape_deep_water_to_shore()["in_water"] is True
    if followup == "new_life":
        status["last_death"] = {"t": 2}
    assert env.escape_deep_water_to_shore()["ok"] is True

    assert attempts == [blocked_bank, alternative_bank if followup == "stalled" else blocked_bank]

