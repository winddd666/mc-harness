from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize(("health", "distance", "blocked", "retreat_first"), [
    (1.33, 2.8, False, True),
    (1.33, 2.8, True, False),
    (1.33, 4.0, False, False),
    (6.0, 2.8, False, False),
])
def test_surface_bunker_yields_to_retreat_when_lethal_melee_is_exposed(monkeypatch, health, distance, blocked, retreat_first):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    threat = {"id": 42, "name": "zombie", "dist": distance, "pos": [0.5 + distance, 76, 0.5],
              "blocked_by_wall": blocked}
    status = {"pos": [0.5, 76, 0.5], "health": health, "food": 17, "ready": True, "alive": True,
              "on_ground": True, "nearest_hostile": threat, "dimension": "overworld"}
    attempts = []

    class SurfaceBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "dirt", "count": 6}]}
            if action == "get_entities":
                return {"entities": [threat]}
            return {"ok": True}

    class BunkerSkills:
        def dig_safety_bunker(self, *, urgent):
            assert urgent
            attempts.append("bunker")
            return {"ok": True}

    def checked_flee(threat_pos, *, distance, urgent, wait_for_ground):
        assert threat_pos == threat["pos"] and distance == 14.0
        assert urgent and wait_for_ground
        attempts.append("retreat")
        status.update(pos=[-4.0, 76, 0.5], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 4.5}

    env = CombatEnv(SurfaceBot(), skills=BunkerSkills())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", checked_flee)
    result = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie", "distance": distance},
    )

    assert attempts == (["retreat"] if retreat_first else ["bunker"])
    assert result["mode"] == ("cover_or_flee" if retreat_first else "bunker_sealed")


@pytest.mark.parametrize("mob, distance, blocked, retreat_first", [
    ("zombie", 2.9, False, True),
    ("cave_spider", 2.5, False, True),
    ("zombie", 2.9, True, False),
    ("zombie", 4.4, False, False),
    ("skeleton", 2.9, False, False),
])
def test_exposed_cave_melee_retreats_before_digging_a_cover_pocket(monkeypatch, mob, distance, blocked, retreat_first):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    threat = {"id": 42, "name": mob, "dist": distance, "pos": [0.5 + distance, -39, 0.5],
              "blocked_by_wall": blocked}
    status = {
        "pos": [0.5, -39, 0.5], "health": 17, "ready": True, "alive": True,
        "on_ground": True, "nearest_hostile": threat, "dimension": "overworld",
    }
    attempts = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "get_entities":
                return {"entities": [threat]}
            if endpoint == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            return {"ok": True}

    def build(mode):
        attempts.append(mode)
        return {"ok": True, "mode": "cover_pocket"}

    def retreat(threat_pos, *, distance, urgent, wait_for_ground):
        assert threat_pos == threat["pos"] and urgent and wait_for_ground
        attempts.append("flee")
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    env = CombatEnv(CaveBot(), skills=object())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "build_tactical", build)
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged" if mob == "skeleton" else "pre_hurt_melee",
        "primary_entity_id": 42, "primary_type": mob, "distance": distance,
    })

    assert attempts == (["flee"] if retreat_first else ["cover_pocket"])
    assert outcome["mode"] == ("cover_or_flee" if retreat_first else "cover_pocket")


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("terrain", [
    "safe", "drop", "target_gone", "blocked_by_wall", "stalled", "knockback", "wet_landing", "respawned_after_step",
    "pursuer_closes", "out_of_reach", "different_nearest",
])
@pytest.mark.parametrize("shaft", [False, True])
def test_unarmed_retreat_follows_up_when_same_melee_attacker_is_still_close(monkeypatch, terrain, complete, shaft):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    threat = {"id": 42, "name": "zombie", "dist": 1.2, "pos": [1.7, 69, 0.5]}
    status = {"pos": [0.5, 69, 0.5], "health": 14, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "nearest_hostile": threat, "last_death": {"t": 1}}
    pulses = []
    distance = 0.9 if shaft else 2.6

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}] if shaft else []}
            if action == "get_entities":
                if body["radius"] == 5:
                    assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                    nearby = {
                        **threat, "pos": [-1.7 + distance, 69, 0.5], "dist": distance,
                        "blocked_by_wall": terrain == "blocked_by_wall",
                    }
                    return {"entities": [] if terrain == "target_gone" else [nearby]}
                return {"entities": [threat]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] == 68 and terrain != "drop" else "air"}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                pulses.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx < 0 and dz == pytest.approx(0)
                if terrain != "stalled":
                    displacement = -0.7 if terrain == "knockback" else 0.7
                    status["pos"] = [-1.7 + displacement * dx, 69, 0.5 + displacement * dz]
                    end_distance = {
                        "pursuer_closes": distance - 0.2, "out_of_reach": 4.2, "different_nearest": 0.5,
                    }.get(terrain, distance + displacement)
                    status["nearest_hostile"] = {
                        "id": 56 if terrain == "different_nearest" else 42, "name": "zombie", "dist": end_distance,
                    }
                if terrain == "wet_landing":
                    status["in_water"] = True
                if terrain == "respawned_after_step":
                    status["last_death"] = {"t": 2}
            return {"ok": True}

    def initial_retreat(_threat_pos, **kwargs):
        status.update(
            pos=[-1.7, 69, 0.5], health=10.333333 if shaft else 8.3,
            nearest_hostile={"id": 42, "name": "zombie", "dist": distance},
        )
        return {"ok": complete, "mode": "flee", "moved_dist": 2.2 if complete else 1.39}

    env = CombatEnv(RetreatBot())
    monkeypatch.setattr(env, "flee", initial_retreat)
    if shaft:
        monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "carved_shaft"})
        monkeypatch.setattr(env, "build_tactical", lambda *_args, **_kwargs: {"ok": False, "reason": "place_failed"})
    outcome = CombatControllerSlot().handle(env, {"source": "pre_hurt_melee", "primary_entity_id": 42})

    followups = [a for a in outcome["actions"] if a["action"] == "followup_melee_retreat"]
    assert outcome["mode"] == ("flee" if shaft else "cover_or_flee")
    expected_step = terrain not in {"drop", "target_gone", "blocked_by_wall"}
    assert pulses == ([{"forward": True, "ms": 220, "stop_on": ["death"]}] if expected_step else [])
    if terrain in {"target_gone", "blocked_by_wall"}:
        assert not followups
        return
    followup, = followups
    assert followup["entity_id"] == 42
    assert followup["ok"] is (terrain in {"safe", "out_of_reach", "different_nearest"})
    if terrain in {"safe", "pursuer_closes", "out_of_reach", "different_nearest"}:
        assert followup["moved_dist"] == pytest.approx(0.7)
        if terrain == "different_nearest":
            assert "target_distance_after" not in followup
        else:
            assert followup["target_distance_before"] == distance
            assert followup["target_distance_after"] == status["nearest_hostile"]["dist"]
            assert followup["target_in_melee_range"] is (terrain != "out_of_reach")
        if terrain == "pursuer_closes":
            assert followup["reason"] == "melee_clearance_unconfirmed"


@pytest.mark.parametrize("landing", [
    "stable", "settled_after_slide", "unsafe_after_slide", "still_sliding", "large_slide",
    "different_floor", "respawned", "deadline",
])
def test_short_melee_retreat_reprobes_after_a_small_same_life_slide(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {"pos": [0.5, 64, 0.5], "on_ground": True, "ready": True, "alive": True,
              "dimension": "overworld", "last_death": {"t": 1}, "health": 2.333}
    reads = 0
    rescanned = False
    moves = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            nonlocal reads, rescanned
            if action == "status":
                reads += 1
                if reads == 1 and landing != "stable":
                    status["pos"] = [0.9, 63 if landing == "different_floor" else 64, 0.5]
                    if landing == "large_slide":
                        status["pos"] = [1.2, 64, 0.5]
                    if landing == "respawned":
                        status["last_death"] = {"t": 2}
                    if landing == "deadline":
                        clock[0] += 0.7
                if reads == 2 and landing == "still_sliding":
                    status["pos"] = [1.13, 64, 0.5]
                return dict(status)
            if action == "get_block":
                rescanned |= reads > 0
                assert kwargs["timeout_s"] <= 0.35
                name = "water" if reads and landing == "unsafe_after_slide" else "stone"
                return {"name": name if body["pos"][1] == 63 else "air"}
            if action == "look":
                status["yaw"] = body["yaw"]
                moves.append(action)
                return {"ok": True}
            if action == "control":
                dx, dz = yaw_to_unit_xz(status["yaw"])
                x, y, z = status["pos"]
                status["pos"] = [x + 0.7 * dx, y, z + 0.7 * dz]
                moves.append(action)
                return {"ok": True}
            pytest.fail(f"Unexpected retreat action: {action}")

    outcome = CombatEnv(BankBot()).step_away_from_melee(dict(status), [2.1, 64, 0.5], deadline=1001.5)

    reached = outcome["ok"]
    assert reached is (landing in {"stable", "settled_after_slide"})
    assert rescanned is (landing in {"settled_after_slide", "unsafe_after_slide", "still_sliding"})
    assert moves == (["look", "control"] if reached else [])


@pytest.mark.parametrize("moved, remaining, same_target, hazard, expected", [
    (1.97, 1.6, True, None, True),
    (1.97, 2.0, True, None, False),
    (3.0, 1.6, True, None, False),
    (1.97, 1.6, False, None, False),
    (0.81, 1.0, True, None, True),
    (0.81, 1.0, False, None, False),
    (0.81, 1.0, True, "unsafe_landing", False),
    (0.0, 1.0, True, "no_safe_retreat", False),
])
def test_healthy_unarmed_bot_follows_up_short_retreat_when_the_attacker_gains_ground(
    monkeypatch, moved, remaining, same_target, hazard, expected,
):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    partial = moved < 1.5
    origin = [381015.51, 69, 374036.3] if partial else [-6270.3, -40, -6565.9]
    distance = 2.5 if partial else 1.9
    hostile = {"id": 42, "name": "spider" if partial else "zombie", "dist": distance,
               "pos": [origin[0] + distance, *origin[1:]]}
    status = {
        "pos": origin, "health": 20 if partial else 17, "food": 20, "alive": True, "ready": True,
        "on_ground": True, "dimension": "overworld", "nearest_hostile": hostile, "is_day": partial,
    }
    steps = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 63}]}
            if action == "get_entities":
                return {"entities": [status["nearest_hostile"]]}
            return {"ok": True}

    def retreat(_pos, **_kwargs):
        landing = [origin[0] - moved, *origin[1:]]
        survivor = {**hostile, "id": 42 if same_target else 43, "dist": remaining,
                    "pos": [landing[0] + remaining, *landing[1:]]}
        status.update(pos=landing, nearest_hostile=survivor)
        if partial:
            status["health"] = 19
        return {"ok": not partial, "mode": "flee", "moved_dist": moved,
                **({"reason": hazard} if hazard else {})}

    def verified_step(landing, threat_pos, *, deadline, target_id):
        assert target_id == 42
        assert landing["pos"] == status["pos"] and threat_pos == status["nearest_hostile"]["pos"]
        steps.append(threat_pos)
        return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface" if partial else "open_cave"})
    monkeypatch.setattr(env, "flee", retreat)
    monkeypatch.setattr(env, "step_away_from_melee", verified_step)
    outcome = CombatControllerSlot().handle(env, {"source": "pre_hurt_melee", "primary_entity_id": 42})

    assert steps == ([status["nearest_hostile"]["pos"]] if expected else [])
    assert any(a["action"] == "followup_melee_retreat" for a in outcome["actions"]) is expected


@pytest.mark.parametrize("landing", [
    "cleared", "open_cave", "pursued", "unsupported", "new_life_during_probe", "too_far", "behind_wall",
    "not_critical", "near_creeper",
])
def test_w20_ob2_critical_melee_pursuit_can_continue_after_a_long_retreat(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    after_first = [233459.5, 64, 246384.5]
    zombie = {"id": 2339720, "name": "zombie", "dist": 3.2, "pos": [233449.3, 64, 246384.5]}
    distance = 9.0 if landing == "too_far" else 6.3
    pursuer = {**zombie, "pos": [after_first[0] - distance, *after_first[1:]], "dist": distance,
               "blocked_by_wall": landing == "behind_wall"}
    status = {
        "pos": [233452.5, 64, 246384.5], "dimension": "overworld", "last_death": {"t": 1},
        "health": 2.7, "food": 19, "ready": True, "alive": True, "on_ground": True, "is_day": False,
        "nearest_hostile": zombie, "yaw": 0.0,
    }
    retreats = []
    pulses = []
    meals = []

    class FieldBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **_kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}] if landing in {"pursued", "cleared"} else []}
            if action == "get_entities":
                if not retreats:
                    return {"entities": [zombie]}
                creeper = {"id": 2340123, "name": "creeper", "pos": [233462.5, 64, 246384.5], "dist": 3.0}
                return {"entities": [pursuer, creeper] if landing == "near_creeper" else [pursuer]}
            if action == "get_block":
                if landing == "new_life_during_probe":
                    status.update(pos=[233451.5, 68.1, 246405.5], last_death={"t": 2})
                supported = body["pos"][1] == 63 and landing != "unsupported"
                return {"name": "stone" if supported else "air"}
            if action == "look" and "yaw" in body:
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                pulses.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx > 0.9 and abs(dz) < 0.1
                status.update(pos=[233461.9, 64, 246384.5], nearest_hostile={
                    "id": zombie["id"], "name": "zombie", "dist": 7.1 if landing == "pursued" else 8.7,
                })
            if action == "use_item":
                meals.append(body["item"])
            return {"ok": True}

    env = CombatEnv(FieldBot(), post_combat_food=True)
    checked_flee = env.flee

    def flee(threat_pos, **kwargs):
        retreats.append(threat_pos)
        if len(retreats) == 1:
            status.update(pos=after_first, health=2.1 if landing == "not_critical" else 1.16666555,
                          nearest_hostile={"id": zombie["id"], "name": "zombie", "dist": 6.3})
            return {"ok": True, "mode": "flee", "moved_dist": 7.0, "pos_after": after_first}
        return checked_flee(threat_pos, **kwargs)

    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave" if landing == "open_cave" else "surface"})
    monkeypatch.setattr(env, "flee", flee)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_entity_id": zombie["id"], "primary_type": "zombie", "distance": 3.2,
    })

    followups = [a for a in outcome["actions"] if a["action"] == "followup_melee_retreat"]
    if landing in {"too_far", "behind_wall", "not_critical", "near_creeper"}:
        assert retreats == [zombie["pos"]] and not followups and not pulses
        return
    followup, = followups
    assert retreats == [zombie["pos"], pursuer["pos"]]
    if landing in {"cleared", "open_cave", "pursued"}:
        pulse, = pulses
        assert pulse["sprint"] and pulse["stop_on"] == ["death"]
        assert followup["target_distance_before"] == pytest.approx(6.3)
        assert followup["target_distance_after"] == (7.1 if landing == "pursued" else 8.7)
    else:
        assert not pulses
    assert followup["ok"] is (landing in {"cleared", "open_cave"})
    assert meals == (["bread"] if landing == "cleared" else [])
    if landing == "pursued":
        assert followup["reason"] == "melee_still_close"
    if landing == "new_life_during_probe":
        assert followup["respawned"] is True


@pytest.mark.parametrize("pursuit", [
    "clear", "visible_shooter", "occluded_shooter", "corner_shooter", "slow_corner_shooter", "drifted_into_fire",
    "blocked", "lava", "out_of_range", "respawned", "time_budget",
])
def test_unarmed_night_retreat_rechecks_a_surviving_melee_pursuer(monkeypatch, pursuit):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    landing = [23043.5, 72, 9938.5]
    zombie = {"id": 257948, "name": "zombie", "dist": 3.4, "pos": [23034.1, 72, 9932.5]}
    distance = 9.2 if pursuit == "out_of_range" else 7.4
    survivor = {**zombie, "dist": distance, "pos": [landing[0] - distance, *landing[1:]],
                "blocked_by_wall": pursuit == "blocked"}
    shooter = {
        "id": 257949, "name": "skeleton", "dist": 4.2, "pos": [landing[0] + 4.2, *landing[1:]],
        "blocked_by_wall": pursuit in {"occluded_shooter", "corner_shooter", "slow_corner_shooter", "drifted_into_fire"},
    }
    wall = set()
    if shooter["blocked_by_wall"]:
        shooter["pos"] = [landing[0] + 3, landing[1], landing[2] + 2.5]
        shooter["dist"] = math.dist(shooter["pos"], landing)
        wall = {(23044, 9939), (23045, 9940)}
        if pursuit == "occluded_shooter":
            wall.update((x, 9939) for x in range(23045, 23049))
    status = {
        "pos": [23037.5, 72, 9932.5], "health": 20, "food": 20, "is_day": False,
        "on_ground": True, "ready": True, "alive": True, "nearest_hostile": zombie,
    }
    plans = []
    steps = []
    yaw = 0.0

    class FieldBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal yaw
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_entities":
                if not plans:
                    return {"entities": [zombie]}
                has_shooter = shooter["blocked_by_wall"] or pursuit == "visible_shooter"
                return {"entities": [survivor, shooter] if has_shooter else [survivor]}
            if action == "get_block":
                if pursuit == "slow_corner_shooter" and "timeout_s" in kwargs:
                    clock[0] += min(0.4, kwargs["timeout_s"])
                x, y, z = body["pos"]
                if y in (72, 73) and (x, z) in wall:
                    return {"name": "stone"}
                support = "lava" if pursuit == "lava" else "stone"
                return {"name": support if y < 72 else "air"}
            if action == "look":
                yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(yaw)
                point = [landing[0] + 2 * dx, landing[1], landing[2] + 2 * dz]
                steps.append([landing[0] + 2, *landing[1:]] if pursuit == "drifted_into_fire" else point)
                status.update(pos=steps[-1], nearest_hostile=None)
            if action == "move_to":
                steps.append([body["x"], body["y"], body["z"]])
                status["pos"] = steps[-1]
            return {"ok": True}

    env = CombatEnv(FieldBot())
    if wall:
        assert env._blocks_shot_across_hitbox(landing, shooter["pos"])
        straight_landing = [landing[0] + 2, *landing[1:]]
        assert env._blocks_shot_across_hitbox(straight_landing, shooter["pos"]) is (pursuit == "occluded_shooter")
    real_flee = env.flee

    def retreat(target, **kwargs):
        plans.append(target)
        if len(plans) > 1:
            return real_flee(target, **kwargs)
        status.update(pos=landing, nearest_hostile=survivor)
        if pursuit == "respawned":
            status["last_death"] = {"t": 1}
        if pursuit == "time_budget":
            clock[0] += 4.0
        return {"ok": True, "mode": "flee", "moved_dist": 8.49}

    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"], "distance": 3.4,
    })

    followups = [action for action in outcome["actions"] if action["action"] == "followup_melee_retreat"]
    can_retreat = pursuit in {"clear", "visible_shooter", "occluded_shooter", "corner_shooter", "slow_corner_shooter"}
    attempts_followup = can_retreat or pursuit in {"lava", "drifted_into_fire"}
    assert plans == [zombie["pos"]] + ([survivor["pos"]] if attempts_followup else [])
    assert [action["ok"] for action in followups] == ([can_retreat] if attempts_followup else [])
    if pursuit == "visible_shooter":
        assert all(math.dist(point, shooter["pos"]) >= 4.2 for point in steps)
        assert math.dist(status["pos"], survivor["pos"]) > distance
    elif pursuit in {"corner_shooter", "slow_corner_shooter"}:
        assert all(
            math.dist(point, shooter["pos"]) >= shooter["dist"] or env._blocks_shot_across_hitbox(point, shooter["pos"])
            for point in steps
        )
        assert math.dist(status["pos"], survivor["pos"]) > distance
        assert clock[0] - 1000.0 <= 1.2
        if pursuit == "slow_corner_shooter":
            assert clock[0] > 1000.0
    elif pursuit == "drifted_into_fire":
        assert len(steps) == 1 and followups[0]["reason"] == "projectile_lane_unconfirmed"
    elif can_retreat:
        assert steps == [pytest.approx([landing[0] + 2, *landing[1:]])]
    else:
        assert steps == []


@pytest.mark.parametrize(
    "outcome", ["same_life", "respawned_nearby", "time_budget", "retreat_into_lava", "failed_retreat_into_lava"],
)
def test_guardian_bounds_melee_macro_and_rejects_retreat_after_death(monkeypatch, outcome):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [11032.5, 0.6, -11040.3], "health": 14.0, "food": 20,
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 10},
    }
    moves = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack"}] if since < 1 else []}
            if action == "get_entities":
                return {"entities": []}
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}]}
            moves.append((action, kwargs["timeout_s"]))
            if action == "control":
                if outcome == "time_budget":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("retreat reached its deadline")
                status.update(pos=[11032.5, 6.2, -11035.2], on_ground=True)
                if outcome == "respawned_nearby":
                    status.update(health=20, last_death={"t": 20})
                if outcome in {"retreat_into_lava", "failed_retreat_into_lava"}:
                    status.update(health=0.33, in_lava=True, on_ground=False)
            return {"ok": True}

    def leave_cave(scoped, **kwargs):
        scoped.bot.call("control", {"forward": True, "ms": 1200, "stop_on": ["death"]}, timeout_s=40.0)
        return {"ok": outcome != "failed_retreat_into_lava", "mode": "flee", "pos_after": status["pos"], "respawned": False}

    monkeypatch.setattr(MacroSkills, "_flee_hostiles", leave_cave)
    bot = CaveBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=skills)

    def escape_lava():
        assert status["in_lava"] is True
        status.update(in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    incident = guard.poll()

    retreat, = incident["actions"]
    assert retreat["action"] == "combat_or_flee"
    assert retreat["ok"] is (outcome == "same_life")
    assert skills.bot is bot
    assert moves[0] == ("control", 8.0)
    if outcome in {"retreat_into_lava", "failed_retreat_into_lava"}:
        assert retreat["result"]["reason"] == "retreat_into_lava"
        assert retreat["result"]["lava_recovery"]["ok"] is True
        assert incident["status"]["in_lava"] is False
    if outcome == "respawned_nearby":
        assert retreat["result"]["respawned"] is True
    elif outcome == "time_budget":
        assert retreat["result"] == {"ok": False, "reason": "time_budget", "partial": True}
        assert moves[1:] == [("stop", 1.5)]


def test_unarmed_melee_reflex_checks_footing_before_retreat(monkeypatch):
    status = {
        "pos": [0.5, 64.0, 0.5], "health": 14.0, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 42, "name": "zombie", "dist": 2.0, "pos": [0.5, 64.0, 2.5]},
    }

    class LavaRimBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [hurt] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_block":
                x, y, z = body["pos"]
                name = "air" if y >= 64 else "stone" if (x, z) == (0, 0) else "lava"
                return {"name": name}
            if action == "find_blocks":
                return {"blocks": []}
            pytest.fail(f"Unexpected {action} on an unverified retreat route")

    def unchecked_retreat(*args, **kwargs):
        pytest.fail("The unarmed bot must check retreat footing before moving")

    monkeypatch.setattr(MacroSkills, "_flee_hostiles", unchecked_retreat)
    guard = GenerationGuard(LavaRimBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    retreat, = guard.poll()["actions"]

    assert retreat["action"] == "combat_or_flee"
    assert retreat["ok"] is False
    assert retreat["result"]["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("landing", ["grounded", "airborne", "respawned"])
def test_unshielded_critical_melee_knockback_rechecks_late_landing(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    zombie = {"id": 42, "name": "zombie", "dist": 2.0, "pos": [0.5, 68, 2.5]}
    status = {"pos": [0.5, 68.6, 0.5], "health": 2.5, "food": 20, "ready": True, "alive": True,
              "on_ground": False, "last_death": {"t": 1}, "dimension": "overworld", "nearest_hostile": zombie}
    motions = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= 1000.45 and status["on_ground"] is False:
                    if landing == "grounded":
                        status.update(pos=[0.5, 68, 0.5], on_ground=True)
                    elif landing == "respawned":
                        status.update(last_death={"t": 2})
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 68 else "air"}
            if action == "control" and body.get("forward"):
                assert status["on_ground"] is True
                motions.append(action)
                status["pos"] = [0.5, 68, -1.5]
            if action == "move_to":
                assert status["on_ground"] is True
                motions.append(action)
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(RidgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    retreat, = guard.poll()["actions"]
    assert 1000.45 <= clock[0] <= 1000.65
    assert retreat["ok"] is (landing == "grounded")
    assert motions == (["control", "move_to"] if landing == "grounded" else [])
    assert bool(retreat["result"].get("respawned")) is (landing == "respawned")

