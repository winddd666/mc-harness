from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_urgent_bunker_uses_tool_and_does_not_chase_drops():
    bot = BunkerBot()
    result = MacroSkills(bot).dig_safety_bunker(urgent=True)
    assert result["ok"] is True
    assert result["roof_placed"] is True
    digs = [body for action, body in bot.calls if action == "dig"]
    assert len(digs) == 3 and all(body["collect"] is False for body in digs)
    assert next(body for action, body in bot.calls if action == "equip")["item"] == "stone_pickaxe"


def test_bunker_centers_edge_position_before_opening_shaft():
    bot = BunkerBot()
    bot.x = bot.z = 0.1
    result = MacroSkills(bot).dig_safety_bunker(urgent=True)
    assert result["ok"] is True and result["inside_shaft"] is True
    actions = [action for action, _ in bot.calls]
    assert actions.index("control") < actions.index("dig")


@pytest.mark.parametrize("open_side,roof,reason", [(True, True, "unsafe_open_side"), (False, False, "no_roof_block")])
def test_urgent_bunker_refuses_unsafe_geometry_and_missing_material(open_side, roof, reason):
    bot = BunkerBot(open_side=open_side, roof=roof)
    result = MacroSkills(bot).dig_safety_bunker(urgent=True)
    assert result["ok"] is False
    assert result["reason"] == reason
    assert not any(action in {"dig", "place", "equip"} for action, _ in bot.calls)


def test_guardian_snapshot_is_visible_before_next_tool():
    replay = ReplayIncrementer('guard.events.jsonl')
    frames = replay.feed_events([{
        "seq": 1, "elapsed": 3.0, "type": "guardian.incident", "reasons": ["fresh_hurt"],
        "status": {"pos": [1.5, 61, 2.5], "health": 16, "food": 20, "blocking": True, "ready": True},
        "actions": [{"action": "raise_shield", "ok": True}],
        "inventory_after": {"items": [{"name": "dirt", "count": 16}]},
    }])
    assert frames[-1]["kind"] == "guardian"
    assert frames[-1]["state_patch"]["pos"] == [1.5, 61, 2.5]
    assert replay.status["health"] == 16
    assert replay.status["blocking"] is True
    assert replay.inventory["items"] == [{"name": "dirt", "count": 16}]


def test_tool_api_error_becomes_receipt_and_run_keeps_going(monkeypatch):
    """A single tool-level ApiError must return a replan-able receipt instead of aborting the run."""
    from mc_agent.errors import ApiError
    from mc_agent.harness.tool_registry import Tool

    class _ToolsetStub:
        def last_block_pos(self):
            return (0, 64, 0)

        def update_block_pos(self, pos):
            return None

    from mc_agent.harness.agent import _RepeatFailureGuard

    monkeypatch.setattr(_RepeatFailureGuard, "_API_RETRY_DELAYS", (0.0, 0.0))

    def _missing(**kwargs):
        raise ApiError("missing_item", "no iron_pickaxe in inventory", context={"inventory": {"items": []}})

    tool = Tool(name="mc_equip", description="", parameters={}, handler=_missing, concurrency="exclusive")
    guard = _RepeatFailureGuard(_ToolsetStub())
    out = json.loads(guard.wrap_tool(tool).handler(item="iron_pickaxe"))
    assert out["ok"] is False
    assert out["requires_replan"] is True
    assert out["error"]["code"] == "missing_item"
    assert out["error"]["message"] == "no iron_pickaxe in inventory"
    assert "hint" in out and "craft_gear" in out["hint"]

    # transient codes get retried before surfacing a receipt
    attempts = {"n": 0}

    def _flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ApiError("not_connected", "socket closed")
        return json.dumps({"ok": True, "result": {}})

    flaky = Tool(name="mc_status", description="", parameters={}, handler=_flaky, concurrency="exclusive")
    raw = guard.wrap_tool(flaky).handler()
    assert attempts["n"] == 3
    assert json.loads(raw)["ok"] is True


def test_guardian_marks_disconnected_instead_of_raising():
    """Three consecutive status-read failures are a disconnect, not a program bug."""
    from mc_agent.errors import ApiError

    class _DeadBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            raise ApiError("not_connected", "socket closed")

    guard = GenerationGuard(_DeadBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    for _ in range(3):
        guard.last_poll = float("-inf")
        assert guard.poll() is None
    assert guard.disconnected is True


def test_disconnected_guard_finishes_run_with_disconnected_status(monkeypatch):
    """A dropped connection must end the run as status='disconnected', never 'runtime_error'."""

    def _poll(self):
        self.disconnected = True
        return None

    monkeypatch.setattr(GenerationGuard, "poll", _poll)

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            result = {"pos": [0.5, 64, 0.5], "health": 20, "food": 20, "alive": True, "ready": True}
        elif action == "inventory":
            result = {"items": []}
        elif action == "events":
            result = {"events": [], "last_seq": 0}
        else:
            result = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": result})

    def chat(messages, tools=None, *, control=None, **kwargs):
        check_control(control)
        return {"role": "assistant", "content": "不应到这里。"}

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("guard", bot, chat_fn=chat, max_calls=3, survival_guard=True,
                              include_initial_snapshot=False)

    assert report["status"] == "disconnected"
    assert "连接中断" in (report.get("error") or "")


def test_tool_transport_error_becomes_receipt_not_run_failure(monkeypatch):
    """httpx transport breaks (RemoteProtocolError) must not bypass the tool-level guard."""
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.tool_registry import Tool

    class _ToolsetStub:
        def last_block_pos(self):
            return (0, 64, 0)

        def update_block_pos(self, pos):
            return None

    monkeypatch.setattr(_RepeatFailureGuard, "_API_RETRY_DELAYS", (0.0, 0.0))
    attempts = {"n": 0}

    def _broken(**kwargs):
        attempts["n"] += 1
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    tool = Tool(name="mc_skill_mine_dfs", description="", parameters={}, handler=_broken, concurrency="exclusive")
    guard = _RepeatFailureGuard(_ToolsetStub())
    out = json.loads(guard.wrap_tool(tool).handler())

    assert attempts["n"] == 3                     # transient: retried twice, then surfaced
    assert out["ok"] is False
    assert out["error"]["code"] == "network_error"
    assert out["requires_replan"] is True


def test_low_hp_inside_own_shaft_bunkers_instead_of_running(monkeypatch):
    """59% of live deaths were mob/arrow chip damage: at low HP in our tunnel, seal and hold."""

    class _ShardSkills:
        class _Eng:
            nodes = {1: object()}

            def is_inside_shaft(self):
                return True

        mining_dfs = _Eng()

        def dig_safety_bunker(self, urgent=False):
            return {"ok": True, "inside_shaft": True, "roof_placed": True}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee"}

    class _HurtBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, -50.0, 0.5], "health": 7.0, "food": 20.0, "alive": True,
                        "ready": True, "in_water": False, "on_ground": True,
                        "nearest_hostile": {"name": "zombie", "dist": 3.0}}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "health": 7.0, "source": "mob_attack"}],
                        "last_seq": 1}
            if action == "inventory":
                return {"items": [], "offhand": {}}
            return {"ok": True}

    guard = GenerationGuard(_HurtBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.skills = _ShardSkills()
    guard.last_poll = float("-inf")
    incident = guard.poll()

    assert incident is not None
    assert [a["action"] for a in incident["actions"]] == ["emergency_bunker"]
    assert incident["actions"][0]["ok"] is True


def test_harness_nudges_model_that_claims_done_with_zero_goal_items():
    """Live m13: answered with diamond=0. The harness must challenge it exactly once."""
    events = []
    requests = {"n": 0}

    def chat(messages, tools=None, *, control=None, **kwargs):
        requests["n"] += 1
        return {"role": "assistant", "content": "任务完成，已挖到钻石。"}

    guard = GenerationGuard(_goal_bot([]), EventSink(lambda _: None), checkpoint=lambda: None)
    with BotSession("cached", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))) as bot:
        report = run_mc_agent("挖到钻石", bot, chat_fn=chat, max_calls=6, survival_guard=False,
                              include_initial_snapshot=False, on_event=events.append)

    assert report["status"] == "answered"
    assert requests["n"] == 2                              # nudge once, then accept
    assert any(e["type"] == "harness.goal_unmet" for e in events)


def test_harness_accepts_completion_when_goal_item_is_present():
    requests = {"n": 0}

    def chat(messages, tools=None, *, control=None, **kwargs):
        requests["n"] += 1
        return {"role": "assistant", "content": "任务完成，钻石已入包。"}

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": [{"name": "diamond", "count": 2}]}})
        if action == "status":
            return httpx.Response(200, json={"ok": True, "result": {
                "pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20.0, "alive": True, "ready": True}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("挖到钻石", bot, chat_fn=chat, max_calls=4, survival_guard=False,
                              include_initial_snapshot=False)
    assert report["status"] == "answered"
    assert requests["n"] == 1


@pytest.mark.parametrize("endpoint, body, expected", [
    ("dig", {"pos": [11, 67, 24]}, [11, 67, 24]),
    ("move_to", {"x": 11.5, "y": 69, "z": 24.5}, [11.5, 69, 24.5]),
    ("move_to", {"x": 11.5, "z": 24.5}, [11.5, None, 24.5]),
    ("dig", {"pos": [11, 24]}, None),
])
def test_interrupted_harvest_receipt_reports_the_dispatched_work_target(monkeypatch, endpoint, body, expected):
    shooter = {"id": 809241, "name": "skeleton", "dist": 2.3}
    status = {
        "pos": [10.5, 65, 20.5], "dimension": "overworld", "health": 12.0,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    emitted = []

    class CaveBot:
        def call(self, action, payload=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == endpoint:
                assert payload == body
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(emitted.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", lambda _trigger: {
        "handled": True, "mode": "cover_or_flee", "status_after": dict(status),
    })
    receipt = json.loads(guard.run_tool_with_reflex("mc_skill_harvest_logs", lambda: json.dumps(bot.call(endpoint, body))))

    assert receipt["error"]["code"] == "guardian_preempted"
    assert receipt["pos_at_preemption"] == status["pos"]
    event, = [event for event in emitted if event["type"] == "guardian.preempted_tool"]
    if expected is None:
        assert "last_work_action" not in receipt and "last_work_action" not in event
    else:
        assert receipt["last_work_action"] == event["last_work_action"] == {"endpoint": endpoint, "target": expected}
        assert receipt["preemption_threat"] == event["preemption_threat"] == {"id": shooter["id"], "name": shooter["name"]}


def test_bug077_prime_ages_deaths_and_retains_recent_live_deaths(monkeypatch):
    now_s = 1_795_200_000
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.time", lambda: now_s + clock[0] - 1000.0)
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    events = [
        {"type": "death", "seq": 1, "t": (now_s - 2400) * 1000},
        {"type": "death", "seq": 2},
        {"type": "death", "seq": 3, "t": (now_s - 42) * 1000},
    ]

    class ReplayBot:
        _last_event_seq = 0
        record_death_event = BotSession.record_death_event
        recent_deaths_in_window = BotSession.recent_deaths_in_window

        def __init__(self):
            self._seen_death_keys = set()
            self._death_mono_times = []

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"health": 20, "ready": True, "alive": True}
            if action == "inventory":
                return {}
            if action == "events":
                return {"events": [e for e in events if e["seq"] > kwargs["params"]["since"]], "last_seq": events[-1]["seq"]}
            raise AssertionError(action)

    bot = ReplayBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    guard.prime()

    assert guard.cursor == bot._last_event_seq == 3
    assert bot.recent_deaths_in_window() == [958.0]

    clock[0] = 1019.0
    recent = {"type": "death", "seq": 4, "t": (now_s + 10) * 1000}
    events.extend([recent, recent.copy(), {"type": "death", "seq": 5}])
    guard._drain_events_pages()
    guard._drain_events_pages()

    assert guard.cursor == 5
    assert bot.recent_deaths_in_window() == [1010.0, 1019.0]


def test_bug_010_respawn_not_ready_skips_eat_and_retries_after_ready(monkeypatch):
    """BUG-010: deaths or not status.ready skips eat + sets retry_after_ready; not_ready isn't counted as failure; eats once after ready returns."""
    from mc_agent.errors import ApiError

    clock = [600.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    use_item_calls: list[dict] = []
    events: list[dict] = []
    state = {
        "ready": False,
        "alive": False,
        "food": 12,
        "health": 18.0,
        "event_seq": 1,
        "events": [{"seq": 1, "type": "death"}, {"seq": 2, "type": "spawn"}],
        "eat_api_error": None,
    }

    class _RespawnBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [4.5, 64.0, 4.5],
                    "health": state["health"],
                    "food": state["food"],
                    "ready": state["ready"],
                    "alive": state["alive"],
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": False,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [e for e in state["events"] if int(e.get("seq", 0)) > since]
                return {"events": rows, "last_seq": max([since] + [int(e.get("seq", 0)) for e in rows])}
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 3}]}
            if action == "use_item":
                err_code = state["eat_api_error"]
                if err_code:
                    raise ApiError(err_code, f"transient {err_code}")
                use_item_calls.append(dict(body or {}))
                state["food"] = 18
                return {"ok": True}
            return {"ok": True}

    guard = GenerationGuard(_RespawnBot(), EventSink(events.append), checkpoint=lambda: None)

    # 1) Poll during death/spawn + not ready: must skip use_item and mark retry_after_ready=True
    inc_death = guard.poll()
    assert inc_death is not None
    assert "death_or_connection_changed" in inc_death["reasons"]
    assert use_item_calls == []
    assert guard.retry_after_ready is True
    assert guard.failures == 0
    assert not any(a["action"] == "eat" for a in inc_death["actions"])

    clock[0] += 4.0
    state["events"] = []
    assert guard.poll() is None  # The same loading window must not cancel the next generation.
    assert guard.retry_after_ready is True

    # 2) Even if ready=True arrives, if use_item returns ApiError('not_ready') or 'wrong_server', do NOT count as failure
    clock[0] += 0.5
    state["ready"] = True
    state["alive"] = True
    state["eat_api_error"] = "wrong_server"
    assert guard.poll() is None
    assert guard.retry_after_ready is True
    assert guard.failures == 0
    assert not any(e.get("type") == "guardian.action.completed" and e.get("ok") is False for e in events)

    # 3) Once server truly becomes ready (<3s later too!), poll() eats once and clears retry_after_ready
    clock[0] += 0.5
    state["eat_api_error"] = None
    inc_eat = guard.poll()
    assert inc_eat is not None
    assert [a["action"] for a in inc_eat["actions"]] == ["eat"]
    assert inc_eat["actions"][0]["ok"] is True
    assert inc_eat["requires_replan"] is False
    assert use_item_calls == [{"item": "bread"}]
    assert guard.retry_after_ready is False
    assert guard.failures == 0

    # 4) Subsequent poll at food=18 (above hunger threshold, retry_after_ready already cleared) does not eat again
    clock[0] += 5.0
    assert guard.poll() is None
    assert use_item_calls == [{"item": "bread"}]


@pytest.mark.parametrize("state", ["ready", "loading", "died_again"])
def test_guardian_defends_current_life_when_spawn_and_hurt_share_a_poll(monkeypatch, state):
    old_attacker = {"id": 30, "name": "skeleton", "pos": [12.5, 64, 0.5]}
    zombie = {"id": 44, "name": "zombie", "pos": [1.8, 61, 0.5], "dist": 1.3}
    hits = [
        {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": old_attacker},
        {"seq": 2, "type": "death"},
        {"seq": 3, "type": "spawn"},
        {"seq": 4, "type": "hurt", "source": "mob_attack", "source_entity": zombie},
    ]
    if state == "died_again":
        hits.append({"seq": 5, "type": "death"})
    retreats = []

    class RespawnBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 61, 0.5], "health": 7, "food": 20, "dimension": "overworld",
                    "ready": state == "ready", "alive": state != "died_again", "on_ground": False,
                    "nearest_hostile": zombie,
                }
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [event for event in hits if event["seq"] > since]}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    guard = GenerationGuard(RespawnBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def retreat(_status, attacker, _counts):
        retreats.append(attacker)
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()
    assert "death_or_connection_changed" in incident["reasons"]
    assert incident["requires_replan"]
    assert incident["event_seqs"] == [event["seq"] for event in hits]
    assert retreats == ([zombie] if state == "ready" else [])


def test_bug_011_guardian_http_error_returns_network_uncertain_without_crashing_episode(monkeypatch):
    """BUG-011: httpx.HTTPError (ReadTimeout/ReadError) inside _action or guard.poll() must never raise RuntimeError or terminate episode."""
    clock = [700.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    # 1) Direct _action calls return structured network_uncertain on ReadTimeout and ReadError
    class _DummyBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            return {"ok": True}

    guard = GenerationGuard(_DummyBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    out_timeout = guard._action("subcortical_combat", lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")))
    assert out_timeout["ok"] is False
    assert out_timeout["error"]["code"] == "network_uncertain"

    out_read_err = guard._action("subcortical_combat", lambda: (_ for _ in ()).throw(httpx.ReadError("reset by peer")))
    assert out_read_err["ok"] is False
    assert out_read_err["error"]["code"] == "network_uncertain"

    # 2) Poll-time attack ReadTimeout in run_mc_agent cancels waiting generation and continues to status='answered'
    attack_timed_out = {"active": False, "done": False}
    events: list[dict] = []

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            result = {
                "pos": [0.5, 64.0, 0.5],
                "health": 20.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "blocking": False,
                "nearest_hostile": (
                    {"id": 19, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.5}
                    if attack_timed_out["active"] and not attack_timed_out["done"]
                    else None
                ),
            }
        elif action == "events":
            result = {"last_seq": 0, "events": []}
        elif action == "inventory":
            result = {"items": [{"name": "iron_sword", "count": 1}]}
        elif action == "get_entities":
            result = {
                "entities": (
                    [{"id": 19, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.5}]
                    if attack_timed_out["active"] and not attack_timed_out["done"]
                    else []
                )
            }
        elif action == "attack":
            attack_timed_out["done"] = True
            raise httpx.ReadTimeout("attack read timeout")
        elif action == "get_block":
            body = json.loads(request.content) if request.content else {}
            y = body.get("pos", [0, 64, 0])[1]
            result = {"name": "stone" if y < 64 else "air"}
        else:
            result = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": result})

    turns = {"n": 0}

    def chat(messages, tools=None, *, control=None, **kwargs):
        turns["n"] += 1
        if turns["n"] == 1:
            attack_timed_out["active"] = True
            clock[0] += 0.5
            check_control(control)
            pytest.fail("Expected poll reflex timeout incident to replan turn 1 instead of crashing.")
        return {"role": "assistant", "content": "网络超时已降级为结构化恢复，本集继续完成。"}

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        rep = run_mc_agent(
            "bug-011 network_uncertain recovery",
            bot,
            chat_fn=chat,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
            on_event=events.append,
        )
    assert rep["status"] == "answered"
    assert any(
        e.get("type") == "guardian.action.completed"
        and e.get("ok") is False
        and isinstance(e.get("error"), dict)
        and e["error"].get("code") == "network_uncertain"
        for e in events
    )


@pytest.mark.parametrize(("oxygen", "terrain"), [
    (0, "air"), (4, "air"), (None, "air"), (0, "blocked"), (0, "new_life"),
])
def test_w19_stalled_underground_ascent_reaches_verified_air_pocket(oxygen, terrain):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    start_pos = [219029.7, 34.2, 172182.3]
    pocket = [219029.5, 35.2, 172184.5]
    status = {
        "pos": start_pos, "dimension": "overworld", "last_death": {"t": 1}, "yaw": 0.0,
        "health": 9.33, "ready": True, "alive": True, "on_ground": False,
        "in_water": True, "submerged": True, "oxygen": oxygen,
    }
    motion = []

    class CaveWaterBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                if terrain == "new_life" and not motion:
                    status.update(last_death={"t": 2}, pos=[219031.5, 65, 172190.5])
                return self.status()
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if x not in {219029, 219030} or z not in range(172182, 172185):
                    return {"name": "stone"}
                if y == 35 and z == 172183 and terrain == "blocked":
                    return {"name": "stone"}
                if y in {34, 35}:
                    return {"name": "water"}
                return {"name": "air" if z == 172184 and y in {36, 37} else "stone"}
            if action == "look":
                ux, uz = yaw_to_unit_xz(body["yaw"])
                if uz > 0.9 and abs(ux) < 0.2:
                    motion.append("aim_at_air")
            if action == "control" and body.get("forward"):
                if motion == ["aim_at_air"]:
                    motion.append("swim_to_air")
                    status.update(pos=pocket, submerged=False, oxygen=4)
                else:
                    motion.append("fallback")
            if action == "move_to":
                motion.append("pathfind")
            return {"ok": True}

    result = CombatEnv(CaveWaterBot()).escape_deep_water_to_shore(surface_first=True)

    if terrain == "air":
        assert motion == ["aim_at_air", "swim_to_air"]
        assert result["actions"] == ["water_surface_surge", "water_air_pocket"]
        assert result["mode"] == "breathing" and result["in_water"] is True and result["ok"] is False
        assert result["pos_after"] == pocket
    elif terrain == "new_life":
        assert result["mode"] == "respawned" and not motion
    else:
        assert "water_air_pocket" not in result["actions"]
        assert result["mode"] == "surface_retreat" and result["in_water"] is True


@pytest.mark.parametrize("landing", ["pursued", "clear", "still_airborne", "water", "respawned"])
def test_low_health_after_a_kill_waits_for_safe_footing_and_faces_the_pursuer(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _delay: None)
    target = {"id": 291033, "name": "zombie", "dist": 0.8, "pos": [1.3, 75, 0.5]}
    survivor = {"id": 291376, "name": "zombie", "dist": 2.3, "pos": [2.8, 75, 0.5]}
    sword = {"name": "iron_sword", "count": 1}
    shield = {"name": "shield", "count": 1}
    status = {
        "pos": [0.5, 75, 0.5], "health": 16.3, "food": 20, "dimension": "overworld", "last_death": {"t": 1},
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": target,
        "held": sword, "offhand": shield, "blocking": True,
    }
    actions = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [sword], "held": sword, "offhand": shield}
            if action == "get_entities":
                if body["radius"] == 6:
                    assert kwargs["timeout_s"] == 0.5
                    actions.append("sighting")
                    return {"entities": [survivor]}
                return {"entities": [target]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 75 else "air"}
            if action == "attack":
                assert body["entity_id"] == target["id"]
                clock[0] += 5.8
                hostile_hint = {key: survivor[key] for key in ("id", "name", "dist")}
                status.update(health=6.02, on_ground=False, pos=[0.5, 75.4, 0.5], nearest_hostile=hostile_hint)
                return {"killed": True, "hits": 1}
            if action == "status":
                actions.append("landing_check")
                if landing == "respawned":
                    status.update(last_death={"t": 2}, pos=[25.5, 75, 0.5], blocking=False)
                elif landing == "water":
                    status.update(in_water=True, submerged=True, blocking=False)
                elif landing != "still_airborne":
                    status.update(on_ground=True, pos=[0.5, 75, 0.5])
                return self.status()
            if action == "look" and "entity_id" in body:
                actions.append(("face", body["entity_id"]))
            if action == "posture":
                status["blocking"] = bool(body.get("shield"))
            if action == "control" and body.get("forward"):
                assert status["on_ground"] and not status.get("in_water")
                actions.append("retreat")
                status.update(pos=[-1.5, 75, 0.5], nearest_hostile=survivor if landing == "pursued" else None)
            assert action != "move_to", "there is only time for one safe retreat pulse"
            return {"ok": True}

    outcome = CombatControllerSlot().handle(CombatEnv(CaveBot()), {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": target["id"],
    })

    retreat, = (action for action in outcome["actions"] if action["action"] == "low_hp_abort_flee")
    assert actions[:3] == [("face", survivor["id"]), "sighting", "landing_check"]
    assert ("retreat" in actions) is (landing in {"pursued", "clear"})
    assert retreat["ok"] is (landing in {"pursued", "clear"})
    assert outcome["mode"] == ("life_unavailable" if landing == "respawned" else "low_hp_disengage")
    assert status["blocking"] is outcome["latched_guard"] is (landing in {"pursued", "still_airborne"})
    assert actions.count(("face", survivor["id"])) == (2 if landing in {"pursued", "still_airborne"} else 1)

