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
