from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_hurt_during_model_wait_raises_shield_and_discards_stale_response(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    state = {"hurt": False, "blocking": False}
    invoked = []
    events = []
    requests = 0

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "events":
            since = int(request.url.params.get("since", 0)) if request.method == "GET" else body.get("since", 0)
            # BUG-026 moved ranged "arrow" hits to cover_or_flee; melee "mob_attack" exercises shield-on-hurt cancellation.
            rows = [{"seq": 1, "type": "hurt", "health": 16, "source": "mob_attack", "source_entity": {"id": 12}}] if state["hurt"] and since < 1 else []
            result = {"events": rows, "last_seq": 1 if state["hurt"] else 0}
        elif action == "status":
            result = {"pos": [0.5, 64, 0.5], "health": 16 if state["hurt"] else 20,
                      "food": 20, "alive": True, "ready": True, "on_ground": True, "blocking": state["blocking"]}
        elif action == "inventory":
            result = {"items": [{"name": "shield", "count": 1}]}
        elif action in ("look", "posture"):
            invoked.append((action, body))
            if action == "posture":
                state["blocking"] = body["shield"]
            assert request.extensions["timeout"]["read"] == 8.0
            result = {"blocking": state["blocking"]}
        else:
            raise AssertionError(action)
        return httpx.Response(200, json={"ok": True, "result": result})

    def chat(messages, tools=None, *, control=None, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            state["hurt"] = True
            clock[0] += 1
            check_control(control)
            pytest.fail("Old generation should have been cancelled before delivering tool calls.")
        assert any("模型等待期间的真实生存事件" in m.get("content", "") for m in messages)
        assert any('"inventory_after"' in m.get("content", "") for m in messages)
        assert state["blocking"] is True
        return {"role": "assistant", "content": "已举盾，按新状态重规划。"}

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("guard", bot, chat_fn=chat, max_calls=4, survival_guard=True,
                              include_initial_snapshot=False, on_event=events.append)
    assert report["status"] == "answered"
    assert requests == 2
    assert invoked == [("look", {"entity_id": 12}), ("posture", {"shield": True, "jump": False, "sneak": False})]
    assert report["guardian"]["cancelled_generations"] == 1
    assert any(e["type"] == "model.failed" and e.get("cancelled") for e in events)
    assert not any(e["type"] == "tool.started" for e in events)
    assert any(e["type"] == "mc.notable_events" and "受伤" in e["text"] for e in events)


@pytest.mark.parametrize("entry, warden_visible", [("poll", True), ("boundary", True), ("boundary", False)])
def test_sonic_boom_prioritizes_warden_retreat_over_blocking_or_eating(monkeypatch, entry, warden_visible):
    from mc_agent.skills.combat_env import CombatEnv, WARDEN_EVADE_DISTANCE

    warden = {"id": 81, "name": "warden", "pos": [-3493, -54, 18504.5], "dist": 5.5}
    zombie = {"id": 82, "name": "zombie", "pos": [-3497, -54, 18504.5], "dist": 1.5}
    status = {
        "pos": [-3498.5, -54, 18504.5], "health": 3.33, "food": 14, "blocking": True,
        "offhand": {"name": "shield"}, "nearest_hostile": zombie if warden_visible else None,
        "dimension": "overworld", "ready": True, "alive": True, "on_ground": True,
    }
    retreat_calls = []
    events = []

    class DeepDarkBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "sonic_boom", "likely_attacker": zombie}
                return {"events": [hit] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 8}], "offhand": status["offhand"]}
            if action == "get_entities":
                return {"entities": [zombie, warden] if warden_visible else []}
            if action == "posture":
                assert not body.get("shield")
                status["blocking"] = False
            if action in {"attack", "dig", "place", "use_item"}:
                pytest.fail(f"Unexpected {action} after a sonic boom")
            return {"ok": True}

    def retreat(_env, target, **kwargs):
        assert target == (warden["pos"] if warden_visible else None)
        assert kwargs == {"distance": WARDEN_EVADE_DISTANCE, "urgent": True, "allow_shaft_seal": False,
                          "wait_for_ground": True, "use_nearest_hostile": False,
                          "min_threat_distance": WARDEN_EVADE_DISTANCE}
        assert not status["blocking"]
        retreat_calls.append(target)
        return {"ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "flee", retreat)
    guard = GenerationGuard(DeepDarkBot(), EventSink(events.append), checkpoint=lambda: None)
    if entry == "poll":
        assert guard.poll()["requires_replan"]
    else:
        assert guard.stabilize_after_tools() is not None

    assert len(retreat_calls) == 1
    combat, = [event for event in events if event.get("type") == "guardian.action.completed" and "subcortical_combat" in event["action"]]
    assert combat["result"]["mode"] == "warden_evade"


@pytest.mark.parametrize("route", ["flat", "cliff", "warden_chased", "scan_failed", "missing_nearby_warden"])
def test_warden_retreat_continues_past_ordinary_melee_clearance(route):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    status = {
        "pos": [17245.3, 55, 29988], "health": 11, "food": 20, "dimension": "overworld",
        "alive": True, "ready": True, "on_ground": True,
    }
    warden = {"id": 270255, "name": "warden", "pos": [17253.5, 55, 29988], "dist": 8.2}
    motions = []
    scans = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_entities":
                scans.append(body["radius"])
                if route == "scan_failed" and body["radius"] == 24:
                    raise httpx.ReadTimeout("warden scan stalled")
                if route == "missing_nearby_warden" and body["radius"] == 24:
                    far_warden = {"id": 270256, "name": "warden", "pos": [17254.25, 55, 29988], "dist": 19.0}
                    return {"entities": [far_warden]}
                return {"entities": [warden]}
            if action == "get_block":
                x, y, _ = body["pos"]
                floor = y == 54 and (route != "cliff" or x >= 17238)
                return {"name": "stone" if floor else "air"}
            if action == "look":
                assert body["yaw"] == pytest.approx(math.pi / 2)
            elif action == "control":
                motions.append("sprint")
                status["pos"] = [17243.3, 55, 29988]
            elif action == "move_to":
                motions.append(body["x"])
                status["pos"] = [body["x"] + 0.95, body["y"], body["z"]]
                status["nearest_hostile"] = {"id": warden["id"], "name": "warden", "dist": 8.2}
                if route == "warden_chased":
                    warden["pos"] = [status["pos"][0] + 8.2, 55, 29988]
            else:
                pytest.fail(f"Unexpected {action} during sonic retreat")
            return {"ok": True}

    outcome = SubcorticalInstinctController().on_encounter(CombatEnv(CaveBot()), {
        "source": "tool_interrupted_hurt", "primary_type": "warden", "hurt_source": "sonic_boom",
    })

    assert scans == [16, 24]
    assert motions == ["sprint", pytest.approx(17239.8 if route == "cliff" else 17234.3)]
    assert outcome["handled"] is (route == "flat")
    retreat, = outcome["actions"]
    assert retreat.get("reason") == {
        "flat": None, "cliff": "warden_still_in_range", "warden_chased": "warden_still_in_range",
        "scan_failed": "warden_clearance_unconfirmed", "missing_nearby_warden": "warden_clearance_unconfirmed",
    }[route]


@pytest.mark.parametrize("route", ["clear", "cliff", "new_life", "side_drift", "chased", "other_mob", "lost_target"])
def test_w20_warden_retreat_rechecks_the_same_tunnel_after_short_clearance(route):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    status = {
        "pos": [210533.4, -46, 208314.5], "health": 16.83, "dimension": "overworld", "last_death": {"t": 1},
        "alive": True, "ready": True, "on_ground": True,
        "nearest_hostile": {"id": 2390683, "name": "warden", "dist": 10.6},
    }
    warden = {"id": 2390683, "name": "warden", "pos": [210544, -46, 208314.5], "dist": 10.6}
    moves = []

    class DeepTunnelBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_entities":
                if moves and route == "lost_target":
                    return {"entities": [{**warden, "id": 2390684}]}
                if moves and route == "other_mob":
                    creeper = {"id": 12, "name": "creeper", "pos": [210520.5, -46, 208314.5], "dist": 3.2}
                    return {"entities": [warden, creeper]}
                return {"entities": [warden]}
            if action == "get_block":
                x, y, z = body["pos"]
                if moves and x < 210523 and route == "new_life":
                    status.update(last_death={"t": 2}, pos=[210665.5, 88.1, 208399.5])
                if moves and x < 210523 and route == "side_drift":
                    status.update(pos=[210523.7, -46, 208315.0])
                if z != 208314 or y >= -44:
                    return {"name": "stone"}
                if y == -47 and (route != "cliff" or x >= 210522):
                    return {"name": "stone"}
                return {"name": "air"}
            if action == "look":
                assert body["yaw"] == pytest.approx(math.pi / 2)
            elif action == "control":
                status["pos"] = [210531.4, -46, 208314.5]
            elif action == "move_to":
                moves.append(body)
                if len(moves) == 1:
                    status["pos"] = [210523.7, -46, 208314.5]
                else:
                    status["pos"] = [body["x"] + 0.5, body["y"], body["z"]]
                if len(moves) == 1 or route == "chased":
                    warden.update(pos=[status["pos"][0] + 4.9, -46, 208314.5], dist=4.9)
                status["nearest_hostile"] = {"id": warden["id"], "name": "warden", "dist": warden["dist"]}
            else:
                pytest.fail(f"Unexpected {action} during Warden retreat")
            return {"ok": True}

    outcome = SubcorticalInstinctController().on_encounter(CombatEnv(DeepTunnelBot()), {
        "source": "tool_interrupted_hurt", "primary_type": "warden", "hurt_source": "sonic_boom",
    })

    first = outcome["actions"][0]
    assert first["moved_dist"] == pytest.approx(9.7)
    assert first["reason"] == ("warden_clearance_unconfirmed" if route == "lost_target" else "warden_still_in_range")
    assert outcome["handled"] is (route == "clear")
    if route in {"clear", "chased"}:
        assert len(moves) == 2
        assert moves[1]["x"] < 210516 and moves[1]["y"] == -46 and moves[1]["z"] == pytest.approx(208314.5)
        continuation = outcome["actions"][1]
        assert continuation["action"] == "continue_warden_retreat"
        assert continuation["ok"] is (route == "clear")
        assert continuation.get("reason") == (None if route == "clear" else "warden_still_in_range")
    else:
        assert len(moves) == 1
        if route == "new_life":
            assert outcome["status_after"]["last_death"] == {"t": 2}


@pytest.mark.parametrize("health, distance", [(16.0, 9.0), (5.0, 9.0), (5.0, 1.6)])
def test_arrow_hit_digs_cover_pocket_instead_of_sprinting_away(monkeypatch, health, distance):
    """Live s03: 6 arrows (20 -> 0 hp) while sprint-fleeing in a straight line down a cave."""

    class _ArrowSkills:
        class _Eng:
            nodes = {}

            def is_inside_shaft(self):
                return False

        mining_dfs = _Eng()

        def dig_cover_pocket(self):
            return {"ok": True, "mode": "cover_pocket", "sealed": [True, True]}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee"}

    class _ShotBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, 23.0, 0.5], "health": health, "food": 20.0, "alive": True,
                        "ready": True, "in_water": False, "on_ground": True,
                        "nearest_hostile": {"name": "pillager" if distance <= 3.5 else "skeleton", "dist": distance}}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "health": health, "source": "arrow"}],
                        "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 40}], "offhand": {}}
            return {"ok": True}

    guard = GenerationGuard(_ShotBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.skills = _ArrowSkills()
    guard.last_poll = float("-inf")
    incident = guard.poll()

    assert incident is not None
    expected = "combat_or_flee" if health <= 10.0 and distance <= 3.5 else "dig_cover"
    action, = incident["actions"]
    assert action["action"] == expected and action["ok"] is True


def test_arrow_hit_falls_back_to_flee_when_no_wall_for_cover():
    class _NoCoverSkills:
        class _Eng:
            nodes = {}

            def is_inside_shaft(self):
                return False

        mining_dfs = _Eng()

        def dig_cover_pocket(self):
            return {"ok": False, "reason": "no_solid_wall"}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee"}

    class _ShotBot2:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, 23.0, 0.5], "health": 16.0, "food": 20.0, "alive": True,
                        "ready": True, "in_water": False, "on_ground": True}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "health": 16.0, "source": "arrow"}],
                        "last_seq": 1}
            if action == "inventory":
                return {"items": [], "offhand": {}}
            return {"ok": True}

    guard = GenerationGuard(_ShotBot2(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.skills = _NoCoverSkills()
    guard.last_poll = float("-inf")
    incident = guard.poll()

    assert incident is not None
    assert [a["action"] for a in incident["actions"]] == ["dig_cover", "combat_or_flee"]


@pytest.mark.parametrize("entry", ["poll", "boundary"])
def test_sealed_cover_receipt_outside_pocket_does_not_stop_retreat(monkeypatch, entry):
    pos = [6392.5, 22, -6631.3]
    shooter = {"id": 42, "name": "skeleton", "dist": 6.4, "pos": [6392.5, 22, -6624.9]}
    nearest = {key: shooter[key] for key in ("id", "name", "dist")}
    moves = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": list(pos), "ready": True, "alive": True, "on_ground": True,
                        "health": 12.3, "food": 20, "nearest_hostile": nearest}
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}] if since < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 40}], "offhand": None}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_entities":
                return {"entities": [shooter]}
            if action == "get_block":
                x, y, z = body["pos"]
                is_ceiling = (x, y, z) == (6392, 24, -6632)
                return {"name": "stone" if y < 22 or is_ceiling else "air"}
            if action == "control" and body.get("forward"):
                pos[2] -= 2.3
                moves.append(list(pos))
            return {"ok": True}

    def sealed_but_empty_pocket():
        return {
            "ok": True, "mode": "cover_pocket", "requires_replan": False, "sealed": [True, True],
            "pocket": [[6393, 22, -6632], [6393, 23, -6632]], "pos_after": list(pos),
        }

    events = []
    bot = CaveBot()
    skills = MacroSkills(bot)
    monkeypatch.setattr(skills, "dig_cover_pocket", sealed_but_empty_pocket)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)
    if entry == "poll":
        incident = guard.poll()
        cover, retreat = incident["actions"]
        assert cover["result"]["reason"] == "cover_not_entered"
    else:
        guard.stabilize_after_tools()
        completed, = [event for event in events if event["type"] == "guardian.action.completed"]
        cover, retreat = completed["result"]["actions"]
        assert cover["reason"] == "cover_not_entered"
    assert cover["ok"] is False and retreat["ok"] is True
    move, = moves
    assert move == pytest.approx([6392.5, 22, -6633.6])


@pytest.mark.parametrize("ready_after_respawn", [False, True])
def test_arrow_cover_does_not_retreat_using_a_previous_lifes_attacker(monkeypatch, ready_after_respawn):
    status = {
        "pos": [-6232.5, 29.1, -6416.8], "health": 9.5, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "dimension": "overworld",
        "last_death": {"t": 1},
    }

    class ShotBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                events = [{"seq": 1, "type": "hurt", "source": "arrow"}]
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [], "offhand": None}
            pytest.fail(f"unexpected action {action}")

    bot = ShotBot()
    skills = MacroSkills(bot)

    def die_during_cover():
        status.update(
            pos=[-6240.5, 72.1, -6412.5], health=20, ready=ready_after_respawn,
            last_death={"t": 2},
        )
        return {"ok": False, "mode": "cover_pocket", "pos_after": status["pos"], "sealed": [False, False]}

    def stale_retreat(*_args, **_kwargs):
        pytest.fail("Must stop combat recovery for the previous life")

    monkeypatch.setattr(skills, "dig_cover_pocket", die_during_cover)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=skills)
    monkeypatch.setattr(guard, "_defend_or_flee", stale_retreat)
    incident = guard.poll()

    cover, = incident["actions"]
    assert cover["action"] == "dig_cover"
    assert cover["ok"] is False and cover["result"]["respawned"] is True
    assert incident["requires_replan"] is True


@pytest.mark.parametrize("died_while_digging", [False, True])
def test_arrow_cover_deadline_stops_pending_dig_before_retreat(monkeypatch, died_while_digging):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [7821.6, 66.0, 2774.5], "health": 17.2, "food": 20, "on_ground": True,
        "alive": True, "ready": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    calls = []

    class ShotBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hits = [{"seq": 1, "type": "hurt", "source": "arrow"}]
                return {"events": [e for e in hits if e["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 2}]}
            calls.append((action, kwargs.get("timeout_s")))
            if action == "dig":
                clock[0] += min(kwargs.get("timeout_s", 8.0), 6.94)
                if died_while_digging:
                    status.update(pos=[7790.5, 66.1, 2771.5], health=20, last_death={"t": 2})
                raise httpx.ReadTimeout("cover dig stalled")
            assert action == "stop"
            return {"ok": True}

    def dig_cover(scoped):
        scoped.bot.call("dig", {"pos": [7822, 66, 2774]}, timeout_s=8.0)

    def retreat(*_args, **_kwargs):
        assert calls[-1] == ("stop", 1.5)
        calls.append(("retreat", None))
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(MacroSkills, "dig_cover_pocket", dig_cover)
    bot = ShotBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot))
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()

    cover, *other_actions = incident["actions"]
    assert cover["result"]["reason"] == "time_budget"
    assert cover["result"].get("respawned", False) is died_while_digging
    assert [a["action"] for a in other_actions] == ([] if died_while_digging else ["combat_or_flee"])
    assert calls[:2] == [("dig", 3.5), ("stop", 1.5)]


@pytest.mark.parametrize("end", ["dead", "zero_health", "not_ready", "respawned", "same_life"])
def test_w20_melee_retreat_checks_life_before_exit_controls_and_food(monkeypatch, end):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    zombie = {"id": 2351383, "name": "zombie", "pos": [233455.8, 67, 246408.6], "dist": 2.0}
    status = {
        "pos": [233453.8, 67, 246408.6], "dimension": "overworld", "last_death": {"t": 1},
        "health": 5.2, "food": 20, "ready": True, "alive": True, "on_ground": True, "nearest_hostile": zombie,
    }
    writes = []

    class RespawnBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **_kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}]}
            if action == "get_entities":
                return {"entities": [zombie]}
            writes.append((action, body))
            return {"ok": True}

    def flee(_target, **_kwargs):
        if end != "same_life":
            status.update(pos=[233451.5, 68.1, 246405.5], nearest_hostile=None, food=14)
        if end == "dead":
            status.update(last_death={"t": 2}, health=0, ready=False, alive=False)
        elif end == "zero_health":
            status["health"] = 0
        elif end == "not_ready":
            status["ready"] = False
        elif end == "respawned":
            status.update(last_death={"t": 2}, health=20)
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0}

    env = CombatEnv(RespawnBot(), post_combat_food=True)
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", flee)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"], "distance": 2.0,
    })

    assert outcome["status_after"] == status and outcome["requires_replan"]
    assert outcome["actions"] == [{"action": "flee", "ok": False, "moved": 0.0, "reason": "unsafe_landing"}]
    assert outcome["handled"] is (end == "same_life")
    ready = end in {"same_life", "respawned"}
    assert outcome["mode"] == ("cover_or_flee" if ready else "life_unavailable")
    entry = [("stop", {}), ("control", {"wait": False})]
    assert writes == (entry + [("control", {"wait": False}), ("posture", {})] if ready else entry)


@pytest.mark.parametrize("source, identified, expected", [
    ("mob_attack", True, "cover_or_flee"),
    ("mob_attack", False, "cover_or_flee"),
    ("entity_explosion", False, "creeper_evade"),
])
def test_tool_hurt_does_not_treat_distant_creeper_as_melee_attacker(monkeypatch, source, identified, expected):
    from mc_agent.skills.combat_env import CombatEnv

    creeper = {"id": 42, "name": "creeper", "dist": 11.9, "pos": [12.4, 64, 0.5]}
    zombie = {"id": 77, "name": "zombie", "dist": 2.0, "pos": [0.5, 64, -1.5]}
    status = {
        "pos": [0.5, 64, 0.5], "dimension": "overworld", "health": 19, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": creeper,
    }
    retreat_targets = []

    class InterruptedBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": source}
                if identified:
                    hurt["source_entity"] = zombie
                return {"events": [hurt] if kwargs["params"]["since"] < 1 else [], "last_seq": 1}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [creeper, zombie] if identified else [creeper]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            return {"ok": True}

    def retreat(_env, threat_pos=None, **kwargs):
        retreat_targets.append((threat_pos, kwargs.get("use_nearest_hostile", True)))
        return {"ok": True, "mode": "flee", "moved_dist": 3.0, "pos_after": status["pos"]}

    monkeypatch.setattr(CombatEnv, "flee", retreat)
    guard = GenerationGuard(InterruptedBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": "creeper", "primary_entity_id": 42, "distance": 11.9},
        interrupted_name="mc_dig",
    )

    assert receipt["reflex_mode"] == expected
    expected_target = creeper["pos"] if source == "entity_explosion" else zombie["pos"] if identified else None
    assert retreat_targets == [(expected_target, expected_target is not None)]


@pytest.mark.parametrize("outcome", [
    "fast_descent", "dropped_between_swings", "already_falling", "zero_hits_while_falling",
    "server_timeout_while_falling", "short_knockback", "grounded_on_lower_floor",
])
def test_counter_aborts_during_a_large_fall_without_trying_to_flee(monkeypatch, outcome):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    zombie = {"id": 359290, "name": "zombie", "dist": 1.98, "pos": [40961.21, 8, 40988.52]}
    origin = [40963.19, 8, 40988.52]
    status = {
        "pos": origin, "dimension": "overworld", "health": 17, "food": 20,
        "on_ground": True, "ready": True, "alive": True, "nearest_hostile": zombie,
    }
    if outcome == "already_falling":
        status.update(on_ground=False, velocity=[0, -12, 0])
    calls = []

    class CliffBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}], "held": {"name": "stone_sword"}}
            if action == "get_entities":
                return {"entities": [zombie]}
            calls.append((action, body))
            if action != "attack":
                return {"ok": True}
            if sum(endpoint == "attack" for endpoint, _ in calls) > 1:
                return {"hits": 1, "killed": True}
            status["on_ground"] = outcome == "grounded_on_lower_floor"
            status["pos"] = [origin[0], 7.8 if outcome == "short_knockback" else 4, origin[2]]
            if outcome in {"fast_descent", "zero_hits_while_falling", "server_timeout_while_falling"}:
                status.update(pos=[origin[0], 7.2, origin[2]], velocity=[0, -12, 0])
            if outcome == "server_timeout_while_falling":
                raise ApiError("timeout", "attack timed out")
            return {"hits": 0 if outcome == "zero_hits_while_falling" else 1, "killed": False}

    env = CombatEnv(CliffBot())

    def flee(*_args, **_kwargs):
        calls.append(("flee", {}))
        return {"ok": False, "reason": "unsafe_landing"}

    monkeypatch.setattr(env, "flee", flee)
    result = CombatControllerSlot().handle(env, {
        "source": "tool_interrupted_hurt", "hurt_source": "mob_attack",
        "primary_entity_id": zombie["id"], "primary_type": zombie["name"],
    })

    ordinary_exchange = outcome in {"short_knockback", "grounded_on_lower_floor"}
    assert sum(action == "attack" for action, _ in calls) == (2 if ordinary_exchange else int(outcome != "already_falling"))
    assert all(action != "flee" for action, _ in calls)
    assert calls[-2:] == [("control", {"wait": False}), ("posture", {})]
    assert result["mode"] == ("counter_killed" if ordinary_exchange else "falling")
    assert result["requires_replan"]
    assert result["status_after"]["pos"] == status["pos"]


@pytest.mark.parametrize("outcome", ["hurt_receipt", "hurt_error", "respawned", "healthy"])
def test_unshielded_counter_stops_on_hurt_at_borderline_health(monkeypatch, outcome):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    zombie = {"id": 193294, "name": "zombie", "dist": 2.79, "pos": [9826, 70, 7379.95]}
    status = {
        "pos": [9828.75, 70, 7379.45], "health": 18 if outcome == "healthy" else 11.3,
        "ready": True, "alive": True, "on_ground": True, "dimension": "overworld",
        "last_death": {"t": 1}, "nearest_hostile": zombie, "held": {"name": "iron_sword"},
    }
    actions = []

    class PortalBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "attack":
                actions.append(action)
                if outcome == "healthy":
                    assert body["stop_on"] == ["death"]
                    return {"hits": 3, "killed": True}
                assert body["stop_on"] == ["hurt", "death"]
                status.update(health=9.3, on_ground=False)
                if outcome == "respawned":
                    status.update(health=20, last_death={"t": 2}, nearest_hostile=None)
                if outcome == "hurt_error":
                    raise ApiError("interrupted", "stopped because of hurt")
                return {"hits": 1, "killed": False, "interrupted_by": "hurt"}
            return {"ok": True}

    def retreat(_pos, *, distance, urgent, wait_for_ground):
        assert wait_for_ground and status["health"] == 9.3
        actions.append("flee")
        return {"ok": True, "mode": "flee"}

    env = CombatEnv(PortalBot())
    monkeypatch.setattr(env, "flee", retreat)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_melee", "primary_entity_id": zombie["id"], "primary_type": "zombie",
    })

    assert actions == (["attack", "flee"] if outcome in {"hurt_receipt", "hurt_error"} else ["attack"])
    assert result["mode"] == {"healthy": "counter_killed", "respawned": "life_unavailable"}.get(outcome, "low_hp_disengage")

