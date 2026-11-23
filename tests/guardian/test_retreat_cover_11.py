from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("short_only", [False, True])
def test_creeper_retreat_does_not_take_the_only_route_toward_the_blast(monkeypatch, short_only):
    from mc_agent.skills.combat_env import CombatEnv

    status = {"pos": [0.5, 66, 0.5], "on_ground": True, "alive": True, "ready": True, "dimension": "overworld"}

    class RidgeBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            pytest.fail(f"Retreat dispatched {action} toward the creeper")

    def probe(_pos, yaw, travel, **_kwargs):
        # The ridge opens only toward the creeper at +X.
        return (66, False) if math.sin(yaw) < -0.8 and (not short_only or travel <= 1.65) else None

    env = CombatEnv(RidgeBot())
    monkeypatch.setattr(env, "probe_retreat", probe)
    result = env.flee(
        [5.7, 66, 0.5], distance=11.5, allow_shaft_seal=False, wait_for_ground=True,
        other_melee=[[-1.0, 66, 0.5]], avoid_blast=True,
    )

    assert result["reason"] == "no_safe_retreat" and result["moved_dist"] == 0.0


@pytest.mark.parametrize("distance, short_only", [(3.0, False), (7.0, False), (3.0, True)])
def test_creeper_retreat_avoids_a_second_creeper_on_the_escape_heading(monkeypatch, distance, short_only):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [0.5, 66, 0.5]
    primary = {"id": 11, "name": "creeper", "dist": 2.6, "pos": [-2.1, 66, 0.5]}
    second = {"id": 12, "name": "creeper", "dist": distance, "pos": [0.5 + distance, 66, 0.5]}
    status = {
        "pos": origin, "health": 3.85, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "nearest_hostile": primary,
    }
    path = []

    class RespawnBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [{**mob, "dist": math.dist(status["pos"], mob["pos"])} for mob in (primary, second)]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 66 else "air"}
            if action == "look" and "yaw" in body:
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                travel = 1.65 if short_only else 3.0
                x, y, z = status["pos"]
                status["pos"] = [x + travel * dx, y, z + travel * dz]
                path.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body["x"], body["y"], body["z"]]
                path.append(status["pos"])
            return {"ok": True}

    if short_only:
        def probe(_env, _pos, yaw, travel, *, allow_step_down=False):
            if allow_step_down:
                return None
            dx, dz = yaw_to_unit_xz(yaw)
            if travel <= 1.65 and (dx > 0.95 or (dx < -0.3 and dz > 0.8)):
                return 66, False
            return None

        monkeypatch.setattr(CombatEnv, "probe_retreat", probe)
    outcome = CombatControllerSlot().handle(CombatEnv(RespawnBot()), {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 11, "primary_pos": primary["pos"],
    })

    assert outcome["mode"] == "creeper_evade"
    assert path
    assert all(math.dist(p, second["pos"]) >= min(distance, 6.0) - 1e-6 for p in path), path


@pytest.mark.parametrize("enclosure, endpoint", [("open_cave", "dig"), ("carved_shaft", "place")])
@pytest.mark.parametrize("post_timeout", ["ready", "busy", "respawned", "water"])
def test_combat_fortification_timeout_leaves_time_to_retreat(monkeypatch, enclosure, endpoint, post_timeout):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    shooter = {"id": 42, "name": "skeleton", "dist": 9.0, "pos": [9.5, 24, 0.5]}
    status = {
        "pos": [0.5, 24, 0.5], "dimension": "overworld", "last_death": {"t": 1},
        "health": 17, "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    timeline = []
    events = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            if action == "get_entities":
                return {"entities": [shooter]}
            if action == "stop":
                timeline.append(action)
            if action == endpoint:
                timeline.append(action)
                assert 0.0 < kwargs["timeout_s"] <= 3.0
                clock[0] += kwargs["timeout_s"] + 0.001
                if post_timeout == "busy":
                    status["busy"] = endpoint
                if post_timeout == "respawned":
                    status.update(last_death={"t": 2}, health=20)
                if post_timeout == "water":
                    status["in_water"] = True
                raise httpx.ReadTimeout("fortification timed out")
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=MacroSkills(bot))

    def build(mode, **_kwargs):
        assert mode == ("full_seal" if enclosure == "carved_shaft" else "cover_pocket")
        return guard.bot.call(endpoint)

    def flee(*_args, **_kwargs):
        assert guard.bot.active_deadline - clock[0] >= 3.0
        timeline.append("flee")
        return {"ok": True, "mode": "flee", "moved_dist": 2.3}

    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": enclosure})
    monkeypatch.setattr(guard.combat_env, "build_tactical", build)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    outcome = guard._run_subcortical_combat({
        "source": "pre_hurt_ranged", "primary_entity_id": 42, "primary_type": "skeleton", "distance": 9.0,
    })

    assert timeline == ["stop", endpoint, "stop"] + (["flee"] if post_timeout == "ready" else [])
    event, = [event for event in events if event["type"] == "guardian.fortification_budget_exceeded"]
    assert event["endpoint"] == endpoint
    if post_timeout == "ready":
        assert outcome["actions"][0]["reason"] == "fortification_budget"
        assert outcome["actions"][1]["action"] == "flee" and outcome["actions"][1]["ok"]
    else:
        assert outcome["mode"] == ("life_unavailable" if post_timeout == "respawned" else "combat_timeout")
        assert outcome["status_after"] == status


@pytest.mark.parametrize("scene", [
    "covered", "exposed", "close_melee", "shooter_lost", "respawned", "insufficient_time", "unsupported",
    "late_shooter_lost", "terrain_timeout", "step_to_cover",
])
def test_critical_shielded_arrow_interrupt_guards_before_dig_and_checks_cover_after_stall(monkeypatch, scene):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    shooter = {"id": 930084, "name": "skeleton", "dist": 8.1, "pos": [353617.7, 74, 353661.2]}
    status = {
        "pos": [353609.6, 74, 353661.2], "dimension": "overworld", "last_death": {"t": 1},
        "health": 4.2, "food": 17, "ready": True, "alive": True, "on_ground": True,
        "blocking": False, "nearest_hostile": shooter,
    }
    timeline = []

    class CaveBot:
        def call(self, action, body=None, **_kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 5}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                if "flee" in timeline:
                    timeline.append("rescan")
                    if scene == "shooter_lost":
                        return {"entities": [{**shooter, "id": 93}]}
                    if scene == "late_shooter_lost" and timeline.count("rescan") > 1:
                        return {"entities": [{**shooter, "id": 93}]}
                    if scene == "close_melee":
                        zombie = {"id": 92, "name": "zombie", "dist": 2, "pos": [353607.6, 74, 353661.2]}
                        return {"entities": [shooter, zombie]}
                return {"entities": [shooter]}
            if action == "get_block":
                x, y, z = body["pos"]
                support = y == 73 and x == 353609 and z in {353660, 353661} and scene != "unsupported"
                wall = x == 353614 and 74 <= y <= 77 and scene != "exposed"
                if scene == "terrain_timeout":
                    clock[0] += 2.3
                if scene == "step_to_cover":
                    support = y == 73 and x == 353609 and 353658 <= z <= 353662
                    wall = wall and z <= 353660
                return {"name": "stone" if support or wall else "air"}
            if action == "posture":
                status["blocking"] = bool(body.get("shield"))
                if status["blocking"]:
                    timeline.append("shield")
            if action == "move_to":
                assert scene == "step_to_cover"
                timeline.append("move")
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot))
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", lambda *_args, **_kwargs: {"ok": False})

    def build(mode, **_kwargs):
        assert mode == "cover_pocket"
        timeline.append("dig")
        clock[0] += 3.0
        return {"ok": False, "reason": "fortification_budget"}

    def flee(*_args, **_kwargs):
        timeline.append("flee")
        status["health"] = 1.747
        if scene == "respawned":
            status.update(last_death={"t": 2}, health=20)
        if scene == "insufficient_time":
            clock[0] += 3.2
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(guard.combat_env, "build_tactical", build)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    outcome = guard._run_subcortical_combat({
        "source": "tool_interrupted_hurt", "hurt_source": "arrow", "primary_entity_id": shooter["id"],
        "primary_type": "skeleton", "primary_pos": shooter["pos"], "distance": 8.1,
    })

    assert timeline[:2] == ["shield", "dig"]
    if scene in {"respawned", "insufficient_time"}:
        assert "rescan" not in timeline
    else:
        assert "rescan" in timeline
    assert (outcome["mode"] == "natural_cover") is (scene in {"covered", "step_to_cover"})
    assert ("move" in timeline) is (scene == "step_to_cover")
    if scene == "terrain_timeout":
        action = next(action for action in outcome["actions"] if action["action"] == "natural_cover")
        assert action["reason"] == "time_budget"
    if scene != "respawned":
        assert outcome["latched_guard"] and outcome["status_after"]["blocking"]


@pytest.mark.parametrize("scene", [
    "clear", "one_block", "no_support", "new_life", "replaced_shooter", "near_melee", "exposed_landing",
])
def test_w20_close_arrow_warning_can_step_behind_a_nearby_corner_without_blocks(scene):
    origin = [236820.3, 40, 250032.5]
    shooter = {"id": 2393435, "name": "skeleton", "pos": [236822.8, 40, 250032.5], "dist": 2.5}
    status = {
        "pos": origin, "health": 8.3333, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": shooter,
    }
    scans = 0
    moves = []

    class NarrowCaveBot:
        def call(self, action, body=None, **_kwargs):
            nonlocal scans
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "dirt", "count": 1}] if scene == "one_block" else []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_entities":
                scans += 1
                if scene == "new_life" and scans == 3:
                    status.update(last_death={"t": 2}, pos=[236821.5, 40.1, 250030.5])
                if scene == "replaced_shooter" and scans >= 3:
                    return {"entities": [{**shooter, "id": 2393436}]}
                if scene == "near_melee" and scans >= 2:
                    return {"entities": [shooter, {"id": 2393437, "name": "zombie", "dist": 2.0}]}
                return {"entities": [shooter]}
            if action == "get_block":
                x, y, z = body["pos"]
                floor = (x, y) == (236820, 39) and (z == 250032 or (z == 250031 and scene != "no_support"))
                corner = x == 236821 and y in (40, 41) and z == 250031 and not (scene == "exposed_landing" and moves)
                return {"name": "stone" if floor or corner else "air"}
            if action == "move_to":
                moves.append(body)
                status["pos"] = [body["x"], body["y"], body["z"]]
                return {"ok": True}
            if action in {"stop", "posture"} or (action == "control" and body == {"wait": False}):
                return {"ok": True}
            pytest.fail(f"Unexpected {action} during close arrow warning")

    guard = GenerationGuard(NarrowCaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    outcome = guard._run_subcortical_combat({
        "source": "pre_hurt_ranged", "primary_type": "skeleton", "primary_entity_id": shooter["id"], "distance": 2.0,
    })

    first = outcome["actions"][0]
    assert first["action"] == "flee" and first["reason"] == "no_safe_retreat" and first["moved"] == 0.0
    safe_corner = scene in {"clear", "one_block"}
    assert (outcome["mode"] == "natural_cover") is safe_corner
    if safe_corner or scene == "exposed_landing":
        move, = moves
        assert move["x"] == pytest.approx(origin[0]) and move["y"] == 40 and move["z"] == pytest.approx(250031.35)
        cover = next(action for action in outcome["actions"] if action["action"] == "natural_cover")
        assert cover["ok"] is safe_corner
    else:
        assert moves == []
        if scene == "new_life":
            assert outcome["status_after"]["last_death"] == {"t": 2}


@pytest.mark.parametrize("entry", ["poll", "tool_combat"])
@pytest.mark.parametrize("scene", ["clear_step", "no_support", "new_life", "second_melee"])
def test_w19_close_shooter_checks_natural_cover_after_failed_pocket(monkeypatch, entry, scene):
    origin = [184019.1, 109, 189185.7]
    distance = 1.6 if entry == "poll" else 1.1
    health = 1.5 if entry == "poll" else 5.5
    shooter_pos = [origin[0] + math.cos(math.pi / 6) * distance, 109, origin[2] + distance / 2]
    shooter = {"id": 2196696, "name": "skeleton", "dist": distance, "pos": shooter_pos}
    status = {
        "pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "health": health,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    timeline = []

    class CaveBot:
        def call(self, action, body=None, **_kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "dirt", "count": 13}]}
            if action == "get_entities":
                extra = []
                if scene == "second_melee" and "flee" in timeline:
                    extra.append({"id": 2196700, "name": "zombie", "dist": 2.6, "pos": [184017.1, 109, 189184.0]})
                return {"entities": [shooter, *extra]}
            if action == "get_block":
                x, y, z = body["pos"]
                floor_z = 189183 <= z <= 189187 if scene != "no_support" else z == 189185
                support = y == 108 and x <= 184019 and floor_z
                wall = x == 184019 and 109 <= y <= 111 and z == 189186
                return {"name": "stone" if support or wall else "air"}
            if action == "move_to":
                timeline.append("move")
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot))
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", lambda *_args, **_kwargs: {"ok": False})

    def build(mode, **_kwargs):
        assert mode == "cover_pocket"
        timeline.append("pocket")
        return {"ok": False, "reason": "fortification_budget"}

    def flee(*_args, **_kwargs):
        timeline.append("flee")
        if scene == "new_life":
            status.update(last_death={"t": 2}, health=20)
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(guard.combat_env, "build_tactical", build)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    outcome = guard._run_subcortical_combat({
        "source": "pre_hurt_ranged" if entry == "poll" else "tool_interrupted_hurt",
        "primary_entity_id": shooter["id"], "primary_type": "skeleton", "distance": distance,
    })

    assert timeline == ["pocket", "flee"] + (["move"] if scene == "clear_step" else [])
    assert (outcome["mode"] == "natural_cover") is (scene == "clear_step")
    if scene == "clear_step":
        assert any(action["action"] == "natural_cover" and action["ok"] for action in outcome["actions"])
        assert outcome["status_after"]["health"] == health


@pytest.mark.parametrize("entry", ["poll", "tool_combat"])
@pytest.mark.parametrize("scene", [
    "pursuit", "airborne_stall", "different_target", "far", "missing_position", "new_life", "dead", "blocked",
    "critical_unlisted", "critical_unlisted_far", "critical_unlisted_no_shield", "critical_unlisted_new_life",
    "critical_other_target",
])
def test_shield_turns_to_the_same_close_enderman_after_melee_retreat(monkeypatch, entry, scene):
    first = {"id": 895362, "name": "enderman", "dist": 1.8, "pos": [353689.2, 64, 353636.9]}
    status = {
        "pos": [353687.4, 64, 353636.9], "health": 13.7, "dimension": "overworld", "last_death": {"t": 1},
        "ready": True, "alive": True, "blocking": True, "on_ground": True, "food": 20, "nearest_hostile": first,
    }
    aftermath = {"id": first["id"], "name": "enderman", "dist": 2.2}
    follower_pos = [353675.5, 64.6, 353625.3 if scene == "critical_unlisted_far" else 353631.3]
    post_looks = []
    turns = []
    reads_after = []

    class PursuitBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                rows = [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": first}]
                return {"events": rows if kwargs["params"]["since"] == 0 else []}
            if action == "inventory":
                sword = {"name": "diamond_sword", "count": 1}
                return {"items": [sword], "held": sword, "offhand": {"name": "shield", "count": 1}}
            if action == "get_entities":
                if not turns:
                    return {"entities": [first]}
                reads_after.append(action)
                return {"entities": [aftermath if scene == "missing_position" else {**aftermath, "pos": follower_pos}]}
            if action == "look":
                assert "entity_id" not in body
                if turns:
                    post_looks.append(body)
            if action == "posture":
                status["blocking"] = bool(body.get("shield"))
                return {"ok": True, "blocking": status["blocking"]}
            if action == "attack":
                pytest.fail("defending against pursuit must not attack the enderman")
            return {"ok": True}

    guard = GenerationGuard(PursuitBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})

    def flee(*_args, **_kwargs):
        turns.append("retreat")
        status.update(pos=[353675.5, 64.6, 353633.5], health=11.6, on_ground=False)
        status["nearest_hostile"] = aftermath
        if scene.startswith("critical_"):
            status.update(health=2.7, on_ground=True, blocking=False, nearest_hostile=None)
            if scene != "critical_unlisted_no_shield":
                status["offhand"] = {"name": "shield"}
        if scene in {"different_target", "critical_other_target"}:
            status["nearest_hostile"] = {**aftermath, "id": 90}
        if scene == "far":
            status["nearest_hostile"] = {**aftermath, "dist": 8}
        if scene in {"new_life", "critical_unlisted_new_life"}:
            status["last_death"] = {"t": 2}
        if scene == "dead":
            status["health"] = 0
        if scene == "blocked":
            status["nearest_hostile"] = {**aftermath, "blocked_by_wall": True}
        if scene == "airborne_stall":
            return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0}
        return {"ok": True, "mode": "flee", "moved_dist": 11.7, "pos_after": status["pos"]}

    monkeypatch.setattr(guard.combat_env, "flee", flee)
    monkeypatch.setattr(guard, "_defend_or_flee", flee)
    if entry == "poll":
        outcome = guard.poll()
    else:
        outcome = guard._run_subcortical_combat({
            "source": "tool_interrupted_hurt", "hurt_source": "mob_attack", "primary_type": "enderman",
            "primary_entity_id": first["id"], "primary_pos": first["pos"], "distance": 1.8,
        })
    guards = [action for action in outcome["actions"] if action["action"] == "guard_enderman_pursuit"]

    assert turns == ["retreat"]
    if scene in {"pursuit", "airborne_stall", "critical_unlisted"}:
        turn, = post_looks
        assert "entity_id" not in turn and turn["yaw"] == pytest.approx(0) and turn["pitch"] < 0
        assert len(guards) == 1 and guards[0]["ok"] is True
        detail = guards[0]["result"] if entry == "poll" else guards[0]
        assert detail.get("nearest_hostile_missing", False) is (scene == "critical_unlisted")
        assert status["blocking"] is True
        if entry == "tool_combat":
            assert outcome["latched_guard"] is True
    else:
        assert not post_looks and not any(action["ok"] for action in guards)
    should_rescan = scene in {"pursuit", "airborne_stall", "missing_position", "critical_unlisted", "critical_unlisted_far"}
    assert len(reads_after) == int(should_rescan)


@pytest.mark.parametrize("sighting", ["nearest", "scan", "unconfirmed"])
def test_melee_attack_blocks_a_confirmed_creeper_before_starting(sighting):
    target_id = 5325659
    creeper = {"id": target_id, "name": "creeper", "dist": 5.4}
    attacks = []

    class ScoutBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 67, 0.5], "health": 19, "ready": True,
                    "alive": True, "on_ground": True, "dimension": "overworld",
                    "nearest_hostile": creeper if sighting == "nearest" else {"id": 91, "name": "zombie", "dist": 4.0},
                }
            if action == "get_entities":
                return {"entities": [creeper] if sighting == "scan" else [{**creeper, "id": target_id + 1}]}
            if action == "attack":
                attacks.append(body)
                raise httpx.ReadTimeout("attack reply timed out")
            raise AssertionError(action)

    bot = ScoutBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": target_id, "max_hits": 3}))

    if sighting == "unconfirmed":
        with pytest.raises(httpx.ReadTimeout, match="attack reply timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert len(attacks) == 1
    else:
        result = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert result["requires_replan"]
        assert result["error"]["code"] == "unsafe_creeper_attack"
        assert attacks == []


@pytest.mark.parametrize("landing", ["dry", "water", "respawned"])
def test_retreat_waits_for_ground_after_pathfinder_leg(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {
        "pos": [0.5, 66, 0.5], "dimension": "overworld", "on_ground": True,
        "ready": True, "alive": True, "last_death": {"t": 1},
    }
    steps = []

    class KnockbackBot:
        yaw = 0.0

        def status(self):
            if steps == ["pulse", "pathfinder"] and clock[0] >= 1000.15:
                status["on_ground"] = True
                if landing == "water":
                    status["in_water"] = True
                if landing == "respawned":
                    status.update(pos=[2.5, 68, 2.5], last_death={"t": 2})
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 66 else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            if action == "control":
                steps.append("pulse")
                dx, dz = yaw_to_unit_xz(self.yaw)
                status["pos"] = [0.5 + 2 * dx, 66, 0.5 + 2 * dz]
            if action == "move_to":
                steps.append("pathfinder")
                status.update(pos=[body["x"], body["y"] + 0.2, body["z"]], on_ground=False)
            return {"ok": True}

    outcome = CombatEnv(KnockbackBot()).flee([-2.5, 66, 0.5], wait_for_ground=True, allow_shaft_seal=False)

    assert steps == ["pulse", "pathfinder"]
    assert 1000.15 <= clock[0] <= 1000.35
    assert outcome["ok"] is (landing == "dry")
    if landing == "water":
        assert outcome["reason"] == "unsafe_landing"
    if landing == "respawned":
        assert outcome["respawned"] is True and outcome["moved_dist"] == 0.0


@pytest.mark.parametrize("shooter_visible", [True, False])
def test_arrow_retreat_uses_the_live_shooter_when_nearest_hostile_is_a_creeper(monkeypatch, shooter_visible):
    from mc_agent.skills.combat_env import CombatEnv

    creeper = {"id": 77, "name": "creeper", "dist": 4.2, "pos": [4.7, 30, 0.5]}
    skeleton = {"id": 88, "name": "skeleton", "dist": 8.0, "pos": [0.5, 30, -7.5]}
    status = {
        "pos": [0.5, 30, 0.5], "dimension": "overworld", "health": 4.7, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": creeper,
    }
    retreat_targets = []

    class GullyBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                events = [{"seq": 1, "type": "hurt", "source": "arrow"}]
                return {"events": [] if kwargs["params"]["since"] else events, "last_seq": 1}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [creeper, skeleton] if shooter_visible else [creeper]}
            return {"ok": True}

    def flee(_env, threat_pos, **kwargs):
        retreat_targets.append((threat_pos, kwargs.get("other_melee", [])))
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", lambda *_: {"ok": False, "reason": "no_solid_wall"})
    def seek_cover(*_args, **_kwargs):
        pytest.fail("nearby creeper makes stationary cover unsafe")

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(GullyBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll() is not None

    expected = (skeleton["pos"], [creeper["pos"]]) if shooter_visible else (creeper["pos"], [])
    assert retreat_targets == [expected]


@pytest.mark.parametrize("terrain", ["step", "deep_drop", "lava", "low_ceiling"])
def test_grounded_retreat_can_step_down_one_verified_block_in_a_tunnel(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64, 0.5], "dimension": "overworld", "ready": True, "alive": True, "on_ground": True,
    }
    moves = []

    class StairBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                if z != 0 or x < 0 or x > 2:
                    return {"name": "stone"}
                if x == 0:
                    return {"name": "stone" if y <= 63 else "air"}
                floor = 61 if terrain == "deep_drop" else 62
                if floor == y and terrain == "lava":
                    return {"name": "lava"}
                if terrain == "low_ceiling" and y == 65:
                    return {"name": "stone"}
                return {"name": "stone" if y <= floor else "air"}
            if action == "control":
                moves.append(body)
                status["pos"] = [1.3, 63, 0.5]
            return {"ok": True}

    outcome = CombatEnv(StairBot()).flee([-0.4, 64, 0.5], allow_shaft_seal=False, wait_for_ground=True)

    assert outcome["ok"] is (terrain == "step")
    if terrain == "step":
        move, = moves
        assert move["forward"] and not move["jump"] and not move["sprint"]
    else:
        assert moves == []
        assert outcome["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("terrain", ["step", "low_ceiling", "lava", "fell_short"])
def test_cornered_melee_retreat_can_climb_a_short_supported_step(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.65, 61, 0.5], "dimension": "overworld", "ready": True, "alive": True, "on_ground": True,
    }
    moves = []

    class DepressionBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                if z != 0 or x < 0 or x > 2:
                    return {"name": "stone"}
                if terrain == "low_ceiling" and x == 0 and y == 63:
                    return {"name": "stone"}
                if terrain == "lava" and x > 0 and y == 61:
                    return {"name": "lava"}
                return {"name": "stone" if y <= (60 if x == 0 else 61) else "air"}
            if action == "control":
                moves.append(body)
                status["pos"] = [1.4, 62, 0.5] if terrain != "fell_short" else [1.05, 61, 0.5]
            elif action == "move_to":
                pytest.fail("The short climb grants only one checked movement pulse")
            return {"ok": True}

    result = CombatEnv(DepressionBot()).flee(
        [-0.6, 61, 0.5], allow_shaft_seal=False, wait_for_ground=True,
    )

    assert result["ok"] is (terrain == "step")
    if terrain in {"step", "fell_short"}:
        move, = moves
        assert move["forward"] and move["jump"] and not move["sprint"]
    else:
        assert moves == []
        assert result["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("mode", ["cover", "bunker"])
def test_subcortical_recovery_macros_share_the_combat_transport_deadline(monkeypatch, mode):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {"pos": [0.5, 74, 0.5], "dimension": "overworld", "ready": True, "alive": True, "on_ground": True}
    calls = []
    events = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "dig":
                calls.append(kwargs["timeout_s"])
                clock[0] += kwargs["timeout_s"]
                return {"ok": True}
            return {"ok": True}

    class SurvivalRecovery:
        def on_encounter(self, env, _trigger):
            if mode == "cover":
                env.build_tactical("cover_pocket")
            else:
                env.skills.dig_safety_bunker(urgent=True)
            return {"handled": True, "mode": "covered", "actions": [], "status_after": dict(status)}

    def recovery(skills, **_kwargs):
        return skills.bot.call("dig", {"pos": [0, 75, 0]}, timeout_s=30.0)

    monkeypatch.setattr(MacroSkills, "dig_cover_pocket", recovery)
    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", recovery)
    raw_bot = CaveBot()
    skills = MacroSkills(raw_bot)
    guard = GenerationGuard(raw_bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)
    guard.combat_slot.register(SurvivalRecovery())

    outcome = guard._run_subcortical_combat({"source": "pre_hurt_melee"})

    assert outcome["reason"] == "time_budget"
    budget_event, = [event for event in events if event["type"] == "guardian.combat_budget_exceeded"]
    assert budget_event["endpoint"] == "dig"
    assert calls == [7.5]
    assert clock[0] <= 1009.0
    assert guard.combat_env.skills is skills and skills.bot is raw_bot

