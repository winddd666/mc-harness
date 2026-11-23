from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize("mob, distance", [("creeper", 4.1), ("zombie", 2.5)])
@pytest.mark.parametrize("visibility", [
    "occluded", "exposed", "another_exposed", "unknown", "occluded_then_hurt",
    "rock_wall", "wall_removed", "ray_timeout",
])
def test_mining_watcher_waits_only_for_confirmed_occluded_hostiles(monkeypatch, mob, distance, visibility):
    import threading

    inspected = threading.Event()
    stopped = threading.Event()
    ray_checked = threading.Event()
    wall_present = [True]
    hostile = {"id": 42, "name": mob, "dist": distance, "pos": [0.5, 58, 0.5 + distance]}
    status = {
        "pos": [0.5, 58, 0.5], "health": 20, "on_ground": True,
        "ready": True, "alive": True, "blocking": True, "nearest_hostile": hostile,
    }

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_entities":
                inspected.set()
                if visibility == "unknown":
                    raise httpx.ReadTimeout("entity visibility unavailable")
                entities = [{**hostile, "blocked_by_wall": visibility in {
                    "occluded", "another_exposed", "occluded_then_hurt",
                }}]
                if visibility == "another_exposed":
                    entities.append({**hostile, "id": 43, "dist": distance + 0.3, "blocked_by_wall": False})
                return {"entities": entities}
            if action == "get_block":
                if visibility == "ray_timeout":
                    raise httpx.ReadTimeout("block lookup timed out")
                name = "tuff" if visibility in {"rock_wall", "wall_removed"} and wall_present[0] else "air"
                ray_checked.set()
                return {"name": name}
            if action == "dig":
                assert inspected.wait(1.5)
                if visibility == "occluded_then_hurt":
                    status["health"] = 16
                if visibility in {"rock_wall", "wall_removed"}:
                    assert ray_checked.wait(1.5)
                if visibility == "wall_removed":
                    wall_present[0] = False
                if visibility not in {"occluded", "rock_wall"}:
                    assert stopped.wait(1.5)
                return {"ok": True}
            if action == "stop":
                stopped.set()
            if action == "events":
                return {"events": []}
            return {"ok": True}

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    expected_mode = "creeper_evade" if mob == "creeper" else "cover_or_flee"
    seen_sources = []

    def react(_env, trigger):
        seen_sources.append(trigger["source"])
        return {"mode": expected_mode, "handled": True, "status_after": status}

    monkeypatch.setattr(guard.combat_slot, "handle", react)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", lambda: json.dumps(
        bot.call("dig", {"pos": [1, 58, 0]}),
    )))

    if visibility in {"occluded", "rock_wall"}:
        assert result == {"ok": True} and not stopped.is_set()
        assert seen_sources == []
    else:
        assert result["reflex_mode"] == expected_mode and stopped.is_set()
        if visibility == "occluded_then_hurt" and mob == "zombie":
            assert seen_sources == ["tool_interrupted_hurt"]


@pytest.mark.parametrize("site", [
    "exposed", "walled", "new_life", "moved_away", "other_block", "expired", "other_mob", "probe_timeout", "not_released",
])
def test_mine_blocks_rechecks_active_melee_at_a_recently_interrupted_dig(monkeypatch, site):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    target = [144444, 71, 82436]
    threat_id = 1980720
    contact = [144447.7, 70, 82436.5]
    status = {
        "pos": contact, "health": 18, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    digs = []

    class MineBot:
        def status(self):
            return dict(status)

        def inventory(self):
            return self.call("inventory", method="GET")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                items = [{"name": "wooden_pickaxe", "count": 1, "slot": 42}]
                if digs:
                    items.append({"name": "cobblestone", "count": 1, "slot": 9})
                return {"items": items}
            if action == "find_blocks":
                return {"blocks": [{"name": "stone", "pos": target, "dist": 11.8}]}
            if action == "get_entities":
                if site == "probe_timeout":
                    raise httpx.ReadTimeout("sighting unavailable")
                enemy_pos = [144449.5, 70, 82436.5] if site != "moved_away" else [144451.5, 70, 82447.5]
                enemy_id = threat_id + 1 if site == "other_mob" else threat_id
                return {"entities": [{"id": enemy_id, "name": "spider", "pos": enemy_pos}]}
            if action == "get_block":
                return {"name": "stone" if site == "walled" else "air"}
            if action == "dig":
                digs.append(body["pos"])
                return {"dug": "stone", "gained": {"cobblestone": 1}}
            return {"ok": True}

    def retreat(_trigger):
        status.update(pos=[144449.6, 71, 82433.5])
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    bot = MineBot()
    skills = MacroSkills(bot)
    events = EventSink(lambda _: None)
    guard = GenerationGuard(bot, events, checkpoint=lambda: None, skills=skills)
    monkeypatch.setattr(guard, "_run_subcortical_combat", retreat)
    guard.handle_preemption(
        {"source": "pre_hurt_melee", "primary_type": "spider", "primary_entity_id": threat_id,
         "distance": 2.4, "action_released": site != "not_released"},
        interrupted_name="mc_skill_mine_blocks", last_work_action={"endpoint": "dig", "target": list(target)},
    )
    status["pos"] = [144455.5, 71, 82433.5]
    clock[0] += 61 if site == "expired" else 52.75
    if site == "new_life":
        status["last_death"] = {"t": 2}
    if site == "other_block":
        target[0] += 1

    result = json.loads(guard.run_tool_with_reflex(
        "mc_skill_mine_blocks", lambda: json.dumps(skills.mine_blocks(["stone", "cobblestone"], count=1, radius=16)),
    ))

    denials = [event for event in events.events if event["type"] == "guardian.mining_melee_reentry_blocked"]
    if site == "exposed":
        assert digs == []
        assert result["requires_replan"] and result["partial_error"]["code"] == "mining_melee_reentry"
        assert len(denials) == 1 and denials[0]["entity_id"] == threat_id and denials[0]["target"] == target
    else:
        assert digs == [target] and result["gained"] == {"cobblestone": 1}
        assert denials == []


@pytest.mark.parametrize(("request_mode", "details", "timed_out_without_contact"), [
    ("auto", {"end_reason": "timeout", "hits": 0, "swings": 0, "hits_connected": 0, "killed": False}, True),
    ("rush", {"end_reason": "timeout", "hits": 2, "swings": 2, "hits_connected": 0, "killed": False}, True),
    ("melee", {"end_reason": "timeout", "hits": 0, "killed": False}, True),
    ("rush", {"end_reason": "timeout", "hits": 2, "swings": 2, "hits_connected": 1, "killed": False}, False),
    ("melee", {"end_reason": "target_dead", "hits": 1, "killed": True}, False),
])
def test_combat_timeout_without_contact_reaches_repeat_guard(monkeypatch, request_mode, details, timed_out_without_contact):
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.harness.scheduler import _is_action_failure
    from mc_agent.harness.tool_registry import Tool
    from mc_agent.skills import combat_v1

    dispatched = []
    cleanup = []

    def run_tactics(_adapter, *, target_name, max_time_s):
        dispatched.append((target_name, max_time_s))
        return {**details, "target_id": 1133882, "path_retries": 33}

    monkeypatch.setattr(combat_v1, "action_rush", run_tactics)
    monkeypatch.setattr(combat_v1, "action_melee", run_tactics)

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "get_entities":
            data = {"entities": [{"id": 1133882, "name": "blaze", "pos": [20.6, 64, 0.5], "dist": 20.1}]}
        elif endpoint == "status":
            data = {"dimension": "overworld"}
        elif endpoint == "inventory":
            data = {"items": [{"name": "shield", "count": 1}]}
        elif endpoint in {"stop", "posture"}:
            cleanup.append(endpoint)
            data = {}
        else:
            pytest.fail(f"Unexpected combat endpoint: {endpoint}")
        return httpx.Response(200, json={"ok": True, "result": data})

    with BotSession("cached", bot_username="Bot", transport=httpx.MockTransport(handle)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0.5, 64, 0.5])
        tool = Tool(
            name="mc_skill_combat", description="", parameters={}, concurrency="exclusive", handler=toolset.mc_skill_combat,
        )
        guarded = _RepeatFailureGuard(toolset).wrap_tool(tool)
        replies = [guarded.handler(mode=request_mode, target_name="blaze", max_time_s=20) for _ in range(3)]

    first, *repeats = map(json.loads, replies)
    assert _is_action_failure(replies[0]) is timed_out_without_contact
    assert first["ok"] is not timed_out_without_contact
    expected_result = {**details, "target_id": 1133882, "path_retries": 33}
    if timed_out_without_contact:
        expected_result["hits"] = 0
        assert first["requires_replan"] is True and first["error"]["code"] == "time_budget"
        assert [reply["error"]["code"] for reply in repeats] == ["repeated_identical_failure_blocked"] * 2
        assert dispatched == [("blaze", 20.0)]
    else:
        assert all(reply["ok"] for reply in repeats)
        assert dispatched == [("blaze", 20.0)] * 3
    assert first["result"] == expected_result
    assert cleanup == ["stop", "posture"] * len(dispatched)


def test_combat_status_keeps_retry_and_http_phases_inside_remaining_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.bot.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status_attempts = []
    events = []

    def respond(request):
        if request.url.path.endswith("/status") and clock[0] >= 1006.6 and len(status_attempts) < 2:
            timeout = request.extensions["timeout"]
            status_attempts.append(timeout)
            clock[0] += sum(timeout.values())
            if len(status_attempts) == 1:
                raise httpx.RemoteProtocolError("closed connection")
            raise httpx.ReadTimeout("slow retry")
        return httpx.Response(200, json={"ok": True, "result": {}})

    class LateStatus:
        def on_encounter(self, env, _trigger):
            clock[0] += 6.6
            env.bot.status()

    with BotSession("cached", transport=httpx.MockTransport(respond)) as bot:
        guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None)
        guard.combat_slot.register(LateStatus())
        outcome = guard._run_subcortical_combat({"source": "pre_hurt_melee"})

    assert outcome["reason"] == "time_budget"
    event, = [event for event in events if event["type"] == "guardian.combat_budget_exceeded"]
    assert event["endpoint"] == "status"
    assert len(status_attempts) == 2
    assert clock[0] <= 1007.500001


@pytest.mark.parametrize("cause", ["wall_error", "wall_result", "wall_watch", "arrow", "respawned"])
def test_staircase_preempts_for_fresh_suffocation_without_a_nearby_mob(monkeypatch, cause):
    import threading

    actor = threading.current_thread()
    watcher_read = threading.Event()
    stopped = threading.Event()
    status = {
        "pos": [6073.5, 52, -218.5], "dimension": "overworld", "last_death": {"t": 1},
        "alive": True, "ready": True, "on_ground": True, "health": 12, "nearest_hostile": None,
    }
    damage = []
    continued = []
    unstuck = []

    class StairBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                observed = dict(status)
                if threading.current_thread() is not actor:
                    watcher_read.set()
                return observed
            if action == "events":
                since = kwargs["params"]["since"]
                rows = [row for row in damage if row["seq"] > since]
                if cause == "respawned" and rows:
                    status.update(last_death={"t": 2}, health=20)
                return {"events": rows, "last_seq": len(damage)}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 52 else "air"}
            if action == "stop":
                stopped.set()
                return {"ok": True}
            if action == "dig":
                if cause == "wall_watch":
                    assert watcher_read.wait(2.0)
                damage.append({"seq": 1, "type": "hurt", "source": "arrow" if cause == "arrow" else "in_wall"})
                status["health"] = 11
                if cause == "wall_watch":
                    assert stopped.wait(1.5)
                if cause in {"wall_watch", "wall_result"}:
                    return {"interrupted_by": "hurt"}
                raise ApiError("interrupted", "stopped because of hurt")
            continued.append(action)
            return {"ok": True}

    bot = StairBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_unstick_in_wall", lambda before: unstuck.append(before["last_death"]) or {"ok": True})

    def staircase():
        dig = {"pos": [6073, 53, -220]}
        if cause == "wall_watch":
            dig["stop_on"] = ["death"]
        try:
            bot.call("dig", dig)
        except ApiError:
            continued.append("ignored")
        return json.dumps({"ok": True})

    result = json.loads(guard.run_tool_with_reflex("mc_skill_staircase", staircase))

    if cause == "arrow":
        assert result == {"ok": True}
        assert continued == ["ignored"] and unstuck == []
    else:
        assert result["preempted_by_reflex"]
        assert continued == []
        assert result["reflex_mode"] == ("life_unavailable" if cause == "respawned" else "unstick_in_wall")
        assert unstuck == ([] if cause == "respawned" else [{"t": 1}])

