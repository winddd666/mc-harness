from tests.guardian._common import *  # noqa: F401,F403
from tests.guardian._common import _goal_bot

@pytest.mark.parametrize(("mob", "observation"), [
    ("zombie", "status_wall"), ("zombie", "scan_wall"), ("skeleton", "scan_wall"),
    ("zombie", "visible"), ("creeper", "check_blast"), ("zombie", "fresh_hit"),
])
def test_bug_009_occluded_snapshots_keep_generation_but_fresh_hurt_still_reacts(monkeypatch, mob, observation):
    target = {"id": 41, "name": mob, "dist": 2.5}
    if observation in {"status_wall", "check_blast", "fresh_hit"}:
        target["blocked_by_wall"] = True
    status = {"pos": [0.5, 64, 0.5], "health": 18, "food": 20, "on_ground": True,
              "ready": True, "alive": True, "dimension": "overworld", "nearest_hostile": target}
    scans = []
    reflexes = []

    class TunnelBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if observation == "fresh_hit" and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": target}]}
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                scans.append(kwargs.get("timeout_s"))
                entity = {**target, "blocked_by_wall": observation in {"scan_wall", "check_blast"}}
                if observation != "scan_wall" or mob == "skeleton":
                    entity["pos"] = [3.0, 64, 0.5]
                return {"entities": [entity]}
            if action == "get_block":
                return {"name": "air"}
            pytest.fail(f"Unexpected action: {action}")

    def react(trigger):
        reflexes.append(trigger["source"])
        return {"ok": True, "mode": "cover_or_flee", "status_after": status, "actions": []}

    guard = GenerationGuard(TunnelBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", react)
    monkeypatch.setattr(guard, "_defend_or_flee", lambda *_a, **_kw: {"ok": True, "mode": "flee"})
    incident = guard.poll()

    if observation in {"status_wall", "scan_wall"}:
        assert incident is None and reflexes == []
    elif observation == "fresh_hit":
        assert incident["requires_replan"] and incident["reasons"] == ["fresh_hurt"]
        assert [action["action"] for action in incident["actions"]] == ["combat_or_flee"]
    else:
        assert incident["requires_replan"]
        assert reflexes == ["pre_hurt_creeper" if mob == "creeper" else "pre_hurt_melee"]
    assert scans == ([] if observation in {"status_wall", "fresh_hit"} else [0.6])


def test_bug_009_pre_hurt_same_target_debounce_cancels_once(monkeypatch):
    """BUG-009: Consecutive pre_hurt_* triggers from the same target within 3s must cancel only once."""
    clock = [500.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    class _PreHurtSameTargetBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": 20.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": False,
                    "nearest_hostile": {"id": 55, "name": "creeper", "pos": [2.5, 64.0, 0.5], "dist": 2.0},
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}]}
            if action == "get_block":
                return {"name": "stone" if (body or {}).get("pos", [0, 64, 0])[1] < 64 else "air"}
            if action == "get_entities":
                return {"entities": [{"id": 55, "name": "creeper", "pos": [2.5, 64.0, 0.5], "dist": 2.0}]}
            if action == "find_blocks":
                return {"blocks": []}
            return {"ok": True}

    guard = GenerationGuard(_PreHurtSameTargetBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    inc1 = guard.poll()
    assert inc1 is not None and "pre_hurt_creeper" in inc1["reasons"]

    # Within 3.0s debounce window for the same creeper id=55 (even with creeper close!), poll() suppresses duplicate pre_hurt
    clock[0] += 1.0
    assert guard.poll() is None
    clock[0] += 1.0
    assert guard.poll() is None

    # After 3.0s cooldown elapses, the same target can trigger again if still present
    clock[0] += 1.5
    inc2 = guard.poll()
    assert inc2 is not None and "pre_hurt_creeper" in inc2["reasons"]

    # End-to-end run_mc_agent verification: same ranged skeleton repeatedly observed within 2.5s cancels only once
    skeleton_visible = {"on": False}

    def handle(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            result = {
                "pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "alive": True, "ready": True,
                "on_ground": True, "in_water": False, "in_lava": False, "blocking": False,
                "nearest_hostile": (
                    {"id": 88, "name": "skeleton", "pos": [8.5, 64.0, 0.5], "dist": 8.0}
                    if skeleton_visible["on"]
                    else None
                ),
            }
        elif action == "events":
            result = {"last_seq": 0, "events": []}
        elif action == "inventory":
            result = {"items": [{"name": "shield", "count": 1}]}
        elif action == "get_entities":
            result = {
                "entities": (
                    [{"id": 88, "name": "skeleton", "pos": [8.5, 64.0, 0.5], "dist": 8.0}]
                    if skeleton_visible["on"]
                    else []
                )
            }
        elif action == "get_block":
            body = json.loads(request.content) if request.content else {}
            y = body.get("pos", [0, 64, 0])[1]
            result = {"name": "stone" if y < 64 else "air"}
        elif action == "find_blocks":
            result = {"blocks": []}
        else:
            result = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": result})

    requests = 0
    events: list[dict] = []

    def chat(messages, tools=None, *, control=None, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            skeleton_visible["on"] = True
            clock[0] += 0.5
            check_control(control)
            pytest.fail("First generation should be cancelled on initial pre_hurt_ranged trigger.")
        # Multiple control checks within 2.5s while skeleton id=88 remains nearby must NOT cancel generation 2
        clock[0] += 0.6
        check_control(control)
        clock[0] += 0.6
        check_control(control)
        return {"role": "assistant", "content": "已按同一目标去抖逻辑保持稳态规划。"}

    with BotSession("cached", transport=httpx.MockTransport(handle)) as bot:
        rep = run_mc_agent(
            "skeleton debounce check",
            bot,
            chat_fn=chat,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
            on_event=events.append,
        )
    assert rep["status"] == "answered"
    assert requests == 2
    assert rep["guardian"]["cancelled_generations"] == 1


def test_bug031_watchdog_thread_and_readonly_status_never_raise_reflex_preempted():
    """BUG-031: background scene-watchdog threads and GET/status/inventory/events calls MUST never raise ReflexPreempted."""
    import threading
    import time

    state = {"creeper_close": False}
    watchdog_errors: list[BaseException] = []
    watchdog_ticks = {"n": 0}
    actor_preempted = {"done": False}
    readonly_ok = {"status": False, "inventory": False, "events": False, "get": False}

    class _WatchdogSafeBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status", method="GET")

        def inventory(self):
            return self.call("inventory", method="GET")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": 20.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "nearest_hostile": (
                        {"id": 99, "name": "creeper", "pos": [2.0, 64.0, 0.5], "dist": 1.5}
                        if state["creeper_close"]
                        else None
                    ),
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": None}
            if action == "get_entities":
                return {"entities": [{"id": 99, "name": "creeper", "pos": [2.0, 64.0, 0.5], "dist": 1.5}]} if state["creeper_close"] else {"entities": []}
            if action == "get_block":
                return {"name": "stone" if (body or {}).get("pos", [0, 64, 0])[1] < 64 else "air"}
            return {"ok": True}

    bot = _WatchdogSafeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    stop_wd = threading.Event()
    wd_ready = threading.Event()

    def _fake_scene_watchdog():
        wd_ready.set()
        while not stop_wd.is_set():
            try:
                bot.status()
                bot.inventory()
                # Non-actor thread even calling generic read paths must NEVER get ReflexPreempted!
                bot.call("find_blocks", {"radius": 4, "types": ["cobblestone"], "limit": 1})
                watchdog_ticks["n"] += 1
            except BaseException as exc:
                watchdog_errors.append(exc)
            stop_wd.wait(0.02)

    wd = threading.Thread(target=_fake_scene_watchdog, name="scene-watchdog-test", daemon=True)
    wd.start()
    wd_ready.wait(timeout=1.0)

    def _actor_tool():
        # Wait long enough for _watcher to fire once creeper approaches (>0.30s)
        state["creeper_close"] = True
        time.sleep(0.45)
        # Actor read-only GET / status / inventory / events must NEVER raise ReflexPreempted
        assert bot.call("status")["ready"] is True
        readonly_ok["status"] = True
        assert "items" in bot.call("inventory")
        readonly_ok["inventory"] = True
        assert "events" in bot.call("events", {"since": 0})
        readonly_ok["events"] = True
        assert bot.call("custom_read", None, method="GET")["ok"] is True
        readonly_ok["get"] = True
        # Actor state-mutating action MUST raise ReflexPreempted and unwind cleanly
        bot.call("dig", {"pos": [1, 64, 0]})
        return "should_not_reach"

    try:
        raw_receipt = guard.run_tool_with_reflex("mc_skill_harvest_logs", _actor_tool)
        receipt = json.loads(raw_receipt)
        actor_preempted["done"] = bool(receipt.get("preempted_by_reflex"))
        assert "last_work_action" not in receipt
        # Let watchdog run a few more ticks post-preemption too
        time.sleep(0.08)
    finally:
        stop_wd.set()
        wd.join(timeout=1.0)

    assert watchdog_errors == []
    assert watchdog_ticks["n"] >= 5
    assert all(readonly_ok.values())
    assert actor_preempted["done"] is True


def test_bug009_busy_hungry_bot_keeps_generation_until_it_can_eat(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {"pos": [0.5, 64, 0.5], "health": 20, "food": 12, "alive": True, "ready": True, "busy": "dig"}
    actions = []

    class BusyBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}]}
            actions.append(action)
            return {"ok": True}

    guard = GenerationGuard(BusyBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll() is None
    assert actions == []

    status["busy"] = None
    clock[0] += 0.5
    incident = guard.poll()
    assert incident["requires_replan"] is False
    assert actions == ["use_item"]


@pytest.mark.parametrize("scene", [
    "already_sealed", "new_wall", "uncertain_place", "hurt", "new_target", "gear", "airborne",
    "missing_fluid_state", "no_longer_occluded",
])
def test_bug_009_only_keeps_generation_when_a_shaft_seal_is_confirmed_unchanged(monkeypatch, scene):
    origin = [185773.5, 27, 198462.5]
    zombie = {"id": 2128415, "name": "zombie", "dist": 2.5}
    target = {**zombie, "pos": [185776, 27, 198462.5]}
    held = {"name": "cobblestone", "count": 7, "slot": 37}
    status = {
        "pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "busy": None, "held_controls": None,
        "in_water": False, "in_lava": False, "submerged": False, "on_fire": False,
        "held": held, "offhand": None, "armor_points": 0, "effects": [], "blocking": False, "sneaking": False,
        "yaw": 0.0, "nearest_hostile": zombie,
    }
    wall = [(185774, 27, 198462), (185774, 28, 198462)]
    blocks = {} if scene in {"new_wall", "uncertain_place"} else dict.fromkeys(wall, "cobblestone")
    placements = []
    scans = 0
    hits = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            nonlocal scans
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [held]}
            if action == "get_entities":
                scans += 1
                if scans == 1:
                    return {"entities": [zombie]}
                if scans == 3:
                    if scene == "hurt":
                        status["health"] = 18
                        hits.append({"seq": 1, "type": "hurt", "source": "mob_attack"})
                    elif scene == "new_target":
                        status["nearest_hostile"] = {"id": 2128416, "name": "zombie", "dist": 1.0}
                    elif scene == "gear":
                        status["held"] = {"name": "iron_axe", "slot": 39}
                    elif scene == "airborne":
                        status["on_ground"] = False
                    elif scene == "missing_fluid_state":
                        del status["submerged"]
                    elif scene == "no_longer_occluded":
                        status["nearest_hostile"] = {**target, "blocked_by_wall": False}
                return {"entities": [target]}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "place":
                cell = tuple(body["pos"])
                placements.append(cell)
                blocks[cell] = body["item"]
                if scene == "uncertain_place":
                    raise ApiError("timeout", "placement acknowledgement missing")
            return {"ok": True}

    guard = GenerationGuard(ShaftBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "carved_shaft"})
    incident = guard.poll()

    outcome = incident["actions"][0]["result"]
    seal, = outcome["actions"]
    assert seal["action"] == "seal_shaft" and seal["ok"]
    assert placements == (wall if scene in {"new_wall", "uncertain_place"} else [])
    assert incident["requires_replan"] is (scene != "already_sealed")
    assert seal["placed"] == (2 if scene == "new_wall" else 0)
    assert seal["placement_attempts"] == len(placements) and seal["direction"] == "east"
    assert seal["seal_cells"] == [list(cell) for cell in wall]
    if not placements:
        assert seal["reason"] == "already_sealed"
    if scene == "already_sealed":
        assert "本轮未放置方块" in incident["note"]

