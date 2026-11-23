from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("hurt_source", ["magic", "poison"])
@pytest.mark.parametrize("live_witch", ["visible", "occluded", "different", "unavailable"])
def test_typed_magic_checks_the_nearby_witch_before_treating_it_as_environmental(monkeypatch, hurt_source, live_witch):
    witch = {"id": 807108, "name": "witch", "dist": 11.4, "pos": [11.4, 63.0, 0.0], "blocked_by_wall": False}
    status = {
        "pos": [0.0, 63.0, 0.0], "dimension": "overworld", "health": 9.9, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "nearest_hostile": {"id": witch["id"], "name": witch["name"], "dist": witch["dist"]},
    }
    retreats = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": hurt_source}] if kwargs["params"]["since"] < 1 else []}
            if action == "get_entities":
                assert body == {"radius": 12, "types": ["hostile"]}
                assert kwargs["timeout_s"] == 0.6
                if live_witch == "unavailable":
                    raise httpx.ReadTimeout("witch sighting unavailable")
                if live_witch == "occluded":
                    return {"entities": [{**witch, "blocked_by_wall": True}]}
                if live_witch == "different":
                    return {"entities": [{**witch, "id": 807109}]}
                return {"entities": [witch]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            pytest.fail(f"Unexpected action during typed magic recovery: {action}")

    def defend(_status, attacker, _counts, *, ranged=False):
        retreats.append((attacker, ranged))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    incident = guard.poll()

    if live_witch == "visible":
        assert [action["action"] for action in incident["actions"]] == ["combat_or_flee"]
        assert retreats == [(witch, True)]
    else:
        assert incident is None
        assert retreats == []


@pytest.mark.parametrize("history", [
    "fresh_arrow", "old_arrow", "respawned", "nearby_trident", "nether_fireball", "nether_after_respawn",
])
def test_episode_start_keeps_eating_guard_for_recent_consumed_projectile(monkeypatch, history):
    clock = [1000.0]
    wall_now = 1_795_200_000.0
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.harness.guardian.time.time", lambda: wall_now + clock[0] - 1000.0)
    arrow = {"seq": 19, "type": "hurt", "source": "arrow",
             "t": (wall_now - (8.1 if history == "old_arrow" else 0.7)) * 1000}
    events = [arrow]
    if history in {"respawned", "nether_after_respawn"}:
        events.append({"seq": 20, "type": "spawn", "t": (wall_now - 0.1) * 1000})
    status = {
        "pos": [-5057.2, 71, -7715.6], "health": 13.3, "food": 16, "dimension": "overworld",
        "blocking": True, "on_ground": True, "ready": True, "alive": True,
    }
    if history == "nearby_trident":
        arrow.update(source="trident", t=(wall_now - 9.0) * 1000)
        status.update(health=5.07, nearest_hostile={"id": 42, "name": "drowned", "dist": 8.5})
    elif history in {"nether_fireball", "nether_after_respawn"}:
        arrow.update(source="fireball", t=(wall_now - 8.214) * 1000)
        status.update(dimension="the_nether", on_fire=False)
    commands = []
    cursors = []

    class EpisodeBot:
        _last_event_seq = 20

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                cursors.append(since)
                return {"events": [event for event in events if event["seq"] > since], "last_seq": 20}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            commands.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(EpisodeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    retry_delay = {"fresh_arrow": 7.4, "nearby_trident": 21.1, "nether_fireball": 3.9}
    if history in retry_delay:
        assert incident is None and commands == []
        clock[0] += retry_delay[history]
        incident = guard.poll()

    assert incident["reasons"] == ["needs_food"] and incident["event_seqs"] == []
    assert not incident["requires_replan"]
    assert commands == [("posture", {"shield": False}), ("use_item", {"item": "cooked_beef"})]
    assert cursors.count(0) == 1


@pytest.mark.parametrize(
    "item, shooter_distance, allowed",
    [("cooked_beef", 4.2, False), ("cooked_beef", 23.0, True), ("golden_apple", 4.2, True)],
)
def test_explicit_food_use_reuses_eating_safety_check(item, shooter_distance, allowed):
    from mc_agent.harness.mc_tools import McToolset

    uses = []

    class FoodBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                return {
                    "pos": [9097.3, 96, -11178.8], "health": 9.56, "food": 17, "blocking": True,
                    "on_ground": True, "ready": True, "alive": True,
                    "nearest_hostile": {"id": 44, "name": "skeleton", "dist": shooter_distance},
                }
            if action == "use_item":
                uses.append(body)
            return {"ok": True}

    bot = FoodBot()
    toolset = McToolset(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    receipt = json.loads(guard.run_tool_with_reflex("mc_use_item", lambda: toolset.mc_use_item(item, ms=1200)))

    assert receipt["ok"] is allowed
    if allowed:
        assert uses == [{"item": item, "ms": 1200}]
    else:
        assert receipt["requires_replan"] and receipt["error"]["code"] == "unsafe_to_eat"
        assert uses == []


@pytest.mark.parametrize("landed_behind_seal", [True, False])
def test_guardian_rechecks_landing_after_legacy_shaft_seal(monkeypatch, landed_behind_seal):
    before = {
        "pos": [3103.7, 99.0, -10039.7], "health": 14.0, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True,
    }
    seal_position = [3102.4, 100.0, -10039.3]
    after = {
        **before, "health": 10.833,
        "pos": [3102.3, 100.0, -10039.1] if landed_behind_seal else [3103.4, 99.0, -10039.3],
    }

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            assert action == "status"
            return dict(after)

    def seal_shaft(_skills, **_kwargs):
        return {
            "ok": True, "mode": "seal_shaft", "pos_after": seal_position,
            "placed_positions": [[3101, 100, -10040], [3101, 101, -10040]],
            "requires_replan": True,
        }

    monkeypatch.setattr(MacroSkills, "defend_or_flee", seal_shaft)
    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot))

    result = guard._defend_or_flee(before, None, {"stone_sword": 1})

    assert result["ok"] is landed_behind_seal
    assert result["pos_after"] == after["pos"]
    assert result["requires_replan"] is True
    if not landed_behind_seal:
        assert result["reason"] == "shaft_seal_landing_unconfirmed"


@pytest.mark.parametrize("has_threat", [False, True])
def test_failed_shallow_bank_hop_retries_with_sustained_climb(monkeypatch, has_threat):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [7853.5, 63, 2766.5]
    lip = [7854.3, 62.4, 2766.6]
    status = {
        "pos": [7864.5, 62.8, 2762.5], "in_water": True, "on_ground": False,
        "submerged": False, "oxygen": 300, "alive": True, "ready": True, "dimension": "overworld",
    }
    jumps = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                return {"name": "stone" if body["pos"] == [7853, 62, 2766] else "air"}
            if action == "move_to":
                assert [body["x"], body["y"], body["z"]] == bank
                status["pos"] = lip
                raise ApiError("path_not_found", "cannot climb bank")
            if action == "control" and body.get("forward"):
                assert body["jump"] is True
                jumps.append((body["ms"], body["sprint"]))
                if jumps[-1] == (1150, False):
                    status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: bank)
    threat = [7866.5, 62.8, 2762.5] if has_threat else None

    assert env.escape_deep_water_to_shore(threat)["in_water"] is True
    retry = env.escape_deep_water_to_shore(threat)

    assert "shore_bank_retry" in retry["actions"]
    assert jumps == [(450, True), (450, True) if has_threat else (1150, False)]
    assert retry["ok"] is not has_threat and retry["in_water"] is has_threat


def test_failed_pathfinder_uses_a_short_hop_to_reach_a_nearby_shallow_bank(monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    bank = [7948.5, 63.0, 2771.5]
    status = {
        "pos": [7949.8, 62.0, 2771.5], "in_water": True, "on_ground": False,
        "alive": True, "ready": True, "dimension": "overworld",
    }

    class ShoreBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "look":
                self.yaw = body["yaw"]
            if action == "move_to":
                raise ApiError("path_not_found", "pathfinder cannot climb the lip")
            if action == "control" and body.get("forward"):
                assert body["jump"] is True
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, _, z = status["pos"]
                distance = 3.0 * body["ms"] / 1000
                x += dx * distance
                z += dz * distance
                landed = int(x) == int(bank[0]) and int(z) == int(bank[2])
                status.update(pos=[x, 63.0 if landed else 62.0, z], in_water=not landed, on_ground=landed)
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: bank)

    result = env.escape_deep_water_to_shore()

    assert result["ok"] is True
    assert result["actions"] == ["move_to_shore_failed", "shore_lip_jump"]


@pytest.mark.parametrize("floor", ["magma_block", "stone"])
def test_critical_move_rechecks_recent_hot_floor_before_dispatch(monkeypatch, floor):
    status = {
        "pos": [6174.3, 66, 56.3], "health": 1.3, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "sneaking": True,
    }
    actions = []

    class PlateauBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": floor}
            if action == "move_to":
                actions.append("move")
                return {"ok": True}
            pytest.fail(f"Unexpected action {action}")

    bot = PlateauBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_hot_floor_block = guard._hot_floor_block(status)

    def step_off(_status):
        actions.append("checked_exit")
        return {"ok": False, "reason": "no_safe_hot_floor_exit"}

    monkeypatch.setattr(guard, "_step_off_hot_floor", step_off)
    receipt = json.loads(guard.run_tool_with_reflex(
        "mc_move_to", lambda: json.dumps(bot.call("move_to", {"x": 6170, "y": 66, "z": 50, "stop_on": ["death"]})),
    ))
    if floor == "magma_block":
        assert actions == ["checked_exit"]
        assert receipt["error"]["code"] == "hot_floor" and receipt["requires_replan"]
    else:
        assert actions == ["move"] and receipt["ok"]


@pytest.mark.parametrize("landing_safe", [True, False])
def test_hot_floor_guard_verifies_dry_landing_after_crouch_failure(landing_safe):
    status = {"pos": [0.5, 64, 0.5], "health": 17, "food": 20, "ready": True, "alive": True, "on_ground": True}
    moves = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [] if since else [{"seq": 1, "type": "hurt", "source": "hot_floor"}]}
            if action == "inventory":
                return {"items": []}
            if action == "posture":
                return {"sneaking": False}
            if action == "get_block":
                x, y, z = body["pos"]
                if y >= 64:
                    return {"name": "air"}
                if (x, z) == (1, 0) and (landing_safe or not moves):
                    return {"name": "stone"}
                return {"name": "magma_block" if (x, z) == (0, 0) else "lava"}
            if action == "move_to":
                moves.append(body)
                status["pos"] = [body[k] for k in ("x", "y", "z")]
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()
    step, = incident["actions"]
    move, = moves
    assert [move[k] for k in ("x", "y", "z")] == [1.5, 64, 0.5]
    assert step["ok"] is landing_safe
    assert incident["requires_replan"] is True
    if landing_safe:
        assert step["result"]["mode"] == "stepped_off"
    else:
        assert step["result"]["reason"] == "unsafe_landing"


@pytest.mark.parametrize("feedback, confirmed", [
    ({"projectiles": []}, False),
    ({"projectiles": [], "used": {"arrow": 1}}, True),
    ({"projectiles": [{"id": 2026194, "name": "arrow"}]}, True),
    ({}, True),
])
def test_guarded_shoot_requires_evidence_when_release_telemetry_is_available(feedback, confirmed):
    from mc_agent.harness.mc_tools import McToolset

    shot = {"fired": True, "weapon": "bow", "target": [-3570, 66, 1401.6], "dist": 7.37, **feedback}
    calls = []

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if endpoint == "shoot":
            assert json.loads(request.content)["entity_id"] == 2023374
            result = shot
        elif endpoint == "status":
            result = {"blocking": True, "ready": True, "alive": True}
        else:
            pytest.fail(f"unexpected shot endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": result})

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot)
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        raw = guard.run_tool_with_reflex("mc_shoot", lambda: toolset.mc_shoot(weapon="bow", entity_id=2023374))

    result = json.loads(raw)
    assert result["ok"] is confirmed and result["result"] == shot
    if confirmed:
        assert calls == ["shoot"]
    else:
        assert calls == ["shoot", "status"] and result["requires_replan"]
        assert result["error"]["code"] == "shoot_unconfirmed" and "举盾" in result["error"]["message"]

