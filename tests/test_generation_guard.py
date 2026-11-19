"""Generation-time damage/water response, using real harness cancellation boundaries."""
import json

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.control import check_control
from mc_agent.harness.events import EventSink
from mc_agent.harness.guardian import GenerationGuard
from mc_agent.skills.library import MacroSkills
from mc_agent.live.state import ReplayIncrementer


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
            rows = [{"seq": 1, "type": "hurt", "health": 16, "source": "arrow", "source_entity": {"id": 12}}] if state["hurt"] and since < 1 else []
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
            result = {"health": 20, "food": 20, "alive": True, "ready": True, "pos": [0.5, 64, 0.5],
                      "in_water": mode == "water", "submerged": mode == "water", "on_ground": mode != "water",
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


class BunkerBot:
    def __init__(self, open_side=False, roof=True):
        self.y = 64
        self.open_side = open_side
        self.roof = roof
        self.calls = []
        self.blocks = {}
        self.x = 0.5
        self.z = 0.5

    def status(self):
        return {"pos": [self.x, self.y, self.z], "health": 18, "on_ground": True}

    def inventory(self):
        return {"items": [{"name": "stone_pickaxe", "count": 1}] + ([{"name": "dirt", "count": 1}] if self.roof else [])}

    def call(self, action, body):
        self.calls.append((action, body))
        if action == "get_block":
            return {"name": self.blocks.get(tuple(body["pos"]), "air" if self.open_side and body["pos"] == [1, 62, 0] else "stone")}
        if action == "dig":
            self.y -= 1
            self.blocks[tuple(body["pos"])] = "air"
            return {"dug": "stone"}
        if action == "place":
            self.blocks[tuple(body["pos"])] = body["item"]
            return {"placed": "dirt"}
        if action == "control":
            self.x = self.z = 0.5
            return {"pos": [self.x, self.y, self.z]}
        if action == "equip":
            return {}
        raise AssertionError(action)


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
                        "in_water": True, "submerged": True, "on_ground": False, "jumping": False}
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
                        "in_water": True, "submerged": False, "on_ground": True, "jumping": False, "oxygen": 280}
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


def _goal_bot(inventory_items):
    class _Bot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, 64.0, 0.5], "health": 20.0, "food": 20.0, "alive": True,
                        "ready": True, "on_ground": True, "in_water": False}
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": inventory_items, "offhand": {}}
            return {"ok": True}

    return _Bot()


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


def test_arrow_hit_digs_cover_pocket_instead_of_sprinting_away(monkeypatch):
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
                return {"pos": [0.5, 23.0, 0.5], "health": 16.0, "food": 20.0, "alive": True,
                        "ready": True, "in_water": False, "on_ground": True,
                        "nearest_hostile": {"name": "skeleton", "dist": 9.0}}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "health": 16.0, "source": "arrow"}],
                        "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 40}], "offhand": {}}
            return {"ok": True}

    guard = GenerationGuard(_ShotBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.skills = _ArrowSkills()
    guard.last_poll = float("-inf")
    incident = guard.poll()

    assert incident is not None
    assert [a["action"] for a in incident["actions"]] == ["dig_cover"]
    assert incident["actions"][0]["ok"] is True


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


def test_subcortical_combat_timeout_sends_stop_and_preemption_survives_without_crashing(monkeypatch):
    """Reproduces glm_glm_3_village / hy4_hy_8_nether: ReadTimeout in subcortical_combat must /stop & not crash run."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    # 1) Bounded attack timeout on SubcorticalInstinctController
    seen_attack_payloads: list[dict] = []

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
                return {"hits": 2, "killed": True}
            return {"ok": True}

    ctrl = SubcorticalInstinctController()
    out = ctrl.on_encounter(CombatEnv(_AttackBot()), {"source": "pre_hurt_melee", "primary_entity_id": 5, "primary_type": "zombie"})
    assert out["mode"] == "counter_killed"
    assert seen_attack_payloads and 1000 <= seen_attack_payloads[0]["timeout"] <= 3000

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


def test_creeper_l_corner_not_carved_shaft_and_close_creeper_latches_shield(monkeypatch):
    """Reproduces hy4_hy_8_nether & glm_glm_3_village: L-corner is open_cave; close creeper post-flee latches shield."""
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, SubcorticalInstinctController

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    postures: list[dict] = []

    class _CornerAndCloseCreeperBot:
        def __init__(self) -> None:
            self.pos = [0.5, 64.0, 0.5]
            self.blocks = {
                (0, 64, -1): "stone", (0, 65, -1): "stone",  # North wall
                (1, 64, 0): "stone", (1, 65, 0): "stone",    # East wall (South & West open = L-corner!)
                (0, 66, 0): "oak_leaves",                     # Overhead leaf
            }

        def status(self):
            return {
                "pos": list(self.pos), "health": 20.0, "food": 20, "alive": True, "ready": True,
                "on_ground": True, "in_water": False, "yaw": 0.0,
                "nearest_hostile": {"id": 55, "name": "creeper", "pos": [self.pos[0], 64.0, self.pos[2] + 2.2], "dist": 2.2},
            }

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                p = tuple(payload["pos"])
                return {"name": self.blocks.get(p, "stone" if p[1] < 64 else "air")}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_entities":
                # Creeper chased alongside us and is STILL 2.2m away!
                return {"entities": [{"id": 55, "name": "creeper", "pos": [self.pos[0], 64.0, self.pos[2] + 2.2], "dist": 2.2}]}
            if action == "move_to":
                self.pos[2] -= 6.0  # Moved >= 1.5m so flee() ok=True, yet creeper still at 2.2m!
                return {"pos": list(self.pos)}
            if action == "posture":
                postures.append(payload)
            return {"ok": True}

    bot = _CornerAndCloseCreeperBot()
    env = CombatEnv(bot)
    snap = env.arena_snapshot([0.5, 64.0, 0.5])
    assert snap["enclosure"] != "carved_shaft"

    out = CombatControllerSlot(SubcorticalInstinctController()).handle(
        env, {"source": "pre_hurt_creeper", "primary_entity_id": 55, "primary_type": "creeper", "distance": 2.6}
    )
    assert out["mode"] == "creeper_evade"
    assert out["latched_guard"] is True
    assert postures and postures[-1].get("shield") is True

    # CombatEnv.flee detects respawn teleport (>64m) as ok=False, mode='respawned'
    class _DeadFleeBot:
        def __init__(self):
            self.dead = False

        def status(self):
            return {"pos": [5000.0, 70.0, 5000.0] if self.dead else [0.5, 64.0, 0.5],
                    "health": 20.0, "alive": True, "ready": True, "yaw": 0.0}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                self.dead = True
            return {"ok": True}

    flee_resp = CombatEnv(_DeadFleeBot()).flee([0.5, 64.0, 3.0])
    assert flee_resp["ok"] is False and flee_resp["mode"] == "respawned"


def test_night_surface_bunker_after_failed_flee_boundary_eat_and_no_dead_eat(monkeypatch):
    """Covers hy4_hy_2_bare night flee->bunker, boundary_eat NameError fix, and post-death not_ready eat guard."""
    clock = [300.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    bunker_calls = []
    eat_calls = []
    state = {"hp": 13.0, "food": 20, "blocking": False, "ready": True, "alive": True, "hurt_seq": 1}

    class _NightSkills:
        mining_dfs = None

        def dig_safety_bunker(self, urgent=False):
            bunker_calls.append(urgent)
            return {"ok": True, "inside_shaft": True, "roof_placed": True}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee", "moved_dist": 12.0}

        def dig_cover_pocket(self):
            return {"ok": True, "mode": "cover_pocket", "sealed": [True, True]}

    class _NightBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 70.0, 0.5], "health": state["hp"], "food": state["food"],
                    "alive": state["alive"], "ready": state["ready"], "on_ground": True,
                    "in_water": False, "in_lava": False, "is_day": False, "blocking": state["blocking"],
                    "nearest_hostile": {"id": 11, "name": "zombie", "dist": 2.0} if state["hp"] <= 13 else None,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                seq = state["hurt_seq"]
                rows = [{"seq": seq, "type": "hurt", "health": state["hp"], "source": "mob_attack"}] if seq > since else []
                return {"events": rows, "last_seq": seq}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 16}, {"name": "bread", "count": 2}]}
            if action == "use_item":
                eat_calls.append(dict(body or {}))
                return {"ok": True}
            return {"ok": True}

    guard = GenerationGuard(_NightBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_NightSkills())
    # 1) First melee hurt on surface at night -> flees
    inc1 = guard.poll()
    assert inc1 is not None and [a["action"] for a in inc1["actions"]] == ["combat_or_flee"]

    # 2) Second melee hurt shortly after with no sword -> bunkers instead of infinite night surface flee
    clock[0] += 5.0
    state["hurt_seq"] = 2
    inc2 = guard.poll()
    assert inc2 is not None and "emergency_bunker" in [a["action"] for a in inc2["actions"]]
    assert bunker_calls == [True]

    # 3) stabilize_after_tools with blocking=True and food=10 eats cleanly without NameError('st')
    state["hp"] = 18.0
    state["food"] = 10
    state["blocking"] = True
    eat_calls.clear()
    note = guard.stabilize_after_tools(["ok"])
    assert note is not None and "顺手进食 bread" in note
    assert eat_calls == [{"item": "bread"}]

    # 4) If bot died / ready=False after combat, poll() must NOT call use_item(food)
    state["ready"] = False
    state["alive"] = False
    eat_calls.clear()
    clock[0] += 5.0
    inc_dead = guard.poll()
    assert inc_dead is not None
    assert eat_calls == []

    # 5) Already blocking arrow hurt escalates to dig_cover instead of standing still calling raise_shield
    state["ready"] = True
    state["alive"] = True
    state["hp"] = 15.0
    state["food"] = 20
    state["blocking"] = True

    class _ShieldAlreadyUpBot(_NightBot):
        def call(self, action, body=None, **kwargs):
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 10, "type": "hurt", "health": 15.0, "source": "arrow"}] if since < 10 else []
                return {"events": rows, "last_seq": 10}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}]}
            return super().call(action, body, **kwargs)

    guard2 = GenerationGuard(_ShieldAlreadyUpBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_NightSkills())
    inc_shield = guard2.poll()
    assert inc_shield is not None
    assert [a["action"] for a in inc_shield["actions"]] == ["dig_cover"]


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

    # 2) Even if ready=True arrives, if use_item returns ApiError('not_ready') or 'wrong_server', do NOT count as failure
    clock[0] += 0.5
    state["events"] = []
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


def test_nether_deep_lava_preemption_prioritizes_escape_over_nearby_mob(monkeypatch):
    """Reproduces glm_glm_2_nether_bastion: in_lava overrides nearby zombified_piglin during preemption & deep lava swims up first."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    state = {"in_lava": True, "pos": [6.4, 27.6, -815.0], "attacked": False}
    action_log: list[tuple[str, dict]] = []

    class _DeepLavaNetherBot:
        _last_event_seq = 0

        def status(self):
            return {
                "pos": list(state["pos"]),
                "health": 16.8,
                "food": 20,
                "alive": True,
                "ready": True,
                "in_water": False,
                "in_lava": state["in_lava"],
                "dimension": "minecraft:the_nether",
                "yaw": 0.0,
                "nearest_hostile": {"id": 88302, "name": "zombified_piglin", "dist": 18.9, "pos": [25.0, 31.0, -815.0]},
            }

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            action_log.append((action, payload))
            if action == "status":
                return self.status()
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "netherrack", "count": 32}]}
            if action == "get_block":
                p = payload["pos"]
                # Deep lava below y=30; solid bank at z=-813, y=30
                if p == [6, 30, -813]:
                    return {"name": "netherrack"}
                if p[1] in (27, 28, 29, 30) and p[2] <= -814:
                    return {"name": "lava"}
                if p[1] < 27:
                    return {"name": "netherrack"}
                return {"name": "air"}
            if action == "control" and payload.get("jump"):
                if state["pos"][1] < 30.2:
                    state["pos"] = [6.4, 30.5, -815.0]
                elif payload.get("forward"):
                    state["in_lava"] = False
                    state["pos"] = [6.5, 31.0, -812.5]
            if action in ("attack", "move_to"):
                state["attacked"] = True
            return {"ok": True}

    guard = GenerationGuard(_DeepLavaNetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    # 1) Even when preemption trigger names a nearby mob (`zombified_piglin`), handle_preemption escapes lava FIRST!
    receipt = guard.handle_preemption(
        {"source": "boundary_threat", "primary_entity_id": 88302, "primary_type": "zombified_piglin", "distance": 18.9},
        interrupted_name="mc_skill_mine_dfs",
    )
    assert receipt["reflex_mode"] == "lava_escape"
    assert state["in_lava"] is False
    assert state["attacked"] is False
    assert any(act == "control" and pl.get("jump") for act, pl in action_log)

    # 2) Direct SubcorticalInstinctController call while in_lava also prioritizes lava_escape
    state["in_lava"] = True
    state["pos"] = [6.4, 27.6, -815.0]
    out = SubcorticalInstinctController().on_encounter(
        CombatEnv(_DeepLavaNetherBot()),
        {"source": "boundary_threat", "primary_entity_id": 88302, "primary_type": "zombified_piglin", "distance": 18.9},
    )
    assert out["mode"] == "lava_escape"
    assert out["handled"] is True
    assert state["in_lava"] is False


def test_non_combat_fire_fall_hurt_skips_shield_spam_and_water_trident_equips_shield(monkeypatch):
    """Covers r12 ds_ds_3_village on_fire/fall shield-spam & hy4_hy_9_deep_water trident offhand shield block."""
    clock = [800.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    # 1) Non-combat on_fire / fall hurt without close hostile must NOT raise shield or repeatedly cancel LLM
    calls: list[tuple[str, dict]] = []
    ev_state = {"seq": 1, "source": "on_fire", "hp": 17.0, "food": 20}

    class _EnvHurtBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            calls.append((action, payload))
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": ev_state["hp"],
                    "food": ev_state["food"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": False,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = (
                    [{"seq": ev_state["seq"], "type": "hurt", "health": ev_state["hp"], "source": ev_state["source"]}]
                    if ev_state["seq"] > since
                    else []
                )
                return {"events": rows, "last_seq": ev_state["seq"]}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}]}
            return {"ok": True}

    guard = GenerationGuard(_EnvHurtBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll() is None  # on_fire tick with hp=17, no water_bucket -> zero fake shield/cancel!
    clock[0] += 5.0
    ev_state["seq"] = 2
    ev_state["source"] = "fall"
    ev_state["hp"] = 11.0
    assert guard.poll() is None  # mild fall with hp=11, no hostile -> zero fake raise_shield!
    assert not any(a in ("posture", "look") for a, _ in calls)

    # Critical fall (hp <= 6.0) still notifies once for replan, without raising shield
    clock[0] += 5.0
    ev_state["seq"] = 3
    ev_state["hp"] = 4.0
    inc_crit = guard.poll()
    assert inc_crit is not None and "fresh_hurt" in inc_crit["reasons"]
    assert not any(a["action"] == "raise_shield" for a in inc_crit["actions"])

    # 2) Open-ocean trident hurt when escape_water fails equips shield from bag & blocks while jumping!
    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: {"ok": False, "in_water": True, "pos_after": [0.5, 62.5, 0.5]},
    )
    water_calls: list[tuple[str, dict]] = []

    class _DeepWaterTridentBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            water_calls.append((action, payload))
            if action == "status":
                return {
                    "pos": [0.5, 62.5, 0.5],
                    "health": 10.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": True,
                    "submerged": False,
                    "on_ground": False,
                    "blocking": False,
                    "nearest_hostile": {"id": 44, "name": "drowned", "dist": 11.0},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 1, "type": "hurt", "health": 10.0, "source": "trident", "source_entity": {"id": 44}}] if since < 1 else []
                return {"events": rows, "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": None}
            return {"ok": True}

    guard_w = GenerationGuard(_DeepWaterTridentBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_DeepWaterTridentBot()))
    inc_w = guard_w.poll()
    assert inc_w is not None
    act_names = [a["action"] for a in inc_w["actions"]]
    assert "escape_water" in act_names
    assert "equip_shield" in act_names
    assert "raise_shield" in act_names
    assert any(act == "equip" and pl == {"item": "shield", "slot": "off-hand"} for act, pl in water_calls)
    assert any(act == "posture" and pl.get("shield") is True and pl.get("jump") is True for act, pl in water_calls)

    # 3) If escape_water fails with not_connected/bot-dead while submerged, NEVER fire follow-up equip/look/posture
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: (_ for _ in ()).throw(ApiError("not_connected", "bot not connected")),
    )
    dead_water_calls: list[str] = []

    class _DisconnectedSubmergedBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            dead_water_calls.append(action)
            if action == "status":
                return {
                    "pos": [0.5, 58.0, 0.5],
                    "health": 10.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": True,
                    "submerged": True,
                    "on_ground": False,
                    "blocking": False,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 1, "type": "hurt", "health": 10.0, "source": "trident", "source_entity": {"id": 44}}] if since < 1 else []
                return {"events": rows, "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": None}
            return {"ok": True}

    guard_dc = GenerationGuard(_DisconnectedSubmergedBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_DisconnectedSubmergedBot()))
    inc_dc = guard_dc.poll()
    assert inc_dc is not None
    assert [a["action"] for a in inc_dc["actions"]] == ["escape_water"]
    assert "equip" not in dead_water_calls and "posture" not in dead_water_calls

    # 4) escape_lava wide shore detection (4.5m away) & death-respawn teleport (>64m, ready=False) -> ok=False, respawned=True
    look_yaws: list[float] = []

    class _WideLavaPoolBot:
        def __init__(self, *, respawn_teleport: bool = False):
            self.respawn = respawn_teleport
            self.pos = [-23.5, 30.0, 778.0]
            self.in_lava = True
            self.ready = True

        def status(self):
            return {
                "pos": list(self.pos),
                "health": 10.0,
                "alive": True,
                "ready": self.ready,
                "in_lava": self.in_lava,
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                p = pl["pos"]
                # Shore only exists 4.5m to the East (x=-19), NOT along initial yaw=0.0!
                if p[1] == 30 and abs(p[0] - (-19)) <= 1 and abs(p[2] - 778) <= 2:
                    return {"name": "netherrack"}
                if p[1] == 30:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "look":
                look_yaws.append(round(float(pl["yaw"]), 2))
            if action == "control" and self.respawn:
                self.pos = [0.5, 64.1, 800.5]
                self.in_lava = False
                self.ready = False
            return {"ok": True}

    out_wide = CombatEnv(_WideLavaPoolBot(respawn_teleport=False)).escape_lava()
    assert look_yaws and abs(look_yaws[0]) > 0.5  # Turned away from initial yaw=0.0 toward East bank!
    out_tp = CombatEnv(_WideLavaPoolBot(respawn_teleport=True)).escape_lava()
    assert out_tp["ok"] is False and out_tp.get("respawned") is True





