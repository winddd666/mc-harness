from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize(
    "sighting", ["exposed", "covered", "timeout", "covered_with_melee", "fireball_in_cover", "burn_after_fireball"],
)
@pytest.mark.parametrize("health", [4.2, 16.0])
def test_burning_nether_retreat_checks_cover_against_nearby_blazes(monkeypatch, sighting, health):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    primary = {"name": "blaze", "id": 44, "dist": 7.8, "pos": [1534.2, 62, 3108.9]}
    secondary = {"name": "blaze", "id": 45, "dist": 14.4, "pos": [1529.5, 58, 3101.3]}
    distant = {"name": "blaze", "id": 46, "dist": 17.9, "pos": [1532, 58.5, 3097.5]}
    status = {
        "pos": [1532.6, 58, 3115.4], "dimension": "the_nether", "health": health, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "on_fire": True, "blocking": True,
        "nearest_hostile": {key: primary[key] for key in ("name", "id", "dist")},
    }
    calls = []
    cover_checks = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hits = [{"seq": 1, "type": "hurt", "source": "on_fire"}]
                if sighting == "fireball_in_cover":
                    hits.append({"seq": 2, "type": "hurt", "source": "fireball"})
                if sighting == "covered_with_melee":
                    hits = [
                        {"seq": 1, "type": "hurt", "source": "mob_attack"},
                        {"seq": 2, "type": "hurt", "source": ""},
                    ]
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                if sighting == "timeout":
                    raise httpx.ReadTimeout("hostile scan timeout")
                return {"entities": [primary, secondary, distant]}
            return {"ok": True}

    def flee(threat_pos, *, wait_for_ground, other_shooters=()):
        calls.append((threat_pos, wait_for_ground, other_shooters))
        return {"ok": True, "mode": "flee"}

    def blocks_shot(_env, _pos, shooter_pos):
        cover_checks.append(shooter_pos)
        return shooter_pos == primary["pos"] or sighting in {"covered", "covered_with_melee", "fireball_in_cover", "burn_after_fireball"}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(CombatEnv, "_blocks_shot", blocks_shot)
    if sighting in {"fireball_in_cover", "burn_after_fireball"}:
        monkeypatch.setattr(CombatEnv, "probe_retreat", lambda *_args: None)
    if sighting == "burn_after_fireball":
        guard._last_ranged_hurt_at = 999.0
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    incident = guard.poll()

    if sighting == "timeout":
        assert cover_checks == [] and calls == [(None, True, ())]
    elif (
        sighting in {"covered_with_melee", "fireball_in_cover"}
        or (sighting == "burn_after_fireball" and health <= 10)
    ):
        assert cover_checks == [] and calls == [(primary["pos"], True, [secondary["pos"]])]
    else:
        assert cover_checks == [primary["pos"], secondary["pos"]]
        if sighting in {"covered", "burn_after_fireball"}:
            assert calls == []
            retreat = next(a for a in incident["actions"] if a["action"] == "combat_or_flee")
            assert retreat["result"]["mode"] == "natural_cover"
        else:
            assert calls == [(primary["pos"], True, [secondary["pos"]])]


@pytest.mark.parametrize("scene", [
    "attributed_blaze", "small_fireball", "unknown_fireball", "ghast_also_present",
    "close_melee", "retreat_succeeded", "new_life", "respawn_during_scan",
])
def test_stalled_nether_blaze_retreat_can_move_to_verified_cover(monkeypatch, scene):
    from mc_agent.skills.combat_env import CombatEnv

    blaze = {"id": 44, "name": "blaze", "pos": [-1972.3, 62, 2032.4], "dist": 8.0}
    other_blaze = {"id": 45, "name": "blaze", "pos": [-1964.3, 61, 2042.4], "dist": 10.0}
    status = {
        "pos": [-1964.3, 61, 2032.4], "dimension": "the_nether", "health": 17.3, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "nearest_hostile": blaze, "last_death": {"t": 1},
    }
    hits = [{"seq": 1, "type": "hurt", "source": "small_fireball" if scene == "small_fireball" else "fireball"}]
    if scene not in {"unknown_fireball", "small_fireball"}:
        hits[0]["source_entity"] = blaze
    calls = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                calls.append("scan")
                if scene == "respawn_during_scan":
                    status["last_death"] = {"t": 2}
                hostiles = [blaze, other_blaze]
                if scene == "close_melee":
                    hostiles.append({"id": 46, "name": "wither_skeleton", "dist": 3.3, "pos": [-1961, 61, 2032]})
                if scene == "ghast_also_present":
                    hostiles.append({"id": 46, "name": "ghast", "dist": 11.0, "pos": [-1953.3, 61, 2032]})
                return {"entities": hostiles}
            pytest.fail(f"Unexpected action: {action}")

    def flee(threat_pos, **_kwargs):
        calls.append("retreat")
        assert threat_pos == blaze["pos"]
        if scene == "new_life":
            status["last_death"] = {"t": 2}
        if scene == "retreat_succeeded":
            return {"ok": True, "mode": "flee", "moved_dist": 2.8}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    def cover(_env, shooter, *, other_shooters, allow_stationary, verify_live_shooters, expected_life):
        calls.append("cover")
        assert shooter == blaze["pos"] and other_shooters == [other_blaze["pos"]]
        assert not allow_stationary and verify_live_shooters
        assert expected_life == ("the_nether", {"t": 1})
        return {"ok": True, "mode": "natural_cover", "pos_after": [-1964.3, 61, 2030.2]}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    if scene != "respawn_during_scan":
        monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    actions = guard.poll()["actions"]

    if scene in {"attributed_blaze", "small_fireball"}:
        assert calls == ["retreat", "scan", "cover"]
        assert actions[-1]["action"] == "natural_cover_after_retreat" and actions[-1]["ok"]
    elif scene in {"close_melee", "ghast_also_present", "respawn_during_scan"}:
        assert calls == ["retreat", "scan"]
        assert actions[-1]["action"] == "natural_cover_after_retreat" and not actions[-1]["ok"]
        if scene == "respawn_during_scan":
            assert actions[-1]["result"]["respawned"] is True
    else:
        assert calls == ["retreat"]


@pytest.mark.parametrize("scene", [
    "covered", "unattributed_burn", "exposed", "ghast", "near_melee", "no_support", "fresh_hit", "respawn_during_scan",
    "shooter_missing",
])
def test_burning_nether_bot_checks_stationary_blaze_cover_after_retreat_fails(monkeypatch, scene):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    pos = [1855.1, 58, 1804.2]
    blaze = {"id": 44, "name": "blaze", "pos": [1860.5, 59, 1805.1], "dist": 5.6}
    distant_melee = {"id": 46, "name": "wither_skeleton", "pos": [1855.1, 58, 1811.7], "dist": 7.5}
    status = {
        "pos": pos, "dimension": "the_nether", "last_death": {"t": 1}, "health": 1.43, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "on_fire": True, "blocking": True,
        "nearest_hostile": distant_melee if scene == "unattributed_burn" else blaze,
    }
    hits = [{"seq": 1, "type": "hurt", "source": "fireball" if scene == "fresh_hit" else "on_fire"}]
    retreats = []
    checked = []

    class FortressBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if endpoint == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if endpoint == "get_entities":
                entities = [] if scene == "shooter_missing" else [blaze]
                if scene == "ghast":
                    entities.append({"id": 45, "name": "ghast", "pos": [1863.5, 60, 1805.1], "dist": 8.8})
                if scene == "near_melee":
                    entities.append({"id": 45, "name": "wither_skeleton", "pos": [1858, 58, 1804.2], "dist": 2.9})
                if scene == "unattributed_burn":
                    entities.append(distant_melee)
                return {"entities": entities}
            if endpoint == "get_block":
                x, y, _ = body["pos"]
                support = y == 57 and scene != "no_support"
                wall = x == 1858 and 58 <= y <= 61 and scene != "exposed"
                return {"name": "netherrack" if support or wall else "air"}
            if endpoint == "look":
                return {"ok": True}
            pytest.fail(f"Unexpected mutation during stationary cover check: {endpoint}")

    def flee(*_args, **_kwargs):
        retreats.append(status["health"])
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    original_blocks_shot = CombatEnv._blocks_shot_across_hitbox

    def blocks_shot(env, origin, threat, **kwargs):
        checked.append((origin, threat))
        blocked = original_blocks_shot(env, origin, threat, **kwargs)
        if scene == "respawn_during_scan":
            status["last_death"] = {"t": 2}
        return blocked

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_ranged_hurt_at = 999.0
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(CombatEnv, "_blocks_shot_across_hitbox", blocks_shot)

    incident = guard.poll()
    holds = [action for action in incident["actions"] if action["action"] == "hold_blaze_cover"]
    if scene in {"covered", "unattributed_burn"}:
        assert len(holds) == 1 and holds[0]["ok"]
        assert holds[0]["result"]["pos_after"] == pos
        assert holds[0]["result"]["moved_dist"] == 0.0
        hits.append({"seq": 2, "type": "hurt", "source": "on_fire"})
        clock[0] += 1.0
        status["health"] = 0.57
        next_actions = guard.poll()["actions"]
        assert any(action["action"] == "hold_blaze_cover" and action["ok"] for action in next_actions)
        assert all(action["action"] != "combat_or_flee" for action in next_actions)
        assert checked == [(pos, blaze["pos"]), (pos, blaze["pos"])]
        hits.append({"seq": 3, "type": "hurt", "source": "fireball"})
        clock[0] += 1.0
        next_actions = guard.poll()["actions"]
        assert all(action["action"] != "hold_blaze_cover" for action in next_actions)
        assert retreats == [1.43, 0.57]
    else:
        assert not any(action["ok"] for action in holds)
        assert bool(holds) is (scene != "fresh_hit")
        assert checked == ([(pos, blaze["pos"])] if scene in {"exposed", "respawn_during_scan"} else [])
        if scene == "respawn_during_scan":
            assert holds[0]["result"]["respawned"] is True
            assert "death_or_connection_changed" in incident["reasons"]
        assert retreats == [1.43]


@pytest.mark.parametrize(("pressure", "bunker_first"), [
    ("direct_melee", False), ("unattributed_followup", False), ("occluded", True),
    ("inside_shaft", True), ("out_of_reach", True), ("stale_hit", True),
])
def test_nether_guard_retreats_before_digging_when_critical_melee_persists(monkeypatch, pressure, bunker_first):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [1870.3, 64, -3792.3]
    skeleton = {"id": 310015, "name": "wither_skeleton", "dist": 2.6, "pos": [1872.9, 64, -3792.3]}
    status = {
        "pos": origin, "health": 6.3, "food": 20, "dimension": "the_nether",
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "nearest_hostile": skeleton,
    }
    hits = [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": skeleton}]
    calls = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "look":
                calls.append("face_attacker")
                return {"ok": True}
            raise AssertionError(action)

    def bunker(_skills, *, urgent):
        assert urgent
        calls.append("emergency_bunker")
        return {"ok": True, "inside_shaft": True, "roof_placed": True}

    def retreat(*_args, **_kwargs):
        calls.append("retreat")
        if len(hits) == 1:
            return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}
        status.update(pos=[1872.3, 64, -3792.5], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 2.01}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", bunker)
    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    monkeypatch.setattr(guard, "_inside_own_shaft", lambda: len(hits) > 1 and pressure == "inside_shaft")
    guard.poll()
    assert calls == ["retreat", "face_attacker"]
    calls.clear()

    clock[0] += 4.1 if pressure == "stale_hit" else 1.6
    status["health"] = 1.67
    if pressure == "occluded":
        status["nearest_hostile"] = {**skeleton, "blocked_by_wall": True}
    elif pressure == "out_of_reach":
        status["nearest_hostile"] = {**skeleton, "pos": [1875, 64, -3792.3], "dist": 4.7}
    hit = {"seq": 2, "type": "hurt"}
    if pressure not in {"unattributed_followup", "stale_hit"}:
        hit.update(source="mob_attack", source_entity=status["nearest_hostile"])
    hits.append(hit)

    incident = guard.poll()
    assert incident["requires_replan"]
    assert calls == (["emergency_bunker"] if bunker_first else ["retreat"])


@pytest.mark.parametrize("landed", [False, True])
def test_lava_reflex_requires_landing_even_without_lava_under_the_hitbox(monkeypatch, landed):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-733.7, 64.4, 287.3], "dimension": "the_nether", "health": 20, "food": 20,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, y, z) == (-734, 64, 287):
                    return {"name": "lava"}
                return {"name": "netherrack" if y < 64 else "air"}
            if action == "control" and body.get("forward"):
                assert body["jump"] is True
                status.update(in_lava=False, on_ground=landed, pos=[-734.4, 64.0 if landed else 64.8, 286.3])
                clock[0] += 5.0
            return {"ok": True}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    escape, = incident["actions"]
    assert escape["action"] == "escape_lava"
    assert escape["ok"] is landed
    assert escape["result"]["in_lava"] is False
    assert "airborne_above_lava" not in escape["result"]
    if not landed:
        assert escape["result"]["reason"] == "lava_landing_unconfirmed"


@pytest.mark.parametrize("landing", ["lava", "dry", "respawned", "loading"])
@pytest.mark.parametrize("entry", ["poll", "boundary"])
def test_combat_retreat_that_enters_lava_hands_off_to_escape_immediately(monkeypatch, landing, entry):
    brute = {"id": 44, "name": "piglin_brute", "dist": 2.5, "pos": [247.2, 35.5, 195.5]}
    status = {
        "pos": [249.4, 35.5, 195.5], "dimension": "the_nether", "last_death": {"t": 1},
        "health": 18, "food": 20, "ready": True, "alive": True, "on_ground": True,
        "in_lava": False, "nearest_hostile": brute,
    }
    calls = []
    events = []

    class BastionBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            if endpoint == "inventory":
                return {"items": []}
            raise AssertionError(endpoint)

    def retreat(trigger):
        calls.append(trigger["source"])
        status.update(pos=[251.6, 34.2, 196], in_lava=landing != "dry", on_ground=landing == "dry")
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        if landing == "loading":
            status["ready"] = False
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    def escape():
        calls.append("escape")
        status.update(pos=[252.5, 35, 196.5], in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    guard = GenerationGuard(BastionBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    monkeypatch.setattr(guard, "_run_subcortical_combat", retreat)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)

    incident = guard.poll() if entry == "poll" else guard.stabilize_after_tools()
    assert calls == ["pre_hurt_melee"] + (["escape"] if landing == "lava" else [])
    prefix = "boundary_" if entry == "boundary" else ""
    combat, escape = prefix + "subcortical_combat", prefix + "escape_lava"
    assert [event["action"] for event in events if event["type"] == "guardian.action.completed"
            and event["action"] in {combat, escape}] == ([combat, escape] if landing == "lava" else [combat])
    if entry == "poll":
        assert ("in_lava" in incident["reasons"]) is (landing == "lava")
    if entry == "poll" and landing == "lava":
        assert [action["action"] for action in incident["actions"]] == ["subcortical_combat", "escape_lava"]
        assert incident["status"]["in_lava"] is False
        assert incident["status"]["pos"] == [252.5, 35, 196.5] and incident["requires_replan"]
    if entry == "boundary" and landing == "lava":
        assert "战斗后脱离岩浆" in incident


@pytest.mark.parametrize("entry", ["before_reflex", "after_reflex"])
@pytest.mark.parametrize("landing", ["lava", "dry", "still_falling", "new_life"])
def test_nether_fast_fall_during_tool_preemption_waits_for_contact(monkeypatch, entry, landing):
    rim = [1858.6, 69, 1808.9]
    airborne = [1862.4, 34.6, 1809.8]
    fall = {"pos": airborne, "on_ground": False, "velocity": [0.262, -38.118, 0.061]}
    skeleton = {"id": 877411, "name": "wither_skeleton", "dist": 7.7}
    status = {
        "pos": rim, "on_ground": True, "velocity": [0, 0, 0], "dimension": "the_nether", "last_death": {"t": 1},
        "health": 14.93, "food": 20, "ready": True, "alive": True, "in_lava": False, "in_water": False,
        "nearest_hostile": skeleton,
    }
    calls = []

    class FortressBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            if endpoint == "dig":
                if entry == "before_reflex":
                    status.update(fall)
                return {"interrupted_by": "hurt"}
            pytest.fail(f"Unexpected API call during airborne handoff: {endpoint}")

    bot = FortressBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def wait(seconds):
        assert seconds == 0.1
        calls.append("wait")
        if landing == "lava":
            status.update(pos=[1863.2, 28.1, 1810.3], in_lava=True, velocity=[0, -2.5, 0])
        elif landing == "dry":
            status.update(pos=[1863.1, 29, 1810.0], on_ground=True, velocity=[0, 0, 0])
        elif landing == "new_life":
            status.update(pos=[-130.5, 64, 63.5], last_death={"t": 2}, dimension="overworld", on_ground=True)

    def fight(_trigger):
        calls.append("combat")
        if entry == "after_reflex":
            status.update(fall)
        else:
            assert status["on_ground"] is True
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    def escape():
        calls.append("escape")
        assert status["in_lava"] is True
        status.update(pos=[1864.5, 29, 1810.5], in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False, "pos_after": status["pos"]}

    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", wait)
    monkeypatch.setattr(guard, "_run_subcortical_combat", fight)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    receipt = json.loads(guard.run_tool_with_reflex("mc_skill_staircase", lambda: json.dumps(
        bot.call("dig", {"pos": [1859, 69, 1808]}),
    )))

    expected = ["combat"] if entry == "after_reflex" else []
    expected += ["wait", "wait"] if landing == "still_falling" else ["wait"]
    if landing == "lava":
        expected += ["escape"]
        assert receipt["reflex_mode"] == "lava_escape" and receipt["status_after"]["in_lava"] is False
    elif landing == "new_life":
        assert receipt["reflex_mode"] == "life_unavailable"
    elif landing == "still_falling":
        assert receipt["reflex_mode"] == "falling" and receipt["status_after"]["on_ground"] is False
    else:
        if entry == "before_reflex":
            expected += ["combat"]
        assert receipt["reflex_mode"] == "cover_or_flee"
    assert calls == expected
    assert receipt["requires_replan"] is True
    assert receipt["pos_at_preemption"] == (airborne if entry == "before_reflex" else rim)


@pytest.mark.parametrize("entry", ["poll", "after_combat"])
@pytest.mark.parametrize("landing", [
    "lava", "above_lava", "dry", "still_falling", "new_life", "read_failed", "position_missing", "overworld",
])
def test_generation_guard_checks_nether_fall_contact_without_another_hit(monkeypatch, entry, landing):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    fall = {"pos": [1856.6, 34.6, 1805.4], "on_ground": False, "velocity": [0, -38, 0]}
    status = {
        "pos": [1857, 82, 1804.8], "on_ground": True, "velocity": [0, 0, 0], "dimension": "the_nether",
        "health": 18.3, "food": 20, "ready": True, "alive": True, "in_water": False, "in_lava": False,
        "last_death": {"t": 1},
    }
    if entry == "poll":
        status.update(fall)
    if landing == "overworld":
        status["dimension"] = "overworld"
    calls = []

    class FortressBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                if landing == "read_failed" and "wait" in calls:
                    raise httpx.ReadTimeout("temporary status timeout")
                return dict(status)
            if endpoint == "events":
                if entry == "after_combat" and kwargs["params"]["since"] == 0:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "fireball"}]}
                return {"events": []}
            if endpoint == "get_block":
                lava_y = {"above_lava": 28, "read_failed": 32, "position_missing": 32}.get(landing)
                return {"name": "lava" if body["pos"][1] == lava_y else "air"}
            if endpoint == "inventory":
                return {"items": []}
            pytest.fail(f"Unexpected API action during Nether fall: {endpoint}")

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if entry == "poll":
        guard.last_reaction = 999.0

    def wait(seconds):
        assert seconds == 0.1
        calls.append("wait")
        if landing == "lava":
            status.update(pos=[1856.6, 27.6, 1805.4], in_lava=True)
        elif landing == "above_lava":
            status.update(pos=[1856.6, 30.1, 1805.4])
        elif landing == "dry":
            status.update(pos=[1856.6, 29, 1805.4], on_ground=True)
        elif landing == "new_life":
            status.update(pos=[-135.5, 69, 73.5], dimension="overworld", last_death={"t": 2}, on_ground=True)
        elif landing == "position_missing":
            status.pop("pos", None)

    def fight(*_args, **_kwargs):
        calls.append("combat")
        status.update(fall)
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    def escape(**_kwargs):
        calls.append("escape")
        assert status["in_lava"] or landing == "above_lava"
        status.update(pos=[1858.5, 30, 1805.5], in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False, "pos_after": status["pos"]}

    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", wait)
    monkeypatch.setattr(guard, "_defend_or_flee", fight)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    incident = guard.poll()

    expected = ["combat"] if entry == "after_combat" else []
    if landing != "overworld":
        expected += ["wait", "wait"] if landing in {"above_lava", "still_falling", "position_missing"} else ["wait"]
    if landing in {"lava", "above_lava"}:
        assert incident["requires_replan"] and "in_lava" in incident["reasons"]
        assert incident["actions"][-1]["action"] == "escape_lava"
        assert incident["status"]["pos"] == [1858.5, 30, 1805.5]
        expected.append("escape")
    elif landing == "new_life":
        assert incident["requires_replan"] and "death_or_connection_changed" in incident["reasons"]
    elif entry == "poll":
        assert incident is None
    else:
        assert "in_lava" not in incident["reasons"]
    assert calls == expected


@pytest.mark.parametrize("bridge_edge", [False, True])
def test_r20_lava_rim_hitbox_airborne_and_lava_hurt_trigger_escape_not_bunker(monkeypatch, bridge_edge):
    """Reproduces r20_short ds_ds_4_nether_fortress:
    1) Mid-air pos `[705.9, 64.6, 640]` whose 0.3m hitbox clips `(706, 64, 640)` lava stays in escape_lava until grounded dry.
    2) `hurt(source="lava")` while `status["in_lava"]` fluctuates False routes to `escape_lava`, NEVER `emergency_bunker`.
    """
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)

    rim_state: dict[str, Any] = {
        "in_lava": not bridge_edge,
        "on_ground": False,
        "pos": [705.9, 64.6, 640.0] if bridge_edge else [706.5, 64.0, 640.5],
        "placed": set(),
    }

    class _LavaRimBot:
        def status(self):
            return {
                "pos": list(rim_state["pos"]),
                "health": 15.6,
                "alive": True,
                "ready": True,
                "in_lava": rim_state["in_lava"],
                "on_ground": rim_state["on_ground"],
                "yaw": -1.57,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in rim_state["placed"]:
                    return {"name": "cobblestone"}
                if p[0] <= (705 if bridge_edge else 704) and p[1] == 63:
                    return {"name": "netherrack"}
                if p[0] >= 706 and (p[1] == 63 or (p[1] == 64 and not bridge_edge)):
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "control" and pl.get("forward") and not rim_state["placed"]:
                # Jump leaves center at 705.9 over air while 0.3m hitbox still clips lava at x=706!
                rim_state["pos"] = [705.9, 64.6, 640.0]
                rim_state["in_lava"] = False
                rim_state["on_ground"] = False
            elif action == "place":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                rim_state["placed"].add(p)
                rim_state["pos"] = [704.5, 64.0, 640.5]
                rim_state["on_ground"] = True
            return {"ok": True}

    out_rim = CombatEnv(_LavaRimBot()).escape_lava()
    assert out_rim["ok"] is True
    assert rim_state["placed"]  # Did NOT exit ok=True while [705.9, 64.6, 640.0] clipped (706, 64, 640) lava!

    # 2) poll() with hurt(source="lava") while in_lava=False must escape lava rim instead of digging a bunker
    poll_state: dict[str, Any] = {
        "pos": [705.9, 64.0, 640.5],
        "in_lava": False,
        "on_ground": True,
        "hurt_seq": 1,
    }
    bunker_calls: list[bool] = []
    flee_calls: list[bool] = []
    control_calls: list[dict] = []

    class _LavaHurtPollSkills:
        mining_dfs = None

        def dig_safety_bunker(self, urgent=False):
            bunker_calls.append(bool(urgent))
            return {"ok": False}

        def defend_or_flee(self, **kwargs):
            flee_calls.append(True)
            return {"ok": False}

    class _LavaHurtPollBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return {
                    "pos": list(poll_state["pos"]),
                    "health": 12.5,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": False,
                    "in_lava": poll_state["in_lava"],
                    "on_ground": poll_state["on_ground"],
                    "dimension": "minecraft:the_nether",
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                seq = poll_state["hurt_seq"]
                rows = [{"seq": seq, "type": "hurt", "health": 12.5, "source": "lava"}] if seq > since else []
                return {"events": rows, "last_seq": seq}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p[0] <= 704 and p[1] <= 63:
                    return {"name": "netherrack"}
                if p[0] >= 706 and p[1] in (63, 64):
                    return {"name": "lava"}
                if p[1] <= 62:
                    return {"name": "netherrack"}
                return {"name": "air"}
            if action == "control" and pl.get("forward"):
                control_calls.append(pl)
                poll_state["pos"] = [703.5, 64.0, 640.5]
            return {"ok": True}

    guard = GenerationGuard(_LavaHurtPollBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_LavaHurtPollSkills())
    inc_lava = guard.poll()
    assert inc_lava is not None
    assert "in_lava" in inc_lava["reasons"]
    assert any(a["action"] == "escape_lava" and a["ok"] for a in inc_lava["actions"])
    assert bunker_calls == []
    assert flee_calls == []
    assert control_calls

