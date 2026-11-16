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
def test_guard_ignores_old_damage_and_keeps_water_and_respawn_semantics(mode):
    actions = []
    state = {"jumping": False}

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
        assert actions == [{"jump": True, "sneak": False, "shield": True}]
        assert incident["requires_replan"] is True


class BunkerBot:
    def __init__(self, open_side=False, roof=True):
        self.y = 64
        self.open_side = open_side
        self.roof = roof
        self.calls = []

    def status(self):
        return {"pos": [0.5, self.y, 0.5], "health": 18, "on_ground": True}

    def inventory(self):
        return {"items": [{"name": "stone_pickaxe", "count": 1}] + ([{"name": "dirt", "count": 1}] if self.roof else [])}

    def call(self, action, body):
        self.calls.append((action, body))
        if action == "get_block":
            return {"name": "air" if self.open_side and body["pos"] == [1, 62, 0] else "stone"}
        if action == "dig":
            self.y -= 1
            return {"dug": "stone"}
        if action == "place":
            return {"placed": "dirt"}
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
    }])
    assert frames[-1]["kind"] == "guardian"
    assert frames[-1]["state_patch"]["pos"] == [1.5, 61, 2.5]
    assert replay.status["health"] == 16
    assert replay.status["blocking"] is True
