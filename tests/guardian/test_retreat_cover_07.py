from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("landing", [
    "clear", "still_near", "no_support", "new_life_during_probe", "scan_timeout", "target_missing",
    "outside_margin", "generation_warning", "shielded", "terrain_unavailable", "new_life_during_clearance_scan",
])
def test_w20_creeper_still_near_after_released_woodcutting_retreat(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [240903.5, 63, 193579.5]
    after_first = [240907.5, 63, 193583.5]
    creeper = {"id": 2346904, "name": "creeper", "pos": [240900.4, 63, 193576.4], "dist": 4.4}
    distance = 6.8 if landing == "outside_margin" else 5.6
    pursuer = {**creeper, "pos": [after_first[0] - distance, *after_first[1:]], "dist": distance}
    shield = {"name": "shield"} if landing == "shielded" else None
    status = {
        "pos": origin, "health": 18.1667, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": creeper, "offhand": shield,
    }
    retreats = []
    pulses = []

    class FieldBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **_kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [], "offhand": shield}
            if action == "get_entities":
                if not retreats:
                    return {"entities": [creeper]}
                if landing == "scan_timeout":
                    raise httpx.ReadTimeout("creeper scan timed out")
                if landing == "target_missing":
                    return {"entities": []}
                if pulses and landing == "new_life_during_clearance_scan":
                    status.update(last_death={"t": 2}, pos=[240900.5, 63.1, 193586.5])
                return {"entities": [pursuer]}
            if action == "get_block":
                if landing == "new_life_during_probe":
                    status.update(last_death={"t": 2}, pos=[240900.5, 63.1, 193586.5])
                if landing == "terrain_unavailable":
                    return {}
                supported = body["pos"][1] == 62 and landing != "no_support"
                return {"name": "stone" if supported else "air"}
            if action == "look" and "yaw" in body:
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                pulses.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx > 0.9 and abs(dz) < 0.1
                end = [240909.9, 63, 193583.5]
                final_distance = 6.2 if landing == "still_near" else 8.0
                pursuer.update(pos=[end[0] - final_distance, *end[1:]], dist=final_distance)
                status.update(pos=end, nearest_hostile={"id": creeper["id"], "name": "creeper", "dist": final_distance})
            if action == "posture":
                status["blocking"] = bool(body.get("shield"))
            if action in {"move_to", "attack"}:
                pytest.fail(f"Unexpected {action} during creeper follow-up")
            return {"ok": True}

    env = CombatEnv(FieldBot())
    checked_flee = env.flee

    def flee(threat_pos, **kwargs):
        retreats.append(threat_pos)
        if len(retreats) == 1:
            status.update(pos=after_first, nearest_hostile={"id": creeper["id"], "name": "creeper", "dist": distance})
            return {"ok": True, "mode": "flee", "moved_dist": 5.66, "pos_after": after_first}
        return checked_flee(threat_pos, **kwargs)

    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", flee)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"],
        "distance": 4.4, **({} if landing == "generation_warning" else {"action_released": True}),
    })

    first = next(action for action in outcome["actions"] if action["action"] == "retreat_from_creeper")
    followups = [action for action in outcome["actions"] if action["action"] == "creeper_followup_retreat"]
    assert first["moved"] == pytest.approx(5.66)
    if landing in {"outside_margin", "generation_warning", "shielded"}:
        assert first["ok"] and not followups and not pulses
        return
    assert not first["ok"] and first["creeper_distance"] == pytest.approx(5.6)
    if landing in {"scan_timeout", "target_missing"}:
        assert first["reason"] == "blast_clearance_unconfirmed" and not followups and not pulses
        return
    followup, = followups
    assert followup["ok"] is (landing == "clear")
    assert bool(pulses) is (landing in {"clear", "still_near", "new_life_during_clearance_scan"})
    if landing == "still_near":
        assert followup["reason"] == "creeper_still_close" and followup["creeper_distance"] == pytest.approx(6.2)
    if landing in {"new_life_during_probe", "new_life_during_clearance_scan"}:
        assert followup["respawned"] is True


@pytest.mark.parametrize("scene", [
    "covered", "no_support", "side_gap", "second_creeper", "placement_unconfirmed", "new_life", "target_moved", "target_missing",
])
def test_w21_hy10_creeper_stall_can_place_supported_cover_in_place(scene):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    origin = [256017.5, 109, 266793.1]
    target = {"id": 2443833, "name": "creeper", "pos": [256020.1, 109, 266793.65], "dist": 2.66}
    if scene == "side_gap":
        target["pos"][2] = origin[2]
    status = {
        "pos": origin, "health": 20, "dimension": "overworld", "last_death": {"t": 1}, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": target,
    }
    blocks = {}
    placements = []

    class SlopeBot:
        def status(self):
            return dict(status)

        def call(self, name, body=None, **_kwargs):
            if name == "status":
                return self.status()
            if name == "inventory":
                return {"items": [{"name": "oak_planks", "count": 3}, {"name": "acacia_log", "count": 2}]}
            if name == "find_blocks":
                return {"blocks": []}
            if name == "get_entities":
                if len(placements) == 2 and scene == "target_missing":
                    return {"entities": []}
                if scene == "second_creeper":
                    extra = {"id": 2443834, "name": "creeper", "pos": [256017.5, 109, 266798.5], "dist": 5.4}
                    return {"entities": [target, extra]}
                return {"entities": [target]}
            if name == "get_block":
                cell = tuple(body["pos"])
                support = cell[1] == 108 and (
                    (cell[0] == 256017 and cell[2] in {266792, 266793})
                    or (cell[0] == 256018 and cell[2] == 266793 and scene != "no_support")
                )
                return {"name": blocks.get(cell, "stone" if support else "air")}
            if name == "place":
                cell = tuple(body["pos"])
                assert body["item"] == "oak_planks"
                placements.append(cell)
                if scene != "placement_unconfirmed":
                    blocks[cell] = body["item"]
                if scene == "new_life":
                    status.update(pos=[256016.5, 117.1, 266801.5], last_death={"t": 2})
                if len(placements) == 2 and scene == "target_moved":
                    target["pos"] = [256017.5, 109, 266795.6]
                return {"ok": True}
            if name in {"move_to", "attack", "dig"} or (name == "control" and body.get("forward")):
                pytest.fail(f"Unexpected {name} during stationary blast cover")
            return {"ok": True}

    outcome = CombatControllerSlot().handle(CombatEnv(SlopeBot()), {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": target["id"], "distance": 2.66,
    })

    first, *remaining = outcome["actions"]
    assert first["action"] == "retreat_from_creeper" and first["reason"] == "no_safe_retreat" and first["moved"] == 0.0
    if scene in {"no_support", "side_gap", "second_creeper"}:
        assert not placements and all(not step["ok"] for step in remaining)
        return
    cover, = remaining
    assert cover["action"] == "place_blast_cover" and cover["ok"] is (scene == "covered")
    column = [(256018, 109, 266793), (256018, 110, 266793)]
    partial = scene in {"new_life", "placement_unconfirmed"}
    assert placements == (column[:1] if partial else column)
    assert cover["placed"] == (0 if scene == "placement_unconfirmed" else len(placements))


@pytest.mark.parametrize("after_first", [
    "close", "pursued", "scan_unavailable", "first_scan_unavailable", "first_scan_empty", "clear", "respawned",
])
def test_guardian_rechecks_a_creeper_after_a_short_unshielded_retreat(after_first):
    pos = [0.5, 64.0, 0.5]
    creeper_pos = [0.5, 64.0, 4.7]
    yaw = 0.0
    last_death = {"t": 10}
    pulses = []

    class CreekBot:
        def call(self, action, body=None, **kwargs):
            nonlocal yaw, last_death
            creeper = {"id": 66, "name": "creeper", "pos": list(creeper_pos), "dist": math.dist(pos, creeper_pos)}
            if action == "status":
                nearest = creeper
                if pulses and after_first in {"first_scan_unavailable", "first_scan_empty"}:
                    nearest = {"id": 67, "name": "zombie", "dist": 4.0}
                return {
                    "pos": list(pos), "health": 20, "food": 20, "on_ground": True, "yaw": yaw,
                    "ready": True, "alive": True, "last_death": last_death, "nearest_hostile": nearest,
                }
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                if len(pulses) == 1 and after_first == "first_scan_unavailable":
                    raise httpx.ReadTimeout("first creeper rescan timed out")
                if len(pulses) == 1 and after_first == "first_scan_empty":
                    return {"entities": []}
                if len(pulses) == 2 and after_first == "scan_unavailable":
                    raise httpx.ReadTimeout("creeper scan timed out")
                return {"entities": [creeper]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                supported = (-1 <= x <= 1 and -4 <= z <= 5) or (-5 <= x <= 1 and -3 <= z <= -1)
                return {"name": "stone" if y == 63 and supported else "air"}
            if action == "look":
                yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                assert body["jump"] is False and body["stop_on"] == ["death"]
                pos[0] -= math.sin(yaw) * 2.3
                pos[2] -= math.cos(yaw) * 2.3
                pulses.append(list(pos))
                if len(pulses) == 1 and after_first in {"close", "pursued", "scan_unavailable"}:
                    creeper_pos[:] = [5.1, 64.0, -1.8]
                elif len(pulses) == 2 and after_first == "pursued":
                    creeper_pos[:] = [pos[0] + 4.5, pos[1], pos[2]]
                elif after_first == "respawned":
                    last_death = {"t": 20}
            if action in {"attack", "move_to"}:
                pytest.fail(f"Unexpected {action} during short retreat")
            return {"ok": True}

    guard = GenerationGuard(CreekBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    reaction, = incident["actions"]
    actions = reaction["result"]["actions"]
    assert actions[0]["action"] == "retreat_from_creeper"
    assert actions[0]["ok"] is (after_first in {"clear", "first_scan_empty"})
    if after_first == "first_scan_unavailable":
        assert actions[0]["reason"] == "blast_clearance_unconfirmed"
    assert pulses[0] == pytest.approx([0.5, 64.0, -1.8])
    if after_first in {"close", "pursued", "scan_unavailable"}:
        assert actions[0]["creeper_distance"] == pytest.approx(4.6)
        assert actions[1]["action"] == "creeper_followup_retreat" and actions[1]["ok"] is (after_first == "close")
        if after_first == "pursued":
            assert actions[1]["reason"] == "creeper_still_close" and actions[1]["creeper_distance"] == pytest.approx(4.5)
        elif after_first == "scan_unavailable":
            assert actions[1]["reason"] == "blast_clearance_unconfirmed"
        assert pulses[1] == pytest.approx([-1.8, 64.0, -1.8])
    else:
        assert len(pulses) == len(actions) == 1


@pytest.mark.parametrize("scene", [
    "overlap", "centered", "unsafe_floor", "still_overlapping", "new_life", "new_life_after_placement",
])
def test_w20_creeper_shaft_seal_clears_its_own_hitbox(monkeypatch, scene):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    start = [229655.8 if scene != "centered" else 229655.5, 79, 237023.5]
    wall = [[229656, 79, 237023], [229656, 80, 237023]]
    creeper = {"id": 2339850, "name": "creeper", "pos": [229660, 79, 237023.5], "dist": 4.2}
    status = {
        "pos": start, "dimension": "overworld", "last_death": {"t": 1}, "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": creeper,
    }
    blocks = {}
    actions = []

    class ShaftBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 6}]}
            if action == "get_entities":
                return {"entities": [creeper]}
            if action == "get_block":
                pos = tuple(body["pos"])
                return {"name": blocks.get(pos, "stone" if pos[1] == 78 and scene != "unsafe_floor" else "air")}
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(body["yaw"])
                assert dx == pytest.approx(-1) and dz == pytest.approx(0)
                actions.append("step")
                if scene not in {"still_overlapping", "new_life"}:
                    status["pos"] = [229655.5, 79, 237023.5]
                if scene == "new_life":
                    status.update(last_death={"t": 2}, pos=[229664.5, 84, 237008.5])
            if action == "place":
                actions.append(body["pos"])
                if status["pos"][0] + 0.3 >= body["pos"][0]:
                    raise ApiError("blocked_by_self", "the bot overlaps the placement")
                blocks[tuple(body["pos"])] = "cobblestone"
                if scene == "new_life_after_placement":
                    status.update(last_death={"t": 2}, pos=[229664.5, 84, 237008.5])
            return {"ok": True}

    def flee(*_args, **_kwargs):
        actions.append("retreat")
        return {"ok": False, "mode": "flee", "moved_dist": 0.0, "reason": "no_safe_retreat"}

    env = CombatEnv(ShaftBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "carved_shaft"})
    monkeypatch.setattr(env, "flee", flee)

    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"],
        "primary_pos": creeper["pos"], "distance": 4.2,
    })

    if scene in {"new_life", "new_life_after_placement"}:
        assert actions == (["step"] if scene == "new_life" else ["step", wall[0]])
        assert outcome["mode"] == "life_unavailable"
        return
    seal = outcome["actions"][0]
    assert seal["action"] == "seal_creeper_shaft"
    assert seal["ok"] is (scene in {"overlap", "centered"})
    assert bool(seal.get("repositioned_for_seal")) is (scene == "overlap")
    if scene in {"overlap", "centered"}:
        expected = ["step", *wall] if scene == "overlap" else wall
        assert actions[:len(expected)] == expected
        assert all(blocks[tuple(pos)] == "cobblestone" for pos in wall)
    else:
        assert actions == (["retreat"] if scene == "unsafe_floor" else ["step", "retreat"])


@pytest.mark.parametrize("pursuer_blocked", [False, True])
@pytest.mark.parametrize(("first_move", "reason"), [
    (1.6, None), (1.4, None), (0.0, "no_safe_retreat"), (1.4, "unsafe_landing"),
])
def test_creeper_followup_uses_fresh_melee_position_to_choose_a_safe_route(monkeypatch, pursuer_blocked, first_move, reason):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    landing = [17054.4, 25, 29963.1]
    creeper = {"id": 42, "name": "creeper", "pos": [17059.76, 25, 29963.1], "dist": 4}
    zombie = {"id": 43, "name": "zombie", "pos": [17049.8, 25, 29963.1], "dist": 4.6,
              "blocked_by_wall": pursuer_blocked}
    status = {"pos": [landing[0] + first_move, 25, 29963.1], "health": 20, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "dimension": "overworld", "nearest_hostile": creeper}
    moves = []
    first_retreat = False

    class CaveBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                hostiles = [creeper, zombie] if first_retreat else [creeper]
                return {"entities": [{**mob, "dist": math.dist(status["pos"], mob["pos"])} for mob in hostiles]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 25 else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, y, z = status["pos"]
                status["pos"] = [x + dx * 2.3, y, z + dz * 2.3]
                moves.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body[key] for key in ("x", "y", "z")]
                moves.append(status["pos"])
            return {"ok": True}

    env = CombatEnv(CaveBot())
    followup = env.flee

    def flee(threat_pos, **kwargs):
        nonlocal first_retreat
        if first_retreat:
            return followup(threat_pos, **kwargs)
        first_retreat = True
        status.update(pos=landing, nearest_hostile=zombie)
        return {
            "ok": first_move >= 1.5 and reason is None, "mode": "flee", "moved_dist": first_move,
            "pos_after": landing, "reason": reason,
        }

    monkeypatch.setattr(env, "flee", flee)
    result = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42,
    })

    if reason:
        retreat, = result["actions"]
        assert not retreat["ok"] and retreat["reason"] == reason
        assert moves == []
        return
    assert result["actions"][1]["action"] == "creeper_followup_retreat" and result["actions"][1]["ok"]
    assert math.dist(moves[-1], creeper["pos"]) > 5.5
    assert (math.dist(moves[0], zombie["pos"]) < 3.0) is pursuer_blocked


@pytest.mark.parametrize("landing", [
    "critical", "sticky_pursuer", "late_respawn", "healthy", "respawned", "scan_timeout", "creeper_returns",
])
def test_critical_creeper_retreat_checks_for_a_surviving_melee_pursuer(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    origin = [19070.9, 64, 40967.5]
    safe_landing = [19077.5, 64, 40967.5]
    creeper = {"id": 40, "name": "creeper", "pos": [19066.8, 64, 40967.5]}
    zombie = {"id": 41, "name": "zombie", "pos": [19072.9, 64, 40967.5], "dist": 4.6}
    skeleton = {"id": 42, "name": "skeleton", "pos": [19077.5, 64, 40975.5]}
    other_creeper = {"id": 43, "name": "creeper", "pos": [19083.7, 64, 40967.5], "blocked_by_wall": True}
    status = {
        "pos": origin, "health": 12.3, "food": 17, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": creeper,
    }
    moves = []

    class NightBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if not moves:
                    return {"entities": [{**creeper, "dist": 4.1}]}
                if landing == "scan_timeout":
                    raise httpx.ReadTimeout("creeper clearance unavailable")
                live_creeper = other_creeper
                if landing == "creeper_returns" and body["radius"] == 12:
                    live_creeper = {**other_creeper, "pos": [19081.5, 64, 40967.5]}
                return {"entities": [zombie, skeleton, live_creeper]}
            return {"ok": True}

    def flee(pos, **options):
        moves.append(pos)
        if len(moves) == 1:
            status.update(pos=safe_landing, health=9 if landing == "healthy" else 2.3333, nearest_hostile=zombie)
            if landing == "respawned":
                status["last_death"] = {"t": 2}
            return {"ok": True, "mode": "flee", "moved_dist": 6.62}
        assert options["other_creepers"] == [other_creeper["pos"]] and options["keep_blast_clearance"]
        assert options["other_shooters"] == options["occluded_shooters"] == [skeleton["pos"]]
        assert not options["allow_second_leg"] and not options["allow_shaft_seal"]
        status["pos"] = [19078.0, 64, 40965.3]
        if landing == "sticky_pursuer":
            status["nearest_hostile"] = {**zombie, "pos": [19074, 64, 40965.3]}
        if landing == "late_respawn":
            status["last_death"] = {"t": 2}
        return {"ok": True, "mode": "flee", "moved_dist": 2.26}

    env = CombatEnv(NightBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", flee)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_type": "creeper", "primary_entity_id": creeper["id"],
    })

    if landing in {"critical", "sticky_pursuer", "late_respawn"}:
        assert moves == [creeper["pos"], zombie["pos"]]
        followup = outcome["actions"][-1]
        assert followup["action"] == "followup_melee_retreat" and followup["entity_id"] == zombie["id"]
        assert followup["ok"] is (landing == "critical")
        if landing == "sticky_pursuer":
            assert followup["reason"] == "melee_clearance_unconfirmed"
        if landing == "late_respawn":
            assert all(step["respawned"] and not step["ok"] for step in outcome["actions"])
    else:
        assert moves == [creeper["pos"]]


def test_creeper_retreat_does_not_report_success_if_the_final_sample_is_dead(monkeypatch):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    creeper = {"id": 66, "name": "creeper", "pos": [4868.3, 22, -7670], "dist": 1.7}
    status = {
        "pos": [4870.0, 22, -7670], "health": 12, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 10},
        "nearest_hostile": creeper,
    }
    retreat_returned = False

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if retreat_returned:
                    status.update(pos=[4858.5, 69.1, -7603.5], health=0, ready=False, alive=False,
                                  last_death={"t": 20}, nearest_hostile=None)
                    return {"entities": []}
                return {"entities": [creeper]}
            return {"ok": True}

        def status(self):
            return dict(status)

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "open_cave"})

    def retreat(*_args, **_kwargs):
        nonlocal retreat_returned
        retreat_returned = True
        status.update(pos=[4866, 22, -7670], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 4.0}

    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"]},
    )

    action, = outcome["actions"]
    assert action == {"action": "retreat_from_creeper", "ok": False, "moved": 4.0, "respawned": True}
    assert outcome["requires_replan"] and outcome["status_after"]["alive"] is False


@pytest.mark.parametrize("after", [
    "close_melee", "far_melee", "shooter", "blocked_shooter", "far_shooter", "healthy", "new_life", "still_wet",
])
def test_low_health_creeper_retreat_keeps_shield_facing_surviving_hostile(monkeypatch, after):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    creeper = {"id": 42, "name": "creeper", "dist": 2.8, "pos": [0.5, 68, 3.3]}
    if after.endswith("shooter"):
        survivor = {"id": 43, "name": "skeleton", "dist": 16.1 if after == "far_shooter" else 11.8}
        if after == "blocked_shooter":
            survivor["blocked_by_wall"] = True
    else:
        survivor = {"id": 43, "name": "zombie", "dist": 5.6 if after == "far_melee" else 5.4}
    status = {"pos": [0.5, 68, 0.5], "health": 16.4, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "blocking": False, "dimension": "overworld", "last_death": {"t": 1},
              "nearest_hostile": creeper}
    looks = []
    posture = []

    class RidgeBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                nearest = status["nearest_hostile"]
                return {"entities": [nearest] if nearest["dist"] <= body["radius"] else []}
            if action == "look":
                looks.append(body["entity_id"])
            if action == "posture":
                posture.append(body)
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def retreat(*_a, **_kw):
        hp = 1.7 if after.endswith("shooter") else 12.0 if after == "healthy" else 5.73
        status.update(pos=[0.5, 68, -6.9], health=hp, nearest_hostile=survivor,
                      last_death={"t": 2 if after == "new_life" else 1}, in_water=after == "still_wet")
        return {"ok": True, "mode": "flee", "moved_dist": 7.4}

    env = CombatEnv(RidgeBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    if after == "still_wet":
        monkeypatch.setattr(env, "escape_deep_water_to_shore", lambda *_a, **_kw: {"ok": False, "in_water": True})
    result = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42,
    })

    face_survivor = after in {"close_melee", "shooter"}
    guard_latched = face_survivor or after == "still_wet"
    assert result["latched_guard"] is guard_latched
    assert looks == ([42, 43] if face_survivor else [42])
    assert status["blocking"] is guard_latched
    if face_survivor:
        assert posture[-1] == {"shield": True, "sneak": False, "jump": False}
        assert result["actions"][-1]["action"] == (
            "guard_ranged_after_creeper_retreat" if after == "shooter" else "guard_melee_after_creeper_retreat"
        )


@pytest.mark.parametrize("shield_in_offhand", [True, False])
def test_creeper_retreat_raises_shield_once_before_terrain_probe(monkeypatch, shield_in_offhand):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    creeper = {"id": 55, "name": "creeper", "pos": [4.3, 64.0, 0.5], "dist": 3.8}
    status = {
        "pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True,
        "on_ground": True, "nearest_hostile": creeper,
    }
    calls = []

    class CreekBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **_kwargs):
            calls.append((action, body))
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}],
                    "offhand": {"name": "shield"} if shield_in_offhand else None,
                    "held": {"name": "iron_pickaxe"},
                }
            if action == "get_entities":
                return {"entities": [status["nearest_hostile"]] if status["nearest_hostile"] else []}
            return {"ok": True}

    env = CombatEnv(CreekBot())

    def probe(_pos):
        calls.append(("probe", None))
        return {"enclosure": "open"}

    def retreat(_pos, **_kwargs):
        status.update(pos=[-6.5, 64.0, 0.5], nearest_hostile=None)
        return {"ok": True, "moved_dist": 7.0}

    monkeypatch.setattr(env, "arena_snapshot", probe)
    monkeypatch.setattr(env, "flee", retreat)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"],
        "primary_pos": creeper["pos"], "distance": 3.8,
    })

    shield_equip = [] if shield_in_offhand else [("equip", {"item": "shield", "slot": "off-hand"})]
    assert [(action, body) for action, body in calls if action in {"equip", "look", "posture", "probe"}] == [
        *shield_equip,
        ("look", {"entity_id": creeper["id"]}),
        ("posture", {"shield": True, "sneak": False, "jump": False}),
        ("probe", None),
    ]
    assert result["mode"] == "creeper_evade"


def test_melee_warning_faces_nearby_creeper_before_raising_shield(monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    zombie = {"id": 11, "name": "zombie", "pos": [1.9, 64, 0.5], "dist": 1.4}
    creeper = {"id": 12, "name": "creeper", "pos": [-2.8, 64, 0.5], "dist": 3.3}
    status = {
        "pos": [0.5, 64, 0.5], "health": 16, "food": 20, "ready": True, "alive": True,
        "on_ground": True, "blocking": False, "nearest_hostile": zombie,
    }
    hostiles = [zombie, creeper]
    faced = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": hostiles}
            if action == "look":
                faced.append(body["entity_id"])
            return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open"})

    def flee(pos, **_kwargs):
        assert pos == creeper["pos"]
        hostiles.clear()
        return {"ok": True, "moved_dist": 6.0}

    monkeypatch.setattr(env, "flee", flee)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"],
        "primary_pos": zombie["pos"], "distance": 1.4,
    })

    assert result["mode"] == "creeper_evade"
    assert faced and set(faced) == {creeper["id"]}


@pytest.mark.parametrize("creeper_in_status", [False, True])
def test_creeper_warning_uses_a_creeper_position_when_scan_only_finds_other_mobs(monkeypatch, creeper_in_status):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    parched = {"id": 4621113, "name": "parched", "dist": 11.8, "pos": [12.3, 65, 0.5]}
    creeper_pos = [-3.2, 65, 0.5]
    status = {
        "pos": [0.5, 65, 0.5], "health": 19, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "nearest_hostile": parched,
    }
    if creeper_in_status:
        status["nearest_hostile"] = {"id": 4623433, "name": "creeper", "dist": 3.7, "pos": [-3.2, 65, 1.5]}
    expected = status["nearest_hostile"]["pos"] if creeper_in_status else creeper_pos
    moves = []

    class NightBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [parched]}
            raise AssertionError(action)

    env = CombatEnv(NightBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open"})

    def flee(threat_pos, **_kwargs):
        moves.append(threat_pos)
        status["nearest_hostile"] = parched
        return {"ok": True, "moved_dist": 3.0}

    monkeypatch.setattr(env, "flee", flee)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 4623433,
        "primary_pos": creeper_pos, "distance": 3.7,
    })

    assert result["mode"] == "creeper_evade"
    assert moves == [expected]

