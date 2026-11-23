from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("shore", [
    "clear", "unsupported", "airborne", "still_wet", "new_life", "different_target", "occluded", "not_near",
])
def test_w20_hy8_water_melee_checks_the_pursuer_immediately_after_landing(monkeypatch, shore):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    bank = [240997.9, 63, 193570.9]
    target = {"id": 2411332, "name": "drowned", "pos": [240995.7, 62, 193574.4], "dist": 2.7}
    distance = 4.2 if shore == "not_near" else 2.5
    pursuer = {**target, "pos": [bank[0] - distance, *bank[1:]], "dist": distance,
               "blocked_by_wall": shore == "occluded"}
    if shore == "different_target":
        pursuer["id"] = 2411333
    status = {
        "pos": [240995.7, 61.6, 193571.7], "health": 9.1667, "food": 17, "oxygen": 20,
        "dimension": "overworld", "last_death": {"t": 1}, "in_water": True, "submerged": False,
        "on_ground": False, "alive": True, "ready": True, "nearest_hostile": target,
    }
    hits = [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": target, "health": 9.1667}]
    pulses = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "oak_log", "count": 1}], "offhand": None}
            if action == "get_entities":
                return {"entities": [pursuer]}
            if action == "get_block":
                support = body["pos"][1] == 62 and shore != "unsupported"
                return {"name": "stone" if support else "air"}
            if action == "look" and "yaw" in body:
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx > 0.9 and abs(dz) < 0.1
                pulses.append(body)
                status.update(pos=[240999.9, 63, 193570.9], nearest_hostile={"id": target["id"], "name": "drowned", "dist": 4.5})
            if action in {"move_to", "attack", "use_item"}:
                pytest.fail(f"Unexpected {action} while checking the shoreline pursuer")
            return {"ok": True}

    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def reach_shore(threat_pos, **options):
        assert threat_pos == target["pos"] and options["under_attack"] is True
        status.update(pos=bank, health=6.166666, on_ground=shore != "airborne", in_water=shore == "still_wet",
                      nearest_hostile=pursuer)
        if shore == "new_life":
            status.update(last_death={"t": 2}, pos=[240965.5, 65.1, 193590.5])
        hits.append({"seq": 2, "type": "hurt", "source": "mob_attack", "source_entity": target, "health": 6.166666})
        return {"ok": True, "mode": "move_to_shore", "in_water": False, "pos_after": bank}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    outcome = guard.poll()

    landed, *followups = outcome["actions"]
    assert landed["action"] == "escape_to_shore" and landed["ok"]
    assert outcome["requires_replan"] is True
    assert bool(pulses) is (shore == "clear")
    if shore in {"clear", "unsupported", "occluded"}:
        followup, = followups
        assert followup["action"] == "followup_melee_retreat" and followup["ok"] is (shore == "clear")
        if shore == "clear":
            assert followup["result"]["target_distance_after"] == 4.5
    else:
        assert not followups


@pytest.mark.parametrize(("entry", "pressure"), [
    ("poll", "low_hp"), ("poll", "fresh_health"), ("poll", "burst"),
    ("boundary", "low_hp"), ("boundary", "burst"), ("tool", "low_hp"),
    ("poll", "wading_burst"), ("boundary", "wading_burst"), ("tool", "wading_low_hp"),
])
def test_water_melee_pressure_prefers_shore_without_a_live_attacker_snapshot(monkeypatch, entry, pressure):
    health = 8.166667 if pressure.endswith("low_hp") else 14.0
    status = {
        "pos": [19023.7, 62.4, 41009.2], "health": health, "food": 20, "oxygen": 200,
        "in_water": True, "on_ground": pressure.startswith("wading_"), "ready": True, "alive": True,
    }
    events = [{"seq": 1, "type": "hurt", "source": "mob_attack", "health": 8.0 if pressure == "fresh_health" else health}]
    if pressure.endswith("burst"):
        events.append({"seq": 2, "type": "hurt", "source": "mob_attack", "health": health})
    calls = []

    class PoolBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            pytest.fail(f"Unexpected action without an identified attacker: {action}")

    def reach_shore(threat_pos, *, keep_shield, shield_target_id, under_attack):
        assert threat_pos is None and shield_target_id is None and not keep_shield and under_attack
        calls.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(PoolBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard, "_escape_water", lambda *_a, **_kw: pytest.fail("Ordinary escape delayed shore movement"))

    if entry == "poll":
        assert guard.poll()["actions"][-1]["action"] == "escape_to_shore"
    elif entry == "boundary":
        assert "登岸" in guard.stabilize_after_tools()
    else:
        result = guard.handle_preemption({"source": "tool_interrupted_hurt", "hurt_source": "mob_attack"},
                                         interrupted_name="mc_skill_unstick")
        assert result["reflex_mode"] == "water_surface"
    assert calls == ["shore"] and status["on_ground"]


@pytest.mark.parametrize("landing", ["grounded", "grounded_on_second_sample", "airborne", "wet_again"])
def test_water_macro_checks_that_a_dry_jump_reaches_ground(monkeypatch, landing):
    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", lambda _: None)
    status = {
        "pos": [9148.7, 63.4, -11301.5], "health": 20, "food": 20, "oxygen": 140,
        "in_water": True, "on_ground": False, "ready": True, "alive": True,
    }
    rechecks = []
    shore_calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if kwargs.get("timeout_s") == 0.25:
                    rechecks.append(action)
                    grounded = landing == "grounded" or (landing == "grounded_on_second_sample" and len(rechecks) == 2)
                    status.update(on_ground=grounded, in_water=landing == "wet_again")
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    class EscapeMacro:
        def escape_water(self):
            status.update(in_water=False, pos=[9149.7, 64.2, -11301.6])
            return {"ok": True, "in_water": False, "pos_after": status["pos"]}

    def reach_shore(*args, **kwargs):
        shore_calls.append(dict(status))
        return {"ok": False, "in_water": status["in_water"]}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=EscapeMacro())
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    incident = guard.poll()

    water = incident["actions"][0]
    assert water["action"] == "escape_water"
    assert water["ok"] is landing.startswith("grounded")
    assert rechecks == ["status"] * (2 if landing in {"grounded_on_second_sample", "airborne"} else 1)
    if landing.startswith("grounded"):
        assert shore_calls == []
    else:
        assert water["result"]["reason"] == "shore_landing_unconfirmed"
        assert water["result"]["in_water"] is (landing == "wet_again")
        assert len(shore_calls) == 1


@pytest.mark.parametrize("landing", ["dry", "wet", "airborne", "busy", "respawned"])
def test_water_macro_read_timeout_requires_confirmed_same_life_shore(landing):
    status = {
        "pos": [5011.2, 63, 18008.8], "health": 20, "food": 20, "oxygen": 260, "dimension": "overworld",
        "in_water": True, "submerged": True, "on_ground": False, "ready": True, "alive": True, "last_death": {"t": 1},
    }
    stops = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            assert action == "stop"
            stops.append(action)
            return {"ok": True}

    class EscapeMacro:
        def escape_water(self):
            status.update(
                pos=[5005.1, 63, 18015.6], in_water=landing == "wet", submerged=landing == "wet",
                on_ground=landing not in {"wet", "airborne"}, busy="move_to" if landing == "busy" else None,
            )
            if landing == "respawned":
                status["last_death"] = {"t": 2}
            raise httpx.ReadTimeout("lost the move reply")

    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=EscapeMacro())
    escape, = guard.poll()["actions"]

    assert escape["ok"] is (landing == "dry")
    assert guard._last_water_escape_failed is (landing != "dry")
    assert stops == ([] if landing == "dry" else ["stop"])
    if landing == "dry":
        assert escape["result"]["confirmed_after_read_timeout"] is True
        assert escape["result"]["pos_after"] == [5005.1, 63, 18015.6]
    else:
        assert escape["error"]["code"] == "network_uncertain"


@pytest.mark.parametrize("landing", ["threat_close", "threat_clear", "respawned"])
def test_water_macro_restores_raised_shield_on_dry_landing(landing):
    zombie = {"name": "zombie", "id": 4748724, "dist": 1.5}
    status = {
        "pos": [-11071.4, 50, 10869.4], "health": 16, "food": 20, "oxygen": 140,
        "in_water": True, "on_ground": True, "ready": True, "alive": True, "blocking": True,
        "dimension": "overworld", "last_death": {"t": 1}, "offhand": {"name": "shield"},
        "nearest_hostile": zombie,
    }
    commands = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": status["offhand"]}
            commands.append((action, body))
            return {"blocking": True}

    class EscapeMacro:
        def escape_water(self):
            status.update(pos=[-11071.7, 50, 10869.7], in_water=False, blocking=False, health=6)
            if landing == "respawned":
                status.update(last_death={"t": 2}, ready=False)
            if landing == "threat_clear":
                status["nearest_hostile"] = None
            return {"ok": True, "in_water": False, "pos_after": status["pos"]}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=EscapeMacro())
    water, = guard.poll()["actions"]

    assert water["ok"] is (landing != "respawned")
    if landing == "threat_close":
        assert commands == [
            ("look", {"entity_id": zombie["id"]}),
            ("posture", {"shield": True, "jump": False, "sneak": False}),
        ]
        assert water["result"]["shield_restored"] is True
    else:
        assert commands == []
        assert "shield_restored" not in water["result"]


@pytest.mark.parametrize("rising", [True, False])
def test_drowning_continues_a_verified_ascent_before_trying_to_path_to_shore(rising):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [-11061.7, 60.2, 10936.7], "in_water": True, "submerged": True,
        "oxygen": 0, "on_ground": False, "ready": True, "alive": True,
    }
    bank = [-11075, 62, 10937]
    actions = []

    class WetCaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "control" and not body.get("forward"):
                actions.append("up")
                if rising and actions == ["up"]:
                    status["pos"] = [-11061.7, 61.0, 10936.7]
                elif rising:
                    status.update(pos=[-11061.7, 63, 10936.7], oxygen=300, submerged=False, in_water=False, on_ground=True)
            if action == "find_blocks":
                return {"blocks": [{"name": "stone", "pos": bank}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                actions.append("move_to")
                status.update(pos=[body["x"], body["y"], body["z"]], submerged=False, in_water=False, on_ground=True)
            return {"ok": True}

        def status(self):
            return dict(status)

    result = CombatEnv(WetCaveBot()).escape_deep_water_to_shore(surface_first=True)

    assert result["ok"] is True and result["in_water"] is False
    assert actions == (["up", "up"] if rising else ["up", "move_to"])


@pytest.mark.parametrize("pocket", ["breathes", "no_air", "lava", "lava_overhead", "respawned", "still_submerged", "probe_timeout"])
def test_stalled_drowning_ascent_tries_an_adjacent_air_pocket_before_the_bank(monkeypatch, pocket):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    status = {"pos": [34027.5, 61.2, 34000.5], "in_water": True, "submerged": True, "oxygen": 0,
              "on_ground": False, "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1}}
    motions = []

    class FloodedPassageBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                assert kwargs["timeout_s"] <= 0.3 and kwargs["allow_reauth"] is False
                return self.status()
            if action == "get_block":
                x, y, z = body["pos"]
                if pocket == "probe_timeout" and kwargs.get("timeout_s") is not None:
                    raise httpx.ReadTimeout("water geometry unavailable")
                if z == 34000 and x == 34027:
                    if pocket == "lava_overhead" and y == 63:
                        return {"name": "lava"}
                    return {"name": "water" if y <= 62 else "stone"}
                if z == 34000 and x == 34028:
                    if pocket == "lava" and y == 62:
                        return {"name": "lava"}
                    return {"name": "water" if y <= 62 or pocket == "no_air" else "air"}
                return {"name": "stone"}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                if not body.get("forward"):
                    motions.append("up")
                    return {"ok": True}
                motions.append("side")
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx == pytest.approx(1) and dz == pytest.approx(0)
                assert body["jump"] and body["stop_on"] == ["death"]
                status["pos"] = [34028.5, 62, 34000.5]
                if pocket == "respawned":
                    status.update(in_water=False, last_death={"t": 2})
                if pocket == "breathes":
                    status.update(submerged=False, oxygen=20)
            if action == "move_to":
                motions.append("bank")
                status.update(pos=[body["x"], body["y"], body["z"]], in_water=False, submerged=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(FloodedPassageBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: [34028.5, 64.0, 34001.5])
    result = env.escape_deep_water_to_shore(surface_first=True)

    if pocket == "breathes":
        assert motions == ["up", "side"]
        assert result["mode"] == "breathing" and result["in_water"] is True and result["ok"] is False
    elif pocket == "respawned":
        assert motions == ["up", "side"] and result["mode"] == "respawned"
    else:
        expected = ["up", "side", "bank"] if pocket == "still_submerged" else ["up", "bank"]
        assert motions == expected and result["ok"] is True


@pytest.mark.parametrize("starts_in_lava, surface_first", [(True, True), (False, True), (False, False)])
def test_directed_shore_escape_does_not_report_lava_as_dry_land(starts_in_lava, surface_first):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [-6139.7, 43.2, -6089.7], "in_water": not starts_in_lava, "in_lava": starts_in_lava,
        "on_ground": starts_in_lava, "ready": True, "alive": True, "dimension": "overworld",
        "submerged": not starts_in_lava,
    }
    commands = []

    class FloodedCaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            commands.append(action)
            if action == "control":
                status.update(in_water=False, in_lava=True, on_ground=True)
            return {"ok": True}

    result = CombatEnv(FloodedCaveBot()).escape_deep_water_to_shore(surface_first=surface_first)

    assert result["ok"] is False and result["in_lava"] is True
    assert result["mode"] == "entered_lava"
    assert commands == ([] if starts_in_lava else ["posture", "control"])


@pytest.mark.parametrize("terrain", ["scan", "radial", "open_water"])
def test_flooded_cave_escape_uses_local_water_level(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {"pos": [2930.5, 39.2, -10120.5], "in_water": True, "on_ground": False, "alive": True, "ready": True}
    local_ledge = [2930, 39, -10125]
    moves = []

    class FloodedCaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                distant_lip = {"name": "grass_block", "pos": [2930, 62, -10121]}
                ledge = {"name": "stone", "pos": local_ledge}
                return {"blocks": [distant_lip, ledge] if terrain == "scan" else []}
            if action == "get_block":
                x, y, z = body["pos"]
                return {"name": "stone" if terrain != "open_water" and [x, y, z] == local_ledge else "air"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                moves.append(goal)
                if goal == [2930.5, 40.0, -10124.5]:
                    status.update(pos=goal, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(FloodedCaveBot()).escape_deep_water_to_shore()

    goal, = moves
    if terrain == "open_water":
        assert goal == pytest.approx([2930.5, 40.2, -10132.5])
        assert result["mode"] == "surface_retreat"
        assert result["ok"] is False and result["in_water"] is True
    else:
        assert goal == [2930.5, 40.0, -10124.5]
        assert result["ok"] is True and result["in_water"] is False


def test_directed_water_escape_searches_the_bank_at_the_post_surge_water_level():
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [11208.5, 59.2, -11028.5], "in_water": True, "submerged": True,
        "on_ground": False, "alive": True, "ready": True,
    }
    bank = [11206, 62, -11048]
    moves = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "control" and body.get("ms") == 750:
                status.update(pos=[11206.5, 62.2, -11040.5], submerged=False)
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": bank}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                moves.append(goal)
                if goal == [11206.5, 63.0, -11047.5]:
                    status.update(pos=goal, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore()

    assert moves == [[11206.5, 63.0, -11047.5]]
    assert result["ok"] is True and result["in_water"] is False


@pytest.mark.parametrize("bank_flooded", [False, True])
def test_water_retry_rechecks_nearby_bank_before_swimming_past_it(monkeypatch, bank_flooded):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [7938.5, 63.0, 2777.5]
    status = {
        "pos": [7923.2, 62.8, 2778.8], "in_water": True, "submerged": False,
        "on_ground": False, "alive": True, "ready": True, "dimension": "overworld",
    }
    attempts = []
    retry = False
    surges = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (7938, 2777):
                    return {"name": "stone" if y == 62 else "water" if y == 63 and bank_flooded else "air"}
                return {"name": "water"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                attempts.append(goal)
                if not retry:
                    raise ApiError("path_not_found", "shore movement stalled")
                if goal == bank:
                    status.update(pos=goal, in_water=False, on_ground=True)
            if action == "control" and not retry and body.get("ms") == 1150:
                status.update(pos=[7936.0, 62.6, 2777.5])
            if action == "control" and retry and body.get("ms") == 750:
                surges.append(body["forward"])
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: None if retry else bank)

    assert env.escape_deep_water_to_shore()["in_water"] is True
    retry = True
    status.update(submerged=True)
    result = env.escape_deep_water_to_shore()

    assert surges == [bank_flooded]
    assert ("shore_bank_retry" in result["actions"]) is not bank_flooded
    if bank_flooded:
        assert attempts.count(bank) == 1
        assert result["in_water"] is True
    else:
        assert attempts == [bank, bank]
        assert result["ok"] is True and result["in_water"] is False


@pytest.mark.parametrize("bank_state", [
    "clear", "flooded", "new_life", "observed_after_tool", "observed_during_poll", "higher_bank", "too_high",
])
def test_open_water_retreat_rechecks_the_last_confirmed_dry_bank(monkeypatch, bank_state):
    bank_y = {"higher_bank": 64, "too_high": 65}.get(bank_state, 63)
    bank = [7884.5, bank_y, 2779.5]
    wet = [7885.5, 62.7, 2751.5]
    status = {
        "pos": wet, "in_water": True, "on_ground": False, "alive": True, "ready": True,
        "dimension": "overworld", "last_death": {"t": 1},
    }
    moves = []

    class WaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (7884, 2779):
                    if y == bank_y - 1:
                        return {"name": "stone"}
                    return {"name": "water" if bank_state == "flooded" else "air"}
                return {"name": "water"}
            if action == "move_to":
                target = [body["x"], body["y"], body["z"]]
                moves.append(target)
                if target == bank:
                    status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    def generic_escape(_skills):
        status.update(pos=bank, in_water=False, on_ground=True)
        return {"ok": True, "in_water": False, "pos_after": bank}

    guard = GenerationGuard(WaterBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(MacroSkills, "escape_water", generic_escape)
    if bank_state in {"observed_after_tool", "observed_during_poll"}:
        status.update(pos=bank, in_water=False, on_ground=True)
        if bank_state == "observed_after_tool":
            guard.stabilize_after_tools()
        else:
            guard.poll()
    else:
        assert guard._escape_water(dict(status))["ok"] is True

    status.update(pos=wet, in_water=True, on_ground=False)
    if bank_state == "new_life":
        status["last_death"] = {"t": 2}
    result = guard.combat_env.escape_deep_water_to_shore()

    safe_bank = bank_state not in {"flooded", "new_life", "too_high"}
    assert ("return_to_dry_bank" in result["actions"]) is safe_bank
    assert (bank in moves) is safe_bank
    assert result["ok"] is safe_bank


@pytest.mark.parametrize("threat", ["none", "known", "unlocated"])
def test_short_shore_crossing_limits_pathfinder_wait_when_threatened(threat):
    from mc_agent.skills.combat_env import CombatEnv

    under_attack = threat != "none"
    status = {"pos": [10989.2, 62.7, -10987.8], "in_water": True, "on_ground": False,
              "alive": True, "ready": True}
    ledge = [10992, 62, -10985]
    motions = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": ledge}]}
            if action == "get_block":
                return {"name": "grass_block" if body["pos"] == ledge else "air"}
            if action == "move_to":
                motions.append("move_to")
                assert body["timeout"] == (1800 if under_attack else 4200)
                if under_attack:
                    assert kwargs["timeout_s"] == 2.4 and kwargs["allow_reauth"] is False
                raise httpx.ReadTimeout("shore pathfinder did not settle")
            if action == "stop" or (action == "control" and body == {"wait": False}):
                if under_attack:
                    assert kwargs["timeout_s"] == 0.5 and kwargs["allow_reauth"] is False
            if action == "control" and body.get("forward"):
                motions.append("jump")
                assert body["jump"] is True
                status.update(pos=[10992.5, 63.0, -10984.5], in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore(
        shield_target_id=44 if threat == "known" else None, under_attack=threat == "unlocated",
    )

    assert motions == ["move_to", "jump"]
    assert result["ok"] is True and result["in_water"] is False
    assert result["actions"] == ["move_to_shore_failed", "shore_lip_jump"]


@pytest.mark.parametrize("hop_result", ["landed", "still_wet", "respawned"])
@pytest.mark.parametrize("under_attack", [False, True])
def test_shore_escape_hops_onto_a_verified_adjacent_bank_before_pathing(hop_result, under_attack):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [19062.5, 63.0, 41037.5]
    floor = [19062, 62, 41037]
    status = {
        "pos": [19062.0, 62.0, 41038.3], "in_water": True, "on_ground": False,
        "alive": True, "ready": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    motions = []

    class BankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": floor}]}
            if action == "get_block":
                return {"name": "grass_block" if body["pos"] == floor else "air"}
            if action == "control" and body.get("forward"):
                motions.append("jump")
                assert body["jump"] and body["ms"] == 450
                if hop_result == "landed":
                    status.update(pos=bank, in_water=False, on_ground=True)
                elif hop_result == "respawned":
                    status.update(pos=[19105, 76, 41080], in_water=False, on_ground=True, last_death={"t": 2})
            if action == "move_to":
                motions.append("move_to")
                assert [body[axis] for axis in ("x", "y", "z")] == bank
                status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(BankBot()).escape_deep_water_to_shore(under_attack=under_attack)

    assert motions == (["jump", "move_to"] if hop_result == "still_wet" else ["jump"])
    if hop_result == "respawned":
        assert result["mode"] == "respawned" and result["ok"] is False
    else:
        assert result["ok"] is True and result["pos_after"] == bank


@pytest.mark.parametrize("bank_found", [True, False])
@pytest.mark.parametrize("pressure", ["none", "melee", "drowning"])
def test_w20_water_escape_bounds_pathfinder_even_without_nearby_bank(monkeypatch, bank_found, pressure):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [233456.5, 65, 246404.5]
    status = {
        "pos": [233452.7, 62.2, 246400.7], "health": 2.17, "in_water": True, "submerged": False,
        "on_ground": False, "alive": True, "ready": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    if pressure == "drowning":
        status.update(submerged=True, oxygen=4)
    commands = []

    class PoolBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                return {"name": "water"}
            if action == "move_to":
                commands.append(("move", body["timeout"], kwargs.get("timeout_s")))
                raise httpx.ReadTimeout("path not confirmed before the budget")
            if action == "stop":
                commands.append(("stop", kwargs.get("timeout_s")))
            if action == "control" and body.get("forward"):
                commands.append(("swim", body["ms"]))
            return {"ok": True}

    env = CombatEnv(PoolBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: bank if bank_found else None)
    result = env.escape_deep_water_to_shore(under_attack=pressure == "melee", surface_first=pressure == "drowning")

    if pressure != "none":
        server_ms, transport_s, stop_s = (1800, 2.4, 0.5) if bank_found else (1000, 1.5, 0.35)
        assert commands[:2] == [("move", server_ms, transport_s), ("stop", stop_s)]
    else:
        assert commands[:2] == [("move", 4200 if bank_found else 2800, None), ("stop", None)]
    assert commands[-1] == ("swim", 1150 if bank_found else 950)
    assert result["shore_target"] == (bank if bank_found else None)
    assert result["ok"] is False and result["in_water"] is True

