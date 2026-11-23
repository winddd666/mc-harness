from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_combat_environment_imports_in_a_fresh_python_process():
    source_root = Path(__file__).resolve().parents[2] / "src"
    process = subprocess.run(
        [sys.executable, "-c", "from mc_agent.skills.combat_env import CombatEnv"],
        cwd=source_root, capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize("signal", [RequestCancelled, RunTimedOut])
def test_combat_fallbacks_propagate_harness_control_signals(signal):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    class InterruptedBot:
        def call(self, action, body=None, **kwargs):
            raise signal("interrupted")

    env = CombatEnv(InterruptedBot())
    with pytest.raises(signal, match="interrupted"):
        env.has_los([0.5, 63, 0.5], [4.5, 63, 0.5])
    controller = SubcorticalInstinctController()
    with pytest.raises(signal, match="interrupted"):
        controller._scan_live_hostiles(env, radius=8)


@pytest.mark.parametrize("life", [{"ready": False, "alive": True}, {"ready": True, "alive": False}])
def test_tool_preemption_skips_combat_when_life_is_unavailable(monkeypatch, life):
    status = {"pos": [10975.5, 70.1, 11349.5], "health": 20, "in_water": False, **life}

    class RespawningBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            pytest.fail(f"Unexpected {action} before the bot can act")

    events = []
    guard = GenerationGuard(RespawningBot(), EventSink(events.append), checkpoint=lambda: None)

    def stale_combat(*args):
        pytest.fail("The interrupted tool's threat belongs to an unavailable life")

    monkeypatch.setattr(guard.combat_slot, "handle", stale_combat)
    receipt = guard.handle_preemption(
        {"source": "pre_hurt_melee", "primary_type": "drowned", "primary_entity_id": 71},
        interrupted_name="mc_move_to",
    )

    assert receipt["requires_replan"] and receipt["preempted_by_reflex"]
    assert receipt["reflex_mode"] == "life_unavailable"
    assert receipt["status_after"]["pos"] == status["pos"]
    assert [event["type"] for event in events] == ["guardian.preempted_tool"]


@pytest.mark.parametrize("life", [
    {"ready": False, "alive": True},
    {"ready": True, "alive": False, "health": 0},
    {},
])
def test_combat_slot_stops_when_life_becomes_unavailable_after_entry(life):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    calls = []

    class RespawningBot:
        def status(self):
            calls.append("status")
            return dict(life)

        def call(self, action, body=None, **kwargs):
            calls.append(action)
            return {"ok": True}

    outcome = CombatControllerSlot().handle(CombatEnv(RespawningBot()), {
        "source": "tool_interrupted_hurt", "primary_type": "zombie", "primary_entity_id": 42,
    })

    assert outcome["mode"] == "life_unavailable"
    assert outcome["handled"] is False and outcome["requires_replan"] is True
    assert outcome["actions"] == [] and outcome["status_after"] == life
    assert calls == ["stop", "control", "status"]


@pytest.mark.parametrize("mob, source, distance, preempt", [
    ("creeper", "fall", 10.3, False), ("creeper", "stalagmite", 7.8, False),
    ("creeper", "stalagmite", 3.9, True), ("creeper", "entity_explosion", 9.9, True),
    ("creeper", None, 10.3, True),
    ("spider", "fall", 12.1, False), ("spider", "fall", 2.7, True),
    ("spider", "mob_attack", 12.1, True), ("spider", None, 12.1, True),
])
def test_terrain_interruption_does_not_attribute_hurt_to_a_distant_melee_mob(monkeypatch, mob, source, distance, preempt):
    status = {
        "pos": [144434.3, 76, 82445], "health": 19, "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 1980720, "name": mob, "dist": distance},
    }
    endpoint, tool = ("dig", "mc_skill_harvest_logs") if mob == "spider" else ("move_to", "mc_move_to")
    triggers = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == endpoint:
                return {"interrupted_by": "hurt"}
            if action == "events":
                events = [{"seq": 1, "type": "hurt", "source": source}] if source else []
                return {"events": events, "last_seq": len(events)}
            return {"ok": True}

    def combat(trigger):
        triggers.append(trigger)
        mode = "creeper_evade" if mob == "creeper" else "cover_or_flee"
        return {"handled": True, "mode": mode, "status_after": dict(status)}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    result = json.loads(guard.run_tool_with_reflex(tool, lambda: json.dumps(bot.call(endpoint))))

    assert bool(triggers) is preempt
    if preempt:
        assert result["reflex_mode"] == ("creeper_evade" if mob == "creeper" else "cover_or_flee")
    else:
        assert result == {"interrupted_by": "hurt"}


@pytest.mark.parametrize("mob, source, distance", [("creeper", "stalagmite", 8.8), ("spider", "fall", 12.1)])
def test_watched_movement_ignores_confirmed_terrain_damage_near_a_distant_melee_mob(monkeypatch, mob, source, distance):
    import threading

    baseline = threading.Event()
    damaged = threading.Event()
    read_hurt = threading.Event()
    status = {"pos": [0.5, 68, 0.5], "health": 20, "ready": True, "alive": True, "on_ground": True,
              "nearest_hostile": {"id": 42, "name": mob, "dist": distance}}
    stops = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                sample = dict(status)
                if threading.current_thread().name == "mc-creeper-watch":
                    baseline.set()
                return sample
            if action == "move_to":
                assert baseline.wait(1.5)
                status["health"] = 18
                damaged.set()
                assert read_hurt.wait(1.5)
                return {"moved": True}
            if action == "events" and damaged.is_set():
                read_hurt.set()
                return {"events": [{"seq": 1, "type": "hurt", "source": source}]}
            if action == "stop":
                stops.append(action)
            return {"ok": True}

    def unexpected_combat(_trigger):
        raise AssertionError("terrain damage started a combat reflex")

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", unexpected_combat)
    result = json.loads(guard.run_tool_with_reflex("mc_move_to", lambda: json.dumps(bot.call("move_to"))))

    assert result == {"moved": True}
    assert stops == []


@pytest.mark.parametrize("respawned", [False, True])
def test_interrupted_tool_reports_combat_api_failure_and_rechecks_life(monkeypatch, respawned):
    status = {
        "pos": [-10972.3, 72, -11002.3], "health": 2.8, "food": 17, "ready": True,
        "alive": True, "dimension": "overworld", "on_ground": False, "last_death": {"t": 1},
    }

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            pytest.fail(f"unexpected action {action}")

    def fail_combat(_env, _trigger):
        if respawned:
            status.update(pos=[-10971.5, 79.1, -11001.5], health=20, last_death={"t": 2})
        raise ApiError("not_loaded", "chunk not loaded")

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_slot, "handle", fail_combat)
    receipt = guard.handle_preemption({
        "source": "tool_interrupted_hurt", "primary_type": "pillager",
        "primary_entity_id": 5014994, "hurt_source": "arrow",
    }, interrupted_name="mc_skill_unstick")

    assert receipt["requires_replan"] is True
    assert receipt["reflex_mode"] == ("life_unavailable" if respawned else "combat_failed")
    assert "not_loaded" in receipt["message"]
    assert "已完成" not in receipt["message"]
    assert receipt["status_after"]["pos"] == status["pos"]
    assert (guard._ranged_combat_stance_until == float("-inf")) is respawned


@pytest.mark.parametrize("ready_after_respawn", [False, True])
def test_interrupted_tool_reports_respawn_returned_by_combat(monkeypatch, ready_after_respawn):
    before = {
        "pos": [0.5, 56.0, 0.5], "health": 20, "on_ground": True, "ready": True,
        "alive": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    after = {
        **before, "pos": [66.5, 87.1, 26.5], "health": 20 if ready_after_respawn else 0,
        "on_ground": False, "ready": ready_after_respawn, "alive": ready_after_respawn,
        "last_death": {"t": 2}, "in_lava": True,
    }

    class ScoutBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(before)
            if action == "events":
                return {"events": []}
            return {"ok": True}

    guard = GenerationGuard(ScoutBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_slot, "handle", lambda *_: {
        "handled": True, "mode": "creeper_evade", "summary": "creeper reflex", "status_after": after,
    })
    monkeypatch.setattr(guard.combat_env, "escape_lava", lambda: pytest.fail("old reflex must end at respawn"))
    trigger = {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42}
    receipt = guard.handle_preemption(trigger, interrupted_name="mc_skill_scout_surface")

    assert receipt["requires_replan"]
    assert receipt["reflex_mode"] == "life_unavailable"
    assert receipt["status_after"]["pos"] == after["pos"]
    assert not guard._is_pre_hurt_debounced(42, "creeper")


@pytest.mark.parametrize("slow_cleanup", [False, True])
def test_poll_combat_reflex_bounds_stalled_attack_and_releases_controls(monkeypatch, slow_cleanup):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 42, "name": "zombie", "pos": [2.5, 64.0, 0.5], "dist": 2.0}
    status = {
        "pos": [0.5, 64.0, 0.5], "health": 14, "food": 20, "on_ground": True,
        "alive": True, "ready": True, "nearest_hostile": zombie,
    }
    calls = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            calls.append((action, kwargs.get("timeout_s")))
            if action == "attack":
                clock[0] += min(12.0, kwargs["timeout_s"])
                raise httpx.ReadTimeout("combat request stalled")
            if slow_cleanup and clock[0] >= 1007.5 and action in {"stop", "control", "posture"}:
                timeout = kwargs["timeout_s"]
                if isinstance(timeout, httpx.Timeout):
                    # One closed keepalive can use another attempt after a 150 ms delay.
                    duration = 2 * sum(timeout.as_dict().values()) + 0.15
                else:
                    duration = 4 * timeout
                clock[0] += duration
                raise httpx.ReadTimeout("cleanup request stalled")
            return {"ok": True}

    class StalledAttack:
        def on_encounter(self, env, trigger):
            assert trigger["source"] == "pre_hurt_melee"
            env.bot.call("attack", {"entity_id": 42}, timeout_s=20.0)

    emitted = []
    guard = GenerationGuard(CaveBot(), EventSink(emitted.append), checkpoint=lambda: None)
    guard.combat_slot.register(StalledAttack())
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    incident = guard.poll()

    combat, = incident["actions"]
    assert combat["ok"] is False
    assert combat["result"]["reason"] == "time_budget"
    assert incident["requires_replan"] is True
    assert ("attack", 7.5) in calls
    budget_event, = [event for event in emitted if event["type"] == "guardian.combat_budget_exceeded"]
    assert budget_event["endpoint"] == "attack"
    assert budget_event["cleanup_s"] <= 1.5
    if slow_cleanup:
        assert calls[calls.index(("attack", 7.5)) + 1][0] == "stop"
        assert clock[0] <= 1009.000001
    else:
        assert [a for a, _ in calls[-3:]] == ["stop", "control", "posture"]


@pytest.mark.parametrize("same_shooter", [False, True])
def test_death_during_combat_does_not_suppress_the_next_threat_at_spawn(monkeypatch, same_shooter):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    prior_threat = {
        "id": 43 if same_shooter else 42, "name": "skeleton" if same_shooter else "spider",
        "pos": [2.0, 64.0, 0.5], "dist": 1.5,
    }
    skeleton = {"id": 43, "name": "skeleton", "pos": [20.5, 64.0, 4.5], "dist": 4.0}
    status = {
        "pos": [0.5, 64.0, 0.5], "health": 8, "food": 20, "on_ground": True,
        "alive": True, "ready": True, "dimension": "overworld", "nearest_hostile": prior_threat,
    }
    lifecycle = []
    encounters = []

    class RespawnBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [ev for ev in lifecycle if ev["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    class RespawnDuringCombat:
        def on_encounter(self, _env, trigger):
            encounters.append(trigger["primary_entity_id"])
            if len(encounters) == 1:
                status.update(pos=[20.5, 64.0, 0.5], health=20, last_death={"t": 1}, nearest_hostile=skeleton)
                lifecycle.extend([{"seq": 1, "type": "death"}, {"seq": 2, "type": "spawn"}])
            return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status), "requires_replan": True}

    guard = GenerationGuard(RespawnBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.combat_slot.register(RespawnDuringCombat())
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    first = guard.poll()
    assert "death_or_connection_changed" in first["reasons"]
    clock[0] += 3.1
    second = guard.poll()

    assert encounters == [prior_threat["id"], skeleton["id"]]
    assert "pre_hurt_ranged" in second["reasons"]


@pytest.mark.parametrize("combat_duration", [1.0, 4.0])
def test_guard_eats_at_the_next_safe_poll_after_combat(monkeypatch, combat_duration):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    zombie = {"id": 42, "name": "zombie", "pos": [2.0, 11, 0.5], "dist": 1.5}
    status = {
        "pos": [0.5, 11, 0.5], "health": 14.2, "food": 17, "on_ground": True,
        "alive": True, "ready": True, "dimension": "overworld", "nearest_hostile": zombie,
    }
    meals = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 3}]}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "attack":
                clock[0] += combat_duration
                status["nearest_hostile"] = None
                return {"hits": 2, "killed": True}
            if action == "use_item":
                meals.append(body["item"])
                clock[0] += 4.0
                status["food"] = 20
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    outcome = guard._run_subcortical_combat({
        "source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie",
    })

    assert outcome["mode"] == "counter_killed"
    assert meals == []
    guard.poll()
    assert meals == ["cooked_beef"]


@pytest.mark.parametrize("scenario", ["close", "leaves_reach", "melee_edge", "far", "explicit_miss"])
def test_match_zombie_counter_reserves_time_for_axe_swing_interval(monkeypatch, scenario):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    distance = {"far": 4.0, "melee_edge": 3.1}.get(scenario, 1.6)
    zombie = {"id": 42, "name": "zombie", "dist": distance}
    status = {
        "pos": [-12208.4, -53.4, -6133.5], "health": 11, "ready": True, "alive": True,
        "dimension": "overworld", "on_ground": False, "nearest_hostile": zombie,
        "held": {"name": "iron_pickaxe"},
    }
    equipped = []
    attacks = []

    class MatchBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "wooden_axe", "count": 1}, {"name": "iron_pickaxe", "count": 1}]}
            if action == "get_entities":
                return {"entities": [] if attacks else [zombie]}
            if action == "equip":
                equipped.append(body)
            if action == "attack":
                attacks.append(body)
                if scenario == "explicit_miss":
                    return {"hits": 2, "connected_hits": 0, "target_hp_delta": 0, "killed": False}
                if scenario in {"melee_edge", "far"}:
                    return {"hits": 2, "killed": True}
                # The backend checks its timer between complete swing intervals.
                ticks, hits = 0, 0
                while ticks < body["timeout"]:
                    if hits == 0 or scenario == "close":
                        hits += 1
                        if hits == body.get("max_hits"):
                            break
                    ticks += 1250
                clock[0] += ticks / 1000
                if ticks / 1000 > kwargs["timeout_s"]:
                    raise httpx.ReadTimeout("attack reply timed out")
                return {"hits": hits, "killed": hits == 2, "hit_log": [{"entity_id": 42}]}
            return {"ok": True}

    outcome = SubcorticalInstinctController().on_encounter(CombatEnv(MatchBot()), {
        "source": "tool_interrupted_hurt", "primary_entity_id": 42, "primary_type": "zombie",
        "hurt_source": "mob_attack",
    })

    assert equipped == [{"item": "wooden_axe", "slot": "hand"}]
    attack, = attacks
    assert attack.get("chase") is (None if scenario == "far" else False)
    assert attack["feedback"] is True
    tight_counter = scenario in {"close", "leaves_reach", "explicit_miss"}
    assert attack["timeout"] == (1350 if tight_counter else 2600)
    assert attack.get("max_hits") == (2 if tight_counter else None)
    if scenario == "explicit_miss":
        melee, = [action for action in outcome["actions"] if action["action"] == "melee_attack"]
        assert not melee["ok"] and melee["hits"] == 2
        assert any(action["action"] == "unreachable_flee" for action in outcome["actions"])
        assert outcome["mode"] == "cover_or_flee"
        return
    if tight_counter:
        melee, = [action for action in outcome["actions"] if action["action"] == "melee_attack" and action["ok"]]
        assert melee["hit_log"] == [{"entity_id": 42}]
    assert outcome["mode"] == ("counter_kill" if scenario == "leaves_reach" else "counter_killed")


def test_subcortical_combat_timeout_sends_stop_and_preemption_survives_without_crashing(monkeypatch):
    """Reproduces glm_glm_3_village / hy4_hy_8_nether: ReadTimeout in subcortical_combat must /stop & not crash run."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    # 1) Bounded attack timeout on SubcorticalInstinctController
    seen_attack_payloads: list[dict] = []
    attack_result = "killed"

    class _AttackBot:
        def status(self):
            return {"pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "nearest_hostile": {"id": 5, "name": "zombie", "dist": 1.8}}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [{"id": 5, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.8}]}
            if action == "attack":
                seen_attack_payloads.append(dict(body or {}))
                if attack_result == "server_timeout":
                    raise ApiError("timeout", "timed out waiting for server attack")
                if attack_result == "zero_hits":
                    return {"hits": 0, "killed": False}
                return {"hits": 2, "killed": True}
            return {"ok": True}

    ctrl = SubcorticalInstinctController()
    out = ctrl.on_encounter(CombatEnv(_AttackBot()), {"source": "pre_hurt_melee", "primary_entity_id": 5, "primary_type": "zombie"})
    assert out["mode"] == "counter_killed"
    assert seen_attack_payloads and 1000 <= seen_attack_payloads[0]["timeout"] <= 3000

    for attack_result, fallback in [("server_timeout", "attack_timeout_flee"), ("zero_hits", "unreachable_flee")]:
        env = CombatEnv(_AttackBot())
        monkeypatch.setattr(env, "flee", lambda *_args, **_kwargs: {"ok": True, "mode": "flee"})
        out = SubcorticalInstinctController().on_encounter(
            env, {"source": "pre_hurt_melee", "primary_entity_id": 5, "primary_type": "zombie"},
        )
        assert out["mode"] == "cover_or_flee"
        assert {"action": fallback, "ok": True} in out["actions"]
        assert out["status_after"]["alive"] and out["status_after"]["ready"]

    # 2) ReadTimeout during run_tool_with_reflex preemption emits /stop, posture {}, and returns replan receipt
    stop_calls: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {
                "pos": [0.5, 64.0, 0.5], "health": 18.0, "food": 20, "alive": True, "ready": True,
                "on_ground": True, "in_water": False,
                "nearest_hostile": {"id": 7, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.5},
            }})
        if action == "events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "iron_sword", "count": 1}]}})
        if action == "get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": [{"id": 7, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.5}]}})
        if action == "attack":
            raise httpx.ReadTimeout("timed out waiting for server attack")
        if action in ("stop", "posture", "control"):
            stop_calls.append(action)
            return httpx.Response(200, json={"ok": True, "result": {}})
        if action == "dig":
            return httpx.Response(200, json={"ok": True, "result": {"interrupted_by": "hurt"}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    turns = {"n": 0}

    def chat(messages, tools=None, **kwargs):
        turns["n"] += 1
        if turns["n"] == 1:
            return {"role": "assistant", "content": "挖石", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "mc_dig", "arguments": json.dumps({"x": 1, "y": 64, "z": 0})}}
            ]}
        return {"role": "assistant", "content": "超时已恢复"}

    with BotSession("tok", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("test", bot, chat_fn=chat, max_calls=3, survival_guard=True, include_initial_snapshot=False)

    assert report["status"] == "answered"
    assert "stop" in stop_calls
    assert "posture" in stop_calls


@pytest.mark.parametrize("change", ["none", "hurt", "approaching", "another_enemy", "respawned"])
def test_boundary_avoids_repeating_an_unchanged_melee_warning_after_network_timeout(monkeypatch, change):
    spider = {"id": 7, "name": "spider", "pos": [2.5, 64, 0.5], "dist": 2.0}
    status = {"pos": [0.5, 64, 0.5], "health": 18, "food": 20, "on_ground": True, "ready": True,
              "alive": True, "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": spider}
    events = []
    calls = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [row for row in events if row["seq"] > kwargs["params"]["since"]]}
            return {"ok": True}

    def combat(trigger):
        calls.append(trigger)
        if len(calls) == 1:
            raise httpx.ReadTimeout("attack stalled")
        return {"handled": True, "mode": "cover_or_flee"}

    guard = GenerationGuard(RidgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    receipt = guard.handle_preemption({
        "source": "tool_interrupted_hurt", "primary_entity_id": 7, "primary_type": "spider", "hurt_source": "mob_attack",
    }, interrupted_name="mc_dig")
    assert receipt["reflex_mode"] == "network_timeout"
    if change == "hurt":
        status["health"] = 16
        events.append({"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": spider})
    elif change == "approaching":
        status["nearest_hostile"] = {**spider, "dist": 1.4}
    elif change == "another_enemy":
        status["nearest_hostile"] = {**spider, "id": 8}
    elif change == "respawned":
        status["last_death"] = {"t": 2}
    guard.stabilize_after_tools([json.dumps(receipt)])

    assert len(calls) == (1 if change == "none" else 2)


@pytest.mark.parametrize("after, should_retreat", [
    ({}, True),
    ({"nearest_hostile": {"name": "zombie", "dist": 4.1}}, False),
    ({"last_death": {"t": 2}}, False),
    ({"in_lava": True}, False),
])
def test_bug095_mining_interruption_keeps_escape_priority_after_combat_timeout(monkeypatch, after, should_retreat):
    status = {
        "pos": [-12208.4, -53.4, -6133.5], "health": 11, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1},
        "nearest_hostile": {"id": 42, "name": "zombie", "dist": 1.6},
    }

    class MineBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            return {"ok": True}

    def combat(_trigger):
        status.update(after)
        raise httpx.ReadTimeout("attack stalled")

    guard = GenerationGuard(MineBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)

    receipt = guard.handle_preemption({
        "source": "tool_interrupted_hurt", "primary_entity_id": 42, "primary_type": "zombie",
        "hurt_source": "mob_attack",
    }, interrupted_name="mc_skill_mine_dfs")

    assert receipt["requires_replan"] and receipt["status_after"]["health"] == 11
    assert receipt["reflex_mode"] == ("life_unavailable" if "last_death" in after else "network_timeout")
    assert ("立即调用 mc_skill_flee" in receipt["message"]) is should_retreat
    assert ("禁止继续挖掘或调用 mc_skill_mine_dfs（包括 reset）" in receipt["message"]) is should_retreat


@pytest.mark.parametrize("followup", ["unchanged", "closer", "hurt", "new_life", "new_target"])
def test_bug_009_stationary_melee_warning_does_not_cancel_again(monkeypatch, followup):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 5462415, "name": "zombie", "dist": 2.6, "pos": [40995.5, 61, 10998.5]}
    status = {
        "pos": [40994, 63, 10997.3], "dimension": "overworld", "health": 2, "food": 17, "last_death": {"t": 1},
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "nearest_hostile": zombie,
    }
    hits = []

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    def combat(_env, _trigger):
        return {
            "mode": "cover_or_flee", "handled": True,
            "actions": [{"action": "flee", "ok": False, "reason": "no_safe_retreat", "moved": 0.0}],
            "status_after": dict(status),
        }

    class Combat:
        def on_encounter(self, env, trigger):
            return combat(env, trigger)

    guard = GenerationGuard(LedgeBot(), EventSink(lambda _: None), checkpoint=lambda: None, combat_controller=Combat())
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    assert guard.poll()["requires_replan"] is True

    clock[0] += 15.5
    if followup == "closer":
        zombie["dist"] = 2.1
    elif followup == "hurt":
        status["health"] = 1.0
        hits.append({"seq": 1, "type": "hurt", "source": "mob_attack"})
    elif followup == "new_life":
        status["last_death"] = {"t": 2}
    elif followup == "new_target":
        zombie["id"] += 1

    assert guard.poll()["requires_replan"] is (followup != "unchanged")
    if followup == "unchanged":
        for _ in range(3):
            clock[0] += 15.5
            assert guard.poll()["requires_replan"] is False

