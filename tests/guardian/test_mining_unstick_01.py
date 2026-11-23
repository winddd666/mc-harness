from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

def test_bunker_that_digs_but_never_enters_does_not_seal_or_claim_success(monkeypatch):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    bot = BunkerBot()
    original = bot.call

    def no_fall(action, body):
        result = original(action, body)
        if action == "dig":
            bot.y = 64
        return result

    bot.call = no_fall
    result = MacroSkills(bot).dig_safety_bunker(urgent=True)
    assert result["ok"] is False and result["requires_replan"] is True
    assert result["reason"] == "shaft_entry_not_confirmed"
    assert len([a for a, _ in bot.calls if a == "dig"]) == 1
    assert not any(a == "place" for a, _ in bot.calls)


def test_in_wall_hurt_triggers_unstick_instead_of_bunker(monkeypatch):
    """Suffocating inside a block must carve out (no pickaxe needed), never bunker (needs a pickaxe)."""
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    state = {"hurt": False}
    requests = 0
    events = []
    skill_calls = []

    monkeypatch.setattr(
        MacroSkills, "unstick", lambda self: skill_calls.append("unstick") or {"ok": True, "patched": True}
    )
    monkeypatch.setattr(
        MacroSkills,
        "dig_safety_bunker",
        lambda self, **kwargs: skill_calls.append("bunker") or {"ok": True, "patched": True},
    )

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "events":
            since = int(request.url.params.get("since", 0)) if request.method == "GET" else 0
            rows = [{"seq": 1, "type": "hurt", "health": 12, "source": "in_wall"}] if state["hurt"] and since < 1 else []
            result = {"events": rows, "last_seq": 1 if state["hurt"] else 0}
        elif action == "status":
            result = {"pos": [0.5, 64, 0.5], "health": 12 if state["hurt"] else 20, "food": 20,
                      "alive": True, "ready": True, "on_ground": True, "in_water": False}
        elif action == "inventory":
            result = {"items": []}
        else:
            result = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": result})

    def chat(messages, tools=None, *, control=None, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            state["hurt"] = True
            clock[0] += 1
            check_control(control)
            pytest.fail("in_wall reflex should have cancelled the stale generation.")
        return {"role": "assistant", "content": "已从墙里脱困，按新状态重规划。"}

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("guard", bot, chat_fn=chat, max_calls=4, survival_guard=True,
                              include_initial_snapshot=False, on_event=events.append)

    assert report["status"] == "answered"
    assert skill_calls == ["unstick"]
    started = [e["action"] for e in events if e["type"] == "guardian.action.started"]
    assert "unstick_in_wall" in started
    assert "emergency_bunker" not in started


@pytest.mark.parametrize("failure", ["terrain_read", "dig_released", "dig_rejected", "dig_stop_timeout"])
def test_unstick_transport_failure_keeps_the_actor_in_the_same_episode(monkeypatch, failure):
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _seconds: None)
    events = []
    calls = []
    tool_receipts = []
    y = 64

    def observe(event):
        events.append(event)

    def handle(request):
        nonlocal y
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "events":
            result = {"events": [], "last_seq": 0}
        elif endpoint == "status":
            result = {"pos": [0.5, y, 0.5], "health": 20, "food": 20, "alive": True, "ready": True,
                      "on_ground": True, "in_water": False, "dimension": "overworld"}
        elif endpoint == "inventory":
            result = {"items": []}
        elif endpoint == "get_block":
            body = json.loads(request.content)
            if failure == "terrain_read":
                calls.append("get_block")
                raise httpx.ReadTimeout("canopy support was not read")
            floor = body["pos"][1]
            result = {"name": {63: "oak_leaves", 62: "oak_log"}.get(floor, "stone"), "diggable": True}
        elif endpoint == "dig":
            calls.append("dig")
            if calls.count("dig") == 1:
                y = 63
                result = {"dug": "oak_leaves", "gained": {"oak_sapling": 1}}
            else:
                raise httpx.ReadTimeout("canopy dig acknowledgement was lost")
        elif endpoint == "stop":
            calls.append("stop")
            assert request.extensions["timeout"]["read"] <= 1.5
            if failure == "dig_stop_timeout":
                raise httpx.ReadTimeout("action release is unknown")
            result = {"stopped": True, "released": failure == "dig_released"}
        else:
            result = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": result})

    def chat(messages, tools=None, *, control=None, **kwargs):
        receipt = next((m for m in messages if m.get("tool_call_id") == "unstick-timeout"), None)
        if receipt is None:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "unstick-timeout", "type": "function", "function": {"name": "mc_skill_unstick", "arguments": "{}"},
            }]}
        tool_receipts.append(json.loads(receipt["content"]))
        return {"role": "assistant", "content": "已收到脱困结果，检查当前位置后换路线。"}

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent("检查脱困结果", bot, chat_fn=chat, max_calls=3, survival_guard=True,
                              include_initial_snapshot=False, on_event=observe)

    assert report["status"] == "answered"
    receipt, = tool_receipts
    assert receipt["ok"] is False and receipt["requires_replan"] is True
    assert receipt["error"]["code"] == "network_uncertain" and receipt["stop_reason"] == "network_timeout"
    assert receipt["error"]["endpoint"] == ("get_block" if failure == "terrain_read" else "dig")
    assert receipt["status_after"]["pos"] == [0.5, y, 0.5]
    assert calls == (["get_block"] if failure == "terrain_read" else ["dig", "dig", "stop"])
    if failure.startswith("dig"):
        assert receipt["partial_progress"] is True and receipt["dug_count"] == 1
        assert receipt["gained"] == {"oak_sapling": 1}
    if failure in {"dig_released", "dig_rejected"}:
        assert receipt["action_released"] is (failure == "dig_released")
    else:
        assert "action_released" not in receipt
    event, = [event for event in events if event["type"] == "guardian.unstick_transport_failed"]
    assert event["endpoint"] == receipt["error"]["endpoint"]


@pytest.mark.parametrize("entry", ["poll", "tool_boundary"])
@pytest.mark.parametrize("scenario", ["plain_fall", "near_melee", "mixed_hurt", "attributed_fall"])
def test_unattributed_fall_uses_independent_threat_detection(monkeypatch, entry, scenario):
    distance = 2.5 if scenario == "near_melee" else 4.0
    zombie = {"id": 42, "name": "zombie", "dist": distance, "pos": [0.5, 72, 0.5 + distance]}
    status = {"pos": [0.5, 72, 0.5], "health": 16, "food": 20, "on_ground": True, "ready": True,
              "alive": True, "dimension": "overworld", "nearest_hostile": zombie}
    events = [{"seq": 1, "type": "hurt", "source": "fall"}]
    if scenario == "mixed_hurt":
        events.append({"seq": 2, "type": "hurt", "source": "mob_attack", "source_entity": zombie})
    elif scenario == "attributed_fall":
        events[0]["source_entity"] = zombie
    reactions = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [row for row in events if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            pytest.fail(f"Unexpected action after falling: {action}")

    def react(source):
        reactions.append(source)
        return {"ok": True, "handled": True, "mode": "flee"}

    guard = GenerationGuard(RidgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    monkeypatch.setattr(guard, "_run_subcortical_combat", lambda trigger: react(trigger["source"]))
    monkeypatch.setattr(guard, "_defend_or_flee", lambda *_a, **_kw: react("fresh_hurt"))
    response = guard.poll() if entry == "poll" else guard.stabilize_after_tools()

    if scenario == "plain_fall":
        assert reactions == [] and response is None
    elif scenario == "near_melee":
        assert reactions == ["pre_hurt_melee"]
    else:
        assert reactions == (["fresh_hurt"] if entry == "poll" else ["boundary_threat"])


def test_unstick_canopy_branch_shares_the_tool_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.library.time.sleep", lambda _: None)
    calls = []
    events = []

    class CanopyBot:
        def status(self):
            return self.call("status", method="GET")

        def call(self, action, body=None, **kwargs):
            timeout = kwargs["timeout_s"]
            assert kwargs["allow_reauth"] is False
            calls.append((action, clock[0], timeout))
            if action == "status":
                return {"pos": [0.5, 52.0, 0.5], "ready": True, "alive": True, "on_ground": True}
            if action == "get_block":
                return {"name": "oak_leaves" if body["pos"][1] == 51 else "oak_log", "diggable": True}
            if action == "dig":
                clock[0] += timeout
                if clock[0] >= 1028.0:
                    raise httpx.ReadTimeout("canopy dig reached the tool budget")
            return {"ok": True}

    bot = CanopyBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)
    original_call = bot.call
    result = json.loads(
        guard.run_tool_with_reflex("mc_skill_unstick", lambda: json.dumps(skills.unstick(max_duration_s=60.0)))
    )

    assert result["ok"] is False and result["requires_replan"] is True
    assert result["error"]["code"] == "time_budget"
    assert result["interrupted_tool"] == "mc_skill_unstick"
    assert calls[-1] == ("stop", 1028.0, 1.5)
    assert all(start + timeout <= 1028.0 for _, start, timeout in calls[:-1])
    assert bot.call == original_call
    assert events[-1]["type"] == "guardian.recovery_budget_exceeded"


@pytest.mark.parametrize("solid_exit", [True, False, "far_support_changed"])
@pytest.mark.parametrize("after_step", ["unshielded", "blocking", "respawned"])
def test_guardian_checks_support_before_unstick_moves(solid_exit, after_step):
    movements = []
    faces = []
    far_floor_reads = 0
    death = {"t": 10}

    class LedgeBot:
        def status(self):
            return self.call("status", method="GET")

        def call(self, action, body=None, **kwargs):
            nonlocal far_floor_reads
            if action == "status":
                return {
                    "pos": [0.5, 44, 0.5], "ready": True, "alive": True, "on_ground": True,
                    "health": 1.8, "blocking": after_step != "unshielded", "last_death": dict(death),
                    "nearest_hostile": {"id": 44, "name": "pillager", "dist": 10.2},
                }
            if action == "get_block":
                x, y, z = body["pos"]
                if solid_exit == "far_support_changed" and (x, y, z) == (2, 43, 0):
                    far_floor_reads += 1
                    if far_floor_reads > 1:
                        return {"name": "air"}
                if y == 43 and (solid_exit or (x, z) == (0, 0)):
                    return {"name": "stone", "diggable": True}
                return {"name": "air"}
            if action == "control":
                movements.append(body)
                if after_step == "respawned":
                    death["t"] = 20
            if action == "look" and "entity_id" in body:
                assert movements
                faces.append(body["entity_id"])
            return {"ok": True}

    bot = LedgeBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=skills)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_unstick", lambda: json.dumps(skills.unstick())))
    assert result["ok"] is bool(solid_exit)
    if solid_exit:
        step, = movements
        assert step["ms"] == (150 if solid_exit == "far_support_changed" else 350)
        assert not step["jump"] and not step["sprint"]
    else:
        assert result["error"]["code"] == "unsafe_landing" and result["requires_replan"] is True
        assert movements == []
    assert faces == ([44] if solid_exit and after_step == "blocking" else [])


@pytest.mark.parametrize("terrain", ["level_short_exit", "unsupported_exit", "raised_exit", "full_exit"])
def test_unstick_guard_keeps_a_verified_short_exit_with_a_lower_pulse(terrain):
    origin = [112182.5, 64, 122770.5]
    movements = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": origin, "on_ground": True, "ready": True, "alive": True}
            if action == "get_block":
                x, y, z = body["pos"]
                near = x == 112182 and z in (122769, 122770)
                supported = y == 63 and (
                    terrain == "full_exit" or (x, z) == (112182, 122770) or (near and terrain != "unsupported_exit")
                )
                upper_block = terrain == "raised_exit" and z == 122769 and y == 64
                return {"name": "stone" if supported or upper_block else "air"}
            if action == "control":
                movements.append(body)
            return {"ok": True}

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_unstick", lambda: json.dumps(bot.call(
        "control", {"forward": True, "jump": False, "yaw": 0.0, "ms": 350, "stop_on": []},
    ))))

    assert result["ok"] is (terrain in {"level_short_exit", "full_exit"})
    if result["ok"]:
        step, = movements
        assert step["ms"] == (150 if terrain == "level_short_exit" else 350)
        assert step["sprint"] is False and step["jump"] is False
    else:
        assert not movements and result["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize(("above_roof", "in_water", "error_code"), [
    ("lava", False, "unsafe_lava_roof"), ("flowing_lava", False, "unsafe_lava_roof"),
    ("water", False, "unsafe_water_roof"), ("flowing_water", False, "unsafe_water_roof"),
    ("water", True, None), ("air", False, None),
])
def test_unstick_preserves_a_roof_that_holds_back_fluid(above_roof, in_water, error_code):
    target = [-421, 26, -1914]
    digs = []

    class PocketBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-420.5, 24, -1913.5], "on_ground": True,
                    "in_water": in_water, "ready": True, "alive": True,
                }
            if action == "get_block":
                assert body["pos"] == [-421, 27, -1914]
                return {"name": above_roof}
            if action == "dig":
                digs.append(body["pos"])
            return {"ok": True}

    bot = PocketBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex(
        "mc_skill_unstick", lambda: json.dumps(bot.call("dig", {"pos": target, "stop_on": ["death"]})),
    ))

    if error_code is None:
        assert result["ok"] is True and digs == [target]
    else:
        assert result["error"]["code"] == error_code and result["requires_replan"] is True
        assert digs == []


@pytest.mark.parametrize(("tool", "terrain"), [
    ("mc_dig", "deep_drop"), ("mc_dig", "shallow_drop"), ("mc_dig", "lava"),
    ("mc_dig", "airborne_lava"), ("mc_dig", "offset_lava"), ("mc_dig", "airborne_side_dig"),
    ("mc_dig", "side_dig"), ("mc_dig", "edge_support"), ("mc_dig", "edge_dig"),
    ("mc_skill_staircase", "deep_drop"), ("mc_skill_staircase", "shallow_drop"),
    ("mc_skill_staircase", "dripstone"), ("mc_skill_staircase", "side_dig"),
    ("mc_skill_mine_dfs", "deep_drop"), ("mc_skill_mine_dfs", "shallow_drop"), ("mc_skill_mine_dfs", "side_dig"),
])
def test_dig_checks_the_landing_before_removing_support(tool, terrain):
    from mc_agent.harness.mc_tools import McToolset

    target = [2015 if terrain in {"side_dig", "edge_dig", "airborne_side_dig"} else 2014, 70, 1882]
    x = 2014.8 if terrain in {"edge_support", "edge_dig"} else 2014.5
    airborne = terrain.startswith("airborne_")
    feet_y = 71.2 if airborne or terrain == "offset_lava" else 71.0
    digs = []
    probes = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [x, feet_y, 1882.5], "on_ground": not airborne, "ready": True, "alive": True}
            if action == "get_block":
                probes.append(body["pos"])
                y = body["pos"][1]
                if y == 69 and terrain in {"lava", "airborne_lava", "offset_lava"}:
                    return {"name": "lava"}
                if y == 69 and terrain == "dripstone":
                    return {"name": "pointed_dripstone"}
                if y == 68 and terrain == "shallow_drop":
                    return {"name": "netherrack"}
                return {"name": "netherrack" if y >= 70 else "air"}
            if action == "dig":
                digs.append(body["pos"])
            return {"ok": True}

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    toolset = McToolset(bot)

    def invoke():
        if tool == "mc_dig":
            return toolset.mc_dig(*target)
        return json.dumps(bot.call("dig", {"pos": target, "collect": True, "stop_on": ["death"]}))

    result = json.loads(guard.run_tool_with_reflex(tool, invoke))

    if terrain in {"deep_drop", "lava", "airborne_lava", "offset_lava", "dripstone", "edge_support"}:
        assert result["ok"] is False and result["requires_replan"] is True
        assert result["error"]["code"] == "unsafe_landing"
        assert digs == []
    else:
        assert result["ok"] is True and digs == [target]
    if terrain in {"side_dig", "airborne_side_dig"}:
        assert probes == ([target] if tool == "mc_dig" else [])


@pytest.mark.parametrize("terrain", [
    "stone", "blocked_approach", "deep_drop", "lava", "unavailable", "slow_read", "within_remote_budget",
    "budget_exhausted", "repaired", "repair_unconfirmed", "partial_then_denied",
])
def test_dfs_probes_fresh_support_before_moving_to_a_mining_step(monkeypatch, terrain):
    target = [23080.5, 34.0, 10014.5]
    support = [23080, 33, 10014]
    events = []
    calls = []
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [23081.5, 35, 10015.5], "on_ground": True, "ready": True, "alive": True, "health": 20}
            if action == "get_block":
                assert body["pos"] == support
                calls.append(action)
                if terrain == "unavailable" or (terrain == "slow_read" and calls.count(action) == 1):
                    raise httpx.ReadTimeout("support block was not read")
                if terrain in {"within_remote_budget", "budget_exhausted"} and calls.count(action) == 1:
                    clock[0] += 2.4 if terrain == "within_remote_budget" else 2.6
                if terrain in {"repair_unconfirmed", "partial_then_denied"} or (terrain == "repaired" and "place" not in calls):
                    return {"name": "air"}
                return {"name": {"deep_drop": "air", "lava": "lava"}.get(terrain, "stone")}
            if action == "move_to":
                calls.append(action)
                if terrain == "blocked_approach":
                    raise ApiError("path_not_found", "terrain blocked the direct approach")
            if action in {"control", "place"}:
                calls.append(action)
            if action == "dig":
                calls.append(action)
                return {"dug": "iron_ore", "gained": {"raw_iron": 1}}
            return {"ok": True}

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None)

    def step():
        if terrain == "partial_then_denied":
            bot.call("dig", {"pos": [23082, 35, 10015], "collect": True})
        try:
            return json.dumps(bot.call("move_to", {
                "x": target[0], "y": target[1], "z": target[2], "range": 0.35,
                "timeout": 4500, "stop_on": ["hurt", "death"],
            }))
        except ApiError:
            if terrain in {"repaired", "repair_unconfirmed"}:
                bot.call("place", {"pos": support, "item": "cobblestone"})
            return json.dumps(bot.call("control", {"forward": True, "ms": 420, "stop_on": ["hurt", "death"]}))

    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", step))

    if terrain in {"stone", "blocked_approach", "slow_read", "within_remote_budget"}:
        assert result["ok"]
        reads = 2 if terrain == "slow_read" else 1
        assert calls == ["get_block"] * reads + ["move_to"] + (["control"] if terrain == "blocked_approach" else [])
    elif terrain == "repaired":
        assert result["ok"] and calls == ["get_block", "place", "get_block", "control"]
    else:
        reason = "mining_footing_unavailable" if terrain in {"unavailable", "budget_exhausted"} else "unsafe_landing"
        assert result["requires_replan"] and result["error"]["code"] == reason
        if terrain == "repair_unconfirmed":
            assert calls == ["get_block", "place", "get_block"]
        elif terrain == "partial_then_denied":
            assert calls == ["dig", "get_block"]
            assert result["partial_progress"] is True and result["dug_count"] == 1
            assert result["gained"] == {"raw_iron": 1}
        else:
            assert calls == ["get_block"] * (2 if terrain == "unavailable" else 1)
        rejected_events = [row for row in events if row["type"] == "guardian.mining_landing_blocked"]
        assert len(rejected_events) == (2 if terrain == "repair_unconfirmed" else 1)
        assert all(event["target"] == result["error"]["target"] == target and event["reason"] == reason for event in rejected_events)
        if terrain == "partial_then_denied":
            assert rejected_events[0]["gained"] == {"raw_iron": 1}


@pytest.mark.parametrize("terrain", ["cliff", "lava", "supported_descent", "terrain_unavailable"])
def test_dfs_fallback_checks_past_the_supported_first_step(tmp_path, terrain):
    from mc_agent.skills.mining_dfs import MiningDFSEngine

    origin = [112229.5, 14, 122771.5]
    target = (112229, 13, 122770)
    destination = [112229.5, 13, 122770.5]
    position = list(origin)
    movements = []
    blocked = terrain in {"cliff", "lava"}

    class ShaftBot:
        def status(self):
            return {"pos": list(position), "on_ground": True, "ready": True, "alive": True, "yaw": 0.0}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                _, y, z = body["pos"]
                if z <= 122769 and terrain == "terrain_unavailable":
                    raise httpx.ReadTimeout("cliff slope unavailable")
                if z <= 122769 and y == 12 and terrain == "lava":
                    return {"name": "lava"}
                supported = y <= 12 and (z >= 122770 or terrain == "supported_descent")
                return {"name": "stone" if supported else "air"}
            if action == "move_to":
                raise ApiError("path_not_found", "pathfinder could not descend one block")
            if action == "control":
                movements.append(body)
                position[:] = [112229.5, -18, 122767.2] if blocked else destination
            return {"ok": True}

    bot = ShaftBot()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)
    dfs = MiningDFSEngine(MacroSkills(bot), ledger_path=tmp_path / "ledger.json")

    result = json.loads(guard.run_tool_with_reflex(
        "mc_skill_mine_dfs", lambda: json.dumps({"ok": dfs._move_exact(target)}),
    ))

    assert result["ok"] is (not blocked)
    assert len(movements) == int(not blocked)
    denials = [event for event in events.events if event["type"] == "guardian.mining_fallback_blocked"]
    if blocked:
        assert len(denials) == 1 and denials[0]["hazard"] == terrain
        assert position == origin
    else:
        assert denials == [] and position == destination


@pytest.mark.parametrize("terrain", ["cliff", "lava", "supported", "unavailable"])
def test_dfs_checks_actual_direction_of_a_jump_before_its_first_waypoint(tmp_path, terrain):
    from mc_agent.skills.mining_dfs import MiningDFSEngine

    origin = (112229, 14, 122771)
    next_step = (112230, 15, 122771)
    start = [112229.5, 14, 122771.5]
    landing = [112230.5, 15, 122771.5]
    pos = list(start)
    motions = []
    hazard = terrain in {"cliff", "lava"}

    class ShaftBot:
        def status(self):
            return {"pos": list(pos), "on_ground": True, "ready": True, "alive": True, "yaw": 0.0}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                x, y, _ = body["pos"]
                if x >= 112231:
                    if terrain == "unavailable":
                        raise httpx.ReadTimeout("terrain beyond the next step is unavailable")
                    if hazard:
                        return {"name": "lava" if terrain == "lava" and y == 14 else "air"}
                support_y = 14 if x >= 112230 else 13
                return {"name": "stone" if y <= support_y else "air"}
            if action in {"control", "move_to"}:
                motions.append((action, body))
                pos[:] = landing
            return {"ok": True}

    bot = ShaftBot()
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None)
    dfs = MiningDFSEngine(MacroSkills(bot), ledger_path=tmp_path / "ledger.json")
    result = json.loads(guard.run_tool_with_reflex(
        "mc_skill_mine_dfs", lambda: json.dumps({"ok": dfs._step_to(next_step, from_cell=origin)}),
    ))

    assert result["ok"] is True and pos == landing
    action, payload = motions[0]
    assert len(motions) == 1
    denials = [record for record in events.events if record["type"] == "guardian.mining_fallback_blocked"]
    if hazard:
        assert action == "move_to"
        assert [payload[axis] for axis in ("x", "y", "z")] == landing
        denial, = denials
        assert denial["hazard"] == terrain and denial["yaw"] == pytest.approx(-math.pi / 2)
    else:
        assert action == "control" and payload["jump"] is True and denials == []


@pytest.mark.parametrize("terrain", ["unchanged", "repaired_edge", "repair_unconfirmed", "expiry", "unavailable"])
def test_dfs_rechecks_a_rejected_waypoint_only_when_the_observation_can_change(monkeypatch, terrain):
    target = [304191.2, 16.0, 309200.8]
    # The west edge is outside the block below the waypoint center.
    edge_support = [304190, 15, 309200]
    clock = [100.0]
    probes = []
    movements = []
    events = []
    repaired = False
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            nonlocal repaired
            if action == "get_block":
                probes.append(body["pos"])
                if terrain == "unavailable" and len(probes) <= 2:
                    raise httpx.ReadTimeout("missing footing observation")
                return {"name": "air" if body["pos"] == edge_support and not repaired else "stone"}
            if action == "place":
                assert body["pos"] == edge_support
                repaired = terrain == "repaired_edge"
            if action in {"move_to", "control"}:
                movements.append(action)
            return {"ok": True}

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None)
    waypoint = dict(zip(("x", "y", "z"), target), range=0.35)

    def invoke():
        nonlocal repaired
        with pytest.raises(ApiError) as denied:
            bot.call("move_to", waypoint)
        assert denied.value.code == ("mining_footing_unavailable" if terrain == "unavailable" else "unsafe_landing")
        if terrain == "unavailable":
            repaired = True
            return json.dumps(bot.call("move_to", waypoint))
        for _ in range(2):
            clock[0] += 0.75
            with pytest.raises(ApiError, match="采矿落点"):
                bot.call("move_to", {**waypoint, "range": 0.3})
        if terrain == "expiry":
            clock[0] += 0.6
            repaired = True
            return json.dumps(bot.call("move_to", waypoint))
        if terrain in {"repaired_edge", "repair_unconfirmed"}:
            bot.call("place", {"pos": edge_support, "item": "cobblestone"})
        return json.dumps(bot.call("control", {"forward": True, "ms": 420}))

    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", invoke))
    accepted = terrain in {"repaired_edge", "expiry", "unavailable"}
    assert result["ok"] is accepted
    assert movements == (["control" if terrain == "repaired_edge" else "move_to"] if accepted else [])
    assert probes.count(edge_support) == {"unchanged": 1, "repaired_edge": 2, "repair_unconfirmed": 2, "expiry": 2, "unavailable": 3}[terrain]
    reuses = [event for event in events if event["type"] == "guardian.mining_landing_blocked" and event.get("probe_reused")]
    assert len(reuses) == (0 if terrain == "unavailable" else 2)

