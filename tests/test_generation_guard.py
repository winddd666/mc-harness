"""Generation-time damage/water response, using real harness cancellation boundaries."""
import json
import math

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.errors import ApiError
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
    # Shield is equipped & raised BEFORE escape_water attempts retreat; once escape_water fails with not_connected,
    # NO follow-up escape_to_shore or post-failure status/equip/posture may fire.
    assert [a["action"] for a in inc_dc["actions"]] == ["equip_shield", "face_attacker", "raise_shield", "escape_water"]
    assert dead_water_calls.count("equip") == 1 and dead_water_calls.count("posture") == 1
    assert "find_blocks" not in dead_water_calls and "move_to" not in dead_water_calls

    # If equip itself fails with not_connected, zero follow-up look/posture/escape_water/escape_to_shore may fire.
    class _EquipFailSubmergedBot(_DisconnectedSubmergedBot):
        def call(self, action, body=None, **kwargs):
            if action == "equip":
                dead_water_calls.append(action)
                raise ApiError("not_connected", "bot not connected")
            return super().call(action, body, **kwargs)

    dead_water_calls.clear()
    guard_eq_dc = GenerationGuard(_EquipFailSubmergedBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_EquipFailSubmergedBot()))
    inc_eq_dc = guard_eq_dc.poll()
    assert inc_eq_dc is not None
    assert [a["action"] for a in inc_eq_dc["actions"]] == ["equip_shield"]
    assert not any(a in dead_water_calls for a in ("look", "posture", "find_blocks", "move_to"))

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


def test_cave_combat_ignores_distant_wall_hostiles_and_finishes_shield_rush():
    """Reproduces r15 cave_combat: distant mobs >8.5m outside walls must not disable sword counter_kill or multi-hit shield_rush_ranged."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    strikes = {"n": 0}

    class _CaveArenaBot:
        def __init__(self, *, solo_skeleton: bool = False):
            self.solo_skeleton = solo_skeleton

        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5],
                "health": 18.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "yaw": 0.0,
            }

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]}
            if action == "get_entities":
                if self.solo_skeleton:
                    return {
                        "entities": [
                            {"id": 20, "name": "skeleton", "pos": [0.5, 64.0, 5.0], "dist": 4.5},
                            # Ambient outdoor mobs > 8.5m behind cobblestone walls
                            {"id": 31, "name": "zombie", "pos": [12.5, 64.0, 0.5], "dist": 12.0},
                            {"id": 32, "name": "zombie", "pos": [-13.5, 64.0, 0.5], "dist": 14.0},
                        ]
                    }
                return {
                    "entities": [
                        {"id": 10, "name": "zombie", "pos": [2.5, 64.0, 0.5], "dist": 2.0},
                        {"id": 20, "name": "skeleton", "pos": [0.5, 64.0, 5.0], "dist": 4.5},
                        {"id": 31, "name": "zombie", "pos": [12.5, 64.0, 0.5], "dist": 12.0},
                        {"id": 32, "name": "spider", "pos": [-13.5, 64.0, 0.5], "dist": 14.0},
                    ]
                }
            if action == "get_block":
                p = (body or {}).get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 or abs(p[0]) >= 5 or abs(p[2]) >= 5 else "air"}
            if action == "attack":
                strikes["n"] += 1
                return {"hits": 1, "killed": strikes["n"] >= 2}
            return {"ok": True}

    ctrl = SubcorticalInstinctController()
    # 1) Zombie + skeleton inside arena + 2 mobs outside at 12-14m -> counter-kills zombie instead of circle-fleeing
    out_z = ctrl.on_encounter(
        CombatEnv(_CaveArenaBot(solo_skeleton=False)),
        {"source": "pre_hurt_melee", "primary_entity_id": 10, "primary_type": "zombie", "distance": 2.0},
    )
    assert out_z["mode"] == "counter_killed"

    # 2) Solo skeleton inside + 2 outdoor mobs -> shield_rush_ranged finishes skeleton in 2 strikes
    strikes["n"] = 0
    out_s = ctrl.on_encounter(
        CombatEnv(_CaveArenaBot(solo_skeleton=True)),
        {"source": "pre_hurt_ranged", "primary_entity_id": 20, "primary_type": "skeleton", "distance": 4.5},
    )
    assert out_s["mode"] == "shield_rush_ranged"
    assert strikes["n"] == 2
    assert any(a["action"] == "shield_rush_attack" and a.get("killed") is True for a in out_s["actions"])


@pytest.mark.parametrize("scenario", ["cornered", "escaped", "second_hostile", "out_of_reach"])
def test_cornered_unshielded_bot_strikes_once_only_against_a_close_lone_melee_mob(monkeypatch, scenario):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    distance = 2.5 if scenario == "out_of_reach" else 0.6
    zombie = {"id": 11, "name": "zombie", "dist": distance, "pos": [0.5 + distance, 64.0, 0.5]}
    calls = []

    class CaveBot:
        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5], "health": 2.7, "food": 20, "alive": True, "ready": True,
                "on_ground": True, "nearest_hostile": zombie,
            }

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}], "held": {"name": "dirt"}}
            if action == "get_entities":
                mobs = [zombie]
                if scenario == "second_hostile":
                    mobs.append({"id": 12, "name": "zombie", "dist": 1.7, "pos": [2.2, 64, 0.5]})
                return {"entities": mobs}
            if action == "get_block":
                return {"name": "air"}
            if action in {"equip", "attack"}:
                calls.append((action, body))
            return {"hits": 1} if action == "attack" else {"ok": True}

    def flee(*args, **kwargs):
        if scenario == "escaped":
            return {"ok": True, "mode": "flee", "moved_dist": 5.0}
        return {"ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    result = CombatControllerSlot().handle(
        CombatEnv(CaveBot()), {"source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": 11},
    )

    attacks = [body for action, body in calls if action == "attack"]
    if scenario == "cornered":
        attack, = attacks
        assert calls[0] == ("equip", {"item": "stone_sword", "slot": "hand"})
        assert attack["entity_id"] == 11 and attack["max_hits"] == 1 and attack["timeout"] <= 350
        assert result["actions"][-1]["ok"] is True
    else:
        assert attacks == []


@pytest.mark.parametrize(("remaining_hostile", "retreat_works"), [(True, True), (True, False), (False, True)])
def test_counter_kill_disengages_from_second_hostile_at_critical_health(monkeypatch, remaining_hostile, retreat_works):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    target = {"id": 42, "name": "zombie", "dist": 2.8, "pos": [3.3, 64.0, 0.5]}
    survivor = {"id": 43, "name": "zombie", "dist": 2.8, "pos": [0.5, 64.0, 3.3]}
    retreats = []
    attacks = []
    looks = []

    class CaveBot:
        def status(self):
            nearest = survivor if attacks and remaining_hostile else target
            return {
                "pos": [0.5, 64.0, 0.5], "health": 3.5 if attacks else 20.0, "food": 20,
                "ready": True, "alive": True, "on_ground": True, "blocking": False,
                "nearest_hostile": {key: nearest[key] for key in ("id", "name", "dist")},
            }

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                items = [{"name": "stone_sword", "count": 1}]
                if not retreat_works:
                    items.append({"name": "shield", "count": 1})
                return {"items": items}
            if action == "get_entities":
                return {"entities": [survivor] if attacks else [target, survivor]}
            if action == "look":
                looks.append(body)
            if action == "attack":
                attacks.append(body["entity_id"])
                return {"hits": 1, "killed": True}
            return {"ok": True}

    def retreat(_env, threat_pos, **kwargs):
        retreats.append(threat_pos)
        return {"ok": retreat_works}

    monkeypatch.setattr(CombatEnv, "flee", retreat)
    outcome = CombatControllerSlot().handle(
        CombatEnv(CaveBot()), {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie"},
    )

    assert attacks == [42]
    assert retreats == ([survivor["pos"]] if remaining_hostile else [])
    assert outcome["mode"] == ("low_hp_disengage" if remaining_hostile else "counter_killed")
    if not retreat_works:
        assert outcome["latched_guard"] is True
        assert looks[-1] == {"entity_id": 43}


def test_bug023_deep_water_trident_shield_first_moves_to_dry_shore_and_3hit_cooldown(monkeypatch):
    """Covers BUG-023: trident/drowned in deep water raises shield first, moves to dry shore, and blocks reentry 30s on 3rd hit."""
    from mc_agent.skills.combat_env import CombatEnv
    from mc_agent.skills.safety import SafetyEmergency, check_and_stabilize

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])

    escape_calls: list[str] = []
    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: escape_calls.append("escape_water") or {"ok": False, "in_water": True},
    )

    # 1) Trident hit in deep water: equip+raise shield BEFORE escape, ignore y=50 ocean floor, move_to dry shore at [12.5, 63.0, -7.5]
    state = {
        "in_water": True,
        "submerged": True,
        "pos": [0.5, 61.5, 0.5],
        "hp": 15.5,
        "blocking": False,
        "offhand": None,
        "hurt_seq": 1,
        "shore_succeeds": True,
    }
    calls_1: list[tuple[str, dict]] = []

    class _ShoreOceanBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status")

        def inventory(self):
            return self.call("inventory")

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            calls_1.append((action, payload))
            if action == "status":
                return {
                    "pos": list(state["pos"]),
                    "health": state["hp"],
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": state["in_water"],
                    "submerged": state["submerged"],
                    "on_ground": not state["in_water"],
                    "blocking": state["blocking"],
                    "oxygen": 240,
                    "nearest_hostile": {"id": 44, "name": "drowned", "pos": [7.5, 62.0, 0.5], "dist": 7.0},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = (
                    [{"seq": state["hurt_seq"], "type": "hurt", "health": state["hp"], "source": "trident", "source_entity": {"id": 44, "pos": [7.5, 62.0, 0.5]}}]
                    if state["hurt_seq"] > since
                    else []
                )
                return {"events": rows, "last_seq": state["hurt_seq"]}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": state["offhand"]}
            if action == "equip" and payload.get("item") == "shield":
                state["offhand"] = {"name": "shield", "count": 1}
                return {"ok": True}
            if action == "posture":
                state["blocking"] = bool(payload.get("shield"))
                return {"ok": True}
            if action == "find_blocks":
                return {
                    "blocks": [
                        {"name": "sand", "pos": [1, 50, 1], "dist": 11.5},  # ocean floor below waterline -> must ignore
                        {"name": "grass_block", "pos": [12, 62, -8], "dist": 14.3},
                        {"name": "stone", "pos": [20, 62, 10], "dist": 21.7},
                    ]
                }
            if action == "block_at":
                p = payload.get("pos", [0, 64, 0])
                if p[0] == 12 and p[2] == -8 and p[1] in (63, 64):
                    return {"name": "air"}
                return {"name": "water"}
            if action == "move_to":
                if state["shore_succeeds"]:
                    state["pos"] = [payload["x"], payload["y"], payload["z"]]
                    state["in_water"] = False
                    state["submerged"] = False
                return {"ok": True}
            return {"ok": True}

    emitted: list[dict] = []
    guard = GenerationGuard(_ShoreOceanBot(), EventSink(emitted.append), checkpoint=lambda: None, skills=MacroSkills(_ShoreOceanBot()))
    inc = guard.poll()
    assert inc is not None
    act_names = [a["action"] for a in inc["actions"]]
    assert act_names == ["equip_shield", "face_attacker", "raise_shield", "escape_water", "escape_to_shore"]
    move_calls = [pl for act, pl in calls_1 if act == "move_to"]
    assert move_calls and (move_calls[0]["x"], move_calls[0]["y"], move_calls[0]["z"]) == (12.5, 63.0, -7.5)
    assert state["in_water"] is False

    # 2) Three consecutive hurts still in water (<3s apart) -> 3rd hit forces shore escape & sets 30s no-water cooldown
    emitted.clear()
    escape_calls.clear()
    state.update({"in_water": True, "submerged": True, "pos": [0.5, 61.5, 0.5], "hp": 14.0, "blocking": False, "shore_succeeds": False, "hurt_seq": 2})
    bot2 = _ShoreOceanBot()
    bot2._last_event_seq = 1
    guard2 = GenerationGuard(bot2, EventSink(emitted.append), checkpoint=lambda: None, skills=MacroSkills(bot2))

    # Hit 1
    inc1 = guard2.poll()
    assert inc1 is not None
    assert guard2._water_hurt_streak == 1
    assert guard2.no_water_until < 0

    # Hit 2 (+1.2s, inside normal 3s window -> must NOT be dropped!)
    clock[0] += 1.2
    state.update({"hurt_seq": 3, "hp": 10.0, "blocking": False})
    inc2 = guard2.poll()
    assert inc2 is not None
    assert guard2._water_hurt_streak == 2

    # Hit 3 (+1.2s) -> climb succeeds on 3rd try, forces escape_to_shore (skipping slow escape_water) and activates 30s cooldown
    clock[0] += 1.2
    escape_calls.clear()
    state.update({"hurt_seq": 4, "hp": 6.5, "blocking": True, "shore_succeeds": True})
    inc3 = guard2.poll()
    assert inc3 is not None
    assert "water_avoid_cooldown" in inc3["reasons"]
    assert "禁止下水冷却中" in inc3["note"]
    assert guard2.no_water_until == pytest.approx(clock[0] + 30.0)
    assert any(ev.get("type") == "guardian.no_water_cooldown" and ev.get("duration_s") == 30.0 for ev in emitted)
    assert escape_calls == []
    assert [a["action"] for a in inc3["actions"]] == ["escape_to_shore"]
    assert state["in_water"] is False

    # Step back into water 6s later (within 30s cooldown, no new hurt) -> forced back ashore immediately!
    clock[0] += 6.0
    calls_1.clear()
    state.update({"in_water": True, "submerged": False, "pos": [10.5, 62.0, -7.5], "shore_succeeds": True})
    inc_reentry = guard2.poll()
    assert inc_reentry is not None
    assert "water_avoid_cooldown" in inc_reentry["reasons"]
    assert [a["action"] for a in inc_reentry["actions"]] == ["escape_to_shore"]
    assert state["in_water"] is False

    # 3) handle_preemption & stabilize_after_tools & safety.check_and_stabilize water-shield priority
    state.update({"in_water": True, "submerged": True, "pos": [0.5, 61.5, 0.5], "hp": 5.0, "blocking": False, "offhand": None, "shore_succeeds": True})
    guard_pre = GenerationGuard(_ShoreOceanBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(_ShoreOceanBot()))
    rec = guard_pre.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_entity_id": 44, "primary_type": "drowned", "primary_pos": [7.5, 62.0, 0.5], "distance": 7.0},
        interrupted_name="mc_move_to",
    )
    assert rec["reflex_mode"] == "water_surface"
    assert state["blocking"] is True
    assert state["in_water"] is False

    state.update({"in_water": True, "submerged": False, "pos": [0.5, 62.2, 0.5], "hp": 11.0, "blocking": False, "offhand": None, "hurt_seq": 5, "shore_succeeds": True})
    calls_1.clear()
    bot_b = _ShoreOceanBot()
    bot_b._last_event_seq = 4
    guard_b = GenerationGuard(bot_b, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot_b))
    b_note = guard_b.stabilize_after_tools(['{"ok": false, "in_water": true}'])
    assert b_note is not None and "游向最近干燥陆地登岸" in b_note
    assert any(act == "equip" for act, _ in calls_1)

    state.update({"in_water": True, "submerged": True, "pos": [0.5, 61.5, 0.5], "hp": 4.0, "blocking": False, "offhand": None})
    calls_1.clear()
    with pytest.raises(SafetyEmergency):
        check_and_stabilize(_ShoreOceanBot())
    assert any(act == "equip" and pl == {"item": "shield", "slot": "off-hand"} for act, pl in calls_1)
    assert any(act == "posture" and pl.get("shield") is True and pl.get("jump") is True for act, pl in calls_1)

    # 4) Open ocean with zero shore blocks nearby lunges upward and retreats AWAY from threat_pos
    open_calls: list[tuple[str, dict]] = []

    class _OpenSeaNoLandBot:
        def status(self):
            return {
                "pos": [0.5, 61.2, 0.5],
                "health": 10.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "in_water": True,
                "submerged": True,
                "oxygen": 200,
                "yaw": 0.0,
            }

        def call(self, action, body=None, **kwargs):
            payload = dict(body or {})
            open_calls.append((action, payload))
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": []}
            if action == "block_at":
                return {"name": "water"}
            return {"ok": True}

    env_res = CombatEnv(_OpenSeaNoLandBot()).escape_deep_water_to_shore([7.5, 62.0, 0.5], keep_shield=True)
    assert env_res["mode"] == "surface_retreat"
    mv_open = [pl for act, pl in open_calls if act == "move_to"]
    assert mv_open and mv_open[0]["x"] < 0.0 and mv_open[0]["y"] >= 63.0
    assert any(act == "control" and pl.get("jump") is True and pl.get("forward") is True for act, pl in open_calls)


def test_r15_nether_bastion_wide_lava_steers_via_find_blocks_onto_elevated_ledge():
    """Reproduces r15 hy4_hy_6_nether_bastion: wide Nether lava lake steers toward elevated basalt ledge via find_blocks+move_to."""
    from mc_agent.skills.combat_env import CombatEnv

    st = {
        "pos": [227.4, 31.5, 240.0],
        "in_lava": True,
        "yaw": 3.14159,  # facing South deeper into lava lake!
    }
    calls: list[tuple[str, dict]] = []

    class _NetherLavaLakeBot:
        def status(self):
            return {
                "pos": list(st["pos"]),
                "health": 18.0,
                "alive": True,
                "ready": True,
                "in_lava": st["in_lava"],
                "yaw": st["yaw"],
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            calls.append((action, pl))
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 61}]}
            if action == "get_block":
                p = pl.get("pos", [0, 0, 0])
                if tuple(p) in {(229, 33, 234), (229, 34, 234), (227, 32, 240)}:
                    return {"name": "air"}
                if tuple(p) in {(229, 32, 234), (235, 31, 240), (235, 32, 240)}:
                    return {"name": "basalt"}  # (235,31,240) has solid at y+1 -> sheer wall, skip!
                return {"name": "lava"}
            if action == "find_blocks":
                return {
                    "blocks": [
                        {"name": "basalt", "pos": [235, 31, 240], "dist": 7.6},
                        {"name": "netherrack", "pos": [229, 32, 234], "dist": 6.2},
                    ]
                }
            if action == "move_to":
                st["pos"] = [pl["x"], pl["y"], pl["z"]]
                st["in_lava"] = False
                return {"ok": True}
            return {"ok": True}

    out = CombatEnv(_NetherLavaLakeBot()).escape_lava()
    assert out["ok"] is True and out["in_lava"] is False
    assert "lava_move_to_shore" in out["actions"]
    ctrl = next(pl for act, pl in calls if act == "control" and pl.get("forward"))
    assert ctrl["ms"] >= 1350
    mv = next(pl for act, pl in calls if act == "move_to")
    assert (mv["x"], mv["y"], mv["z"]) == (229.5, 33.0, 234.5)

    # Pathfinding ReadTimeout inside lava move_to must /stop and continue into place fallback rather than throwing!
    import httpx
    st_rt = {"pos": [227.4, 31.5, 240.0], "in_lava": True}
    rt_calls: list[str] = []

    class _LavaMoveTimeoutBot:
        def status(self):
            return {**st_rt, "health": 12.0, "alive": True, "ready": True, "yaw": 3.14, "dimension": "minecraft:the_nether"}

        def call(self, action, body=None, **kwargs):
            rt_calls.append(action)
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {"blocks": [{"name": "netherrack", "pos": [229, 32, 234], "dist": 5.9}]}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 0, 0])
                if tuple(p) in {(229, 33, 234), (229, 34, 234), (227, 32, 240)}:
                    return {"name": "air"}
                return {"name": "lava"}
            if action == "move_to":
                raise httpx.ReadTimeout("simulated pathfinder timeout in lava")
            if action == "place":
                st_rt["in_lava"] = False
            return {"ok": True}

    out_rt = CombatEnv(_LavaMoveTimeoutBot()).escape_lava()
    assert out_rt["ok"] is True and out_rt["in_lava"] is False
    assert "lava_move_to_shore_timeout" in out_rt["actions"]
    assert "stop" in rt_calls
    assert "lava_place_cobblestone" in out_rt["actions"]


def test_bug026_ranged_arrow_cover_or_flee_lava_sneak_armor_and_cooldown(monkeypatch):
    """Covers BUG-026: arrow hurts cover/flee instead of static shield-block, auto-armor/torches, lava-edge sneak+seal, and 10s ranged cooldown."""
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    clock = [2000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])

    # 1) Arrow hit WITH unblocking shield in inventory -> NEVER stops at static raise_shield!
    class _ArrowCoverSkills:
        class _Eng:
            nodes = {}
            def is_inside_shaft(self):
                return False

        mining_dfs = _Eng()

        def __init__(self, *, cover_ok: bool):
            self.cover_ok = cover_ok

        def dig_cover_pocket(self):
            return {"ok": True, "mode": "cover_pocket"} if self.cover_ok else {"ok": False, "reason": "no_solid_wall"}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee", "moved_dist": 12.0}

    def _make_arrow_bot():
        class _ArrowShieldBot:
            _last_event_seq = 0
            def call(self, action, body=None, **kwargs):
                if action == "status":
                    return {
                        "pos": [0.5, 64.0, 0.5], "health": 16.0, "food": 20, "alive": True, "ready": True,
                        "in_water": False, "in_lava": False, "on_ground": True, "blocking": False,
                        "nearest_hostile": {"id": 61, "name": "skeleton", "dist": 10.0},
                    }
                if action == "events":
                    since = int((kwargs.get("params") or {}).get("since", 0))
                    rows = [{"seq": 1, "type": "hurt", "health": 16.0, "source": "arrow", "source_entity": {"id": 61}}] if since < 1 else []
                    return {"events": rows, "last_seq": 1}
                if action == "inventory":
                    return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}], "offhand": None}
                return {"ok": True}
        return _ArrowShieldBot()

    g_cov = GenerationGuard(_make_arrow_bot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_ArrowCoverSkills(cover_ok=True))
    inc_cov = g_cov.poll()
    assert inc_cov is not None
    assert [a["action"] for a in inc_cov["actions"]] == ["dig_cover"]  # not standing still raising shield!

    g_flee = GenerationGuard(_make_arrow_bot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_ArrowCoverSkills(cover_ok=False))
    inc_flee = g_flee.poll()
    assert inc_flee is not None
    flee_names = [a["action"] for a in inc_flee["actions"]]
    assert "dig_cover" in flee_names and "combat_or_flee" in flee_names

    # Skeleton at 8.5m (>5m) with shield+sword prioritizes cover_or_flee, and blaze skips fire-trap cover_pocket!
    class _RangedCaveEnvBot:
        def __init__(self, mob_name: str, dist: float):
            self.mob_name = mob_name
            self.dist = dist
            self.fled = False

        def status(self):
            return self.call("status")

        def inventory(self):
            return self.call("inventory")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 40.0, 0.5], "health": 20.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False,
                }
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}],
                    "offhand": {"name": "shield", "count": 1},
                    "held": {"name": "iron_sword", "count": 1},
                    "armor": {"head": None, "chest": None, "legs": None, "feet": None},
                }
            if action == "get_entities":
                return {"entities": [{"id": 70, "name": self.mob_name, "pos": [0.5 + self.dist, 40.0, 0.5], "dist": self.dist}]}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 40, 0])
                return {"name": "stone" if p[1] < 40 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "move_to":
                self.fled = True
                return {"ok": True}
            return {"ok": True}

    ctrl = SubcorticalInstinctController()
    out_skel = ctrl.on_encounter(
        CombatEnv(_RangedCaveEnvBot("skeleton", 8.5)),
        {"source": "pre_hurt_ranged", "primary_entity_id": 70, "primary_type": "skeleton", "pos": [9.0, 40.0, 0.5], "distance": 8.5},
    )
    assert out_skel["mode"] == "cover_or_flee"

    out_blaze = ctrl.on_encounter(
        CombatEnv(_RangedCaveEnvBot("blaze", 6.0)),
        {"source": "pre_hurt_ranged", "primary_entity_id": 70, "primary_type": "blaze", "pos": [6.5, 40.0, 0.5], "distance": 6.0},
    )
    assert out_blaze["mode"] == "cover_or_flee"
    assert not any(a["action"] == "cover_pocket" for a in out_blaze["actions"])

    # 2) Cave gear (armor slots -> head/torso/legs/feet + offhand shield + cave torch) & lava-edge sneak+seal
    gear_calls: list[tuple[str, dict]] = []

    class _CaveAndLavaBot:
        _last_event_seq = 0
        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            gear_calls.append((action, pl))
            if action == "status":
                return {
                    "pos": [10.5, 40.0, 10.5], "health": 18.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False, "sneaking": False, "blocking": False,
                    "dimension": "minecraft:overworld",
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {
                    "items": [
                        {"name": "iron_helmet", "count": 1},
                        {"name": "iron_chestplate", "count": 1},
                        {"name": "iron_leggings", "count": 1},
                        {"name": "iron_boots", "count": 1},
                        {"name": "shield", "count": 1},
                        {"name": "torch", "count": 8},
                        {"name": "cobblestone", "count": 16},
                    ],
                    "armor": {"head": None, "chest": None, "legs": None, "feet": None},
                    "offhand": None,
                }
            if action == "get_block":
                p = pl.get("pos", [0, 40, 0])
                if tuple(p) == (11, 39, 10):
                    return {"name": "lava"}
                if p[1] < 40:
                    return {"name": "stone"}
                return {"name": "air"}
            return {"ok": True}

    g_prep = GenerationGuard(_CaveAndLavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    g_prep.prime()
    equips = [(pl.get("item"), pl.get("slot")) for act, pl in gear_calls if act == "equip"]
    assert ("iron_helmet", "head") in equips
    assert ("iron_chestplate", "torso") in equips
    assert ("iron_leggings", "legs") in equips
    assert ("iron_boots", "feet") in equips
    assert ("shield", "off-hand") in equips

    gear_calls.clear()
    note_edge = g_prep.stabilize_after_tools(['{"cave": true, "lava": true}'])
    assert note_edge is not None
    assert "岩浆边潜行" in note_edge
    assert "补放洞穴火把" in note_edge
    assert any(act == "posture" and pl.get("sneak") is True for act, pl in gear_calls)
    assert any(act == "place" and pl == {"item": "cobblestone", "pos": [11, 39, 10]} for act, pl in gear_calls)
    assert any(act == "place" and pl == {"item": "torch", "pos": [10, 40, 10]} for act, pl in gear_calls)

    # 3) pre_hurt_ranged suppresses repeats for 10s AND on_fire ticks don't clear suppress_until!
    class _BlazeOnFireDebounceBot:
        _last_event_seq = 0
        fire_seq = 0
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5], "health": 16.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False,
                    "nearest_hostile": {"id": 91, "name": "skeleton", "pos": [11.0, 64.0, 0.5], "dist": 10.5},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": self.fire_seq, "type": "hurt", "health": 16.0, "source": "on_fire"}] if self.fire_seq > since else []
                return {"events": rows, "last_seq": self.fire_seq}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 64, 0])
                return {"name": "netherrack" if p[1] < 64 else "air"}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    blaze_bot = _BlazeOnFireDebounceBot()
    g_cd = GenerationGuard(blaze_bot, EventSink(lambda _: None), checkpoint=lambda: None)
    inc_r1 = g_cd.poll()
    assert inc_r1 is not None and "pre_hurt_ranged" in inc_r1["reasons"]
    clock[0] += 5.0
    blaze_bot.fire_seq = 1  # ambient on_fire tick in between
    assert g_cd.poll() is None  # no fake cancel on non-combat fire tick AND pre_hurt_ranged 10s cooldown stays intact!
    clock[0] += 1.0
    assert g_cd.poll() is None  # still inside 10s pre_hurt_ranged cooldown!

    # 4) Known shooter pos on arrow hurt sidesteps ~2m perpendicularly BEFORE equip/raise_shield + cover/flee,
    #    and piglin_brute / pre_hurt_ranged immediately equip+raise shield on entry!
    side_calls: list[tuple[str, dict]] = []

    class _KnownShooterArrowBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            side_calls.append((action, pl))
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5], "health": 16.0, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "in_water": False, "in_lava": False, "blocking": False,
                    "nearest_hostile": {"id": 88, "name": "skeleton", "pos": [10.5, 64.0, 0.5], "dist": 10.0},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": 1, "type": "hurt", "health": 16.0, "source": "arrow", "source_entity": {"id": 88, "pos": [10.5, 64.0, 0.5]}}] if since < 1 else []
                return {"events": rows, "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}, {"name": "cobblestone", "count": 16}], "offhand": None}
            if action == "get_block":
                p = pl.get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            return {"ok": True}

    g_side = GenerationGuard(_KnownShooterArrowBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_ArrowCoverSkills(cover_ok=False))
    inc_side = g_side.poll()
    assert inc_side is not None
    side_acts = [a["action"] for a in inc_side["actions"]]
    assert side_acts[:4] == ["lateral_sidestep", "equip_shield", "face_attacker", "raise_shield"]
    assert side_acts[-1] == "combat_or_flee"

    out_brute = ctrl.on_encounter(
        CombatEnv(_RangedCaveEnvBot("piglin_brute", 2.9)),
        {"source": "pre_hurt_melee", "primary_entity_id": 70, "primary_type": "piglin_brute", "pos": [3.4, 40.0, 0.5], "distance": 2.9},
    )
    assert any(a["action"] == "raise_shield_entry" for a in out_brute["actions"])


def test_bug028_real_void_geofence_across_escape_lava_water_and_lava_edge():
    """BUG-028 P0 regression: real VoidGeofence uses check_point(), NEVER contains(), in lava/water/edge paths."""
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.skills.combat_env import CombatEnv

    gf = VoidGeofence(center_x=225.0, center_z=235.0, radius=16.0, margin=4.0)  # safe_radius = 12.0

    # 1) escape_lava with find_blocks candidates (one outside safe_radius=12, one inside!) -> must NOT raise AttributeError!
    st_lava = {"pos": [227.4, 31.5, 235.0], "in_lava": True, "yaw": 3.14}
    moves: list[tuple[float, float, float]] = []

    class _GfLavaBot:
        def status(self):
            return {**st_lava, "health": 12.0, "alive": True, "ready": True, "dimension": "minecraft:the_nether"}

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {
                    "blocks": [
                        {"name": "basalt", "pos": [239, 32, 235], "dist": 11.6},  # outside VoidGeofence safe_radius=12!
                        {"name": "netherrack", "pos": [229, 32, 234], "dist": 2.1},  # inside safe_radius -> pick this!
                    ]
                }
            if action == "get_block":
                p = pl.get("pos", [0, 0, 0])
                if tuple(p) in {(229, 33, 234), (229, 34, 234), (227, 32, 235)}:
                    return {"name": "air"}
                return {"name": "lava"}
            if action == "move_to":
                moves.append((pl["x"], pl["y"], pl["z"]))
                st_lava["pos"] = [pl["x"], pl["y"], pl["z"]]
                st_lava["in_lava"] = False
                return {"ok": True}
            return {"ok": True}

    out_l = CombatEnv(_GfLavaBot(), geofence=gf).escape_lava()
    assert out_l["ok"] is True and out_l["in_lava"] is False
    assert (229.5, 33.0, 234.5) in moves

    # 2) escape_deep_water_to_shore (radial fallback + surface_retreat) with real VoidGeofence -> no AttributeError!
    class _GfWaterBot:
        def status(self):
            return {"pos": [225.5, 61.5, 235.5], "health": 14.0, "alive": True, "ready": True, "in_water": True, "submerged": True, "oxygen": 200}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            return {"ok": True}

    out_w = CombatEnv(_GfWaterBot(), geofence=gf).escape_deep_water_to_shore([232.0, 62.0, 235.5])
    assert out_w["mode"] == "surface_retreat"

    # 3) guard_lava_edge skips lava cell outside safe_radius and seals in-bounds adjacent lava!
    placed_edge: list[list[int]] = []

    class _GfEdgeBot:
        def status(self):
            return {"pos": [236.5, 40.0, 235.5], "on_ground": True, "in_lava": False, "ready": True, "alive": True, "sneaking": False}

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                p = tuple(pl.get("pos", [0, 0, 0]))
                if p in {(237, 39, 235), (235, 39, 235)}:  # 237.5 out of safe_radius(12 from 225); 235.5 in bounds!
                    return {"name": "lava"}
                return {"name": "stone" if p[1] < 40 else "air"}
            if action == "place":
                placed_edge.append(list(pl["pos"]))
            return {"ok": True}

    edge_res = CombatEnv(_GfEdgeBot(), geofence=gf).guard_lava_edge([236.5, 40.0, 235.5])
    assert edge_res["ok"] is True and edge_res["sneak"] is True
    assert placed_edge == [[235, 39, 235]]


def test_water_retreat_turns_inward_at_geofence():
    import math

    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    geofence = VoidGeofence(radius=55.0, margin=5.0)
    status = {"pos": [49.9, 62.0, 0.0], "yaw": -math.pi / 2, "in_water": True, "submerged": True}
    motions = []

    class WaterBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            if action == "move_to":
                assert body["x"] < status["pos"][0]
                assert geofence.check_point(body["x"], body["z"]) is None
                status["pos"] = [body["x"], body["y"], body["z"]]
            if action == "control":
                motions.append(body)
                if body.get("forward"):
                    x, _, z = status["pos"]
                    ux, uz = yaw_to_unit_xz(body["yaw"])
                    assert geofence.check_point(x + 5 * ux, z + 5 * uz) is None
            return {"ok": True}

    result = CombatEnv(WaterBot(), geofence=geofence).escape_deep_water_to_shore()
    assert result["actions"] == ["water_surface_surge", "surface_retreat_move", "surface_retreat_jump"]
    assert motions[0]["forward"] is False and motions[0]["jump"] is True
    assert motions[1]["forward"] is True


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
        # Let watchdog run a few more ticks post-preemption too
        time.sleep(0.08)
    finally:
        stop_wd.set()
        wd.join(timeout=1.0)

    assert watchdog_errors == []
    assert watchdog_ticks["n"] >= 5
    assert all(readonly_ok.values())
    assert actor_preempted["done"] is True


def test_r18_lava_airborne_swim_timeout_and_creeper_flee_timeout_hardening(monkeypatch):
    """Reproduces r18_22bot hy4_hy_6_nether_bastion & glm_glm_4_brewing_alchemy:
    1) Airborne bob above lava (`in_lava=False, on_ground=False` over lava) must NOT exit ok=True prematurely; continue to place filler block.
    2) Transient ReadTimeout in lava `control` or creeper `flee.move_to` must `/stop` and continue to cobblestone place / creeper shield latch.
    """
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)

    # 1) Airborne bob above lava + ReadTimeout during lava_surface_swim -> /stop & bridge cobblestone before returning ok=True
    state_l: dict[str, Any] = {
        "pos": [237.5, 34.3, 196.3],
        "in_lava": True,
        "on_ground": False,
        "placed_solid": set(),
    }
    lava_actions: list[str] = []

    class _AirborneLavaBot:
        def status(self):
            return {
                "pos": list(state_l["pos"]),
                "health": 16.0,
                "alive": True,
                "ready": True,
                "in_lava": state_l["in_lava"],
                "on_ground": state_l["on_ground"],
                "yaw": 0.64,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            lava_actions.append(action)
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in state_l["placed_solid"]:
                    return {"name": "cobblestone"}
                if p == (237, 35, 196) and state_l["in_lava"]:
                    return {"name": "lava"}
                if p[1] == 34:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                # First swim call simulates transient ReadTimeout; jump-sprint launches mid-air above lava!
                if pl.get("ms") == 950 and lava_actions.count("control") == 1:
                    assert pl["jump"] is True
                    assert pl["forward"] is False and pl["sprint"] is False
                    raise httpx.ReadTimeout("simulated swim control timeout")
                if pl.get("forward"):
                    state_l["pos"] = [234.7, 36.0, 193.8]
                    state_l["in_lava"] = False
                    state_l["on_ground"] = False
                return {"ok": True}
            if action == "place":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                state_l["placed_solid"].add(p)
                state_l["in_lava"] = False
                if p in {(234, 35, 193), (234, 34, 193)}:
                    state_l["pos"] = [234.5, float(p[1] + 1), 193.5]
                    state_l["on_ground"] = True
                return {"ok": True}
            return {"ok": True}

    out_l = CombatEnv(_AirborneLavaBot()).escape_lava()
    assert out_l["ok"] is True and out_l["in_lava"] is False
    assert "lava_surface_swim_timeout" in out_l["actions"]
    assert "lava_jump_sprint" in out_l["actions"]
    assert "lava_place_cobblestone" in out_l["actions"]
    assert "stop" in lava_actions
    assert state_l["placed_solid"]  # Did NOT exit ok=True while airborne at y=36 above y=34 lava!

    # 2) Creeper evade: move_to ReadTimeout inside flee() must /stop and still latch shield (`latched_guard=True`)
    creeper_calls: list[tuple[str, dict]] = []

    class _CreeperFleeTimeoutBot:
        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5],
                "health": 19.0,
                "food": 17,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "blocking": False,
                "nearest_hostile": {"id": 88, "name": "creeper", "pos": [2.5, 64.0, 0.5], "dist": 2.0},
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            creeper_calls.append((action, pl))
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [{"id": 88, "name": "creeper", "pos": [2.5, 64.0, 0.5], "dist": 2.0}]}
            if action == "get_block":
                p = pl.get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "move_to":
                raise httpx.ReadTimeout("simulated creeper flee pathfinder timeout")
            return {"ok": True}

    out_c = SubcorticalInstinctController().on_encounter(
        CombatEnv(_CreeperFleeTimeoutBot()),
        {"source": "pre_hurt_creeper", "primary_entity_id": 88, "primary_type": "creeper", "primary_pos": [2.5, 64.0, 0.5], "distance": 2.0},
    )
    assert out_c["mode"] == "creeper_evade"
    assert out_c["latched_guard"] is True
    assert any(a == "stop" for a, _ in creeper_calls)
    assert any(a == "posture" and pl_c.get("shield") is True for a, pl_c in creeper_calls)


def test_bug034_ranged_combat_stance_blocking_and_cover_exempt_cancels(monkeypatch):
    """Covers BUG-034: 30s ranged cooldown + 35s combat stance + blocking/dig_cover exemption stop skeleton cancel storms."""
    clock = [3000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])

    state = {
        "skeleton_on": False,
        "blocking": False,
        "health": 18.0,
        "hurt_seq": 0,
        "close_creeper": False,
    }
    cover_calls = {"n": 0}

    class _CoverSkills:
        class _Eng:
            nodes = {}
            def is_inside_shaft(self):
                return False

        mining_dfs = _Eng()

        def __init__(self, *, cover_ok: bool):
            self.cover_ok = cover_ok

        def dig_cover_pocket(self):
            cover_calls["n"] += 1
            return {"ok": self.cover_ok, "mode": "cover_pocket"} if self.cover_ok else {"ok": False, "reason": "no_filler_block"}

        def defend_or_flee(self, **kwargs):
            return {"ok": True, "mode": "flee", "moved_dist": 4.0}

    # 1) Direct poll checks: initial pre_hurt_ranged cancels once, then 35s stance & blocking=True exempt sustained arrows!
    class _SustainedSkeletonBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                hostile = (
                    {"id": 99, "name": "creeper", "pos": [2.0, 64.0, 0.5], "dist": 1.5}
                    if state["close_creeper"]
                    else ({"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0} if state["skeleton_on"] else None)
                )
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": state["health"],
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": state["blocking"],
                    "nearest_hostile": hostile,
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                rows = [{"seq": state["hurt_seq"], "type": "hurt", "health": state["health"], "source": "arrow"}] if state["hurt_seq"] > since else []
                return {"events": rows, "last_seq": state["hurt_seq"]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                if state["close_creeper"]:
                    return {"entities": [{"id": 99, "name": "creeper", "pos": [2.0, 64.0, 0.5], "dist": 1.5}]}
                return {"entities": [{"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0}] if state["skeleton_on"] else []}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            return {"ok": True}

    guard = GenerationGuard(_SustainedSkeletonBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_CoverSkills(cover_ok=False))
    state["skeleton_on"] = True
    inc1 = guard.poll()
    assert inc1 is not None and "pre_hurt_ranged" in inc1["reasons"]
    assert inc1["requires_replan"] is True

    # 12s & 24s later (inside 30s target cooldown & 35s stance): pre_hurt_ranged MUST NOT re-fire!
    clock[0] += 12.0
    assert guard.poll() is None
    clock[0] += 12.0
    assert guard.poll() is None

    # Sustained arrow hit while blocking=True and HP=18.0 -> exempted from cancelling generation (`requires_replan=False`),
    # and subsequent arrow hits within 15s of failed dig_cover stay completely silent (`None`)!
    state["blocking"] = True
    state["hurt_seq"] = 1
    clock[0] += 1.0
    inc_blk1 = guard.poll()
    assert inc_blk1 is None or inc_blk1["requires_replan"] is False
    clock[0] += 4.0
    state["hurt_seq"] = 2
    assert guard.poll() is None  # no repeat dig_cover churn or cancel storm!

    # Low HP (<=10.0) or close creeper STILL immediately overrides and requires replan!
    state["health"] = 9.0
    state["hurt_seq"] = 3
    clock[0] += 3.5
    inc_low = guard.poll()
    assert inc_low is not None and inc_low["requires_replan"] is True

    state["health"] = 18.0
    state["close_creeper"] = True
    clock[0] += 0.5
    inc_cr = guard.poll()
    assert inc_cr is not None and "pre_hurt_creeper" in inc_cr["reasons"] and inc_cr["requires_replan"] is True

    # 2) End-to-end run_mc_agent: sustained skeleton visibility + arrows over 26s cancels ONLY ONCE!
    clock[0] = 4000.0
    state.update({"skeleton_on": False, "blocking": False, "health": 18.0, "hurt_seq": 0, "close_creeper": False})
    req_n = {"n": 0}

    def handle_http(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            res = {
                "pos": [0.5, 64.0, 0.5],
                "health": state["health"],
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "in_lava": False,
                "blocking": state["blocking"],
                "nearest_hostile": {"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0} if state["skeleton_on"] else None,
            }
        elif action == "events":
            params_s = str(request.url.query or b"")
            since = int(params_s.split("since=")[-1].split("&")[0]) if "since=" in params_s else 0
            rows = [{"seq": state["hurt_seq"], "type": "hurt", "health": state["health"], "source": "arrow"}] if state["hurt_seq"] > since else []
            res = {"events": rows, "last_seq": state["hurt_seq"]}
        elif action == "inventory":
            res = {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}], "offhand": {"name": "shield"}}
        elif action == "get_entities":
            res = {"entities": [{"id": 468872, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0}] if state["skeleton_on"] else []}
        elif action == "get_block":
            body = json.loads(request.content) if request.content else {}
            p = body.get("pos", [0, 64, 0])
            res = {"name": "stone" if p[1] < 64 else "air"}
        elif action == "find_blocks":
            res = {"blocks": []}
        else:
            res = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": res})

    def chat_ranged(messages, tools=None, *, control=None, **kwargs):
        req_n["n"] += 1
        if req_n["n"] == 1:
            state["skeleton_on"] = True
            clock[0] += 0.5
            check_control(control)
            pytest.fail("Expected first ranged trigger to cancel generation 1 once.")
        # Simulate 26s of sustained skeleton pressure (11s, 21s, + arrow hits while blocking=True)
        state["blocking"] = True
        for seq_i in range(1, 4):
            clock[0] += 9.0
            state["hurt_seq"] = seq_i
            check_control(control)
        return {"role": "assistant", "content": "处于远程交战姿态持续输出，未被反复打断。"}

    with BotSession("cached", transport=httpx.MockTransport(handle_http)) as bot:
        rep = run_mc_agent(
            "bug-034 sustained ranged stance",
            bot,
            chat_fn=chat_ranged,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
        )
    assert rep["status"] == "answered"
    assert req_n["n"] == 2
    assert rep["guardian"]["cancelled_generations"] == 1


def test_deep_lava_keeps_swimming_up_while_the_head_is_submerged():
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 30.0, 0.5], "in_lava": True, "alive": True,
        "ready": True, "on_ground": False, "dimension": "the_nether",
    }
    controls = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (0, 0) and y <= 33:
                    return {"name": "lava"}
                return {"name": "netherrack" if y <= 32 else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                controls.append(body["forward"])
                if body["forward"]:
                    assert status["pos"][1] >= 33
                    status.update(in_lava=False, on_ground=True, pos=[2.5, 33.0, 0.5])
                else:
                    status["pos"] = [0.5, status["pos"][1] + 1.0, 0.5]
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()
    assert controls == [False, False, False, True]
    assert result["ok"] is True and result["in_lava"] is False


def test_r19_lava_pit_places_netherrack_filler_before_second_sprint(monkeypatch):
    """Reproduces r19_short hy4_hy_8_nether: boxed Nether pit must try netherrack bridge right after first sprint instead of starving."""
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    clock = [5000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    state_p = {"in_lava": True, "pos": [967.5, 30.1, 373.5], "sprint_n": 0}
    action_seq: list[str] = []

    class _PitLavaBot:
        def status(self):
            return {
                "pos": list(state_p["pos"]),
                "health": 10.0,
                "alive": True,
                "ready": True,
                "in_lava": state_p["in_lava"],
                "on_ground": not state_p["in_lava"],
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            action_seq.append(action)
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "netherrack", "count": 48}]}
            if action == "get_block":
                p = (body or {}).get("pos", [0, 0, 0])
                if p[1] in (30, 31) and p[0] == 967 and p[2] == 373:
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "control":
                if (body or {}).get("forward"):
                    state_p["sprint_n"] += 1
                    # Simulate elapsed time hitting 4.6s right after sprint #1 (just before old 5.0s deadline!)
                    clock[0] += 4.6 if state_p["sprint_n"] == 1 else 1.0
                return {"ok": True}
            if action == "place":
                state_p["in_lava"] = False
                return {"ok": True}
            return {"ok": True}

    out_p = CombatEnv(_PitLavaBot()).escape_lava()
    assert out_p["ok"] is True and out_p["in_lava"] is False
    assert "lava_place_netherrack" in out_p["actions"]
    assert out_p["actions"].count("lava_jump_sprint") == 1


def test_r19_poll_skips_eat_in_close_melee_range_after_combat_timeout(monkeypatch):
    """Reproduces r19_short hy4_hy_3_midgame: never drop shield to eat when a hostile is <= 5.5m after melee cooldown."""
    clock = [6000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    use_items: list[dict] = []
    state = {"hp": 7.8, "food": 17, "blocking": True, "mob_dist": 2.2}

    class _CloseThreatEatBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": state["hp"],
                    "food": state["food"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": state["blocking"],
                    "nearest_hostile": {"id": 51, "name": "zombie", "pos": [0.5 + state["mob_dist"], 64.0, 0.5], "dist": state["mob_dist"]},
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 10}, {"name": "iron_sword", "count": 1}]}
            if action == "use_item":
                use_items.append(dict(body or {}))
            return {"ok": True}

    guard = GenerationGuard(_CloseThreatEatBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    # Mark zombie id=51 as already in 12s pre_hurt_melee cooldown (simulating prior subcortical_combat timeout)
    guard._mark_pre_hurt_cooldown({"source": "pre_hurt_melee", "primary_entity_id": 51, "primary_type": "zombie"}, clock[0])
    assert guard.poll() is None
    assert use_items == []  # MUST NOT drop shield (`posture(shield=False)`) to eat right in zombie's face!

    # Once distanced > 5.5m safely on solid ground, poll() eats cleanly; while falling (on_ground=False) it skips eat!
    state["mob_dist"] = 9.0
    clock[0] += 0.5
    inc_safe = guard.poll()
    assert inc_safe is not None
    assert [a["action"] for a in inc_safe["actions"]] == ["eat"]
    assert use_items == [{"item": "cooked_beef"}]

    # Falling mid-air must NOT call eat
    class _FallingBot(_CloseThreatEatBot):
        def call(self, action, body=None, **kwargs):
            res = super().call(action, body, **kwargs)
            if action == "status":
                res["on_ground"] = False
                res["nearest_hostile"] = None
            return res

    use_items.clear()
    g_fall = GenerationGuard(_FallingBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert g_fall.poll() is None
    assert use_items == []

    # cover_or_flee when flee fails against skeleton must keep shield latched (`latched_guard=True`)
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    latch_calls: list[tuple[str, dict]] = []

    class _FailedFleeSkeletonBot:
        def status(self):
            return {
                "pos": [0.5, 64.0, 0.5],
                "health": 6.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "on_ground": False,
                "in_water": False,
                "in_lava": False,
                "nearest_hostile": {"id": 77, "name": "skeleton", "pos": [6.5, 64.0, 0.5], "dist": 6.0},
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            latch_calls.append((action, pl))
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [{"id": 77, "name": "skeleton", "pos": [6.5, 64.0, 0.5], "dist": 6.0}]}
            if action == "get_block":
                p = pl.get("pos", [0, 64, 0])
                return {"name": "stone" if p[1] < 64 else "air"}
            if action == "move_to":
                raise ApiError("path_not_found", "blocked")
            return {"ok": True}

    out_latch = CombatControllerSlot().handle(
        CombatEnv(_FailedFleeSkeletonBot()),
        {"source": "pre_hurt_ranged", "primary_entity_id": 77, "primary_type": "skeleton", "distance": 6.0},
    )
    assert out_latch["latched_guard"] is True
    assert latch_calls[-1] == ("posture", {"shield": True, "sneak": False, "jump": False})


@pytest.mark.parametrize("distance_after, keep_shield", [(11.6, True), (16.0, False)])
def test_low_health_retreat_keeps_shield_against_a_nearby_shooter(distance_after, keep_shield):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    pos = [0.5, 64.0, 0.5]
    shooter = {"id": 77, "name": "skeleton", "pos": [9.6, 64.0, 0.5], "dist": 9.1}
    posture_calls = []

    class RetreatBot:
        def status(self):
            return {"pos": list(pos), "health": 7, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "nearest_hostile": dict(shooter)}

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [dict(shooter)]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            if action == "control" and body.get("forward"):
                pos[0] -= 2.5
                shooter["dist"] = distance_after
            if action in {"posture", "look"}:
                posture_calls.append((action, body))
            return {"ok": True}

    outcome = CombatControllerSlot().handle(
        CombatEnv(RetreatBot()), {"source": "pre_hurt_ranged", "primary_entity_id": 77, "primary_type": "skeleton"},
    )
    assert {"action": "flee", "ok": True, "moved": 2.5} in outcome["actions"]
    assert outcome["latched_guard"] is keep_shield
    if keep_shield:
        assert posture_calls[-2:] == [
            ("look", {"entity_id": 77}), ("posture", {"shield": True, "sneak": False, "jump": False}),
        ]
    else:
        assert posture_calls[-1] == ("posture", {})


@pytest.mark.parametrize("entry", ["poll", "boundary"])
def test_recent_melee_damage_defers_eating_with_shield_up(entry, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    actions = []

    class ShieldBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [814.2, 78, -850.7], "health": 12, "food": 13, "on_ground": True,
                    "blocking": True, "ready": True, "alive": True,
                    "nearest_hostile": {"name": "enderman", "dist": 17.7},
                }
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            actions.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(ShieldBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    check = guard.poll if entry == "poll" else guard.stabilize_after_tools
    clock[0] += 8.0
    assert check() is None
    assert actions == []

    clock[0] += 2.5
    assert check() is not None
    assert actions == [("posture", {"shield": False}), ("use_item", {"item": "cooked_beef"})]


def test_r20_lava_bridge_places_against_adjacent_solid_wall_when_underfoot_unsupported(monkeypatch):
    """Reproduces r20_short hy4_hy_6_nether_bastion: when underfoot place fails no_support/blocked_by_self, place side bridge against bank & step onto it."""
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    bridge_state = {"in_lava": True, "pos": [249.5, 28.1, 208.5], "placed": set()}

    class _DeepBankLavaBot:
        def status(self):
            return {
                "pos": list(bridge_state["pos"]),
                "health": 15.0,
                "alive": True,
                "ready": True,
                "in_lava": bridge_state["in_lava"],
                "on_ground": not bridge_state["in_lava"],
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 60}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in bridge_state["placed"]:
                    return {"name": "cobblestone"}
                # Solid bastion wall 2 blocks East (x=251) while x=249..250 y=28 is lava
                if p[0] == 251 and p[1] in (27, 28):
                    return {"name": "blackstone"}
                if p[1] in (27, 28):
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "place":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in {(249, 28, 208), (249, 27, 208)}:
                    raise ApiError("no_support", "no solid face in open lava")
                bridge_state["placed"].add(p)
                return {"ok": True}
            if action == "control" and pl.get("forward") and bridge_state["placed"]:
                bridge_state["in_lava"] = False
                bridge_state["pos"] = [250.5, 29.0, 208.5]
            return {"ok": True}

    out_b = CombatEnv(_DeepBankLavaBot()).escape_lava()
    assert out_b["ok"] is True and out_b["in_lava"] is False
    assert "lava_place_cobblestone" in out_b["actions"]
    assert "lava_step_bridge" in out_b["actions"]


@pytest.mark.parametrize("hit_source", ["fireball", "fire_tick_only", "mob_attack"])
def test_nether_fire_ticks_do_not_replace_ranged_hit_or_start_a_bunker(monkeypatch, hit_source):
    mob = "zombie" if hit_source == "mob_attack" else "blaze"
    attacker = {"id": 42, "name": mob, "dist": 5.1}
    hurt = []
    if hit_source != "fire_tick_only":
        hurt.append({"seq": 1, "type": "hurt", "source": hit_source, "source_entity": attacker})
    hurt.append({"seq": 2, "type": "hurt", "source": "on_fire"})
    retreat_calls = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-79.3, 64, 624.2], "health": 3.0, "food": 17, "dimension": "the_nether",
                    "on_fire": True, "on_ground": True, "alive": True, "ready": True, "nearest_hostile": attacker,
                }
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [row for row in hurt if row["seq"] > since]}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": []}
            return {"ok": True}

    class BunkerSkills:
        def dig_safety_bunker(self, *, urgent):
            assert urgent
            return {"ok": True}

    def retreat(status, target, counts, *, ranged=False, prefer_cover=False):
        retreat_calls.append((target, ranged))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=BunkerSkills())
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()

    action, = incident["actions"]
    if hit_source == "mob_attack":
        assert action["action"] == "emergency_bunker"
        assert retreat_calls == []
    else:
        assert action["action"] == "combat_or_flee"
        assert retreat_calls == [(attacker if hit_source == "fireball" else None, True)]


@pytest.mark.parametrize("landed", [False, True])
def test_lava_reflex_requires_landing_even_without_lava_under_the_hitbox(monkeypatch, landed):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-733.7, 64.4, 287.3], "dimension": "the_nether", "health": 20, "food": 20,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, y, z) == (-734, 64, 287):
                    return {"name": "lava"}
                return {"name": "netherrack" if y < 64 else "air"}
            if action == "control" and body.get("forward"):
                assert body["jump"] is True
                status.update(in_lava=False, on_ground=landed, pos=[-734.4, 64.0 if landed else 64.8, 286.3])
                clock[0] += 5.0
            return {"ok": True}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    escape, = incident["actions"]
    assert escape["action"] == "escape_lava"
    assert escape["ok"] is landed
    assert escape["result"]["in_lava"] is False
    assert "airborne_above_lava" not in escape["result"]
    if not landed:
        assert escape["result"]["reason"] == "lava_landing_unconfirmed"


@pytest.mark.parametrize("bridge_edge", [False, True])
def test_r20_lava_rim_hitbox_airborne_and_lava_hurt_trigger_escape_not_bunker(monkeypatch, bridge_edge):
    """Reproduces r20_short ds_ds_4_nether_fortress:
    1) Mid-air pos `[705.9, 64.6, 640]` whose 0.3m hitbox clips `(706, 64, 640)` lava stays in escape_lava until grounded dry.
    2) `hurt(source="lava")` while `status["in_lava"]` fluctuates False routes to `escape_lava`, NEVER `emergency_bunker`.
    """
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)

    rim_state: dict[str, Any] = {
        "in_lava": not bridge_edge,
        "on_ground": False,
        "pos": [705.9, 64.6, 640.0] if bridge_edge else [706.5, 64.0, 640.5],
        "placed": set(),
    }

    class _LavaRimBot:
        def status(self):
            return {
                "pos": list(rim_state["pos"]),
                "health": 15.6,
                "alive": True,
                "ready": True,
                "in_lava": rim_state["in_lava"],
                "on_ground": rim_state["on_ground"],
                "yaw": -1.57,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p in rim_state["placed"]:
                    return {"name": "cobblestone"}
                if p[0] <= (705 if bridge_edge else 704) and p[1] == 63:
                    return {"name": "netherrack"}
                if p[0] >= 706 and (p[1] == 63 or (p[1] == 64 and not bridge_edge)):
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "control" and pl.get("forward") and not rim_state["placed"]:
                # Jump leaves center at 705.9 over air while 0.3m hitbox still clips lava at x=706!
                rim_state["pos"] = [705.9, 64.6, 640.0]
                rim_state["in_lava"] = False
                rim_state["on_ground"] = False
            elif action == "place":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                rim_state["placed"].add(p)
                rim_state["pos"] = [704.5, 64.0, 640.5]
                rim_state["on_ground"] = True
            return {"ok": True}

    out_rim = CombatEnv(_LavaRimBot()).escape_lava()
    assert out_rim["ok"] is True
    assert rim_state["placed"]  # Did NOT exit ok=True while [705.9, 64.6, 640.0] clipped (706, 64, 640) lava!

    # 2) poll() with hurt(source="lava") while in_lava=False must escape lava rim instead of digging a bunker
    poll_state: dict[str, Any] = {
        "pos": [705.9, 64.0, 640.5],
        "in_lava": False,
        "on_ground": True,
        "hurt_seq": 1,
    }
    bunker_calls: list[bool] = []
    flee_calls: list[bool] = []
    control_calls: list[dict] = []

    class _LavaHurtPollSkills:
        mining_dfs = None

        def dig_safety_bunker(self, urgent=False):
            bunker_calls.append(bool(urgent))
            return {"ok": False}

        def defend_or_flee(self, **kwargs):
            flee_calls.append(True)
            return {"ok": False}

    class _LavaHurtPollBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return {
                    "pos": list(poll_state["pos"]),
                    "health": 12.5,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "in_water": False,
                    "in_lava": poll_state["in_lava"],
                    "on_ground": poll_state["on_ground"],
                    "dimension": "minecraft:the_nether",
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                seq = poll_state["hurt_seq"]
                rows = [{"seq": seq, "type": "hurt", "health": 12.5, "source": "lava"}] if seq > since else []
                return {"events": rows, "last_seq": seq}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                p = tuple(int(x) for x in pl.get("pos", [0, 0, 0]))
                if p[0] <= 704 and p[1] <= 63:
                    return {"name": "netherrack"}
                if p[0] >= 706 and p[1] in (63, 64):
                    return {"name": "lava"}
                if p[1] <= 62:
                    return {"name": "netherrack"}
                return {"name": "air"}
            if action == "control" and pl.get("forward"):
                control_calls.append(pl)
                poll_state["pos"] = [703.5, 64.0, 640.5]
            return {"ok": True}

    guard = GenerationGuard(_LavaHurtPollBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_LavaHurtPollSkills())
    inc_lava = guard.poll()
    assert inc_lava is not None
    assert "in_lava" in inc_lava["reasons"]
    assert any(a["action"] == "escape_lava" and a["ok"] for a in inc_lava["actions"])
    assert bunker_calls == []
    assert flee_calls == []
    assert control_calls


def test_r20_poll_and_boundary_skip_eat_in_ranged_mob_fire(monkeypatch):
    """Reproduces r20_short ds_ds_3_enchant_anvil: never drop shield to eat in open ranged-mob fire (6..16m)."""
    clock = [7000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    use_items: list[dict] = []
    posture_calls: list[dict] = []
    state = {"hp": 5.3, "food": 19, "blocking": True, "dist": 6.2, "inside_shaft": False}

    class _EnclosedSkills:
        class _Eng:
            nodes = {(0, 64, 0): True}

            def is_inside_shaft(self):
                return state["inside_shaft"]

        mining_dfs = _Eng()

    class _SkeletonRangeBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": state["hp"],
                    "food": state["food"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": state["blocking"],
                    "nearest_hostile": {"id": 612494, "name": "skeleton", "pos": [0.5 + state["dist"], 64.0, 0.5], "dist": state["dist"]},
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 10}, {"name": "shield", "count": 1}]}
            if action == "posture":
                posture_calls.append(pl)
            if action == "use_item":
                use_items.append(pl)
            return {"ok": True}

    guard = GenerationGuard(_SkeletonRangeBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_EnclosedSkills())
    guard._mark_ranged_engagement(clock[0], attacker={"id": 612494, "name": "skeleton"})
    assert guard.poll() is None
    assert guard.stabilize_after_tools(["ok"]) is None
    assert posture_calls == []
    assert use_items == []

    # Safe inside enclosed shaft (or once skeleton moves >16m), eating succeeds
    state["inside_shaft"] = True
    clock[0] += 0.5
    inc_shaft = guard.poll()
    assert inc_shaft is not None
    assert [a["action"] for a in inc_shaft["actions"]] == ["eat"]
    assert use_items == [{"item": "cooked_beef"}]


def test_r21_preempted_lava_death_reports_respawned_not_escaped():
    """Reproduces r21_short hy4_hy_8_nether: lava preemption ending in death/respawn must NOT claim '已中断原工具并脱困'."""
    dead_lava_state = {"calls": 0}

    class _LavaDiePreemptBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                dead_lava_state["calls"] += 1
                if dead_lava_state["calls"] <= 2:
                    return {
                        "pos": [778.9, 29.8, -824.5],
                        "health": 2.0,
                        "alive": True,
                        "ready": True,
                        "in_lava": True,
                        "on_ground": False,
                        "dimension": "minecraft:the_nether",
                    }
                return {
                    "pos": [7828.5, 64.1, 2818.5],
                    "health": 20.0,
                    "alive": True,
                    "ready": True,
                    "in_lava": False,
                    "on_ground": True,
                    "dimension": "minecraft:overworld",
                }
            if action == "inventory":
                return {"items": []}
            if action in {"find_blocks", "get_entities"}:
                return {"blocks": [], "entities": []}
            if action == "get_block":
                return {"name": "lava"}
            return {"ok": True}

    guard = GenerationGuard(_LavaDiePreemptBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    receipt = guard.handle_preemption(
        {"source": "boundary_threat", "primary_type": "lava", "distance": 0.0},
        interrupted_name="mc_skill_mine_dfs",
    )
    assert receipt["reflex_mode"] == "lava_escape"
    assert "并脱困" not in receipt["message"]
    assert "死亡并重生" in receipt["message"]

    # Also catch close-to-spawn Nether lava death (<64m displacement) where ready=True & health=20 on respawn
    close_state = {"calls": 0}

    class _CloseSpawnLavaDieBot:
        _last_event_seq = 0

        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                close_state["calls"] += 1
                if close_state["calls"] <= 2:
                    return {
                        "pos": [712.0, 58.0, 635.0],
                        "health": 11.0,
                        "alive": True,
                        "ready": True,
                        "in_lava": True,
                        "on_ground": False,
                        "dimension": "minecraft:the_nether",
                        "last_death": {"pos": [704.5, 64.0, 640.5], "t": 1000},
                    }
                return {
                    "pos": [704.5, 64.0, 640.5],
                    "health": 20.0,
                    "alive": True,
                    "ready": True,
                    "in_lava": False,
                    "on_ground": True,
                    "dimension": "minecraft:the_nether",
                    "last_death": {"pos": [712.0, 58.0, 635.0], "t": 2000},
                }
            if action == "inventory":
                return {"items": []}
            if action in {"find_blocks", "get_entities"}:
                return {"blocks": [], "entities": []}
            if action == "get_block":
                return {"name": "lava"}
            return {"ok": True}

    from mc_agent.skills.combat_env import CombatEnv

    close_state["calls"] = 0
    out_close = CombatEnv(_CloseSpawnLavaDieBot()).escape_lava()
    assert out_close["ok"] is False
    assert out_close.get("respawned") is True

    close_state["calls"] = 0
    g_close = GenerationGuard(_CloseSpawnLavaDieBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    rec_close = g_close.handle_preemption(
        {"source": "boundary_threat", "primary_type": "lava", "distance": 0.0},
        interrupted_name="mc_skill_unstick",
    )
    assert rec_close["reflex_mode"] == "lava_escape"
    assert "并脱困" not in rec_close["message"]
    assert "死亡并重生" in rec_close["message"]


def test_r21_water_surface_abort_latches_shield_and_post_combat_eat_skips_ranged():
    """Reproduces r21_short hy4_hy_3_midgame: water_surface_abort near drowned latches shield & skips ranged/latched post-combat eat."""
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    water_calls: list[tuple[str, dict]] = []
    water_state = {"in_water": False, "submerged": False, "oxygen": 100, "hp": 15.0}

    class _DrownedWaterSkills:
        def escape_water(self):
            water_state["in_water"] = False
            water_state["submerged"] = False
            water_state["hp"] = 3.4
            return {"ok": True, "in_water": False}

    class _DrownedAbortBot:
        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            water_calls.append((action, pl))
            if action == "status":
                return {
                    "pos": [0.5, 63.0, 0.5],
                    "health": water_state["hp"],
                    "food": 17,
                    "oxygen": water_state["oxygen"],
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": water_state["in_water"],
                    "submerged": water_state["submerged"],
                    "in_lava": False,
                    "nearest_hostile": {"id": 665559, "name": "drowned", "pos": [1.5, 63.0, 0.5], "dist": 1.1},
                }
            if action == "attack":
                # Match hy4_hy_3 seq 199: plunging after drowned pulls bot underwater mid counter_kill
                water_state["in_water"] = True
                water_state["submerged"] = True
                return {"ok": True, "hits": 1, "killed": False}
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 4}],
                    "offhand": {"name": "shield", "count": 1},
                }
            if action == "get_entities":
                return {"entities": [{"id": 665559, "name": "drowned", "pos": [1.5, 63.0, 0.5], "dist": 1.1}]}
            if action == "get_block":
                p = pl.get("pos", [0, 63, 0])
                return {"name": "stone" if p[1] < 63 else "air"}
            return {"ok": True}

    out_w = CombatControllerSlot().handle(
        CombatEnv(_DrownedAbortBot(), skills=_DrownedWaterSkills()),
        {"source": "pre_hurt_melee", "primary_entity_id": 665559, "primary_type": "drowned", "distance": 2.3},
    )
    assert out_w["mode"] == "water_surface_abort"
    assert out_w["latched_guard"] is True
    assert [c for c in water_calls if c[0] == "posture"][-1] == (
        "posture",
        {"shield": True, "sneak": False, "jump": False},
    )
    assert all(act != "use_item" for act, _ in water_calls)

    # Also verify SubcorticalInstinctController post_combat_eat skips when ranged skeleton is at 9.0m
    ranged_post_calls: list[str] = []

    class _PostRangedSkipBot:
        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            ranged_post_calls.append(action)
            if action == "status":
                return {
                    "pos": [0.5, 64.0, 0.5],
                    "health": 13.0,
                    "food": 14,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "nearest_hostile": {"id": 88, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0},
                }
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 4}]}
            if action == "get_entities":
                return {"entities": [{"id": 88, "name": "skeleton", "pos": [9.5, 64.0, 0.5], "dist": 9.0}]}
            if action == "get_block":
                return {"name": "stone" if (body or {}).get("pos", [0, 64, 0])[1] < 64 else "air"}
            return {"ok": True}

    CombatControllerSlot().handle(
        CombatEnv(_PostRangedSkipBot()),
        {"source": "pre_hurt_ranged", "primary_entity_id": 88, "primary_type": "skeleton", "distance": 9.0},
    )
    assert "use_item" not in ranged_post_calls


def test_r22_shore_query_bypasses_lakebed_and_failed_shore_backs_off_cooldown(monkeypatch):
    """Reproduces r22_short hy4_hy_7_village:
    1) Surface bank query finds dry grass_block/dirt_path shore even when oceanbed sand/dirt fills generic limit=36.
    2) When escape_to_shore fails inside 30s water_avoid_cooldown without new hurt, poll backs off 8.0s instead of spamming cancels every 5s.
    """
    from mc_agent.skills.combat_env import CombatEnv

    # 1) Surface bank query finds dry grass_block shore when generic SHORE_LAND_BLOCKS query returns underwater riverbed sand
    move_goals: list[dict] = []

    class _RiverbedBot:
        def status(self):
            return {
                "pos": [0.5, 62.2, 0.5],
                "health": 16.0,
                "food": 20,
                "alive": True,
                "ready": True,
                "in_water": True,
                "submerged": False,
                "oxygen": 300,
                "yaw": 0.0,
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                return self.status()
            if action == "find_blocks":
                types = set(pl.get("types") or [])
                if "sand" in types or "dirt" in types:
                    # Generic riverbed blocks at y=58 (filtered out by min_y=61)
                    return {"blocks": [{"name": "sand", "pos": [i, 58, 0]} for i in range(40)]}
                # Surface bank query finds grass_block at [6, 62, 0] -> walkable [6.5, 63.0, 0.5]
                return {"blocks": [{"name": "grass_block", "pos": [6, 62, 0]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                move_goals.append(pl)
                return {"ok": True}
            return {"ok": True}

    env_river = CombatEnv(_RiverbedBot())
    # Bypass final in_water flip only to inspect selected shore_target and move_to range
    out_river = env_river.escape_deep_water_to_shore(keep_shield=True)
    assert out_river["shore_target"] == [6.5, 63.0, 0.5]
    assert move_goals and move_goals[0]["range"] <= 0.6

    # 2) Failed escape_to_shore during 30s cooldown backs off 8.0s when no new hurt / drowning occurs
    clock = [8000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    class _WideWaterBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 62.2, 0.5],
                    "health": 14.0,
                    "food": 20,
                    "oxygen": 280,
                    "alive": True,
                    "ready": True,
                    "in_water": True,
                    "submerged": False,
                    "in_lava": False,
                    "blocking": False,
                }
            if action == "events":
                return {"events": [], "last_seq": 0}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            return {"ok": True}

    guard = GenerationGuard(_WideWaterBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._no_water_until = clock[0] + 30.0
    inc_first = guard.poll()
    assert inc_first is not None
    assert "water_avoid_cooldown" in inc_first["reasons"]
    assert guard._last_water_escape_failed is True

    # +5.0s later (previously spammed cancel every ~5s) -> still in 8.0s backoff!
    clock[0] += 5.0
    assert guard.poll() is None

    # +3.5s more (total 8.5s >= 8.0s) -> resumes retry cleanly
    clock[0] += 3.5
    inc_retry = guard.poll()
    assert inc_retry is not None
    assert "water_avoid_cooldown" in inc_retry["reasons"]


def test_r22_fireball_skips_dig_cover_and_same_spot_arrow_escalates_to_flee(monkeypatch):
    """Reproduces r22_short glm_1 (20s dig_cover in fireball) & hy4_10 (3x repeated dig_cover at same exposed spot)."""
    clock = [9000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])

    dig_cover_calls: list[str] = []
    flee_calls: list[dict] = []
    hurt_state = {"seq": 1, "src": "fireball", "hp": 16.0, "mob": "blaze"}

    class _RangedFallbackSkills:
        mining_dfs = None

        def dig_cover_pocket(self):
            dig_cover_calls.append(hurt_state["src"])
            return {"ok": True, "mode": "cover_pocket", "pos_after": [6346.3, 54.0, -9022.3]}

        def defend_or_flee(self, **kwargs):
            flee_calls.append(kwargs)
            return {"ok": True, "mode": "flee", "moved_dist": 6.0}

    class _RangedFallbackBot:
        _last_event_seq = 0

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [6346.3, 54.0, -9022.3],
                    "health": hurt_state["hp"],
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": False,
                    "nearest_hostile": {"id": 91, "name": hurt_state["mob"], "pos": [6350.0, 54.0, -9022.3], "dist": 3.7},
                }
            if action == "events":
                since = int((kwargs.get("params") or {}).get("since", 0))
                seq = hurt_state["seq"]
                rows = [{"seq": seq, "type": "hurt", "health": hurt_state["hp"], "source": hurt_state["src"]}] if seq > since else []
                return {"events": rows, "last_seq": seq}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 16}], "offhand": {"name": "shield"}}
            if action in {"find_blocks", "get_entities"}:
                return {"blocks": [], "entities": []}
            if action == "get_block":
                return {"name": "stone" if (body or {}).get("pos", [0, 54, 0])[1] < 54 else "air"}
            return {"ok": True}

    guard = GenerationGuard(_RangedFallbackBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_RangedFallbackSkills())
    # 1) Blaze fireball must NEVER spend 20s digging cover pocket — directly execute combat_or_flee
    inc_fb = guard.poll()
    assert inc_fb is not None
    assert [a["action"] for a in inc_fb["actions"] if a["action"] in {"dig_cover", "combat_or_flee"}] == ["combat_or_flee"]
    assert dig_cover_calls == []

    # 2) Arrow hit at exposed spot digs cover once; a subsequent arrow hit 6.5s later at the SAME block escalates to combat_or_flee!
    hurt_state.update({"seq": 2, "src": "arrow", "hp": 14.0, "mob": "skeleton"})
    clock[0] += 4.0
    inc_a1 = guard.poll()
    assert inc_a1 is not None
    assert "dig_cover" in [a["action"] for a in inc_a1["actions"]]
    assert len(dig_cover_calls) == 1

    hurt_state.update({"seq": 3, "src": "arrow", "hp": 6.0, "mob": "skeleton"})
    clock[0] += 6.5
    inc_a2 = guard.poll()
    assert inc_a2 is not None
    assert len(dig_cover_calls) == 1  # NOT re-dug on the exact same broken block!
    assert any(a["action"] == "combat_or_flee" for a in inc_a2["actions"])


@pytest.mark.parametrize("scenario", ["lava_rim", "cliff", "airborne", "sprint_lava", "path_airborne", "close_respawn"])
def test_bug026_flee_probes_player_width_and_rechecks_each_landing(scenario):
    import math

    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    calls = []
    status = {
        "pos": [0.5, 64, 0.83], "alive": True, "ready": True, "on_ground": scenario != "airborne",
        "in_lava": False, "dimension": "the_nether", "last_death": {"t": 10},
    }

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                name = "air" if y >= 64 else "netherrack"
                if scenario == "lava_rim" and x >= 1 and z >= 1 and y == 63:
                    name = "lava"
                if scenario == "cliff" and (x != 0 or z not in {0, 1}) and y > 60:
                    name = "air"
                return {"name": name}
            calls.append((action, body))
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                ux, uz = yaw_to_unit_xz(status["yaw"])
                status["pos"] = [0.5 + 2.26 * ux, 64, 0.83 + 2.26 * uz]
                if scenario == "sprint_lava":
                    status.update(in_lava=True, on_ground=False)
                if scenario == "close_respawn":
                    status["last_death"] = {"t": 20}
            if action == "move_to" and scenario == "path_airborne":
                assert body["y"] == 64
                status.update(pos=[16.82, 50, 0.83], on_ground=False)
            return {"ok": True}

    result = CombatEnv(RetreatBot()).flee([-3, 64, 0.83], allow_shaft_seal=False)
    if scenario == "lava_rim":
        assert result["ok"]
        assert status["yaw"] != pytest.approx(-math.pi / 2)
    elif scenario in {"cliff", "airborne"}:
        assert not result["ok"]
        assert not calls
    elif scenario == "close_respawn":
        assert not result["ok"]
        assert result["respawned"] is True
        assert [action for action, _ in calls] == ["look", "control"]
    else:
        assert not result["ok"]
        assert result["reason"] == "unsafe_landing"
        assert [action for action, _ in calls] == (
            ["look", "control"] if scenario == "sprint_lava" else ["look", "control", "move_to"]
        )


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


def test_r23_recent_arrow_hit_keeps_shield_up_when_nearest_mob_is_melee(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    state = {
        "health": 14, "food": 20, "blocking": True, "pos": [0.5, 81, 0.5],
        "nearest_hostile": {"name": "zombie", "dist": 3.0},
        "alive": True, "ready": True, "on_ground": True,
    }
    fresh = [{"seq": 1, "type": "hurt", "source": "mob_attack"}]
    calls = []

    class DuringRetreat:
        def defend_or_flee(self, **kwargs):
            state.update(health=3.9, food=17, nearest_hostile={"name": "zombie", "dist": 10.3})
            fresh.append({"seq": 2, "type": "hurt", "source": "arrow"})
            return {"ok": True, "mode": "flee"}

    class VillageBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(state)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [e for e in fresh if e["seq"] > since], "last_seq": fresh[-1]["seq"]}
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}, {"name": "iron_sword", "count": 1}]}
            calls.append((action, body))
            if action == "use_item":
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    guard = GenerationGuard(VillageBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=DuringRetreat())
    assert guard.poll()["event_seqs"] == [1, 2]

    clock[0] += 6.0
    assert guard.poll() is None
    assert calls == []

    clock[0] += 3.0
    incident = guard.poll()
    assert calls == [("posture", {"shield": False}), ("use_item", {"item": "bread"})]
    assert incident["requires_replan"] is True
    assert "fresh_hurt" in incident["reasons"]


@pytest.mark.parametrize("blocking", [False, True])
def test_r24_airborne_arrow_hit_faces_shooter_without_jumping_or_blind_flee(blocking):
    calls = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-7807.3, 102.2, -2813.7], "health": 5.9, "food": 16,
                    "alive": True, "ready": True, "on_ground": False, "blocking": blocking,
                    "dimension": "overworld", "nearest_hostile": {"id": 42, "name": "skeleton", "dist": 7.8},
                }
            if action == "events":
                since = kwargs["params"]["since"]
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": {"id": 42}}
                return {"events": [hurt] if since < 1 else [], "last_seq": 1}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            calls.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    expected = [("look", {"entity_id": 42})]
    if not blocking:
        expected.append(("posture", {"shield": True, "jump": False, "sneak": False}))
    assert calls == expected
    retreat = incident["actions"][-1]
    assert retreat["action"] == "combat_or_flee"
    assert retreat["ok"] is False
    assert retreat["result"]["reason"] == "unsafe_landing"


@pytest.mark.parametrize("landing", ["landed", "airborne", "respawned"])
def test_unshielded_arrow_knockback_retries_cover_if_landed_with_no_retreat(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {
        "pos": [-7918.3, 44.6, 2790.6], "health": 7.3, "food": 20,
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 1},
        "nearest_hostile": {"name": "skeleton", "dist": 7.0},
    }
    cover_calls = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= 1000.15 and landing != "airborne":
                    status.update(pos=[-7918.3, 44, 2790.6], on_ground=True)
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow"}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 43}]}
            if action == "get_block":
                return {"name": "air"}
            raise AssertionError(action)

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def no_route(*args):
        assert status["on_ground"] is True
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        return None

    def dig_cover(skills):
        assert status["on_ground"] is True
        assert status["last_death"] == {"t": 1}
        cover_calls.append(status["pos"])
        return {"ok": True, "mode": "cover_pocket"}

    monkeypatch.setattr(guard.combat_env, "probe_retreat", no_route)
    monkeypatch.setattr(guard.combat_env, "dig_cover_pocket", dig_cover)
    guard.poll()
    assert cover_calls == ([[-7918.3, 44, 2790.6]] if landing == "landed" else [])


@pytest.mark.parametrize("respawned", [False, True])
def test_low_health_shield_faces_the_shooter_again_after_retreat(respawned):
    status = {
        "pos": [-11014.4, 76, -11004.5], "yaw": 0.1, "health": 1.0, "food": 17, "ready": True,
        "alive": True, "on_ground": True, "blocking": True, "last_death": {"t": 10},
        "nearest_hostile": {"id": 42, "name": "skeleton", "dist": 8.0},
    }
    movements = []

    class ShieldedBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": {"id": 42, "name": "skeleton"}}
                return {"events": [hurt] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield", "count": 1}}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 76 else "air"}
            if action == "look":
                if "entity_id" in body:
                    assert body["entity_id"] == 42
                    status["yaw"] = -0.267
                    movements.append("face_shooter")
                else:
                    status["yaw"] = body["yaw"]
                    movements.append("turn_to_retreat")
            if action == "control" and body.get("forward"):
                movements.append("sprint")
                status["pos"] = [-11014.3, 76, -11006.8]
                if respawned:
                    status.update(health=20, last_death={"t": 20})
            if action == "move_to":
                movements.append("move_to")
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(ShieldedBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    names = [action["action"] for action in incident["actions"]]
    assert movements[:3] == ["face_shooter", "turn_to_retreat", "sprint"]
    if respawned:
        assert "face_attacker_after_retreat" not in names
        assert movements[-1] == "sprint"
    else:
        assert names[-1] == "face_attacker_after_retreat"
        assert movements[-1] == "face_shooter"
        assert status["yaw"] == -0.267


@pytest.mark.parametrize("landing", ["grounded", "still_airborne", "respawned"])
@pytest.mark.parametrize("entry", ["poll_ranged", "poll_melee", "subcortical_ranged", "subcortical_melee"])
def test_knockback_waits_briefly_for_ground_before_retreat(monkeypatch, landing, entry):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {
        "pos": [-5191.8, 69.6, -7616.0], "health": 7.7, "food": 20, "ready": True, "alive": True,
        "on_ground": False, "dimension": "overworld", "last_death": {"t": 10},
        "nearest_hostile": {"id": 42, "name": "skeleton", "dist": 9.0, "pos": [-5182.5, 70.0, -7616.2]},
    }
    melee = entry.endswith("_melee")
    hurt_source = "mob_attack" if melee else "arrow"
    if melee:
        status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 0.6, "pos": [-5191.2, 69.6, -7616.0]}
    moves = []

    class KnockbackBot:
        def status(self):
            return self.call("status")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= 1000.15:
                    if landing == "grounded":
                        status.update(pos=[-5192.2, 69.0, -7616.0], on_ground=True)
                    elif landing == "respawned":
                        status.update(pos=[-5184.5, 70.1, -7614.5], health=20, on_ground=True, last_death={"t": 20})
                return dict(status)
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": hurt_source}] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 69 else "air"}
            if action == "move_to" or (action == "control" and body.get("forward")):
                assert status["on_ground"] is True
                moves.append(action)
            return {"ok": True}

    bot = KnockbackBot()
    if entry.startswith("poll_"):
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        incident = guard.poll()
        retreat = next(a["result"] for a in incident["actions"] if a["action"] == "combat_or_flee")
    else:
        outcome = CombatControllerSlot().handle(
            CombatEnv(bot), {
                "source": "pre_hurt_melee" if melee else "pre_hurt_ranged",
                "primary_type": "zombie" if melee else "skeleton",
                "primary_entity_id": 42,
            },
        )
        retreat = next(a for a in outcome["actions"] if a["action"] == "flee")
    assert bool(moves) is (landing == "grounded")
    assert clock[0] <= 1000.45
    if landing == "respawned":
        assert retreat["respawned"] is True


@pytest.mark.parametrize("landing", ["grounded", "still_airborne", "water", "respawned"])
def test_unarmed_retreat_confirms_landing_after_short_pulse(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    threat = {"id": 42, "name": "spider", "dist": 2.0, "pos": [2.5, 74, 0.5]}
    status = {
        "pos": [0.5, 74, 0.5], "health": 14, "ready": True, "alive": True, "on_ground": True,
        "is_day": True, "nearest_hostile": threat, "last_death": {"t": 1},
    }
    mutations = []

    class RetreatBot:
        def status(self):
            if clock[0] >= 1000.15 and status.get("on_ground") is False:
                if landing == "grounded":
                    status.update(pos=[-1.8, 74, 0.5], on_ground=True)
                elif landing == "water":
                    status.update(in_water=True)
                elif landing == "respawned":
                    status.update(pos=[0.5, 74, 4.5], on_ground=True, last_death={"t": 2})
            return dict(status)

        def inventory(self):
            return {"items": []}

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_entities":
                return {"entities": [threat]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 74 else "air"}
            if action == "control" and body.get("forward"):
                assert status["on_ground"] is True
                mutations.append(action)
                status.update(pos=[-1.66, 74.6, 0.5], on_ground=False)
            if action == "move_to":
                assert status["on_ground"] is True
                mutations.append(action)
                status.update(pos=[body["x"], body["y"], body["z"]])
            return {"ok": True}

    env = CombatEnv(RetreatBot())
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "spider"}
    )
    retreat = next(a for a in outcome["actions"] if a["action"] == "flee")
    assert clock[0] <= 1000.45
    assert retreat["ok"] is (landing == "grounded")
    assert mutations == (["control", "move_to"] if landing == "grounded" else ["control"])
    assert bool(retreat.get("respawned")) is (landing == "respawned")


@pytest.mark.parametrize(
    "outcome", ["same_life", "respawned_nearby", "time_budget", "retreat_into_lava", "failed_retreat_into_lava"],
)
def test_guardian_bounds_melee_macro_and_rejects_retreat_after_death(monkeypatch, outcome):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [11032.5, 0.6, -11040.3], "health": 14.0, "food": 20,
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 10},
    }
    moves = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack"}] if since < 1 else []}
            if action == "get_entities":
                return {"entities": []}
            if action == "inventory":
                return {"items": []}
            moves.append((action, kwargs["timeout_s"]))
            if action == "control":
                if outcome == "time_budget":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("retreat reached its deadline")
                status.update(pos=[11032.5, 6.2, -11035.2], on_ground=True)
                if outcome == "respawned_nearby":
                    status.update(health=20, last_death={"t": 20})
                if outcome in {"retreat_into_lava", "failed_retreat_into_lava"}:
                    status.update(health=0.33, in_lava=True, on_ground=False)
            return {"ok": True}

    def leave_cave(scoped, **kwargs):
        scoped.bot.call("control", {"forward": True, "ms": 1200, "stop_on": ["death"]}, timeout_s=40.0)
        return {"ok": outcome != "failed_retreat_into_lava", "mode": "flee", "pos_after": status["pos"], "respawned": False}

    monkeypatch.setattr(MacroSkills, "_flee_hostiles", leave_cave)
    bot = CaveBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=skills)

    def escape_lava():
        assert status["in_lava"] is True
        status.update(in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    incident = guard.poll()

    retreat, = incident["actions"]
    assert retreat["action"] == "combat_or_flee"
    assert retreat["ok"] is (outcome == "same_life")
    assert skills.bot is bot
    assert moves[0] == ("control", 8.0)
    if outcome in {"retreat_into_lava", "failed_retreat_into_lava"}:
        assert retreat["result"]["reason"] == "retreat_into_lava"
        assert retreat["result"]["lava_recovery"]["ok"] is True
        assert incident["status"]["in_lava"] is False
    if outcome == "respawned_nearby":
        assert retreat["result"]["respawned"] is True
    elif outcome == "time_budget":
        assert retreat["result"] == {"ok": False, "reason": "time_budget", "partial": True}
        assert moves[1:] == [("stop", 1.5)]


@pytest.mark.parametrize("after_first", ["close", "clear", "respawned"])
def test_guardian_rechecks_a_creeper_after_a_short_unshielded_retreat(after_first):
    pos = [0.5, 64.0, 0.5]
    creeper_pos = [0.5, 64.0, 4.7]
    yaw = 0.0
    last_death = {"t": 10}
    pulses = []

    class CreekBot:
        def call(self, action, body=None, **kwargs):
            nonlocal yaw, last_death
            creeper = {"id": 66, "name": "creeper", "pos": list(creeper_pos), "dist": math.dist(pos, creeper_pos)}
            if action == "status":
                return {
                    "pos": list(pos), "health": 20, "food": 20, "on_ground": True, "yaw": yaw,
                    "ready": True, "alive": True, "last_death": last_death, "nearest_hostile": creeper,
                }
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                return {"entities": [creeper]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                supported = (-1 <= x <= 1 and -4 <= z <= 5) or (-5 <= x <= 1 and -3 <= z <= -1)
                return {"name": "stone" if y == 63 and supported else "air"}
            if action == "look":
                yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                assert body["jump"] is False and body["stop_on"] == ["death"]
                pos[0] -= math.sin(yaw) * 2.3
                pos[2] -= math.cos(yaw) * 2.3
                pulses.append(list(pos))
                if len(pulses) == 1 and after_first == "close":
                    creeper_pos[:] = [5.1, 64.0, -1.8]
                elif after_first == "respawned":
                    last_death = {"t": 20}
            if action in {"attack", "move_to"}:
                pytest.fail(f"Unexpected {action} during short retreat")
            return {"ok": True}

    guard = GenerationGuard(CreekBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    reaction, = incident["actions"]
    actions = reaction["result"]["actions"]
    assert actions[0]["action"] == "retreat_from_creeper"
    assert actions[0]["ok"] is (after_first == "clear")
    assert pulses[0] == pytest.approx([0.5, 64.0, -1.8])
    if after_first == "close":
        assert actions[0]["creeper_distance"] == pytest.approx(4.6)
        assert actions[1]["action"] == "creeper_followup_retreat" and actions[1]["ok"] is True
        assert pulses[1] == pytest.approx([-1.8, 64.0, -1.8])
    else:
        assert len(pulses) == len(actions) == 1


def test_missing_offhand_pickaxe_for_bunker_still_allows_boundary_retreat():
    pos = [5199.5, 84, 7615.5]
    shooter = {"id": 42, "name": "skeleton", "dist": 8.0, "pos": [5207.5, 84, 7615.5]}
    moves = []

    class RidgeBot:
        def status(self):
            return {
                "pos": list(pos), "health": 3.5, "food": 20, "ready": True, "alive": True,
                "on_ground": True, "nearest_hostile": shooter,
            }

        def inventory(self):
            return {
                "items": [{"name": "stone_sword", "count": 1}, {"name": "stone_pickaxe", "count": 1},
                          {"name": "cobblestone", "count": 16}],
                "offhand": {"name": "iron_pickaxe", "count": 1}, "held": {"name": "stone_sword"},
            }

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return self.inventory()
            if action == "events":
                return {"events": []}
            if action == "get_entities":
                return {"entities": [shooter]}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 84 else "air"}
            if action == "equip" and body["item"] == "iron_pickaxe":
                raise ApiError("missing_item", "no iron_pickaxe in inventory")
            if action == "control" and body.get("forward"):
                assert body["jump"] is False
                pos[0] -= 2.3
                moves.append(list(pos))
            assert action not in {"dig", "attack"}
            return {"ok": True}

    events = []
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=MacroSkills(bot))
    guard.stabilize_after_tools()

    reflex, = [event for event in events if event["type"] == "guardian.action.completed"]
    assert reflex["action"] == "boundary_subcortical_combat"
    bunker, retreat = reflex["result"]["actions"]
    assert bunker == {"action": "emergency_bunker", "ok": False, "code": "missing_item"}
    assert retreat["action"] == "flee" and retreat["ok"] is True
    assert moves == [[5197.2, 84, 7615.5]]


@pytest.mark.parametrize(("health", "fresh_hurt", "expect_retreat"), [(3.3, True, True), (8.0, True, False), (3.3, False, False)])
def test_guardian_handles_fresh_critical_combat_hurt_inside_reaction_cooldown(monkeypatch, health, fresh_hurt, expect_retreat):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    retreats = []

    class RangedBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 63, 0.5], "health": health, "food": 20, "ready": True, "alive": True,
                    "on_ground": True, "nearest_hostile": {"id": 44, "name": "pillager", "dist": 16.0},
                }
            if action == "events":
                if fresh_hurt and kwargs["params"]["since"] < 1:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}]}
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            return {"ok": True}

    def retreat(*args, **kwargs):
        retreats.append(True)
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(RangedBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_reaction = 999.0
    monkeypatch.setattr(guard.combat_env, "flee", retreat)

    incident = guard.poll()

    assert bool(retreats) is expect_retreat
    assert (incident is not None) is expect_retreat


def test_ranged_retreat_without_shield_resolves_the_shooter_from_live_entities(monkeypatch):
    retreat_targets = []
    shooter = {"id": 44, "name": "pillager", "pos": [-10936.8, 63.0, -10992.5]}

    class RiverbankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-10934.7, 63.2, -10993.8], "health": 16.0, "food": 20, "ready": True, "alive": True,
                    "on_ground": False, "nearest_hostile": {"id": 44, "name": "pillager", "dist": 2.6},
                }
            if action == "events":
                event = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": {"id": 44, "name": "pillager"}}
                return {"events": [event] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                return {"entities": [{"id": 47, "name": "pillager", "pos": [-10931.0, 63.0, -10992.5]}, shooter]}
            return {"ok": True}

    def retreat(threat_pos, **kwargs):
        retreat_targets.append(threat_pos)
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(RiverbankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    assert guard.poll() is not None
    assert retreat_targets == [shooter["pos"]]


@pytest.mark.parametrize(
    "entry, scenario",
    [
        ("boundary", "surface"),
        ("boundary", "close_respawn"),
        ("boundary", "macro_respawn"),
        ("boundary", "macro_close_respawn"),
        ("boundary", "negative_oxygen"),
        ("poll", "surface"),
        ("poll", "oxygen_only"),
        ("poll", "negative_oxygen"),
        ("preemption", "oxygen_only"),
        ("preemption", "negative_oxygen"),
    ],
)
def test_guardian_drowning_surfaces_first_and_checks_respawn(entry, scenario, monkeypatch):
    events = []
    calls = []
    macro = scenario.startswith("macro_")
    state = {
        "pos": [9971.3, 47.2, 2897.3], "health": 18, "oxygen": 0,
        "in_water": True, "submerged": True, "on_ground": False, "jumping": True,
        "ready": True, "alive": True, "last_death": {"t": 10},
    }

    class WaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(state)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "drown"}
                return {"events": [] if macro or scenario in {"oxygen_only", "negative_oxygen"} else [hurt]}
            if action == "inventory":
                return {"items": []}
            calls.append((action, body))
            if action == "control":
                assert body["jump"] is True and body["forward"] is False and body["sprint"] is False
                state.update(in_water=False, submerged=False, on_ground=True, pos=[9971.3, 49, 2897.3])
                if scenario == "close_respawn":
                    state.update(health=20, last_death={"t": 20})
            return {"ok": True}

    class EscapeMacro:
        def escape_water(self):
            assert macro
            pos = [9974.5, 62.1, 2978.5] if scenario == "macro_respawn" else [9971.3, 49, 2897.3]
            state.update(in_water=False, pos=pos, last_death={"t": 20})
            return {"ok": True, "in_water": False, "pos_after": state["pos"]}

    if macro:
        state["oxygen"] = 100
    elif scenario == "oxygen_only":
        state["submerged"] = False
    elif scenario == "negative_oxygen":
        state["oxygen"] = -1
    guard = GenerationGuard(WaterBot(), EventSink(events.append), checkpoint=lambda: None, skills=EscapeMacro())
    if entry == "poll":
        monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
        guard.last_reaction = 999.5
        assert guard.poll() is not None
    elif entry == "preemption":
        receipt = guard.handle_preemption({"source": "tool_interrupted_hurt"}, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface"
        assert receipt["status_after"]["in_water"] is False
    else:
        note = guard.stabilize_after_tools()
    actions = [e for e in events if e["type"] == "guardian.action.completed"]
    assert len(actions) == 1
    if macro:
        assert actions[0]["action"] == "boundary_escape_water"
        assert actions[0]["result"]["respawned"] is True
        assert actions[0]["ok"] is False
        assert note is None
        assert calls == []
    else:
        assert actions[0]["action"] == ("boundary_escape_to_shore" if entry == "boundary" else "escape_to_shore")
        assert [action for action, _ in calls] == ["posture", "control"]
        assert actions[0]["ok"] is (scenario != "close_respawn")
        assert actions[0]["result"]["mode"] == ("respawned" if scenario == "close_respawn" else "surfaced")


@pytest.mark.parametrize("safe_sidestep", [True, False])
def test_bug069_retreat_can_sidestep_on_ground_when_long_routes_are_unsafe(safe_sidestep):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    status = {"pos": [0.5, 64.0, 0.5], "alive": True, "ready": True, "on_ground": True}
    controls = []

    class NarrowBankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                far_edge = 2 if safe_sidestep else 1
                return {"name": "stone" if x == 0 and y == 63 and 0 <= z <= far_edge else "air"}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                controls.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                status["pos"] = [0.5 + 0.7 * dx, 64.0, 0.5 + 0.7 * dz]
            return {"ok": True}

    result = CombatEnv(NarrowBankBot()).flee([4.5, 64, 0.5], allow_shaft_seal=False)

    assert result["ok"] is safe_sidestep
    if safe_sidestep:
        control, = controls
        assert result["pos_after"] == pytest.approx([0.5, 64.0, 1.2])
        assert control["forward"] and not control["sprint"] and not control["jump"]
        assert control["ms"] < 350
    else:
        assert controls == []


@pytest.mark.parametrize("room_for_short_exit", [True, False])
def test_flee_uses_verified_short_exit_when_full_followup_hits_drop(room_for_short_exit):
    from mc_agent.skills.combat_env import CombatEnv

    status = {"pos": [0.5, 64, 0.5], "alive": True, "ready": True, "on_ground": True}
    followups = []
    floor_edge = 6 if room_for_short_exit else 4

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, _ = body["pos"]
                return {"name": "stone" if y == 63 and x <= floor_edge else "air"}
            if action == "control":
                status["pos"] = [2.6, 64, 0.5]
            if action == "move_to":
                followups.append(body)
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    result = CombatEnv(RetreatBot()).flee([-1.8, 64, 0.5], distance=13.0, allow_shaft_seal=False)

    if room_for_short_exit:
        move, = followups
        assert (move["x"], move["y"], move["z"]) == pytest.approx((6.1, 64, 0.5))
        assert result["ok"] is True and result["moved_dist"] == pytest.approx(5.6)
    else:
        assert followups == []
        assert result["moved_dist"] == pytest.approx(2.1)


@pytest.mark.parametrize(
    "dimension, health",
    [
        ("the_nether", 16.5),
        ("overworld", 8.0),
    ],
)
def test_guardian_damage_retreat_checks_terrain(dimension, health):
    mutations = []

    class RetreatBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [724.3, 68, 636.7], "health": health, "food": 20, "ready": True, "alive": True,
                    "on_ground": True, "blocking": True, "dimension": dimension,
                    "nearest_hostile": {"id": 42, "name": "zombie", "dist": 1.1},
                }
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack"}] if since < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_block":
                x, y, z = body["pos"]
                if y == 67:
                    if dimension == "the_nether":
                        return {"name": "magma_block" if (x, z) == (724, 636) else "lava"}
                    return {"name": "stone" if (x, z) == (724, 636) else "air"}
                if dimension == "overworld":
                    return {"name": "air"}
                return {"name": "air" if y >= 68 else "netherrack"}
            if action == "find_blocks":
                return {"blocks": []}
            mutations.append(action)
            return {"ok": True}

    guard = GenerationGuard(RetreatBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    retreat, = incident["actions"]
    assert retreat["action"] == "combat_or_flee"
    assert retreat["result"]["reason"] == "no_safe_retreat"
    assert mutations == []


@pytest.mark.parametrize("water_state", ["surfaced", "submerged", "drowning", "failed_unstick"])
def test_tool_boundary_does_not_repeat_water_macro_after_its_own_reflex(monkeypatch, water_state):
    status = {"pos": [6378.7, 62.2, -6442.3], "health": 17, "food": 20, "oxygen": 180,
              "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True}
    calls = []

    class WetBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(WetBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def failed_water_macro(_status):
        calls.append("water_macro")
        return {"ok": False, "in_water": True}

    def failed_shore(*args, surface_first=False, **kwargs):
        calls.append("urgent_shore" if surface_first else "shore")
        return {"ok": False, "in_water": True}

    monkeypatch.setattr(guard, "_escape_water", failed_water_macro)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", failed_shore)
    if water_state == "failed_unstick":
        status.update(submerged=False)
        receipt = {"ok": False, "in_water": True, "error": {"code": "water_escape_failed"}}
    else:
        receipt = guard.handle_preemption({"source": "tool_interrupted_hurt"}, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface"
        assert calls == ["water_macro", "shore"]
        calls.clear()
        status.update(submerged=water_state != "surfaced", oxygen=0 if water_state == "drowning" else 180)

    guard.stabilize_after_tools([json.dumps(receipt)])

    expected = {"surfaced": [], "submerged": ["shore"], "drowning": ["urgent_shore"], "failed_unstick": ["shore"]}
    assert calls == expected[water_state]


@pytest.mark.parametrize("reentry", ["still_submerged", "observed_dry", "retry_window_expired"])
def test_boundary_retries_shore_directly_after_water_macro_times_out(monkeypatch, reentry):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [6383.3, 56.2, -6537.3], "health": 18, "food": 20, "oxygen": 100,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
    }
    attempts = []

    class FloodedCaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            assert action == "stop"
            return {"ok": True}

    def timed_out_water_macro(_status):
        attempts.append("water_macro")
        raise httpx.ReadTimeout("water macro timed out")

    def missed_shore(*args, surface_first=False, **kwargs):
        attempts.append("surface_then_shore" if surface_first else "shore")
        return {"ok": False, "in_water": True}

    guard = GenerationGuard(FloodedCaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", timed_out_water_macro)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", missed_shore)
    guard.stabilize_after_tools()
    assert attempts == ["water_macro", "shore"]
    attempts.clear()

    clock[0] += 31.0 if reentry == "retry_window_expired" else 4.25
    if reentry == "observed_dry":
        status.update(in_water=False, submerged=False, on_ground=True)
        guard.stabilize_after_tools()
        status.update(in_water=True, submerged=True, on_ground=False)
    guard.stabilize_after_tools()

    expected = ["surface_then_shore"] if reentry == "still_submerged" else ["water_macro", "shore"]
    assert attempts == expected


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


def test_bug056_guardian_bounds_shared_water_macro_and_falls_back_to_shore(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    events = []
    calls = []
    status = {"pos": [0.5, 52, 0.5], "health": 20, "oxygen": 240, "in_water": True,
              "submerged": True, "on_ground": False, "alive": True, "ready": True}

    class DeepWaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
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
    assert "游向最近干燥陆地登岸" in guard.stabilize_after_tools()
    completed = [e for e in events if e["type"] == "guardian.action.completed"]
    shallow, shore = completed
    assert shallow["action"] == "boundary_escape_water"
    assert shallow["duration"] == pytest.approx(20.0)
    assert shallow["result"] == {"ok": False, "reason": "time_budget", "partial": True, "in_water": True}
    assert shore["action"] == "boundary_escape_to_shore" and shore["ok"] is True
    assert all(start + timeout <= 1020.0 for _, timeout, start in calls[:-1])
    assert skills.bot is bot


@pytest.mark.parametrize("terrain", ["scan", "radial", "open_water"])
def test_flooded_cave_escape_uses_local_water_level(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {"pos": [2930.5, 39.2, -10120.5], "in_water": True, "on_ground": False, "alive": True, "ready": True}
    local_ledge = [2930, 39, -10125]
    moves = []

    class FloodedCaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                distant_lip = {"name": "grass_block", "pos": [2930, 62, -10121]}
                ledge = {"name": "stone", "pos": local_ledge}
                return {"blocks": [distant_lip, ledge] if terrain == "scan" else []}
            if action == "get_block":
                x, y, z = body["pos"]
                return {"name": "stone" if terrain != "open_water" and [x, y, z] == local_ledge else "air"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                moves.append(goal)
                if goal == [2930.5, 40.0, -10124.5]:
                    status.update(pos=goal, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(FloodedCaveBot()).escape_deep_water_to_shore()

    goal, = moves
    if terrain == "open_water":
        assert goal == pytest.approx([2930.5, 40.2, -10132.5])
        assert result["mode"] == "surface_retreat"
        assert result["ok"] is False and result["in_water"] is True
    else:
        assert goal == [2930.5, 40.0, -10124.5]
        assert result["ok"] is True and result["in_water"] is False


@pytest.mark.parametrize("landing", ["grounded", "airborne", "in_water"])
def test_shore_escape_confirms_landing_after_airborne_edge_receipt(landing):
    from mc_agent.skills.combat_env import CombatEnv

    moves = []
    posture = []
    status = {"pos": [10937.5, 62.5, -11007.3], "in_water": True, "on_ground": False,
              "alive": True, "ready": True}

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": [10938, 63, -11008]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "posture":
                posture.append(body)
            if action == "move_to":
                moves.append(body)
                if len(moves) == 1:
                    status.update(pos=[10937.7, 63.3, -11007.3], in_water=False)
                else:
                    assert posture[-1] == {"shield": True, "sneak": False, "jump": False}
                    if landing == "grounded":
                        status.update(pos=[10938.5, 64.0, -11007.5], on_ground=True)
                    elif landing == "in_water":
                        status.update(pos=[10937.7, 63.0, -11007.3], in_water=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore(keep_shield=True)

    assert len(moves) == 2
    assert moves[1]["range"] < moves[0]["range"]
    assert moves[1]["timeout"] < moves[0]["timeout"]
    assert moves[1]["stop_on"] == ["death"]
    assert tuple(moves[1][axis] for axis in ("x", "y", "z")) == (10938.5, 64.0, -11007.5)
    assert result["ok"] is (landing == "grounded")
    assert result["in_water"] is (landing == "in_water")
    assert posture[-1]["jump"] is (landing == "in_water")


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
    assert calls[-1] == ("stop", 1028.0, 1.5)
    assert all(start + timeout <= 1028.0 for _, start, timeout in calls[:-1])
    assert bot.call == original_call
    assert events[-1]["type"] == "guardian.recovery_budget_exceeded"


@pytest.mark.parametrize("solid_exit", [True, False])
def test_guardian_checks_support_before_unstick_moves(solid_exit):
    movements = []

    class LedgeBot:
        def status(self):
            return self.call("status", method="GET")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [0.5, 44, 0.5], "ready": True, "alive": True, "on_ground": True}
            if action == "get_block":
                x, y, z = body["pos"]
                if y == 43 and (solid_exit or (x, z) == (0, 0)):
                    return {"name": "stone", "diggable": True}
                return {"name": "air"}
            if action == "control":
                movements.append(body)
            return {"ok": True}

    bot = LedgeBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=skills)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_unstick", lambda: json.dumps(skills.unstick())))
    assert result["ok"] is solid_exit
    if solid_exit:
        step, = movements
        assert step["ms"] == 350 and not step["jump"] and not step["sprint"]
    else:
        assert result["error"]["code"] == "unsafe_landing" and result["requires_replan"] is True
        assert movements == []


@pytest.mark.parametrize("episode_timeout", [False, True])
def test_bug072_flee_bounds_nested_shaft_moves_and_stops_on_timeout(monkeypatch, episode_timeout):
    from mc_agent.harness.control import RunTimedOut

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    moves = []
    calls = []
    events = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            timeout_s = kwargs.get("timeout_s", 30.0)
            calls.append((action, timeout_s))
            if action == "move_to":
                moves.append((clock[0], timeout_s, body["timeout"]))
                clock[0] += min(7.0, timeout_s)
                if timeout_s < 7.0:
                    raise httpx.ReadTimeout("nested shaft move reached the retreat deadline")
            if action == "stop":
                clock[0] += timeout_s
            return {"ok": True}

    def checkpoint():
        if episode_timeout and clock[0] >= 1014.0:
            raise RunTimedOut("episode deadline")

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=checkpoint)
    orig_call = bot.call

    def retreat_recorded_shaft():
        for y in range(-17, -3):
            bot.call("move_to", {"x": -2712.5, "y": y, "z": -10275.4, "timeout": 20000})
        return json.dumps({"ok": True, "mode": "retreat_shaft"})

    if episode_timeout:
        with pytest.raises(RunTimedOut):
            guard.run_tool_with_reflex("mc_skill_flee", retreat_recorded_shaft)
        assert len(moves) == 2
    else:
        result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", retreat_recorded_shaft))
        assert result["ok"] is False and result["error"]["code"] == "time_budget"
        assert len(moves) == 5
        assert any(event["type"] == "guardian.recovery_budget_exceeded" and event["budget_s"] == 35.0 for event in events)

    assert all(start + timeout <= 1033.0 and start + server_ms / 1000 <= 1033.0 for start, timeout, server_ms in moves)
    assert calls[-1] == ("stop", 1.5)
    assert clock[0] - 1000.0 < 35.0
    assert bot.call == orig_call


@pytest.mark.parametrize("tool", ["mc_skill_flee", "mc_skill_unstick"])
@pytest.mark.parametrize("safe_exit", [True, False])
def test_bug076_unsafe_actor_exit_checks_another_route_when_a_hostile_is_close(tool, safe_exit):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    status = {
        "pos": [0.5, 64, 0.5], "alive": True, "ready": True, "on_ground": True, "health": 5.0,
        "nearest_hostile": {"id": 7, "name": "pillager", "dist": 2.0, "pos": [2.5, 64, 0.5]},
    }
    motions = []
    yaw = [0.0]

    class RiverbankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                x, y, z = body["pos"]
                supported = x <= 0 if safe_exit else (x == 0 and z == 0)
                return {"name": "stone" if y == 63 and supported else "air"}
            if action == "look":
                yaw[0] = body["yaw"]
            if action == "control":
                motions.append(action)
                dx, dz = yaw_to_unit_xz(yaw[0])
                status["pos"] = [0.5 + 2.3 * dx, 64.0, 0.5 + 2.3 * dz]
            if action == "move_to":
                motions.append(action)
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = RiverbankBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    stop_on = ["death"] if tool == "mc_skill_flee" else []
    first_choice = {"forward": True, "yaw": -1.5708, "ms": 900, "stop_on": stop_on}

    result = json.loads(guard.run_tool_with_reflex(tool, lambda: json.dumps(bot.call("control", first_choice))))

    assert result["requires_replan"] is True
    assert result["ok"] is safe_exit
    if safe_exit:
        assert motions[0] == "control"
        assert status["pos"][0] < 0
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert motions == []


def test_bug072_real_flee_entry_accepts_the_mining_sessions_httpx_timeout(monkeypatch):
    from mc_agent.harness.mc_tools import McToolset

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    request_timeouts = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            request_timeouts.append(kwargs["timeout_s"])
            return {"pos": [0.5, 64, 0.5], "on_ground": True}

    def retreat(scoped, **kwargs):
        scoped.bot.call("status", method="GET")
        clock[0] += 32.6
        scoped.bot.call("status", method="GET")
        return {"ok": True, "mode": "retreat_shaft", "pos_after": [1.5, 64, 0.5]}

    monkeypatch.setattr(MacroSkills, "_flee_hostiles", retreat)
    bot = ShaftBot()
    toolset = McToolset(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", toolset.mc_skill_flee))

    assert result["ok"] is True and result["mode"] == "retreat_shaft"
    initial, final = request_timeouts
    assert isinstance(initial, httpx.Timeout) and isinstance(final, httpx.Timeout)
    assert initial.read == 5.0
    assert initial.connect == initial.write == initial.pool == 0.5
    assert 2 * sum(final.as_dict().values()) + 0.15 <= 1033.0 - clock[0]
    assert final.connect <= 0.5 and final.write <= 0.5 and final.pool <= 0.5


@pytest.mark.parametrize("terrain", ["wall", "unknown", "drifted_during_probe", "missed_landing"])
def test_bug076_natural_cover_requires_solid_shot_blocking_and_confirmed_landing(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64.0, 0.5], "alive": True, "ready": True, "on_ground": True, "health": 5.0, "food": 20,
        "nearest_hostile": {"id": 44, "name": "pillager", "dist": 8.0},
    }
    shooter = [8.5, 64.0, 0.5]
    moves = []
    status_reads = []

    class CoverBot:
        def status(self):
            status_reads.append(True)
            if terrain == "drifted_during_probe" and len(status_reads) >= 2:
                status.update(pos=[1.0, 63.4, 0.5], on_ground=False)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                return {"entities": [{"id": 44, "name": "pillager", "pos": shooter}]}
            if action == "get_block":
                x, y, z = body["pos"]
                if y in (64, 65) and 3 <= x <= 5 and 1 <= z <= 2:
                    if terrain == "unknown":
                        raise httpx.ReadTimeout("cover voxel was not read")
                    return {"name": "stone"}
                return {"name": "stone" if y == 63 else "air"}
            if action == "move_to":
                moves.append(body)
                if terrain != "missed_landing":
                    status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = CoverBot()
    if terrain == "wall":
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        incident = guard.poll()
        result = next(a["result"] for a in incident["actions"] if a["action"] == "combat_or_flee")
    else:
        result = CombatEnv(bot).seek_natural_cover(shooter)

    if terrain in {"unknown", "drifted_during_probe"}:
        assert result is None
        assert moves == []
    else:
        goal, = moves
        assert (goal["x"], goal["y"], goal["z"]) == pytest.approx((0.5, 64.0, 2.7))
        assert result["ok"] is (terrain == "wall")
        if terrain == "wall":
            assert result["mode"] == "natural_cover"
        else:
            assert result["reason"] == "cover_unconfirmed"


@pytest.mark.parametrize("action", ["control", "move_to"])
@pytest.mark.parametrize("solid_exit", [True, False])
@pytest.mark.parametrize("dimension", ["the_nether", "overworld"])
def test_explicit_flee_checks_support_before_each_motion(action, solid_exit, dimension):
    movements = []

    class RetreatBot:
        def call(self, name, body=None, **kwargs):
            if name == "status":
                return {"pos": [0.5, 36, 0.5], "dimension": dimension, "ready": True, "alive": True, "on_ground": True}
            if name == "get_block":
                x, y, z = body["pos"]
                if y == 35:
                    if solid_exit or (x, z) == (0, 0):
                        return {"name": "stone"}
                    return {"name": "lava" if dimension == "the_nether" else "air"}
                return {"name": "air"}
            movements.append((name, body))
            return {"ok": True}

    body = {"x": 22.5, "z": 0.5, "range": 2.0, "stop_on": ["death"]}
    if action == "control":
        body = {"forward": True, "sprint": True, "jump": True, "yaw": -1.5708, "ms": 1200, "stop_on": ["death"]}
    bot = RetreatBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(bot.call(action, body))))
    assert result["ok"] is solid_exit
    if solid_exit:
        (_, move), = movements
        if action == "control":
            assert move["ms"] == 450 and move["jump"] is False
        else:
            assert (move["x"], move["y"], move["z"]) == (6.5, 36, 0.5)
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert movements == []


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("failure", ["timeout", "death"])
def test_guardian_wall_recovery_respects_deadline_and_death(entry, failure, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    events = []
    attempts = []
    status = {"pos": [0.5, 35.0, 0.5], "health": 18, "food": 20, "dimension": "the_nether",
              "ready": True, "alive": True, "on_ground": True}

    class WallBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": "in_wall"}]}
            attempts.append((action, kwargs["timeout_s"]))
            if action == "dig":
                if failure == "timeout":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("wall dig reached its deadline")
                status.update(health=0, ready=False, alive=False)
            return {"ok": True}

    monkeypatch.setattr(MacroSkills, "unstick", lambda self: self.bot.call("dig", {}, timeout_s=15))
    guard = GenerationGuard(WallBot(), EventSink(events.append), checkpoint=lambda: None)
    expected_action = "unstick_in_wall"
    if entry == "boundary":
        assert guard.stabilize_after_tools() is None
        expected_action = "boundary_unstick_wall"
    else:
        assert guard.poll() is not None

    completed, = [e for e in events if e["type"] == "guardian.action.completed"]
    assert completed["action"] == expected_action
    assert completed["ok"] is False
    if failure == "timeout":
        assert completed["result"]["reason"] == "time_budget"
        assert attempts == [("dig", 6.0), ("stop", 1.5)]
    else:
        assert completed["result"]["respawned"] is True
        assert attempts == [("dig", 6.0)]


@pytest.mark.parametrize("starts_submerged", [True, False])
def test_bug056_subcortical_water_escape_uses_budget_once(starts_submerged, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    calls = []
    events = []
    zombie = {"id": 42, "name": "zombie", "dist": 2.0, "pos": [2.5, 52, 0.5]}
    status = {
        "pos": [0.5, 52, 0.5], "health": 17, "food": 20, "alive": True, "ready": True,
        "in_water": starts_submerged, "submerged": starts_submerged, "on_ground": not starts_submerged,
        "nearest_hostile": zombie,
    }

    class CombatWaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "attack":
                calls.append("attack")
                status.update(in_water=True, submerged=True, on_ground=False)
                return {"hits": 1, "killed": False}
            if action == "move_to":
                calls.append("water_move")
                assert kwargs["timeout_s"] == pytest.approx(20.0)
                clock[0] += kwargs["timeout_s"]
                raise httpx.ReadTimeout("water move reached its deadline")
            if action == "stop" and kwargs["timeout_s"] == 1.5:
                calls.append("water_stop")
            return {"ok": True}

    monkeypatch.setattr(MacroSkills, "escape_water", lambda self: self.bot.call("move_to", {}, timeout_s=60))
    bot = CombatWaterBot()
    skills = MacroSkills(bot)
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)

    def blocked_shore(*args, **kwargs):
        calls.append("shore")
        return {"ok": False, "in_water": True}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", blocked_shore)
    trigger = {"source": "tool_interrupted_hurt", "primary_entity_id": 42, "primary_type": "zombie", "distance": 2.0}
    if starts_submerged:
        result = guard.combat_slot.handle(guard.combat_env, trigger)
        assert result["mode"] == "water_surface"
    else:
        receipt = guard.handle_preemption(trigger, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface_abort"
        result = next(e["result"] for e in events if e["type"] == "guardian.action.completed")
    assert result["actions"][-2]["reason"] == "time_budget"
    assert result["actions"][-1] == {"action": "water_surface_shore", "ok": False}
    assert calls == (["attack"] if not starts_submerged else []) + ["water_move", "water_stop", "shore"]
    assert skills.bot is bot


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("posture_state", ["normal", "stale", "blocked"])
def test_bug057_hot_floor_crouches_early_without_dropping_shield(entry, posture_state, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    seq = [1]
    calls = []
    saved_sneak = posture_state == "stale"
    status = {
        "pos": [724.3, 68, 636.7], "health": 16.5, "food": 17, "ready": True, "alive": True,
        "on_ground": True, "blocking": True, "sneaking": False, "dimension": "the_nether",
        "nearest_hostile": {"id": 42, "name": "wither_skeleton", "dist": 2.0},
    }

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            nonlocal saved_sneak
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                since = kwargs["params"]["since"]
                hit = {"seq": seq[0], "type": "hurt", "source": "hot_floor"}
                return {"events": [hit] if since < seq[0] else []}
            if action == "get_block":
                return {"name": "magma_block" if body["pos"][1] < 68 else "air"}
            if action == "posture":
                calls.append(body)
                if body["sneak"] != saved_sneak:
                    saved_sneak = body["sneak"]
                    status["sneaking"] = body["sneak"] and posture_state != "blocked"
                return {"sneaking": status["sneaking"]}
            raise AssertionError(action)

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if entry == "poll":
        incident = guard.poll()
        assert incident["requires_replan"] is (posture_state == "blocked")
        assert incident["reasons"] == ["hot_floor"]
    else:
        note = guard.stabilize_after_tools()
        assert ("已潜行避伤" in (note or "")) is (posture_state != "blocked")
    expected_sneaks = [True] if posture_state == "normal" else [True, False, True]
    assert calls == [{"sneak": sneak, "shield": True, "jump": False} for sneak in expected_sneaks]

    seq[0] += 1
    clock[0] += 4.0
    if entry == "poll":
        assert guard.poll() is None
    else:
        assert guard.stabilize_after_tools() is None
    assert len(calls) == len(expected_sneaks)

    if posture_state == "blocked":
        status["pos"][0] += 1.0
        seq[0] += 1
        clock[0] += 0.5
        if entry == "poll":
            assert guard.poll()["requires_replan"] is True
        else:
            assert guard.stabilize_after_tools() is None
        assert len(calls) == 2 * len(expected_sneaks)


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


@pytest.mark.parametrize("still_magma", [True, False])
def test_attack_replans_only_on_confirmed_magma(still_magma):
    status = {"pos": [0.5, 64, 0.5], "health": 17, "food": 20, "ready": True, "alive": True, "on_ground": True}
    moves = []
    attacks = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [] if since else [{"seq": 1, "type": "hurt", "source": "hot_floor"}]}
            if action == "posture":
                return {"sneaking": True}
            if action == "get_block":
                x, y, z = body["pos"]
                if y >= 64:
                    return {"name": "air"}
                return {"name": "magma_block" if still_magma and (x, z) == (0, 0) else "stone"}
            if action == "move_to":
                moves.append(body)
                status["pos"] = [body[k] for k in ("x", "y", "z")]
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll()["actions"][0]["ok"] is True

    def attack():
        attacks.append(True)
        raise httpx.ReadTimeout("attack timed out")

    if still_magma:
        result = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert result["requires_replan"] is True and result["error"]["code"] == "hot_floor"
        assert result["recovery"]["result"]["mode"] == "stepped_off"
        assert len(moves) == 1 and attacks == []
    else:
        with pytest.raises(httpx.ReadTimeout, match="attack timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert attacks == [True] and moves == []


@pytest.mark.parametrize("attack_result", ["returned", "interrupted", "read_timeout"])
def test_attack_in_water_requests_stop_and_preserves_read_timeout(monkeypatch, attack_result):
    import threading

    stopped = threading.Event()
    actions = []
    status = {
        "pos": [0.5, 63, 0.5], "health": 12, "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 27, "name": "drowned", "dist": 2.0, "pos": [2.5, 62, 0.5]},
    }

    class AttackBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            actions.append(action)
            if action == "attack":
                status.update(in_water=True, on_ground=False, health=6.7)
                assert stopped.wait(2.0), "attack remained active after entering water under threat"
                if attack_result == "read_timeout":
                    raise httpx.ReadTimeout("attack timed out")
                if attack_result == "interrupted":
                    raise ApiError("interrupted", "attack stopped")
                return {"killed": False}
            if action == "stop":
                stopped.set()
                return {"ok": True}
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    bot = AttackBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def escape_to_shore(threat_pos, **kwargs):
        assert threat_pos == [2.5, 62, 0.5]
        assert actions[:2] == ["attack", "stop"]
        actions.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", escape_to_shore)
    monkeypatch.setattr(guard, "_escape_water", lambda before: {"ok": False, "in_water": True})

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": 27, "timeout": 6000}))

    if attack_result == "read_timeout":
        with pytest.raises(httpx.ReadTimeout, match="attack timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert actions == ["attack", "stop"]
    else:
        receipt = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert receipt["preempted_by_reflex"] is True
        assert receipt["status_after"]["in_water"] is False
        assert actions.count("attack") == 1
        assert "shore" in actions


def test_mining_on_magma_is_interrupted_before_the_boundary():
    import threading

    stopped = threading.Event()
    status = {
        "pos": [0.5, 38.0, 0.5], "health": 19.5, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "the_nether", "sneaking": False,
    }
    actions = []

    class MiningBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                assert body["pos"] == [0, 37, 0]
                return {"name": "magma_block"}
            if action == "dig":
                assert stopped.wait(3.0)
                return {"interrupted_by": "hurt"}
            if action == "stop":
                actions.append("stop")
                stopped.set()
            if action == "posture":
                actions.append("sneak")
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            return {"ok": True}

    bot = MiningBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", lambda: json.dumps(bot.call("dig", {}))))
    assert result["reflex_mode"] == "hot_floor"
    assert result["requires_replan"] is True
    assert status["sneaking"] is True
    assert actions == ["stop", "sneak"]
























