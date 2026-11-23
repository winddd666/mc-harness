from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("next_event", ["burn_only", "burn_with_bucket", "melee"])
def test_post_lava_burning_does_not_treat_a_nearby_drowned_as_the_attacker(monkeypatch, next_event):
    drowned = {"id": 44, "name": "drowned", "dist": 3.3}
    hits = [{"seq": 1, "type": "hurt", "source": "lava"}]
    defenses = []
    douses = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [9150.3, 67, -11277.3], "health": 8.24, "food": 17,
                    "ready": True, "alive": True, "on_ground": True, "dimension": "overworld",
                    "blocking": True, "on_fire": True, "nearest_hostile": drowned,
                }
            if action == "events":
                return {"events": [h for h in hits if h["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                items = [{"name": "diamond_sword", "count": 1}]
                if next_event == "burn_with_bucket":
                    items.append({"name": "water_bucket", "count": 1})
                return {"items": items, "offhand": {"name": "shield"}}
            return {"ok": True}

    def retreat(_status, attacker, _counts):
        defenses.append(attacker)
        return {"ok": True, "mode": "flee"}

    def douse(mode):
        douses.append(mode)
        return {"ok": True}

    guard = GenerationGuard(RidgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    hits.append({"seq": 2, "type": "hurt", "source": "on_fire", "likely_attacker": drowned})
    if next_event == "melee":
        hits.append({"seq": 3, "type": "hurt", "source": "mob_attack", "source_entity": drowned})
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    monkeypatch.setattr(guard.combat_env, "build_tactical", douse)
    incident = guard.poll()

    assert defenses == ([drowned] if next_event == "melee" else [])
    assert douses == (["place_water_feet"] if next_event == "burn_with_bucket" else [])
    if next_event == "burn_only":
        assert incident is None


@pytest.mark.parametrize("hit", ["unattributed", "unattributed_later_page", "identified", "melee", "no_fire_context"])
def test_post_lava_fire_ticks_keep_adjacent_water_available_at_low_health(monkeypatch, hit):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 42, "name": "zombie", "dist": 9.0}
    status = {"pos": [6356.7, 22, 6515.3], "health": 14.0, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "on_fire": True, "dimension": "overworld", "nearest_hostile": zombie}
    events = [{"seq": 1, "type": "hurt", "source": "lava"}]
    attempts = []
    defenses = []

    class RimBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [row for row in events if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    def douse(snapshot):
        attempts.append(snapshot["health"])
        status.update(in_water=True, on_fire=False)
        return {"ok": True, "in_water": True}

    def defend(_status, attacker, _counts, **_kwargs):
        defenses.append(attacker)
        return {"ok": False, "mode": "flee"}

    guard = GenerationGuard(RimBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    monkeypatch.setattr(guard.combat_env, "douse_in_adjacent_water", douse)
    monkeypatch.setattr(guard, "_defend_or_flee", defend)

    events.extend([{"seq": 2, "type": "hurt", "source": "on_fire"}, {"seq": 3, "type": "hurt"}])
    assert guard.poll() is None
    assert attempts == [] and defenses == []

    clock[0] += 4.0
    status.update(health=8.0, on_fire=hit != "no_fire_context")
    if hit != "unattributed_later_page":
        events.append({"seq": 4, "type": "hurt", "source": "on_fire"})
    last = {"seq": 5, "type": "hurt"}
    if hit == "identified":
        last["source_entity"] = zombie
    elif hit == "melee":
        last.update(source="mob_attack", source_entity=zombie)
    events.append(last)

    incident = guard.poll()
    if hit in {"unattributed", "unattributed_later_page"}:
        assert attempts == [8.0] and defenses == []
        assert [step["action"] for step in incident["actions"]] == ["douse_in_water"]
    else:
        assert attempts == [] and len(defenses) == 1


@pytest.mark.parametrize(("stage", "submerged", "oxygen", "surface_needed"), [
    ("entry", False, 20, False), ("between_hits", False, 20, False), ("exit", False, 20, False),
    ("entry", None, 20, False), ("entry", None, 300, False), ("entry", None, 9, True), ("entry", False, 9, False),
    ("between_hits", None, 130, True), ("between_hits", True, 20, True),
    ("entry", False, 0, True), ("exit", True, 20, True), ("exit", False, 0, True),
])
def test_combat_surfaces_only_when_water_status_indicates_danger(monkeypatch, stage, submerged, oxygen, surface_needed):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    zombie = {"id": 24, "name": "zombie", "pos": [1.5, 63.0, 0.5], "dist": 1.0}
    status = {
        "pos": [0.5, 63, 0.5], "health": 18, "food": 20, "alive": True, "ready": True,
        "on_ground": True, "in_water": stage == "entry", "nearest_hostile": zombie,
    }
    calls = []

    class WaterCombatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "attack":
                calls.append((action, body))
                status["in_water"] = True
                finished = stage != "between_hits" or sum(name == "attack" for name, _ in calls) == 2
                return {"hits": 1, "killed": finished}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 63 else "air"}
            if action == "posture":
                calls.append((action, body))
            return {"ok": True}

    status["oxygen"] = oxygen
    if submerged is not None:
        status["submerged"] = submerged
    env = CombatEnv(WaterCombatBot())

    def surface(_status):
        calls.append(("surface", {}))
        return {"ok": True, "in_water": True}

    monkeypatch.setattr(env, "surface_from_water", surface)
    monkeypatch.setattr(env, "escape_deep_water_to_shore", lambda *_args, **_kwargs: {"ok": False})
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_entity_id": zombie["id"], "primary_type": "zombie", "distance": 1.0,
    })

    surfacing = stage != "exit" and surface_needed
    expected_attacks = 0 if stage == "entry" and surfacing else 2 if stage == "between_hits" and not surfacing else 1
    assert sum(name == "attack" for name, _ in calls) == expected_attacks
    assert any(name == "surface" for name, _ in calls) is surfacing
    assert (outcome["mode"] in {"water_surface", "water_surface_abort"}) is surfacing
    final_posture = [body for name, body in calls if name == "posture"][-1]
    assert bool(final_posture.get("jump")) is surface_needed


@pytest.mark.parametrize("ending", ["dry_bank", "respawn_on_shore", "respawn_during_float"])
def test_low_health_counter_kill_uses_direct_shore_under_melee_pressure(monkeypatch, ending):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    attacker = {"id": 1180912, "name": "zombie", "pos": [3.1, 63.0, 0.5], "dist": 2.6}
    status = {
        "pos": [0.5, 63, 0.5], "health": 11.5, "food": 15, "alive": True, "ready": True,
        "on_ground": False, "in_water": False, "dimension": "overworld", "nearest_hostile": attacker,
        "last_death": None,
    }
    calls = []

    class BankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            calls.append((action, body))
            if action == "attack":
                status.update(health=9.4, in_water=True, submerged=True, oxygen=19)
                if ending == "respawn_during_float":
                    status["nearest_hostile"] = {**attacker, "blocked_by_wall": True}
                return {"hits": 1, "killed": False}
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 3}],
                    "offhand": {"name": "shield", "count": 1},
                }
            if action == "get_entities":
                return {"entities": [attacker]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 63 else "air"}
            return {"ok": True}

    env = CombatEnv(BankBot())

    def respawn():
        status.update(in_water=False, submerged=False, on_ground=True, last_death={"t": 1}, health=20, nearest_hostile=None)
        return {"ok": False, "in_water": False, "respawned": True}

    def surface(_status):
        calls.append(("surface", {}))
        assert ending == "respawn_during_float"
        return respawn()

    def shore(pos, **kwargs):
        calls.append(("shore", kwargs))
        assert pos == attacker["pos"]
        assert kwargs == {
            "keep_shield": True, "shield_target_id": attacker["id"], "surface_first": True,
            "avoid_close_threat": True, "under_attack": True,
        }
        if ending == "respawn_on_shore":
            return respawn()
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(env, "surface_from_water", surface)
    monkeypatch.setattr(env, "escape_deep_water_to_shore", shore)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_entity_id": attacker["id"], "primary_type": "zombie", "distance": 2.8,
    })

    after_first_hit = [name for name, _ in calls][next(i for i, (name, _) in enumerate(calls) if name == "attack") + 1:]
    if ending == "dry_bank":
        assert after_first_hit == ["shore", "look", "posture"]
        assert outcome["mode"] == "water_surface_abort" and outcome["latched_guard"] is True
    else:
        assert after_first_hit == (["surface"] if ending == "respawn_during_float" else ["shore"])
        assert outcome["mode"] == "life_unavailable"
        assert outcome["ok"] is False and outcome["handled"] is False and not outcome["latched_guard"]


def test_r21_water_surface_abort_latches_shield_and_post_combat_eat_skips_ranged():
    """Reproduces r21_short hy4_hy_3_midgame: water_surface_abort near drowned latches shield & skips ranged/latched post-combat eat."""
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    water_calls: list[tuple[str, dict]] = []
    water_state = {"in_water": False, "submerged": False, "oxygen": 100, "hp": 15.0}

    class _DrownedWaterSkills:
        def escape_water(self):
            water_state["in_water"] = False
            water_state["submerged"] = False
            water_state["hp"] = 3.4
            return {"ok": True, "in_water": False}

    class _DrownedAbortBot:
        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            water_calls.append((action, pl))
            if action == "status":
                return {
                    "pos": [0.5, 63.0, 0.5],
                    "health": water_state["hp"],
                    "food": 17,
                    "oxygen": water_state["oxygen"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": water_state["in_water"],
                    "submerged": water_state["submerged"],
                    "in_lava": False,
                    "nearest_hostile": {"id": 665559, "name": "drowned", "pos": [1.5, 63.0, 0.5], "dist": 1.1},
                }
            if action == "attack":
                # Match hy4_hy_3 seq 199: plunging after drowned pulls bot underwater mid counter_kill
                water_state["in_water"] = True
                water_state["submerged"] = True
                return {"ok": True, "hits": 1, "killed": False}
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 4}],
                    "offhand": {"name": "shield", "count": 1},
                }
            if action == "get_entities":
                return {"entities": [{"id": 665559, "name": "drowned", "pos": [1.5, 63.0, 0.5], "dist": 1.1}]}
            if action == "get_block":
                p = pl.get("pos", [0, 63, 0])
                return {"name": "stone" if p[1] < 63 else "air"}
            return {"ok": True}

    out_w = CombatControllerSlot().handle(
        CombatEnv(_DrownedAbortBot(), skills=_DrownedWaterSkills()),
        {"source": "pre_hurt_melee", "primary_entity_id": 665559, "primary_type": "drowned", "distance": 2.3},
    )
    assert out_w["mode"] == "water_surface_abort"
    assert out_w["latched_guard"] is True
    assert [c for c in water_calls if c[0] == "posture"][-1] == (
        "posture",
        {"shield": True, "sneak": False, "jump": False},
    )
    assert all(act != "use_item" for act, _ in water_calls)

    # Also verify SubcorticalInstinctController post_combat_eat skips when ranged skeleton is at 9.0m
    ranged_post_calls: list[str] = []

    class _PostRangedSkipBot:
        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            ranged_post_calls.append(action)
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": 13.0,
                    "food": 14,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "nearest_hostile": {"id": 88, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0},
                }
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 4}]}
            if action == "get_entities":
                return {"entities": [{"id": 88, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0}]}
            if action == "get_block":
                return {"name": "stone" if (body or {}).get("pos", [0, 64, 0])[1] < 64 else "air"}
            return {"ok": True}

    CombatControllerSlot().handle(
        CombatEnv(_PostRangedSkipBot()),
        {"source": "pre_hurt_ranged", "primary_entity_id": 88, "primary_type": "skeleton", "distance": 9.0},
    )
    assert "use_item" not in ranged_post_calls


def test_r22_shore_query_bypasses_lakebed_and_failed_shore_backs_off_cooldown(monkeypatch):
    """Reproduces r22_short hy4_hy_7_village:
    1) Surface bank query finds dry grass_block/dirt_path shore even when oceanbed sand/dirt fills generic limit=36.
    2) When escape_to_shore fails inside 30s water_avoid_cooldown without new hurt, poll backs off 8.0s instead of spamming cancels every 5s.
    """
    from mc_agent.skills.combat_env import CombatEnv

    # 1) Surface bank query finds dry grass_block shore when generic SHORE_LAND_BLOCKS query returns underwater riverbed sand
    move_goals: list[dict] = []

    class _RiverbedBot:
        def status(self):
            return {
                "pos": [0.5, 62.2, 0.5],
                "health": 16.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "in_water": True,
                "submerged": False,
                "oxygen": 300,
                "yaw": 0.0,
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "find_blocks":
                types = set(pl.get("types") or [])
                if "sand" in types or "dirt" in types:
                    # Generic riverbed blocks at y=58 (filtered out by min_y=61)
                    return {"blocks": [{"name": "sand", "pos": [i, 58, 0]} for i in range(40)]}
                # Surface bank query finds grass_block at [6, 62, 0] -> walkable [6.5, 63.0, 0.5]
                return {"blocks": [{"name": "grass_block", "pos": [6, 62, 0]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                move_goals.append(pl)
                return {"ok": True}
            return {"ok": True}

    env_river = CombatEnv(_RiverbedBot())
    # Bypass final in_water flip only to inspect selected shore_target and move_to range
    out_river = env_river.escape_deep_water_to_shore(keep_shield=True)
    assert out_river["shore_target"] == [6.5, 63.0, 0.5]
    assert move_goals and move_goals[0]["range"] <= 0.6

    # 2) Failed escape_to_shore during 30s cooldown backs off 8.0s when no new hurt / drowning occurs
    clock = [8000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    class _WideWaterBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 62.2, 0.5],
                    "health": 14.0,
                    "food": 20,
                    "oxygen": 280,
                    "alive": True,
                    "ready": True,
                    "in_water": True,
                    "submerged": False,
                    "in_lava": False,
                    "blocking": False,
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            return {"ok": True}

    guard = GenerationGuard(_WideWaterBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._no_water_until = clock[0] + 30.0
    inc_first = guard.poll()
    assert inc_first is not None
    assert "water_avoid_cooldown" in inc_first["reasons"]
    assert guard._last_water_escape_failed is True

    # +5.0s later (previously spammed cancel every ~5s) -> still in 8.0s backoff!
    clock[0] += 5.0
    assert guard.poll() is None

    # +3.5s more (total 8.5s >= 8.0s) -> resumes retry cleanly
    clock[0] += 3.5
    inc_retry = guard.poll()
    assert inc_retry is not None
    assert "water_avoid_cooldown" in inc_retry["reasons"]


@pytest.mark.parametrize("landing", ["water", "lava", "respawn", "retreat_respawn", "new_dimension", "not_ready"])
def test_creeper_retreat_hands_off_to_water_escape_in_the_same_life(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    creeper = {"id": 42, "name": "creeper", "pos": [0.5, 62, 4.5], "dist": 4.0}
    status = {
        "pos": [0.5, 62.5, 0.5], "health": 20, "on_ground": False,
        "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1},
        "nearest_hostile": creeper,
    }
    shore_calls = []
    scans = []

    class BankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                scans.append(body["radius"])
                return {"entities": [creeper]}
            if action == "get_block":
                return {"name": "air"}
            return {"ok": True}

    def invalid_retreat(*_args, **_kwargs):
        status.update(pos=[0.5, 61.3, 0.5], in_water=True, in_lava=landing == "lava")
        if landing == "respawn":
            status["last_death"] = {"t": 2}
        elif landing == "new_dimension":
            status["dimension"] = "the_nether"
        elif landing == "not_ready":
            status["ready"] = False
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0,
                "respawned": landing == "retreat_respawn"}

    def reach_shore(threat_pos, **kwargs):
        shore_calls.append((threat_pos, kwargs))
        status.update(pos=[0.5, 62, -5.5], in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    env = CombatEnv(BankBot())
    monkeypatch.setattr(env, "flee", invalid_retreat)
    monkeypatch.setattr(env, "escape_deep_water_to_shore", reach_shore)
    result = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42},
    )

    assert result["mode"] == "creeper_evade"
    assert scans == ([16, 8] if landing in {"water", "lava"} else [16])
    retreat = next(step for step in result["actions"] if step["action"] == "retreat_from_creeper")
    assert bool(retreat.get("respawned")) is (landing in {"respawn", "retreat_respawn", "new_dimension"})
    if landing == "water":
        assert shore_calls == [(creeper["pos"], {"keep_shield": True, "shield_target_id": 42, "avoid_close_threat": True})]
        assert result["status_after"]["in_water"] is False
    else:
        assert shore_calls == []


@pytest.mark.parametrize(
    "entry, scenario",
    [
        ("boundary", "surface"),
        ("boundary", "close_respawn"),
        ("boundary", "macro_respawn"),
        ("boundary", "macro_close_respawn"),
        ("boundary", "negative_oxygen"),
        ("poll", "surface"),
        ("poll", "oxygen_only"),
        ("poll", "negative_oxygen"),
        ("preemption", "oxygen_only"),
        ("preemption", "negative_oxygen"),
    ],
)
def test_guardian_drowning_surfaces_first_and_checks_respawn(entry, scenario, monkeypatch):
    events = []
    calls = []
    macro = scenario.startswith("macro_")
    state = {
        "pos": [9971.3, 47.2, 2897.3], "health": 18, "oxygen": 0,
        "in_water": True, "submerged": True, "on_ground": False, "jumping": True,
        "ready": True, "alive": True, "last_death": {"t": 10},
    }

    class WaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(state)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "drown"}
                return {"events": [] if macro or scenario in {"oxygen_only", "negative_oxygen"} else [hurt]}
            if action == "inventory":
                return {"items": []}
            calls.append((action, body))
            if action == "control":
                assert body["jump"] is True and body["forward"] is False and body["sprint"] is False
                state.update(in_water=False, submerged=False, on_ground=True, pos=[9971.3, 49, 2897.3])
                if scenario == "close_respawn":
                    state.update(health=20, last_death={"t": 20})
            return {"ok": True}

    class EscapeMacro:
        def escape_water(self):
            assert macro
            pos = [9974.5, 62.1, 2978.5] if scenario == "macro_respawn" else [9971.3, 49, 2897.3]
            state.update(in_water=False, pos=pos, last_death={"t": 20})
            return {"ok": True, "in_water": False, "pos_after": state["pos"]}

    if macro:
        state["oxygen"] = 100
    elif scenario == "oxygen_only":
        state["submerged"] = False
    elif scenario == "negative_oxygen":
        state["oxygen"] = -1
    guard = GenerationGuard(WaterBot(), EventSink(events.append), checkpoint=lambda: None, skills=EscapeMacro())
    if entry == "poll":
        monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
        guard.last_reaction = 999.5
        assert guard.poll() is not None
    elif entry == "preemption":
        receipt = guard.handle_preemption({"source": "tool_interrupted_hurt"}, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface"
        assert receipt["status_after"]["in_water"] is False
    else:
        note = guard.stabilize_after_tools()
    actions = [e for e in events if e["type"] == "guardian.action.completed"]
    assert len(actions) == 1
    if macro:
        assert actions[0]["action"] == "boundary_escape_water"
        assert actions[0]["result"]["respawned"] is True
        assert actions[0]["ok"] is False
        assert note is None
        assert calls == []
    else:
        assert actions[0]["action"] == ("boundary_escape_to_shore" if entry == "boundary" else "escape_to_shore")
        assert [action for action, _ in calls] == ["posture", "control"]
        assert actions[0]["ok"] is (scenario != "close_respawn")
        assert actions[0]["result"]["mode"] == ("respawned" if scenario == "close_respawn" else "surfaced")


@pytest.mark.parametrize(
    "water_state", ["surfaced", "submerged", "drowning", "failed_unstick", "unstick_timeout", "dry_after_unstick_timeout"],
)
def test_tool_boundary_does_not_repeat_water_macro_after_its_own_reflex(monkeypatch, water_state):
    status = {"pos": [6378.7, 62.2, -6442.3], "health": 17, "food": 20, "oxygen": 180,
              "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True}
    calls = []

    class WetBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(WetBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def failed_water_macro(_status):
        calls.append("water_macro")
        return {"ok": False, "in_water": True}

    def failed_shore(*args, surface_first=False, **kwargs):
        calls.append("urgent_shore" if surface_first else "shore")
        return {"ok": False, "in_water": True}

    monkeypatch.setattr(guard, "_escape_water", failed_water_macro)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", failed_shore)
    if water_state == "failed_unstick":
        status.update(submerged=False)
        receipt = {"ok": False, "in_water": True, "error": {"code": "water_escape_failed"}}
    elif water_state in {"unstick_timeout", "dry_after_unstick_timeout"}:
        status.update(in_water=water_state == "unstick_timeout", submerged=False)
        receipt = {"ok": False, "interrupted_tool": "mc_skill_unstick", "error": {"code": "time_budget"}}
    else:
        receipt = guard.handle_preemption({"source": "tool_interrupted_hurt"}, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface"
        assert calls == ["water_macro", "shore"]
        calls.clear()
        status.update(submerged=water_state != "surfaced", oxygen=0 if water_state == "drowning" else 180)

    guard.stabilize_after_tools([json.dumps(receipt)])

    expected = {
        "surfaced": [], "submerged": ["shore"], "drowning": ["urgent_shore"], "failed_unstick": ["shore"],
        "unstick_timeout": ["shore"], "dry_after_unstick_timeout": [],
    }
    assert calls == expected[water_state]


@pytest.mark.parametrize("reentry", ["still_submerged", "observed_dry", "retry_window_expired", "poll_timeout"])
def test_boundary_retries_shore_directly_after_water_macro_times_out(monkeypatch, reentry):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [6383.3, 56.2, -6537.3], "health": 18, "food": 20, "oxygen": 100,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
    }
    attempts = []

    class FloodedCaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            assert action == "stop"
            return {"ok": True}

    def timed_out_water_macro(_status):
        attempts.append("water_macro")
        raise httpx.ReadTimeout("water macro timed out")

    def missed_shore(*args, surface_first=False, **kwargs):
        attempts.append("surface_then_shore" if surface_first else "shore")
        return {"ok": False, "in_water": True}

    guard = GenerationGuard(FloodedCaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", timed_out_water_macro)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", missed_shore)
    if reentry == "poll_timeout":
        guard.poll()
        assert attempts == ["water_macro"]
    else:
        guard.stabilize_after_tools()
        assert attempts == ["water_macro", "shore"]
    attempts.clear()

    clock[0] += 31.0 if reentry == "retry_window_expired" else 4.25
    if reentry == "observed_dry":
        status.update(in_water=False, submerged=False, on_ground=True)
        guard.stabilize_after_tools()
        status.update(in_water=True, submerged=True, on_ground=False)
    guard.stabilize_after_tools()

    expected = ["surface_then_shore"] if reentry in {"still_submerged", "poll_timeout"} else ["water_macro", "shore"]
    assert attempts == expected


@pytest.mark.parametrize("after_shore", ["still_airborne", "landed", "fresh_hurt", "new_drowned"])
def test_water_attack_preemption_waits_for_shore_landing_before_retrying_combat(monkeypatch, after_shore):
    attacker = {"id": 44, "name": "drowned", "dist": 3.1}
    status = {
        "pos": [9056.5, 63, -11385.4], "health": 20, "food": 20, "ready": True, "alive": True,
        "on_ground": after_shore == "landed", "in_water": False,
        "nearest_hostile": {**attacker, "id": 45} if after_shore == "new_drowned" else attacker,
    }
    combat = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "trident"}
                return {"events": [hurt] if after_shore == "fresh_hurt" and kwargs["params"]["since"] < 1 else []}
            pytest.fail(f"Unexpected action on the bank: {action}")

    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_detect_pre_hurt_trigger", lambda st: {
        "source": "pre_hurt_melee", "primary_type": "drowned", "primary_entity_id": st["nearest_hostile"]["id"],
    })

    def react(env, trigger):
        combat.append(trigger["primary_entity_id"])
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", react)
    receipt = json.dumps({
        "preempted_by_reflex": True, "reflex_mode": "water_surface",
        "status_after": {"in_water": False, "on_ground": False, "nearest_hostile": attacker},
    })
    guard.stabilize_after_tools([receipt])

    assert combat == ([] if after_shore == "still_airborne" else [status["nearest_hostile"]["id"]])

