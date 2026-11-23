from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize(("landing", "initial_move"), [
    ("cleared", 1.8), ("pursued", 1.8), ("unsupported", 1.8), ("new_life", 1.8),
    ("new_life_after_probe", 1.8), ("cleared", 0.92),
])
def test_w20_poll_critical_melee_pursuit_reuses_checked_disengagement(monkeypatch, landing, initial_move):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    first_landing = [236820.9, 38, 250024.3]
    start_x = first_landing[0] + initial_move
    zombie = {"id": 2323053, "name": "zombie", "dist": 2.0, "pos": [start_x + 2.0, 38, 250024.3]}
    pursuer = {**zombie, "dist": 1.1, "pos": [236822, 38, 250024.3]}
    status = {
        "pos": [start_x, 38, 250024.3], "dimension": "overworld", "last_death": {"t": 1},
        "health": 3.0, "food": 20, "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": zombie, "yaw": 0.0,
    }
    calls = []
    pulses = []

    class CaveBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "inventory":
                return {"items": []}
            if endpoint == "events":
                return {"events": [] if kwargs["params"]["since"] else [
                    {"type": "hurt", "seq": 1, "source": "mob_attack", "source_entity": zombie},
                ]}
            if endpoint == "get_entities":
                if landing == "new_life":
                    status.update(last_death={"t": 2})
                return {"entities": [pursuer]}
            if endpoint == "get_block":
                if landing == "new_life_after_probe":
                    status.update(last_death={"t": 2})
                return {"name": "stone" if body["pos"][1] < 38 and landing != "unsupported" else "air"}
            if endpoint == "look" and "yaw" in body:
                status["yaw"] = body["yaw"]
            if endpoint == "control" and body.get("forward"):
                pulses.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx < -0.9 and abs(dz) < 0.1
                status.update(pos=[236818.2, 38, 250024.3], nearest_hostile={
                    "id": zombie["id"], "name": zombie["name"], "dist": 1.9 if landing == "pursued" else 3.8,
                })
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    verified_flee = guard.combat_env.flee

    def flee(threat_pos, **kwargs):
        calls.append(threat_pos)
        if len(calls) == 1:
            status.update(pos=first_landing, health=1.0, nearest_hostile={"id": zombie["id"], "name": "zombie", "dist": 1.1})
            return {"ok": initial_move >= 1.5, "mode": "flee", "moved_dist": initial_move, "pos_after": first_landing}
        return verified_flee(threat_pos, **kwargs)

    monkeypatch.setattr(guard.combat_env, "flee", flee)
    incident = guard.poll()

    assert calls == [zombie["pos"], pursuer["pos"]]
    followup, = [action for action in incident["actions"] if action["action"] == "followup_melee_retreat"]
    assert followup["ok"] is (landing == "cleared")
    if landing in {"cleared", "pursued"}:
        assert len(pulses) == 1 and pulses[0]["sprint"] is True
        assert followup["result"]["target_in_melee_range"] is (landing == "pursued")
    else:
        assert not pulses
    if landing in {"new_life", "new_life_after_probe"}:
        assert followup["result"]["respawned"] is True


def test_r19_poll_skips_eat_in_close_melee_range_after_combat_timeout(monkeypatch):
    """Reproduces r19_short hy4_hy_3_midgame: never drop shield to eat when a hostile is <= 5.5m after melee cooldown."""
    clock = [6000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    use_items: list[dict] = []
    state = {"hp": 7.8, "food": 17, "blocking": True, "mob_dist": 2.2}

    class _CloseThreatEatBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": state["hp"],
                    "food": state["food"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": state["blocking"],
                    "nearest_hostile": {"id": 51, "name": "zombie", "pos": [0.5 + state["mob_dist"], 64.0, 0.5], "dist": state["mob_dist"]},
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 10}, {"name": "iron_sword", "count": 1}]}
            if action == "use_item":
                use_items.append(dict(body or {}))
            return {"ok": True}

    guard = GenerationGuard(_CloseThreatEatBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    # Mark zombie id=51 as already in 12s pre_hurt_melee cooldown (simulating prior subcortical_combat timeout)
    guard._mark_pre_hurt_cooldown({"source": "pre_hurt_melee", "primary_entity_id": 51, "primary_type": "zombie"}, clock[0])
    assert guard.poll() is None
    assert use_items == []  # MUST NOT drop shield (`posture(shield=False)`) to eat right in zombie's face!

    # Once distanced > 5.5m safely on solid ground, poll() eats cleanly; while falling (on_ground=False) it skips eat!
    state["mob_dist"] = 9.0
    clock[0] += 0.5
    inc_safe = guard.poll()
    assert inc_safe is not None
    assert [a["action"] for a in inc_safe["actions"]] == ["eat"]
    assert use_items == [{"item": "cooked_beef"}]

    # Falling mid-air must NOT call eat
    class _FallingBot(_CloseThreatEatBot):
        def call(self, action, body=None, **kwargs):
            res = super().call(action, body, **kwargs)
            if action == "status":
                res["on_ground"] = False
                res["nearest_hostile"] = None
            return res

    use_items.clear()
    g_fall = GenerationGuard(_FallingBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert g_fall.poll() is None
    assert use_items == []

    # cover_or_flee when flee fails against skeleton must keep shield latched (`latched_guard=True`)
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    latch_calls: list[tuple[str, dict]] = []

    class _FailedFleeSkeletonBot:
        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5],
                "health": 6.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": False,
                "in_water": False,
                "in_lava": False,
                "nearest_hostile": {"id": 77, "name": "skeleton", "pos": [6.5, 64.0, 0.5], "dist": 6.0},
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            latch_calls.append((action, pl))
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [{"id": 77, "name": "skeleton", "pos": [6.5, 64.0, 0.5], "dist": 6.0}]}
            if action == "get_block":
                p = pl.get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            if action == "move_to":
                raise ApiError("path_not_found", "blocked")
            return {"ok": True}

    out_latch = CombatControllerSlot().handle(
        CombatEnv(_FailedFleeSkeletonBot()),
        {"source": "pre_hurt_ranged", "primary_entity_id": 77, "primary_type": "skeleton", "distance": 6.0},
    )
    assert out_latch["latched_guard"] is True
    assert latch_calls[-1] == ("posture", {"shield": True, "sneak": False, "jump": False})


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize(("dimension", "blocking"), [("overworld", True), ("minecraft:the_nether", False)])
def test_recent_melee_damage_defers_eating(entry, dimension, blocking, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    actions = []

    class ShieldBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [814.2, 78, -850.7], "health": 12, "food": 13, "on_ground": True,
                    "blocking": blocking, "dimension": dimension, "ready": True, "alive": True,
                    "nearest_hostile": {"name": "enderman", "dist": 17.7},
                }
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            actions.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(ShieldBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    check = guard.poll if entry == "poll" else guard.stabilize_after_tools
    clock[0] += 8.0
    assert check() is None
    assert actions == []

    clock[0] += 2.5
    assert check() is not None
    expected = [("posture", {"shield": False})] if blocking else []
    assert actions == [*expected, ("use_item", {"item": "cooked_beef"})]


@pytest.mark.parametrize("landing", [
    "after_rejection", "after_airborne_start", "airborne", "new_life", "water", "unsafe_side", "time_budget",
])
def test_unarmed_melee_without_an_exit_checks_bunker_after_landing(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    drowned = {"id": 42, "name": "drowned", "dist": 2.3, "pos": [-7986.5, 63, -3019.0]}
    status = {
        "pos": [-7986.5, 63, -3016.7], "health": 6.5, "food": 17, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": landing != "after_airborne_start",
        "last_death": {"t": 1}, "nearest_hostile": drowned,
    }
    calls = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": drowned}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "diamond_pickaxe", "count": 1}, {"name": "cobblestone", "count": 9}]}
            if action == "stop" and landing == "time_budget":
                calls.append("stop")
                assert kwargs["timeout_s"] == 1.5
                return {"ok": True}
            pytest.fail(f"Unexpected action: {action}")

    def dig_bunker(skills, *, urgent):
        assert urgent
        if "retreat" not in calls:
            calls.append("early_bunker")
            if landing == "unsafe_side":
                return {"ok": False, "reason": "unsafe_open_side", "block_pos": [-7987, 62, -3017], "block": "air"}
            status["on_ground"] = False
            return {"ok": False, "reason": "not_grounded"}
        calls.append("grounded_bunker")
        assert status["on_ground"] is True and not status.get("in_water") and status["last_death"] == {"t": 1}
        if landing == "time_budget":
            clock[0] += 3.6
            skills.bot.status()
            pytest.fail("Bunker exceeded the recovery deadline")
        return {"ok": True, "inside_shaft": True, "roof_placed": True}

    def retreat(*_args, **_kwargs):
        calls.append("retreat")
        status.update(on_ground=landing != "airborne", health=5.1973, in_water=landing == "water")
        if landing == "new_life":
            status["last_death"] = {"t": 2}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", dig_bunker)
    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    incident = guard.poll()

    expected = [] if landing == "after_airborne_start" else ["early_bunker"]
    expected.append("retreat")
    if landing in {"after_rejection", "after_airborne_start", "time_budget"}:
        expected.append("grounded_bunker")
    if landing == "time_budget":
        expected.append("stop")
    assert calls == expected
    if landing == "time_budget":
        assert incident["actions"][-1]["result"]["reason"] == "time_budget"
    elif expected[-1] == "grounded_bunker":
        assert incident["actions"][-1]["action"] == "emergency_bunker" and incident["actions"][-1]["ok"]
        assert guard._consecutive_flees == 0


@pytest.mark.parametrize("reason", ["unsafe_open_side", "unsafe_floor"])
def test_repeated_melee_hurt_skips_bunker_when_the_same_block_still_blocks_it(monkeypatch, reason):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    if reason == "unsafe_floor":
        unsafe_pos, unsafe_name = [-2273, 64, 3938], "air"
        pos = [-2272.5, 68, 3938.8]
        details = {"floor_pos": unsafe_pos, "floor_block": unsafe_name}
    else:
        unsafe_pos, unsafe_name = [-11016, 93, 10931], "water"
        pos = [-11014.3, 94, 10931.7]
        details = {"block_pos": unsafe_pos, "block": unsafe_name}
    obstacle = unsafe_name
    hits = []
    bunkers = []
    retreats = []
    status = {
        "pos": pos, "health": 3.9, "dimension": "overworld", "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": False,
        "nearest_hostile": {"id": 42, "name": "spider", "dist": 1.5},
    }

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in hits if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                assert body["pos"] == unsafe_pos
                return {"name": obstacle}
            raise AssertionError(action)

    class BunkerSkills:
        def dig_safety_bunker(self, *, urgent):
            assert urgent
            bunkers.append(obstacle)
            if obstacle == unsafe_name:
                return {"ok": False, "reason": reason, **details}
            return {"ok": True}

    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=BunkerSkills())

    def retreat(*_args, require_safe_footing):
        assert require_safe_footing
        retreats.append(True)
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}

    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    for seq, block in enumerate((unsafe_name, unsafe_name, "stone"), 1):
        obstacle = block
        hits.append({"seq": seq, "type": "hurt", "source": "mob_attack"})
        clock[0] += 0.6
        assert guard.poll()["requires_replan"]

    assert bunkers == [unsafe_name, "stone"]
    assert retreats == [True, True]


@pytest.mark.parametrize("retry", ["same_tile", "moved", "new_life", "expired", "partial_shaft", "stopped_without_hurt"])
def test_emergency_bunker_defers_an_untouched_same_tile_dig_under_repeated_melee(monkeypatch, retry):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [34019.4, 63, 34003.6]
    status = {"pos": origin, "health": 7.2, "food": 20, "dimension": "overworld", "last_death": {"t": 1},
              "ready": True, "alive": True, "on_ground": True, "is_day": False}
    hits = []
    digs = []
    retreats = []
    events = []

    class CoastBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "dirt", "count": 8}]}
            if action == "get_block":
                return {"name": "dirt"}
            if action == "dig":
                assert body["stop_on"] == ["hurt", "death"]
                digs.append(body["pos"])
                if retry == "partial_shaft" and len(digs) == 1:
                    status["pos"] = [34019.5, 62, 34003.5]
                    return {"ok": True}
                source = "stop" if retry == "stopped_without_hurt" else "hurt"
                raise ApiError("interrupted", f"stopped because of {source}")
            pytest.fail(f"Unexpected bunker command: {action}")

    def retreat(current, *_args, **_kwargs):
        retreats.append(current["pos"])
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    guard = GenerationGuard(CoastBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    hits.append({"seq": 1, "type": "hurt", "source": "mob_attack"})
    first = guard.poll()
    assert [action["action"] for action in first["actions"]] == ["emergency_bunker", "combat_or_flee"]

    status["pos"] = [34020.4, 63, 34003.6] if retry == "moved" else origin
    if retry == "new_life":
        status["last_death"] = {"t": 2}
    clock[0] += 4.1 if retry == "expired" else 1.8
    status["health"] = 2.17
    hits.append({"seq": 2, "type": "hurt", "source": "mob_attack"})
    second = guard.poll()

    expected = ["combat_or_flee"] if retry == "same_tile" else ["emergency_bunker", "combat_or_flee"]
    assert [action["action"] for action in second["actions"]] == expected
    assert len(retreats) == 2
    assert len(digs) == (1 if retry == "same_tile" else 3 if retry == "partial_shaft" else 2)
    assert any(event["type"] == "guardian.bunker_retry_deferred" for event in events) is (retry == "same_tile")


@pytest.mark.parametrize("landing", ["cleared", "pursued", "unsupported", "respawned"])
def test_w19_critical_melee_pursuit_uses_checked_disengagement(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    after_first = [184017.0, 112, 189172.1]
    zombie = {"id": 2273757, "name": "zombie_villager", "dist": 3.0, "pos": [184022.5, 112, 189172.1]}
    pursuing = {**zombie, "dist": 2.5, "pos": [184019.5, 112, 189172.1]}
    status = {
        "pos": [184019.5, 112, 189172.1], "dimension": "overworld", "last_death": {"t": 1},
        "health": 5.2, "ready": True, "alive": True, "on_ground": True, "is_day": False,
        "nearest_hostile": zombie, "yaw": 0.0,
    }
    pulses = []

    class FieldBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [status["nearest_hostile"]]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 112 and landing != "unsupported" else "air"}
            if action == "look" and "yaw" in body:
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                pulses.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx < -0.9 and abs(dz) < 0.1
                moved = 2.3 if body.get("sprint") else 0.71
                status.update(pos=[after_first[0] + dx * moved, 112, after_first[2] + dz * moved])
                distance = 2.6 if landing == "pursued" else 2.5 + moved
                status["nearest_hostile"] = {"id": zombie["id"], "name": zombie["name"], "dist": distance}
                if landing == "respawned":
                    status.update(last_death={"t": 2}, ready=False)
            return {"ok": True}

    env = CombatEnv(FieldBot())
    verified_flee = env.flee
    flee_calls = []

    def retreat(pos, **kwargs):
        flee_calls.append(pos)
        if len(flee_calls) == 1:
            status.update(pos=after_first, health=2.18, nearest_hostile=pursuing)
            return {"ok": True, "mode": "flee", "moved_dist": 2.47, "pos_after": after_first}
        return verified_flee(pos, **kwargs)

    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_entity_id": zombie["id"], "primary_type": zombie["name"], "distance": 3.0,
    })

    followup, = [action for action in outcome["actions"] if action["action"] == "followup_melee_retreat"]
    assert flee_calls == [zombie["pos"], pursuing["pos"]]
    if landing == "unsupported":
        assert not pulses and followup["reason"] == "no_safe_retreat"
    else:
        assert len(pulses) == 1 and pulses[0]["sprint"] is True
    assert followup["ok"] is (landing == "cleared")
    if landing in {"cleared", "pursued"}:
        assert followup["target_in_melee_range"] is (landing == "pursued")
    if landing == "pursued":
        assert followup["reason"] == "melee_still_close"
    if landing == "respawned":
        assert followup["respawned"] is True


@pytest.mark.parametrize(("health", "fresh_hurt", "expect_retreat"), [(3.3, True, True), (8.0, True, False), (3.3, False, False)])
def test_guardian_handles_fresh_critical_combat_hurt_inside_reaction_cooldown(monkeypatch, health, fresh_hurt, expect_retreat):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    retreats = []

    class RangedBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 63, 0.5], "health": health, "food": 20, "ready": True, "alive": True,
                    "on_ground": True, "nearest_hostile": {"id": 44, "name": "pillager", "dist": 16.0},
                }
            if action == "events":
                if fresh_hurt and kwargs["params"]["since"] < 1:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}]}
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            return {"ok": True}

    def retreat(*args, **kwargs):
        retreats.append(True)
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(RangedBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_reaction = 999.0
    monkeypatch.setattr(guard.combat_env, "flee", retreat)

    incident = guard.poll()

    assert bool(retreats) is expect_retreat
    assert (incident is not None) is expect_retreat


@pytest.mark.parametrize("tool", ["mc_skill_flee", "mc_skill_unstick"])
@pytest.mark.parametrize("safe_exit", [True, False])
@pytest.mark.parametrize("blocking", [False, True])
def test_bug076_unsafe_actor_exit_checks_another_route_when_a_hostile_is_close(tool, safe_exit, blocking):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    status = {
        "pos": [0.5, 64, 0.5], "alive": True, "ready": True, "on_ground": True, "health": 5.0, "blocking": blocking,
        "nearest_hostile": {"id": 7, "name": "pillager", "dist": 2.0, "pos": [2.5, 64, 0.5]},
    }
    motions = []
    yaw = [0.0]

    class RiverbankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                x, y, z = body["pos"]
                supported = x <= 0 if safe_exit else (x == 0 and z == 0)
                return {"name": "stone" if y == 63 and supported else "air"}
            if action == "look":
                if "entity_id" in body:
                    assert body["entity_id"] == 7
                    motions.append("face_attacker")
                else:
                    yaw[0] = body["yaw"]
            if action == "control":
                motions.append(action)
                dx, dz = yaw_to_unit_xz(yaw[0])
                status["pos"] = [0.5 + 2.3 * dx, 64.0, 0.5 + 2.3 * dz]
            if action == "move_to":
                motions.append(action)
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = RiverbankBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    stop_on = ["death"] if tool == "mc_skill_flee" else []
    first_choice = {"forward": True, "yaw": -1.5708, "ms": 900, "stop_on": stop_on}

    result = json.loads(guard.run_tool_with_reflex(tool, lambda: json.dumps(bot.call("control", first_choice))))

    assert result["requires_replan"] is True
    assert result["ok"] is safe_exit
    if safe_exit:
        assert motions[0] == "control"
        assert (motions[-1] == "face_attacker") is blocking
        assert status["pos"][0] < 0
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert motions == []


@pytest.mark.parametrize("floor", ["magma_block", "netherrack"])
@pytest.mark.parametrize(("dimension", "tool", "shooter"), [
    ("the_nether", "mc_skill_staircase", "blaze"), ("overworld", "pre_ensure", "skeleton"),
])
def test_critical_tool_preemption_checks_floor_before_combat(monkeypatch, floor, dimension, tool, shooter):
    status = {
        "pos": [2014.3, 76, 1890.2], "health": 0.25, "dimension": dimension,
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "sneaking": False,
    }
    postures = []
    combat = []

    class HazardBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                assert body["pos"][1] == 75 and kwargs["timeout_s"] <= 0.6
                return {"name": floor}
            if action == "posture":
                postures.append(body)
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            pytest.fail(f"Unexpected action during critical preemption: {action}")

    guard = GenerationGuard(HazardBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def respond(env, trigger):
        combat.append(trigger["primary_type"])
        return {"handled": True, "status_after": dict(status), "mode": "cover_or_flee"}

    monkeypatch.setattr(guard.combat_slot, "handle", respond)
    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": shooter}, interrupted_name=tool,
    )

    if floor == "magma_block":
        assert receipt["reflex_mode"] == "hot_floor"
        assert postures == [{"sneak": True, "shield": True, "jump": False}]
        assert combat == []
    else:
        assert receipt["reflex_mode"] == "cover_or_flee"
        assert postures == [] and combat == [shooter]


@pytest.mark.parametrize("attack_result", ["returned", "interrupted", "read_timeout"])
@pytest.mark.parametrize("hazard", [
    "water", "ranged_water", "lava", "unarmed_melee", "unarmed_above_critical", "other_melee", "other_ranged",
])
def test_attack_hazard_requests_stop_and_preserves_read_timeout(monkeypatch, attack_result, hazard):
    import threading

    actor_thread = threading.current_thread()
    stopped = threading.Event()
    observed = threading.Event()
    actions = []
    armed = hazard in {"other_melee", "other_ranged"}
    attack_target = 44 if armed else 27
    above_critical = hazard == "unarmed_above_critical"
    in_water = hazard in {"water", "ranged_water"}
    drowned_distance = 8.5 if hazard == "ranged_water" else 2.0
    hostile_pos = [0.5 + drowned_distance, 62, 0.5]
    hostile_name = {"other_ranged": "pillager", "water": "drowned", "ranged_water": "drowned"}.get(hazard, "zombie")
    status = {
        "pos": [0.5, 63, 0.5], "health": 18 if above_critical else 12, "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 27, "name": hostile_name, "dist": drowned_distance, "pos": hostile_pos},
    }

    class AttackBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                current = dict(status)
                if threading.current_thread() is not actor_thread:
                    observed.set()
                return current
            if action == "get_entities":
                return {"entities": []}
            actions.append(action)
            if action == "attack":
                if in_water or hazard == "lava":
                    health = 9.98 if hazard == "ranged_water" else 6.7
                    status.update(in_water=in_water, in_lava=hazard == "lava", on_ground=False, health=health)
                else:
                    assert observed.wait(2.0)
                    weapon = "iron_sword" if armed else "wooden_pickaxe"
                    status.update(health=12 if above_critical else 6.7, held={"name": weapon})
                assert stopped.wait(2.0), "attack remained active after the survival hazard"
                if attack_result == "read_timeout":
                    raise httpx.ReadTimeout("attack timed out")
                if attack_result == "interrupted":
                    raise ApiError("interrupted", "attack stopped")
                return {"killed": False}
            if action == "stop":
                stopped.set()
                return {"ok": True}
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    bot = AttackBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def escape_to_shore(threat_pos, **kwargs):
        assert threat_pos == hostile_pos
        assert actions[:2] == ["attack", "stop"]
        actions.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def escape_lava():
        assert actions[:2] == ["attack", "stop"]
        actions.append("lava_escape")
        status.update(in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", escape_to_shore)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    monkeypatch.setattr(
        guard, "_escape_water", lambda *_args, **_kwargs: pytest.fail("drowned attack should hand off directly to shore"),
    )

    def retreat(_env, trigger):
        assert trigger["source"] == "tool_interrupted_hurt" and trigger["primary_entity_id"] == 27
        actions.append("retreat")
        return {"handled": True, "mode": "cover_or_flee", "actions": [], "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", retreat)

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": attack_target, "timeout": 6000}))

    if attack_result == "read_timeout":
        with pytest.raises(httpx.ReadTimeout, match="attack timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert actions == ["attack", "stop"]
    else:
        receipt = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert receipt["preempted_by_reflex"] is True
        assert not receipt["status_after"]["in_water"]
        assert actions.count("attack") == 1
        assert {"water": "shore", "ranged_water": "shore", "lava": "lava_escape"}.get(hazard, "retreat") in actions
        assert not status.get("in_lava")

