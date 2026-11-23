from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("pocket, covered", [
    ("no_filler_block", True),
    ("no_filler_block", False),
    ("no_solid_wall", True),
    ("no_solid_wall", False),
    ("cover_not_entered", False),
])
@pytest.mark.parametrize("hurt_source", ["arrow", "trident"])
def test_projectile_poll_without_a_diggable_pocket_checks_cover_before_flee(monkeypatch, pocket, covered, hurt_source):
    from mc_agent.skills.combat_env import CombatEnv

    trident = hurt_source == "trident"
    shooter = {"id": 41, "name": "drowned" if trident else "skeleton", "dist": 8.0 if trident else 2.0}
    live_shooter = {**shooter, "pos": [0.5, 69, 0.5 + shooter["dist"]]}
    status = {
        "pos": [0.5, 69, 0.5], "dimension": "overworld", "health": 7.17 if trident else 16.0, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    checks = []

    class StairBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": hurt_source, "source_entity": shooter}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_entities":
                return {"entities": [live_shooter]}
            raise AssertionError(action)

    def dig_cover(_env, _skills):
        checks.append("dig")
        return {"ok": False, "reason": pocket}

    def seek_cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters=False):
        checks.append("natural_cover")
        assert pos == live_shooter["pos"]
        assert other_shooters == []
        assert allow_stationary is (pocket == "cover_not_entered")
        assert verify_live_shooters is (not trident)
        if covered:
            return {"ok": True, "mode": "natural_cover", "pos_after": [2.7, 69, 0.5]}
        return None

    def flee(_env, _pos, **_kwargs):
        checks.append("flee")
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", dig_cover)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(StairBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    expected = ["dig"]
    if trident or pocket in {"no_filler_block", "no_solid_wall"}:
        expected.append("natural_cover")
    if not covered:
        expected.append("flee")
    assert checks == expected
    assert incident["actions"][-1]["result"]["mode"] == ("natural_cover" if covered else "flee")


@pytest.mark.parametrize("landing", ["covered", "exposed", "respawned", "no_shooter", "near_melee"])
@pytest.mark.parametrize("has_shield", [False, True])
@pytest.mark.parametrize("hurt_source, shooter_name, with_attacker", [
    ("arrow", "skeleton", True),
    ("trident", "drowned", True),
    ("trident", "drowned", False),
])
def test_critical_projectile_retreat_checks_cover_from_landing(
    monkeypatch, landing, has_shield, hurt_source, shooter_name, with_attacker,
):
    from mc_agent.skills.combat_env import CombatEnv

    shooter = {"id": 41, "name": shooter_name, "dist": 8.0, "pos": [8.5, 102, 0.5]}
    fresh_dig_failure = has_shield and hurt_source == "arrow" and landing == "covered"
    origin = [0.5, 102 if fresh_dig_failure else 102.2, 0.5]
    status = {
        "pos": origin, "dimension": "overworld", "health": 4.0, "food": 20,
        "ready": True, "alive": True, "on_ground": fresh_dig_failure,
        "nearest_hostile": shooter, "blocking": has_shield,
    }
    checks = []
    melee = {"id": 42, "name": "zombie", "dist": 2.0}

    class HillBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": hurt_source}
                if with_attacker:
                    hurt["source_entity"] = shooter
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"} if has_shield else None}
            if action == "get_entities":
                checks.append("scan")
                entities = [] if landing == "no_shooter" else [shooter]
                if landing == "no_shooter" and hurt_source == "trident":
                    entities.extend([{**shooter, "id": 49}, {**shooter, "id": 50, "name": "skeleton"}])
                if landing == "near_melee":
                    entities.append(melee)
                return {"entities": entities}
            if action == "look" and has_shield:
                return {"ok": True}
            pytest.fail(f"Unexpected call: {action}")

    def flee(_env, _pos, **_kwargs):
        checks.append("flee")
        moved = 0.71 if landing == "covered" else 5.86
        status.update(pos=[0.5 - moved, 102, 0.5], on_ground=True, nearest_hostile=None)
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        elif landing == "near_melee":
            status["nearest_hostile"] = melee
        return {"ok": True, "mode": "flee", "moved_dist": moved, "pos_after": status["pos"]}

    def seek_cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters=False):
        assert pos == shooter["pos"] and not other_shooters and allow_stationary
        assert verify_live_shooters is (status["pos"] != origin and hurt_source == "arrow" and not has_shield)
        if status["pos"] == origin:
            checks.append("initial_cover")
            return None
        checks.append("cover")
        if landing == "covered":
            return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}
        return None

    def dig_cover(_env, _skills):
        assert fresh_dig_failure
        checks.append("dig_cover")
        return {"ok": False, "reason": "no_solid_wall"}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", dig_cover)
    guard = GenerationGuard(HillBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if has_shield:
        monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
        if not fresh_dig_failure:
            guard._last_dig_cover_fail_at = 999.0
    incident = guard.poll()

    expected_checks = ["flee"] if has_shield else ["initial_cover", "flee"]
    expected_actions = ["face_attacker", "combat_or_flee"] if has_shield else ["combat_or_flee"]
    if fresh_dig_failure:
        expected_checks[:0] = ["dig_cover", "scan", "initial_cover"]
        expected_actions.insert(1, "dig_cover")
    if landing != "respawned":
        expected_checks.append("scan")
        expected_actions.append("natural_cover_after_retreat")
    if landing in {"covered", "exposed"}:
        expected_checks.append("cover")
    if has_shield:
        expected_actions.append("face_attacker_after_retreat")
    assert checks == expected_checks
    assert [action["action"] for action in incident["actions"]] == expected_actions
    assert (guard._ranged_cover_active_until > guard.last_reaction) is (landing == "covered")


@pytest.mark.parametrize(("reason", "grounded", "moved", "expect_cover"), [
    ("no_safe_retreat", True, 0.0, True), ("unsafe_landing", True, 0.0, True),
    ("unsafe_landing", False, 0.0, False), (None, True, 0.67, True), (None, False, 0.67, False),
    (None, True, 0.0, False),
])
def test_critical_unshielded_arrow_retreat_checks_cover_after_a_stalled_landing(monkeypatch, reason, grounded, moved, expect_cover):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    shooter = {"id": 41, "name": "skeleton", "dist": 7.0}
    status = {
        "pos": [22788.3, 63.2, 17812.7], "dimension": "overworld", "health": 6.7, "food": 17,
        "ready": True, "alive": True, "on_ground": False, "nearest_hostile": shooter, "last_death": {"t": 1},
    }

    class QuarryBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": shooter}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 24}], "offhand": None}
            if action == "get_entities":
                return {"entities": [shooter]}
            raise AssertionError(action)

    guard = GenerationGuard(QuarryBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_dig_cover_block = (22788, 63, 17812)
    guard._last_dig_cover_at = clock[0] - 15
    landing_hp = 1.17 if moved else 3.7

    def defend(landing, attacker, counts, *, cover_only=False, **kwargs):
        if cover_only:
            assert landing["on_ground"] and landing["health"] == landing_hp and kwargs["prefer_cover"]
            return {"ok": True, "mode": "natural_cover"}
        status.update(health=landing_hp, pos=[22788.3 + moved, 63 if grounded else 63.2, 17812.7], on_ground=grounded)
        return {"ok": False, "mode": "flee", "moved_dist": moved, "pos_after": status["pos"],
                **({"reason": reason} if reason is not None else {})}

    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    incident = guard.poll()

    assert [action["action"] for action in incident["actions"]] == (
        ["combat_or_flee", "natural_cover_after_retreat"] if expect_cover else ["combat_or_flee"]
    )


@pytest.mark.parametrize("situation", [
    "covered", "receipt_only", "knockback", "near_melee", "two_shooters", "shooter_moved",
    "logs_only", "logs_and_one_stone", "above_critical", "no_materials", "wall_removed_in_rescan",
    "placed_cover_exposed_edge",
    "arrow_burst_with_logs", "followup_arrow_with_logs", "single_arrow_with_logs", "stale_arrow_with_logs",
    "arrow_burst_with_one_log", "critical_partial_wall", "critical_missing_wall",
    "stalled_landing", "stalled_no_materials", "stalled_two_shooters",
    "stalled_natural_respawn", "stalled_above_limit",
])
def test_critical_arrow_retreat_builds_verified_cover_when_no_natural_cover_exists(monkeypatch, situation):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    z = 7381.08 if situation == "placed_cover_exposed_edge" else 7381.5
    shooter = {"id": 41, "name": "skeleton", "dist": 8, "pos": [9824.9, 63, z]}
    status = {
        "pos": [9816.9, 63.2, z], "dimension": "overworld", "health": 5.5, "food": 17,
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 1},
        "nearest_hostile": {"id": 41, "name": "skeleton", "dist": 8},
    }
    entities = [shooter]
    if situation == "near_melee":
        entities.append({"id": 42, "name": "zombie", "dist": 3, "pos": [9816.9, 63, 7378.1]})
    if situation in {"two_shooters", "stalled_two_shooters"}:
        entities.append({"id": 43, "name": "skeleton", "dist": 8, "pos": [9816.9, 63, 7373.1]})
    sustained_arrows = situation in {"arrow_burst_with_logs", "arrow_burst_with_one_log"}
    critical_one_log = situation in {"critical_partial_wall", "critical_missing_wall"}
    previous_arrow = situation in {"followup_arrow_with_logs", "stale_arrow_with_logs"} or critical_one_log
    early_landing = sustained_arrows or previous_arrow or situation == "single_arrow_with_logs"
    material = "oak_log" if situation in {"logs_only", "logs_and_one_stone"} else "cobblestone"
    if early_landing:
        material = "dark_oak_log"
        status["health"] = 7.666667 if critical_one_log else 9.7
    items = [] if situation in {"no_materials", "stalled_no_materials"} else [{"name": material, "count": 3}]
    if early_landing:
        items[0]["count"] = 1 if situation == "arrow_burst_with_one_log" or critical_one_log else 4
    if situation == "logs_and_one_stone":
        items.insert(0, {"name": "cobblestone", "count": 1})
    blocks = {(9818, 64, 7381): "stone"} if situation == "critical_partial_wall" else {}
    attempts = []
    hits = [{"seq": 1, "type": "hurt", "source": "arrow"}]
    if sustained_arrows or previous_arrow:
        hits.append({"seq": 2, "type": "hurt", "source": "arrow"})
    available_events = 1

    class RavineBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits[:available_events] if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": items, "offhand": None}
            if action == "get_entities":
                if situation == "wall_removed_in_rescan" and len(attempts) == 2:
                    blocks.clear()
                return {"entities": entities}
            if action == "get_block":
                cell = tuple(body["pos"])
                return {"name": blocks.get(cell, "stone" if cell[1] < 63 else "air")}
            if action == "place":
                cell = tuple(body["pos"])
                attempts.append(cell)
                assert body["item"] == material and kwargs["timeout_s"] <= 0.65
                if situation != "receipt_only":
                    blocks[cell] = material
                if situation == "knockback":
                    status["pos"] = [9816.4, 63, 7381.1]
                if situation == "shooter_moved" and len(attempts) == 2:
                    shooter["pos"] = [9816.9, 63, 7373.1]
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(RavineBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if previous_arrow:
        guard._drain_events_pages()
        clock[0] += 8.1 if situation == "stale_arrow_with_logs" else 5.5
    available_events = len(hits)

    def defend(_position, _attacker, _counts, *, cover_only=False, **_kwargs):
        if cover_only:
            if situation == "stalled_natural_respawn":
                return {"ok": False, "mode": "respawned", "respawned": True}
            return {"ok": False, "mode": "natural_cover", "reason": "no_safe_natural_cover"}
        landing_hp = 6.1 if situation in {"above_critical", "stalled_above_limit"} else 4.833 if material == "oak_log" else 2.48
        if early_landing:
            landing_hp = 5.666667 if critical_one_log else 6.166667
        status.update(health=landing_hp, pos=[9816.9, 63, z], on_ground=True)
        if situation.startswith("stalled_"):
            return {"ok": False, "mode": "flee", "moved_dist": 0.0, "reason": "no_safe_retreat", "pos_after": status["pos"]}
        return {"ok": True, "mode": "flee", "moved_dist": 2.31, "pos_after": status["pos"]}

    if situation.startswith("stalled_"):
        monkeypatch.setattr(guard.combat_env, "dig_cover_pocket", lambda _skills: {"ok": False, "reason": "no_solid_wall"})
    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    incident = guard.poll()

    action = incident["actions"][-1]
    if situation.startswith("stalled_"):
        assert [item["action"] for item in incident["actions"]][:3] == ["combat_or_flee", "dig_cover", "natural_cover"]
        if situation in {"stalled_no_materials", "stalled_natural_respawn", "stalled_above_limit"}:
            assert action["action"] == "natural_cover" and attempts == []
            return
    no_cover = {"above_critical", "single_arrow_with_logs", "stale_arrow_with_logs", "arrow_burst_with_one_log"}
    if situation in no_cover or situation == "no_materials":
        assert action["action"] == ("combat_or_flee" if situation in no_cover else "natural_cover_after_retreat")
        assert attempts == []
        return
    assert action["action"] == "place_arrow_cover"
    cover_verified = situation in {
        "covered", "logs_only", "logs_and_one_stone", "arrow_burst_with_logs", "followup_arrow_with_logs",
        "critical_partial_wall", "stalled_landing",
    }
    assert action["ok"] is cover_verified
    exposed_after_placement = situation in {"shooter_moved", "wall_removed_in_rescan", "placed_cover_exposed_edge"}
    if exposed_after_placement or situation == "stalled_two_shooters":
        assert action["result"]["reason"] == "projectile_lane_unconfirmed"
    if situation == "critical_partial_wall":
        assert attempts == [(9818, 63, 7381)] and action["result"]["placed"] == 1
    elif cover_verified or exposed_after_placement:
        assert attempts == [(9818, 63, 7381), (9818, 64, 7381)]
    elif situation in {"receipt_only", "knockback"}:
        assert attempts == [(9818, 63, 7381)]
    else:
        assert attempts == []


@pytest.mark.parametrize("placement", ["confirmed", "missing", "respawned", "first_of_two", "shooter_changed"])
def test_arrow_cover_reads_back_a_placement_with_a_lost_response(placement):
    shooter = {"id": 1119132, "name": "skeleton", "dist": 8, "pos": [381035.5, 67, 374009.5]}
    status = {
        "pos": [381027.5, 67, 374009.5], "health": 0.294, "ready": True, "alive": True,
        "on_ground": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    wall = (381028, 67, 374009)
    blocks = {} if placement == "first_of_two" else {(wall[0], wall[1] + 1, wall[2]): "stone"}
    actions = []

    class BareBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_entities":
                if placement == "shooter_changed" and "stop" in actions:
                    return {"entities": [{**shooter, "id": 1119133}]}
                return {"entities": [shooter]}
            if action == "get_block":
                cell = tuple(body["pos"])
                return {"name": blocks.get(cell, "stone" if cell[1] == 66 else "air")}
            if action == "place":
                actions.append(("place", tuple(body["pos"])))
                if placement != "missing":
                    blocks[tuple(body["pos"])] = body["item"]
                if placement == "respawned":
                    status.update(last_death={"t": 2}, pos=[381013.5, 67, 374016.5])
                raise httpx.ReadTimeout("placement response lost")
            if action == "stop":
                actions.append(action)
                return {"ok": True}
            pytest.fail(f"Unexpected arrow cover action: {action}")

    guard = GenerationGuard(BareBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    landing = dict(status)
    result = guard._action(
        "place_arrow_cover",
        lambda: guard._place_arrow_cover(landing, {"dark_oak_log": 2 if placement == "first_of_two" else 1}, shooter["id"]),
    )

    assert actions == [("place", wall), "stop"]
    assert result["ok"] is (placement == "confirmed")
    cover = result["result"]
    assert cover["placement_timed_out"] is True
    assert cover["placed"] == (0 if placement == "missing" else 1)
    if placement == "shooter_changed":
        assert cover["reason"] == "projectile_lane_unconfirmed"
    elif placement != "confirmed":
        assert cover["reason"] == "placement_unconfirmed"


@pytest.mark.parametrize("scenario", ["cornered", "second_hostile", "out_of_reach", "respawned", "blocked_by_wall"])
def test_cornered_shielded_bot_strikes_a_lone_reachable_zombie_and_keeps_guard(monkeypatch, scenario):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    distance = 1.5 if scenario == "out_of_reach" else 0.6
    zombie = {
        "id": 11, "name": "zombie_villager", "dist": distance, "pos": [0.5 + distance, 29, 0.5],
        "blocked_by_wall": scenario == "blocked_by_wall",
    }
    status = {
        "pos": [0.5, 29, 0.5], "dimension": "overworld", "health": 4.7, "food": 20,
        "alive": True, "ready": True, "on_ground": True, "blocking": False,
        "nearest_hostile": zombie, "last_death": {"t": 1},
    }
    calls = []

    class TunnelBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                sword = {"name": "iron_sword", "count": 1}
                return {"items": [sword], "held": sword, "offhand": {"name": "shield", "count": 1}}
            if action == "get_entities":
                mobs = [zombie]
                if scenario == "second_hostile":
                    mobs.append({"id": 12, "name": "husk", "dist": 2.0, "pos": [0.5, 29, 2.5]})
                return {"entities": mobs}
            if action == "get_block":
                return {"name": "air"}
            if action in {"attack", "posture"}:
                calls.append((action, body))
            if action == "attack":
                assert status["blocking"] is True
                status["blocking"] = False
                return {"hits": 1}
            if action == "posture":
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def flee(_env, _pos, **_kwargs):
        if scenario == "respawned":
            status.update(health=20, last_death={"t": 2})
        return {"ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    outcome = CombatControllerSlot().handle(CombatEnv(TunnelBot()), {
        "source": "pre_hurt_melee", "primary_type": "zombie_villager", "primary_entity_id": 11,
    })

    attacks = [body for action, body in calls if action == "attack"]
    if scenario == "cornered":
        attack, = attacks
        assert attack["entity_id"] == 11 and attack["max_hits"] == 1
        assert outcome["actions"][-1]["action"] == "cornered_melee_strike"
        assert outcome["latched_guard"] is True and status["blocking"] is True
    else:
        assert attacks == []


@pytest.mark.parametrize("landing", ["critical", "healthy", "new_life", "airborne", "close_melee", "long_retreat"])
def test_short_ranged_retreat_checks_cover_before_the_next_arrow(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    shooter = {"id": 41, "name": "skeleton", "dist": 2.4, "pos": [2.9, -9, 0.5]}
    status = {
        "pos": [0.5, -9, 0.5], "health": 15.2, "food": 19, "alive": True, "ready": True,
        "dimension": "overworld", "on_ground": True, "nearest_hostile": shooter, "last_death": {"t": 1},
    }
    checks = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [shooter]}
            return {"ok": True}

    def retreat(_pos, **_kwargs):
        moved = 3.0 if landing == "long_retreat" else 1.41
        status.update(pos=[0.5 - moved, -9, 0.5], health=12 if landing == "healthy" else 6.17,
                      on_ground=landing != "airborne", last_death={"t": 2 if landing == "new_life" else 1})
        if landing == "close_melee":
            status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 0.9}
        return {"ok": True, "mode": "flee", "moved_dist": moved}

    def cover(pos, *, other_shooters, allow_stationary, verify_live_shooters):
        assert pos == shooter["pos"] and not other_shooters and allow_stationary and verify_live_shooters
        checks.append(status["pos"])
        return {"ok": True, "mode": "natural_cover"}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "flee", retreat)
    monkeypatch.setattr(env, "seek_natural_cover", cover)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged", "primary_type": "skeleton", "primary_entity_id": 41,
    })

    assert checks == ([status["pos"]] if landing == "critical" else [])
    assert (outcome["mode"] == "natural_cover") is (landing == "critical")


@pytest.mark.parametrize("sight", ["same_shooter", "different_shooter", "close_melee", "new_life"])
def test_w20_critical_short_ranged_retreat_reacquires_shooter_at_landing(monkeypatch, sight):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    landed = [236819.9, 39, 250030.0]
    shooter = {"id": 2329667, "name": "skeleton", "dist": 7.3}
    live_shooter = {**shooter, "pos": [236826.5, 39, 250033.2], "dist": 7.4}
    status = {
        "pos": [236820.7, 39, 250030.3], "health": 2.33, "food": 17, "alive": True, "ready": True,
        "dimension": "overworld", "on_ground": True, "nearest_hostile": shooter, "last_death": {"t": 1},
    }
    checks = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if status["pos"] != landed:
                    return {"entities": [shooter]}
                checks.append("rescan")
                if sight == "different_shooter":
                    return {"entities": [{**live_shooter, "id": 7}]}
                if sight == "close_melee":
                    return {"entities": [live_shooter, {"id": 8, "name": "zombie", "dist": 2.0}]}
                return {"entities": [live_shooter]}
            return {"ok": True}

    def flee(threat_pos, **kwargs):
        assert threat_pos is None
        status.update(pos=landed)
        if sight == "new_life":
            status["last_death"] = {"t": 2}
        return {"ok": True, "mode": "flee", "moved_dist": 0.86, "pos_after": landed}

    def seek_cover(threat_pos, **kwargs):
        assert threat_pos == live_shooter["pos"] and kwargs["expected_shooter_id"] == shooter["id"]
        assert kwargs["expected_life"] == ("overworld", {"t": 1}) and kwargs["verify_live_shooters"]
        checks.append("cover")
        return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "flee", flee)
    monkeypatch.setattr(env, "seek_natural_cover", seek_cover)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged", "primary_type": "skeleton", "primary_entity_id": shooter["id"], "distance": 7.3,
    })

    assert checks == {
        "same_shooter": ["rescan", "cover"], "different_shooter": ["rescan"], "close_melee": ["rescan"], "new_life": [],
    }[sight]
    assert (outcome["mode"] == "natural_cover") is (sight == "same_shooter")


@pytest.mark.parametrize("fallback", ["covered", "exposed", "respawned", "near_melee", "little_time"])
def test_interrupted_arrow_tool_checks_natural_cover_after_no_safe_retreat(monkeypatch, fallback):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    shooter = {"id": 41, "name": "skeleton", "dist": 4.3, "pos": [4.8, 71, 0.5]}
    second_shooter = {"id": 42, "name": "skeleton", "dist": 7.0, "pos": [0.5, 71, 7.5]}
    hostiles = [shooter, second_shooter]
    if fallback == "near_melee":
        hostiles.append({"id": 43, "name": "spider", "dist": 2.0, "pos": [2.5, 71, 0.5]})
    status = {"pos": [0.5, 71, 0.5], "health": 3.33, "food": 17, "alive": True, "ready": True,
              "dimension": "overworld", "on_ground": True, "nearest_hostile": shooter}
    checks = []

    class RidgeBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": hostiles}
            return {"ok": True}

    def flee(pos, **kwargs):
        assert pos == shooter["pos"] and kwargs["wait_for_ground"]
        checks.append("flee")
        if fallback == "little_time":
            clock[0] += 6.0
        return {"ok": False, "moved_dist": 0.0, "reason": "no_safe_retreat", "respawned": fallback == "respawned"}

    def seek_cover(pos, *, other_shooters, allow_stationary, verify_live_shooters):
        assert pos == shooter["pos"] and other_shooters == [second_shooter["pos"]] and not allow_stationary
        assert verify_live_shooters
        checks.append("cover")
        if fallback == "covered":
            status.update(pos=[0.5, 71, -1.7])
            return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}
        return None

    env = CombatEnv(RidgeBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", flee)
    monkeypatch.setattr(env, "seek_natural_cover", seek_cover)
    outcome = CombatControllerSlot().handle(env, {
        "source": "tool_interrupted_hurt", "hurt_source": "arrow", "primary_entity_id": 41, "primary_type": "skeleton",
    })

    assert checks == (["flee", "cover"] if fallback in {"covered", "exposed"} else ["flee"])
    assert outcome["mode"] == ("natural_cover" if fallback == "covered" else "cover_or_flee")


@pytest.mark.parametrize("event_source", [
    "arrow", "same_shooter_missing_pos", "other_shooter", "old_arrow", "melee", "read_timeout", "new_life",
])
def test_mining_reflex_recovers_missing_arrow_source_before_a_stalled_retreat(monkeypatch, event_source):
    monkeypatch.setattr("mc_agent.harness.guardian.time.time", lambda: 1000.0)
    shooter = {"id": 505830, "name": "skeleton", "pos": [41043.6, -2, 18938.5], "dist": 3.1}
    alternate = {"id": 505831, "name": "skeleton", "pos": [41040.5, -2, 18944.5], "dist": 6.0}
    targeted = alternate if event_source == "other_shooter" else shooter
    status = {
        "pos": [41040.5, -2, 18938.5], "health": 3.0, "food": 17, "dimension": "overworld",
        "alive": True, "ready": True, "on_ground": True, "last_death": {"t": 2 if event_source == "new_life" else 1},
        "nearest_hostile": shooter,
    }
    event = {
        "seq": 1, "t": 995000 if event_source == "old_arrow" else 999500,
        "type": "hurt", "source": "mob_attack" if event_source == "melee" else "arrow",
        "source_entity": targeted,
    }
    if event_source == "same_shooter_missing_pos":
        event["source_entity"] = {"id": shooter["id"], "name": shooter["name"]}
    calls = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if kwargs.get("timeout_s") == 0.4:
                    calls.append("read_hurt")
                    if event_source == "read_timeout":
                        raise httpx.ReadTimeout("hurt events not returned")
                return {"events": [event] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if event_source == "same_shooter_missing_pos":
                    return {"entities": [event["source_entity"]]}
                return {"entities": [shooter, alternate] if event_source == "other_shooter" else [shooter]}
            return {"ok": True}

    def flee(pos, **_kwargs):
        calls.append("flee")
        assert pos == targeted["pos"]
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    def cover(pos, *, other_shooters, allow_stationary, verify_live_shooters):
        calls.append("cover")
        assert pos == targeted["pos"] and not allow_stationary and verify_live_shooters
        assert other_shooters == ([shooter["pos"]] if event_source == "other_shooter" else [])
        return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}

    guard = GenerationGuard(ShaftBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    monkeypatch.setattr(guard.combat_env, "seek_natural_cover", cover)
    result = guard.handle_preemption({
        "source": "tool_interrupted_hurt", "primary_type": "skeleton", "primary_entity_id": shooter["id"],
        "primary_pos": shooter["pos"], "distance": 3.1, "observed_life": ("overworld", {"t": 1}),
    }, interrupted_name="mc_skill_mine_dfs")

    if event_source == "new_life":
        assert calls == [] and result["reflex_mode"] == "life_unavailable"
    elif event_source in {"arrow", "same_shooter_missing_pos", "other_shooter"}:
        assert calls == ["read_hurt", "flee", "cover"] and result["reflex_mode"] == "natural_cover"
    else:
        assert calls == ["read_hurt", "flee"] and result["reflex_mode"] == "cover_or_flee"


@pytest.mark.parametrize("unbroken_ledge", [True, False])
def test_short_retreat_checks_diagonal_supported_ledge(unbroken_ledge):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    origin = [0.3, 63.0, 0.3]
    status = {"pos": origin, "on_ground": True, "alive": True, "ready": True, "dimension": "overworld"}
    support = {(-2, -1), (-2, 0), (-1, -1), (-1, 0), (0, -1), (0, 0)}
    if not unbroken_ledge:
        support.remove((-2, -1))
    steps = []
    away = math.radians(15)
    away_dx, away_dz = yaw_to_unit_xz(away)
    threat = [origin[0] - 2.8 * away_dx, origin[1], origin[2] - 2.8 * away_dz]

    class LedgeBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                return {"name": "stone" if y == 62 and (x, z) in support else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            elif action == "control":
                assert body["ms"] == 220 and not body["jump"] and not body["sprint"]
                steps.append(self.yaw)
                dx, dz = yaw_to_unit_xz(self.yaw)
                status["pos"] = [origin[0] + 0.8 * dx, origin[1], origin[2] + 0.8 * dz]
            elif action == "move_to":
                pytest.fail("Short ledge cannot support a second retreat leg")
            return {"ok": True}

    outcome = CombatEnv(LedgeBot()).flee(threat, allow_shaft_seal=False, wait_for_ground=True)

    assert outcome["ok"] is unbroken_ledge
    assert steps == ([pytest.approx(math.radians(85))] if unbroken_ledge else [])
    if not unbroken_ledge:
        assert outcome["reason"] == "no_safe_retreat"

