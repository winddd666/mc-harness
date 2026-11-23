from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("cover", ["confirmed", "exposed_edge", "wall_removed", "second_shooter"])
def test_arrow_poll_rechecks_natural_cover_before_remaining_in_place(monkeypatch, cover):
    from mc_agent.skills.combat_env import CombatEnv

    z = 0.08 if cover == "exposed_edge" else 0.5
    pos = [0.5, 71, z]
    shooter = {"id": 41, "name": "skeleton", "dist": 8.0, "pos": [8.5, 71, z]}
    flank = {"id": 42, "name": "skeleton", "dist": 6.0, "pos": [0.5, 71, z - 6.0]}
    status = {
        "pos": pos, "dimension": "overworld", "health": 5.833332, "food": 17,
        "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 41, "name": "skeleton", "dist": 8.0},
    }
    rescans = []
    retreats = []

    class ArrowBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [
                    {"seq": seq, "type": "hurt", "source": "arrow"}
                    for seq in (1, 2, 3) if seq > kwargs["params"]["since"]
                ]}
            if action == "inventory":
                return {"items": [{"name": "dark_oak_log", "count": 12}], "offhand": None}
            if action == "get_entities":
                if kwargs["timeout_s"] == 0.4:
                    rescans.append(action)
                    if cover == "second_shooter":
                        return {"entities": [shooter, flank]}
                return {"entities": [shooter]}
            if action == "get_block":
                x, y, block_z = body["pos"]
                wall_removed = cover == "wall_removed" and bool(rescans)
                if (x, block_z) == (2, 0) and y in (71, 72) and not wall_removed:
                    return {"name": "stone"}
                return {"name": "air"}
            pytest.fail(f"Unexpected action: {action}")

    def flee(_env, threat, **_kwargs):
        retreats.append(threat)
        return {"ok": False, "mode": "flee", "reason": "no_movement", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", lambda *_args: {"ok": False, "reason": "time_budget"})
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(ArrowBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    assert [action["action"] for action in incident["actions"]] == ["dig_cover", "combat_or_flee"]
    assert rescans == ([] if cover == "exposed_edge" else ["get_entities"])
    assert retreats == ([] if cover == "confirmed" else [shooter["pos"]])
    defense = incident["actions"][-1]
    assert defense["result"]["mode"] == ("natural_cover" if cover == "confirmed" else "flee")
    assert defense["ok"] is (cover == "confirmed")
    assert (guard._ranged_cover_active_until > guard.last_reaction) is (cover == "confirmed")


@pytest.mark.parametrize("action", ["control", "move_to"])
@pytest.mark.parametrize("solid_exit", [True, False])
@pytest.mark.parametrize("dimension", ["the_nether", "overworld"])
def test_explicit_flee_checks_support_before_each_motion(action, solid_exit, dimension):
    movements = []

    class RetreatBot:
        def call(self, name, body=None, **kwargs):
            if name == "status":
                return {"pos": [0.5, 36, 0.5], "dimension": dimension, "ready": True, "alive": True, "on_ground": True}
            if name == "get_block":
                x, y, z = body["pos"]
                if y == 35:
                    if solid_exit or (x, z) == (0, 0):
                        return {"name": "stone"}
                    return {"name": "lava" if dimension == "the_nether" else "air"}
                return {"name": "air"}
            movements.append((name, body))
            return {"ok": True}

    body = {"x": 22.5, "z": 0.5, "range": 2.0, "stop_on": ["death"]}
    if action == "control":
        body = {"forward": True, "sprint": True, "jump": True, "yaw": -1.5708, "ms": 1200, "stop_on": ["death"]}
    bot = RetreatBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(bot.call(action, body))))
    assert result["ok"] is solid_exit
    if solid_exit:
        (_, move), = movements
        if action == "control":
            assert move["ms"] == 450 and move["jump"] is False
        else:
            assert (move["x"], move["y"], move["z"]) == (6.5, 36, 0.5)
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert movements == []


@pytest.mark.parametrize("terrain", ["short", "unsupported", "lava", "new_life"])
def test_w20_hy10_flee_checks_a_short_supported_prefix_before_rejecting_the_exit(terrain):
    origin = [228965.5, 63, 228625.5]
    status = {
        "pos": origin, "health": 11, "dimension": "overworld", "last_death": {"t": 1},
        "ready": True, "alive": True, "on_ground": True,
    }
    writes = []
    events = []

    class RidgeBot:
        def status(self):
            return self.call("status", method="GET")

        def call(self, name, body=None, **_kwargs):
            if name == "status":
                return dict(status)
            if name == "get_entities":
                return {"entities": []}
            if name == "get_block":
                _, y, z = body["pos"]
                if z <= 228623 and terrain == "new_life":
                    status.update(last_death={"t": 2}, pos=[228975.5, 68, 228640.5])
                if terrain == "lava" and y == 63 and z == 228624:
                    return {"name": "lava"}
                edge = 228625 if terrain == "unsupported" else 228622
                return {"name": "stone" if y == 62 and z >= edge else "air"}
            if name == "control" and body.get("forward"):
                assert body["yaw"] == pytest.approx(0.0)
                writes.append((name, body))
                status["pos"] = [origin[0], 63, origin[2] - 0.7]
            if name == "move_to":
                writes.append((name, body))
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = RidgeBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(skills.flee_hostiles(distance=30))))

    assert result["ok"] is (terrain == "short")
    if terrain == "short":
        (action, pulse), (navigation, target) = writes
        assert action == "control" and navigation == "move_to"
        assert pulse["ms"] <= 220 and not pulse["sprint"] and not pulse["jump"]
        assert target["y"] == 63 and target["z"] == pytest.approx(228623.15)
        assert result["pos_after"] == status["pos"] and result["moved_dist"] == pytest.approx(2.4)
        assert [event["action"] for event in events if event["type"] == "guardian.flee_exit_shortened"] == ["control", "move_to"]
    else:
        assert not writes
        assert result["error"]["code"] == ("respawned" if terrain == "new_life" else "unsafe_landing")


@pytest.mark.parametrize("landing", ["respawned", "airborne", "water"])
def test_blocked_flee_preserves_the_checked_retreat_observation(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv

    shooter = {"id": 44, "name": "skeleton", "dist": 6.0, "pos": [0.5, 69, 6.5]}
    origin = [0.5, 69, 0.5]
    spawn = [-79.5, 63.1, 75.5]
    fallback_pos = [2.5, 68.9, 0.7]
    status = {"pos": origin, "dimension": "overworld", "health": 0.8, "blocking": True,
              "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter}
    attempts = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            pytest.fail(f"Unexpected action during blocked flee: {action}")

    def flee(_env, target, **_kwargs):
        assert target == shooter["pos"]
        attempts.append(target)
        if landing == "respawned":
            return {"ok": False, "mode": "respawned", "respawned": True, "pos_after": spawn,
                    "moved_dist": 0.0, "reason": "unsafe_landing"}
        return {
            "ok": False, "mode": "flee", "moved_dist": 2.19, "pos_after": fallback_pos,
            "reason": "unsafe_landing", "on_ground": False, "in_water": landing == "water", "in_lava": False,
        }

    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    outcome = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    assert attempts == [shooter["pos"]]
    assert outcome["ok"] is False and outcome["requires_replan"] is True
    assert outcome["pos_before"] == origin
    if landing == "respawned":
        assert outcome["mode"] == "respawned" and outcome["respawned"] is True
        assert outcome["pos_after"] == spawn
    else:
        assert outcome["error"]["code"] == "unsafe_landing" and outcome["mode"] == "flee"
        assert outcome["pos_after"] == fallback_pos and outcome["moved_dist"] == 2.19
        assert outcome["on_ground"] is False and outcome["in_water"] is (landing == "water")


@pytest.mark.parametrize("landing", ["grounded", "airborne", "water", "respawned"])
def test_blocked_melee_flee_rechecks_landing_after_a_retreat_pulse(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv

    origin = [136237.7, 69, 141207.3]
    airborne = [136242.3, 69.6, 141210.5]
    grounded = [airborne[0], 69, airborne[2]]
    spawn = [136216.5, 74, 141188.5]
    spider = {"id": 2039171, "name": "spider", "dist": 3.0, "pos": [origin[0] - 3, 69, origin[2]]}
    status = {"pos": origin, "dimension": "overworld", "health": 18, "ready": True, "alive": True,
              "on_ground": True, "last_death": {"t": 1}, "nearest_hostile": spider}
    movements = []
    post_move_reads = 0

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            nonlocal post_move_reads
            if action == "status":
                if not movements:
                    return dict(status)
                post_move_reads += 1
                if post_move_reads == 1 or landing == "airborne":
                    return {**status, "pos": airborne, "on_ground": False}
                if landing == "respawned":
                    return {**status, "pos": spawn, "last_death": {"t": 2}}
                return {**status, "pos": grounded, "in_water": landing == "water"}
            if action == "look":
                return {"ok": True}
            if action == "control":
                movements.append(body)
                return {"ok": True}
            pytest.fail(f"unexpected action during retreat: {action}")

    def probe(_env, pos, yaw, distance, **kwargs):
        assert pos == origin and distance == 3.5
        return (69, False) if math.isclose(yaw, -math.pi / 2) else None

    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(CombatEnv, "probe_retreat", probe)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("control", {"forward": True, "yaw": 0.0, "stop_on": ["death"]}),
    )))

    assert len(movements) == 1 and post_move_reads >= 2
    assert result["ok"] is (landing == "grounded") and result["requires_replan"]
    if landing == "respawned":
        assert result["respawned"] and result["moved_dist"] == 0.0 and result["pos_after"] == spawn
    else:
        assert result["moved_dist"] == 5.6
        assert result["pos_after"] == (airborne if landing == "airborne" else grounded)
    if landing in {"airborne", "water"}:
        assert result["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize("landing", ["still_close", "second_creeper", "clear", "scan_timeout", "respawned"])
def test_blocked_flee_checks_creeper_clearance_after_safe_step(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv

    origin = [378989.0, 92, 318967.5]
    end = [378989.6, 92, 318968.1]
    creeper = {"name": "creeper", "id": 1024313, "pos": [378985.2, 92, 318967.5], "dist": 3.8}
    status = {"pos": origin, "ready": True, "alive": True, "dimension": "overworld",
              "on_ground": True, "nearest_hostile": creeper, "last_death": {"t": 1}}

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                if landing == "scan_timeout":
                    raise httpx.ReadTimeout("creeper scan timed out")
                if landing == "second_creeper":
                    return {"entities": [{"name": "creeper", "id": 1024314, "dist": 3.0,
                                          "pos": [end[0], end[1], end[2] + 3]}]}
                return {"entities": [creeper] if landing == "still_close" else []}
            pytest.fail(f"Unexpected action during blocked flee: {action}")

    def flee(_env, target, **kwargs):
        assert target == creeper["pos"] and kwargs["keep_blast_clearance"]
        status.update(pos=end, nearest_hostile=creeper if landing == "still_close" else None)
        if landing == "respawned":
            status.update(last_death={"t": 2})
        return {"ok": True, "mode": "flee", "moved_dist": 0.85, "pos_after": end}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 378990.0, "z": 318979, "stop_on": ["death"]}),
    )))

    assert result["ok"] is (landing == "clear")
    assert result["requires_replan"] and result["pos_after"] == end
    if landing == "respawned":
        assert result["respawned"] and result["moved_dist"] == 0.0
    else:
        assert result["moved_dist"] == 0.85 and result["pos_before"] == origin
    if landing in {"still_close", "second_creeper"}:
        assert result["error"]["code"] == "creeper_still_close"
        assert 0 < result["creeper_distance"] <= 5.5
    if landing == "scan_timeout":
        assert result["error"]["code"] == "blast_clearance_unconfirmed"


@pytest.mark.parametrize("blocking, landing", [
    (False, "covered"), (False, "exposed"), (False, "new_life"), (False, "shooter_gone"), (False, "healthy"),
    (False, "cover_moved"), (False, "cover_stationary"),
    (True, "covered"), (True, "exposed"), (True, "new_life"), (True, "shooter_gone"), (True, "near_melee"),
])
def test_blocked_flee_checks_cover_after_a_critical_landing(monkeypatch, blocking, landing):
    from mc_agent.skills.combat_env import CombatEnv

    shooter = {"id": 44, "name": "skeleton", "dist": 5.5, "pos": [0.5, 77, 6.0]}
    origin = [0.5, 77, 0.5]
    landing_pos = [0.5, 77, -2.0]
    failed_cover_pos = [-2.1, 76.2, -2.2]
    status = {"pos": origin, "dimension": "overworld", "health": 1.55 if blocking else 8.5,
              "blocking": blocking, "on_ground": True,
              "ready": True, "alive": True, "last_death": {"t": 1}, "nearest_hostile": shooter}
    checks = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                entities = [shooter]
                if status["pos"] == landing_pos and landing == "near_melee":
                    entities.append({"id": 45, "name": "zombie", "dist": 5.4})
                return {"entities": entities}
            if action == "look" and blocking:
                return {"ok": True}
            pytest.fail(f"Unexpected action during retreat: {action}")

    def flee(_env, target, **_kwargs):
        assert target == shooter["pos"]
        checks.append("flee")
        health_after = {"healthy": 8.5}.get(landing, 1.55 if blocking else 4.5)
        shooter_dist = 4.5 if landing == "near_melee" else 9.4 if blocking else 8.0
        status.update(pos=landing_pos, health=health_after,
                      nearest_hostile=None if landing == "shooter_gone" else {**shooter, "dist": shooter_dist},
                      last_death={"t": 2 if landing == "new_life" else 1})
        return {"ok": True, "mode": "flee", "moved_dist": 2.5, "pos_after": landing_pos}

    def cover(_env, target, *, other_shooters, allow_stationary):
        assert target == shooter["pos"] and not other_shooters and not allow_stationary
        if status["pos"] == origin:
            checks.append("initial_cover")
            return None
        checks.append("cover")
        if landing == "covered":
            return {"ok": True, "mode": "natural_cover", "pos_after": [-1.7, 77, -2.0]}
        if landing in {"cover_moved", "cover_stationary"}:
            status["pos"] = failed_cover_pos if landing == "cover_moved" else landing_pos
            return {"ok": False, "mode": "natural_cover", "reason": "cover_unconfirmed", "pos_after": status["pos"]}
        return None

    monkeypatch.setattr(CombatEnv, "flee", flee)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    if blocking:
        monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
        guard._last_ranged_hurt_at = 999.0
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    expected_checks = ["initial_cover", "flee"] if blocking else ["flee"]
    if landing in {"covered", "exposed", "cover_moved", "cover_stationary"}:
        expected_checks.append("cover")
    assert checks == expected_checks
    assert result["mode"] == {
        "covered": "natural_cover", "cover_moved": "natural_cover", "new_life": "respawned",
    }.get(landing, "flee")
    assert result["ok"] is (landing not in {"new_life", "cover_moved"}) and result["requires_replan"]
    if landing == "cover_moved":
        assert result["pos_after"] == failed_cover_pos and result["reason"] == "cover_unconfirmed"


@pytest.mark.parametrize("terrain", ["cliff", "same_column", "missing_target", "clear", "new_life", "moved", "expired"])
def test_attack_rechecks_footing_near_a_recent_failed_retreat(monkeypatch, terrain):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [0.5, 9, 0.5]
    hostile = {"id": 42, "name": "zombie", "pos": [5.5, 9, 0.5], "dist": 5.0}
    status = {"pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": hostile,
              "health": 8, "food": 20, "ready": True, "alive": True, "on_ground": True}
    attacks = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_entities":
                return {"entities": []}
            if action == "get_block":
                x, y, _ = body["pos"]
                return {"name": "stone" if y == 8 and (terrain == "clear" or x <= 1) else "air"}
            if action == "attack":
                attacks.append(body["entity_id"])
                return {"ok": True, "hits": 1}
            pytest.fail(f"Unexpected action during attack: {action}")

    monkeypatch.setattr(CombatEnv, "probe_retreat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(CombatEnv, "flee", lambda *_args, **_kwargs: {
        "ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0, "pos_after": origin,
    })
    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    flee = guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 5.5, "z": 0.5, "stop_on": ["death"]}),
    ))
    assert json.loads(flee)["error"]["code"] == "unsafe_landing"

    clock[0] += 11.0 if terrain == "expired" else 4.8
    if terrain == "same_column":
        hostile["pos"] = [0.5, -22, 0.5]
    elif terrain == "missing_target":
        hostile.pop("pos")
    elif terrain == "new_life":
        status["last_death"] = {"t": 2}
    elif terrain == "moved":
        status["pos"] = [4.5, 9, 0.5]
    attack = json.loads(guard.run_tool_with_reflex("mc_attack", lambda: json.dumps(
        bot.call("attack", {"entity_id": 42, "max_hits": 8}),
    )))

    safe = terrain in {"clear", "new_life", "moved", "expired"}
    assert attacks == ([42] if safe else [])
    assert attack["ok"] is safe
    if not safe:
        assert attack["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize("pressure", ["arrow", "stale_arrow", "melee_and_arrow", "exposed"])
def test_unsafe_overworld_flee_checks_cover_when_critical_arrows_bypass_shield(monkeypatch, pressure):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    skeleton = {"id": 44, "name": "skeleton", "dist": 2.0, "pos": [0.5, 73, 2.5]}
    pillager = {"id": 45, "name": "pillager", "dist": 7.8, "pos": [-7.3, 73, 0.5]}
    status = {
        "pos": [0.5, 73, 0.5], "dimension": "overworld", "health": 2.6, "blocking": True,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": skeleton,
    }
    checks = []

    class CliffBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                return {"entities": [skeleton, pillager]}
            raise AssertionError(action)

    def seek_cover(_env, pos, *, other_shooters, allow_stationary):
        checks.append("cover")
        assert pos == skeleton["pos"] and other_shooters == [pillager["pos"]]
        assert allow_stationary is False
        if pressure == "exposed":
            return None
        return {"ok": True, "mode": "natural_cover", "moved_dist": 2.2, "pos_after": [2.7, 73, 0.5]}

    def flee(_env, pos, **kwargs):
        checks.append("flee")
        assert kwargs.get("other_shooters", []) == ([pillager["pos"]] if pressure == "exposed" else [])
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = CliffBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_ranged_hurt_at = 991.0 if pressure == "stale_arrow" else 999.0
    if pressure == "melee_and_arrow":
        guard._last_melee_hurt_at = 999.0
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    assert result["requires_replan"]
    if pressure == "arrow":
        assert result["mode"] == "natural_cover"
        assert checks == ["cover"]
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert checks == (["cover", "flee"] if pressure == "exposed" else ["flee"])


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("failure", ["timeout", "death", "failed_respawn"])
def test_guardian_wall_recovery_respects_deadline_and_death(entry, failure, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    events = []
    attempts = []
    status = {"pos": [0.5, 35.0, 0.5], "health": 18, "food": 20, "dimension": "the_nether",
              "ready": True, "alive": True, "on_ground": True}

    class WallBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": "in_wall"}]}
            attempts.append((action, kwargs["timeout_s"]))
            if action == "dig":
                if failure == "timeout":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("wall dig reached its deadline")
                if failure == "failed_respawn":
                    status.update(pos=[-152.5, 71, -382.5], health=20, last_death={"t": 2})
                    return {"ok": False, "error": {"code": "unsafe_landing"}, "pos_after": status["pos"]}
                status.update(health=0, ready=False, alive=False)
            return {"ok": True}

    monkeypatch.setattr(MacroSkills, "unstick", lambda self: self.bot.call("dig", {}, timeout_s=15))
    guard = GenerationGuard(WallBot(), EventSink(events.append), checkpoint=lambda: None)
    expected_action = "unstick_in_wall"
    if entry == "boundary":
        assert guard.stabilize_after_tools() is None
        expected_action = "boundary_unstick_wall"
    else:
        assert guard.poll() is not None

    completed, = [e for e in events if e["type"] == "guardian.action.completed"]
    assert completed["action"] == expected_action
    assert completed["ok"] is False
    if failure == "timeout":
        assert completed["result"]["reason"] == "time_budget"
        assert attempts == [("dig", 6.0), ("stop", 1.5)]
    else:
        assert completed["result"]["respawned"] is True
        assert attempts == [("dig", 6.0)]


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("posture_state", ["normal", "stale", "blocked"])
def test_bug057_hot_floor_crouches_early_without_dropping_shield(entry, posture_state, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    seq = [1]
    calls = []
    saved_sneak = posture_state == "stale"
    status = {
        "pos": [724.3, 68, 636.7], "health": 16.5, "food": 17, "ready": True, "alive": True,
        "on_ground": True, "blocking": True, "sneaking": False, "dimension": "the_nether",
        "nearest_hostile": {"id": 42, "name": "wither_skeleton", "dist": 2.0},
    }

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            nonlocal saved_sneak
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                since = kwargs["params"]["since"]
                hit = {"seq": seq[0], "type": "hurt", "source": "hot_floor"}
                return {"events": [hit] if since < seq[0] else []}
            if action == "get_block":
                return {"name": "magma_block" if body["pos"][1] < 68 else "air"}
            if action == "posture":
                calls.append(body)
                if body["sneak"] != saved_sneak:
                    saved_sneak = body["sneak"]
                    status["sneaking"] = body["sneak"] and posture_state != "blocked"
                return {"sneaking": status["sneaking"]}
            raise AssertionError(action)

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if entry == "poll":
        incident = guard.poll()
        assert incident["requires_replan"] is (posture_state == "blocked")
        assert incident["reasons"] == ["hot_floor"]
    else:
        note = guard.stabilize_after_tools()
        assert ("已潜行避伤" in (note or "")) is (posture_state != "blocked")
    expected_sneaks = [True] if posture_state == "normal" else [True, False, True]
    assert calls == [{"sneak": sneak, "shield": True, "jump": False} for sneak in expected_sneaks]

    seq[0] += 1
    clock[0] += 4.0
    if entry == "poll":
        assert guard.poll() is None
    else:
        assert guard.stabilize_after_tools() is None
    assert len(calls) == len(expected_sneaks)

    if posture_state == "blocked":
        status["pos"][0] += 1.0
        seq[0] += 1
        clock[0] += 0.5
        if entry == "poll":
            assert guard.poll()["requires_replan"] is True
        else:
            assert guard.stabilize_after_tools() is None
        assert len(calls) == 2 * len(expected_sneaks)


@pytest.mark.parametrize("interrupted_error", [False, True])
def test_close_creeper_preempts_movement_that_ignores_ordinary_hurt(monkeypatch, interrupted_error):
    import threading

    stopped = threading.Event()
    creeper = {"id": 44, "name": "creeper", "dist": 4.0, "pos": [0.5, 64, 4.5]}
    status = {
        "pos": [0.5, 64, 0.5], "health": 20, "alive": True, "ready": True, "on_ground": True,
        "nearest_hostile": creeper,
    }
    calls = []

    class MovingBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "move_to":
                calls.append("move_to")
                assert body["stop_on"] == ["death"]
                assert stopped.wait(2.0)
                if interrupted_error:
                    raise ApiError("interrupted", "movement stopped")
            if action == "stop":
                calls.append("stop")
                stopped.set()
            return {"ok": True}

    bot = MovingBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def handle(_env, trigger):
        assert trigger["source"] == "pre_hurt_creeper" and trigger["primary_entity_id"] == 44
        calls.append("creeper_evade")
        return {"handled": True, "mode": "creeper_evade", "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", handle)
    result = json.loads(guard.run_tool_with_reflex(
        "mc_move_to", lambda: json.dumps(bot.call("move_to", {"x": 10, "z": 10, "stop_on": ["death"]})),
    ))

    assert result["preempted_by_reflex"] and result["reflex_mode"] == "creeper_evade"
    assert calls == ["move_to", "stop", "creeper_evade"]


@pytest.mark.parametrize("request_kind", [
    "mixed", "coordinates", "entity", "retreat", "wide_range", "other_floor", "behind_wall", "other_entity", "missing_fix",
])
def test_w19_move_rejects_a_confirmed_creeper_destination_before_dispatch(request_kind):
    from mc_agent.harness.mc_tools import McToolset

    origin = [210790.5, 99, 151019.8]
    creeper = {"name": "creeper", "id": 2197772, "pos": [210799.1, 99, 151015.3], "dist": 9.7}
    target = {"x": 210799, "y": 99, "z": 151015}
    move_args = {**target, "entity_id": creeper["id"], "range": 2.5}
    if request_kind == "coordinates":
        move_args.pop("entity_id")
    elif request_kind == "entity":
        move_args = {"entity_id": creeper["id"], "range": 2.5}
    elif request_kind == "retreat":
        move_args.update(x=210781, z=151023)
    elif request_kind == "wide_range":
        move_args["range"] = 8
    elif request_kind == "other_floor":
        move_args["y"] = 104
    elif request_kind == "other_entity":
        move_args = {"entity_id": 2197773, "range": 2.5}

    nearest = {key: value for key, value in creeper.items() if key != "pos"}
    if request_kind == "behind_wall":
        creeper["blocked_by_wall"] = True
    status = {"pos": origin, "health": 20, "ready": True, "alive": True, "on_ground": True, "nearest_hostile": nearest}
    reads = []
    writes = []
    events = []

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "status":
            reads.append(endpoint)
            result = status
        elif endpoint == "get_entities":
            reads.append(endpoint)
            result = {"entities": [] if request_kind == "missing_fix" else [creeper]}
        elif endpoint == "move_to":
            writes.append(json.loads(request.content))
            result = {"pos": origin}
        else:
            pytest.fail(f"Unexpected movement endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot)
        guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None)
        result = json.loads(guard.run_tool_with_reflex("mc_move_to", lambda: toolset.mc_move_to(**move_args)))

    blocked = request_kind in {"mixed", "coordinates", "entity"}
    assert len(writes) == int(not blocked)
    assert result["ok"] is not blocked
    assert reads.count("status") == 1
    denials = [event for event in events if event["type"] == "guardian.creeper_move_blocked"]
    if blocked:
        assert result["requires_replan"] and result["error"]["code"] == "unsafe_creeper_move"
        assert result["error"]["entity_id"] == creeper["id"]
        assert len(denials) == 1 and denials[0]["entity_id"] == creeper["id"]
    else:
        assert denials == []
        if request_kind in {"retreat", "wide_range", "other_floor", "behind_wall", "missing_fix"}:
            assert "entity_id" not in writes[0]
        else:
            assert writes[0]["entity_id"] == 2197773

