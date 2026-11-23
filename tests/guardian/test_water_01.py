from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("mode", ["historical", "respawn", "water"])
def test_guard_ignores_old_damage_and_keeps_water_and_respawn_semantics(mode, monkeypatch):
    actions = []
    state = {"jumping": False}
    escaped = []
    # Water reflex now climbs out onto dry land instead of juggling in place.
    monkeypatch.setattr(
        MacroSkills, "escape_water", lambda self: escaped.append("escape_water") or {"ok": True, "in_water": False}
    )

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "status":
            wet = mode == "water" and not escaped
            result = {"health": 20, "food": 20, "alive": True, "ready": True, "pos": [0.5, 64, 0.5],
                      "in_water": wet, "submerged": wet, "on_ground": not wet,
                      "oxygen": 100, "blocking": mode == "water", "jumping": state["jumping"]}
        elif action == "events":
            result = {"last_seq": 51 if mode == "respawn" else 50, "events": (
                [{"seq": 51, "type": "death", "pos": [0, 64, 0]}, {"seq": 52, "type": "spawn", "pos": [4, 64, 4]}]
                if mode == "respawn" else [{"seq": 49, "type": "hurt", "health": 1}]
            )}
        elif action == "posture":
            actions.append(body)
            state["jumping"] = body["jump"]
            result = {"jumping": True, "blocking": body["shield"]}
        elif action == "inventory":
            result = {"items": []}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        bot._last_event_seq = 50
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        incident = guard.poll()
    if mode == "historical":
        assert incident is None
        assert not actions
    elif mode == "respawn":
        assert incident["reasons"] == ["death_or_connection_changed"]
        assert not actions  # Do not dig a new spawn's feet due to an old body's damage.
    else:
        assert escaped == ["escape_water"]
        assert actions == []  # no more in-place posture juggling that only delays drowning
        assert incident["requires_replan"] is True


def test_water_reflex_escapes_to_dry_land_instead_of_floating_in_place(monkeypatch):
    """Live: 7 straight water_float reflexes then 10 drown ticks = death. The reflex must climb out."""
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    escaped = []

    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: escaped.append("escape_water") or {"ok": True, "in_water": False, "escaped": True},
    )

    class _WaterBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, 60.0, 0.5], "health": 8.0, "food": 20.0, "alive": True, "ready": True,
                        "in_water": not escaped, "submerged": not escaped, "on_ground": bool(escaped), "jumping": False}
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [], "offhand": {}}
            return {"ok": True}

    guard = GenerationGuard(_WaterBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_poll = float("-inf")
    incident = guard.poll()

    assert incident is not None
    assert "water_float" in incident["reasons"]
    action_names = [a["action"] for a in incident["actions"]]
    assert action_names == ["escape_water"]
    assert escaped == ["escape_water"]


@pytest.mark.parametrize("elapsed", [4.0, 31.0])
@pytest.mark.parametrize("oxygen", [120, 20, 7])
def test_water_float_continues_shore_recovery_after_recent_failure(monkeypatch, elapsed, oxygen):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 100.0)
    status = {
        "pos": [7982.5, 59.9, 2869.5], "health": 20, "food": 20, "oxygen": oxygen,
        "alive": True, "ready": True, "in_water": True, "submerged": True, "on_ground": False,
    }
    attempts = []

    class WetBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    guard = GenerationGuard(WetBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_water_escape_at = 100.0 - elapsed
    guard._last_water_escape_failed = True

    def recover(method, **kwargs):
        attempts.append((method, kwargs.get("surface_first", False)))
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard, "_escape_water", lambda *_: recover("generic"))
    monkeypatch.setattr(
        guard.combat_env, "escape_deep_water_to_shore", lambda *_, **kwargs: recover("shore", **kwargs),
    )

    incident = guard.poll()

    if elapsed == 4.0 and oxygen == 20:
        assert incident is None and attempts == []
        assert guard._last_water_escape_failed is True
        return
    assert "water_float" in incident["reasons"]
    assert attempts == ([("shore", True)] if elapsed == 4.0 else [("generic", False)])
    assert guard._last_water_escape_failed is False


@pytest.mark.parametrize("observe", ["poll", "boundary"])
@pytest.mark.parametrize("landed", [False, True])
def test_water_recovery_history_clears_only_after_grounded_dry_observation(monkeypatch, observe, landed):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [7824.5, 63.5, 2843.3], "health": 20, "food": 20, "oxygen": 120,
        "alive": True, "ready": True, "in_water": False, "submerged": False, "on_ground": landed,
    }
    calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            pytest.fail(f"Unexpected call: {action}")

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_water_escape_at = 96.0
    guard._last_water_escape_failed = True
    guard._water_hurt_streak = 1

    def recover(method, *, surface_first=False):
        calls.append((method, surface_first))
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard, "_escape_water", lambda *_: recover("generic"))
    monkeypatch.setattr(
        guard.combat_env, "escape_deep_water_to_shore",
        lambda *_, **kwargs: recover("shore", surface_first=kwargs.get("surface_first", False)),
    )

    first_observation = guard.poll if observe == "poll" else guard.stabilize_after_tools
    assert first_observation() is None
    assert guard._last_water_escape_failed is not landed
    assert guard._water_hurt_streak == (0 if landed else 1)
    status.update(in_water=True, submerged=True, on_ground=False)
    clock[0] += 1.0
    assert "water_float" in guard.poll()["reasons"]
    assert calls == ([("generic", False)] if landed else [("shore", True)])


@pytest.mark.parametrize("entry, risk, oxygen, reports_head", [
    ("poll", None, 20, True), ("boundary", None, 20, True), ("boundary", None, 400, True),
    ("preemption", None, 20, True), ("boundary", "drowned", 400, True), ("poll", "drown", 20, True),
    ("poll", "creeper", 400, True), ("boundary", "creeper", 400, True),
    ("poll", None, 20, False), ("boundary", None, 20, False), ("preemption", None, 20, False),
])
def test_bug009_shallow_water_uses_live_danger_outside_the_watcher(monkeypatch, entry, risk, oxygen, reports_head):
    status = {
        "pos": [10.5, 63, 20.5], "health": 20, "food": 20, "oxygen": oxygen, "dimension": "overworld",
        "in_water": True, "submerged": False, "on_ground": True, "ready": True, "alive": True,
    }
    if not reports_head:
        status.pop("submerged")
    if risk in {"drowned", "creeper"}:
        distance = 5.0 if risk == "drowned" else 3.0
        status["nearest_hostile"] = {"name": risk, "id": 64, "dist": distance, "pos": [10.5 + distance, 63, 20.5]}
    hits = [{"seq": 1, "type": "hurt", "source": "drown"}] if risk == "drown" else []
    water_actions = []
    combat_calls = []

    class WadingBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(WadingBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def recover(method, **kwargs):
        water_actions.append(method)
        status.update(in_water=False)
        return {"ok": True, "in_water": False}

    def combat(trigger):
        combat_calls.append(trigger["primary_entity_id"])
        return {"ok": True, "handled": True, "mode": "cover_or_flee"}

    monkeypatch.setattr(guard, "_escape_water", lambda *_, **kw: recover("generic", **kw))
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", lambda *_, **kw: recover("shore", **kw))
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    if entry == "poll":
        result = guard.poll()
    elif entry == "boundary":
        receipt = {"ok": True, "dug_count": 1}
        if reports_head:
            receipt["in_water"] = True
        result = guard.stabilize_after_tools([json.dumps(receipt)])
    else:
        result = guard.handle_preemption(
            {"source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": 42, "distance": 3.0},
            interrupted_name="mc_skill_mine_blocks",
        )
    if risk:
        assert result is not None and water_actions == ["shore"]
    else:
        assert water_actions == []
        if entry == "preemption":
            assert result["reflex_mode"] == "cover_or_flee" and combat_calls == [42]
        else:
            assert result is None


@pytest.mark.parametrize(("submerged", "grounded", "oxygen", "should_float"), [
    (False, True, 20, False), (None, True, 20, False), (True, True, 20, True),
    (False, True, 0, True), (False, False, 20, True),
])
def test_l0_water_float_distinguishes_wading_from_air_loss(submerged, grounded, oxygen, should_float):
    from mc_agent.skills.safety import check_and_stabilize

    status = {"health": 20, "food": 20, "alive": True, "in_water": True, "on_ground": grounded, "oxygen": oxygen}
    if submerged is not None:
        status["submerged"] = submerged
    calls = []

    class WadingBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            calls.append((action, body))
            return {"ok": True}

    result = check_and_stabilize(WadingBot())

    assert result["actions"] == (["posture_water_float"] if should_float else [])
    assert calls == ([("posture", {"sneak": False, "shield": False, "jump": True})] if should_float else [])


def test_hurt_while_standing_in_shallow_water_still_escapes(monkeypatch):
    """Live: in_water + hurt produced actions=[] (melee branch needs dry land, needs_float false)."""
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    escaped = []
    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: escaped.append("escape_water") or {"ok": True, "in_water": False},
    )

    class _ShallowBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, 16.0, 0.5], "health": 4.3, "food": 20.0, "alive": True, "ready": True,
                        "in_water": not escaped, "submerged": False, "on_ground": True, "jumping": False, "oxygen": 280}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "health": 4.3, "source": "arrow"}], "last_seq": 1}
            if action == "inventory":
                return {"items": [], "offhand": {}}
            return {"ok": True}

    guard = GenerationGuard(_ShallowBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_poll = float("-inf")
    incident = guard.poll()

    assert incident is not None
    assert "water_float" not in incident["reasons"]  # shallow ground: not a float emergency
    assert [a["action"] for a in incident["actions"]] == ["escape_water"]
    assert escaped == ["escape_water"]


def test_escape_lava_does_not_trust_in_water_false_and_nether_skips_water_bucket(monkeypatch):
    """Reproduces hy4_hy_7_village: escape_water() lied ok=True in lava, looping 11 ticks to death."""
    from mc_agent.skills.safety import SafetyEmergency, check_and_stabilize

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    state = {"in_lava": True, "pos": [10.5, 62.0, 10.5]}
    calls: list[tuple[str, dict]] = []

    class _LavaPitBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            calls.append((action, payload))
            if action == "status":
                return {
                    "pos": list(state["pos"]),
                    "health": 15.0 if state["in_lava"] else 15.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": not state["in_lava"],
                    "in_water": False,
                    "in_lava": state["in_lava"],
                    "dimension": "minecraft:overworld",
                    "yaw": 0.0,
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 5}]}
            if action == "get_block":
                p = payload["pos"]
                # Surround at y=62..64 except a 1-block step onto solid dry bank at x=11, y=62
                if p == [11, 62, 10]:
                    return {"name": "stone"}
                if p[1] < 62:
                    return {"name": "stone"}
                if p == [10, 62, 10]:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "control" and payload.get("forward"):
                state["in_lava"] = False
                state["pos"] = [11.5, 63.0, 10.5]
            return {"ok": True}

    guard = GenerationGuard(_LavaPitBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_poll = float("-inf")
    inc = guard.poll()
    assert inc is not None
    assert "in_lava" in inc["reasons"]
    esc_act = next(a for a in inc["actions"] if a["action"] == "escape_lava")
    assert esc_act["ok"] is True
    assert state["in_lava"] is False

    # safety.check_and_stabilize in Nether must NOT waste water_bucket (it evaporates immediately).
    nether_calls: list[str] = []

    class _NetherBot:
        def status(self):
            return {"pos": [0.5, 50.0, 0.5], "health": 16.0, "food": 20, "alive": True,
                    "in_water": False, "in_lava": True, "dimension": "minecraft:the_nether"}

        def inventory(self):
            return {"items": [{"name": "water_bucket", "count": 1}]}

        def call(self, action, body=None, **kwargs):
            nether_calls.append(action)
            return {"ok": True}

    with pytest.raises(SafetyEmergency):
        check_and_stabilize(_NetherBot())
    assert "use_item" not in nether_calls


@pytest.mark.parametrize("recovery", ["shore_receipt", "status_changed"])
def test_water_tool_preemption_reports_life_unavailable_after_respawn(monkeypatch, recovery):
    status = {
        "pos": [40995.8, 62.1, 10997.6], "dimension": "overworld", "health": 2, "food": 17,
        "ready": True, "alive": True, "in_water": True, "submerged": True, "last_death": {"t": 1},
    }
    events = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    def shore(*_args, **_kwargs):
        if recovery == "status_changed":
            status.update(health=20, in_water=False, on_ground=True, last_death={"t": 2})
            return {"ok": False, "in_water": False}
        return {"ok": False, "mode": "respawned", "in_water": False}

    guard = GenerationGuard(RiverBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": "zombie", "primary_entity_id": 5462415},
        interrupted_name="mc_attack",
    )

    assert receipt["reflex_mode"] == "life_unavailable"
    assert receipt["requires_replan"]
    assert guard._last_water_escape_failed is False
    assert guard._water_hurt_streak == 0
    assert [e["action"] for e in events if e["type"] == "guardian.action.completed"] == ["escape_to_shore"]


@pytest.mark.parametrize("landing", ["airborne", "grounded", "back_in_water", "respawned"])
def test_water_tool_preemption_keeps_shore_recovery_during_airborne_handoff(monkeypatch, landing):
    status = {
        "pos": [-7780.1, 32.2, -2845.7], "dimension": "overworld", "health": 20,
        "ready": True, "alive": True, "in_water": False, "on_ground": False, "last_death": {"t": 1},
    }
    shore_calls = []

    class RiverBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                if kwargs.get("timeout_s") == 0.25:
                    if landing == "grounded":
                        status.update(on_ground=True)
                    elif landing == "back_in_water":
                        status.update(in_water=True, submerged=True)
                    elif landing == "respawned":
                        status.update(on_ground=True, last_death={"t": 2})
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            if endpoint == "inventory":
                return {"items": []}
            raise AssertionError(endpoint)

    def shore(*args, **kwargs):
        shore_calls.append(kwargs.get("surface_first"))
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def combat(_trigger):
        pytest.fail("Water recovery must not flee from a distant zombie")

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    receipt = guard.handle_preemption(
        {
            "source": "tool_water_threat", "primary_type": "zombie", "primary_entity_id": 45,
            "distance": 20.3, "observed_life": ("overworld", {"t": 1}),
        },
        interrupted_name="mc_skill_unstick",
    )

    assert receipt["requires_replan"]
    assert receipt["reflex_mode"] == ("life_unavailable" if landing == "respawned" else "water_surface")
    assert receipt["status_after"]["on_ground"] is (landing != "airborne")
    assert guard._last_water_escape_failed is (landing == "airborne")
    assert shore_calls == ([True] if landing == "back_in_water" else [])


def test_underwater_drowned_preemption_surfaces_and_ignores_swim_pre_hurt(monkeypatch):
    """Reproduces hy4_hy_10_enchant_anvil & hy4_hy_3_iron: underwater reflex surfaces first; swimming skips empty pre_hurt."""
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, SubcorticalInstinctController

    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 200.0)
    surfaced = []
    monkeypatch.setattr(MacroSkills, "escape_water", lambda self: surfaced.append("escape_water") or {"ok": True, "in_water": False})

    class _WetBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 56.0, 0.5], "health": 18.0, "food": 20, "alive": True, "ready": True,
                    "in_water": True, "submerged": True, "on_ground": False, "oxygen": 60, "jumping": True,
                    "nearest_hostile": {"id": 31, "name": "drowned", "pos": [1.5, 56.0, 0.5], "dist": 2.0},
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "get_entities":
                return {"entities": [{"id": 31, "name": "drowned", "pos": [1.5, 56.0, 0.5], "dist": 2.0}]}
            if action == "get_block":
                return {"name": "water"}
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}]}
            if action == "attack":
                raise AssertionError("must not melee underwater with low oxygen")
            return {"ok": True}

    bot = _WetBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot))
    # 1) handle_preemption underwater surfaces instead of burning remaining oxygen in melee
    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_entity_id": 31, "primary_type": "drowned", "distance": 2.0},
        interrupted_name="mc_skill_mine_dfs",
    )
    assert receipt["reflex_mode"] == "water_surface"
    assert "上浮后 in_water=True" in receipt["message"]
    assert surfaced == ["escape_water"]

    # 2) CombatControllerSlot preserves jump=True when still submerged on exit
    postures: list[dict] = []

    class _StillWetBot:
        def status(self):
            return {"pos": [0.5, 56.0, 0.5], "health": 18.0, "food": 20, "alive": True, "ready": True,
                    "in_water": True, "submerged": True, "on_ground": False, "oxygen": 40}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [{"id": 31, "name": "drowned", "pos": [1.5, 56.0, 0.5], "dist": 2.0}]}
            if action == "posture":
                postures.append(dict(body or {}))
            if action == "attack":
                raise AssertionError("must not attack underwater")
            return {"ok": True}

    slot = CombatControllerSlot(SubcorticalInstinctController())
    slot_out = slot.handle(CombatEnv(_StillWetBot()), {"source": "tool_interrupted_hurt", "primary_entity_id": 31, "primary_type": "drowned"})
    assert slot_out["mode"] == "water_surface"
    assert postures and postures[-1].get("jump") is True

    # 3) poll() while already jumping in water with drowned nearby does NOT cancel LLM with empty actions=[]
    guard.last_poll = float("-inf")
    assert guard.poll() is None

