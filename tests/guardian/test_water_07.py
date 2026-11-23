from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("low_bank", ["grass_block", "stone", "blocked", "scan_timeout"])
def test_shore_escape_prefers_an_accessible_low_bank_over_a_closer_high_lip(low_bank):
    from mc_agent.skills.combat_env import CombatEnv

    high = [1, 64, 0]
    low = [3, 62, 0]
    status = {"pos": [0.5, 62.4, 0.5], "in_water": True, "on_ground": False, "alive": True, "ready": True}
    moves = []
    retry_options = []

    class PoolBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                if low_bank == "scan_timeout" and "stone" in body["types"]:
                    retry_options.append(kwargs)
                    raise httpx.ReadTimeout("low-bank scan stalled")
                rows = [{"name": "grass_block", "pos": high}]
                material = "stone" if low_bank in {"stone", "scan_timeout"} else "grass_block"
                if material in body["types"]:
                    rows.append({"name": material, "pos": low})
                return {"blocks": rows}
            if action == "get_block":
                return {"name": "stone" if low_bank == "blocked" and body["pos"] == [3, 64, 0] else "air"}
            if action == "move_to":
                target = [body[axis] for axis in ("x", "y", "z")]
                moves.append(target)
                status.update(pos=target, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(PoolBot()).escape_deep_water_to_shore()

    assert result["ok"] is True
    assert moves == [[1.5, 65.0, 0.5] if low_bank in {"blocked", "scan_timeout"} else [3.5, 63.0, 0.5]]
    if low_bank == "scan_timeout":
        options, = retry_options
        assert 0 < options["timeout_s"] <= 0.65 and options["allow_reauth"] is False


@pytest.mark.parametrize("reentry", ["same_pool", "new_life", "expired"])
def test_shore_escape_remembers_both_failed_banks_in_the_same_pool(monkeypatch, reentry):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    high_banks = ([1, 63, 0], [0, 64, -2])
    low_bank = [4, 62, 0]
    status = {"pos": [0.5, 62.4, 0.5], "in_water": True, "on_ground": False, "alive": True,
              "ready": True, "dimension": "overworld", "last_death": {"t": 1}}
    moves = []

    class PoolBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": bank} for bank in (*high_banks, low_bank)]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                target = [body[axis] for axis in ("x", "y", "z")]
                moves.append(target)
                if target[1] > 63:
                    raise ApiError("path_not_found", "bank above a steep lip")
                status.update(pos=target, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(PoolBot())
    assert env.escape_deep_water_to_shore()["in_water"] is True
    clock[0] += 8.5
    assert env.escape_deep_water_to_shore()["in_water"] is True
    clock[0] += 31.0 if reentry == "expired" else 8.5
    if reentry == "new_life":
        status["last_death"] = {"t": 2}
    third = env.escape_deep_water_to_shore()

    assert third["ok"] is (reentry == "same_pool")
    assert moves == [[1.5, 64.0, 0.5], [0.5, 65.0, -1.5],
                     [4.5, 63.0, 0.5] if reentry == "same_pool" else [1.5, 64.0, 0.5]]


@pytest.mark.parametrize("same_life", [True, False])
@pytest.mark.parametrize("under_attack", [False, True])
def test_water_escape_retries_a_different_bank_after_stalling_below_a_high_lip(monkeypatch, same_life, under_attack):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [11298.2, 62.2, 11021.3], "dimension": "overworld", "last_death": {"t": 1},
        "health": 9.0, "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    high_bank = [11300, 64, 11021]
    low_bank = [11296, 62, 11023]
    targets = []

    class RiverBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                banks = (high_bank, low_bank) if targets else (high_bank,)
                return {"blocks": [{"name": "grass_block", "pos": point} for point in banks]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                target = [body["x"], body["y"], body["z"]]
                targets.append(target)
                if target[1] == 63:
                    status.update(pos=target, in_water=False, on_ground=True)
                else:
                    status["pos"] = [11300.5, 62.9, 11021.8]
            return {"ok": True}

    env = CombatEnv(RiverBot())
    shield_target = 44 if under_attack else None
    first = env.escape_deep_water_to_shore(shield_target_id=shield_target)
    assert first["in_water"] is True and first["shore_target"] == [11300.5, 65.0, 11021.5]
    clock[0] += 4.0
    if not same_life:
        status["last_death"] = {"t": 2}
    second = env.escape_deep_water_to_shore(shield_target_id=shield_target)

    expected_bank = [11296.5, 63.0, 11023.5] if same_life else first["shore_target"]
    assert second["ok"] is same_life
    assert targets == [first["shore_target"], expected_bank]


@pytest.mark.parametrize("reentry", ["same_approach", "higher", "other_side", "new_life", "expired"])
def test_w20_drowning_does_not_reselect_the_same_high_bank_after_drifting(monkeypatch, reentry):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    high_bank = [241023.5, 18.0, 193551.5]
    alternative = [241024.5, 17.0, 193560.5]
    status = {
        "pos": [241023.5, 14.7, 193551.5], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "submerged": True, "oxygen": 17, "on_ground": False, "alive": True, "ready": True,
    }
    targets = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                return {"name": "water"}
            if action == "move_to":
                bank = [body[axis] for axis in ("x", "y", "z")]
                targets.append(bank)
                if len(targets) == 1:
                    status["pos"] = [241023.5, 15.2, 193553.5]
                else:
                    status.update(pos=bank, in_water=False, submerged=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda _pos, *, excluded_shores, **_kw: (
        alternative if tuple(high_bank) in excluded_shores else high_bank
    ))
    first = env.escape_deep_water_to_shore(surface_first=True)
    assert first["shore_target"] == high_bank and first["in_water"] is True

    y, z = {"higher": (17.0, 193554.5), "other_side": (16.2, 193549.5)}.get(reentry, (16.2, 193559.4))
    status.update(pos=[241023.5, y, z], oxygen=4)
    if reentry == "new_life":
        status["last_death"] = {"t": 2}
    clock[0] += 31.0 if reentry == "expired" else 8.0
    second = env.escape_deep_water_to_shore(surface_first=True)

    expected = alternative if reentry == "same_approach" else high_bank
    assert targets == [high_bank, expected]
    assert second["shore_target"] == expected and second["ok"] is True


def test_water_boundary_avoids_a_shore_path_that_crosses_a_creeper(monkeypatch):
    status = {
        "pos": [0.5, 62.2, 0.5], "health": 20, "food": 20, "oxygen": 320,
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "creeper", "dist": 5.0},
    }
    moves = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [{"id": 44, "name": "creeper", "pos": [5.5, 62.2, 0.5]}]}
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": [11, 62, 0]},
                                   {"name": "grass_block", "pos": [-13, 62, 0]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                moves.append(goal)
                status.update(pos=goal, in_water=False, on_ground=True)
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda _status: pytest.fail("nearby creeper needs a threat-aware shore"))

    note = guard.stabilize_after_tools()

    assert moves == [[-12.5, 63.0, 0.5]]
    assert note is not None and "登岸" in note


@pytest.mark.parametrize(("entry", "pressure"), [
    ("poll", "nearby"), ("poll", "at_surface"), ("poll", "fresh_hit"), ("boundary", "nearby"),
    ("poll", "distant"), ("poll", "occluded"),
    ("poll", "wading_hit"), ("poll", "wading_occluded"), ("poll", "wading_idle"),
])
def test_swimming_near_exposed_melee_uses_checked_shore_without_generic_float(monkeypatch, entry, pressure):
    zombie_pos = [19981.4, 62, 32993.7]
    distance = 7.0 if pressure == "distant" else 3.2
    zombie = {"id": 357966, "name": "zombie", "dist": distance, "blocked_by_wall": pressure.endswith("occluded")}
    status = {
        "pos": [19981.4, 62, 32990.5], "health": 18.5, "food": 20, "oxygen": 220,
        "in_water": True, "submerged": True, "on_ground": False, "ready": True, "alive": True,
        "nearest_hostile": zombie, "dimension": "overworld",
    }
    if pressure == "at_surface":
        status.update(oxygen=300, submerged=False, jumping=True)
    if pressure.startswith("wading_"):
        status.update(oxygen=300, submerged=False, on_ground=True)
    calls = []

    class CreekBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                has_hit = pressure in {"fresh_hit", "wading_hit", "wading_occluded"}
                return {"events": [hit] if has_hit and not kwargs["params"]["since"] else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                return {"entities": [{**zombie, "pos": zombie_pos}]}
            raise AssertionError(action)

    guard = GenerationGuard(CreekBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def float_to_surface(*_args, **_kwargs):
        calls.append("float")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def reach_shore(threat_pos, *, keep_shield, shield_target_id, avoid_close_threat, under_attack=False):
        assert threat_pos == zombie_pos and shield_target_id == zombie["id"]
        assert not keep_shield and avoid_close_threat
        assert under_attack is (pressure == "wading_hit")
        calls.append("shore")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard, "_escape_water", float_to_surface)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    outcome = guard.poll() if entry == "poll" else guard.stabilize_after_tools()
    if pressure == "wading_idle":
        assert outcome is None and calls == []
    else:
        assert outcome is not None
        assert calls == (["float"] if pressure in {"distant", "occluded", "wading_occluded"} else ["shore"])


@pytest.mark.parametrize("entry", ["poll", "mc_attack", "mc_move_to"])
def test_nearby_drowned_sends_water_recovery_directly_to_shore(monkeypatch, entry):
    drowned = {"id": 44, "name": "drowned", "dist": 4.7}
    drowned_pos = [9266.3, 61, -11355]
    status = {
        "pos": [9265.5, 62, -11350.5], "health": 14.1, "food": 20, "oxygen": 220,
        "in_water": True, "submerged": True, "on_ground": False, "blocking": False,
        "ready": True, "alive": True, "nearest_hostile": drowned,
    }
    actions = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [
                    {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": drowned},
                ]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                return {"entities": [{**drowned, "pos": drowned_pos}]}
            if action == "posture":
                assert body["shield"] and body["jump"]
                actions.append("shield")
                status["blocking"] = True
            return {"ok": True}

    def reach_shore(threat_pos, *, keep_shield, shield_target_id):
        assert threat_pos == drowned_pos and keep_shield and shield_target_id == 44
        assert status["blocking"]
        actions.append("shore")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda *args, **kwargs: pytest.fail("generic escape delays shore recovery"))
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    if entry == "poll":
        incident = guard.poll()
        assert incident["actions"][-1]["action"] == "escape_to_shore"
    else:
        receipt = guard.handle_preemption({
            "source": "tool_interrupted_hurt" if entry == "mc_move_to" else "boundary_threat",
            "primary_entity_id": 44, "primary_type": "drowned",
        }, interrupted_name=entry)
        assert receipt["reflex_mode"] == "water_surface" and receipt["status_after"]["on_ground"]
    assert actions == ["shield", "shore"]


@pytest.mark.parametrize("landing", ["grounded", "airborne", "in_water", "settled", "settled_above", "respawned"])
def test_shore_escape_confirms_landing_after_airborne_edge_receipt(landing):
    from mc_agent.skills.combat_env import CombatEnv

    moves = []
    posture = []
    landing_reads = []
    status = {"pos": [10937.5, 62.5, -11007.3], "in_water": True, "on_ground": False,
              "alive": True, "ready": True}

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                landing_reads.append(kwargs["timeout_s"])
                assert kwargs["allow_reauth"] is False
                if landing in {"settled", "settled_above"}:
                    status.update(on_ground=True, pos=[10938.5, 64.0, -11007.5])
                elif landing == "respawned":
                    status.update(on_ground=True, last_death={"t": 1})
                return self.status()
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": [10938, 63, -11008]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "posture":
                posture.append(body)
            if action == "move_to":
                moves.append(body)
                if len(moves) == 1:
                    status.update(pos=[10937.7, 63.3, -11007.3], in_water=False)
                else:
                    assert posture[-1] == {"shield": True, "sneak": False, "jump": False}
                    if landing == "grounded":
                        status.update(pos=[10938.5, 64.0, -11007.5], on_ground=True)
                    elif landing in {"settled", "respawned"}:
                        status.update(pos=[10938.5, 64.0, -11007.7])
                    elif landing == "settled_above":
                        status.update(pos=[10938.4, 64.9, -11007.5])
                    elif landing == "in_water":
                        status.update(pos=[10937.7, 63.0, -11007.3], in_water=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore(keep_shield=True)

    assert len(moves) == 2
    assert moves[1]["range"] < moves[0]["range"]
    assert moves[1]["timeout"] < moves[0]["timeout"]
    assert moves[1]["stop_on"] == ["death"]
    assert tuple(moves[1][axis] for axis in ("x", "y", "z")) == (10938.5, 64.0, -11007.5)
    assert result["ok"] is (landing in {"grounded", "settled", "settled_above"})
    assert result["in_water"] is (landing == "in_water")
    assert posture[-1]["jump"] is (landing == "in_water")
    assert landing_reads == ([0.25] if landing in {"settled", "settled_above", "respawned"} else [])
    if landing == "respawned":
        assert result["mode"] == "respawned"


@pytest.mark.parametrize(("starts_submerged", "low_health_after_s", "float_timeout"), [
    (True, None, 20.0), (False, None, 20.0), (False, 3.0, 2.5), (False, 5.25, None),
])
def test_bug056_subcortical_water_escape_uses_budget_once(starts_submerged, low_health_after_s, float_timeout, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    calls = []
    events = []
    zombie = {"id": 42, "name": "zombie", "dist": 2.0, "pos": [2.5, 52, 0.5]}
    status = {
        "pos": [0.5, 52, 0.5], "health": 17, "food": 20, "alive": True, "ready": True,
        "in_water": starts_submerged, "submerged": starts_submerged, "on_ground": not starts_submerged,
        "nearest_hostile": zombie,
    }

    class CombatWaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "attack":
                calls.append("attack")
                status.update(in_water=True, submerged=True, on_ground=False)
                if low_health_after_s is not None:
                    clock[0] += low_health_after_s
                    status.update(health=8.1, nearest_hostile={"id": 43, "name": "skeleton", "dist": 8.0})
                return {"hits": 1, "killed": False}
            if action == "move_to":
                calls.append("water_move")
                assert float_timeout is not None and kwargs["timeout_s"] == pytest.approx(float_timeout)
                clock[0] += kwargs["timeout_s"]
                raise httpx.ReadTimeout("water move reached its deadline")
            if action == "stop" and kwargs["timeout_s"] == 1.5:
                calls.append("water_stop")
            return {"ok": True}

    monkeypatch.setattr(MacroSkills, "escape_water", lambda self: self.bot.call("move_to", {}, timeout_s=60))
    bot = CombatWaterBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)

    def blocked_shore(*args, **kwargs):
        calls.append("shore")
        if low_health_after_s is not None:
            assert guard.bot.active_deadline - clock[0] == pytest.approx(2.25 if float_timeout is None else 2.0)
            status.update(in_water=False, submerged=False, on_ground=True)
            return {"ok": True, "in_water": False}
        return {"ok": False, "in_water": True}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", blocked_shore)
    trigger = {"source": "tool_interrupted_hurt", "primary_entity_id": 42, "primary_type": "zombie", "distance": 2.0}
    if starts_submerged:
        result = guard.combat_slot.handle(guard.combat_env, trigger)
        assert result["mode"] == "water_surface"
    else:
        receipt = guard.handle_preemption(trigger, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface_abort"
        result = next(e["result"] for e in events if e["type"] == "guardian.action.completed")
    assert result["actions"][-2]["reason"] == "time_budget"
    assert result["actions"][-1] == {"action": "water_surface_shore", "ok": low_health_after_s is not None}
    float_calls = [] if float_timeout is None else ["water_move", "water_stop"]
    assert calls == (["attack"] if not starts_submerged else []) + float_calls + ["shore"]
    assert skills.bot is bot


@pytest.mark.parametrize(("terrain", "elapsed"), [("water", 21.0), ("stone", 21.0), ("water", 31.0)])
def test_critical_ranged_chase_checks_for_water_after_landing(monkeypatch, terrain, elapsed):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 100.0)
    pillager = {"id": 5475818, "name": "pillager", "dist": 6.9, "pos": [-10924.6, 62.5, -10944.3]}
    status = {
        "pos": [-10921.5, 63, -10950.4], "health": 3.3, "ready": True, "alive": True,
        "on_ground": True, "dimension": "overworld", "nearest_hostile": pillager,
    }
    attacks = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                y = body["pos"][1]
                return {"name": terrain if y == 62 else "air"}
            if action == "attack":
                attacks.append(body["entity_id"])
                raise httpx.ReadTimeout("attack response timed out")
            raise AssertionError(action)

    bot = ShoreBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_water_escape_at = 100.0 - elapsed

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": pillager["id"], "timeout": 6000}))

    if terrain == "water" and elapsed < guard.WATER_ESCAPE_RETRY_WINDOW_S:
        receipt = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert receipt["error"]["code"] == "unsafe_landing" and receipt["requires_replan"]
        assert attacks == []
    else:
        with pytest.raises(httpx.ReadTimeout, match="attack response timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert attacks == [pillager["id"]]


@pytest.mark.parametrize("tool, action, body, oxygen", [
    ("mc_skill_mine_dfs", "dig", {"pos": [41013, 22, 11034]}, 7),
    ("mc_skill_mine_blocks", "dig", {"pos": [41012, 21, 11034], "stop_on": ["death"]}, 7),
    ("mc_skill_mine_blocks", "dig", {"pos": [41012, 21, 11034], "stop_on": ["death"]}, 285),
    ("mc_skill_mine_blocks", "dig", {"pos": [41012, 21, 11034], "stop_on": ["death"]}, 110),
    ("mc_skill_unstick", "control", {"jump": True, "ms": 900, "stop_on": ["death"]}, 7),
])
def test_tool_water_threat_preempts_before_the_first_hit(monkeypatch, tool, action, body, oxygen):
    import threading

    stopped = threading.Event()
    status = {
        "pos": [41012.5, 22, 11034.4], "health": 20, "oxygen": 285,
        "ready": True, "alive": True, "on_ground": True, "in_water": False,
    }
    calls = []
    drowned = {"id": 45, "name": "drowned", "dist": 4.0, "pos": [41016.5, 22, 11034.4]}
    first_dig = [41014, 22, 11036]

    class CaveBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == action:
                if action == "dig" and body["pos"] == first_dig:
                    calls.append("first_dig")
                    return {"dug": "stone", "gained": {"cobblestone": 1}}
                calls.append(endpoint)
                status.update(in_water=True, submerged=True, on_ground=False, oxygen=oxygen)
                if oxygen == 110:
                    status.pop("submerged")
                if oxygen == 285:
                    status["nearest_hostile"] = drowned
                assert stopped.wait(1.5)
                if action == "dig":
                    if oxygen == 285:
                        raise ApiError("interrupted", "no confirmed dig")
                    return {"dug": "stone", "gained": {"cobblestone": 1}, "interrupted_by": "stop"}
            if endpoint == "stop":
                calls.append(endpoint)
                stopped.set()
                if oxygen != 285:
                    return {"stopped": True, "action": action, "released": tool != "mc_skill_unstick"}
            if endpoint == "inventory":
                return {"items": []}
            if endpoint == "events":
                return {"events": []}
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def shore(pos, *, surface_first, shield_target_id, **_kwargs):
        assert surface_first and status["health"] == 20
        if oxygen == 285:
            assert pos == drowned["pos"] and shield_target_id == drowned["id"]
        calls.append("surface")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    def invoke():
        if action == "dig":
            bot.call("dig", {"pos": first_dig})
        return json.dumps(bot.call(action, body))

    result = json.loads(guard.run_tool_with_reflex(tool, invoke))
    assert result["error"]["code"] == "guardian_preempted"
    assert result["stop_reason"] == "tool_water_threat"
    assert result["water_risk"] == {7: "low_oxygen", 110: "legacy_oxygen_hint", 285: "drowned"}[oxygen]
    assert result["reflex_mode"] == "water_surface"
    assert result["pos_at_preemption"] == status["pos"] == result["pos_after"]
    assert calls == (["first_dig"] if action == "dig" else []) + [action, "stop", "surface"]
    if oxygen == 285:
        assert "action_released" not in result
    else:
        assert result["action_released"] is (tool != "mc_skill_unstick")
    if action == "dig":
        expected_count = 1 if oxygen == 285 else 2
        assert result["partial_progress"] is True
        assert result["dug_count"] == expected_count and result["gained"] == {"cobblestone": expected_count}
        assert result["last_work_action"] == {"endpoint": "dig", "target": body["pos"]}
    else:
        assert "partial_progress" not in result and "dug_count" not in result and "gained" not in result
        assert "last_work_action" not in result
    assert guard._water_hurt_streak == 0


@pytest.mark.parametrize("condition, oxygen, expected_reason, sample_count", [
    ("brief", 20, None, 2), ("brief", 140, None, 2),
    ("continuous", 17, "sustained_submersion", 3), ("continuous", 20, "sustained_submersion", 3),
    ("continuous", 140, "sustained_submersion", 3),
    ("continuous", 400, "sustained_submersion", 3), ("continuous", None, "sustained_submersion", 3),
    ("health_missing", None, "sustained_submersion", 3), ("sinking", 20, "sustained_sinking", 2),
    ("new_life", 400, "sustained_submersion", 4),
    ("depleted", 7, "low_oxygen", 1), ("low_health", 20, "critical_health", 1),
])
def test_bug009_full_air_reading_uses_water_motion_and_duration(monkeypatch, condition, oxygen, expected_reason, sample_count):
    import threading

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-10890.5, 58, 2566.3], "in_water": True, "submerged": True, "on_ground": False,
        "health": 5 if condition == "low_health" else 20,
        "velocity": [0, -1.5 if condition == "sinking" else 0, 0], "dimension": "overworld",
        "ready": True, "alive": True,
    }
    if oxygen is not None:
        status["oxygen"] = oxygen
    if condition == "health_missing":
        status.pop("health")
    finished = threading.Event()
    samples = []
    calls = []

    class SubmergedMiner:
        def call(self, action, body=None, **kwargs):
            if action == "dig":
                calls.append(action)
                assert finished.wait(3.0)
                return {"dug": "stone", "gained": {"cobblestone": 1}}
            if action == "status":
                if threading.current_thread().name == "mc-creeper-watch" and not finished.is_set():
                    clock[0] += 1.1
                    samples.append(clock[0])
                    if condition == "new_life" and len(samples) == 2:
                        status["last_death"] = {"t": 1}
                    if condition == "brief" and len(samples) == 2:
                        status.update(submerged=False, on_ground=True)
                        finished.set()
                return dict(status)
            if action == "stop":
                calls.append(action)
                finished.set()
                return {"stopped": True, "released": True, "action": "dig"}
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    bot = SubmergedMiner()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)

    def shore(*args, **kwargs):
        calls.append("surface")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", lambda: json.dumps(bot.call(
        "dig", {"pos": [-10890, 58, 2567], "stop_on": ["death"]},
    ))))

    assert len(samples) == sample_count
    if expected_reason is None:
        assert calls == ["dig"] and result["dug"] == "stone"
    else:
        assert calls == ["dig", "stop", "surface"] and result["water_risk"] == expected_reason
        preempted = next(event for event in events.events if event["type"] == "guardian.preempted_tool")
        observation = preempted["water_observation"]
        assert observation["reason"] == expected_reason and observation["submerged"] is True
        first_current_life_sample = 1 if condition == "new_life" else 0
        assert observation["submerged_for_s"] == pytest.approx(samples[-1] - samples[first_current_life_sample])
        if expected_reason == "sustained_submersion":
            assert result["water_reentry_site"] == preempted["water_reentry_site"] == [-10891, 58, 2566]
            assert "沿干地换一个采矿入口" in result["message"]
        else:
            assert "water_reentry_site" not in result and "water_reentry_site" not in preempted

