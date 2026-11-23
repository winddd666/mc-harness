from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("landing", ["lava", "above_lava", "dry", "still_airborne", "new_life", "successful_retreat"])
def test_w19_pre_ensure_failed_melee_retreat_checks_airborne_handoff(monkeypatch, landing):
    zombie = {"id": 2280599, "name": "zombie", "dist": 2.3}
    status = {
        "pos": [184019.3, 112, 189196.7], "dimension": "overworld", "last_death": {"t": 1},
        "health": 13.6, "ready": True, "alive": True, "on_ground": True, "in_lava": False,
        "in_water": False, "nearest_hostile": zombie,
    }
    waits = []
    escapes = []
    events = []

    class RetreatBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "get_block":
                return {"name": "lava" if landing == "above_lava" and body["pos"][1] == 110 else "air"}
            if endpoint == "events":
                return {"events": []}
            pytest.fail(f"Unexpected API call during retreat handoff: {endpoint}")

    def retreat(_trigger):
        status.update(
            pos=[184020.4, 112.5, 189197.3], health=10.67, on_ground=False, velocity=[3.176, 3.825, 1.655],
            nearest_hostile={**zombie, "dist": 1.6},
        )
        return {
            "handled": True, "mode": "cover_or_flee", "status_after": dict(status),
            "actions": [
                {"action": "flee", "ok": landing == "successful_retreat", "moved": 1.25},
                {"action": "followup_melee_retreat", "ok": False, "entity_id": zombie["id"]},
            ],
        }

    def wait(seconds):
        waits.append(seconds)
        if landing in {"lava", "above_lava"} and len(waits) == 2:
            status.update(pos=[184027.7, 111.3, 189201.3], velocity=[3.2, -7, 1.6])
            if landing == "lava":
                status.update(in_lava=True, health=7.2)
        elif landing == "dry":
            status.update(on_ground=True, pos=[184021.9, 112, 189197.8], velocity=[0, 0, 0])
        elif landing == "new_life":
            status.update(in_lava=True, last_death={"t": 2})

    def escape(*, force_rim_escape=False):
        escapes.append(force_rim_escape)
        status.update(on_ground=True, in_lava=False, pos=[184029.5, 113, 189201.5])
        return {"ok": True, "in_lava": False}

    guard = GenerationGuard(RetreatBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", wait)
    monkeypatch.setattr(guard, "_run_subcortical_combat", retreat)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    receipt = guard.handle_preemption({
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"],
        "distance": 2.3, "action_released": True,
    }, interrupted_name="pre_ensure")

    expected_modes = {
        "lava": "lava_escape", "above_lava": "lava_escape", "dry": "cover_or_flee",
        "still_airborne": "falling", "new_life": "life_unavailable", "successful_retreat": "cover_or_flee",
    }
    assert receipt["reflex_mode"] == expected_modes[landing]
    assert bool(waits) is (landing != "successful_retreat")
    assert escapes == {"lava": [False], "above_lava": [True]}.get(landing, [])
    if escapes:
        assert receipt["status_after"]["in_lava"] is False and receipt["status_after"]["on_ground"] is True
    assert [event["action"] for event in events if event["type"] == "guardian.action.completed"] == (
        ["subcortical_combat", "escape_lava"] if escapes else ["subcortical_combat"]
    )


@pytest.mark.parametrize("position", ["failed_cover", "moved", "window_elapsed"])
def test_recent_arrow_defers_eating_at_failed_cover_site(monkeypatch, position):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-2945.7, -8, -10074.3], "dimension": "overworld", "health": 9.56, "food": 19,
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "nearest_hostile": None,
    }
    eats = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "arrow"}
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            if action == "use_item":
                eats.append(body)
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    guard._last_dig_cover_block = (-2946, -8, -10075)
    guard._last_dig_cover_fail_at = clock[0]
    if position == "moved":
        status["pos"] = [-2943.7, -8, -10074.3]
    clock[0] += 8.1 if position == "window_elapsed" else 3.75

    incident = guard.poll()

    if position == "failed_cover":
        assert incident is None and eats == []
    else:
        assert [action["action"] for action in incident["actions"]] == ["eat"]
        assert eats == [{"item": "cooked_beef"}]


@pytest.mark.parametrize("after_eat", ["spear_in_reach", "respawned", "clear"])
def test_interrupted_eat_restores_shield_against_a_confirmed_threat(after_eat):
    status = {
        "pos": [2562.3, 76, 9263.5], "dimension": "overworld", "health": 11.67, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "offhand": {"name": "shield"}, "last_death": {"t": 1},
    }
    actions = []

    class FoodBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}], "offhand": status["offhand"]}
            actions.append((action, body))
            if action == "posture":
                status["blocking"] = body["shield"]
                return {"blocking": status["blocking"]}
            if action == "use_item":
                status.update(pos=[2561.3, 75.9, 9261.5], health=4.67, food=20, on_ground=False)
                if after_eat == "spear_in_reach":
                    status["nearest_hostile"] = {"id": 78, "name": "husk", "dist": 2.5}
                if after_eat == "respawned":
                    status.update(ready=False, last_death={"t": 2})
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    guard = GenerationGuard(FoodBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    eat, = incident["actions"]
    assert incident["requires_replan"] and "fresh_hurt" in incident["reasons"]
    expected = [("posture", {"shield": False}), ("use_item", {"item": "cooked_beef"})]
    if after_eat == "spear_in_reach":
        expected += [("look", {"entity_id": 78}), ("posture", {"shield": True, "jump": False, "sneak": False})]
        assert eat["result"]["shield_restored"] is True
    else:
        assert "shield_restored" not in eat["result"]
    assert actions == expected


@pytest.mark.parametrize("scenario", ["lava_rim", "cliff", "airborne", "sprint_lava", "path_airborne", "close_respawn"])
def test_bug026_flee_probes_player_width_and_rechecks_each_landing(scenario):
    import math

    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    calls = []
    status = {
        "pos": [0.5, 64, 0.83], "alive": True, "ready": True, "on_ground": scenario != "airborne",
        "in_lava": False, "dimension": "the_nether", "last_death": {"t": 10},
    }

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                name = "air" if y >= 64 else "netherrack"
                if scenario == "lava_rim" and x >= 1 and z >= 1 and y == 63:
                    name = "lava"
                if scenario == "cliff" and (x != 0 or z not in {0, 1}) and y > 60:
                    name = "air"
                return {"name": name}
            calls.append((action, body))
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                ux, uz = yaw_to_unit_xz(status["yaw"])
                status["pos"] = [0.5 + 2.26 * ux, 64, 0.83 + 2.26 * uz]
                if scenario == "sprint_lava":
                    status.update(in_lava=True, on_ground=False)
                if scenario == "close_respawn":
                    status["last_death"] = {"t": 20}
            if action == "move_to" and scenario == "path_airborne":
                assert body["y"] == 64
                status.update(pos=[16.82, 50, 0.83], on_ground=False)
            return {"ok": True}

    result = CombatEnv(RetreatBot()).flee([-3, 64, 0.83], allow_shaft_seal=False)
    if scenario == "lava_rim":
        assert result["ok"]
        assert status["yaw"] != pytest.approx(-math.pi / 2)
    elif scenario in {"cliff", "airborne"}:
        assert not result["ok"]
        assert not calls
    elif scenario == "close_respawn":
        assert not result["ok"]
        assert result["respawned"] is True
        assert result["moved_dist"] == 0.0
        assert result["pos_after"] == status["pos"]
        assert [action for action, _ in calls] == ["look", "control"]
    else:
        assert not result["ok"]
        assert result["reason"] == "unsafe_landing"
        assert [action for action, _ in calls] == (
            ["look", "control"] if scenario == "sprint_lava" else ["look", "control", "move_to"]
        )


def test_r23_recent_arrow_hit_keeps_shield_up_when_nearest_mob_is_melee(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    state = {
        "health": 14, "food": 20, "blocking": True, "pos": [0.5, 81, 0.5],
        "nearest_hostile": {"name": "zombie", "dist": 3.0},
        "alive": True, "ready": True, "on_ground": True,
    }
    fresh = [{"seq": 1, "type": "hurt", "source": "mob_attack"}]
    calls = []

    class DuringRetreat:
        def defend_or_flee(self, **kwargs):
            state.update(health=3.9, food=17, nearest_hostile={"name": "zombie", "dist": 10.3})
            fresh.append({"seq": 2, "type": "hurt", "source": "arrow"})
            return {"ok": True, "mode": "flee"}

    class VillageBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(state)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [e for e in fresh if e["seq"] > since], "last_seq": fresh[-1]["seq"]}
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}, {"name": "iron_sword", "count": 1}]}
            calls.append((action, body))
            if action == "use_item":
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    guard = GenerationGuard(VillageBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=DuringRetreat())
    assert guard.poll()["event_seqs"] == [1, 2]

    clock[0] += 6.0
    assert guard.poll() is None
    assert calls == []

    clock[0] += 3.0
    incident = guard.poll()
    assert calls == [("posture", {"shield": False}), ("use_item", {"item": "bread"})]
    assert incident["requires_replan"] is True
    assert "fresh_hurt" in incident["reasons"]


@pytest.mark.parametrize("blocking", [False, True])
def test_r24_airborne_arrow_hit_faces_shooter_without_jumping_or_blind_flee(blocking):
    calls = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-7807.3, 102.2, -2813.7], "health": 5.9, "food": 16,
                    "alive": True, "ready": True, "on_ground": False, "blocking": blocking,
                    "dimension": "overworld", "nearest_hostile": {"id": 42, "name": "skeleton", "dist": 7.8},
                }
            if action == "events":
                since = kwargs["params"]["since"]
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": {"id": 42}}
                return {"events": [hurt] if since < 1 else [], "last_seq": 1}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            calls.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    expected = [("look", {"entity_id": 42})]
    if not blocking:
        expected.append(("posture", {"shield": True, "jump": False, "sneak": False}))
    assert calls == expected
    retreat = incident["actions"][-1]
    assert retreat["action"] == "combat_or_flee"
    assert retreat["ok"] is False
    assert retreat["result"]["reason"] == "unsafe_landing"


@pytest.mark.parametrize("landing", [
    "landed", "late_landing", "five_hp_late_landing", "airborne", "respawned", "no_filler", "no_wall", "no_cover",
])
def test_unshielded_arrow_knockback_retries_cover_if_landed_with_no_retreat(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    late_landing = landing in {"late_landing", "five_hp_late_landing"}
    landing_at = 1000.45 if late_landing else 1000.15
    status = {
        "pos": [-7918.3, 44.6, 2790.6], "health": {"late_landing": 2.2, "five_hp_late_landing": 5.0}.get(landing, 7.3),
        "food": 20,
        "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 1},
        "nearest_hostile": {"name": "skeleton", "dist": 7.0},
    }
    cover_calls = []
    natural_checks = []
    fallback = landing in {"no_filler", "no_wall", "no_cover"}

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= landing_at and landing != "airborne":
                    status.update(pos=[-7918.3, 44, 2790.6], on_ground=True)
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow"}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 43}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities" and fallback:
                return {"entities": [{"id": 42, "name": "skeleton", "dist": 7.0, "pos": [-7911.3, 44, 2790.6]}]}
            raise AssertionError(action)

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def no_route(*args, **kwargs):
        assert status["on_ground"] is True
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        return None

    def dig_cover(skills):
        assert status["on_ground"] is True
        assert status["last_death"] == {"t": 1}
        cover_calls.append(status["pos"])
        if fallback:
            return {"ok": False, "reason": "no_filler_block" if landing == "no_filler" else "no_solid_wall"}
        return {"ok": True, "mode": "cover_pocket"}

    def natural_cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters):
        assert status["on_ground"] and not allow_stationary and verify_live_shooters
        assert not other_shooters and pos == [-7911.3, 44, 2790.6]
        natural_checks.append(pos)
        if landing != "no_cover":
            return {"ok": True, "mode": "natural_cover", "pos_after": [-7920.5, 44, 2790.6]}
        return None

    monkeypatch.setattr(guard.combat_env, "probe_retreat", no_route)
    monkeypatch.setattr(guard.combat_env, "dig_cover_pocket", dig_cover)
    monkeypatch.setattr("mc_agent.skills.combat_env.CombatEnv.seek_natural_cover", natural_cover)
    incident = guard.poll()
    assert cover_calls == ([[-7918.3, 44, 2790.6]] if landing not in {"airborne", "respawned"} else [])
    assert len(natural_checks) == int(fallback)
    if fallback:
        assert [a["action"] for a in incident["actions"]] == ["combat_or_flee", "dig_cover", "natural_cover"]
        assert incident["actions"][-1]["ok"] is (landing != "no_cover")


@pytest.mark.parametrize("respawned", [False, True])
def test_low_health_shield_faces_the_shooter_again_after_retreat(respawned):
    status = {
        "pos": [-11014.4, 76, -11004.5], "yaw": 0.1, "health": 1.0, "food": 17, "ready": True,
        "alive": True, "on_ground": True, "blocking": True, "last_death": {"t": 10},
        "nearest_hostile": {"id": 42, "name": "skeleton", "dist": 8.0},
    }
    movements = []

    class ShieldedBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": {"id": 42, "name": "skeleton"}}
                return {"events": [hurt] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield", "count": 1}}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 76 else "air"}
            if action == "look":
                if "entity_id" in body:
                    assert body["entity_id"] == 42
                    status["yaw"] = -0.267
                    movements.append("face_shooter")
                else:
                    status["yaw"] = body["yaw"]
                    movements.append("turn_to_retreat")
            if action == "control" and body.get("forward"):
                movements.append("sprint")
                status["pos"] = [-11014.3, 76, -11006.8]
                if respawned:
                    status.update(health=20, last_death={"t": 20})
            if action == "move_to":
                movements.append("move_to")
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(ShieldedBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    names = [action["action"] for action in incident["actions"]]
    assert movements[:3] == ["face_shooter", "turn_to_retreat", "sprint"]
    if respawned:
        assert "face_attacker_after_retreat" not in names
        assert movements[-1] == "sprint"
    else:
        assert names[-1] == "face_attacker_after_retreat"
        assert movements[-1] == "face_shooter"
        assert status["yaw"] == -0.267


@pytest.mark.parametrize("landing", ["grounded", "still_airborne", "respawned"])
@pytest.mark.parametrize("entry", [
    "poll_ranged", "poll_melee", "subcortical_ranged", "subcortical_melee", "subcortical_armed_melee",
])
def test_knockback_waits_briefly_for_ground_before_retreat(monkeypatch, landing, entry):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {
        "pos": [-5191.8, 69.6, -7616.0], "health": 7.7, "food": 20, "ready": True, "alive": True,
        "on_ground": False, "dimension": "overworld", "last_death": {"t": 10},
        "nearest_hostile": {"id": 42, "name": "skeleton", "dist": 9.0, "pos": [-5182.5, 70.0, -7616.2]},
    }
    melee = entry.endswith("_melee")
    armed_swarm = entry == "subcortical_armed_melee"
    hurt_source = "mob_attack" if melee else "arrow"
    if melee:
        status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 0.6, "pos": [-5191.2, 69.6, -7616.0]}
    if armed_swarm:
        status.update(health=11, held={"name": "stone_sword"})
        status["nearest_hostile"].update(name="zombie_villager", dist=1.4, pos=[-5190.4, 69.6, -7616.0])
    moves = []

    class KnockbackBot:
        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= 1000.15:
                    if landing == "grounded":
                        status.update(pos=[-5192.2, 69.0, -7616.0], on_ground=True)
                    elif landing == "respawned":
                        status.update(pos=[-5184.5, 70.1, -7614.5], health=20, on_ground=True, last_death={"t": 20})
                return dict(status)
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": hurt_source}] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}] if armed_swarm else []}
            if action == "get_entities" and armed_swarm:
                return {"entities": [
                    status["nearest_hostile"],
                    {"id": 43, "name": "zombie", "dist": 4.0, "pos": [-5187.8, 69.6, -7616.0]},
                    {"id": 44, "name": "skeleton", "dist": 7.0, "pos": [-5184.8, 69.6, -7616.0]},
                ]}
            if action == "attack" and armed_swarm:
                pytest.fail("the armed bot must disengage from the group")
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 69 else "air"}
            if action == "move_to" or (action == "control" and body.get("forward")):
                assert status["on_ground"] is True
                moves.append(action)
            return {"ok": True}

    bot = KnockbackBot()
    if entry.startswith("poll_"):
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        incident = guard.poll()
        retreat = next(a["result"] for a in incident["actions"] if a["action"] == "combat_or_flee")
    else:
        outcome = CombatControllerSlot().handle(
            CombatEnv(bot), {
                "source": "pre_hurt_melee" if melee else "pre_hurt_ranged",
                "primary_type": status["nearest_hostile"]["name"],
                "primary_entity_id": 42,
            },
        )
        retreat = next(a for a in outcome["actions"] if a["action"] == "flee")
    assert bool(moves) is (landing == "grounded")
    assert clock[0] <= 1000.45
    if landing == "respawned":
        assert retreat["respawned"] is True


@pytest.mark.parametrize("landing", ["late_ground", "airborne", "respawned", "water", "has_roof", "offhand_roof"])
def test_bug152_retreat_waits_for_landing_and_skips_missing_roof(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    zombie = {"id": 2199410, "name": "zombie", "dist": 1.2, "pos": [209589.5, 63, 222063.7]}
    status = {
        "pos": [209588.3, 63, 222063.7], "health": 4.166666, "food": 19, "ready": True, "alive": True,
        "on_ground": True, "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    attempts = []
    has_roof = landing in {"has_roof", "offhand_roof"}

    class FieldBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if status["on_ground"] is False and clock[0] >= 1000.35:
                    if landing == "late_ground" or has_roof:
                        status.update(pos=[209588.3, 63, 222063.7], on_ground=True)
                    elif landing == "respawned":
                        status.update(pos=[209598.5, 65.1, 222067.5], health=20, last_death={"t": 2})
                    elif landing == "water":
                        status.update(in_water=True, submerged=True)
                return dict(status)
            if action == "events":
                event = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [] if kwargs["params"]["since"] else [event]}
            if action == "inventory":
                if not attempts:
                    status.update(pos=[209588.3, 63.2, 222063.7], on_ground=False)
                roof = {"name": "dirt", "count": 1}
                return {
                    "items": [roof] if landing == "has_roof" else [],
                    "offhand": roof if landing == "offhand_roof" else None,
                }
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 63 else "air"}
            if action == "move_to" or action == "control" and body.get("forward"):
                assert status["on_ground"] is True and not status.get("in_water")
                assert status["last_death"] == {"t": 1}
                attempts.append("retreat")
                status.update(pos=[209584.9, 63, 222063.7], nearest_hostile=None)
            return {"ok": True}

    def bunker(_skills, *, urgent):
        assert urgent
        attempts.append("bunker")
        return {"ok": False, "reason": "not_grounded"}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", bunker)
    guard = GenerationGuard(FieldBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_inside_own_shaft", lambda: False)
    if landing == "water":
        monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", lambda *_a, **_kw: {"ok": False, "in_water": True})
    incident = guard.poll()
    retreat = next(action["result"] for action in incident["actions"] if action["action"] == "combat_or_flee")

    if landing == "late_ground" or has_roof:
        assert "retreat" in attempts and retreat["ok"]
    else:
        assert attempts == [] and not retreat["ok"]
    assert ("bunker" in attempts) is has_roof
    assert clock[0] <= 1000.65


@pytest.mark.parametrize("primary_name, primary_distance", [("zombie", 0.8), ("creeper", 3.8), ("creeper", 2.1)])
def test_retreat_avoids_another_attacker_when_it_has_room(primary_name, primary_distance):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    primary = {"id": 42, "name": primary_name, "dist": primary_distance, "pos": [0.5 - primary_distance, 74, 0.5]}
    secondary = {"id": 43, "name": "zombie", "dist": 3.0, "pos": [3.5, 74, 0.5]}
    spider = {"id": 44, "name": "spider", "dist": 4.0, "pos": [0.5, 74, -3.5]}
    status = {
        "pos": [0.5, 74, 0.5], "health": 20, "ready": True, "alive": True,
        "on_ground": True, "is_day": True, "dimension": "overworld",
        "nearest_hostile": primary, "blocking": True, "last_death": {"t": 1},
    }
    forward = []

    class SwarmBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]}
            if action == "get_entities":
                return {"entities": [primary, secondary, spider]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 74 else "air"}
            if action == "look" and "yaw" in body:
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, y, z = status["pos"]
                status["pos"] = [x + 3.0 * dx, y, z + 3.0 * dz]
                forward.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    source = "pre_hurt_creeper" if primary_name == "creeper" else "pre_hurt_melee"
    outcome = CombatControllerSlot().handle(
        CombatEnv(SwarmBot()), {"source": source, "primary_entity_id": 42, "primary_type": primary_name}
    )
    assert outcome["mode"] == ("creeper_evade" if primary_name == "creeper" else "cover_or_flee")
    assert len(forward) == 1
    if primary_name == "creeper" and primary_distance == 2.1:
        assert forward[0] == pytest.approx([3.5, 74, 0.5])
    else:
        assert forward[0][2] > 2.0
        if primary_name == "creeper":
            assert math.dist(forward[0], primary["pos"]) > 5.5


@pytest.mark.parametrize("archer_blocked", [False, True])
def test_unshielded_melee_retreat_avoids_an_exposed_archer_on_the_escape_line(archer_blocked):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [17058.2, 26, 29971.9]
    spider = {"id": 224968, "name": "spider", "pos": [17059.9, 26, 29971.9], "dist": 1.7}
    archer = {"id": 224969, "name": "skeleton", "pos": [17054.0, 26, 29971.9], "dist": 4.2,
              "blocked_by_wall": archer_blocked}
    blocked = {"id": 224970, "name": "skeleton", "pos": [17058.2, 26, 29979.9], "dist": 8,
               "blocked_by_wall": True}
    status = {"pos": origin, "health": 13, "food": 20, "ready": True, "alive": True, "on_ground": True,
              "dimension": "overworld", "nearest_hostile": spider}
    moves = []

    class TunnelBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [spider, archer, blocked]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 26 else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, y, z = status["pos"]
                status["pos"] = [x + 2.3 * dx, y, z + 2.3 * dz]
                moves.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body[k] for k in ("x", "y", "z")]
                moves.append(status["pos"])
            return {"ok": True}

    result = CombatControllerSlot().handle(CombatEnv(TunnelBot()), {
        "source": "pre_hurt_melee", "primary_type": "spider", "primary_entity_id": spider["id"],
    })

    assert result["mode"] == "cover_or_flee" and result["actions"][0]["ok"]
    assert math.dist(moves[0], spider["pos"]) > spider["dist"]
    assert (math.dist(moves[0], archer["pos"]) < 3.0) is archer_blocked


@pytest.mark.parametrize("landing", ["grounded", "still_airborne", "water", "respawned"])
def test_unarmed_retreat_confirms_landing_after_short_pulse(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    threat = {"id": 42, "name": "spider", "dist": 2.0, "pos": [2.5, 74, 0.5]}
    status = {
        "pos": [0.5, 74, 0.5], "health": 14, "ready": True, "alive": True, "on_ground": True,
        "is_day": True, "nearest_hostile": threat, "last_death": {"t": 1},
    }
    mutations = []

    class RetreatBot:
        def status(self):
            if clock[0] >= 1000.15 and status.get("on_ground") is False:
                if landing == "grounded":
                    status.update(pos=[-1.8, 74, 0.5], on_ground=True)
                elif landing == "water":
                    status.update(in_water=True)
                elif landing == "respawned":
                    status.update(pos=[0.5, 74, 4.5], on_ground=True, last_death={"t": 2})
            return dict(status)

        def inventory(self):
            return {"items": []}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_entities":
                return {"entities": [threat]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 74 else "air"}
            if action == "control" and body.get("forward"):
                assert status["on_ground"] is True
                mutations.append(action)
                status.update(pos=[-1.66, 74.6, 0.5], on_ground=False)
            if action == "move_to":
                assert status["on_ground"] is True
                mutations.append(action)
                status.update(pos=[body["x"], body["y"], body["z"]])
            return {"ok": True}

    env = CombatEnv(RetreatBot())
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "spider"}
    )
    retreat = next(a for a in outcome["actions"] if a["action"] == "flee")
    assert clock[0] <= 1000.45
    assert retreat["ok"] is (landing == "grounded")
    assert mutations == (["control", "move_to"] if landing == "grounded" else ["control"])
    assert bool(retreat.get("respawned")) is (landing == "respawned")

