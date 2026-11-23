from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("landing", ["near_melee", "near_shooter", "clear", "respawned"])
@pytest.mark.parametrize("encounter", ["surface_melee", "shaft_melee", "shaft_ranged"])
def test_short_retreat_keeps_shield_facing_a_close_melee_enemy(monkeypatch, landing, encounter):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    failed_shaft_seal = encounter != "surface_melee"
    shaft_ranged = encounter == "shaft_ranged"
    mob = "skeleton" if shaft_ranged else "wither_skeleton" if failed_shaft_seal else "husk"
    primary = {"id": 77, "name": mob, "pos": [1.3, 69, 0.5], "dist": 0.8}
    status = {
        "pos": [0.5, 69, 0.5], "health": 20.0 if failed_shaft_seal else 7.0, "food": 17, "ready": True, "alive": True,
        "on_ground": True, "blocking": True, "last_death": {"t": 1}, "nearest_hostile": primary,
    }
    posture_calls = []

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                items = [{"name": "cobblestone", "count": 64}] if failed_shaft_seal else []
                return {"items": items, "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [primary]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 69 else "air"}
            if action in {"look", "posture"}:
                posture_calls.append((action, body))
            if action == "posture":
                status["blocking"] = body.get("shield", False)
            return {"ok": True}

    def short_retreat(*args, **kwargs):
        status.update(pos=[-1.68, 69, 0.5], health=4.84)
        status["nearest_hostile"] = {
            "id": 78, "name": "husk" if shaft_ranged else mob, "dist": 2.3 if landing != "clear" else 4.0,
        }
        if shaft_ranged:
            status["blocking"] = False
        if landing == "near_shooter":
            status["nearest_hostile"].update(name="skeleton", dist=3.4)
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        return {"ok": not failed_shaft_seal, "mode": "flee", "moved_dist": 2.18}

    env = CombatEnv(RetreatBot())
    monkeypatch.setattr(env, "flee", short_retreat)
    if failed_shaft_seal:
        monkeypatch.setattr(env, "arena_snapshot", lambda *args: {"enclosure": "carved_shaft"})
        monkeypatch.setattr(env, "build_tactical", lambda *args, **kwargs: {"ok": False, "reason": "no_safe_seal"})
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_ranged" if shaft_ranged else "pre_hurt_melee", "primary_entity_id": 77, "primary_type": mob},
    )

    if failed_shaft_seal:
        assert {"action": "seal_shaft", "ok": False, "reason": "no_safe_seal"} in outcome["actions"]
        assert {"action": "flee", "ok": False, "moved": 2.18} in outcome["actions"]
    should_latch = landing == "near_melee" or (landing == "near_shooter" and encounter != "shaft_melee")
    assert outcome["latched_guard"] is should_latch
    if should_latch and landing == "near_shooter":
        action = "guard_ranged_after_failed_seal" if shaft_ranged else "guard_ranged_after_melee_retreat"
        assert {"action": action, "ok": True, "entity_id": 78} in outcome["actions"]
    if should_latch:
        assert posture_calls[-2:] == [("look", {"entity_id": 78}), ("posture", {"shield": True, "sneak": False, "jump": False})]
        assert status["blocking"] is True
    else:
        assert posture_calls[-1] == ("posture", {})


@pytest.mark.parametrize("hostile_still_close", [True, False])
def test_seal_during_melee_retreat_reports_cover_and_keeps_shield_if_needed(monkeypatch, hostile_still_close):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    hostile = {"id": 77, "name": "wither_skeleton", "dist": 3.2, "pos": [3532.5, 70, 1796.7]}
    status = {
        "pos": [3532.5, 70, 1793.5], "health": 20, "food": 17, "ready": True, "alive": True,
        "on_ground": True, "blocking": False, "dimension": "the_nether", "nearest_hostile": hostile,
    }

    class FortressBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [hostile]}
            if action == "posture":
                status["blocking"] = body.get("shield", False)
            return {"ok": True}

    env = CombatEnv(FortressBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "open_cave"})

    def seal_while_retreating(*_args, **_kwargs):
        if not hostile_still_close:
            status["nearest_hostile"] = None
        return {"ok": True, "mode": "sealed", "moved_dist": 0.0, "pos_after": status["pos"]}

    monkeypatch.setattr(env, "flee", seal_while_retreating)
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": hostile["id"], "primary_type": hostile["name"]},
    )

    assert outcome["mode"] == "sealed_shaft"
    assert {"action": "seal_shaft", "ok": True} in outcome["actions"]
    assert not any(action["action"] == "flee" for action in outcome["actions"])
    assert outcome["latched_guard"] is hostile_still_close
    assert status["blocking"] is hostile_still_close


@pytest.mark.parametrize("landing", [
    "pursued", "different_mob", "too_far", "behind_wall", "ordinary_retreat", "critical_pursued", "critical_clear",
    "critical_occluded", "critical_other", "critical_airborne", "critical_non_point_blank",
])
def test_melee_retreat_keeps_the_shield_up_when_a_point_blank_attacker_pursues(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    critical = landing.startswith("critical_")
    initial_distance = 2.0 if landing in {"ordinary_retreat", "critical_non_point_blank"} else 1.1 if critical else 0.3
    zombie = {"id": 77, "name": "zombie", "dist": initial_distance}
    status = {
        "pos": [-6996.5, 120, 5825.5], "health": 4.312 if critical else 17.2, "food": 20, "ready": True, "alive": True,
        "dimension": "overworld", "on_ground": True, "blocking": critical, "nearest_hostile": zombie,
    }
    poses = []

    class VillageBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "posture":
                poses.append(body)
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def retreat(*_a, **_kw):
        distance = {"critical_clear": 5.6, "too_far": 3.6}.get(landing, 5.0 if critical else 3.5)
        after = {
            **zombie, "id": 78 if landing in {"different_mob", "critical_other"} else 77,
            "dist": distance, "blocked_by_wall": landing in {"behind_wall", "critical_occluded"},
        }
        status.update(pos=[-6992.6, 120, 5824.5], health=2.248 if critical else 14.41, nearest_hostile=after)
        if landing == "critical_airborne":
            status["on_ground"] = False
        return {"ok": True, "mode": "flee", "moved_dist": 5.18}

    env = CombatEnv(VillageBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_a: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {"source": "pre_hurt_melee", "primary_entity_id": 77})

    should_latch = landing in {"pursued", "critical_pursued"}
    assert outcome["latched_guard"] is should_latch
    assert status["blocking"] is should_latch
    if should_latch:
        assert outcome["actions"][-1] == {"action": "guard_melee_after_retreat", "ok": True, "entity_id": 77}
    else:
        assert poses[-1] == {}


@pytest.mark.parametrize("landing", [
    "pursued", "different_target", "out_of_range", "behind_wall", "no_shield", "no_wither",
    "new_life", "airborne", "water", "not_ready",
])
def test_withered_pursuit_keeps_the_shield_up_through_combat_exit(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    target = {"id": 1084245, "name": "wither_skeleton", "dist": 2.9, "pos": [-373.4, 57, 1742.6]}
    sword = {"name": "iron_sword", "count": 1}
    shield = {"name": "shield", "count": 1}
    status = {
        "pos": [-373.4, 57, 1742.45], "health": 20, "food": 20, "dimension": "the_nether",
        "last_death": {"t": 1}, "ready": True, "alive": True, "on_ground": True,
        "held": sword, "offhand": shield, "blocking": False, "nearest_hostile": target,
    }
    postures = []

    class FortressBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **_kwargs):
            if action == "inventory":
                return {"items": [sword], "held": sword, "offhand": status["offhand"]}
            if action == "get_entities":
                return {"entities": [target]}
            if action == "posture":
                postures.append(body)
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def retreat(*_args, **_kwargs):
        status.update(
            pos=[-373.4, 57, 1740.2], health=16.2, blocking=True,
            nearest_hostile={
                **target,
                "id": 1084246 if landing == "different_target" else target["id"],
                "dist": 3.6 if landing == "out_of_range" else 2.6,
                "blocked_by_wall": landing == "behind_wall",
            },
            effects=[] if landing == "no_wither" else [{"name": "wither", "level": 1, "duration_s": 10}],
            offhand=None if landing == "no_shield" else shield,
            last_death={"t": 2 if landing == "new_life" else 1},
            ready=landing != "not_ready", on_ground=landing != "airborne", in_water=landing == "water",
        )
        return {"ok": True, "mode": "flee", "moved_dist": 2.25, "pos_after": status["pos"]}

    env = CombatEnv(FortressBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": target["id"], "primary_type": target["name"]},
    )

    should_latch = landing == "pursued"
    assert {"action": "flee", "ok": True, "moved": 2.25} in outcome["actions"]
    assert outcome["latched_guard"] is should_latch
    assert status["blocking"] is should_latch
    if should_latch:
        assert {"action": "guard_melee_after_retreat", "ok": True, "entity_id": target["id"]} in outcome["actions"]
    else:
        assert postures[-1] == {}


@pytest.mark.parametrize("second_shooter", [False, True])
def test_unshielded_boundary_retreat_accounts_for_a_second_shooter(second_shooter):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [0.5, 7.0, 0.5]
    primary = {"id": 77, "name": "skeleton", "pos": [8.2, 7.0, 0.5], "dist": 7.7}
    secondary = {"id": 78, "name": "skeleton", "pos": [-7.5, 7.0, 0.5], "dist": 8.0}
    pos = list(origin)
    yaw = 0.0

    class CaveBot:
        def status(self):
            return {"pos": list(pos), "health": 7, "food": 20, "ready": True,
                    "alive": True, "on_ground": True, "nearest_hostile": dict(primary)}

        def call(self, action, body=None, **kwargs):
            nonlocal yaw
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}], "offhand": {"name": "iron_pickaxe"}}
            if action == "get_entities":
                return {"entities": [primary, secondary] if second_shooter else [primary]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 7 else "air"}
            if action == "look" and "yaw" in body:
                yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                ux, uz = yaw_to_unit_xz(yaw)
                pos[0] += 2.5 * ux
                pos[2] += 2.5 * uz
            if action == "move_to":
                pos[:] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    outcome = CombatControllerSlot().handle(
        CombatEnv(CaveBot()), {"source": "boundary_threat", "primary_entity_id": 77, "primary_type": "skeleton"},
    )

    assert outcome["actions"][0]["action"] == "flee"
    assert outcome["actions"][0]["ok"] is True
    if second_shooter:
        assert abs(pos[2] - origin[2]) > 2.0
        assert math.dist(pos, secondary["pos"]) >= secondary["dist"] - 0.3
    else:
        assert pos == pytest.approx([-2.0, 7.0, 0.5])


@pytest.mark.parametrize("incoming_magic", [False, True])
def test_indirect_magic_uses_checked_retreat_without_digging_a_bunker(monkeypatch, incoming_magic):
    hits = [{"seq": 1, "type": "hurt", "source": "magic"}]
    if incoming_magic:
        hits.extend([{"seq": 2, "type": "hurt", "source": "indirect_magic"}, {"seq": 3, "type": "hurt", "source": "magic"}])
    retreats = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [-10333.4, 15.0, -9057.6], "dimension": "overworld", "health": 13.5,
                        "food": 20, "ready": True, "alive": True, "on_ground": True}
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            pytest.fail(f"Unexpected action during magic damage recovery: {action}")

    def checked_flee(threat_pos, *, wait_for_ground):
        retreats.append(threat_pos)
        assert wait_for_ground is True
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", checked_flee)

    incident = guard.poll()

    if incoming_magic:
        assert retreats == [None]
        assert [action["action"] for action in incident["actions"]] == ["combat_or_flee"]
    else:
        assert retreats == [] and incident is None


@pytest.mark.parametrize("witch_visible", [False, True])
def test_magic_damage_with_visible_witch_retreats_from_the_witch(monkeypatch, witch_visible):
    zombie = {"id": 41, "name": "zombie", "dist": 1.9, "pos": [2.4, 71, 0.5]}
    witch = {"id": 42, "name": "witch", "dist": 7.5, "pos": [0.5, 71, -7.0], "blocked_by_wall": not witch_visible}
    status = {
        "pos": [0.5, 71, 0.5], "dimension": "overworld", "health": 16.7, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "nearest_hostile": zombie,
    }
    hits = [{"seq": 1, "type": "hurt"}, {"seq": 2, "type": "hurt", "source": "magic"}]
    retreats = []

    class MagicBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [h for h in hits if h["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [zombie, witch]}
            return {"ok": True}

    guard = GenerationGuard(MagicBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def defend(_status, attacker, _counts, *, ranged=False):
        retreats.append((attacker, ranged))
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    assert guard.poll()["requires_replan"]
    assert retreats == ([(witch, True)] if witch_visible else [(None, False)])


@pytest.mark.parametrize("ground", ["safe", "lava_drop", "slow_probe", "slow_control"])
@pytest.mark.parametrize("bunker_reason", ["unsafe_floor", "no_roof_block"])
def test_bunker_rejection_keeps_an_armed_witch_retreat_on_verified_ground(monkeypatch, ground, bunker_reason):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [17029.1, 87, 29985.6]
    witch = {"id": 255310, "name": "witch", "dist": 9.1, "pos": [17022, 87, 29991.3]}
    status = {
        "pos": origin, "dimension": "overworld", "health": 12.3, "food": 20,
        "alive": True, "ready": True, "on_ground": True, "nearest_hostile": witch,
    }
    moves = []
    stops = []
    yaw = 0.0

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            nonlocal yaw
            if action == "status":
                return dict(status)
            if action == "events":
                hits = [
                    {"seq": 1, "type": "hurt", "source": "magic"},
                    {"seq": 2, "type": "hurt", "source_entity": witch},
                ]
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_block":
                if ground == "slow_probe":
                    clock[0] += min(0.36, kwargs["timeout_s"])
                x, y, z = body["pos"]
                if y == 86:
                    supported = ground in {"safe", "slow_control"} or (x, z) == (17029, 29985)
                    return {"name": "stone" if supported else "lava"}
                return {"name": "air"}
            if action == "look":
                yaw = body["yaw"]
                return {"ok": True}
            if action == "control":
                moves.append(body)
                if ground == "slow_control":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("control response exceeded the retreat deadline")
                dx, dz = yaw_to_unit_xz(yaw)
                status["pos"] = [origin[0] + 2.1 * dx, origin[1], origin[2] + 2.1 * dz]
                return {"ok": True}
            if action == "stop":
                stops.append((clock[0], kwargs["timeout_s"]))
                return {"ok": True}
            pytest.fail(f"Unexpected action after an unsafe bunker: {action}")

    guard = GenerationGuard(LedgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", lambda *_args, **_kwargs: {
        "ok": False, "reason": bunker_reason, "floor_pos": [17029, 83, 29985], "floor_block": "lava",
    })
    monkeypatch.setattr(MacroSkills, "defend_or_flee", lambda *_args, **_kwargs: pytest.fail("unchecked retreat"))

    incident = guard.poll()

    bunker, retreat = incident["actions"]
    assert bunker["result"]["reason"] == bunker_reason
    assert retreat["action"] == "combat_or_flee" and retreat["ok"] is (ground == "safe")
    if ground.startswith("slow_"):
        assert retreat["result"] == {"ok": False, "reason": "time_budget", "partial": True}
        assert stops == [(1008.0, 1.5)]
        assert clock[0] == 1008.0
    else:
        assert retreat["result"].get("reason") == (None if ground == "safe" else "no_safe_retreat")
        assert stops == []
    assert len(moves) == (1 if ground in {"safe", "slow_control"} else 0)


@pytest.mark.parametrize("scene", ["exposed", "occluded", "out_of_reach", "own_shaft", "shielded"])
def test_w20_overworld_critical_melee_retreat_precedes_bunker(monkeypatch, scene):
    pos = [229652.5, 78, 237019.3]
    distance = 4.1 if scene == "out_of_reach" else 2.3
    zombie = {
        "id": 2352226, "name": "zombie", "dist": distance, "pos": [pos[0] + distance, *pos[1:]],
        "blocked_by_wall": scene == "occluded",
    }
    status = {
        "pos": pos, "health": 0.2, "food": 19, "dimension": "overworld", "last_death": {"t": 1},
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": zombie,
    }
    actions = []

    class QuarryBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "events":
                return {"events": [] if kwargs["params"]["since"] else [
                    {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie},
                ]}
            if endpoint == "inventory":
                return {
                    "items": [{"name": "cobblestone", "count": 6}],
                    "offhand": {"name": "shield"} if scene == "shielded" else None,
                }
            pytest.fail(f"Unexpected action under melee pressure: {endpoint}")

    def dig_bunker(_skills, *, urgent):
        assert urgent
        actions.append("bunker")
        return {"ok": True, "inside_shaft": True, "roof_placed": True}

    def flee(*_args, **_kwargs):
        actions.append("retreat")
        status.update(pos=[229648.5, 78, 237019.3], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 4.0, "pos_after": status["pos"]}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", dig_bunker)
    guard = GenerationGuard(QuarryBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_inside_own_shaft", lambda: scene == "own_shaft")
    monkeypatch.setattr(guard.combat_env, "flee", flee)

    incident = guard.poll()

    assert actions == (["retreat"] if scene == "exposed" else ["bunker"])
    action, = incident["actions"]
    assert action["action"] == ("combat_or_flee" if scene == "exposed" else "emergency_bunker") and action["ok"]


@pytest.mark.parametrize("outcome", [
    "reported_dead", "api_dead", "dead_snapshot", "respawned", "not_ready", "sealed_after_respawn", "same_life",
])
def test_poll_only_retreats_after_a_bunker_failure_in_the_same_life(monkeypatch, outcome):
    status = {
        "pos": [27953.3, 65, 34988.4], "health": 3.33, "food": 17, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "last_death": {"t": 1},
    }
    calls = []

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 6}]}
            raise AssertionError(action)

    def dig_bunker(_skills, *, urgent):
        assert urgent
        calls.append("dig")
        if outcome == "api_dead":
            raise ApiError("dead", "stopped because of death")
        if outcome == "dead_snapshot":
            status.update(alive=False, health=0)
        elif outcome == "not_ready":
            status["ready"] = False
        elif outcome in {"respawned", "sealed_after_respawn"}:
            status.update(last_death={"t": 2}, health=20)
        if outcome == "sealed_after_respawn":
            return {"ok": True, "inside_shaft": True, "roof_placed": True}
        return {"ok": False, "reason": "dig_failed", "error": "dead" if outcome == "reported_dead" else "interrupted"}

    def retreat(*_args, **_kwargs):
        calls.append("retreat")
        return {"ok": True, "mode": "flee", "moved_dist": 2.01}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", dig_bunker)
    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)

    incident = guard.poll()

    if outcome == "same_life":
        assert calls == ["dig", "retreat"]
        assert [step["action"] for step in incident["actions"]] == ["emergency_bunker", "combat_or_flee"]
    else:
        assert calls == ["dig"]
        assert "death_or_connection_changed" in incident["reasons"]
        bunker, = incident["actions"]
        assert bunker["action"] == "emergency_bunker" and not bunker["ok"]


@pytest.mark.parametrize(("dimension", "nearest", "health"), [
    ("the_nether", "attacker", 6.1), ("the_nether", "other_mob", 6.1), ("the_nether", "scan_timeout", 6.1),
    ("overworld", "attacker", 6.1), ("overworld", "other_mob", 6.1), ("overworld", "scan_timeout", 6.1),
    ("overworld", "unknown_attacker", 6.1), ("overworld", "unknown_attacker", 14.0),
    ("overworld", "other_mob", 14.0), ("overworld", "scan_timeout", 14.0), ("overworld", "distant_mob", 14.0),
])
def test_melee_retreat_resolves_an_attacker_without_position(monkeypatch, nearest, dimension, health):
    attacker = {"id": 4580454, "name": "enderman" if dimension == "the_nether" else "zombie", "dist": 1.1}
    attacker_pos = [3517.2, 60, 1781.9]
    blaze = {"id": 82, "name": "blaze", "dist": 0.9, "pos": [3515.2, 60, 1781.9]}
    status = {
        "pos": [3516.1, 60, 1781.9], "health": health, "food": 17, "dimension": dimension,
        "ready": True, "alive": True, "on_ground": True, "blocking": dimension == "the_nether",
        "nearest_hostile": attacker if nearest in {"attacker", "unknown_attacker"} else blaze,
    }
    if nearest == "distant_mob":
        status["nearest_hostile"] = {**attacker, "dist": 14.0}
    retreats = []
    scans = []

    class AmbushBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                if nearest not in {"unknown_attacker", "distant_mob"}:
                    hit["source_entity"] = attacker
                hits = [hit]
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"} if dimension == "the_nether" else None}
            if action == "get_entities":
                scans.append(kwargs["timeout_s"])
                if nearest == "scan_timeout":
                    raise httpx.ReadTimeout("attacker lookup timed out")
                return {"entities": [blaze, {**attacker, "pos": attacker_pos}]}
            return {"ok": True}

    guard = GenerationGuard(AmbushBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def flee(threat_pos, *, wait_for_ground, use_nearest_hostile=True):
        assert wait_for_ground
        assert use_nearest_hostile is (nearest != "scan_timeout")
        assert scans == ([] if nearest == "distant_mob" else [0.6])
        retreats.append(threat_pos)
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(guard.combat_env, "flee", flee)
    guard.poll()
    assert retreats == [None if nearest in {"scan_timeout", "distant_mob"} else attacker_pos]


@pytest.mark.parametrize("shooters", ["visible", "timeout", "melee_only"])
@pytest.mark.parametrize("melee_source", ["mob_attack", "spear"])
def test_low_health_mixed_hurt_retreat_accounts_for_the_shooter(monkeypatch, shooters, melee_source):
    melee = {"id": 44, "name": "zombie", "dist": 2.0, "pos": [-2, 72, 0]}
    shooter = {"id": 45, "name": "skeleton", "dist": 8.0, "pos": [8, 72, 0]}
    status = {
        "pos": [0, 72, 0], "dimension": "overworld", "health": 8.56, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "nearest_hostile": {key: melee[key] for key in ("id", "name", "dist")},
    }
    hits = [{"seq": 2, "type": "hurt", "source": melee_source}]
    if shooters != "melee_only":
        hits.insert(0, {"seq": 1, "type": "hurt", "source": "arrow"})
    scans = []
    retreats = []

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                scans.append(body)
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                if shooters == "timeout":
                    raise httpx.ReadTimeout("hostile scan timeout")
                return {"entities": [melee, shooter]}
            return {"ok": True}

    def retreat(primary_pos, *, wait_for_ground, other_shooters=()):
        assert wait_for_ground
        retreats.append((primary_pos, list(other_shooters)))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    incident = guard.poll()

    assert [action["action"] for action in incident["actions"]] == ["combat_or_flee"]
    assert scans == ([] if shooters == "melee_only" else [{"radius": 16, "types": ["hostile"]}])
    expected = (melee["pos"], [shooter["pos"]]) if shooters == "visible" else (None, [])
    assert retreats == [expected]


@pytest.mark.parametrize("stance", ["unshielded", "shielded", "failed_cover"])
@pytest.mark.parametrize("scan", ["visible", "timeout"])
def test_low_health_surface_retreat_accounts_for_nearby_shooters(monkeypatch, scan, stance):
    from mc_agent.skills.combat_env import CombatEnv

    shielded = stance != "unshielded"
    failed_cover = stance == "failed_cover"
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 100.0)
    primary = {"id": 4882325, "name": "pillager", "pos": [-10959.1, 72, -11030.2], "dist": 7.3}
    secondary = {"id": 4883019, "name": "pillager", "pos": [-10965.5, 72, -11050.4], "dist": 13.5}
    status = {
        "pos": [-10962.4, 72, -11038], "health": 7.9 if failed_cover else 2.39 if shielded else 5.33, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": shielded,
        "nearest_hostile": primary,
    }
    plans = []

    class RoadBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": primary}
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"} if shielded else None}
            if action == "look":
                assert shielded
                return {"ok": True}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                if scan == "timeout":
                    raise httpx.ReadTimeout("shooter scan expired")
                return {"entities": [primary, secondary]}
            raise AssertionError(action)

    def cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters=False):
        assert verify_live_shooters is (not shielded)
        if failed_cover:
            assert not allow_stationary
        plans.append(("cover", pos, list(other_shooters)))
        return None

    def flee(pos, *, wait_for_ground, other_shooters=()):
        assert wait_for_ground
        plans.append(("flee", pos, list(other_shooters)))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(RoadBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if failed_cover:
        guard._last_dig_cover_fail_at = 88.5
        guard._last_dig_cover_block = (-10961, 72, -11038)
    monkeypatch.setattr(MacroSkills, "dig_cover_pocket", lambda _skills: {"ok": False, "reason": "no_solid_wall"})
    monkeypatch.setattr(guard.combat_env, "probe_retreat", lambda *_args: None)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    monkeypatch.setattr(guard.combat_env, "flee", flee)

    expected_actions = ["combat_or_flee"] if failed_cover else ["dig_cover", "combat_or_flee"]
    if shielded:
        expected_actions = ["face_attacker", *expected_actions, "face_attacker_after_retreat"]
    assert [a["action"] for a in guard.poll()["actions"]] == expected_actions
    extra = [secondary["pos"]] if scan == "visible" else []
    assert plans == [("cover", primary["pos"], extra), ("flee", primary["pos"], extra)]

