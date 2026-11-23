from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("scene", ["sealed", "unsealed", "still_trapped", "read_unavailable"])
def test_bug132_staircase_edge_checks_that_the_lava_seal_actually_blocks_the_source(scene):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    origin = [1503.4, 66, 1417.5]
    source = [1502, 66, 1418]
    status = {
        "pos": origin, "dimension": "the_nether", "last_death": {"t": 1},
        "health": 20, "food": 20, "ready": True, "alive": True, "on_ground": True, "in_lava": False,
    }
    placements = []
    moves = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "basalt", "count": 4}]}
            if action == "get_block":
                cell = body["pos"]
                if cell == source:
                    if placements and scene == "read_unavailable":
                        raise httpx.ReadTimeout("seal observation timed out")
                    return {"name": "basalt" if placements and scene == "sealed" else "lava"}
                return {"name": "netherrack" if cell[1] < 66 and scene != "still_trapped" else "air"}
            if action == "place":
                placements.append(body["pos"])
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                moves.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                status["pos"] = [origin[0] + 2.0 * dx, 66, origin[2] + 2.0 * dz]
            return {"ok": True}

    events = EventSink(lambda _: None)
    guard = GenerationGuard(NetherBot(), events, checkpoint=lambda: None)
    result = json.dumps({
        "ok": False, "stop_reason": "fluid_or_bedrock", "direction": "south", "vertical": "down",
        "pos_after": origin,
        "blocker": {"block": "lava", "pos": source, "relation": "adjacent_fluid", "target": [1503, 66, 1418]},
    })

    note = guard.stabilize_after_tools([result])

    assert placements == [source]
    seal = next(e["result"] for e in events.events if e["type"] == "guardian.action.completed"
                and e["action"] == "boundary_guard_lava_edge")
    assert seal["sealed_positions"] == ([source] if scene == "sealed" else [])
    assert (seal.get("unsealed_lava_target") == source) is (scene in {"unsealed", "still_trapped"})
    assert len(moves) == int(scene == "unsealed")
    if scene == "unsealed":
        source_position = [source[0] + 0.5, 66, source[2] + 0.5]
        assert math.dist(status["pos"], source_position) > math.dist(origin, source_position)
        assert "已撤离未封住的岩浆" in note


@pytest.mark.parametrize("bank_state", [
    "dry", "flooded", "new_life", "old_bank", "healthy", "late", "late_flooded", "at_surface", "after_third_ascent",
])
def test_critical_lava_ascent_aims_for_a_recent_verified_dry_bank(monkeypatch, bank_state):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    bank = [-9984.5, -54, 2972.5]
    ascent_levels = {
        "late": [-55.3, -53.8],
        "late_flooded": [-55.3, -53.8],
        "at_surface": [-54.7, -53.8],
        "after_third_ascent": [-57, -56, -55],
    }.get(bank_state, [-53.8])
    start_y = {"late": -57.0, "late_flooded": -57.0, "at_surface": -56.5, "after_third_ascent": -58.0}.get(
        bank_state, -55.3,
    )
    status = {
        "pos": [-9986.4, start_y, 2971.5], "health": 16 if bank_state == "healthy" else 10,
        "dimension": "overworld", "last_death": {"t": 2}, "ready": True, "alive": True,
        "on_ground": False, "in_lava": True,
    }
    swims = []
    headings = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                lava_top = -55 if bank_state == "at_surface" else -54
                if (x, z) == (-9987, 2971) and y <= lava_top:
                    return {"name": "lava"}
                if (x, y, z) == (-9985, -55, 2972):
                    return {"name": "stone"}
                if (x, y, z) == (-9985, -54, 2972) and bank_state in {"flooded", "late_flooded"}:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "look":
                headings.append(body["yaw"])
            if action == "control" and body.get("jump"):
                swims.append(body["forward"])
                clock[0] += body["ms"] / 1000
                if body["forward"]:
                    destination = bank if headings[-1] != 0.0 else [-9989.5, -53, 2971.5]
                    status.update(pos=destination, on_ground=True, in_lava=False)
                else:
                    status.update(pos=[-9986.4, ascent_levels[swims.count(False) - 1], 2971.5])
            return {"ok": True}

    env = CombatEnv(LavaBot())
    env.remember_dry_bank({
        **status, "pos": bank, "on_ground": True, "in_lava": False,
        "last_death": {"t": 1 if bank_state == "new_life" else 2},
    })
    if bank_state == "old_bank":
        clock[0] += 31
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *_a, **_kw: {"best_yaw": 0.0})

    result = env.escape_lava()

    assert result["ok"] and not result["in_lava"]
    vertical_count = {"dry": 0, "late_flooded": 2, "after_third_ascent": 3}.get(bank_state, 1)
    assert swims == [False] * vertical_count + [True]
    assert ("lava_swim_to_recent_dry_bank" in result["actions"]) is (bank_state in {
        "dry", "late", "at_surface", "after_third_ascent",
    })


@pytest.mark.parametrize("bank", ["unsupported", "still_blocked", "clear_after_dig", "obsidian", "crying_obsidian"])
def test_lava_notch_checks_bank_support_and_actual_clearance(bank):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64.3, 0.5], "dimension": "the_nether", "health": 12,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    blocks = {(0, 64, 0): "lava"}
    if bank != "unsupported":
        blocks.update({(0, 64, -1): "netherrack", (0, 65, -1): "netherrack"})
    hard_block = bank in {"obsidian", "crying_obsidian"}
    if hard_block:
        blocks[(0, 65, -1)] = bank
        blocks.update({(0, 64, 1): "netherrack", (0, 65, 1): "netherrack"})
    digs = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "dig":
                pos = tuple(body["pos"])
                digs.append(pos)
                if bank == "clear_after_dig" or (hard_block and pos == (0, 65, 1)):
                    blocks.pop(pos)
            if action == "control" and body.get("forward") and digs:
                if hard_block and digs[-1] == (0, 65, 1):
                    status.update(pos=[0.5, 65, 1.5], in_lava=False, on_ground=True)
                elif bank == "clear_after_dig":
                    status.update(pos=[0.5, 65, -0.5], in_lava=False, on_ground=True)
            return {"ok": True}

    escaped = CombatEnv(LavaBot()).escape_lava()

    if hard_block:
        assert escaped["ok"] and "lava_notch_south" in escaped["actions"]
        assert "lava_notch_hard_block" in escaped["actions"]
        assert digs == [(0, 65, 1)]
        return
    assert escaped["ok"] is (bank == "clear_after_dig")
    assert ("lava_notch_north" in escaped["actions"]) is (bank == "clear_after_dig")
    assert digs == ([] if bank == "unsupported" else [(0, 65, -1)])
    assert not any(a.startswith("lava_notch_") for a in escaped["actions"] if a != "lava_notch_north")


@pytest.mark.parametrize("retry", ["tool_changed", "new_bank", "expired"])
@pytest.mark.parametrize("timeout_kind", ["transport", "server"])
def test_lava_notch_skips_recent_timeout_until_next_viable_attempt(monkeypatch, retry, timeout_kind):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3566.3, 64.4, 1794.7], "dimension": "the_nether", "health": 20,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    bank = {(3566, 64, 1793): "blackstone", (3566, 65, 1793): "blackstone"}
    digs = []
    held = None

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "obsidian", "count": 1}], "held": held}
            if action == "get_block":
                return {"name": bank.get(tuple(body["pos"]), "air")}
            if action == "dig":
                pos = tuple(body["pos"])
                digs.append(pos)
                if len(digs) == 1:
                    if timeout_kind == "server":
                        raise ApiError("timeout", "could not open the bank")
                    raise httpx.ReadTimeout("could not open the bank")
                bank.pop(pos)
            if action == "control" and body.get("forward") and len(digs) == 2:
                status.update(in_lava=False, on_ground=True, pos=[digs[-1][0] + 0.5, 65, 1793.5])
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {"best_yaw": 0.0})
    failure = env.escape_lava()
    assert "lava_notch_dig_timeout" in failure["actions"]
    assert failure["notch_dig_failures"] == [{
        "pos": [3566, 65, 1793], "block": "blackstone", "held": None,
        "error": "timeout" if timeout_kind == "server" else "ReadTimeout",
    }]

    clock[0] += 4.0
    assert "lava_notch_dig_deferred" in env.escape_lava()["actions"]
    assert digs == [(3566, 65, 1793)]

    if retry == "tool_changed":
        held = {"name": "iron_pickaxe"}
    elif retry == "new_bank":
        bank.update({(3567, 64, 1793): "blackstone", (3567, 65, 1793): "blackstone"})
        status["pos"] = [3567.3, 64.4, 1794.7]
    else:
        clock[0] += 15.0
    assert env.escape_lava()["ok"] is True
    assert digs[-1] == (3567 if retry == "new_bank" else 3566, 65, 1793)


def test_r19_lava_pit_places_netherrack_filler_before_second_sprint(monkeypatch):
    """Reproduces r19_short hy4_hy_8_nether: boxed Nether pit must try netherrack bridge right after first sprint instead of starving."""
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    clock = [5000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    state_p = {"in_lava": True, "pos": [967.5, 30.1, 373.5], "sprint_n": 0}
    action_seq: list[str] = []

    class _PitLavaBot:
        def status(self):
            return {
                "pos": list(state_p["pos"]),
                "health": 10.0,
                "alive": True,
                "ready": True,
                "in_lava": state_p["in_lava"],
                "on_ground": not state_p["in_lava"],
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            action_seq.append(action)
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "netherrack", "count": 48}]}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 0, 0])
                if p[1] in (30, 31) and p[0] == 967 and p[2] == 373:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                if (body or {}).get("forward"):
                    state_p["sprint_n"] += 1
                    # Simulate elapsed time hitting 4.6s right after sprint #1 (just before old 5.0s deadline!)
                    clock[0] += 4.6 if state_p["sprint_n"] == 1 else 1.0
                return {"ok": True}
            if action == "place":
                state_p["in_lava"] = False
                return {"ok": True}
            return {"ok": True}

    out_p = CombatEnv(_PitLavaBot()).escape_lava()
    assert out_p["ok"] is True and out_p["in_lava"] is False
    assert "lava_place_netherrack" in out_p["actions"]
    assert out_p["actions"].count("lava_jump_sprint") == 1


@pytest.mark.parametrize("distance", [2.2, 5.0])
def test_poll_raises_main_hand_shield_when_cornered_by_nether_melee(monkeypatch, distance):
    attacker = {"id": 77, "name": "wither_skeleton", "dist": distance, "pos": [-417.8 + distance, 53, -1880.3]}
    status = {
        "pos": [-417.8, 53, -1880.3], "dimension": "the_nether", "health": 2.48,
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "food": 20,
        "nearest_hostile": attacker,
    }
    shield = {"name": "shield", "count": 1}
    commands = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": attacker}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [shield, {"name": "iron_sword", "count": 1}], "held": shield, "offhand": None}
            commands.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", lambda *args, **kwargs: {
        "ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0,
    })

    incident = guard.poll()

    actions = ["combat_or_flee"]
    if distance <= 3.5:
        actions += ["equip_shield", "face_attacker", "raise_shield"]
        assert commands == [
            ("equip", {"item": "shield", "slot": "off-hand"}),
            ("look", {"entity_id": 77}),
            ("posture", {"shield": True, "jump": False, "sneak": False}),
        ]
    else:
        assert commands == []
    assert [action["action"] for action in incident["actions"]] == actions


@pytest.mark.parametrize("pressure", ["missing_shooter", "other_nearest", "melee_only", "new_life"])
def test_low_health_poll_defers_food_after_attack_read_timeout_under_arrow_fire(monkeypatch, pressure):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 51, "name": "zombie", "pos": [2.5, 71, 0.5], "dist": 2.0}
    status = {
        "pos": [0.5, 71, 0.5], "health": 7.67, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "nearest_hostile": zombie,
    }
    events = []
    commands = []

    class PortalBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 1}]}
            if action in {"stop", "use_item"}:
                commands.append(action)
                return {"ok": True}
            raise AssertionError(action)

    log = []
    guard = GenerationGuard(PortalBot(), EventSink(log.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def attack(_env, trigger):
        assert trigger["primary_entity_id"] == zombie["id"]
        events.append({"seq": 1, "type": "hurt", "source": "mob_attack" if pressure == "melee_only" else "arrow"})
        status["nearest_hostile"] = {**zombie, "dist": 9.0} if pressure == "other_nearest" else None
        raise httpx.ReadTimeout("attack reply timed out", request=httpx.Request("POST", "http://localhost/attack"))

    monkeypatch.setattr(guard.combat_slot, "handle", attack)
    first = guard.poll()
    assert first["event_seqs"] == [1]
    assert first["actions"][0]["error"]["code"] == "network_uncertain"
    assert commands == ["stop"]
    assert any(event.get("endpoint") == "attack" for event in log)

    clock[0] += 3.1
    if pressure == "new_life":
        events.append({"seq": 2, "type": "spawn"})
        status["last_death"] = {"t": 2}
        assert "death_or_connection_changed" in guard.poll()["reasons"]
        clock[0] += 3.1
    status["food"] = 19
    incident = guard.poll()
    if pressure in {"missing_shooter", "other_nearest"}:
        assert incident is None and commands == ["stop"]
        clock[0] += 5.0
        incident = guard.poll()
    assert incident["actions"][0]["action"] == "eat"
    assert commands == ["stop", "use_item"]


@pytest.mark.parametrize("destination", ["shore", "bridge", "no_blocks", "respawned", "slow_head", "slow_inventory"])
def test_critical_nether_lava_leaves_time_for_the_first_rescue_action(monkeypatch, destination):
    from mc_agent.skills.combat_env import CombatEnv, aim_yaw_pitch

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    start = [1498.5, 80.0, 1419.8]
    bank = [1500.5, 81, 1419.5]
    bank_block = [1500, 80, 1419]
    bank_yaw = aim_yaw_pitch(start, bank)[0]
    known_bank = destination in {"shore", "respawned"}
    status = {
        "pos": start, "dimension": "the_nether", "last_death": {"t": 1}, "health": 5.7,
        "in_lava": True, "ready": True, "alive": True, "on_ground": False, "yaw": 0.0,
    }
    rescue_actions = []
    head_reads = 0

    class LavaBot:
        def status(self):
            if status["in_lava"] and clock[0] >= 1001.43:
                status.update(health=0, alive=False, ready=False)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal head_reads
            if action == "status":
                return self.status()
            if action == "inventory":
                if destination == "slow_inventory":
                    clock[0] += kwargs.get("timeout_s", 2.0)
                    raise httpx.ReadTimeout("inventory read delayed")
                items = [] if destination == "no_blocks" else [{"name": "cobblestone", "count": 4}]
                return {"items": items}
            if action == "get_block":
                if body["pos"] == [1498, 81, 1419] and head_reads == 0:
                    head_reads += 1
                    if destination == "slow_head":
                        clock[0] += kwargs.get("timeout_s", 2.0)
                        raise httpx.ReadTimeout("head block read delayed")
                elif "timeout_s" in kwargs:
                    clock[0] += min(0.12, kwargs["timeout_s"])
                if body["pos"] == bank_block and known_bank:
                    return {"name": "netherrack"}
                return {"name": "lava" if body["pos"][1] <= 80 else "air"}
            if action == "find_blocks":
                clock[0] += min(0.24, kwargs["timeout_s"]) if known_bank else kwargs["timeout_s"]
                if destination == "respawned":
                    status.update(pos=[-132.5, 64, 60.5], last_death={"t": 2}, in_lava=False, on_ground=True)
                blocks = [{"name": "netherrack", "pos": bank_block}] if known_bank else []
                return {"blocks": blocks}
            if action in {"look", "control", "place", "posture"} and self.status()["alive"] is False:
                raise ApiError("dead", "already died")
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                rescue_actions.append(("swim", clock[0]))
                if destination in {"no_blocks", "slow_inventory"} or (
                    destination == "shore" and math.isclose(status["yaw"], bank_yaw)
                ):
                    status.update(pos=bank, in_lava=False, on_ground=True)
            if action == "place":
                rescue_actions.append(("bridge", clock[0]))
                status.update(pos=[1498.5, 81, 1419.8], in_lava=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()

    if destination == "respawned":
        assert result["respawned"] and not result["ok"]
        assert rescue_actions == []
    else:
        assert result["ok"] and not result["in_lava"]
        (action, started), = rescue_actions
        assert action == ("bridge" if destination in {"bridge", "slow_head"} else "swim")
        assert started < (1001.35 if destination == "slow_head" else 1001.2)


@pytest.mark.parametrize("transport_overruns", [False, True])
def test_lava_guard_deadline_includes_last_resort_notch_dig(monkeypatch, transport_overruns):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3186.7, 64.6, -2046.8], "health": 13, "food": 20, "alive": True, "ready": True,
        "in_lava": True, "on_ground": False, "dimension": "the_nether",
    }
    calls = []
    events = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            timeout = kwargs.get("timeout_s")
            calls.append((action, clock[0], timeout))
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                return {"events": []}
            if action == "get_block":
                return {"name": "netherrack"}
            if action == "control" and body.get("forward"):
                clock[0] += body["ms"] / 1000
            if action == "dig":
                if transport_overruns:
                    clock[0] = 1000.0 + guard.LAVA_ESCAPE_BUDGET_S + 0.000001
                else:
                    clock[0] += timeout + 0.000001
                raise httpx.ReadTimeout("notch dig exceeded its transport budget")
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "scan_safe_yaws", lambda *args, **kwargs: {"best_yaw": 0.0})
    incident = guard.poll()

    escape = incident["actions"][0]
    assert escape["action"] == "escape_lava"
    digs = [(started, timeout) for action, started, timeout in calls if action == "dig"]
    assert digs and all(0 < timeout <= 1.0 and started + timeout <= 1004.3 for started, timeout in digs)
    if transport_overruns:
        assert escape["result"] == {"ok": False, "in_lava": True, "reason": "time_budget", "partial": True}
        budget_event, = [e for e in events if e["type"] == "guardian.lava_budget_exceeded"]
        assert budget_event["endpoint"] == "dig" and budget_event["budget_s"] == guard.LAVA_ESCAPE_BUDGET_S
        assert any(action == "stop" and timeout == 1.5 for action, _, timeout in calls)
    else:
        assert escape["result"]["ok"] is False
        assert "lava_notch_dig_timeout" in escape["result"]["actions"]
        assert clock[0] < 1000.0 + guard.LAVA_ESCAPE_BUDGET_S
        assert any(action == "stop" and timeout == 0.25 for action, _, timeout in calls)
    guard.bot.status()
    assert calls[-1][2] == 2.0


@pytest.mark.parametrize("landing", [
    "grounded", "after_jump", "after_two_checks", "after_two_edge_checks", "loses_footing",
    "underfoot", "underfoot_loses_footing",
])
def test_r20_lava_bridge_places_against_adjacent_solid_wall_when_underfoot_unsupported(monkeypatch, landing):
    """Reproduces r20_short hy4_hy_6_nether_bastion: when underfoot place fails no_support/blocked_by_self, place side bridge against bank & step onto it."""
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    bridge_state = {"in_lava": True, "on_ground": False, "pos": [249.5, 28.1, 208.5], "placed": set()}
    landing_reads = []
    brake_calls = []
    underfoot = landing.startswith("underfoot")
    loses_footing = landing.endswith("loses_footing")

    class _DeepBankLavaBot:
        def status(self):
            return {
                "pos": list(bridge_state["pos"]),
                "health": 15.0,
                "alive": True,
                "ready": True,
                "in_lava": bridge_state["in_lava"],
                "on_ground": bridge_state["on_ground"],
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                if brake_calls:
                    assert kwargs["method"] == "GET" and 0 < kwargs["timeout_s"] <= 0.35
                    if loses_footing:
                        bridge_state.update(on_ground=False, pos=[251.3, 28.7, 208.5])
                elif "timeout_s" in kwargs and not bridge_state["in_lava"] and not bridge_state["on_ground"]:
                    assert kwargs["allow_reauth"] is False
                    landing_reads.append(kwargs["timeout_s"])
                    if landing not in {"after_two_checks", "after_two_edge_checks"} or len(landing_reads) == 2:
                        bridge_state.update(on_ground=True, pos=[250.5, 29.0, 208.5])
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 60}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in bridge_state["placed"]:
                    return {"name": "cobblestone"}
                # Solid bastion wall 2 blocks East (x=251) while x=249..250 y=28 is lava
                if p[0] == 251 and p[1] in (27, 28):
                    return {"name": "blackstone"}
                if p[1] in (27, 28):
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "place":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in {(249, 28, 208), (249, 27, 208)}:
                    if not underfoot:
                        raise ApiError("no_support", "no solid face in open lava")
                    bridge_state.update(in_lava=False, on_ground=True, pos=[249.2, 29.0, 208.3])
                bridge_state["placed"].add(p)
                return {"ok": True}
            if action == "control" and pl.get("forward") and bridge_state["placed"]:
                after_jump = landing in {"after_jump", "after_two_checks", "after_two_edge_checks"}
                bridge_state.update(
                    in_lava=False, on_ground=not after_jump,
                    pos=[250.2 if landing == "after_two_edge_checks" else 250.5,
                         29.3 if after_jump else 29.0, 208.5],
                )
            if action == "posture":
                assert bridge_state["on_ground"] and 0 < kwargs["timeout_s"] <= 0.35
                brake_calls.append(pl)
            return {"ok": True}

    out_b = CombatEnv(_DeepBankLavaBot()).escape_lava()
    assert out_b["ok"] is (not loses_footing) and out_b["in_lava"] is False
    assert "lava_place_cobblestone" in out_b["actions"]
    assert ("lava_step_bridge" in out_b["actions"]) is (not underfoot)
    assert out_b["actions"][-1] == "lava_bridge_sneak"
    assert brake_calls == [{"sneak": True, "jump": False, "shield": False}]
    if loses_footing:
        assert out_b["reason"] == "lava_landing_unconfirmed"
    else:
        assert out_b["pos_after"] == ([249.2, 29.0, 208.3] if underfoot else [250.5, 29.0, 208.5])
    assert len(landing_reads) == {"after_jump": 1, "after_two_checks": 2, "after_two_edge_checks": 2}.get(landing, 0)
    assert all(0 < timeout <= 0.25 for timeout in landing_reads)


@pytest.mark.parametrize("hit_source", ["fireball", "fire_tick_only", "mob_attack"])
def test_nether_fire_ticks_do_not_replace_ranged_hit_or_start_a_bunker(monkeypatch, hit_source):
    mob = "zombie" if hit_source == "mob_attack" else "blaze"
    attacker = {"id": 42, "name": mob, "dist": 5.1}
    hurt = []
    if hit_source != "fire_tick_only":
        hurt.append({"seq": 1, "type": "hurt", "source": hit_source, "source_entity": attacker})
    hurt.append({"seq": 2, "type": "hurt", "source": "on_fire"})
    retreat_calls = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-79.3, 64, 624.2], "health": 3.0, "food": 17, "dimension": "the_nether",
                    "on_fire": True, "on_ground": True, "alive": True, "ready": True, "nearest_hostile": attacker,
                }
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [row for row in hurt if row["seq"] > since]}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": []}
            return {"ok": True}

    class BunkerSkills:
        def dig_safety_bunker(self, *, urgent):
            assert urgent
            return {"ok": True}

    def retreat(
        status, target, counts, *, ranged=False, prefer_cover=False, allow_stationary_cover=True,
        verify_live_shooters=False,
    ):
        assert allow_stationary_cover == (hit_source != "fireball")
        assert not verify_live_shooters
        retreat_calls.append((target, ranged))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=BunkerSkills())
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()

    action, = incident["actions"]
    if hit_source == "mob_attack":
        assert action["action"] == "emergency_bunker"
        assert retreat_calls == []
    else:
        assert action["action"] == "combat_or_flee"
        assert retreat_calls == [(attacker if hit_source == "fireball" else None, True)]


@pytest.mark.parametrize("respawned", [False, True])
def test_failed_nether_cover_falls_back_to_checked_retreat_only_in_same_life(monkeypatch, respawned):
    from mc_agent.skills.combat_env import CombatEnv

    blaze = {"id": 44, "name": "blaze", "dist": 4.1, "pos": [3190.5, 66.2, -2046.4]}
    status = {
        "pos": [3186.9, 68, -2045.5], "health": 2.1, "food": 17, "dimension": "the_nether",
        "ready": True, "alive": True, "on_ground": True, "on_fire": True, "blocking": True,
        "nearest_hostile": blaze,
    }
    retreats = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "on_fire"}
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [blaze]}
            return {"ok": True}

    def fail_cover(_env, pos, *, other_shooters, allow_stationary):
        assert pos == blaze["pos"]
        assert not other_shooters and allow_stationary
        return {"ok": False, "mode": "natural_cover", "reason": "cover_unconfirmed", "respawned": respawned}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def flee(pos, *, wait_for_ground):
        retreats.append((pos, wait_for_ground))
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", fail_cover)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    retreat = next(action for action in guard.poll()["actions"] if action["action"] == "combat_or_flee")
    assert retreat["result"]["mode"] == ("natural_cover" if respawned else "flee")
    assert retreats == ([] if respawned else [(blaze["pos"], True)])

