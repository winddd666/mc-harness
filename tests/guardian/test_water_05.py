from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_drowned_tool_preemption_bounds_float_before_shore_fallback(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [9067.9, 62.7, -11364.9], "health": 14.8, "oxygen": 200, "food": 19,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": 12.3},
    }
    calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                return {"events": []}
            if action == "move_to":
                calls.append((action, kwargs["timeout_s"]))
                clock[0] += kwargs["timeout_s"]
                raise httpx.ReadTimeout("float pathfinder exceeded its budget")
            calls.append((action, clock[0]))
            return {"ok": True}

    def slow_float(skills):
        return skills.bot.call("move_to", {"x": 9070.5, "y": 63, "z": -11351.5}, timeout_s=8.5)

    def reach_shore(*args, **kwargs):
        calls.append(("shore", clock[0]))
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(MacroSkills, "escape_water", slow_float)
    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": "drowned", "primary_entity_id": 44},
        interrupted_name="mc_skill_scout_surface",
    )

    assert calls == [("move_to", 4.0), ("stop", 1004.0), ("shore", 1004.0)]
    assert receipt["reflex_mode"] == "water_surface" and receipt["status_after"]["in_water"] is False


@pytest.mark.parametrize("has_shield", [False, True])
def test_tool_reflex_landing_in_water_near_drowned_heads_straight_to_shore(monkeypatch, has_shield):
    status = {"pos": [10935.7, 62.4, 11345.7], "health": 6.17, "oxygen": 230,
              "ready": True, "alive": True, "on_ground": False, "in_water": False,
              "last_death": {"t": 1}, "dimension": "overworld",
              "nearest_hostile": {"id": 44, "name": "drowned", "dist": 3.0}}
    threat_pos = [10932.7, 62.4, 11345.7]
    actions = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [{**status["nearest_hostile"], "pos": threat_pos}]}
            return {"ok": True}

    def combat(_env, trigger):
        status.update(in_water=True, offhand={"name": "shield"} if has_shield else None)
        return {"ok": False, "mode": "cover_or_flee", "status_after": dict(status)}

    def land(pos, *, keep_shield, shield_target_id, avoid_close_threat):
        actions.append((pos, keep_shield, shield_target_id, avoid_close_threat))
        status.update(in_water=False, on_ground=True, pos=[10939.5, 63, 11345.5])
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_slot, "handle", combat)
    monkeypatch.setattr(guard, "_escape_water", lambda *_: pytest.fail("close drowned requires immediate shore escape"))
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", land)
    trigger = {"source": "tool_interrupted_hurt", "primary_type": "drowned", "primary_entity_id": 44}

    result = guard.handle_preemption(trigger, interrupted_name="mc_skill_unstick")

    assert actions == [(threat_pos, has_shield, 44, True)]
    assert result["status_after"]["in_water"] is False
    assert result["status_after"]["on_ground"] is True


@pytest.mark.parametrize("respawned", [False, True])
def test_trident_preemption_restores_shield_facing_after_shore_attempt(respawned):
    status = {
        "pos": [-11031.7, 62.2, 11119.6], "in_water": True, "submerged": True, "on_ground": False,
        "health": 9.6, "oxygen": 240, "ready": True, "alive": True, "last_death": {"t": 10},
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": 7.0},
    }
    look_calls = []
    events = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield", "count": 1}}
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": [-11016, 62, 11119]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "look":
                look_calls.append(body)
            if action == "move_to":
                if respawned:
                    status.update(in_water=False, health=20, last_death={"t": 20})
                else:
                    status["pos"] = [-11023.0, 62.6, 11119.6]
                raise ApiError("path_not_found", "shore not reached")
            return {"ok": True}

    class WaterSkills:
        def escape_water(self):
            return {"ok": False, "in_water": True}

    guard = GenerationGuard(RiverBot(), EventSink(events.append), checkpoint=lambda: None, skills=WaterSkills())
    trigger = {
        "source": "tool_interrupted_hurt", "hurt_source": "trident", "primary_type": "drowned",
        "primary_entity_id": 44, "primary_pos": [-11038.7, 62.0, 11119.6],
    }
    guard.handle_preemption(trigger, interrupted_name="mc_skill_scout_surface")

    shore, = [e["result"] for e in events if e["type"] == "guardian.action.completed" and e["action"] == "escape_to_shore"]
    assert look_calls[0] == {"entity_id": 44}
    assert "yaw" in look_calls[1]
    assert shore["ok"] is False
    if respawned:
        assert shore["mode"] == "respawned"
        assert look_calls.count({"entity_id": 44}) == 1
    else:
        assert shore["in_water"] is True
        assert look_calls[-1] == {"entity_id": 44}
        assert shore["actions"][-1] == "face_water_attacker"


@pytest.mark.parametrize("entry", ["poll_float", "poll_hurt", "boundary", "tool_preemption"])
def test_critical_health_water_recovery_heads_to_shore_before_slow_float(monkeypatch, entry):
    status = {
        "pos": [-10955.9, 62.4, -10979.5], "health": 5.3, "food": 17, "oxygen": 180,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
    }
    calls = []

    class WetBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if entry == "poll_hurt" and kwargs["params"]["since"] < 1:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}]}
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    def slow_float(_status):
        calls.append("float")
        return {"ok": False, "in_water": True}

    def reach_shore(*args, **kwargs):
        calls.append("shore")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(WetBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", slow_float)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    if entry == "boundary":
        guard.stabilize_after_tools()
    elif entry == "tool_preemption":
        guard.handle_preemption({"source": "tool_interrupted_hurt"}, interrupted_name="mc_move_to")
    else:
        guard.poll()

    assert calls == ["shore"]
    assert not status["in_water"]


@pytest.mark.parametrize("recent_engagement", [True, False])
def test_poll_enters_water_near_recent_shooter_before_next_hurt(monkeypatch, recent_engagement):
    status = {
        "pos": [0.5, 62.2, 0.5], "health": 17.64, "food": 20, "oxygen": 180,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "pillager", "dist": 4.3},
    }
    shooter_pos = [4.8, 62.2, 0.5]
    calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield", "count": 1}}
            if action == "get_entities":
                return {"entities": [{"id": 44, "name": "pillager", "pos": shooter_pos}]}
            calls.append((action, body))
            return {"ok": True}

    def leave_water():
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def float_to_surface(_status):
        calls.append(("float", None))
        return leave_water()

    def reach_shore(attacker_pos, *, keep_shield, shield_target_id):
        calls.append(("shore", (attacker_pos, keep_shield, shield_target_id)))
        return leave_water()

    now = 1000.0
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: now)
    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    elapsed = 3.2 if recent_engagement else guard.RANGED_COMBAT_STANCE_S + 1
    guard._mark_ranged_engagement(now - elapsed, attacker=status["nearest_hostile"])
    monkeypatch.setattr(guard, "_escape_water", float_to_surface)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    guard.poll()

    if recent_engagement:
        assert calls == [
            ("look", {"entity_id": 44}),
            ("posture", {"shield": True, "jump": True, "sneak": False}),
            ("shore", (shooter_pos, True, 44)),
        ]
    else:
        assert calls == [("float", None)]
    assert not status["in_water"]


@pytest.mark.parametrize("respawned", [False, True])
def test_ranged_knockback_into_water_goes_to_shore_in_same_poll(monkeypatch, respawned):
    shooter = {"id": 44, "name": "skeleton", "dist": 6.0, "pos": [6.5, 67.0, 0.5]}
    status = {
        "pos": [0.5, 63.2, 0.5], "health": 9.5, "food": 20, "oxygen": 400,
        "in_water": False, "on_ground": False, "alive": True, "ready": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": shooter,
    }
    shore_calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": "arrow"}]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    def fall_into_water(_pos, *, wait_for_ground):
        assert wait_for_ground is True
        status.update(in_water=True, health=1.5, pos=[-1.5, 62.6, 0.5])
        if respawned:
            status["last_death"] = {"t": 2}
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0}

    def reach_shore(attacker_pos, *, keep_shield, shield_target_id, surface_first):
        shore_calls.append((attacker_pos, keep_shield, shield_target_id, surface_first))
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", fall_into_water)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    incident = guard.poll()

    names = [action["action"] for action in incident["actions"]]
    if respawned:
        assert names == ["combat_or_flee"]
        assert shore_calls == []
    else:
        assert names == ["combat_or_flee", "escape_to_shore"]
        assert shore_calls == [(shooter["pos"], False, 44, False)]
        assert incident["status"]["in_water"] is False


@pytest.mark.parametrize("landing", ["water", "dry", "respawned", "lava"])
def test_melee_retreat_back_into_water_goes_to_shore_in_same_poll(monkeypatch, landing):
    zombie = {"id": 44, "name": "zombie", "dist": 1.5, "pos": [11084, 63, -11050]}
    status = {
        "pos": [11085.4, 63, -11050.4], "health": 10.18, "food": 20, "oxygen": 285,
        "in_water": False, "on_ground": True, "alive": True, "ready": True, "blocking": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    shore_calls = []
    lava_calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            raise AssertionError(action)

    def retreat(*_args, **_kwargs):
        status.update(
            in_water=landing != "dry", in_lava=landing == "lava", health=4.71, pos=[11086, 62, -11051.3],
        )
        if landing == "respawned":
            status.update(last_death={"t": 2}, health=20)
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.45}

    def reach_shore(pos, **kwargs):
        shore_calls.append((pos, kwargs))
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def escape_lava():
        lava_calls.append(status["pos"])
        status.update(in_lava=False, in_water=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)

    incident = guard.poll()
    assert lava_calls == ([[11086, 62, -11051.3]] if landing == "lava" else [])

    if landing == "water":
        assert [a["action"] for a in incident["actions"]] == ["combat_or_flee", "escape_to_shore"]
        assert shore_calls == [(zombie["pos"], {
            "keep_shield": True, "shield_target_id": 44, "surface_first": False, "avoid_close_threat": True,
        })]
        assert incident["status"]["in_water"] is False and incident["requires_replan"]
        assert not guard._last_water_escape_failed
    else:
        assert shore_calls == []


def test_bug069_surface_recovery_faces_skeleton_and_swims_away_using_live_position():
    status = {
        "pos": [6378.7, 62.2, -6442.3], "health": 17, "food": 20, "oxygen": 180, "yaw": -1.5708,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "skeleton", "dist": 8},
    }
    calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield", "count": 1}}
            if action == "get_entities":
                return {"entities": [{"id": 44, "name": "skeleton", "pos": [6386.7, 63.0, -6442.3]}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            calls.append((action, body))
            if action == "move_to":
                status.update(pos=[body["x"], body["y"], body["z"]], in_water=False, submerged=False, on_ground=True)
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    receipt = json.dumps({"ok": False, "in_water": True, "error": {"code": "water_escape_failed"}})

    note = guard.stabilize_after_tools([receipt])

    surge = next(body for action, body in calls if action == "control")
    shore_move = next(body for action, body in calls if action == "move_to")
    assert calls[0] == ("look", {"entity_id": 44})
    assert calls[1][0] == "posture" and calls[1][1]["shield"] is True
    assert surge["jump"] is True and surge["forward"] is False
    assert shore_move["x"] < 6378.7
    assert status["in_water"] is False and "登岸" in note


@pytest.mark.parametrize("scenario", [
    "ordinary", "surface_tool", "grounded_surface_tool", "flooded_staircase", "submerged_move", "surface_partial_move",
    "boundary_trident", "poll_trident", "boundary_melee", "poll_melee", "tool_melee", "boundary_arrow", "poll_arrow",
    "poll_high_health_melee", "tool_high_health_melee", "tool_ranged_status_only",
])
def test_bug056_guardian_bounds_shared_water_macro_and_falls_back_to_shore(monkeypatch, scenario):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    events = []
    calls = []
    health = 14 if scenario.endswith("melee") and "high_health" not in scenario else 20
    status = {"pos": [0.5, 52, 0.5], "health": health, "oxygen": 240, "in_water": True,
              "submerged": scenario != "surface_partial_move", "on_ground": False, "alive": True, "ready": True}
    if scenario == "grounded_surface_tool":
        status.update(oxygen=400, submerged=False, on_ground=True)
    if scenario == "tool_ranged_status_only":
        status["nearest_hostile"] = {"id": 42, "name": "skeleton", "dist": 3.5}
    if scenario.endswith("arrow"):
        status.update(
            health=17.6, submerged=False, blocking=True,
            nearest_hostile={"id": 42, "name": "pillager", "dist": 9.4, "pos": [9.9, 53, 0.5]},
        )

    class DeepWaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if scenario.endswith("trident") and not kwargs["params"]["since"]:
                    return {"events": [
                        {"seq": 1, "type": "hurt", "source": "trident"},
                        {"seq": 2, "type": "hurt", "source": "mob_attack"},
                    ]}
                if scenario.endswith("melee") and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack"}]}
                if scenario.endswith("arrow") and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}]}
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            calls.append((action, kwargs["timeout_s"], clock[0]))
            assert kwargs["allow_reauth"] is False
            if action == "get_block":
                timeout = kwargs["timeout_s"]
                clock[0] += min(1.1, timeout)
                if timeout < 1.1:
                    raise httpx.ReadTimeout("water probe reached its deadline")
                return {"name": "water"}
            if action == "find_blocks":
                return {"blocks": []}
            return {"ok": True}

    bot = DeepWaterBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)

    def reach_shore(*args, **kwargs):
        assert calls[-1][:2] == ("stop", 1.5)
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    receipt = {
        "ok": False, "stop_reason": "in_water", "pos_after": status["pos"],
        "error": {"code": "in_water"},
    }
    if scenario.startswith("poll_"):
        assert guard.poll() is not None
    elif scenario.startswith("tool_"):
        if scenario == "tool_ranged_status_only":
            trigger = {"source": "tool_interrupted_hurt", "primary_entity_id": 42, "primary_type": "skeleton"}
            interrupted = "mc_skill_harvest_logs"
        else:
            trigger = {"source": "tool_interrupted_hurt", "hurt_source": "mob_attack", "primary_type": "zombie"}
            interrupted = "mc_skill_unstick"
        report = guard.handle_preemption(trigger, interrupted_name=interrupted)
        assert report["status_after"]["in_water"] is False
    else:
        if scenario == "flooded_staircase":
            receipt = {
                "ok": False, "stop_reason": "dig_failed",
                "last_error": {"code": "unsafe_refilled_cell", "pos": [0, 53, 0], "block": "water"},
            }
        if scenario in {"submerged_move", "surface_partial_move"}:
            receipt = {"ok": True, "result": {"pos": status["pos"], "partial_move": scenario == "surface_partial_move"}}
        tool_results = [json.dumps(receipt)] if scenario in {
            "surface_tool", "grounded_surface_tool", "flooded_staircase", "submerged_move", "surface_partial_move",
        } else None
        assert "游向最近干燥陆地登岸" in guard.stabilize_after_tools(tool_results)
    completed = [e for e in events if e["type"] == "guardian.action.completed"]
    shallow, shore = completed
    prefix = "" if scenario.startswith(("poll_", "tool_")) else "boundary_"
    assert shallow["action"] == f"{prefix}escape_water"
    budget_s = 20.0 if scenario == "ordinary" else 4.0
    assert shallow["duration"] == pytest.approx(budget_s)
    assert shallow["result"] == {"ok": False, "reason": "time_budget", "partial": True, "in_water": True}
    assert shore["action"] == f"{prefix}escape_to_shore" and shore["ok"] is True
    assert all(start + timeout <= 1000.0 + budget_s for _, timeout, start in calls[:-1])
    assert skills.bot is bot


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("landed", [False, True])
def test_underwater_hot_floor_keeps_sneak_until_dry(monkeypatch, entry, landed):
    status = {
        "pos": [6111.5, 37.2, -108.5], "health": 19, "food": 20, "oxygen": 260,
        "in_water": True, "submerged": True, "on_ground": False, "ready": True, "alive": True,
    }
    bank = [6113.5, 38, -108.5]
    posture = []

    class BubbleBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "hot_floor"}
                return {"events": [hit] if not kwargs["params"]["since"] else []}
            if action == "inventory":
                return {"items": []}
            if action == "posture":
                posture.append(body)
            if action == "move_to" and landed:
                status.update(pos=bank, in_water=False, submerged=False, on_ground=True)
            if action == "get_block":
                return {"name": "water"}
            return {"ok": True}

    guard = GenerationGuard(BubbleBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda *_args, **_kwargs: pytest.fail("no digging in a magma column"))
    monkeypatch.setattr(guard.combat_env, "_find_walkable_shore_target", lambda *_args, **_kwargs: bank if landed else None)

    if entry == "poll":
        guard.poll()
    else:
        guard.stabilize_after_tools()
    assert posture[0] == {"shield": False, "sneak": True, "jump": True}
    assert posture[-1] == {"shield": False, "sneak": not landed, "jump": not landed}
    assert status["in_water"] is not landed


@pytest.mark.parametrize("lava_escape_succeeds", [True, False])
def test_boundary_switches_water_macro_to_lava_escape_on_contact(monkeypatch, lava_escape_succeeds):
    status = {
        "pos": [10834.4, 32.1, -11103.5], "health": 20, "food": 20, "oxygen": 240,
        "in_water": True, "in_lava": False, "submerged": True, "on_ground": False,
        "ready": True, "alive": True, "last_death": {"t": 1},
    }
    recovery = []
    events = []

    class FluidBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "stop":
                return {"ok": True}
            pytest.fail(f"unexpected action {action}")

    def legacy_escape(scoped):
        assert scoped.bot.status()["in_water"] is True
        status.update(in_water=False, in_lava=True, pos=[10832.6, 35.8, -11103.5], health=10)
        scoped.bot.status()
        pytest.fail("The water macro must unwind as soon as it sees lava")

    def lava_escape():
        recovery.append("lava")
        if lava_escape_succeeds:
            status.update(in_lava=False, on_ground=True)
        return {"ok": lava_escape_succeeds, "in_lava": status["in_lava"], "pos_after": status["pos"]}

    def stale_shore(*_args, **_kwargs):
        pytest.fail("A failed lava escape must not return to the water escape path")

    monkeypatch.setattr(MacroSkills, "escape_water", legacy_escape)
    bot = FluidBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=MacroSkills(bot))
    monkeypatch.setattr(guard.combat_env, "escape_lava", lava_escape)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", stale_shore)

    receipt = {"ok": True, "result": {"pos": status["pos"]}}
    guard.stabilize_after_tools([json.dumps(receipt)])

    assert recovery == ["lava"]
    water, = [e for e in events if e["type"] == "guardian.action.completed" and e["action"] == "boundary_escape_water"]
    assert water["result"]["mode"] == "lava_escape"
    assert water["ok"] is lava_escape_succeeds
    assert status["in_lava"] is not lava_escape_succeeds


@pytest.mark.parametrize("health", [9.8, 10.0, 15.5])
def test_trident_hit_directs_low_health_swimmer_to_shore_after_shield(monkeypatch, health):
    status = {
        "pos": [9264.9, 62.3, -11339.2], "health": health, "food": 20, "oxygen": 285,
        "in_water": True, "on_ground": False, "ready": True, "alive": True, "blocking": False,
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": 7.0, "pos": [9257.9, 62.3, -11339.2]},
    }

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "trident"}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "posture":
                status["blocking"] = body["shield"]
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def reach_shore(pos, *, keep_shield, shield_target_id):
        assert pos == [9257.9, 62.3, -11339.2]
        assert keep_shield and shield_target_id == 44 and status["blocking"]
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard, "_escape_water", lambda *_args, **_kwargs: {"ok": False, "in_water": True})
    incident = guard.poll()

    actions = ["face_attacker", "raise_shield"]
    if health >= 10:
        actions.append("escape_water")
    assert [action["action"] for action in incident["actions"]] == [*actions, "escape_to_shore"]


@pytest.mark.parametrize("second_source", ["mob_attack", "arrow"])
def test_water_combat_retries_shore_after_unconfirmed_landing(monkeypatch, second_source):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [0.5, 62.5, 0.5], "health": 17.8, "food": 20, "oxygen": 285,
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "drowned", "pos": [3.3, 62.5, 0.5], "dist": 2.8},
    }
    hurts = [{"seq": 1, "type": "hurt", "source": "mob_attack", "health": 17.8}]

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [hurt for hurt in hurts if hurt["seq"] > since], "last_seq": hurts[-1]["seq"]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    shore_attempts = []

    def reach_shore(attacker_pos, **kwargs):
        shore_attempts.append(attacker_pos)
        if len(shore_attempts) == 1:
            return {"ok": False, "in_water": False, "reason": "shore_landing_unconfirmed"}
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def escape_water(*args, **kwargs):
        pytest.fail("a failed shore landing should be followed by another directed escape")

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard, "_escape_water", escape_water)
    first = guard.poll()
    assert [action["action"] for action in first["actions"]] == ["escape_to_shore"]
    assert first["actions"][0]["result"]["reason"] == "shore_landing_unconfirmed"

    clock[0] += 4.0
    status.update(health=11.0, nearest_hostile={})
    hurts.append({"seq": 2, "type": "hurt", "source": second_source, "health": 11.0})
    second = guard.poll()
    assert [action["action"] for action in second["actions"]] == ["escape_to_shore"]
    assert second["actions"][0]["ok"] is True
    assert shore_attempts == [[3.3, 62.5, 0.5], None]

