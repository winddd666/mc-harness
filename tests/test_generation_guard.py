"""Generation-time damage/water response, using real harness cancellation boundaries."""
import json
import math
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.errors import ApiError
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.control import RequestCancelled, RunTimedOut, check_control
from mc_agent.harness.events import EventSink
from mc_agent.harness.guardian import GenerationGuard
from mc_agent.skills.library import MacroSkills
from mc_agent.live.state import ReplayIncrementer


def test_combat_environment_imports_in_a_fresh_python_process():
    source_root = Path(__file__).resolve().parents[1] / "src"
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
            wet = mode == "water" and not escaped
            result = {"health": 20, "food": 20, "alive": True, "ready": True, "pos": [0.5, 64, 0.5],
                      "in_water": wet, "submerged": wet, "on_ground": not wet,
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
                        "in_water": not escaped, "submerged": not escaped, "on_ground": bool(escaped), "jumping": False}
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


@pytest.mark.parametrize("elapsed", [4.0, 31.0])
def test_water_float_continues_shore_recovery_after_recent_failure(monkeypatch, elapsed):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 100.0)
    status = {
        "pos": [7982.5, 59.9, 2869.5], "health": 20, "food": 20, "oxygen": 120,
        "alive": True, "ready": True, "in_water": True, "submerged": True, "on_ground": False,
    }
    attempts = []

    class WetBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    guard = GenerationGuard(WetBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_water_escape_at = 100.0 - elapsed
    guard._last_water_escape_failed = True

    def recover(method, **kwargs):
        attempts.append((method, kwargs.get("surface_first", False)))
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard, "_escape_water", lambda *_: recover("generic"))
    monkeypatch.setattr(
        guard.combat_env, "escape_deep_water_to_shore", lambda *_, **kwargs: recover("shore", **kwargs),
    )

    incident = guard.poll()

    assert "water_float" in incident["reasons"]
    assert attempts == ([("shore", True)] if elapsed == 4.0 else [("generic", False)])
    assert guard._last_water_escape_failed is False


@pytest.mark.parametrize("observe", ["poll", "boundary"])
@pytest.mark.parametrize("landed", [False, True])
def test_water_recovery_history_clears_only_after_grounded_dry_observation(monkeypatch, observe, landed):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [7824.5, 63.5, 2843.3], "health": 20, "food": 20, "oxygen": 120,
        "alive": True, "ready": True, "in_water": False, "submerged": False, "on_ground": landed,
    }
    calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            pytest.fail(f"Unexpected call: {action}")

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_water_escape_at = 96.0
    guard._last_water_escape_failed = True
    guard._water_hurt_streak = 1

    def recover(method, *, surface_first=False):
        calls.append((method, surface_first))
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard, "_escape_water", lambda *_: recover("generic"))
    monkeypatch.setattr(
        guard.combat_env, "escape_deep_water_to_shore",
        lambda *_, **kwargs: recover("shore", surface_first=kwargs.get("surface_first", False)),
    )

    first_observation = guard.poll if observe == "poll" else guard.stabilize_after_tools
    assert first_observation() is None
    assert guard._last_water_escape_failed is not landed
    assert guard._water_hurt_streak == (0 if landed else 1)
    status.update(in_water=True, submerged=True, on_ground=False)
    clock[0] += 1.0
    assert "water_float" in guard.poll()["reasons"]
    assert calls == ([("generic", False)] if landed else [("shore", True)])


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
                        "in_water": not escaped, "submerged": False, "on_ground": True, "jumping": False, "oxygen": 280}
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


@pytest.mark.parametrize("recovery", ["shore_receipt", "status_changed"])
def test_water_tool_preemption_reports_life_unavailable_after_respawn(monkeypatch, recovery):
    status = {
        "pos": [40995.8, 62.1, 10997.6], "dimension": "overworld", "health": 2, "food": 17,
        "ready": True, "alive": True, "in_water": True, "submerged": True, "last_death": {"t": 1},
    }
    events = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    def shore(*_args, **_kwargs):
        if recovery == "status_changed":
            status.update(health=20, in_water=False, on_ground=True, last_death={"t": 2})
            return {"ok": False, "in_water": False}
        return {"ok": False, "mode": "respawned", "in_water": False}

    guard = GenerationGuard(RiverBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": "zombie", "primary_entity_id": 5462415},
        interrupted_name="mc_attack",
    )

    assert receipt["reflex_mode"] == "life_unavailable"
    assert receipt["requires_replan"]
    assert guard._last_water_escape_failed is False
    assert guard._water_hurt_streak == 0
    assert [e["action"] for e in events if e["type"] == "guardian.action.completed"] == ["escape_to_shore"]


@pytest.mark.parametrize("landing", ["airborne", "grounded", "back_in_water", "respawned"])
def test_water_tool_preemption_keeps_shore_recovery_during_airborne_handoff(monkeypatch, landing):
    status = {
        "pos": [-7780.1, 32.2, -2845.7], "dimension": "overworld", "health": 20,
        "ready": True, "alive": True, "in_water": False, "on_ground": False, "last_death": {"t": 1},
    }
    shore_calls = []

    class RiverBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                if kwargs.get("timeout_s") == 0.25:
                    if landing == "grounded":
                        status.update(on_ground=True)
                    elif landing == "back_in_water":
                        status.update(in_water=True, submerged=True)
                    elif landing == "respawned":
                        status.update(on_ground=True, last_death={"t": 2})
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            if endpoint == "inventory":
                return {"items": []}
            raise AssertionError(endpoint)

    def shore(*args, **kwargs):
        shore_calls.append(kwargs.get("surface_first"))
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def combat(_trigger):
        pytest.fail("Water recovery must not flee from a distant zombie")

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    receipt = guard.handle_preemption(
        {
            "source": "tool_water_threat", "primary_type": "zombie", "primary_entity_id": 45,
            "distance": 20.3, "observed_life": ("overworld", {"t": 1}),
        },
        interrupted_name="mc_skill_unstick",
    )

    assert receipt["requires_replan"]
    assert receipt["reflex_mode"] == ("life_unavailable" if landing == "respawned" else "water_surface")
    assert receipt["status_after"]["on_ground"] is (landing != "airborne")
    assert guard._last_water_escape_failed is (landing == "airborne")
    assert shore_calls == ([True] if landing == "back_in_water" else [])


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


@pytest.mark.parametrize("source, distance, preempt", [
    ("fall", 10.3, False), ("stalagmite", 7.8, False), ("stalagmite", 3.9, True),
    ("entity_explosion", 9.9, True), (None, 10.3, True),
])
def test_terrain_interruption_does_not_attribute_hurt_to_a_distant_creeper(monkeypatch, source, distance, preempt):
    status = {
        "pos": [-7028.9, 69, 5757.3], "health": 19, "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 42, "name": "creeper", "dist": distance},
    }
    triggers = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "move_to":
                return {"interrupted_by": "hurt"}
            if action == "events":
                events = [{"seq": 1, "type": "hurt", "source": source}] if source else []
                return {"events": events, "last_seq": len(events)}
            return {"ok": True}

    def combat(trigger):
        triggers.append(trigger)
        return {"handled": True, "mode": "creeper_evade", "status_after": dict(status)}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_run_subcortical_combat", combat)
    result = json.loads(guard.run_tool_with_reflex("mc_move_to", lambda: json.dumps(bot.call("move_to"))))

    assert bool(triggers) is preempt
    if preempt:
        assert result["reflex_mode"] == "creeper_evade"
    else:
        assert result == {"interrupted_by": "hurt"}


def test_watched_movement_ignores_confirmed_stalagmite_damage_near_a_distant_creeper(monkeypatch):
    import threading

    baseline = threading.Event()
    damaged = threading.Event()
    read_hurt = threading.Event()
    status = {"pos": [0.5, 68, 0.5], "health": 20, "ready": True, "alive": True, "on_ground": True,
              "nearest_hostile": {"id": 42, "name": "creeper", "dist": 8.8}}
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
                return {"events": [{"seq": 1, "type": "hurt", "source": "stalagmite"}]}
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


@pytest.mark.parametrize("scenario", ["close", "leaves_reach", "melee_edge", "far"])
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
    tight_counter = scenario in {"close", "leaves_reach"}
    assert attack["timeout"] == (1350 if tight_counter else 2600)
    assert attack.get("max_hits") == (2 if tight_counter else None)
    if tight_counter:
        melee, = [action for action in outcome["actions"] if action["action"] == "melee_attack" and action["ok"]]
        assert melee["hit_log"] == [{"entity_id": 42}]
    assert outcome["mode"] == ("counter_kill" if scenario == "leaves_reach" else "counter_killed")


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


@pytest.mark.parametrize("api_error", [False, True])
def test_hurt_interrupted_move_enters_lava_reflex_with_distant_hostile(monkeypatch, api_error):
    moves = []
    status = {
        "pos": [-395.9, 53, -1889.5], "health": 10.3, "food": 20, "dimension": "the_nether",
        "in_lava": True, "alive": True, "ready": True, "on_ground": False,
        "nearest_hostile": {"id": 44, "name": "blaze", "dist": 16.7},
    }

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "move_to":
                moves.append(body)
                if api_error:
                    raise ApiError("interrupted", "stopped because of hurt")
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    def escape():
        status.update(in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    bot = LedgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    move = {"entity_id": 44, "range": 3}
    receipt = json.loads(guard.run_tool_with_reflex("mc_move_to", lambda: json.dumps(bot.call("move_to", move))))

    assert moves == [move]
    assert receipt["preempted_by_reflex"] and receipt["reflex_mode"] == "lava_escape"
    assert status["in_lava"] is False


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


@pytest.mark.parametrize("new_risk", ["closer", "different_target", "new_hurt"])
@pytest.mark.parametrize("first_warning", ["poll", "tool"])
def test_bug_009_stationary_failed_creeper_retreat_only_cancels_again_on_change(monkeypatch, new_risk, first_warning):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    creeper = {"id": 4547388, "name": "creeper", "dist": 2.9, "pos": [2975.5, 53, -10040.3]}
    status = {
        "pos": [2975.5, 53, -10037.4], "health": 20, "food": 20, "dimension": "overworld",
        "on_ground": True, "alive": True, "ready": True, "blocking": False, "nearest_hostile": creeper,
    }
    hits = []
    attempts = []

    class CreeperBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(CreeperBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def cornered(_env, trigger):
        attempts.append(trigger["primary_entity_id"])
        if len(attempts) == 3 and new_risk == "new_hurt":
            hits.append({"type": "hurt", "seq": 1, "source": "arrow"})
        retreat = {"action": "retreat_from_creeper", "ok": False, "moved": 0.0, "reason": "no_safe_retreat"}
        if first_warning == "tool" and len(attempts) == 2:
            creeper["dist"] = 4.0
        return {"handled": True, "mode": "creeper_evade", "actions": [retreat]}

    monkeypatch.setattr(guard.combat_slot, "handle", cornered)
    if first_warning == "tool":
        creeper.update(dist=4.2, pos=[2975.5, 53, -10041.6])
        receipt = guard.handle_preemption({
            "source": "pre_hurt_creeper", "primary_entity_id": creeper["id"], "primary_type": "creeper", "distance": 4.4,
        }, interrupted_name="mc_skill_staircase")
        assert receipt["preempted_by_reflex"] and receipt["requires_replan"]
        clock[0] += 3.42
        creeper.update(dist=3.9, pos=[2975.5, 53, -10041.3])
    else:
        assert guard.poll()["requires_replan"] is True
        clock[0] += 15.0

    second = guard.poll()
    assert second["reasons"] == ["pre_hurt_creeper"] and second["requires_replan"] is False

    clock[0] += 6.0 if new_risk == "closer" else 15.5
    if new_risk == "closer":
        creeper["dist"] = 2.25
    elif new_risk == "different_target":
        creeper["id"] += 1
    assert guard.poll()["requires_replan"] is True
    assert len(attempts) == 3


@pytest.mark.parametrize("change", ["stationary", "regenerated", "dropped_health", "hurt", "moved", "shooter_closed_in"])
def test_bug_009_failed_distant_ranged_retreat_keeps_generation_if_unchanged(monkeypatch, change):
    shooter = {"id": 4920712, "name": "skeleton", "dist": 11.2, "pos": [5267.6, 23, -7421.5]}
    status = {
        "pos": [5256.4, 23, -7421.5], "health": 19, "food": 20, "dimension": "overworld",
        "on_ground": True, "alive": True, "ready": True, "blocking": False, "nearest_hostile": shooter,
        "held": {"name": "stone_pickaxe", "slot": 41},
    }
    hurt = []

    class ShotBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {**status, "nearest_hostile": dict(shooter)}
            if action == "events":
                return {"events": [e for e in hurt if e["seq"] > kwargs["params"]["since"]]}
            assert action == "inventory"
            return {"items": [], "offhand": None}

    guard = GenerationGuard(ShotBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def blocked_retreat(_env, trigger):
        assert trigger["source"] == "pre_hurt_ranged"
        if change == "hurt":
            hurt.append({"seq": 1, "type": "hurt", "source": "arrow"})
        elif change == "regenerated":
            status["health"] = 20
        elif change == "dropped_health":
            status["health"] = 18
        elif change == "moved":
            status["pos"] = [5254.4, 23, -7421.5]
        elif change == "shooter_closed_in":
            shooter["dist"] = 5.0
        return {"handled": True, "mode": "cover_or_flee", "actions": [
            {"action": "flee", "ok": False, "moved": 0.0, "reason": "no_safe_retreat"},
        ]}

    monkeypatch.setattr(guard.combat_slot, "handle", blocked_retreat)
    incident = guard.poll()

    assert incident["reasons"] == ["pre_hurt_ranged"]
    assert incident["requires_replan"] is (change not in {"stationary", "regenerated"})


@pytest.mark.parametrize("change", ["stationary", "drifted", "hurt", "landed"])
def test_bug_009_stationary_lava_escape_keeps_retrying_without_duplicate_cancel(monkeypatch, change):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3567.0, 64.6, 1790.3], "health": 20, "food": 20, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "ready": True, "alive": True, "last_death": {"t": 10},
    }
    hurts = []
    escapes = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in hurts if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            raise AssertionError(action)

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def escape(**_kwargs):
        escapes.append(True)
        if len(escapes) == 2 and change == "drifted":
            status["pos"] = [3568, 64.6, 1790.3]
        if len(escapes) == 2 and change == "hurt":
            status["health"] = 18
            hurts.append({"seq": 1, "type": "hurt", "source": "lava"})
        if len(escapes) == 2 and change == "landed":
            status.update(in_lava=False, on_ground=True)
        return {"ok": not status["in_lava"], "in_lava": status["in_lava"], "pos_after": status["pos"]}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    assert guard.poll()["requires_replan"] is True
    clock[0] += 2.1
    result = guard.poll()
    assert result["requires_replan"] is (change != "stationary")
    assert len(escapes) == 2
    if change == "stationary":
        clock[0] += 8.0
        assert guard.poll()["requires_replan"] is True


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


@pytest.mark.parametrize("landing", ["magma", "stone", "respawned", "death_event"])
def test_hot_floor_damage_during_combat_preserves_the_shield_and_crouches(monkeypatch, landing):
    spider = {"id": 42, "name": "spider", "dist": 1.4}
    status = {"pos": [-2900.6, 70, -10098.9], "health": 4.0, "food": 17, "ready": True, "alive": True,
              "on_ground": True, "blocking": True, "nearest_hostile": spider, "dimension": "overworld"}
    events = []
    posture = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [row for row in events if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                assert 0 < kwargs["timeout_s"] <= 0.5
                return {"name": "stone" if landing == "stone" else "magma_block"}
            if action == "posture":
                posture.append(body)
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            pytest.fail(f"Unexpected call after combat: {action}")

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_detect_pre_hurt_trigger", lambda _: {
        "source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "spider", "distance": 1.4,
    })

    def retreat(_env, _trigger):
        events.append({"seq": 1, "type": "hurt", "source": "hot_floor"})
        status["health"] = 3.33
        if landing == "respawned":
            status["last_death"] = {"t": 321}
        elif landing == "death_event":
            events.append({"seq": 2, "type": "death"})
        return {"handled": True, "mode": "cover_or_flee", "actions": [
            {"action": "raise_shield_entry", "ok": True},
            {"action": "flee", "ok": False, "reason": "unsafe_landing", "moved": 0.0},
        ]}

    monkeypatch.setattr(guard.combat_slot, "handle", retreat)
    incident = guard.poll()

    assert posture == ([{"sneak": True, "shield": True, "jump": False}] if landing == "magma" else [])
    assert ("hot_floor" in incident["reasons"]) is (landing == "magma")
    assert incident["requires_replan"]


@pytest.mark.parametrize("after_kill", [
    "stable", "hurt", "moved", "shooter", "death", "unverified",
    "shield_released", "shield_changed", "shield_latched", "shield_still_blocking",
])
def test_bug_009_resolved_melee_warning_keeps_generation_only_when_stable(monkeypatch, after_kill):
    spider = {"id": 4614512, "name": "spider", "dist": 2.7, "pos": [3.2, 64, 0.5]}
    status = {
        "pos": [0.5, 64, 0.5], "health": 20, "food": 20, "dimension": "overworld",
        "on_ground": True, "alive": True, "ready": True, "blocking": False, "nearest_hostile": spider,
        "held": {"name": "diamond_sword", "slot": 37, "durability_used": 5},
    }
    events = []
    attempts = []
    raised_shield = after_kill.startswith("shield_")
    if raised_shield:
        status["offhand"] = {"name": "shield", "slot": 45, "durability_used": 7}

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if after_kill == "unverified" and attempts:
                    raise httpx.ReadTimeout("post-combat event read failed")
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [status["held"]]}
            raise AssertionError(action)

    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_detect_pre_hurt_trigger", lambda _: {
        "source": "pre_hurt_melee", "primary_type": "spider", "primary_entity_id": spider["id"], "distance": 2.7,
    })

    def kill(_env, trigger):
        attempts.append(trigger["primary_entity_id"])
        status.update(nearest_hostile=None, held={**status["held"], "durability_used": 6})
        if after_kill == "hurt":
            events.append({"seq": 1, "type": "hurt", "source": "arrow"})
        elif after_kill == "moved":
            status["pos"] = [1.5, 64, 0.5]
        elif after_kill == "shooter":
            status["nearest_hostile"] = {"id": 14, "name": "skeleton", "dist": 9}
        elif after_kill == "death":
            events.append({"seq": 1, "type": "death"})
        steps = []
        if raised_shield:
            steps.append({"action": "raise_shield_entry", "ok": True})
            status["offhand"] = {**status["offhand"], "durability_used": 8}
            if after_kill == "shield_changed":
                status["offhand"] = None
            elif after_kill == "shield_still_blocking":
                status["blocking"] = True
        steps.append({"action": "melee_attack", "entity_id": spider["id"], "ok": True, "killed": True})
        return {
            "handled": True, "mode": "counter_killed", "actions": steps,
            "latched_guard": after_kill == "shield_latched",
        }

    monkeypatch.setattr(guard.combat_slot, "handle", kill)
    incident = guard.poll()
    assert attempts == [spider["id"]]
    assert incident["requires_replan"] is (after_kill not in {"stable", "shield_released"})


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


@pytest.mark.parametrize("recheck", ["grounded", "airborne", "changed_life", "loading", "occluded", "unknown"])
def test_low_health_spawn_reacts_to_visible_melee_before_first_new_hit(monkeypatch, recheck):
    origin = [6152.5, 68, 8.5]
    zombie = {"id": 44, "name": "zombie", "pos": [6153.4, 68, 8.5], "dist": 0.9}
    status = {
        "pos": origin, "health": 11, "food": 16, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    history = [
        {"seq": 1, "type": "hurt", "source": "mob_attack", "health": 0},
        {"seq": 2, "type": "death"},
        {"seq": 3, "type": "hurt", "source": "fall", "health": 11},
        {"seq": 4, "type": "spawn"},
    ]
    reads = 0
    triggers = []

    class RespawnBot:
        def call(self, action, body=None, **kwargs):
            nonlocal reads
            if action == "status":
                reads += 1
                if reads > 1:
                    if recheck == "airborne":
                        return {**status, "on_ground": False}
                    if recheck == "changed_life":
                        return {**status, "last_death": {"t": 2}}
                    if recheck == "loading":
                        return {**status, "ready": False}
                    if recheck == "unknown":
                        return {**status, "nearest_hostile": {"id": 44, "name": "zombie", "dist": 0.9}}
                return dict(status)
            if action == "events":
                return {"events": [row for row in history if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "bread", "count": 1}]}
            if action == "get_entities":
                return {"entities": []}
            raise AssertionError(action)

    guard = GenerationGuard(RespawnBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.previous_health = 20.0
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": recheck != "occluded"})

    def react(trigger):
        triggers.append(trigger["primary_entity_id"])
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    monkeypatch.setattr(guard, "_run_subcortical_combat", react)
    incident = guard.poll()

    assert triggers == ([44] if recheck == "grounded" else [])
    assert ("pre_hurt_melee" in incident["reasons"]) is (recheck == "grounded")
    assert "death_or_connection_changed" in incident["reasons"]
    assert incident["event_seqs"] == [1, 2, 3, 4]
    assert incident["requires_replan"] and guard.retry_after_ready
    assert all(a["action"] != "eat" for a in incident["actions"])


@pytest.mark.parametrize("path, endpoint", [("/api/attack", "attack"), ("/status", "status"), ("/unrecognized", None), (None, None)])
def test_combat_timeout_records_only_a_known_endpoint(path, endpoint):
    events = []
    stops = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            assert action == "stop" and kwargs["allow_reauth"] is False
            stops.append(action)
            return {"ok": True}

    def stalled_call():
        if path is None:
            raise httpx.ReadTimeout("request unavailable")
        request = httpx.Request(
            "POST", f"https://example.invalid{path}?marker=omitted",
            json={"entity_id": 6079222, "extra": "omit-payload"},
        )
        raise httpx.ReadTimeout("request stalled", request=request)

    guard = GenerationGuard(RidgeBot(), EventSink(events.append), checkpoint=lambda: None)
    result = guard._action("subcortical_combat", stalled_call)
    completed, = [event for event in events if event["type"] == "guardian.action.completed"]
    assert completed.get("endpoint") == endpoint
    assert completed.get("target_id") == (6079222 if endpoint == "attack" else None)
    assert "omitted" not in str(completed)
    assert "omit-payload" not in str(completed)
    assert result["error"] == {"code": "network_uncertain", "message": "ReadTimeout", "timeout": True}
    assert stops == ["stop"]


@pytest.mark.parametrize("landing", ["shooter", "clear", "respawned"])
def test_poll_restores_shield_toward_remaining_shooter_after_attack_timeout(monkeypatch, landing):
    zombie = {"id": 42, "name": "zombie", "dist": 1.6, "pos": [4025.6, 69, 26022.5]}
    skeleton = {"id": 178789, "name": "skeleton", "dist": 7.1, "pos": [4027.3, 69, 26029.7]}
    shield = {"name": "shield", "count": 1}
    status = {
        "pos": [4024, 69, 26022.5], "health": 20, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "offhand": shield,
        "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    commands = []

    class ArenaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": shield}
            commands.append((action, body))
            if action == "stop":
                status["nearest_hostile"] = None if landing == "clear" else skeleton
                if landing == "respawned":
                    status["last_death"] = {"t": 2}
            if action == "posture":
                status["blocking"] = body.get("shield") is True
                return {"blocking": status["blocking"]}
            return {"ok": True}

    guard = GenerationGuard(ArenaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def attack(_trigger):
        request = httpx.Request("POST", "https://example.invalid/attack", json={"entity_id": 42})
        raise httpx.ReadTimeout("attack reply timed out", request=request)

    monkeypatch.setattr(guard, "_run_subcortical_combat", attack)
    incident = guard.poll()

    assert incident["actions"][0]["error"]["code"] == "network_uncertain"
    expected = [("stop", {})]
    if landing == "shooter":
        expected += [("look", {"entity_id": 178789}), ("posture", {"shield": True, "jump": False, "sneak": False})]
        assert incident["actions"][-1] == {"action": "raise_shield_after_combat_timeout", "ok": True}
    assert commands == expected
    assert incident["status"]["blocking"] is (landing == "shooter")


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


@pytest.mark.parametrize("shooter_disappears", [False, True])
def test_cave_combat_ignores_distant_wall_hostiles_and_finishes_shield_rush(shooter_disappears):
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
                if self.solo_skeleton and shooter_disappears and strikes["n"] == 2:
                    raise ApiError("no_such_entity", "entity is no longer visible")
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
    if shooter_disappears:
        assert {"action": "shield_rush_attack", "ok": False, "code": "no_such_entity"} in out_s["actions"]
        assert not any(a["action"] == "fallback_flee" for a in out_s["actions"])
    else:
        assert any(a["action"] == "shield_rush_attack" and a.get("killed") is True for a in out_s["actions"])


@pytest.mark.parametrize("ranged", [False, True])
@pytest.mark.parametrize("slow_status", [False, True])
def test_combat_attacks_reserve_time_for_final_status_and_release(monkeypatch, ranged, slow_status):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    mob = "skeleton" if ranged else "zombie"
    threat = {"id": 42, "name": mob, "pos": [2.5, 64, 0.5], "dist": 2.0}
    attacks = []
    reads = []
    commands = []

    class ArenaBot:
        def status(self):
            if slow_status and len(attacks) == 2 and not reads:
                reads.append(clock[0])
                clock[0] += 0.9
            return {"pos": [0.5, 64, 0.5], "health": 20, "food": 20, "on_ground": True,
                    "ready": True, "alive": True, "nearest_hostile": threat}

        def call(self, action, body=None, **kwargs):
            commands.append((action, body))
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]}
            if action == "get_entities":
                return {"entities": [threat]}
            if action == "attack":
                read_s = kwargs["timeout_s"]
                requested_ms = round((read_s - 0.35) * 1000)
                assert body["timeout"] == (requested_ms if ranged else max(300, requested_ms - 1250))
                assert kwargs["allow_reauth"] is False
                attacks.append(requested_ms)
                clock[0] += 2.7 if len(attacks) <= 2 else read_s
                return {"hits": 1, "killed": len(attacks) == 3}
            return {"ok": True}

    env = CombatEnv(ArenaBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "chase_to_range", lambda *_a, **_kw: {"ok": True})
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged" if ranged else "pre_hurt_melee", "primary_type": mob,
        "primary_entity_id": 42, "distance": 2.0,
    })

    assert attacks[:2] == [2400 if ranged else 2600] * 2
    assert attacks[2:] == ([] if slow_status else [pytest.approx(1200, abs=1)])
    assert clock[0] < 1007.0
    assert commands[-1] == ("posture", {})
    assert outcome["status_after"]["health"] == 20


@pytest.mark.parametrize("latest_scan", ["blocked", "empty", "different_target", "timeout"])
def test_counterattack_uses_latest_confirmed_target_sighting(latest_scan):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    target = {"id": 42, "name": "zombie", "pos": [3.5, 64, 0.5], "dist": 3.0}
    scans = 0

    class CaveBot:
        def status(self):
            return {"pos": [0.5, 64, 0.5], "health": 20, "food": 20, "on_ground": True,
                    "ready": True, "alive": True, "nearest_hostile": target}

        def call(self, action, body=None, **kwargs):
            nonlocal scans
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                scans += 1
                if scans == 1:
                    return {"entities": [target]}
                if latest_scan == "blocked":
                    return {"entities": [{**target, "blocked_by_wall": True}]}
                if latest_scan == "different_target":
                    return {"entities": [{**target, "id": 43}]}
                if latest_scan == "timeout":
                    raise httpx.ReadTimeout("entity scan timed out")
                return {"entities": []}
            if action == "attack":
                assert latest_scan == "timeout" and body["entity_id"] == target["id"]
                raise httpx.ReadTimeout("attack reply timed out")
            return {"ok": True}

    trigger = {
        "source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie", "distance": 3.0,
    }
    env = CombatEnv(CaveBot())
    slot = CombatControllerSlot()
    if latest_scan == "timeout":
        with pytest.raises(httpx.ReadTimeout, match="attack reply timed out"):
            slot.handle(env, trigger)
    else:
        outcome = slot.handle(env, trigger)
        code = "blocked_by_wall" if latest_scan == "blocked" else "target_not_visible"
        assert {"action": "melee_attack", "entity_id": 42, "ok": False, "code": code} in outcome["actions"]

    assert scans == 2


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


@pytest.mark.parametrize("situation", ["lone", "second_hostile", "wall", "water", "new_life", "timeout", "escaped"])
def test_poll_tries_one_stationary_strike_when_an_armed_bot_cannot_retreat(monkeypatch, situation):
    zombie = {"id": 300235, "name": "zombie", "pos": [13570.3, 75, 13635.7], "dist": 0.7, "blocked_by_wall": False}
    sword = {"name": "iron_sword", "count": 1}
    status = {
        "pos": [13569.6, 75, 13635.7], "health": 5.66, "food": 20, "ready": True,
        "alive": True, "on_ground": True, "dimension": "overworld", "last_death": {"t": 1},
        "nearest_hostile": zombie, "held": sword,
    }
    attacks = []
    stops = []

    class CornerBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [hit] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [sword], "held": sword}
            if action == "get_entities":
                assert body == {"types": ["hostile"], "radius": 6} and kwargs["timeout_s"] == 0.35
                if situation == "new_life":
                    status["last_death"] = {"t": 2}
                hostiles = [{**zombie, "blocked_by_wall": situation == "wall"}]
                if situation == "second_hostile":
                    hostiles.append({"id": 300236, "name": "skeleton", "dist": 4.0})
                return {"entities": hostiles}
            if action == "attack":
                attacks.append(body)
                if situation == "timeout":
                    raise httpx.ReadTimeout("strike reply went missing")
                return {"hits": 1, "killed": True}
            if action == "stop":
                stops.append(action)
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(CornerBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_try_emergency_bunker", lambda _status: None)

    def flee(*_args, **_kwargs):
        if situation == "water":
            status.update(in_water=True)
        if situation == "escaped":
            return {"ok": True, "mode": "flee", "moved_dist": 5.1}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}

    monkeypatch.setattr(guard.combat_env, "flee", flee)
    incident = guard.poll()

    if situation in {"lone", "timeout"}:
        attack, = attacks
        assert attack["entity_id"] == zombie["id"] and attack["max_hits"] == 1 and attack["chase"] is False
        assert incident["actions"][-1]["action"] == "cornered_melee_strike"
        assert incident["actions"][-1]["ok"] is (situation == "lone")
    else:
        assert attacks == []
    assert stops == (["stop"] if situation == "timeout" else [])


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


@pytest.mark.parametrize(
    "survivor_name, survivor_dist, keep_shield",
    [("skeleton", 8.6, True), ("skeleton", 13.0, False), ("husk", 1.2, True), ("husk", 4.0, False)],
)
def test_counter_kill_keeps_raised_shield_facing_a_nearby_survivor(survivor_name, survivor_dist, keep_shield):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    melee = {"id": 42, "name": "zombie", "dist": 3.5, "pos": [4.0, 72.0, 0.5]}
    survivor = {"id": 43, "name": survivor_name, "dist": survivor_dist, "pos": [0.5, 72.0, 0.5 + survivor_dist]}
    calls = []
    killed = False
    blocking = True

    class ShieldBot:
        def status(self):
            nearest = survivor if killed else melee
            return {
                "pos": [0.5, 72.0, 0.5], "health": 13.32 if killed else 16.68, "food": 17,
                "alive": True, "ready": True, "on_ground": True, "blocking": blocking,
                "nearest_hostile": {key: nearest[key] for key in ("id", "name", "dist")},
            }

        def call(self, action, body=None, **kwargs):
            nonlocal killed, blocking
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [survivor] if killed else [melee, survivor]}
            if action in {"attack", "look", "posture"}:
                calls.append((action, body))
            if action == "attack":
                killed = True
                return {"hits": 1, "killed": True}
            if action == "posture":
                blocking = body.get("shield", False)
            return {"ok": True}

    outcome = CombatControllerSlot().handle(
        CombatEnv(ShieldBot()), {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie"},
    )

    assert outcome["mode"] == "counter_killed"
    assert [body["entity_id"] for action, body in calls if action == "attack"] == [42]
    if keep_shield:
        assert calls[-2:] == [("look", {"entity_id": 43}), ("posture", {"shield": True, "sneak": False, "jump": False})]
        assert outcome["latched_guard"] is True and blocking
    else:
        assert calls[-1] == ("posture", {})
        assert outcome["latched_guard"] is False and not blocking


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
                # A pathfinder can return successfully from the liquid at the edge of its tolerance.
                st["pos"] = [pl["x"] - pl["range"], pl["y"], pl["z"]]
                st["in_lava"] = st["pos"][0] - 0.3 < 229
                assert kwargs["timeout_s"] == 2.0 and kwargs["allow_reauth"] is False
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


@pytest.mark.parametrize("covered", [False, True])
def test_tool_boundary_guards_an_exposed_two_block_drop_into_lava(covered):
    calls = []
    lava = [-1869, 34, -3689]

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [-1868.5, 36, -3687.1], "dimension": "the_nether", "health": 12.76,
                        "food": 20, "on_ground": True, "alive": True, "ready": True, "blocking": True}
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "basalt", "count": 4}]}
            if action == "get_block":
                x, y, z = body["pos"]
                if body["pos"] == lava:
                    return {"name": "lava"}
                if x == lava[0] and z == lava[2] and y == 35 and not covered:
                    return {"name": "air"}
                return {"name": "netherrack" if y < 36 else "air"}
            calls.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    note = guard.stabilize_after_tools()

    if covered:
        assert calls == [] and note is None
    else:
        assert calls == [
            ("posture", {"sneak": True, "shield": True, "jump": False}),
            ("place", {"item": "basalt", "pos": lava}),
        ]
        assert note is not None and "岩浆边潜行封边" in note


@pytest.mark.parametrize("outcome", ["dry", "fell", "respawned", "stale_target", "distant_target"])
def test_tool_boundary_checks_the_reported_lava_flow_above_the_ledge(monkeypatch, outcome):
    status = {
        "pos": [17086.5, 69, 29993.5], "dimension": "overworld", "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "in_lava": False,
    }
    flow = [17087, 71, 29992] if outcome != "distant_target" else [17090, 71, 29992]
    lower_lava = [17087, 68, 29993]
    actions = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                pos = body["pos"]
                lava = pos == lower_lava or (pos == flow and outcome != "stale_target")
                return {"name": "lava" if lava else "air"}
            if action == "place":
                actions.append(body["pos"])
                if body["pos"] == flow and outcome in {"fell", "respawned"}:
                    status.update(in_lava=True, on_ground=False)
                    if outcome == "respawned":
                        status["last_death"] = {"t": 1}
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def escape():
        actions.append("escape")
        status.update(in_lava=False, on_ground=True)
        return {"ok": True}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    receipt = json.dumps({
        "ok": False, "error": {"code": "interact_flowing_fluid", "block": "lava", "target": flow, "level": 4},
    })

    note = guard.stabilize_after_tools([receipt])

    assert actions == {
        "dry": [flow, lower_lava], "fell": [flow, "escape"], "respawned": [flow],
        "stale_target": [lower_lava], "distant_target": [lower_lava],
    }[outcome]
    assert ("封边后脱离岩浆" in note) is (outcome == "fell")


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


def test_open_water_outside_geofence_can_swim_toward_the_safe_radius():
    from mc_agent.harness.geofence import VoidGeofence
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    geofence = VoidGeofence(center_x=-11029.5, center_z=-11008.5, radius=220, margin=8)
    status = {"pos": [-11016.5, 62.2, -10782.8], "in_water": True, "on_ground": False,
              "alive": True, "ready": True, "yaw": math.pi}
    moves = []
    swims = []

    class WaterBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "water"}
            if action == "move_to":
                assert geofence.check_point(body["x"], body["z"]) is None
                moves.append(body)
                raise ApiError("path_not_found", "pathfinder cannot leave the water")
            if action == "control" and body.get("forward"):
                x, y, z = status["pos"]
                dx, dz = yaw_to_unit_xz(body["yaw"])
                end = [x + 5 * dx, y, z + 5 * dz]
                assert geofence.distance_from_center(end[0], end[2]) < geofence.distance_from_center(x, z)
                swims.append(end)
                status["pos"] = end
            return {"ok": True}

    result = CombatEnv(WaterBot(), geofence=geofence).escape_deep_water_to_shore()

    assert len(moves) == 1 and len(swims) == 1
    assert result["pos_after"] == swims[0]
    assert result["in_water"] is True
    assert "surface_retreat_geofence_blocked" not in result["actions"]


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
            if action == "stop" or (action == "control" and pl == {"wait": False}):
                assert kwargs["timeout_s"] <= 0.5
                assert kwargs["allow_reauth"] is False
            if action == "control":
                # First swim call simulates transient ReadTimeout; jump-sprint launches mid-air above lava!
                if pl.get("ms") in {950, 1400}:
                    assert pl["ms"] / 1000 < kwargs["timeout_s"] <= 2.0
                    assert kwargs["allow_reauth"] is False
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


@pytest.mark.parametrize("depth", ["deep", "shallow"])
def test_overworld_lava_poll_uses_water_only_after_reaching_the_surface(depth):
    status = {
        "pos": [11105.2, 66 if depth == "deep" else 67, 11111.4], "health": 10, "food": 20,
        "dimension": "overworld", "ready": True, "alive": True, "in_lava": True, "on_ground": False,
    }
    commands = []

    class PoolBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "water_bucket", "count": 1}]}
            if action == "events":
                return {"events": []}
            if action == "get_block":
                x, y, z = body["pos"]
                return {"name": "lava" if (x, z) == (11105, 11111) and y <= 67 else "air"}
            if action == "control":
                assert body["jump"] is True and body["forward"] is False
                commands.append("swim")
                status["pos"] = [11105.2, 67, 11111.4]
                return {"ok": True}
            if action == "interact":
                commands.append("water")
                assert body == {"pos": [11105, 66, 11111], "item": "water_bucket"}
                status.update(in_lava=False, in_water=True, on_ground=True)
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(PoolBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    escape, = guard.poll()["actions"]

    assert commands == (["swim", "water"] if depth == "deep" else ["water"])
    assert escape["action"] == "escape_lava" and escape["ok"]


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


@pytest.mark.parametrize("bank_state", ["dry", "flooded", "new_life", "old_bank", "healthy"])
def test_critical_lava_ascent_aims_for_a_recent_verified_dry_bank(monkeypatch, bank_state):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    bank = [-9984.5, -54, 2972.5]
    status = {
        "pos": [-9986.4, -55.3, 2971.5], "health": 16 if bank_state == "healthy" else 10,
        "dimension": "overworld", "last_death": {"t": 2}, "ready": True, "alive": True,
        "on_ground": False, "in_lava": True,
    }
    swims = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (-9987, 2971) and y <= -54:
                    return {"name": "lava"}
                if (x, y, z) == (-9985, -55, 2972):
                    return {"name": "stone"}
                if (x, y, z) == (-9985, -54, 2972) and bank_state == "flooded":
                    return {"name": "lava"}
                return {"name": "air"}
            if action == "control" and body.get("jump"):
                swims.append(body["forward"])
                if body["forward"]:
                    destination = bank if len(swims) == 1 else [-9989.5, -53, 2971.5]
                    status.update(pos=destination, on_ground=True, in_lava=False)
                else:
                    status.update(pos=[-9986.4, -53.8, 2971.5])
            return {"ok": True}

    env = CombatEnv(LavaBot())
    env.remember_dry_bank({
        **status, "pos": bank, "on_ground": True, "in_lava": False,
        "last_death": {"t": 1 if bank_state == "new_life" else 2},
    })
    if bank_state == "old_bank":
        clock[0] += 31
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *_a, **_kw: {"best_yaw": 0.0})

    result = env.escape_lava()

    assert result["ok"] and not result["in_lava"]
    assert swims == ([True] if bank_state == "dry" else [False, True])
    assert ("lava_swim_to_recent_dry_bank" in result["actions"]) is (bank_state == "dry")


@pytest.mark.parametrize("bank", ["unsupported", "still_blocked", "clear_after_dig", "obsidian", "crying_obsidian"])
def test_lava_notch_checks_bank_support_and_actual_clearance(bank):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64.3, 0.5], "dimension": "the_nether", "health": 12,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    blocks = {(0, 64, 0): "lava"}
    if bank != "unsupported":
        blocks.update({(0, 64, -1): "netherrack", (0, 65, -1): "netherrack"})
    hard_block = bank in {"obsidian", "crying_obsidian"}
    if hard_block:
        blocks[(0, 65, -1)] = bank
        blocks.update({(0, 64, 1): "netherrack", (0, 65, 1): "netherrack"})
    digs = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "dig":
                pos = tuple(body["pos"])
                digs.append(pos)
                if bank == "clear_after_dig" or (hard_block and pos == (0, 65, 1)):
                    blocks.pop(pos)
            if action == "control" and body.get("forward") and digs:
                if hard_block and digs[-1] == (0, 65, 1):
                    status.update(pos=[0.5, 65, 1.5], in_lava=False, on_ground=True)
                elif bank == "clear_after_dig":
                    status.update(pos=[0.5, 65, -0.5], in_lava=False, on_ground=True)
            return {"ok": True}

    escaped = CombatEnv(LavaBot()).escape_lava()

    if hard_block:
        assert escaped["ok"] and "lava_notch_south" in escaped["actions"]
        assert "lava_notch_hard_block" in escaped["actions"]
        assert digs == [(0, 65, 1)]
        return
    assert escaped["ok"] is (bank == "clear_after_dig")
    assert ("lava_notch_north" in escaped["actions"]) is (bank == "clear_after_dig")
    assert digs == ([] if bank == "unsupported" else [(0, 65, -1)])
    assert not any(a.startswith("lava_notch_") for a in escaped["actions"] if a != "lava_notch_north")


@pytest.mark.parametrize("retry", ["tool_changed", "new_bank", "expired"])
@pytest.mark.parametrize("timeout_kind", ["transport", "server"])
def test_lava_notch_skips_recent_timeout_until_next_viable_attempt(monkeypatch, retry, timeout_kind):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3566.3, 64.4, 1794.7], "dimension": "the_nether", "health": 20,
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    bank = {(3566, 64, 1793): "blackstone", (3566, 65, 1793): "blackstone"}
    digs = []
    held = None

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "obsidian", "count": 1}], "held": held}
            if action == "get_block":
                return {"name": bank.get(tuple(body["pos"]), "air")}
            if action == "dig":
                pos = tuple(body["pos"])
                digs.append(pos)
                if len(digs) == 1:
                    if timeout_kind == "server":
                        raise ApiError("timeout", "could not open the bank")
                    raise httpx.ReadTimeout("could not open the bank")
                bank.pop(pos)
            if action == "control" and body.get("forward") and len(digs) == 2:
                status.update(in_lava=False, on_ground=True, pos=[digs[-1][0] + 0.5, 65, 1793.5])
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {"best_yaw": 0.0})
    failure = env.escape_lava()
    assert "lava_notch_dig_timeout" in failure["actions"]
    assert failure["notch_dig_failures"] == [{
        "pos": [3566, 65, 1793], "block": "blackstone", "held": None,
        "error": "timeout" if timeout_kind == "server" else "ReadTimeout",
    }]

    clock[0] += 4.0
    assert "lava_notch_dig_deferred" in env.escape_lava()["actions"]
    assert digs == [(3566, 65, 1793)]

    if retry == "tool_changed":
        held = {"name": "iron_pickaxe"}
    elif retry == "new_bank":
        bank.update({(3567, 64, 1793): "blackstone", (3567, 65, 1793): "blackstone"})
        status["pos"] = [3567.3, 64.4, 1794.7]
    else:
        clock[0] += 15.0
    assert env.escape_lava()["ok"] is True
    assert digs[-1] == (3567 if retry == "new_bank" else 3566, 65, 1793)


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


@pytest.mark.parametrize("landing", ["cornered", "short", "moved", "clear", "respawned"])
@pytest.mark.parametrize("trailing_event", [None, "unattributed", "arrow"])
def test_poll_refaces_a_close_melee_attacker_with_shield_after_retreat(monkeypatch, landing, trailing_event):
    attacker = {"id": 77, "name": "wither_skeleton", "dist": 3.1, "pos": [3602.5, 64, 1817.6]}
    status = {"pos": [3599.4, 64, 1817.6], "dimension": "the_nether", "health": 14.4,
              "ready": True, "alive": True, "on_ground": True, "blocking": True, "food": 17,
              "last_death": {"t": 1}, "nearest_hostile": attacker}
    looks = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurts = [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": attacker}]
                if trailing_event == "unattributed":
                    hurts += [{"seq": 2, "type": "hurt", "source": "wither"}, {"seq": 3, "type": "hurt"}]
                elif trailing_event == "arrow":
                    hurts += [{"seq": 2, "type": "hurt", "source": "arrow"}, {"seq": 3, "type": "hurt"}]
                return {"events": [] if kwargs["params"]["since"] else hurts}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "look":
                looks.append(body)
            assert action != "posture"
            return {"ok": True}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def retreat(_threat_pos, *, wait_for_ground):
        status.update(health=9.56, nearest_hostile={"id": 78, "name": "wither_skeleton", "dist": 5 if landing == "clear" else 2.2})
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        if landing in {"short", "moved"}:
            return {"ok": landing == "moved", "mode": "flee", "moved_dist": 1.2 if landing == "short" else 2.64}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}

    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    incident = guard.poll()

    actions = [a["action"] for a in incident["actions"]]
    should_face = trailing_event != "arrow" and landing in {"cornered", "short", "moved"}
    assert actions == (["combat_or_flee", "face_attacker_after_retreat"] if should_face else ["combat_or_flee"])
    assert looks == ([{"entity_id": 78}] if should_face else [])


@pytest.mark.parametrize(
    "recent_arrow, hp_before, hp_after, blocking, shooter_dist, expected_action",
    [
        (False, 3.34, 2.55, True, None, None),
        (False, 3.03, 0.96, False, None, "raise_shield_after_retreat"),
        (False, 3.03, 5.3, False, None, None),
        (True, 3.34, 2.55, True, None, "face_attacker_after_retreat"),
        (False, 8.7, 1.63, True, None, "face_attacker_after_retreat"),
        (False, 8.7, 6.2, True, None, None),
        (True, 7.4, 1.3, False, 9.5, "raise_shield_after_retreat"),
        (True, 7.4, 1.3, False, 14.0, None),
        (False, 7.4, 1.3, False, 9.5, None),
        (True, 7.4, 5.3, False, 9.5, None),
    ],
)
def test_poll_checks_shield_after_surface_melee_retreat(
    monkeypatch, recent_arrow, hp_before, hp_after, blocking, shooter_dist, expected_action,
):
    front = {"id": 44, "name": "zombie", "dist": 2.5, "pos": [9250.0, 64, -11469.5]}
    flank = {"id": 55, "name": "zombie", "dist": 1.9, "pos": [9249.8, 64, -11469.6]}
    if shooter_dist is not None:
        flank = {"id": 55, "name": "skeleton", "dist": shooter_dist}
    status = {
        "pos": [9252.5, 64, -11469.5], "health": hp_before, "food": 17, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": blocking, "nearest_hostile": front,
        "offhand": {"name": "shield"},
    }
    looks = []
    postures = []

    class SurfaceBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if kwargs["params"]["since"]:
                    return {"events": []}
                hits = [{"seq": 2, "type": "hurt", "source": "mob_attack", "source_entity": front}]
                if recent_arrow:
                    hits.insert(0, {"seq": 1, "type": "hurt", "source": "arrow"})
                return {"events": hits}
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "look":
                looks.append(body)
            if action == "posture":
                postures.append(body)
                status["blocking"] = body.get("shield") is True
                return {"blocking": status["blocking"]}
            return {"ok": True}

    guard = GenerationGuard(SurfaceBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def retreat(*args, **kwargs):
        status.update(pos=[9250.1, 64, -11467.9], health=hp_after, nearest_hostile=flank)
        return {"ok": True, "mode": "flee", "moved_dist": 2.26}

    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    incident = guard.poll()

    expected = ["combat_or_flee", *([expected_action] if expected_action else [])]
    assert [action["action"] for action in incident["actions"]] == expected
    assert looks == ([{"entity_id": 55}] if expected_action else [])
    assert postures == (
        [{"shield": True, "jump": False, "sneak": False}] if expected_action == "raise_shield_after_retreat" else []
    )


@pytest.mark.parametrize("distance", [2.2, 5.0])
def test_poll_raises_main_hand_shield_when_cornered_by_nether_melee(monkeypatch, distance):
    attacker = {"id": 77, "name": "wither_skeleton", "dist": distance, "pos": [-417.8 + distance, 53, -1880.3]}
    status = {
        "pos": [-417.8, 53, -1880.3], "dimension": "the_nether", "health": 2.48,
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "food": 20,
        "nearest_hostile": attacker,
    }
    shield = {"name": "shield", "count": 1}
    commands = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": attacker}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [shield, {"name": "iron_sword", "count": 1}], "held": shield, "offhand": None}
            commands.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", lambda *args, **kwargs: {
        "ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0,
    })

    incident = guard.poll()

    actions = ["combat_or_flee"]
    if distance <= 3.5:
        actions += ["equip_shield", "face_attacker", "raise_shield"]
        assert commands == [
            ("equip", {"item": "shield", "slot": "off-hand"}),
            ("look", {"entity_id": 77}),
            ("posture", {"shield": True, "jump": False, "sneak": False}),
        ]
    else:
        assert commands == []
    assert [action["action"] for action in incident["actions"]] == actions


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("nearby", [False, True])
def test_poll_follows_unarmed_retreat_when_close_melee_threat_remains(monkeypatch, nearby, complete):
    attacker = {"id": 77, "name": "spider", "dist": 3.1, "pos": [5003.5, 39, -7590.8]}
    status = {"pos": [5000.4, 39, -7591.1], "dimension": "overworld", "health": 15.5, "food": 20,
              "ready": True, "alive": True, "on_ground": True, "nearest_hostile": attacker}
    followups = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": attacker}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [{**attacker, "pos": [5002, 39, -7591.4]}]}
            return {"ok": True}

    def flee(*args, **kwargs):
        status.update(pos=[5004.3, 39, -7591.4], health=6.7, nearest_hostile={
            "id": 77, "name": "spider", "dist": 2.3 if nearby else 5.0,
        })
        return {"ok": complete, "mode": "flee", "moved_dist": 5.94 if complete else 1.39}

    def step(landing, target, *, deadline):
        followups.append((landing["health"], target))
        return True

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    monkeypatch.setattr(guard.combat_env, "step_away_from_melee", step)
    incident = guard.poll()

    actions = [a["action"] for a in incident["actions"]]
    assert actions == (["combat_or_flee", "followup_melee_retreat"] if nearby else ["combat_or_flee"])
    assert followups == ([(6.7, [5002, 39, -7591.4])] if nearby else [])


@pytest.mark.parametrize("threat", ["clear", "nearest_drowned", "other_nearest", "scan_timeout"])
def test_recent_trident_hit_defers_food_until_the_projectile_threat_passes(monkeypatch, threat):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [9261.3, 64, -11343.7], "health": 5.07, "food": 17,
        "dimension": "overworld", "on_ground": True, "alive": True, "ready": True, "blocking": False,
    }
    hits = [{"seq": 1, "type": "hurt", "source": "trident"}]
    dishes = []
    drowned = {"id": 5922198, "name": "drowned", "dist": 8.5}
    if threat == "nearest_drowned":
        status["nearest_hostile"] = drowned
    if threat == "other_nearest":
        status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 7.0}

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 2}]}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.5
                if threat == "scan_timeout":
                    raise httpx.ReadTimeout("hostile scan unavailable")
                return {"entities": [drowned] if threat == "other_nearest" else []}
            if action == "use_item":
                dishes.append(body["item"])
            return {"ok": True}

    bot = RiverBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()

    def eat():
        return json.dumps(bot.call("use_item", {"item": "cooked_beef"}))

    result = json.loads(guard.run_tool_with_reflex("mc_use_item", eat))
    assert result["error"]["code"] == "unsafe_to_eat"
    assert guard.poll() is None and dishes == []

    clock[0] += 8.1
    if threat != "clear":
        result = json.loads(guard.run_tool_with_reflex("mc_use_item", eat))
        assert result["error"]["code"] == "unsafe_to_eat"
        assert guard.poll() is None and dishes == []
        clock[0] += 22.0
    assert guard.poll()["actions"][0]["action"] == "eat"
    assert dishes == ["cooked_beef"]


@pytest.mark.parametrize("pressure", ["missing_shooter", "other_nearest", "melee_only", "new_life"])
def test_low_health_poll_defers_food_after_attack_read_timeout_under_arrow_fire(monkeypatch, pressure):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 51, "name": "zombie", "pos": [2.5, 71, 0.5], "dist": 2.0}
    status = {
        "pos": [0.5, 71, 0.5], "health": 7.67, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "nearest_hostile": zombie,
    }
    events = []
    commands = []

    class PortalBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "cooked_beef", "count": 1}]}
            if action in {"stop", "use_item"}:
                commands.append(action)
                return {"ok": True}
            raise AssertionError(action)

    log = []
    guard = GenerationGuard(PortalBot(), EventSink(log.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})

    def attack(_env, trigger):
        assert trigger["primary_entity_id"] == zombie["id"]
        events.append({"seq": 1, "type": "hurt", "source": "mob_attack" if pressure == "melee_only" else "arrow"})
        status["nearest_hostile"] = {**zombie, "dist": 9.0} if pressure == "other_nearest" else None
        raise httpx.ReadTimeout("attack reply timed out", request=httpx.Request("POST", "http://localhost/attack"))

    monkeypatch.setattr(guard.combat_slot, "handle", attack)
    first = guard.poll()
    assert first["event_seqs"] == [1]
    assert first["actions"][0]["error"]["code"] == "network_uncertain"
    assert commands == ["stop"]
    assert any(event.get("endpoint") == "attack" for event in log)

    clock[0] += 3.1
    if pressure == "new_life":
        events.append({"seq": 2, "type": "spawn"})
        status["last_death"] = {"t": 2}
        assert "death_or_connection_changed" in guard.poll()["reasons"]
        clock[0] += 3.1
    status["food"] = 19
    incident = guard.poll()
    if pressure in {"missing_shooter", "other_nearest"}:
        assert incident is None and commands == ["stop"]
        clock[0] += 5.0
        incident = guard.poll()
    assert incident["actions"][0]["action"] == "eat"
    assert commands == ["stop", "use_item"]


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


@pytest.mark.parametrize(
    "health, distance_after, alternate_shooter, keep_shield",
    [(7.0, 11.6, False, True), (7.0, 16.0, False, False), (13.28, 1.9, True, True), (13.28, 4.0, True, False)],
)
def test_retreat_keeps_shield_against_a_nearby_shooter(health, distance_after, alternate_shooter, keep_shield):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    pos = [0.5, 64.0, 0.5]
    shooter = {"id": 77, "name": "skeleton", "pos": [9.6, 64.0, 0.5], "dist": 9.1}
    posture_calls = []

    class RetreatBot:
        def status(self):
            return {"pos": list(pos), "health": health, "food": 20, "alive": True, "ready": True,
                    "on_ground": True, "blocking": True, "nearest_hostile": dict(shooter)}

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
                if alternate_shooter:
                    shooter.update(id=88, name="blaze")
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
            ("look", {"entity_id": 88 if alternate_shooter else 77}),
            ("posture", {"shield": True, "sneak": False, "jump": False}),
        ]
    else:
        assert posture_calls[-1] == ("posture", {})


@pytest.mark.parametrize("landing", ["near_melee", "near_shooter", "clear", "respawned"])
@pytest.mark.parametrize("encounter", ["surface_melee", "shaft_melee", "shaft_ranged"])
def test_short_retreat_keeps_shield_facing_a_close_melee_enemy(monkeypatch, landing, encounter):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    failed_shaft_seal = encounter != "surface_melee"
    shaft_ranged = encounter == "shaft_ranged"
    mob = "skeleton" if shaft_ranged else "wither_skeleton" if failed_shaft_seal else "husk"
    primary = {"id": 77, "name": mob, "pos": [1.3, 69, 0.5], "dist": 0.8}
    status = {
        "pos": [0.5, 69, 0.5], "health": 20.0 if failed_shaft_seal else 7.0, "food": 17, "ready": True, "alive": True,
        "on_ground": True, "blocking": True, "last_death": {"t": 1}, "nearest_hostile": primary,
    }
    posture_calls = []

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                items = [{"name": "cobblestone", "count": 64}] if failed_shaft_seal else []
                return {"items": items, "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [primary]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 69 else "air"}
            if action in {"look", "posture"}:
                posture_calls.append((action, body))
            if action == "posture":
                status["blocking"] = body.get("shield", False)
            return {"ok": True}

    def short_retreat(*args, **kwargs):
        status.update(pos=[-1.68, 69, 0.5], health=4.84)
        status["nearest_hostile"] = {
            "id": 78, "name": "husk" if shaft_ranged else mob, "dist": 2.3 if landing != "clear" else 4.0,
        }
        if shaft_ranged:
            status["blocking"] = False
        if landing == "near_shooter":
            status["nearest_hostile"].update(name="skeleton", dist=3.4)
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        return {"ok": not failed_shaft_seal, "mode": "flee", "moved_dist": 2.18}

    env = CombatEnv(RetreatBot())
    monkeypatch.setattr(env, "flee", short_retreat)
    if failed_shaft_seal:
        monkeypatch.setattr(env, "arena_snapshot", lambda *args: {"enclosure": "carved_shaft"})
        monkeypatch.setattr(env, "build_tactical", lambda *args, **kwargs: {"ok": False, "reason": "no_safe_seal"})
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_ranged" if shaft_ranged else "pre_hurt_melee", "primary_entity_id": 77, "primary_type": mob},
    )

    if failed_shaft_seal:
        assert {"action": "seal_shaft", "ok": False, "reason": "no_safe_seal"} in outcome["actions"]
        assert {"action": "flee", "ok": False, "moved": 2.18} in outcome["actions"]
    should_latch = landing == "near_melee" or (landing == "near_shooter" and encounter != "shaft_melee")
    assert outcome["latched_guard"] is should_latch
    if should_latch and landing == "near_shooter":
        action = "guard_ranged_after_failed_seal" if shaft_ranged else "guard_ranged_after_melee_retreat"
        assert {"action": action, "ok": True, "entity_id": 78} in outcome["actions"]
    if should_latch:
        assert posture_calls[-2:] == [("look", {"entity_id": 78}), ("posture", {"shield": True, "sneak": False, "jump": False})]
        assert status["blocking"] is True
    else:
        assert posture_calls[-1] == ("posture", {})


@pytest.mark.parametrize("hostile_still_close", [True, False])
def test_seal_during_melee_retreat_reports_cover_and_keeps_shield_if_needed(monkeypatch, hostile_still_close):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    hostile = {"id": 77, "name": "wither_skeleton", "dist": 3.2, "pos": [3532.5, 70, 1796.7]}
    status = {
        "pos": [3532.5, 70, 1793.5], "health": 20, "food": 17, "ready": True, "alive": True,
        "on_ground": True, "blocking": False, "dimension": "the_nether", "nearest_hostile": hostile,
    }

    class FortressBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [hostile]}
            if action == "posture":
                status["blocking"] = body.get("shield", False)
            return {"ok": True}

    env = CombatEnv(FortressBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "open_cave"})

    def seal_while_retreating(*_args, **_kwargs):
        if not hostile_still_close:
            status["nearest_hostile"] = None
        return {"ok": True, "mode": "sealed", "moved_dist": 0.0, "pos_after": status["pos"]}

    monkeypatch.setattr(env, "flee", seal_while_retreating)
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": hostile["id"], "primary_type": hostile["name"]},
    )

    assert outcome["mode"] == "sealed_shaft"
    assert {"action": "seal_shaft", "ok": True} in outcome["actions"]
    assert not any(action["action"] == "flee" for action in outcome["actions"])
    assert outcome["latched_guard"] is hostile_still_close
    assert status["blocking"] is hostile_still_close


@pytest.mark.parametrize("landing", ["pursued", "different_mob", "too_far", "behind_wall", "ordinary_retreat"])
def test_melee_retreat_keeps_the_shield_up_when_a_point_blank_attacker_pursues(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    zombie = {"id": 77, "name": "zombie", "dist": 2.0 if landing == "ordinary_retreat" else 0.3}
    status = {
        "pos": [-6996.5, 120, 5825.5], "health": 17.2, "food": 20, "ready": True, "alive": True,
        "dimension": "overworld", "on_ground": True, "blocking": False, "nearest_hostile": zombie,
    }
    poses = []

    class VillageBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [zombie]}
            if action == "posture":
                poses.append(body)
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def retreat(*_a, **_kw):
        after = {**zombie, "id": 78 if landing == "different_mob" else 77,
                 "dist": 3.6 if landing == "too_far" else 3.5, "blocked_by_wall": landing == "behind_wall"}
        status.update(pos=[-6992.6, 120, 5824.5], health=14.41, nearest_hostile=after)
        return {"ok": True, "mode": "flee", "moved_dist": 5.18}

    env = CombatEnv(VillageBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_a: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {"source": "pre_hurt_melee", "primary_entity_id": 77})

    assert outcome["latched_guard"] is (landing == "pursued")
    assert status["blocking"] is (landing == "pursued")
    if landing == "pursued":
        assert outcome["actions"][-1] == {"action": "guard_melee_after_retreat", "ok": True, "entity_id": 77}
    else:
        assert poses[-1] == {}


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


@pytest.mark.parametrize("second_shooter", [False, True])
def test_unshielded_boundary_retreat_accounts_for_a_second_shooter(second_shooter):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [0.5, 7.0, 0.5]
    primary = {"id": 77, "name": "skeleton", "pos": [8.2, 7.0, 0.5], "dist": 7.7}
    secondary = {"id": 78, "name": "skeleton", "pos": [-7.5, 7.0, 0.5], "dist": 8.0}
    pos = list(origin)
    yaw = 0.0

    class CaveBot:
        def status(self):
            return {"pos": list(pos), "health": 7, "food": 20, "ready": True,
                    "alive": True, "on_ground": True, "nearest_hostile": dict(primary)}

        def call(self, action, body=None, **kwargs):
            nonlocal yaw
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}], "offhand": {"name": "iron_pickaxe"}}
            if action == "get_entities":
                return {"entities": [primary, secondary] if second_shooter else [primary]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 7 else "air"}
            if action == "look" and "yaw" in body:
                yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                ux, uz = yaw_to_unit_xz(yaw)
                pos[0] += 2.5 * ux
                pos[2] += 2.5 * uz
            if action == "move_to":
                pos[:] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    outcome = CombatControllerSlot().handle(
        CombatEnv(CaveBot()), {"source": "boundary_threat", "primary_entity_id": 77, "primary_type": "skeleton"},
    )

    assert outcome["actions"][0]["action"] == "flee"
    assert outcome["actions"][0]["ok"] is True
    if second_shooter:
        assert abs(pos[2] - origin[2]) > 2.0
        assert math.dist(pos, secondary["pos"]) >= secondary["dist"] - 0.3
    else:
        assert pos == pytest.approx([-2.0, 7.0, 0.5])


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize(("dimension", "blocking"), [("overworld", True), ("minecraft:the_nether", False)])
def test_recent_melee_damage_defers_eating(entry, dimension, blocking, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    actions = []

    class ShieldBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [814.2, 78, -850.7], "health": 12, "food": 13, "on_ground": True,
                    "blocking": blocking, "dimension": dimension, "ready": True, "alive": True,
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
    expected = [("posture", {"shield": False})] if blocking else []
    assert actions == [*expected, ("use_item", {"item": "cooked_beef"})]


@pytest.mark.parametrize("bridge_probe", ["normal", "timeout", "respawned"])
def test_lava_terrain_probe_reserves_time_for_a_bridge_when_swimming_stalls(monkeypatch, bridge_probe):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {"pos": [3183.5, 64.4, -2045.3], "dimension": "the_nether", "health": 20,
              "in_lava": True, "on_ground": False, "alive": True, "ready": True}
    actions = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            if action == "get_block":
                timeout = kwargs.get("timeout_s", 0.5)
                if not actions:
                    clock[0] += timeout
                elif bridge_probe != "normal" and status["in_lava"]:
                    assert kwargs["allow_reauth"] is False and timeout <= 0.35
                    clock[0] += timeout
                    if bridge_probe == "respawned":
                        status.update(pos=[-415.5, 64.1, -1887.5], health=20, in_lava=False,
                                      ready=False, last_death={"t": 2})
                    raise httpx.ReadTimeout("bridge block probe stalled")
                y = body["pos"][1]
                return {"name": "lava" if y == 64 else "air"}
            if action == "find_blocks":
                clock[0] += kwargs.get("timeout_s", 2.0)
                return {"blocks": []}
            if action == "control" and body.get("forward"):
                actions.append(("swim", clock[0]))
                clock[0] += body["ms"] / 1000
            if action in {"control", "place"} and status["ready"] is False:
                pytest.fail(f"Unexpected {action} after respawn during the bridge probe")
            if action == "place":
                assert kwargs["allow_reauth"] is False and 0 < kwargs["timeout_s"] <= 0.7
                actions.append(("bridge", clock[0]))
                status.update(in_lava=False, on_ground=True, pos=[3183.5, 65.0, -2045.3])
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()

    assert ("lava_bridge_probe_incomplete" in result["actions"]) is (bridge_probe != "normal")
    if bridge_probe == "respawned":
        assert result["ok"] is False and result["respawned"] is True
        assert result["pos_after"] == status["pos"]
        assert [action for action, _ in actions] == ["swim"]
        return

    assert result["ok"] is True and result["in_lava"] is False
    assert [action for action, _ in actions] == ["swim", "bridge"]
    assert actions[-1][1] < 1005.0


@pytest.mark.parametrize("placement_works", [False, True])
def test_critical_lava_escape_attempts_bridge_before_swimming_in_an_unverified_direction(monkeypatch, placement_works):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    status = {"pos": [3183.5, 66.2, -2048.5], "dimension": "the_nether", "health": 8,
              "in_lava": True, "on_ground": False, "alive": True, "ready": True}
    actions = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "place":
                actions.append("bridge")
                if not placement_works:
                    raise ApiError("no_support", "no adjacent support")
                status.update(in_lava=False, on_ground=True, pos=[3183.5, 67, -2048.5])
            if action == "control" and body.get("forward"):
                actions.append("swim")
            return {"ok": True}

    result = CombatEnv(LavaBot()).escape_lava()

    assert actions[0] == "bridge"
    assert result["ok"] is placement_works
    if placement_works:
        assert actions == ["bridge"]
    else:
        assert actions[-1] == "swim"


@pytest.mark.parametrize("transport_overruns", [False, True])
def test_lava_guard_deadline_includes_last_resort_notch_dig(monkeypatch, transport_overruns):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3186.7, 64.6, -2046.8], "health": 13, "food": 20, "alive": True, "ready": True,
        "in_lava": True, "on_ground": False, "dimension": "the_nether",
    }
    calls = []
    events = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            timeout = kwargs.get("timeout_s")
            calls.append((action, clock[0], timeout))
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                return {"events": []}
            if action == "get_block":
                return {"name": "netherrack"}
            if action == "control" and body.get("forward"):
                clock[0] += body["ms"] / 1000
            if action == "dig":
                if transport_overruns:
                    clock[0] = 1000.0 + guard.LAVA_ESCAPE_BUDGET_S + 0.000001
                else:
                    clock[0] += timeout + 0.000001
                raise httpx.ReadTimeout("notch dig exceeded its transport budget")
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "scan_safe_yaws", lambda *args, **kwargs: {"best_yaw": 0.0})
    incident = guard.poll()

    escape = incident["actions"][0]
    assert escape["action"] == "escape_lava"
    digs = [(started, timeout) for action, started, timeout in calls if action == "dig"]
    assert digs and all(0 < timeout <= 1.0 and started + timeout <= 1004.3 for started, timeout in digs)
    if transport_overruns:
        assert escape["result"] == {"ok": False, "in_lava": True, "reason": "time_budget", "partial": True}
        budget_event, = [e for e in events if e["type"] == "guardian.lava_budget_exceeded"]
        assert budget_event["endpoint"] == "dig" and budget_event["budget_s"] == guard.LAVA_ESCAPE_BUDGET_S
        assert any(action == "stop" and timeout == 1.5 for action, _, timeout in calls)
    else:
        assert escape["result"]["ok"] is False
        assert "lava_notch_dig_timeout" in escape["result"]["actions"]
        assert clock[0] < 1000.0 + guard.LAVA_ESCAPE_BUDGET_S
        assert any(action == "stop" and timeout == 0.25 for action, _, timeout in calls)
    guard.bot.status()
    assert calls[-1][2] == 2.0


@pytest.mark.parametrize("west_blocked", [False, True])
@pytest.mark.parametrize("height_change", [0, 1.6])
def test_lava_escape_changes_unverified_swim_heading_after_stalling(monkeypatch, west_blocked, height_change):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3184.7, 67.2, -2048.3], "yaw": 0.0, "health": 20,
        "dimension": "the_nether", "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    swim_headings = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                return {"name": "lava" if body["pos"][1] <= math.floor(status["pos"][1]) else "air"}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                swim_headings.append(status["yaw"])
                if status["yaw"] != 0:
                    x = 3182.5 if status["yaw"] > 0 else 3186.9
                    status.update(in_lava=False, on_ground=True, pos=[x, 67, -2048.3])
            return {"ok": True}

    env = CombatEnv(LavaBot())
    if west_blocked:
        monkeypatch.setattr(env, "_point_allowed", lambda x, _z, **_kwargs: x >= 3184.7)
    assert env.escape_lava()["in_lava"] is True
    status["pos"] = [3184.7, 67.2 + height_change, -2048.3]
    clock[0] += 3.0
    escaped = env.escape_lava()

    assert escaped["ok"] is True
    assert swim_headings == pytest.approx([0.0, (-1 if west_blocked else 1) * math.pi / 2])


@pytest.mark.parametrize("lands_after_jump", [False, True])
def test_lava_shore_hop_checks_landing_before_requesting_pathfinder(monkeypatch, lands_after_jump):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    bank = [3568.5, 65, 1794.5]
    status = {
        "pos": [3567.3, 64.5, 1794.7], "health": 7, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "alive": True, "ready": True,
    }
    calls = []

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                return {"name": "air"}
            if action == "control" and body.get("forward"):
                status.update(in_lava=False, pos=[3568.4, 65.3, 1794.5])
            if action == "status":
                calls.append("verify_landing")
                if lands_after_jump:
                    status.update(on_ground=True, pos=bank)
                return self.status()
            if action == "move_to":
                calls.append(action)
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {})
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *args, **kwargs: bank)
    result = env.escape_lava()
    assert result["ok"] is lands_after_jump
    assert calls == (["verify_landing"] if lands_after_jump else ["verify_landing", "move_to"])
    if not lands_after_jump:
        assert result["reason"] == "lava_landing_unconfirmed"


@pytest.mark.parametrize("fallback", ["bridge", "alternate_bank", "third_bank"])
@pytest.mark.parametrize("status_timeout", [False, True])
def test_lava_escape_skips_a_shore_path_that_left_the_bot_at_the_same_rim(monkeypatch, fallback, status_timeout):
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    shore = [3188.5, 66, -2046.5]
    status = {
        "pos": [3186.7, 64.6, -2046.8], "health": 20, "alive": True, "ready": True,
        "in_lava": True, "on_ground": False, "dimension": "the_nether",
    }
    calls = []
    can_bridge = False
    headings = []
    check_first_path = False

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal check_first_path
            if action == "status":
                if check_first_path and status_timeout:
                    check_first_path = False
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("shore verification exceeded the recovery deadline")
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}] if can_bridge else []}
            if action == "get_block":
                return {"name": "lava" if body["pos"][1] <= 64 else "air"}
            if action == "find_blocks":
                return {"blocks": [
                    {"name": "netherrack", "pos": [3188, 65, -2047]},
                    {"name": "basalt", "pos": [3183, 64, -2047]},
                    *([{"name": "netherrack", "pos": [3186, 66, -2051]}] if fallback == "third_bank" else []),
                ]}
            if action == "look":
                headings.append(body["yaw"])
                clock[0] += 0.6
            if action == "control" and body.get("forward"):
                clock[0] += body["ms"] / 1000
            if action == "move_to":
                calls.append(action)
                if body["x"] == shore[0]:
                    # The pathfinder replies successfully before the bot leaves the lava hitbox.
                    status["pos"] = [3186.7, 64.6, -2046.2]
                    clock[0] += 1.8
                    check_first_path = True
                elif fallback == "third_bank" and body["x"] == 3183.5:
                    # A vertical rise without movement across the pool does not make a shore path viable.
                    status["pos"] = [3186.7, 65.8, -2046.2]
                else:
                    assert body["x"] == (3186.5 if fallback == "third_bank" else 3183.5)
                    status.update(in_lava=False, on_ground=True, pos=[body["x"], body["y"], body["z"]])
            if action == "place":
                calls.append(action)
                status.update(in_lava=False, on_ground=True, pos=[3186.7, 65, -2046.2])
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    env = guard.combat_env

    def scan(*args, **kwargs):
        clock[0] += 1.3
        return {}

    monkeypatch.setattr(env, "scan_safe_yaws", scan)
    if fallback == "bridge":
        monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *args, **kwargs: shore)
    first = env.escape_lava()
    assert not first["ok"]
    if status_timeout:
        assert first["reason"] == "time_budget"
    else:
        assert "lava_move_to_shore" in first["actions"]

    can_bridge = fallback == "bridge"
    second = env.escape_lava()
    if fallback == "third_bank":
        assert not second["ok"]
        assert env.escape_lava()["ok"]
        assert calls == ["move_to"] * 3
        return
    assert second["ok"]
    if fallback == "bridge":
        assert "lava_shore_path_stalled" in second["actions"]
        assert calls == ["move_to", "place"]
    else:
        assert calls == ["move_to", "move_to"]
        assert headings[0] < 0 < headings[1]


@pytest.mark.parametrize("landing", [
    "grounded", "after_jump", "after_two_checks", "after_two_edge_checks", "loses_footing",
    "underfoot", "underfoot_loses_footing",
])
def test_r20_lava_bridge_places_against_adjacent_solid_wall_when_underfoot_unsupported(monkeypatch, landing):
    """Reproduces r20_short hy4_hy_6_nether_bastion: when underfoot place fails no_support/blocked_by_self, place side bridge against bank & step onto it."""
    from mc_agent.errors import ApiError
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    bridge_state = {"in_lava": True, "on_ground": False, "pos": [249.5, 28.1, 208.5], "placed": set()}
    landing_reads = []
    brake_calls = []
    underfoot = landing.startswith("underfoot")
    loses_footing = landing.endswith("loses_footing")

    class _DeepBankLavaBot:
        def status(self):
            return {
                "pos": list(bridge_state["pos"]),
                "health": 15.0,
                "alive": True,
                "ready": True,
                "in_lava": bridge_state["in_lava"],
                "on_ground": bridge_state["on_ground"],
                "yaw": 0.0,
                "dimension": "minecraft:the_nether",
            }

        def call(self, action, body=None, **kwargs):
            pl = dict(body or {})
            if action == "status":
                if brake_calls:
                    assert kwargs["method"] == "GET" and 0 < kwargs["timeout_s"] <= 0.35
                    if loses_footing:
                        bridge_state.update(on_ground=False, pos=[251.3, 28.7, 208.5])
                elif "timeout_s" in kwargs and not bridge_state["in_lava"] and not bridge_state["on_ground"]:
                    assert kwargs["allow_reauth"] is False
                    landing_reads.append(kwargs["timeout_s"])
                    if landing not in {"after_two_checks", "after_two_edge_checks"} or len(landing_reads) == 2:
                        bridge_state.update(on_ground=True, pos=[250.5, 29.0, 208.5])
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
                    if not underfoot:
                        raise ApiError("no_support", "no solid face in open lava")
                    bridge_state.update(in_lava=False, on_ground=True, pos=[249.2, 29.0, 208.3])
                bridge_state["placed"].add(p)
                return {"ok": True}
            if action == "control" and pl.get("forward") and bridge_state["placed"]:
                after_jump = landing in {"after_jump", "after_two_checks", "after_two_edge_checks"}
                bridge_state.update(
                    in_lava=False, on_ground=not after_jump,
                    pos=[250.2 if landing == "after_two_edge_checks" else 250.5,
                         29.3 if after_jump else 29.0, 208.5],
                )
            if action == "posture":
                assert bridge_state["on_ground"] and 0 < kwargs["timeout_s"] <= 0.35
                brake_calls.append(pl)
            return {"ok": True}

    out_b = CombatEnv(_DeepBankLavaBot()).escape_lava()
    assert out_b["ok"] is (not loses_footing) and out_b["in_lava"] is False
    assert "lava_place_cobblestone" in out_b["actions"]
    assert ("lava_step_bridge" in out_b["actions"]) is (not underfoot)
    assert out_b["actions"][-1] == "lava_bridge_sneak"
    assert brake_calls == [{"sneak": True, "jump": False, "shield": False}]
    if loses_footing:
        assert out_b["reason"] == "lava_landing_unconfirmed"
    else:
        assert out_b["pos_after"] == ([249.2, 29.0, 208.3] if underfoot else [250.5, 29.0, 208.5])
    assert len(landing_reads) == {"after_jump": 1, "after_two_checks": 2, "after_two_edge_checks": 2}.get(landing, 0)
    assert all(0 < timeout <= 0.25 for timeout in landing_reads)


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

    def retreat(
        status, target, counts, *, ranged=False, prefer_cover=False, allow_stationary_cover=True,
        verify_live_shooters=False,
    ):
        assert allow_stationary_cover == (hit_source != "fireball")
        assert not verify_live_shooters
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


@pytest.mark.parametrize("incoming_magic", [False, True])
def test_indirect_magic_uses_checked_retreat_without_digging_a_bunker(monkeypatch, incoming_magic):
    hits = [{"seq": 1, "type": "hurt", "source": "magic"}]
    if incoming_magic:
        hits.extend([{"seq": 2, "type": "hurt", "source": "indirect_magic"}, {"seq": 3, "type": "hurt", "source": "magic"}])
    retreats = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [-10333.4, 15.0, -9057.6], "dimension": "overworld", "health": 13.5,
                        "food": 20, "ready": True, "alive": True, "on_ground": True}
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            pytest.fail(f"Unexpected action during magic damage recovery: {action}")

    def checked_flee(threat_pos, *, wait_for_ground):
        retreats.append(threat_pos)
        assert wait_for_ground is True
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", checked_flee)

    incident = guard.poll()

    if incoming_magic:
        assert retreats == [None]
        assert [action["action"] for action in incident["actions"]] == ["combat_or_flee"]
    else:
        assert retreats == [] and incident is None


@pytest.mark.parametrize("witch_visible", [False, True])
def test_magic_damage_with_visible_witch_retreats_from_the_witch(monkeypatch, witch_visible):
    zombie = {"id": 41, "name": "zombie", "dist": 1.9, "pos": [2.4, 71, 0.5]}
    witch = {"id": 42, "name": "witch", "dist": 7.5, "pos": [0.5, 71, -7.0], "blocked_by_wall": not witch_visible}
    status = {
        "pos": [0.5, 71, 0.5], "dimension": "overworld", "health": 16.7, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "nearest_hostile": zombie,
    }
    hits = [{"seq": 1, "type": "hurt"}, {"seq": 2, "type": "hurt", "source": "magic"}]
    retreats = []

    class MagicBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [h for h in hits if h["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [zombie, witch]}
            return {"ok": True}

    guard = GenerationGuard(MagicBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def defend(_status, attacker, _counts, *, ranged=False):
        retreats.append((attacker, ranged))
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    assert guard.poll()["requires_replan"]
    assert retreats == ([(witch, True)] if witch_visible else [(None, False)])


@pytest.mark.parametrize("ground", ["safe", "lava_drop", "slow_probe", "slow_control"])
def test_bunker_rejection_keeps_an_armed_witch_retreat_on_verified_ground(monkeypatch, ground):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [17029.1, 87, 29985.6]
    witch = {"id": 255310, "name": "witch", "dist": 9.1, "pos": [17022, 87, 29991.3]}
    status = {
        "pos": origin, "dimension": "overworld", "health": 12.3, "food": 20,
        "alive": True, "ready": True, "on_ground": True, "nearest_hostile": witch,
    }
    moves = []
    stops = []
    yaw = 0.0

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            nonlocal yaw
            if action == "status":
                return dict(status)
            if action == "events":
                hits = [
                    {"seq": 1, "type": "hurt", "source": "magic"},
                    {"seq": 2, "type": "hurt", "source_entity": witch},
                ]
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_block":
                if ground == "slow_probe":
                    clock[0] += min(0.36, kwargs["timeout_s"])
                x, y, z = body["pos"]
                if y == 86:
                    supported = ground in {"safe", "slow_control"} or (x, z) == (17029, 29985)
                    return {"name": "stone" if supported else "lava"}
                return {"name": "air"}
            if action == "look":
                yaw = body["yaw"]
                return {"ok": True}
            if action == "control":
                moves.append(body)
                if ground == "slow_control":
                    clock[0] += kwargs["timeout_s"]
                    raise httpx.ReadTimeout("control response exceeded the retreat deadline")
                dx, dz = yaw_to_unit_xz(yaw)
                status["pos"] = [origin[0] + 2.1 * dx, origin[1], origin[2] + 2.1 * dz]
                return {"ok": True}
            if action == "stop":
                stops.append((clock[0], kwargs["timeout_s"]))
                return {"ok": True}
            pytest.fail(f"Unexpected action after an unsafe bunker: {action}")

    guard = GenerationGuard(LedgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", lambda *_args, **_kwargs: {
        "ok": False, "reason": "unsafe_floor", "floor_pos": [17029, 83, 29985], "floor_block": "lava",
    })
    monkeypatch.setattr(MacroSkills, "defend_or_flee", lambda *_args, **_kwargs: pytest.fail("unchecked retreat"))

    incident = guard.poll()

    bunker, retreat = incident["actions"]
    assert bunker["result"]["reason"] == "unsafe_floor"
    assert retreat["action"] == "combat_or_flee" and retreat["ok"] is (ground == "safe")
    if ground.startswith("slow_"):
        assert retreat["result"] == {"ok": False, "reason": "time_budget", "partial": True}
        assert stops == [(1008.0, 1.5)]
        assert clock[0] == 1008.0
    else:
        assert retreat["result"].get("reason") == (None if ground == "safe" else "no_safe_retreat")
        assert stops == []
    assert len(moves) == (1 if ground in {"safe", "slow_control"} else 0)


@pytest.mark.parametrize("respawned", [False, True])
def test_failed_nether_cover_falls_back_to_checked_retreat_only_in_same_life(monkeypatch, respawned):
    from mc_agent.skills.combat_env import CombatEnv

    blaze = {"id": 44, "name": "blaze", "dist": 4.1, "pos": [3190.5, 66.2, -2046.4]}
    status = {
        "pos": [3186.9, 68, -2045.5], "health": 2.1, "food": 17, "dimension": "the_nether",
        "ready": True, "alive": True, "on_ground": True, "on_fire": True, "blocking": True,
        "nearest_hostile": blaze,
    }
    retreats = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "on_fire"}
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [blaze]}
            return {"ok": True}

    def fail_cover(_env, pos, *, other_shooters, allow_stationary):
        assert pos == blaze["pos"]
        assert not other_shooters and allow_stationary
        return {"ok": False, "mode": "natural_cover", "reason": "cover_unconfirmed", "respawned": respawned}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def flee(pos, *, wait_for_ground):
        retreats.append((pos, wait_for_ground))
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", fail_cover)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    retreat = next(action for action in guard.poll()["actions"] if action["action"] == "combat_or_flee")
    assert retreat["result"]["mode"] == ("natural_cover" if respawned else "flee")
    assert retreats == ([] if respawned else [(blaze["pos"], True)])


@pytest.mark.parametrize(
    "sighting", ["exposed", "covered", "timeout", "covered_with_melee", "fireball_in_cover", "burn_after_fireball"],
)
@pytest.mark.parametrize("health", [4.2, 16.0])
def test_burning_nether_retreat_checks_cover_against_nearby_blazes(monkeypatch, sighting, health):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    primary = {"name": "blaze", "id": 44, "dist": 7.8, "pos": [1534.2, 62, 3108.9]}
    secondary = {"name": "blaze", "id": 45, "dist": 14.4, "pos": [1529.5, 58, 3101.3]}
    distant = {"name": "blaze", "id": 46, "dist": 17.9, "pos": [1532, 58.5, 3097.5]}
    status = {
        "pos": [1532.6, 58, 3115.4], "dimension": "the_nether", "health": health, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "on_fire": True, "blocking": True,
        "nearest_hostile": {key: primary[key] for key in ("name", "id", "dist")},
    }
    calls = []
    cover_checks = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hits = [{"seq": 1, "type": "hurt", "source": "on_fire"}]
                if sighting == "fireball_in_cover":
                    hits.append({"seq": 2, "type": "hurt", "source": "fireball"})
                if sighting == "covered_with_melee":
                    hits = [
                        {"seq": 1, "type": "hurt", "source": "mob_attack"},
                        {"seq": 2, "type": "hurt", "source": ""},
                    ]
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                if sighting == "timeout":
                    raise httpx.ReadTimeout("hostile scan timeout")
                return {"entities": [primary, secondary, distant]}
            return {"ok": True}

    def flee(threat_pos, *, wait_for_ground, other_shooters=()):
        calls.append((threat_pos, wait_for_ground, other_shooters))
        return {"ok": True, "mode": "flee"}

    def blocks_shot(_env, _pos, shooter_pos):
        cover_checks.append(shooter_pos)
        return shooter_pos == primary["pos"] or sighting in {"covered", "covered_with_melee", "fireball_in_cover", "burn_after_fireball"}

    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(CombatEnv, "_blocks_shot", blocks_shot)
    if sighting in {"fireball_in_cover", "burn_after_fireball"}:
        monkeypatch.setattr(CombatEnv, "probe_retreat", lambda *_args: None)
    if sighting == "burn_after_fireball":
        guard._last_ranged_hurt_at = 999.0
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    incident = guard.poll()

    if sighting == "timeout":
        assert cover_checks == [] and calls == [(None, True, ())]
    elif (
        sighting in {"covered_with_melee", "fireball_in_cover"}
        or (sighting == "burn_after_fireball" and health <= 10)
    ):
        assert cover_checks == [] and calls == [(primary["pos"], True, [secondary["pos"]])]
    else:
        assert cover_checks == [primary["pos"], secondary["pos"]]
        if sighting in {"covered", "burn_after_fireball"}:
            assert calls == []
            retreat = next(a for a in incident["actions"] if a["action"] == "combat_or_flee")
            assert retreat["result"]["mode"] == "natural_cover"
        else:
            assert calls == [(primary["pos"], True, [secondary["pos"]])]


@pytest.mark.parametrize(("pressure", "bunker_first"), [
    ("direct_melee", False), ("unattributed_followup", False), ("occluded", True),
    ("inside_shaft", True), ("out_of_reach", True), ("stale_hit", True),
])
def test_nether_guard_retreats_before_digging_when_critical_melee_persists(monkeypatch, pressure, bunker_first):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [1870.3, 64, -3792.3]
    skeleton = {"id": 310015, "name": "wither_skeleton", "dist": 2.6, "pos": [1872.9, 64, -3792.3]}
    status = {
        "pos": origin, "health": 6.3, "food": 20, "dimension": "the_nether",
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "nearest_hostile": skeleton,
    }
    hits = [{"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": skeleton}]
    calls = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "look":
                calls.append("face_attacker")
                return {"ok": True}
            raise AssertionError(action)

    def bunker(_skills, *, urgent):
        assert urgent
        calls.append("emergency_bunker")
        return {"ok": True, "inside_shaft": True, "roof_placed": True}

    def retreat(*_args, **_kwargs):
        calls.append("retreat")
        if len(hits) == 1:
            return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}
        status.update(pos=[1872.3, 64, -3792.5], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 2.01}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", bunker)
    guard = GenerationGuard(FortressBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    monkeypatch.setattr(guard, "_inside_own_shaft", lambda: len(hits) > 1 and pressure == "inside_shaft")
    guard.poll()
    assert calls == ["retreat", "face_attacker"]
    calls.clear()

    clock[0] += 4.1 if pressure == "stale_hit" else 1.6
    status["health"] = 1.67
    if pressure == "occluded":
        status["nearest_hostile"] = {**skeleton, "blocked_by_wall": True}
    elif pressure == "out_of_reach":
        status["nearest_hostile"] = {**skeleton, "pos": [1875, 64, -3792.3], "dist": 4.7}
    hit = {"seq": 2, "type": "hurt"}
    if pressure not in {"unattributed_followup", "stale_hit"}:
        hit.update(source="mob_attack", source_entity=status["nearest_hostile"])
    hits.append(hit)

    incident = guard.poll()
    assert incident["requires_replan"]
    assert calls == (["emergency_bunker"] if bunker_first else ["retreat"])


@pytest.mark.parametrize("outcome", [
    "reported_dead", "api_dead", "dead_snapshot", "respawned", "not_ready", "sealed_after_respawn", "same_life",
])
def test_poll_only_retreats_after_a_bunker_failure_in_the_same_life(monkeypatch, outcome):
    status = {
        "pos": [27953.3, 65, 34988.4], "health": 3.33, "food": 17, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "last_death": {"t": 1},
    }
    calls = []

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 6}]}
            raise AssertionError(action)

    def dig_bunker(_skills, *, urgent):
        assert urgent
        calls.append("dig")
        if outcome == "api_dead":
            raise ApiError("dead", "stopped because of death")
        if outcome == "dead_snapshot":
            status.update(alive=False, health=0)
        elif outcome == "not_ready":
            status["ready"] = False
        elif outcome in {"respawned", "sealed_after_respawn"}:
            status.update(last_death={"t": 2}, health=20)
        if outcome == "sealed_after_respawn":
            return {"ok": True, "inside_shaft": True, "roof_placed": True}
        return {"ok": False, "reason": "dig_failed", "error": "dead" if outcome == "reported_dead" else "interrupted"}

    def retreat(*_args, **_kwargs):
        calls.append("retreat")
        return {"ok": True, "mode": "flee", "moved_dist": 2.01}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", dig_bunker)
    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)

    incident = guard.poll()

    if outcome == "same_life":
        assert calls == ["dig", "retreat"]
        assert [step["action"] for step in incident["actions"]] == ["emergency_bunker", "combat_or_flee"]
    else:
        assert calls == ["dig"]
        assert "death_or_connection_changed" in incident["reasons"]
        bunker, = incident["actions"]
        assert bunker["action"] == "emergency_bunker" and not bunker["ok"]


@pytest.mark.parametrize("landing", [
    "after_rejection", "after_airborne_start", "airborne", "new_life", "water", "unsafe_side", "time_budget",
])
def test_unarmed_melee_without_an_exit_checks_bunker_after_landing(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    drowned = {"id": 42, "name": "drowned", "dist": 2.3, "pos": [-7986.5, 63, -3019.0]}
    status = {
        "pos": [-7986.5, 63, -3016.7], "health": 6.5, "food": 17, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": landing != "after_airborne_start",
        "last_death": {"t": 1}, "nearest_hostile": drowned,
    }
    calls = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": drowned}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "diamond_pickaxe", "count": 1}, {"name": "cobblestone", "count": 9}]}
            if action == "stop" and landing == "time_budget":
                calls.append("stop")
                assert kwargs["timeout_s"] == 1.5
                return {"ok": True}
            pytest.fail(f"Unexpected action: {action}")

    def dig_bunker(skills, *, urgent):
        assert urgent
        if "retreat" not in calls:
            calls.append("early_bunker")
            if landing == "unsafe_side":
                return {"ok": False, "reason": "unsafe_open_side", "block_pos": [-7987, 62, -3017], "block": "air"}
            status["on_ground"] = False
            return {"ok": False, "reason": "not_grounded"}
        calls.append("grounded_bunker")
        assert status["on_ground"] is True and not status.get("in_water") and status["last_death"] == {"t": 1}
        if landing == "time_budget":
            clock[0] += 3.6
            skills.bot.status()
            pytest.fail("Bunker exceeded the recovery deadline")
        return {"ok": True, "inside_shaft": True, "roof_placed": True}

    def retreat(*_args, **_kwargs):
        calls.append("retreat")
        status.update(on_ground=landing != "airborne", health=5.1973, in_water=landing == "water")
        if landing == "new_life":
            status["last_death"] = {"t": 2}
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", dig_bunker)
    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    incident = guard.poll()

    expected = [] if landing == "after_airborne_start" else ["early_bunker"]
    expected.append("retreat")
    if landing in {"after_rejection", "after_airborne_start", "time_budget"}:
        expected.append("grounded_bunker")
    if landing == "time_budget":
        expected.append("stop")
    assert calls == expected
    if landing == "time_budget":
        assert incident["actions"][-1]["result"]["reason"] == "time_budget"
    elif expected[-1] == "grounded_bunker":
        assert incident["actions"][-1]["action"] == "emergency_bunker" and incident["actions"][-1]["ok"]
        assert guard._consecutive_flees == 0


@pytest.mark.parametrize("reason", ["unsafe_open_side", "unsafe_floor"])
def test_repeated_melee_hurt_skips_bunker_when_the_same_block_still_blocks_it(monkeypatch, reason):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    if reason == "unsafe_floor":
        unsafe_pos, unsafe_name = [-2273, 64, 3938], "air"
        pos = [-2272.5, 68, 3938.8]
        details = {"floor_pos": unsafe_pos, "floor_block": unsafe_name}
    else:
        unsafe_pos, unsafe_name = [-11016, 93, 10931], "water"
        pos = [-11014.3, 94, 10931.7]
        details = {"block_pos": unsafe_pos, "block": unsafe_name}
    obstacle = unsafe_name
    hits = []
    bunkers = []
    retreats = []
    status = {
        "pos": pos, "health": 3.9, "dimension": "overworld", "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": False,
        "nearest_hostile": {"id": 42, "name": "spider", "dist": 1.5},
    }

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in hits if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                assert body["pos"] == unsafe_pos
                return {"name": obstacle}
            raise AssertionError(action)

    class BunkerSkills:
        def dig_safety_bunker(self, *, urgent):
            assert urgent
            bunkers.append(obstacle)
            if obstacle == unsafe_name:
                return {"ok": False, "reason": reason, **details}
            return {"ok": True}

    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=BunkerSkills())

    def retreat(*_args, require_safe_footing):
        assert require_safe_footing
        retreats.append(True)
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat"}

    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    for seq, block in enumerate((unsafe_name, unsafe_name, "stone"), 1):
        obstacle = block
        hits.append({"seq": seq, "type": "hurt", "source": "mob_attack"})
        clock[0] += 0.6
        assert guard.poll()["requires_replan"]

    assert bunkers == [unsafe_name, "stone"]
    assert retreats == [True, True]


@pytest.mark.parametrize(("dimension", "nearest"), [
    ("the_nether", "attacker"), ("the_nether", "other_mob"), ("the_nether", "scan_timeout"),
    ("overworld", "attacker"), ("overworld", "other_mob"), ("overworld", "scan_timeout"),
    ("overworld", "unknown_attacker"),
])
def test_melee_retreat_resolves_the_confirmed_attacker_without_position(monkeypatch, nearest, dimension):
    attacker = {"id": 4580454, "name": "enderman" if dimension == "the_nether" else "zombie", "dist": 1.1}
    attacker_pos = [3517.2, 60, 1781.9]
    blaze = {"id": 82, "name": "blaze", "dist": 0.9, "pos": [3515.2, 60, 1781.9]}
    status = {
        "pos": [3516.1, 60, 1781.9], "health": 6.1, "food": 17, "dimension": dimension,
        "ready": True, "alive": True, "on_ground": True, "blocking": dimension == "the_nether",
        "nearest_hostile": attacker if nearest in {"attacker", "unknown_attacker"} else blaze,
    }
    retreats = []

    class AmbushBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                if nearest != "unknown_attacker":
                    hit["source_entity"] = attacker
                hits = [hit]
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"} if dimension == "the_nether" else None}
            if action == "get_entities":
                if nearest == "scan_timeout":
                    raise httpx.ReadTimeout("attacker lookup timed out")
                return {"entities": [blaze, {**attacker, "pos": attacker_pos}]}
            return {"ok": True}

    guard = GenerationGuard(AmbushBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def flee(threat_pos, *, wait_for_ground, use_nearest_hostile=True):
        assert wait_for_ground
        assert use_nearest_hostile is (nearest != "scan_timeout")
        retreats.append(threat_pos)
        return {"ok": True, "mode": "flee"}

    monkeypatch.setattr(guard.combat_env, "flee", flee)
    guard.poll()
    assert retreats == [None if nearest == "scan_timeout" else attacker_pos]


@pytest.mark.parametrize("shooters", ["visible", "timeout", "melee_only"])
@pytest.mark.parametrize("melee_source", ["mob_attack", "spear"])
def test_low_health_mixed_hurt_retreat_accounts_for_the_shooter(monkeypatch, shooters, melee_source):
    melee = {"id": 44, "name": "zombie", "dist": 2.0, "pos": [-2, 72, 0]}
    shooter = {"id": 45, "name": "skeleton", "dist": 8.0, "pos": [8, 72, 0]}
    status = {
        "pos": [0, 72, 0], "dimension": "overworld", "health": 8.56, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "nearest_hostile": {key: melee[key] for key in ("id", "name", "dist")},
    }
    hits = [{"seq": 2, "type": "hurt", "source": melee_source}]
    if shooters != "melee_only":
        hits.insert(0, {"seq": 1, "type": "hurt", "source": "arrow"})
    scans = []
    retreats = []

    class NightBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else hits}
            if action == "inventory":
                return {"items": [{"name": "diamond_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                scans.append(body)
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                if shooters == "timeout":
                    raise httpx.ReadTimeout("hostile scan timeout")
                return {"entities": [melee, shooter]}
            return {"ok": True}

    def retreat(primary_pos, *, wait_for_ground, other_shooters=()):
        assert wait_for_ground
        retreats.append((primary_pos, list(other_shooters)))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(NightBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    incident = guard.poll()

    assert [action["action"] for action in incident["actions"]] == ["combat_or_flee"]
    assert scans == ([] if shooters == "melee_only" else [{"radius": 16, "types": ["hostile"]}])
    expected = (melee["pos"], [shooter["pos"]]) if shooters == "visible" else (None, [])
    assert retreats == [expected]


@pytest.mark.parametrize("stance", ["unshielded", "shielded", "failed_cover"])
@pytest.mark.parametrize("scan", ["visible", "timeout"])
def test_low_health_surface_retreat_accounts_for_nearby_shooters(monkeypatch, scan, stance):
    from mc_agent.skills.combat_env import CombatEnv

    shielded = stance != "unshielded"
    failed_cover = stance == "failed_cover"
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 100.0)
    primary = {"id": 4882325, "name": "pillager", "pos": [-10959.1, 72, -11030.2], "dist": 7.3}
    secondary = {"id": 4883019, "name": "pillager", "pos": [-10965.5, 72, -11050.4], "dist": 13.5}
    status = {
        "pos": [-10962.4, 72, -11038], "health": 7.9 if failed_cover else 2.39 if shielded else 5.33, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "blocking": shielded,
        "nearest_hostile": primary,
    }
    plans = []

    class RoadBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": primary}
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"} if shielded else None}
            if action == "look":
                assert shielded
                return {"ok": True}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                if scan == "timeout":
                    raise httpx.ReadTimeout("shooter scan expired")
                return {"entities": [primary, secondary]}
            raise AssertionError(action)

    def cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters=False):
        assert verify_live_shooters is (not shielded)
        if failed_cover:
            assert not allow_stationary
        plans.append(("cover", pos, list(other_shooters)))
        return None

    def flee(pos, *, wait_for_ground, other_shooters=()):
        assert wait_for_ground
        plans.append(("flee", pos, list(other_shooters)))
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(RoadBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if failed_cover:
        guard._last_dig_cover_fail_at = 88.5
        guard._last_dig_cover_block = (-10961, 72, -11038)
    monkeypatch.setattr(MacroSkills, "dig_cover_pocket", lambda _skills: {"ok": False, "reason": "no_solid_wall"})
    monkeypatch.setattr(guard.combat_env, "probe_retreat", lambda *_args: None)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    monkeypatch.setattr(guard.combat_env, "flee", flee)

    expected_actions = ["combat_or_flee"] if failed_cover else ["dig_cover", "combat_or_flee"]
    if shielded:
        expected_actions = ["face_attacker", *expected_actions, "face_attacker_after_retreat"]
    assert [a["action"] for a in guard.poll()["actions"]] == expected_actions
    extra = [secondary["pos"]] if scan == "visible" else []
    assert plans == [("cover", primary["pos"], extra), ("flee", primary["pos"], extra)]


@pytest.mark.parametrize("water_floor", ["stone", "lava", "air"])
@pytest.mark.parametrize("entry", ["poll", "boundary", "tool"])
def test_critical_post_lava_burning_can_douse_in_verified_adjacent_water(monkeypatch, water_floor, entry):
    status = {
        "pos": [0.5, 63.6, 0.5], "health": 5, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "in_water": False, "in_lava": True, "on_ground": False, "on_fire": True,
    }
    targets = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 2}]}
            if action == "get_block":
                x, y, z = body["pos"]
                blocks = {(0, 63, 0): "stone", (1, 63, 0): "water", (1, 62, 0): water_floor}
                return {"name": blocks.get((x, y, z), "air")}
            if action == "move_to":
                targets.append([body[k] for k in ("x", "y", "z")])
                status.update(pos=targets[-1], in_water=True, on_fire=False)
                return {"ok": True}
            raise AssertionError(action)

    events = []
    guard = GenerationGuard(CaveBot(), EventSink(events.append), checkpoint=lambda: None)

    def escape(**_kwargs):
        status.update(pos=[0.5, 64, 0.5], health=9.8, in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False, "pos_after": status["pos"]}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    if entry == "poll":
        guard.poll()
    elif entry == "boundary":
        guard.stabilize_after_tools()
    else:
        receipt = guard.handle_preemption(
            {"source": "boundary_threat", "primary_type": "lava"}, interrupted_name="mc_skill_mine_dfs",
        )
        assert receipt["status_after"]["in_water"] is (water_floor == "stone")
    escape_event, douse = (event for event in events if event["type"] == "guardian.action.completed")
    assert escape_event["action"] == ("boundary_escape_lava" if entry == "boundary" else "escape_lava")
    assert douse["action"] == "douse_in_water"
    assert douse["ok"] is (water_floor == "stone")
    assert targets == ([[1.5, 63, 0.5]] if water_floor == "stone" else [])


@pytest.mark.parametrize("next_event", ["burn_only", "burn_with_bucket", "melee"])
def test_post_lava_burning_does_not_treat_a_nearby_drowned_as_the_attacker(monkeypatch, next_event):
    drowned = {"id": 44, "name": "drowned", "dist": 3.3}
    hits = [{"seq": 1, "type": "hurt", "source": "lava"}]
    defenses = []
    douses = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [9150.3, 67, -11277.3], "health": 8.24, "food": 17,
                    "ready": True, "alive": True, "on_ground": True, "dimension": "overworld",
                    "blocking": True, "on_fire": True, "nearest_hostile": drowned,
                }
            if action == "events":
                return {"events": [h for h in hits if h["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                items = [{"name": "diamond_sword", "count": 1}]
                if next_event == "burn_with_bucket":
                    items.append({"name": "water_bucket", "count": 1})
                return {"items": items, "offhand": {"name": "shield"}}
            return {"ok": True}

    def retreat(_status, attacker, _counts):
        defenses.append(attacker)
        return {"ok": True, "mode": "flee"}

    def douse(mode):
        douses.append(mode)
        return {"ok": True}

    guard = GenerationGuard(RidgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    hits.append({"seq": 2, "type": "hurt", "source": "on_fire", "likely_attacker": drowned})
    if next_event == "melee":
        hits.append({"seq": 3, "type": "hurt", "source": "mob_attack", "source_entity": drowned})
    monkeypatch.setattr(guard, "_defend_or_flee", retreat)
    monkeypatch.setattr(guard.combat_env, "build_tactical", douse)
    incident = guard.poll()

    assert defenses == ([drowned] if next_event == "melee" else [])
    assert douses == (["place_water_feet"] if next_event == "burn_with_bucket" else [])
    if next_event == "burn_only":
        assert incident is None


@pytest.mark.parametrize("hit", ["unattributed", "unattributed_later_page", "identified", "melee", "no_fire_context"])
def test_post_lava_fire_ticks_keep_adjacent_water_available_at_low_health(monkeypatch, hit):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    zombie = {"id": 42, "name": "zombie", "dist": 9.0}
    status = {"pos": [6356.7, 22, 6515.3], "health": 14.0, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "on_fire": True, "dimension": "overworld", "nearest_hostile": zombie}
    events = [{"seq": 1, "type": "hurt", "source": "lava"}]
    attempts = []
    defenses = []

    class RimBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [row for row in events if row["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    def douse(snapshot):
        attempts.append(snapshot["health"])
        status.update(in_water=True, on_fire=False)
        return {"ok": True, "in_water": True}

    def defend(_status, attacker, _counts, **_kwargs):
        defenses.append(attacker)
        return {"ok": False, "mode": "flee"}

    guard = GenerationGuard(RimBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    monkeypatch.setattr(guard.combat_env, "douse_in_adjacent_water", douse)
    monkeypatch.setattr(guard, "_defend_or_flee", defend)

    events.extend([{"seq": 2, "type": "hurt", "source": "on_fire"}, {"seq": 3, "type": "hurt"}])
    assert guard.poll() is None
    assert attempts == [] and defenses == []

    clock[0] += 4.0
    status.update(health=8.0, on_fire=hit != "no_fire_context")
    if hit != "unattributed_later_page":
        events.append({"seq": 4, "type": "hurt", "source": "on_fire"})
    last = {"seq": 5, "type": "hurt"}
    if hit == "identified":
        last["source_entity"] = zombie
    elif hit == "melee":
        last.update(source="mob_attack", source_entity=zombie)
    events.append(last)

    incident = guard.poll()
    if hit in {"unattributed", "unattributed_later_page"}:
        assert attempts == [8.0] and defenses == []
        assert [step["action"] for step in incident["actions"]] == ["douse_in_water"]
    else:
        assert attempts == [] and len(defenses) == 1


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


@pytest.mark.parametrize("landing", ["lava", "dry", "respawned", "loading"])
@pytest.mark.parametrize("entry", ["poll", "boundary"])
def test_combat_retreat_that_enters_lava_hands_off_to_escape_immediately(monkeypatch, landing, entry):
    brute = {"id": 44, "name": "piglin_brute", "dist": 2.5, "pos": [247.2, 35.5, 195.5]}
    status = {
        "pos": [249.4, 35.5, 195.5], "dimension": "the_nether", "last_death": {"t": 1},
        "health": 18, "food": 20, "ready": True, "alive": True, "on_ground": True,
        "in_lava": False, "nearest_hostile": brute,
    }
    calls = []
    events = []

    class BastionBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            if endpoint == "inventory":
                return {"items": []}
            raise AssertionError(endpoint)

    def retreat(trigger):
        calls.append(trigger["source"])
        status.update(pos=[251.6, 34.2, 196], in_lava=landing != "dry", on_ground=landing == "dry")
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        if landing == "loading":
            status["ready"] = False
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    def escape():
        calls.append("escape")
        status.update(pos=[252.5, 35, 196.5], in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    guard = GenerationGuard(BastionBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "has_los", lambda *_: {"clear": True})
    monkeypatch.setattr(guard, "_run_subcortical_combat", retreat)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)

    incident = guard.poll() if entry == "poll" else guard.stabilize_after_tools()
    assert calls == ["pre_hurt_melee"] + (["escape"] if landing == "lava" else [])
    prefix = "boundary_" if entry == "boundary" else ""
    combat, escape = prefix + "subcortical_combat", prefix + "escape_lava"
    assert [event["action"] for event in events if event["type"] == "guardian.action.completed"
            and event["action"] in {combat, escape}] == ([combat, escape] if landing == "lava" else [combat])
    if entry == "poll":
        assert ("in_lava" in incident["reasons"]) is (landing == "lava")
    if entry == "poll" and landing == "lava":
        assert [action["action"] for action in incident["actions"]] == ["subcortical_combat", "escape_lava"]
        assert incident["status"]["in_lava"] is False
        assert incident["status"]["pos"] == [252.5, 35, 196.5] and incident["requires_replan"]
    if entry == "boundary" and landing == "lava":
        assert "战斗后脱离岩浆" in incident


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


@pytest.mark.parametrize(
    "health, shooter_dist, fireball_age",
    [(5.3, 6.2, None), (17.6, None, 5.0), (14.3, None, 9.0), (2.53, 18.7, None)],
)
def test_poll_and_boundary_skip_eat_in_ranged_mob_fire(monkeypatch, health, shooter_dist, fireball_age):
    recent_nether_fireball = fireball_age is not None
    clock = [7000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    use_items: list[dict] = []
    posture_calls: list[dict] = []
    state = {"inside_shaft": False}

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
                    "health": health,
                    "food": 19,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "in_lava": False,
                    "blocking": not recent_nether_fireball,
                    "dimension": "the_nether" if recent_nether_fireball else "overworld",
                    "on_fire": recent_nether_fireball,
                    "nearest_hostile": None if shooter_dist is None else {
                        "id": 612494, "name": "skeleton", "pos": [0.5 + shooter_dist, 64.0, 0.5], "dist": shooter_dist,
                    },
                }
            if action == "events":
                if recent_nether_fireball and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "fireball"}], "last_seq": 1}
                return {"events": [], "last_seq": 0}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 10}, {"name": "shield", "count": 1}]}
            if action == "posture":
                posture_calls.append(pl)
            if action == "use_item":
                use_items.append(pl)
            return {"ok": True}

    guard = GenerationGuard(_SkeletonRangeBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=_EnclosedSkills())
    if recent_nether_fireball:
        guard._drain_events_pages()
        clock[0] += fireball_age
    else:
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


@pytest.mark.parametrize("history", ["fresh_arrow", "old_arrow", "respawned", "nearby_trident"])
def test_episode_start_keeps_eating_guard_for_recent_consumed_projectile(monkeypatch, history):
    clock = [1000.0]
    wall_now = 1_795_200_000.0
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.harness.guardian.time.time", lambda: wall_now + clock[0] - 1000.0)
    arrow = {"seq": 19, "type": "hurt", "source": "arrow",
             "t": (wall_now - (8.1 if history == "old_arrow" else 0.7)) * 1000}
    events = [arrow]
    if history == "respawned":
        events.append({"seq": 20, "type": "spawn", "t": (wall_now - 0.1) * 1000})
    status = {
        "pos": [-5057.2, 71, -7715.6], "health": 13.3, "food": 16, "dimension": "overworld",
        "blocking": True, "on_ground": True, "ready": True, "alive": True,
    }
    if history == "nearby_trident":
        arrow.update(source="trident", t=(wall_now - 9.0) * 1000)
        status.update(health=5.07, nearest_hostile={"id": 42, "name": "drowned", "dist": 8.5})
    commands = []
    cursors = []

    class EpisodeBot:
        _last_event_seq = 20

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                cursors.append(since)
                return {"events": [event for event in events if event["seq"] > since], "last_seq": 20}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            commands.append((action, body))
            return {"ok": True}

    guard = GenerationGuard(EpisodeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    if history in {"fresh_arrow", "nearby_trident"}:
        assert incident is None and commands == []
        clock[0] += 7.4 if history == "fresh_arrow" else 21.1
        incident = guard.poll()

    assert incident["reasons"] == ["needs_food"] and incident["event_seqs"] == []
    assert not incident["requires_replan"]
    assert commands == [("posture", {"shield": False}), ("use_item", {"item": "cooked_beef"})]
    assert cursors.count(0) == 1


@pytest.mark.parametrize("position", ["failed_cover", "moved", "window_elapsed"])
def test_recent_arrow_defers_eating_at_failed_cover_site(monkeypatch, position):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-2945.7, -8, -10074.3], "dimension": "overworld", "health": 9.56, "food": 19,
        "ready": True, "alive": True, "on_ground": True, "blocking": False, "nearest_hostile": None,
    }
    eats = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "arrow"}
                return {"events": [] if kwargs["params"]["since"] else [hit]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            if action == "use_item":
                eats.append(body)
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    guard._last_dig_cover_block = (-2946, -8, -10075)
    guard._last_dig_cover_fail_at = clock[0]
    if position == "moved":
        status["pos"] = [-2943.7, -8, -10074.3]
    clock[0] += 8.1 if position == "window_elapsed" else 3.75

    incident = guard.poll()

    if position == "failed_cover":
        assert incident is None and eats == []
    else:
        assert [action["action"] for action in incident["actions"]] == ["eat"]
        assert eats == [{"item": "cooked_beef"}]


@pytest.mark.parametrize("sources", [("mob_attack", "fireball"), ("fireball", "mob_attack")])
def test_mixed_nether_damage_keeps_melee_eat_cooldown_regardless_of_event_order(monkeypatch, sources):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [-406.3, 58, -1903.3], "dimension": "the_nether", "health": 17, "food": 19,
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "on_fire": False,
    }
    eats = []
    hits = [{"seq": seq, "type": "hurt", "source": source} for seq, source in enumerate(sources, 1)]

    class FoodBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            if action == "use_item":
                eats.append(body)
            return {"ok": True}

    guard = GenerationGuard(FoodBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._drain_events_pages()
    clock[0] += 9.0

    assert guard.poll() is None
    assert eats == []
    clock[0] += 1.1
    eat, = guard.poll()["actions"]
    assert eat["action"] == "eat" and eat["ok"]
    assert eats == [{"item": "cooked_beef"}]


@pytest.mark.parametrize("after_eat", ["spear_in_reach", "respawned", "clear"])
def test_interrupted_eat_restores_shield_against_a_confirmed_threat(after_eat):
    status = {
        "pos": [2562.3, 76, 9263.5], "dimension": "overworld", "health": 11.67, "food": 17,
        "ready": True, "alive": True, "on_ground": True, "blocking": True,
        "offhand": {"name": "shield"}, "last_death": {"t": 1},
    }
    actions = []

    class FoodBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}], "offhand": status["offhand"]}
            actions.append((action, body))
            if action == "posture":
                status["blocking"] = body["shield"]
                return {"blocking": status["blocking"]}
            if action == "use_item":
                status.update(pos=[2561.3, 75.9, 9261.5], health=4.67, food=20, on_ground=False)
                if after_eat == "spear_in_reach":
                    status["nearest_hostile"] = {"id": 78, "name": "husk", "dist": 2.5}
                if after_eat == "respawned":
                    status.update(ready=False, last_death={"t": 2})
                return {"interrupted_by": "hurt"}
            return {"ok": True}

    guard = GenerationGuard(FoodBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    eat, = incident["actions"]
    assert incident["requires_replan"] and "fresh_hurt" in incident["reasons"]
    expected = [("posture", {"shield": False}), ("use_item", {"item": "cooked_beef"})]
    if after_eat == "spear_in_reach":
        expected += [("look", {"entity_id": 78}), ("posture", {"shield": True, "jump": False, "sneak": False})]
        assert eat["result"]["shield_restored"] is True
    else:
        assert "shield_restored" not in eat["result"]
    assert actions == expected


@pytest.mark.parametrize(
    "item, shooter_distance, allowed",
    [("cooked_beef", 4.2, False), ("cooked_beef", 23.0, True), ("golden_apple", 4.2, True)],
)
def test_explicit_food_use_reuses_eating_safety_check(item, shooter_distance, allowed):
    from mc_agent.harness.mc_tools import McToolset

    uses = []

    class FoodBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                return {
                    "pos": [9097.3, 96, -11178.8], "health": 9.56, "food": 17, "blocking": True,
                    "on_ground": True, "ready": True, "alive": True,
                    "nearest_hostile": {"id": 44, "name": "skeleton", "dist": shooter_distance},
                }
            if action == "use_item":
                uses.append(body)
            return {"ok": True}

    bot = FoodBot()
    toolset = McToolset(bot)
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    receipt = json.loads(guard.run_tool_with_reflex("mc_use_item", lambda: toolset.mc_use_item(item, ms=1200)))

    assert receipt["ok"] is allowed
    if allowed:
        assert uses == [{"item": item, "ms": 1200}]
    else:
        assert receipt["requires_replan"] and receipt["error"]["code"] == "unsafe_to_eat"
        assert uses == []


def test_lava_contact_without_damage_preserves_later_melee_reflex(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [3566.3, 64.4, 1791.1], "health": 20, "food": 20, "dimension": "the_nether",
        "in_lava": True, "on_ground": False, "on_fire": True, "alive": True, "ready": True,
        "nearest_hostile": {"name": "wither_skeleton", "id": 44, "dist": 6.0},
    }
    hurts = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hurt for hurt in hurts if hurt["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(LavaBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def escape_lava(*, force_rim_escape):
        assert force_rim_escape is False
        status.update(in_lava=False, on_ground=True, pos=[3566.3, 65, 1791.1])
        return {"ok": True, "in_lava": False}

    retreats = []
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    monkeypatch.setattr(guard, "_defend_or_flee", lambda *args, **kwargs: retreats.append(True) or {
        "ok": False, "mode": "flee", "reason": "no_safe_retreat",
    })
    assert guard.poll()["actions"][0]["action"] == "escape_lava"

    clock[0] += 10.0
    status["health"] = 17.5
    hurts.append({"seq": 1, "type": "hurt", "source": "on_fire"})
    assert guard.poll() is None
    assert retreats == []

    clock[0] += 4.0
    status["health"] = 12.0
    hurts.extend([{"seq": 2, "type": "hurt", "source": "mob_attack"}, {"seq": 3, "type": "hurt", "source": "on_fire"}])
    incident = guard.poll()
    assert incident["requires_replan"] and incident["actions"][0]["action"] == "combat_or_flee"
    assert retreats == [True]


@pytest.mark.parametrize("recent_reaction", [False, True])
@pytest.mark.parametrize(
    "block, block_y, clears",
    [("fire", 52, True), ("fire", 53, True), ("fire", 54, False), ("soul_fire", 52, False), ("netherrack", 52, False)],
)
def test_contact_fire_only_digs_live_flames_inside_the_hitbox(monkeypatch, block, block_y, clears, recent_reaction):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    blocks = {(-419, block_y, -1881): block}
    digs = []

    class FireBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [-418.3, 52, -1880.5], "health": 10, "food": 20, "dimension": "the_nether",
                    "on_ground": True, "on_fire": True, "alive": True, "ready": True, "blocking": True,
                    "nearest_hostile": {"id": 44, "name": "blaze", "dist": 5.5},
                }
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": "in_fire"}]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "dig":
                assert kwargs["timeout_s"] <= 0.6 and kwargs["allow_reauth"] is False
                digs.append(body["pos"])
                if clears:
                    blocks[tuple(body["pos"])] = "air"
            return {"ok": True}

    guard = GenerationGuard(FireBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if recent_reaction:
        guard.last_reaction = 999.0
    incident = guard.poll()
    assert incident is not None
    clear, = incident["actions"]
    assert clear["action"] == "clear_contact_fire" and clear["ok"] is clears
    expected_digs = [[-419, block_y, -1881]] if block != "netherrack" and block_y < 54 else []
    assert digs == expected_digs
    assert not incident["requires_replan"]


def test_boundary_does_not_eat_while_still_taking_contact_fire_damage(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    uses = []
    status = {
        "pos": [-393.2, 55, -1890.5], "health": 10.08, "food": 19, "dimension": "the_nether",
        "on_ground": True, "on_fire": True, "alive": True, "ready": True,
    }

    class FireBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [
                    {"seq": 1, "type": "hurt", "source": "in_fire"},
                ]}
            if action == "inventory":
                return {"items": [{"name": "cooked_beef", "count": 1}]}
            if action == "use_item":
                uses.append(body)
                status["food"] = 20
            return {"ok": True}

    guard = GenerationGuard(FireBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.stabilize_after_tools() is None
    assert uses == []
    clock[0] += 2.0
    assert "进食" in guard.stabilize_after_tools()
    assert uses == [{"item": "cooked_beef"}]


@pytest.mark.parametrize("landing", ["grounded", "airborne", "back_in_lava", "respawned", "stale_success"])
def test_lava_tool_preemption_uses_latest_landing_status(monkeypatch, landing):
    status = {
        "pos": [6571.9, 64.8, 6592.6], "in_lava": True, "on_ground": False, "on_fire": True,
        "health": 18.1, "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    events = []
    douse_checks = []

    class LavaBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                if kwargs.get("timeout_s") == 0.25:
                    if landing == "grounded":
                        status.update(on_ground=True)
                    elif landing == "back_in_lava":
                        status.update(in_lava=True)
                    elif landing == "respawned":
                        status.update(last_death={"t": 2}, health=20, on_ground=True)
                return dict(status)
            if endpoint == "events":
                return {"events": []}
            raise AssertionError(endpoint)

    def escape():
        status.update(pos=[6572.7, 65, 6592.6], in_lava=landing == "stale_success")
        if landing == "stale_success":
            return {"ok": True, "in_lava": False}
        return {"ok": False, "in_lava": False, "reason": "lava_landing_unconfirmed"}

    guard = GenerationGuard(LavaBot(), EventSink(events.append), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    monkeypatch.setattr(guard, "_douse_after_lava", lambda _before, *, after: douse_checks.append(dict(after)))

    receipt = guard.handle_preemption(
        {"source": "boundary_threat", "primary_type": "lava", "distance": 0.0},
        interrupted_name="pre_ensure",
    )

    assert receipt["requires_replan"]
    assert ("并脱困" in receipt["message"]) is (landing == "grounded")
    assert ("死亡并重生" in receipt["message"]) is (landing == "respawned")
    assert bool(douse_checks) is (landing == "grounded")
    latest = receipt["status_after"]
    assert latest["in_lava"] is (landing in {"back_in_lava", "stale_success"})
    assert latest["on_ground"] is (landing in {"grounded", "respawned"})
    event, = [event for event in events if event["type"] == "guardian.preempted_tool"]
    assert event["status_after"] == latest


@pytest.mark.parametrize("death_recorded", [False, True])
def test_lava_landing_at_full_health_uses_death_record_instead_of_healing(monkeypatch, death_recorded):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [17087.0, 71.0, 29993.3], "health": 17.184, "ready": True, "alive": True,
        "dimension": "minecraft:overworld", "in_lava": True, "on_ground": False,
    }

    class LavaBot:
        def status(self):
            return dict(status)

        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "get_block":
                return {"name": "air"}
            if endpoint == "inventory":
                return {"items": []}
            if endpoint == "control" and body.get("forward"):
                status.update(health=20, in_lava=False, on_ground=True, pos=[17086.1, 66.2, 30000.7])
                if death_recorded:
                    status["last_death"] = {"t": 1}
            return {"ok": True}

    env = CombatEnv(LavaBot())
    monkeypatch.setattr(env, "scan_safe_yaws", lambda *args, **kwargs: {"best_yaw": 0.0})

    result = env.escape_lava()

    assert result["ok"] is (not death_recorded)
    assert result.get("respawned", False) is death_recorded


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
        assert result["moved_dist"] == 0.0
        assert result["pos_after"] == status["pos"]
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


@pytest.mark.parametrize("landing", [
    "landed", "late_landing", "five_hp_late_landing", "airborne", "respawned", "no_filler", "no_wall", "no_cover",
])
def test_unshielded_arrow_knockback_retries_cover_if_landed_with_no_retreat(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    late_landing = landing in {"late_landing", "five_hp_late_landing"}
    landing_at = 1000.45 if late_landing else 1000.15
    status = {
        "pos": [-7918.3, 44.6, 2790.6], "health": {"late_landing": 2.2, "five_hp_late_landing": 5.0}.get(landing, 7.3),
        "food": 20,
        "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 1},
        "nearest_hostile": {"name": "skeleton", "dist": 7.0},
    }
    cover_calls = []
    natural_checks = []
    fallback = landing in {"no_filler", "no_wall", "no_cover"}

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= landing_at and landing != "airborne":
                    status.update(pos=[-7918.3, 44, 2790.6], on_ground=True)
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow"}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}, {"name": "cobblestone", "count": 43}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities" and fallback:
                return {"entities": [{"id": 42, "name": "skeleton", "dist": 7.0, "pos": [-7911.3, 44, 2790.6]}]}
            raise AssertionError(action)

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def no_route(*args, **kwargs):
        assert status["on_ground"] is True
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        return None

    def dig_cover(skills):
        assert status["on_ground"] is True
        assert status["last_death"] == {"t": 1}
        cover_calls.append(status["pos"])
        if fallback:
            return {"ok": False, "reason": "no_filler_block" if landing == "no_filler" else "no_solid_wall"}
        return {"ok": True, "mode": "cover_pocket"}

    def natural_cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters):
        assert status["on_ground"] and not allow_stationary and verify_live_shooters
        assert not other_shooters and pos == [-7911.3, 44, 2790.6]
        natural_checks.append(pos)
        if landing != "no_cover":
            return {"ok": True, "mode": "natural_cover", "pos_after": [-7920.5, 44, 2790.6]}
        return None

    monkeypatch.setattr(guard.combat_env, "probe_retreat", no_route)
    monkeypatch.setattr(guard.combat_env, "dig_cover_pocket", dig_cover)
    monkeypatch.setattr("mc_agent.skills.combat_env.CombatEnv.seek_natural_cover", natural_cover)
    incident = guard.poll()
    assert cover_calls == ([[-7918.3, 44, 2790.6]] if landing not in {"airborne", "respawned"} else [])
    assert len(natural_checks) == int(fallback)
    if fallback:
        assert [a["action"] for a in incident["actions"]] == ["combat_or_flee", "dig_cover", "natural_cover"]
        assert incident["actions"][-1]["ok"] is (landing != "no_cover")


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
@pytest.mark.parametrize("entry", [
    "poll_ranged", "poll_melee", "subcortical_ranged", "subcortical_melee", "subcortical_armed_melee",
])
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
    armed_swarm = entry == "subcortical_armed_melee"
    hurt_source = "mob_attack" if melee else "arrow"
    if melee:
        status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 0.6, "pos": [-5191.2, 69.6, -7616.0]}
    if armed_swarm:
        status.update(health=11, held={"name": "stone_sword"})
        status["nearest_hostile"].update(name="zombie_villager", dist=1.4, pos=[-5190.4, 69.6, -7616.0])
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
                return {"items": [{"name": "stone_sword", "count": 1}] if armed_swarm else []}
            if action == "get_entities" and armed_swarm:
                return {"entities": [
                    status["nearest_hostile"],
                    {"id": 43, "name": "zombie", "dist": 4.0, "pos": [-5187.8, 69.6, -7616.0]},
                    {"id": 44, "name": "skeleton", "dist": 7.0, "pos": [-5184.8, 69.6, -7616.0]},
                ]}
            if action == "attack" and armed_swarm:
                pytest.fail("the armed bot must disengage from the group")
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
                "primary_type": status["nearest_hostile"]["name"],
                "primary_entity_id": 42,
            },
        )
        retreat = next(a for a in outcome["actions"] if a["action"] == "flee")
    assert bool(moves) is (landing == "grounded")
    assert clock[0] <= 1000.45
    if landing == "respawned":
        assert retreat["respawned"] is True


@pytest.mark.parametrize("primary_name, primary_distance", [("zombie", 0.8), ("creeper", 3.8), ("creeper", 2.1)])
def test_retreat_avoids_another_attacker_when_it_has_room(primary_name, primary_distance):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    primary = {"id": 42, "name": primary_name, "dist": primary_distance, "pos": [0.5 - primary_distance, 74, 0.5]}
    secondary = {"id": 43, "name": "zombie", "dist": 3.0, "pos": [3.5, 74, 0.5]}
    spider = {"id": 44, "name": "spider", "dist": 4.0, "pos": [0.5, 74, -3.5]}
    status = {
        "pos": [0.5, 74, 0.5], "health": 20, "ready": True, "alive": True,
        "on_ground": True, "is_day": True, "dimension": "overworld",
        "nearest_hostile": primary, "blocking": True, "last_death": {"t": 1},
    }
    forward = []

    class SwarmBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}]}
            if action == "get_entities":
                return {"entities": [primary, secondary, spider]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 74 else "air"}
            if action == "look" and "yaw" in body:
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, y, z = status["pos"]
                status["pos"] = [x + 3.0 * dx, y, z + 3.0 * dz]
                forward.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    source = "pre_hurt_creeper" if primary_name == "creeper" else "pre_hurt_melee"
    outcome = CombatControllerSlot().handle(
        CombatEnv(SwarmBot()), {"source": source, "primary_entity_id": 42, "primary_type": primary_name}
    )
    assert outcome["mode"] == ("creeper_evade" if primary_name == "creeper" else "cover_or_flee")
    assert len(forward) == 1
    if primary_name == "creeper" and primary_distance == 2.1:
        assert forward[0] == pytest.approx([3.5, 74, 0.5])
    else:
        assert forward[0][2] > 2.0
        if primary_name == "creeper":
            assert math.dist(forward[0], primary["pos"]) > 5.5


@pytest.mark.parametrize("archer_blocked", [False, True])
def test_unshielded_melee_retreat_avoids_an_exposed_archer_on_the_escape_line(archer_blocked):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [17058.2, 26, 29971.9]
    spider = {"id": 224968, "name": "spider", "pos": [17059.9, 26, 29971.9], "dist": 1.7}
    archer = {"id": 224969, "name": "skeleton", "pos": [17054.0, 26, 29971.9], "dist": 4.2,
              "blocked_by_wall": archer_blocked}
    blocked = {"id": 224970, "name": "skeleton", "pos": [17058.2, 26, 29979.9], "dist": 8,
               "blocked_by_wall": True}
    status = {"pos": origin, "health": 13, "food": 20, "ready": True, "alive": True, "on_ground": True,
              "dimension": "overworld", "nearest_hostile": spider}
    moves = []

    class TunnelBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [spider, archer, blocked]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 26 else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, y, z = status["pos"]
                status["pos"] = [x + 2.3 * dx, y, z + 2.3 * dz]
                moves.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body[k] for k in ("x", "y", "z")]
                moves.append(status["pos"])
            return {"ok": True}

    result = CombatControllerSlot().handle(CombatEnv(TunnelBot()), {
        "source": "pre_hurt_melee", "primary_type": "spider", "primary_entity_id": spider["id"],
    })

    assert result["mode"] == "cover_or_flee" and result["actions"][0]["ok"]
    assert math.dist(moves[0], spider["pos"]) > spider["dist"]
    assert (math.dist(moves[0], archer["pos"]) < 3.0) is archer_blocked


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


@pytest.mark.parametrize(("health", "distance", "blocked", "retreat_first"), [
    (1.33, 2.8, False, True),
    (1.33, 2.8, True, False),
    (1.33, 4.0, False, False),
    (6.0, 2.8, False, False),
])
def test_surface_bunker_yields_to_retreat_when_lethal_melee_is_exposed(monkeypatch, health, distance, blocked, retreat_first):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    threat = {"id": 42, "name": "zombie", "dist": distance, "pos": [0.5 + distance, 76, 0.5],
              "blocked_by_wall": blocked}
    status = {"pos": [0.5, 76, 0.5], "health": health, "food": 17, "ready": True, "alive": True,
              "on_ground": True, "nearest_hostile": threat, "dimension": "overworld"}
    attempts = []

    class SurfaceBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "dirt", "count": 6}]}
            if action == "get_entities":
                return {"entities": [threat]}
            return {"ok": True}

    class BunkerSkills:
        def dig_safety_bunker(self, *, urgent):
            assert urgent
            attempts.append("bunker")
            return {"ok": True}

    def checked_flee(threat_pos, *, distance, urgent, wait_for_ground):
        assert threat_pos == threat["pos"] and distance == 14.0
        assert urgent and wait_for_ground
        attempts.append("retreat")
        status.update(pos=[-4.0, 76, 0.5], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 4.5}

    env = CombatEnv(SurfaceBot(), skills=BunkerSkills())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", checked_flee)
    result = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_melee", "primary_entity_id": 42, "primary_type": "zombie", "distance": distance},
    )

    assert attempts == (["retreat"] if retreat_first else ["bunker"])
    assert result["mode"] == ("cover_or_flee" if retreat_first else "bunker_sealed")


@pytest.mark.parametrize("mob, distance, blocked, retreat_first", [
    ("zombie", 2.9, False, True),
    ("cave_spider", 2.5, False, True),
    ("zombie", 2.9, True, False),
    ("zombie", 4.4, False, False),
    ("skeleton", 2.9, False, False),
])
def test_exposed_cave_melee_retreats_before_digging_a_cover_pocket(monkeypatch, mob, distance, blocked, retreat_first):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    threat = {"id": 42, "name": mob, "dist": distance, "pos": [0.5 + distance, -39, 0.5],
              "blocked_by_wall": blocked}
    status = {
        "pos": [0.5, -39, 0.5], "health": 17, "ready": True, "alive": True,
        "on_ground": True, "nearest_hostile": threat, "dimension": "overworld",
    }
    attempts = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "get_entities":
                return {"entities": [threat]}
            if endpoint == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            return {"ok": True}

    def build(mode):
        attempts.append(mode)
        return {"ok": True, "mode": "cover_pocket"}

    def retreat(threat_pos, *, distance, urgent, wait_for_ground):
        assert threat_pos == threat["pos"] and urgent and wait_for_ground
        attempts.append("flee")
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    env = CombatEnv(CaveBot(), skills=object())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "build_tactical", build)
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged" if mob == "skeleton" else "pre_hurt_melee",
        "primary_entity_id": 42, "primary_type": mob, "distance": distance,
    })

    assert attempts == (["flee"] if retreat_first else ["cover_pocket"])
    assert outcome["mode"] == ("cover_or_flee" if retreat_first else "cover_pocket")


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("terrain", [
    "safe", "drop", "target_gone", "blocked_by_wall", "stalled", "knockback", "wet_landing", "respawned_after_step",
])
@pytest.mark.parametrize("shaft", [False, True])
def test_unarmed_retreat_follows_up_when_same_melee_attacker_is_still_close(monkeypatch, terrain, complete, shaft):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    threat = {"id": 42, "name": "zombie", "dist": 1.2, "pos": [1.7, 69, 0.5]}
    status = {"pos": [0.5, 69, 0.5], "health": 14, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "nearest_hostile": threat, "last_death": {"t": 1}}
    pulses = []
    distance = 0.9 if shaft else 2.6

    class RetreatBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}] if shaft else []}
            if action == "get_entities":
                if body["radius"] == 5:
                    assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                    nearby = {
                        **threat, "pos": [-1.7 + distance, 69, 0.5], "dist": distance,
                        "blocked_by_wall": terrain == "blocked_by_wall",
                    }
                    return {"entities": [] if terrain == "target_gone" else [nearby]}
                return {"entities": [threat]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] == 68 and terrain != "drop" else "air"}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control" and body.get("forward"):
                pulses.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                assert dx < 0 and dz == pytest.approx(0)
                if terrain != "stalled":
                    displacement = -0.7 if terrain == "knockback" else 0.7
                    status["pos"] = [-1.7 + displacement * dx, 69, 0.5 + displacement * dz]
                if terrain == "wet_landing":
                    status["in_water"] = True
                if terrain == "respawned_after_step":
                    status["last_death"] = {"t": 2}
            return {"ok": True}

    def initial_retreat(_threat_pos, **kwargs):
        status.update(
            pos=[-1.7, 69, 0.5], health=10.333333 if shaft else 8.3,
            nearest_hostile={"id": 42, "name": "zombie", "dist": distance},
        )
        return {"ok": complete, "mode": "flee", "moved_dist": 2.2 if complete else 1.39}

    env = CombatEnv(RetreatBot())
    monkeypatch.setattr(env, "flee", initial_retreat)
    if shaft:
        monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "carved_shaft"})
        monkeypatch.setattr(env, "build_tactical", lambda *_args, **_kwargs: {"ok": False, "reason": "place_failed"})
    outcome = CombatControllerSlot().handle(env, {"source": "pre_hurt_melee", "primary_entity_id": 42})

    followups = [a for a in outcome["actions"] if a["action"] == "followup_melee_retreat"]
    assert outcome["mode"] == ("flee" if shaft else "cover_or_flee")
    assert followups == ([] if terrain in {"target_gone", "blocked_by_wall"} else [
        {"action": "followup_melee_retreat", "ok": terrain == "safe", "entity_id": 42},
    ])
    expected_step = terrain in {"safe", "stalled", "knockback", "wet_landing", "respawned_after_step"}
    assert pulses == ([{"forward": True, "ms": 220, "stop_on": ["death"]}] if expected_step else [])


@pytest.mark.parametrize("moved, remaining, same_target, expected", [
    (1.97, 1.6, True, True),
    (1.97, 2.0, True, False),
    (3.0, 1.6, True, False),
    (1.97, 1.6, False, False),
])
def test_healthy_unarmed_bot_follows_up_short_retreat_when_the_attacker_gains_ground(
    monkeypatch, moved, remaining, same_target, expected,
):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    origin = [-6270.3, -40, -6565.9]
    zombie = {"id": 42, "name": "zombie", "dist": 1.9, "pos": [-6268.4, -40, -6565.9]}
    status = {
        "pos": origin, "health": 17, "food": 20, "alive": True, "ready": True, "on_ground": True,
        "dimension": "overworld", "nearest_hostile": zombie,
    }
    steps = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 63}]}
            if action == "get_entities":
                return {"entities": [status["nearest_hostile"]]}
            return {"ok": True}

    def retreat(_pos, **_kwargs):
        landing = [origin[0] - moved, *origin[1:]]
        survivor = {**zombie, "id": 42 if same_target else 43, "dist": remaining,
                    "pos": [landing[0] + remaining, *landing[1:]]}
        status.update(pos=landing, nearest_hostile=survivor)
        return {"ok": True, "mode": "flee", "moved_dist": moved}

    def verified_step(landing, threat_pos, *, deadline):
        assert landing["pos"] == status["pos"] and threat_pos == status["nearest_hostile"]["pos"]
        steps.append(threat_pos)
        return True

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "flee", retreat)
    monkeypatch.setattr(env, "step_away_from_melee", verified_step)
    outcome = CombatControllerSlot().handle(env, {"source": "pre_hurt_melee", "primary_entity_id": 42})

    assert steps == ([status["nearest_hostile"]["pos"]] if expected else [])
    assert any(a["action"] == "followup_melee_retreat" for a in outcome["actions"]) is expected


@pytest.mark.parametrize("pursuit", [
    "clear", "visible_shooter", "occluded_shooter", "corner_shooter", "slow_corner_shooter", "drifted_into_fire",
    "blocked", "lava", "out_of_range", "respawned", "time_budget",
])
def test_unarmed_night_retreat_rechecks_a_surviving_melee_pursuer(monkeypatch, pursuit):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    landing = [23043.5, 72, 9938.5]
    zombie = {"id": 257948, "name": "zombie", "dist": 3.4, "pos": [23034.1, 72, 9932.5]}
    distance = 9.2 if pursuit == "out_of_range" else 7.4
    survivor = {**zombie, "dist": distance, "pos": [landing[0] - distance, *landing[1:]],
                "blocked_by_wall": pursuit == "blocked"}
    shooter = {
        "id": 257949, "name": "skeleton", "dist": 4.2, "pos": [landing[0] + 4.2, *landing[1:]],
        "blocked_by_wall": pursuit in {"occluded_shooter", "corner_shooter", "slow_corner_shooter", "drifted_into_fire"},
    }
    wall = set()
    if shooter["blocked_by_wall"]:
        shooter["pos"] = [landing[0] + 3, landing[1], landing[2] + 2.5]
        shooter["dist"] = math.dist(shooter["pos"], landing)
        wall = {(23044, 9939), (23045, 9940)}
        if pursuit == "occluded_shooter":
            wall.update((x, 9939) for x in range(23045, 23049))
    status = {
        "pos": [23037.5, 72, 9932.5], "health": 20, "food": 20, "is_day": False,
        "on_ground": True, "ready": True, "alive": True, "nearest_hostile": zombie,
    }
    plans = []
    steps = []
    yaw = 0.0

    class FieldBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal yaw
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_entities":
                if not plans:
                    return {"entities": [zombie]}
                has_shooter = shooter["blocked_by_wall"] or pursuit == "visible_shooter"
                return {"entities": [survivor, shooter] if has_shooter else [survivor]}
            if action == "get_block":
                if pursuit == "slow_corner_shooter" and "timeout_s" in kwargs:
                    clock[0] += min(0.4, kwargs["timeout_s"])
                x, y, z = body["pos"]
                if y in (72, 73) and (x, z) in wall:
                    return {"name": "stone"}
                support = "lava" if pursuit == "lava" else "stone"
                return {"name": support if y < 72 else "air"}
            if action == "look":
                yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(yaw)
                point = [landing[0] + 2 * dx, landing[1], landing[2] + 2 * dz]
                steps.append([landing[0] + 2, *landing[1:]] if pursuit == "drifted_into_fire" else point)
                status.update(pos=steps[-1], nearest_hostile=None)
            if action == "move_to":
                steps.append([body["x"], body["y"], body["z"]])
                status["pos"] = steps[-1]
            return {"ok": True}

    env = CombatEnv(FieldBot())
    if wall:
        assert env._blocks_shot_across_hitbox(landing, shooter["pos"])
        straight_landing = [landing[0] + 2, *landing[1:]]
        assert env._blocks_shot_across_hitbox(straight_landing, shooter["pos"]) is (pursuit == "occluded_shooter")
    real_flee = env.flee

    def retreat(target, **kwargs):
        plans.append(target)
        if len(plans) > 1:
            return real_flee(target, **kwargs)
        status.update(pos=landing, nearest_hostile=survivor)
        if pursuit == "respawned":
            status["last_death"] = {"t": 1}
        if pursuit == "time_budget":
            clock[0] += 4.0
        return {"ok": True, "mode": "flee", "moved_dist": 8.49}

    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"], "distance": 3.4,
    })

    followups = [action for action in outcome["actions"] if action["action"] == "followup_melee_retreat"]
    can_retreat = pursuit in {"clear", "visible_shooter", "occluded_shooter", "corner_shooter", "slow_corner_shooter"}
    attempts_followup = can_retreat or pursuit in {"lava", "drifted_into_fire"}
    assert plans == [zombie["pos"]] + ([survivor["pos"]] if attempts_followup else [])
    assert [action["ok"] for action in followups] == ([can_retreat] if attempts_followup else [])
    if pursuit == "visible_shooter":
        assert all(math.dist(point, shooter["pos"]) >= 4.2 for point in steps)
        assert math.dist(status["pos"], survivor["pos"]) > distance
    elif pursuit in {"corner_shooter", "slow_corner_shooter"}:
        assert all(
            math.dist(point, shooter["pos"]) >= shooter["dist"] or env._blocks_shot_across_hitbox(point, shooter["pos"])
            for point in steps
        )
        assert math.dist(status["pos"], survivor["pos"]) > distance
        assert clock[0] - 1000.0 <= 1.2
        if pursuit == "slow_corner_shooter":
            assert clock[0] > 1000.0
    elif pursuit == "drifted_into_fire":
        assert len(steps) == 1 and followups[0]["reason"] == "projectile_lane_unconfirmed"
    elif can_retreat:
        assert steps == [pytest.approx([landing[0] + 2, *landing[1:]])]
    else:
        assert steps == []


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
                return {"items": [{"name": "stone_sword", "count": 1}]}
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


@pytest.mark.parametrize("landed_behind_seal", [True, False])
def test_guardian_rechecks_landing_after_legacy_shaft_seal(monkeypatch, landed_behind_seal):
    before = {
        "pos": [3103.7, 99.0, -10039.7], "health": 14.0, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True,
    }
    seal_position = [3102.4, 100.0, -10039.3]
    after = {
        **before, "health": 10.833,
        "pos": [3102.3, 100.0, -10039.1] if landed_behind_seal else [3103.4, 99.0, -10039.3],
    }

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            assert action == "status"
            return dict(after)

    def seal_shaft(_skills, **_kwargs):
        return {
            "ok": True, "mode": "seal_shaft", "pos_after": seal_position,
            "placed_positions": [[3101, 100, -10040], [3101, 101, -10040]],
            "requires_replan": True,
        }

    monkeypatch.setattr(MacroSkills, "defend_or_flee", seal_shaft)
    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None, skills=MacroSkills(bot))

    result = guard._defend_or_flee(before, None, {"stone_sword": 1})

    assert result["ok"] is landed_behind_seal
    assert result["pos_after"] == after["pos"]
    assert result["requires_replan"] is True
    if not landed_behind_seal:
        assert result["reason"] == "shaft_seal_landing_unconfirmed"


def test_unarmed_melee_reflex_checks_footing_before_retreat(monkeypatch):
    status = {
        "pos": [0.5, 64.0, 0.5], "health": 14.0, "food": 20, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 42, "name": "zombie", "dist": 2.0, "pos": [0.5, 64.0, 2.5]},
    }

    class LavaRimBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [hurt] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_block":
                x, y, z = body["pos"]
                name = "air" if y >= 64 else "stone" if (x, z) == (0, 0) else "lava"
                return {"name": name}
            if action == "find_blocks":
                return {"blocks": []}
            pytest.fail(f"Unexpected {action} on an unverified retreat route")

    def unchecked_retreat(*args, **kwargs):
        pytest.fail("The unarmed bot must check retreat footing before moving")

    monkeypatch.setattr(MacroSkills, "_flee_hostiles", unchecked_retreat)
    guard = GenerationGuard(LavaRimBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    retreat, = guard.poll()["actions"]

    assert retreat["action"] == "combat_or_flee"
    assert retreat["ok"] is False
    assert retreat["result"]["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("landing", ["grounded", "airborne", "respawned"])
def test_unshielded_critical_melee_knockback_rechecks_late_landing(monkeypatch, landing):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    zombie = {"id": 42, "name": "zombie", "dist": 2.0, "pos": [0.5, 68, 2.5]}
    status = {"pos": [0.5, 68.6, 0.5], "health": 2.5, "food": 20, "ready": True, "alive": True,
              "on_ground": False, "last_death": {"t": 1}, "dimension": "overworld", "nearest_hostile": zombie}
    motions = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if clock[0] >= 1000.45 and status["on_ground"] is False:
                    if landing == "grounded":
                        status.update(pos=[0.5, 68, 0.5], on_ground=True)
                    elif landing == "respawned":
                        status.update(last_death={"t": 2})
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": []}
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 68 else "air"}
            if action == "control" and body.get("forward"):
                assert status["on_ground"] is True
                motions.append(action)
                status["pos"] = [0.5, 68, -1.5]
            if action == "move_to":
                assert status["on_ground"] is True
                motions.append(action)
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(RidgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    retreat, = guard.poll()["actions"]
    assert 1000.45 <= clock[0] <= 1000.65
    assert retreat["ok"] is (landing == "grounded")
    assert motions == (["control", "move_to"] if landing == "grounded" else [])
    assert bool(retreat["result"].get("respawned")) is (landing == "respawned")


@pytest.mark.parametrize("after_first", [
    "close", "pursued", "scan_unavailable", "first_scan_unavailable", "first_scan_empty", "clear", "respawned",
])
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
                nearest = creeper
                if pulses and after_first in {"first_scan_unavailable", "first_scan_empty"}:
                    nearest = {"id": 67, "name": "zombie", "dist": 4.0}
                return {
                    "pos": list(pos), "health": 20, "food": 20, "on_ground": True, "yaw": yaw,
                    "ready": True, "alive": True, "last_death": last_death, "nearest_hostile": nearest,
                }
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                if len(pulses) == 1 and after_first == "first_scan_unavailable":
                    raise httpx.ReadTimeout("first creeper rescan timed out")
                if len(pulses) == 1 and after_first == "first_scan_empty":
                    return {"entities": []}
                if len(pulses) == 2 and after_first == "scan_unavailable":
                    raise httpx.ReadTimeout("creeper scan timed out")
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
                if len(pulses) == 1 and after_first in {"close", "pursued", "scan_unavailable"}:
                    creeper_pos[:] = [5.1, 64.0, -1.8]
                elif len(pulses) == 2 and after_first == "pursued":
                    creeper_pos[:] = [pos[0] + 4.5, pos[1], pos[2]]
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
    assert actions[0]["ok"] is (after_first in {"clear", "first_scan_empty"})
    if after_first == "first_scan_unavailable":
        assert actions[0]["reason"] == "blast_clearance_unconfirmed"
    assert pulses[0] == pytest.approx([0.5, 64.0, -1.8])
    if after_first in {"close", "pursued", "scan_unavailable"}:
        assert actions[0]["creeper_distance"] == pytest.approx(4.6)
        assert actions[1]["action"] == "creeper_followup_retreat" and actions[1]["ok"] is (after_first == "close")
        if after_first == "pursued":
            assert actions[1]["reason"] == "creeper_still_close" and actions[1]["creeper_distance"] == pytest.approx(4.5)
        elif after_first == "scan_unavailable":
            assert actions[1]["reason"] == "blast_clearance_unconfirmed"
        assert pulses[1] == pytest.approx([-1.8, 64.0, -1.8])
    else:
        assert len(pulses) == len(actions) == 1


@pytest.mark.parametrize("pursuer_blocked", [False, True])
@pytest.mark.parametrize(("first_move", "reason"), [
    (1.6, None), (1.4, None), (0.0, "no_safe_retreat"), (1.4, "unsafe_landing"),
])
def test_creeper_followup_uses_fresh_melee_position_to_choose_a_safe_route(monkeypatch, pursuer_blocked, first_move, reason):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    landing = [17054.4, 25, 29963.1]
    creeper = {"id": 42, "name": "creeper", "pos": [17059.76, 25, 29963.1], "dist": 4}
    zombie = {"id": 43, "name": "zombie", "pos": [17049.8, 25, 29963.1], "dist": 4.6,
              "blocked_by_wall": pursuer_blocked}
    status = {"pos": [landing[0] + first_move, 25, 29963.1], "health": 20, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "dimension": "overworld", "nearest_hostile": creeper}
    moves = []
    first_retreat = False

    class CaveBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                hostiles = [creeper, zombie] if first_retreat else [creeper]
                return {"entities": [{**mob, "dist": math.dist(status["pos"], mob["pos"])} for mob in hostiles]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 25 else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, y, z = status["pos"]
                status["pos"] = [x + dx * 2.3, y, z + dz * 2.3]
                moves.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body[key] for key in ("x", "y", "z")]
                moves.append(status["pos"])
            return {"ok": True}

    env = CombatEnv(CaveBot())
    followup = env.flee

    def flee(threat_pos, **kwargs):
        nonlocal first_retreat
        if first_retreat:
            return followup(threat_pos, **kwargs)
        first_retreat = True
        status.update(pos=landing, nearest_hostile=zombie)
        return {
            "ok": first_move >= 1.5 and reason is None, "mode": "flee", "moved_dist": first_move,
            "pos_after": landing, "reason": reason,
        }

    monkeypatch.setattr(env, "flee", flee)
    result = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42,
    })

    if reason:
        retreat, = result["actions"]
        assert not retreat["ok"] and retreat["reason"] == reason
        assert moves == []
        return
    assert result["actions"][1]["action"] == "creeper_followup_retreat" and result["actions"][1]["ok"]
    assert math.dist(moves[-1], creeper["pos"]) > 5.5
    assert (math.dist(moves[0], zombie["pos"]) < 3.0) is pursuer_blocked


@pytest.mark.parametrize("landing", [
    "critical", "sticky_pursuer", "late_respawn", "healthy", "respawned", "scan_timeout", "creeper_returns",
])
def test_critical_creeper_retreat_checks_for_a_surviving_melee_pursuer(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    origin = [19070.9, 64, 40967.5]
    safe_landing = [19077.5, 64, 40967.5]
    creeper = {"id": 40, "name": "creeper", "pos": [19066.8, 64, 40967.5]}
    zombie = {"id": 41, "name": "zombie", "pos": [19072.9, 64, 40967.5], "dist": 4.6}
    skeleton = {"id": 42, "name": "skeleton", "pos": [19077.5, 64, 40975.5]}
    other_creeper = {"id": 43, "name": "creeper", "pos": [19083.7, 64, 40967.5], "blocked_by_wall": True}
    status = {
        "pos": origin, "health": 12.3, "food": 17, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": creeper,
    }
    moves = []

    class NightBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if not moves:
                    return {"entities": [{**creeper, "dist": 4.1}]}
                if landing == "scan_timeout":
                    raise httpx.ReadTimeout("creeper clearance unavailable")
                live_creeper = other_creeper
                if landing == "creeper_returns" and body["radius"] == 12:
                    live_creeper = {**other_creeper, "pos": [19081.5, 64, 40967.5]}
                return {"entities": [zombie, skeleton, live_creeper]}
            return {"ok": True}

    def flee(pos, **options):
        moves.append(pos)
        if len(moves) == 1:
            status.update(pos=safe_landing, health=9 if landing == "healthy" else 2.3333, nearest_hostile=zombie)
            if landing == "respawned":
                status["last_death"] = {"t": 2}
            return {"ok": True, "mode": "flee", "moved_dist": 6.62}
        assert options["other_creepers"] == [other_creeper["pos"]] and options["keep_blast_clearance"]
        assert options["other_shooters"] == options["occluded_shooters"] == [skeleton["pos"]]
        assert not options["allow_second_leg"] and not options["allow_shaft_seal"]
        status["pos"] = [19078.0, 64, 40965.3]
        if landing == "sticky_pursuer":
            status["nearest_hostile"] = {**zombie, "pos": [19074, 64, 40965.3]}
        if landing == "late_respawn":
            status["last_death"] = {"t": 2}
        return {"ok": True, "mode": "flee", "moved_dist": 2.26}

    env = CombatEnv(NightBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", flee)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_melee", "primary_type": "creeper", "primary_entity_id": creeper["id"],
    })

    if landing in {"critical", "sticky_pursuer", "late_respawn"}:
        assert moves == [creeper["pos"], zombie["pos"]]
        followup = outcome["actions"][-1]
        assert followup["action"] == "followup_melee_retreat" and followup["entity_id"] == zombie["id"]
        assert followup["ok"] is (landing == "critical")
        if landing == "sticky_pursuer":
            assert followup["reason"] == "melee_clearance_unconfirmed"
        if landing == "late_respawn":
            assert all(step["respawned"] and not step["ok"] for step in outcome["actions"])
    else:
        assert moves == [creeper["pos"]]


@pytest.mark.parametrize("landing", ["water", "lava", "respawn", "retreat_respawn", "new_dimension", "not_ready"])
def test_creeper_retreat_hands_off_to_water_escape_in_the_same_life(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    creeper = {"id": 42, "name": "creeper", "pos": [0.5, 62, 4.5], "dist": 4.0}
    status = {
        "pos": [0.5, 62.5, 0.5], "health": 20, "on_ground": False,
        "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 1},
        "nearest_hostile": creeper,
    }
    shore_calls = []
    scans = []

    class BankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                scans.append(body["radius"])
                return {"entities": [creeper]}
            if action == "get_block":
                return {"name": "air"}
            return {"ok": True}

    def invalid_retreat(*_args, **_kwargs):
        status.update(pos=[0.5, 61.3, 0.5], in_water=True, in_lava=landing == "lava")
        if landing == "respawn":
            status["last_death"] = {"t": 2}
        elif landing == "new_dimension":
            status["dimension"] = "the_nether"
        elif landing == "not_ready":
            status["ready"] = False
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0,
                "respawned": landing == "retreat_respawn"}

    def reach_shore(threat_pos, **kwargs):
        shore_calls.append((threat_pos, kwargs))
        status.update(pos=[0.5, 62, -5.5], in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    env = CombatEnv(BankBot())
    monkeypatch.setattr(env, "flee", invalid_retreat)
    monkeypatch.setattr(env, "escape_deep_water_to_shore", reach_shore)
    result = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42},
    )

    assert result["mode"] == "creeper_evade"
    assert scans == ([16, 8] if landing in {"water", "lava"} else [16])
    retreat = next(step for step in result["actions"] if step["action"] == "retreat_from_creeper")
    assert bool(retreat.get("respawned")) is (landing in {"respawn", "retreat_respawn", "new_dimension"})
    if landing == "water":
        assert shore_calls == [(creeper["pos"], {"keep_shield": True, "shield_target_id": 42, "avoid_close_threat": True})]
        assert result["status_after"]["in_water"] is False
    else:
        assert shore_calls == []


def test_creeper_retreat_does_not_report_success_if_the_final_sample_is_dead(monkeypatch):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    creeper = {"id": 66, "name": "creeper", "pos": [4868.3, 22, -7670], "dist": 1.7}
    status = {
        "pos": [4870.0, 22, -7670], "health": 12, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "overworld", "last_death": {"t": 10},
        "nearest_hostile": creeper,
    }
    retreat_returned = False

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if retreat_returned:
                    status.update(pos=[4858.5, 69.1, -7603.5], health=0, ready=False, alive=False,
                                  last_death={"t": 20}, nearest_hostile=None)
                    return {"entities": []}
                return {"entities": [creeper]}
            return {"ok": True}

        def status(self):
            return dict(status)

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "open_cave"})

    def retreat(*_args, **_kwargs):
        nonlocal retreat_returned
        retreat_returned = True
        status.update(pos=[4866, 22, -7670], nearest_hostile=None)
        return {"ok": True, "mode": "flee", "moved_dist": 4.0}

    monkeypatch.setattr(env, "flee", retreat)
    outcome = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"]},
    )

    action, = outcome["actions"]
    assert action == {"action": "retreat_from_creeper", "ok": False, "moved": 4.0, "respawned": True}
    assert outcome["requires_replan"] and outcome["status_after"]["alive"] is False


@pytest.mark.parametrize("after", [
    "close_melee", "far_melee", "shooter", "blocked_shooter", "far_shooter", "healthy", "new_life", "still_wet",
])
def test_low_health_creeper_retreat_keeps_shield_facing_surviving_hostile(monkeypatch, after):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    creeper = {"id": 42, "name": "creeper", "dist": 2.8, "pos": [0.5, 68, 3.3]}
    if after.endswith("shooter"):
        survivor = {"id": 43, "name": "skeleton", "dist": 16.1 if after == "far_shooter" else 11.8}
        if after == "blocked_shooter":
            survivor["blocked_by_wall"] = True
    else:
        survivor = {"id": 43, "name": "zombie", "dist": 5.6 if after == "far_melee" else 5.4}
    status = {"pos": [0.5, 68, 0.5], "health": 16.4, "food": 20, "ready": True, "alive": True,
              "on_ground": True, "blocking": False, "dimension": "overworld", "last_death": {"t": 1},
              "nearest_hostile": creeper}
    looks = []
    posture = []

    class RidgeBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                nearest = status["nearest_hostile"]
                return {"entities": [nearest] if nearest["dist"] <= body["radius"] else []}
            if action == "look":
                looks.append(body["entity_id"])
            if action == "posture":
                posture.append(body)
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def retreat(*_a, **_kw):
        hp = 1.7 if after.endswith("shooter") else 12.0 if after == "healthy" else 5.73
        status.update(pos=[0.5, 68, -6.9], health=hp, nearest_hostile=survivor,
                      last_death={"t": 2 if after == "new_life" else 1}, in_water=after == "still_wet")
        return {"ok": True, "mode": "flee", "moved_dist": 7.4}

    env = CombatEnv(RidgeBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", retreat)
    if after == "still_wet":
        monkeypatch.setattr(env, "escape_deep_water_to_shore", lambda *_a, **_kw: {"ok": False, "in_water": True})
    result = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 42,
    })

    face_survivor = after in {"close_melee", "shooter"}
    guard_latched = face_survivor or after == "still_wet"
    assert result["latched_guard"] is guard_latched
    assert looks == ([42, 43] if face_survivor else [42])
    assert status["blocking"] is guard_latched
    if face_survivor:
        assert posture[-1] == {"shield": True, "sneak": False, "jump": False}
        assert result["actions"][-1]["action"] == (
            "guard_ranged_after_creeper_retreat" if after == "shooter" else "guard_melee_after_creeper_retreat"
        )


@pytest.mark.parametrize("shield_in_offhand", [True, False])
def test_creeper_retreat_raises_shield_once_before_terrain_probe(monkeypatch, shield_in_offhand):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    creeper = {"id": 55, "name": "creeper", "pos": [4.3, 64.0, 0.5], "dist": 3.8}
    status = {
        "pos": [0.5, 64.0, 0.5], "health": 20, "food": 20, "ready": True, "alive": True,
        "on_ground": True, "nearest_hostile": creeper,
    }
    calls = []

    class CreekBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **_kwargs):
            calls.append((action, body))
            if action == "inventory":
                return {
                    "items": [{"name": "iron_sword", "count": 1}, {"name": "shield", "count": 1}],
                    "offhand": {"name": "shield"} if shield_in_offhand else None,
                    "held": {"name": "iron_pickaxe"},
                }
            if action == "get_entities":
                return {"entities": [status["nearest_hostile"]] if status["nearest_hostile"] else []}
            return {"ok": True}

    env = CombatEnv(CreekBot())

    def probe(_pos):
        calls.append(("probe", None))
        return {"enclosure": "open"}

    def retreat(_pos, **_kwargs):
        status.update(pos=[-6.5, 64.0, 0.5], nearest_hostile=None)
        return {"ok": True, "moved_dist": 7.0}

    monkeypatch.setattr(env, "arena_snapshot", probe)
    monkeypatch.setattr(env, "flee", retreat)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"],
        "primary_pos": creeper["pos"], "distance": 3.8,
    })

    shield_equip = [] if shield_in_offhand else [("equip", {"item": "shield", "slot": "off-hand"})]
    assert [(action, body) for action, body in calls if action in {"equip", "look", "posture", "probe"}] == [
        *shield_equip,
        ("look", {"entity_id": creeper["id"]}),
        ("posture", {"shield": True, "sneak": False, "jump": False}),
        ("probe", None),
    ]
    assert result["mode"] == "creeper_evade"


def test_melee_warning_faces_nearby_creeper_before_raising_shield(monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    zombie = {"id": 11, "name": "zombie", "pos": [1.9, 64, 0.5], "dist": 1.4}
    creeper = {"id": 12, "name": "creeper", "pos": [-2.8, 64, 0.5], "dist": 3.3}
    status = {
        "pos": [0.5, 64, 0.5], "health": 16, "food": 20, "ready": True, "alive": True,
        "on_ground": True, "blocking": False, "nearest_hostile": zombie,
    }
    hostiles = [zombie, creeper]
    faced = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": hostiles}
            if action == "look":
                faced.append(body["entity_id"])
            return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open"})

    def flee(pos, **_kwargs):
        assert pos == creeper["pos"]
        hostiles.clear()
        return {"ok": True, "moved_dist": 6.0}

    monkeypatch.setattr(env, "flee", flee)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_melee", "primary_type": "zombie", "primary_entity_id": zombie["id"],
        "primary_pos": zombie["pos"], "distance": 1.4,
    })

    assert result["mode"] == "creeper_evade"
    assert faced and set(faced) == {creeper["id"]}


@pytest.mark.parametrize("creeper_in_status", [False, True])
def test_creeper_warning_uses_a_creeper_position_when_scan_only_finds_other_mobs(monkeypatch, creeper_in_status):
    from mc_agent.skills.combat_env import CombatEnv, SubcorticalInstinctController

    parched = {"id": 4621113, "name": "parched", "dist": 11.8, "pos": [12.3, 65, 0.5]}
    creeper_pos = [-3.2, 65, 0.5]
    status = {
        "pos": [0.5, 65, 0.5], "health": 19, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "nearest_hostile": parched,
    }
    if creeper_in_status:
        status["nearest_hostile"] = {"id": 4623433, "name": "creeper", "dist": 3.7, "pos": [-3.2, 65, 1.5]}
    expected = status["nearest_hostile"]["pos"] if creeper_in_status else creeper_pos
    moves = []

    class NightBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [parched]}
            raise AssertionError(action)

    env = CombatEnv(NightBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open"})

    def flee(threat_pos, **_kwargs):
        moves.append(threat_pos)
        status["nearest_hostile"] = parched
        return {"ok": True, "moved_dist": 3.0}

    monkeypatch.setattr(env, "flee", flee)
    result = SubcorticalInstinctController().on_encounter(env, {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 4623433,
        "primary_pos": creeper_pos, "distance": 3.7,
    })

    assert result["mode"] == "creeper_evade"
    assert moves == [expected]


@pytest.mark.parametrize("lands_in_water", [False, True])
@pytest.mark.parametrize(
    "mob, trigger, retreat_action",
    [
        ("creeper", "pre_hurt_creeper", "retreat_from_creeper"),
        ("zombified_piglin", "tool_interrupted_hurt", "flee"),
    ],
)
def test_retreat_rechecks_airborne_landing(monkeypatch, lands_in_water, mob, trigger, retreat_action):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    hostile = {"id": 66, "name": mob, "pos": [0.5, 64.0, 4.5], "dist": 4.0}
    pos = [0.5, 64.4, 0.5]
    landing_checked = False
    blocking = False
    calls = []

    class ShoreBot:
        def status(self):
            return {
                "pos": list(pos), "health": 20, "food": 20, "ready": True, "alive": True,
                "on_ground": landing_checked and not lands_in_water,
                "in_water": landing_checked and lands_in_water,
                "blocking": blocking,
                "nearest_hostile": {**hostile, "dist": math.dist(pos, hostile["pos"])},
            }

        def call(self, action, body=None, **kwargs):
            nonlocal landing_checked, blocking
            if action != "control" or (body or {}).get("forward"):
                calls.append(action)
            if action == "status":
                assert 0 < kwargs["timeout_s"] <= 0.45
                landing_checked = True
                pos[1] = 64.0
                return self.status()
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [self.status()["nearest_hostile"]]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 64 else "air"}
            if action == "posture":
                blocking = bool(body.get("shield"))
            if action == "control" and body.get("forward"):
                assert landing_checked and not lands_in_water
                pos[2] -= 2.3
            if action == "move_to":
                pos[:] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    def water_retreat(*_args, **_kwargs):
        calls.append("water_retreat")
        return {"ok": False, "in_water": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "escape_deep_water_to_shore", water_retreat)
    result = CombatControllerSlot().handle(env, {
        "source": trigger, "primary_entity_id": 66, "primary_type": mob,
    })
    retreat = next(action for action in result["actions"] if action["action"] == retreat_action)
    assert landing_checked
    assert retreat["ok"] is not lands_in_water
    if lands_in_water:
        assert retreat["reason"] == "unsafe_landing"
        assert result["latched_guard"] is (mob == "creeper")
        assert "control" not in calls and "move_to" not in calls
        assert ("water_retreat" in calls) is (mob == "creeper")
    else:
        assert calls.index("status") < calls.index("control")


@pytest.mark.parametrize("landing", ["dry", "airborne"])
def test_unshielded_creeper_retreat_waits_for_a_later_landing(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    creeper = {"id": 4881399, "name": "creeper", "pos": [0.5, 60, 3.5], "dist": 3.9}
    status = {
        "pos": [0.5, 63.1, 0.5], "health": 20, "food": 20,
        "ready": True, "alive": True, "on_ground": False, "nearest_hostile": creeper,
    }
    samples = []
    moves = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                samples.append(kwargs["timeout_s"])
                if len(samples) == 4 and landing == "dry":
                    status.update(on_ground=True, pos=[0.5, 60, 0.5])
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [creeper]}
            if action == "control" and body.get("forward"):
                assert status["on_ground"] is True
                moves.append(action)
                status["pos"] = [0.5, 60, -2.8]
            return {"ok": True}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda *_args: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "probe_retreat", lambda _pos, _yaw, distance: (60, False) if distance == 3.5 else None)
    result = CombatControllerSlot().handle(
        env, {"source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": creeper["id"]},
    )

    assert len(samples) == (4 if landing == "dry" else 5)
    assert all(0 < timeout <= 0.65 for timeout in samples)
    retreat, = result["actions"]
    assert retreat["ok"] is (landing == "dry")
    assert moves == (["control"] if landing == "dry" else [])


@pytest.mark.parametrize("short_bank", [False, True])
def test_poll_nether_retreat_checks_footing_at_high_health(monkeypatch, short_bank):
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda _: None)
    status = {
        "pos": [-407.5, 59.0 if short_bank else 59.8, -1887.3],
        "health": 14.83, "food": 20, "dimension": "the_nether",
        "on_ground": short_bank, "alive": True, "ready": True,
        "nearest_hostile": {"id": 77, "name": "wither_skeleton", "pos": [-407.5, 59, -1883.3], "dist": 4},
    }
    landings = []
    movements = []

    class LedgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if kwargs["timeout_s"] <= 0.45:
                    landings.append(action)
                    status.update(on_ground=True, pos=[-407.5, 59.0, -1887.3])
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": "mob_attack"}]}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if short_bank and (x != -408 or not -1890 <= z <= -1888):
                    return {"name": "lava" if y == 58 else "air"}
                return {"name": "netherrack" if y < 59 else "air"}
            if action == "control" and body.get("forward"):
                assert status["on_ground"]
                movements.append(body)
                status["pos"] = [-407.5, 59.0, -1888.0 if short_bank else -1889.6]
            if action == "move_to":
                assert not short_bank
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(LedgeBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    assert landings == ([] if short_bank else ["status"])
    control, = movements
    assert control["sprint"] is not short_bank and not control["jump"]
    retreat, = incident["actions"]
    assert retreat["action"] == "combat_or_flee" and retreat["ok"]


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


@pytest.mark.parametrize("safe_step", [True, False])
@pytest.mark.parametrize("direction", ["side", "away"])
def test_bug069_retreat_can_take_a_verified_short_step_when_long_routes_are_unsafe(safe_step, direction):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    status = {"pos": [0.5, 64.0, 0.5], "alive": True, "ready": True, "on_ground": True}
    controls = []

    class NarrowBankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                far_edge = 2 if safe_step else 1
                on_bank = x == 0 and 0 <= z <= far_edge if direction == "side" else z == 0 and -far_edge <= x <= 0
                return {"name": "stone" if y == 63 and on_bank else "air"}
            if action == "look":
                status["yaw"] = body["yaw"]
            if action == "control":
                controls.append(body)
                dx, dz = yaw_to_unit_xz(status["yaw"])
                status["pos"] = [0.5 + 0.7 * dx, 64.0, 0.5 + 0.7 * dz]
            return {"ok": True}

    result = CombatEnv(NarrowBankBot()).flee(
        [4.5, 64, 0.5], allow_shaft_seal=False, wait_for_ground=direction == "away",
    )

    assert result["ok"] is safe_step
    if safe_step:
        control, = controls
        expected = [0.5, 64.0, 1.2] if direction == "side" else [-0.2, 64.0, 0.5]
        assert result["pos_after"] == pytest.approx(expected)
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


@pytest.mark.parametrize(
    "water_state", ["surfaced", "submerged", "drowning", "failed_unstick", "unstick_timeout", "dry_after_unstick_timeout"],
)
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
    elif water_state in {"unstick_timeout", "dry_after_unstick_timeout"}:
        status.update(in_water=water_state == "unstick_timeout", submerged=False)
        receipt = {"ok": False, "interrupted_tool": "mc_skill_unstick", "error": {"code": "time_budget"}}
    else:
        receipt = guard.handle_preemption({"source": "tool_interrupted_hurt"}, interrupted_name="mc_skill_mine_dfs")
        assert receipt["reflex_mode"] == "water_surface"
        assert calls == ["water_macro", "shore"]
        calls.clear()
        status.update(submerged=water_state != "surfaced", oxygen=0 if water_state == "drowning" else 180)

    guard.stabilize_after_tools([json.dumps(receipt)])

    expected = {
        "surfaced": [], "submerged": ["shore"], "drowning": ["urgent_shore"], "failed_unstick": ["shore"],
        "unstick_timeout": ["shore"], "dry_after_unstick_timeout": [],
    }
    assert calls == expected[water_state]


@pytest.mark.parametrize("reentry", ["still_submerged", "observed_dry", "retry_window_expired", "poll_timeout"])
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
            if action == "inventory":
                return {"items": []}
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
    if reentry == "poll_timeout":
        guard.poll()
        assert attempts == ["water_macro"]
    else:
        guard.stabilize_after_tools()
        assert attempts == ["water_macro", "shore"]
    attempts.clear()

    clock[0] += 31.0 if reentry == "retry_window_expired" else 4.25
    if reentry == "observed_dry":
        status.update(in_water=False, submerged=False, on_ground=True)
        guard.stabilize_after_tools()
        status.update(in_water=True, submerged=True, on_ground=False)
    guard.stabilize_after_tools()

    expected = ["surface_then_shore"] if reentry in {"still_submerged", "poll_timeout"} else ["water_macro", "shore"]
    assert attempts == expected


@pytest.mark.parametrize("after_shore", ["still_airborne", "landed", "fresh_hurt", "new_drowned"])
def test_water_attack_preemption_waits_for_shore_landing_before_retrying_combat(monkeypatch, after_shore):
    attacker = {"id": 44, "name": "drowned", "dist": 3.1}
    status = {
        "pos": [9056.5, 63, -11385.4], "health": 20, "food": 20, "ready": True, "alive": True,
        "on_ground": after_shore == "landed", "in_water": False,
        "nearest_hostile": {**attacker, "id": 45} if after_shore == "new_drowned" else attacker,
    }
    combat = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "trident"}
                return {"events": [hurt] if after_shore == "fresh_hurt" and kwargs["params"]["since"] < 1 else []}
            pytest.fail(f"Unexpected action on the bank: {action}")

    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_detect_pre_hurt_trigger", lambda st: {
        "source": "pre_hurt_melee", "primary_type": "drowned", "primary_entity_id": st["nearest_hostile"]["id"],
    })

    def react(env, trigger):
        combat.append(trigger["primary_entity_id"])
        return {"handled": True, "mode": "cover_or_flee", "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", react)
    receipt = json.dumps({
        "preempted_by_reflex": True, "reflex_mode": "water_surface",
        "status_after": {"in_water": False, "on_ground": False, "nearest_hostile": attacker},
    })
    guard.stabilize_after_tools([receipt])

    assert combat == ([] if after_shore == "still_airborne" else [status["nearest_hostile"]["id"]])


def test_drowned_tool_preemption_bounds_float_before_shore_fallback(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [9067.9, 62.7, -11364.9], "health": 14.8, "oxygen": 200, "food": 19,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": 12.3},
    }
    calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "events":
                return {"events": []}
            if action == "move_to":
                calls.append((action, kwargs["timeout_s"]))
                clock[0] += kwargs["timeout_s"]
                raise httpx.ReadTimeout("float pathfinder exceeded its budget")
            calls.append((action, clock[0]))
            return {"ok": True}

    def slow_float(skills):
        return skills.bot.call("move_to", {"x": 9070.5, "y": 63, "z": -11351.5}, timeout_s=8.5)

    def reach_shore(*args, **kwargs):
        calls.append(("shore", clock[0]))
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(MacroSkills, "escape_water", slow_float)
    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": "drowned", "primary_entity_id": 44},
        interrupted_name="mc_skill_scout_surface",
    )

    assert calls == [("move_to", 4.0), ("stop", 1004.0), ("shore", 1004.0)]
    assert receipt["reflex_mode"] == "water_surface" and receipt["status_after"]["in_water"] is False


@pytest.mark.parametrize("has_shield", [False, True])
def test_tool_reflex_landing_in_water_near_drowned_heads_straight_to_shore(monkeypatch, has_shield):
    status = {"pos": [10935.7, 62.4, 11345.7], "health": 6.17, "oxygen": 230,
              "ready": True, "alive": True, "on_ground": False, "in_water": False,
              "last_death": {"t": 1}, "dimension": "overworld",
              "nearest_hostile": {"id": 44, "name": "drowned", "dist": 3.0}}
    threat_pos = [10932.7, 62.4, 11345.7]
    actions = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [{**status["nearest_hostile"], "pos": threat_pos}]}
            return {"ok": True}

    def combat(_env, trigger):
        status.update(in_water=True, offhand={"name": "shield"} if has_shield else None)
        return {"ok": False, "mode": "cover_or_flee", "status_after": dict(status)}

    def land(pos, *, keep_shield, shield_target_id, avoid_close_threat):
        actions.append((pos, keep_shield, shield_target_id, avoid_close_threat))
        status.update(in_water=False, on_ground=True, pos=[10939.5, 63, 11345.5])
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_slot, "handle", combat)
    monkeypatch.setattr(guard, "_escape_water", lambda *_: pytest.fail("close drowned requires immediate shore escape"))
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", land)
    trigger = {"source": "tool_interrupted_hurt", "primary_type": "drowned", "primary_entity_id": 44}

    result = guard.handle_preemption(trigger, interrupted_name="mc_skill_unstick")

    assert actions == [(threat_pos, has_shield, 44, True)]
    assert result["status_after"]["in_water"] is False
    assert result["status_after"]["on_ground"] is True


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


@pytest.mark.parametrize("recent_engagement", [True, False])
def test_poll_enters_water_near_recent_shooter_before_next_hurt(monkeypatch, recent_engagement):
    status = {
        "pos": [0.5, 62.2, 0.5], "health": 17.64, "food": 20, "oxygen": 180,
        "in_water": True, "submerged": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "pillager", "dist": 4.3},
    }
    shooter_pos = [4.8, 62.2, 0.5]
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
                return {"entities": [{"id": 44, "name": "pillager", "pos": shooter_pos}]}
            calls.append((action, body))
            return {"ok": True}

    def leave_water():
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def float_to_surface(_status):
        calls.append(("float", None))
        return leave_water()

    def reach_shore(attacker_pos, *, keep_shield, shield_target_id):
        calls.append(("shore", (attacker_pos, keep_shield, shield_target_id)))
        return leave_water()

    now = 1000.0
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: now)
    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    elapsed = 3.2 if recent_engagement else guard.RANGED_COMBAT_STANCE_S + 1
    guard._mark_ranged_engagement(now - elapsed, attacker=status["nearest_hostile"])
    monkeypatch.setattr(guard, "_escape_water", float_to_surface)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    guard.poll()

    if recent_engagement:
        assert calls == [
            ("look", {"entity_id": 44}),
            ("posture", {"shield": True, "jump": True, "sneak": False}),
            ("shore", (shooter_pos, True, 44)),
        ]
    else:
        assert calls == [("float", None)]
    assert not status["in_water"]


@pytest.mark.parametrize("respawned", [False, True])
def test_ranged_knockback_into_water_goes_to_shore_in_same_poll(monkeypatch, respawned):
    shooter = {"id": 44, "name": "skeleton", "dist": 6.0, "pos": [6.5, 67.0, 0.5]}
    status = {
        "pos": [0.5, 63.2, 0.5], "health": 9.5, "food": 20, "oxygen": 400,
        "in_water": False, "on_ground": False, "alive": True, "ready": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": shooter,
    }
    shore_calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": "arrow"}]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    def fall_into_water(_pos, *, wait_for_ground):
        assert wait_for_ground is True
        status.update(in_water=True, health=1.5, pos=[-1.5, 62.6, 0.5])
        if respawned:
            status["last_death"] = {"t": 2}
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.0}

    def reach_shore(attacker_pos, *, keep_shield, shield_target_id, surface_first):
        shore_calls.append((attacker_pos, keep_shield, shield_target_id, surface_first))
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", fall_into_water)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)

    incident = guard.poll()

    names = [action["action"] for action in incident["actions"]]
    if respawned:
        assert names == ["combat_or_flee"]
        assert shore_calls == []
    else:
        assert names == ["combat_or_flee", "escape_to_shore"]
        assert shore_calls == [(shooter["pos"], False, 44, False)]
        assert incident["status"]["in_water"] is False


@pytest.mark.parametrize("landing", ["water", "dry", "respawned", "lava"])
def test_melee_retreat_back_into_water_goes_to_shore_in_same_poll(monkeypatch, landing):
    zombie = {"id": 44, "name": "zombie", "dist": 1.5, "pos": [11084, 63, -11050]}
    status = {
        "pos": [11085.4, 63, -11050.4], "health": 10.18, "food": 20, "oxygen": 285,
        "in_water": False, "on_ground": True, "alive": True, "ready": True, "blocking": True,
        "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": zombie,
    }
    shore_calls = []
    lava_calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": zombie}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "iron_sword", "count": 1}], "offhand": {"name": "shield"}}
            raise AssertionError(action)

    def retreat(*_args, **_kwargs):
        status.update(
            in_water=landing != "dry", in_lava=landing == "lava", health=4.71, pos=[11086, 62, -11051.3],
        )
        if landing == "respawned":
            status.update(last_death={"t": 2}, health=20)
        return {"ok": False, "mode": "flee", "reason": "unsafe_landing", "moved_dist": 0.45}

    def reach_shore(pos, **kwargs):
        shore_calls.append((pos, kwargs))
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def escape_lava():
        lava_calls.append(status["pos"])
        status.update(in_lava=False, in_water=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", retreat)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)

    incident = guard.poll()
    assert lava_calls == ([[11086, 62, -11051.3]] if landing == "lava" else [])

    if landing == "water":
        assert [a["action"] for a in incident["actions"]] == ["combat_or_flee", "escape_to_shore"]
        assert shore_calls == [(zombie["pos"], {
            "keep_shield": True, "shield_target_id": 44, "surface_first": False, "avoid_close_threat": True,
        })]
        assert incident["status"]["in_water"] is False and incident["requires_replan"]
        assert not guard._last_water_escape_failed
    else:
        assert shore_calls == []


@pytest.mark.parametrize("terrain", ["exposed_lava", "solid_cover", "already_landed"])
@pytest.mark.parametrize(("source", "health"), [
    ("mob_attack", 2.86), ("mob_attack", 14.0), ("player_explosion", 15.87), ("fireball", 17.3),
])
def test_nether_knockback_checks_for_lava_before_waiting_to_retreat(monkeypatch, terrain, source, health):
    melee_above_retreat_threshold = source == "mob_attack" and health >= 11.0
    status = {
        "pos": [-1873.7, 36.6, -3688.7], "health": health, "food": 20,
        "in_lava": False, "on_ground": False, "alive": True, "ready": True, "blocking": not melee_above_retreat_threshold,
        "dimension": "the_nether", "nearest_hostile": {"id": 44, "name": "wither_skeleton", "dist": 3.0},
    }
    moves = []

    class NetherBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [{"seq": 1, "type": "hurt", "source": source}]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_block":
                assert kwargs["timeout_s"] <= 0.5 and kwargs["allow_reauth"] is False
                y = body["pos"][1]
                if y == 35 and terrain == "solid_cover":
                    return {"name": "netherrack"}
                return {"name": "lava" if y == 34 else "air"}
            return {"ok": True}

    def escape_lava(*, force_rim_escape=False):
        assert not force_rim_escape
        moves.append("escape_lava")
        status.update(on_ground=True)
        return {"ok": True, "in_lava": False, "already_dry": terrain == "already_landed"}

    def flee(_pos, *, wait_for_ground):
        moves.append("flee")
        assert wait_for_ground is True
        return {"ok": True, "mode": "flee"}

    guard = GenerationGuard(NetherBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    monkeypatch.setattr(guard.combat_env, "flee", flee)

    incident = guard.poll()

    if health >= 11.0 and terrain != "solid_cover":
        assert moves == ["escape_lava"]
        assert incident["actions"][0]["action"] == "escape_lava"
    elif melee_above_retreat_threshold:
        assert moves == []
        assert [action["action"] for action in incident["actions"]] == ["raise_shield"]
    elif terrain == "exposed_lava":
        assert moves == ["escape_lava"]
        assert incident["actions"][0]["result"]["mode"] == "lava_escape"
    else:
        assert moves == (["flee"] if terrain == "solid_cover" else ["escape_lava", "flee"])
        assert incident["actions"][0]["result"]["mode"] == "flee"


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


@pytest.mark.parametrize("scenario", [
    "ordinary", "surface_tool", "grounded_surface_tool", "flooded_staircase", "submerged_move", "surface_partial_move",
    "boundary_trident", "poll_trident", "boundary_melee", "poll_melee", "tool_melee", "boundary_arrow", "poll_arrow",
    "poll_high_health_melee", "tool_high_health_melee", "tool_ranged_status_only",
])
def test_bug056_guardian_bounds_shared_water_macro_and_falls_back_to_shore(monkeypatch, scenario):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    events = []
    calls = []
    health = 14 if scenario.endswith("melee") and "high_health" not in scenario else 20
    status = {"pos": [0.5, 52, 0.5], "health": health, "oxygen": 240, "in_water": True,
              "submerged": scenario != "surface_partial_move", "on_ground": False, "alive": True, "ready": True}
    if scenario == "grounded_surface_tool":
        status.update(oxygen=400, submerged=False, on_ground=True)
    if scenario == "tool_ranged_status_only":
        status["nearest_hostile"] = {"id": 42, "name": "skeleton", "dist": 3.5}
    if scenario.endswith("arrow"):
        status.update(
            health=17.6, submerged=False, blocking=True,
            nearest_hostile={"id": 42, "name": "pillager", "dist": 9.4, "pos": [9.9, 53, 0.5]},
        )

    class DeepWaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if scenario.endswith("trident") and not kwargs["params"]["since"]:
                    return {"events": [
                        {"seq": 1, "type": "hurt", "source": "trident"},
                        {"seq": 2, "type": "hurt", "source": "mob_attack"},
                    ]}
                if scenario.endswith("melee") and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "mob_attack"}]}
                if scenario.endswith("arrow") and not kwargs["params"]["since"]:
                    return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}]}
                return {"events": []}
            if action == "inventory":
                return {"items": []}
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
    receipt = {
        "ok": False, "stop_reason": "in_water", "pos_after": status["pos"],
        "error": {"code": "in_water"},
    }
    if scenario.startswith("poll_"):
        assert guard.poll() is not None
    elif scenario.startswith("tool_"):
        if scenario == "tool_ranged_status_only":
            trigger = {"source": "tool_interrupted_hurt", "primary_entity_id": 42, "primary_type": "skeleton"}
            interrupted = "mc_skill_harvest_logs"
        else:
            trigger = {"source": "tool_interrupted_hurt", "hurt_source": "mob_attack", "primary_type": "zombie"}
            interrupted = "mc_skill_unstick"
        report = guard.handle_preemption(trigger, interrupted_name=interrupted)
        assert report["status_after"]["in_water"] is False
    else:
        if scenario == "flooded_staircase":
            receipt = {
                "ok": False, "stop_reason": "dig_failed",
                "last_error": {"code": "unsafe_refilled_cell", "pos": [0, 53, 0], "block": "water"},
            }
        if scenario in {"submerged_move", "surface_partial_move"}:
            receipt = {"ok": True, "result": {"pos": status["pos"], "partial_move": scenario == "surface_partial_move"}}
        tool_results = [json.dumps(receipt)] if scenario in {
            "surface_tool", "grounded_surface_tool", "flooded_staircase", "submerged_move", "surface_partial_move",
        } else None
        assert "游向最近干燥陆地登岸" in guard.stabilize_after_tools(tool_results)
    completed = [e for e in events if e["type"] == "guardian.action.completed"]
    shallow, shore = completed
    prefix = "" if scenario.startswith(("poll_", "tool_")) else "boundary_"
    assert shallow["action"] == f"{prefix}escape_water"
    budget_s = 20.0 if scenario == "ordinary" else 4.0
    assert shallow["duration"] == pytest.approx(budget_s)
    assert shallow["result"] == {"ok": False, "reason": "time_budget", "partial": True, "in_water": True}
    assert shore["action"] == f"{prefix}escape_to_shore" and shore["ok"] is True
    assert all(start + timeout <= 1000.0 + budget_s for _, timeout, start in calls[:-1])
    assert skills.bot is bot


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("landed", [False, True])
def test_underwater_hot_floor_keeps_sneak_until_dry(monkeypatch, entry, landed):
    status = {
        "pos": [6111.5, 37.2, -108.5], "health": 19, "food": 20, "oxygen": 260,
        "in_water": True, "submerged": True, "on_ground": False, "ready": True, "alive": True,
    }
    bank = [6113.5, 38, -108.5]
    posture = []

    class BubbleBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "hot_floor"}
                return {"events": [hit] if not kwargs["params"]["since"] else []}
            if action == "inventory":
                return {"items": []}
            if action == "posture":
                posture.append(body)
            if action == "move_to" and landed:
                status.update(pos=bank, in_water=False, submerged=False, on_ground=True)
            if action == "get_block":
                return {"name": "water"}
            return {"ok": True}

    guard = GenerationGuard(BubbleBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda *_args, **_kwargs: pytest.fail("no digging in a magma column"))
    monkeypatch.setattr(guard.combat_env, "_find_walkable_shore_target", lambda *_args, **_kwargs: bank if landed else None)

    if entry == "poll":
        guard.poll()
    else:
        guard.stabilize_after_tools()
    assert posture[0] == {"shield": False, "sneak": True, "jump": True}
    assert posture[-1] == {"shield": False, "sneak": not landed, "jump": not landed}
    assert status["in_water"] is not landed


@pytest.mark.parametrize("lava_escape_succeeds", [True, False])
def test_boundary_switches_water_macro_to_lava_escape_on_contact(monkeypatch, lava_escape_succeeds):
    status = {
        "pos": [10834.4, 32.1, -11103.5], "health": 20, "food": 20, "oxygen": 240,
        "in_water": True, "in_lava": False, "submerged": True, "on_ground": False,
        "ready": True, "alive": True, "last_death": {"t": 1},
    }
    recovery = []
    events = []

    class FluidBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "stop":
                return {"ok": True}
            pytest.fail(f"unexpected action {action}")

    def legacy_escape(scoped):
        assert scoped.bot.status()["in_water"] is True
        status.update(in_water=False, in_lava=True, pos=[10832.6, 35.8, -11103.5], health=10)
        scoped.bot.status()
        pytest.fail("The water macro must unwind as soon as it sees lava")

    def lava_escape():
        recovery.append("lava")
        if lava_escape_succeeds:
            status.update(in_lava=False, on_ground=True)
        return {"ok": lava_escape_succeeds, "in_lava": status["in_lava"], "pos_after": status["pos"]}

    def stale_shore(*_args, **_kwargs):
        pytest.fail("A failed lava escape must not return to the water escape path")

    monkeypatch.setattr(MacroSkills, "escape_water", legacy_escape)
    bot = FluidBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=MacroSkills(bot))
    monkeypatch.setattr(guard.combat_env, "escape_lava", lava_escape)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", stale_shore)

    receipt = {"ok": True, "result": {"pos": status["pos"]}}
    guard.stabilize_after_tools([json.dumps(receipt)])

    assert recovery == ["lava"]
    water, = [e for e in events if e["type"] == "guardian.action.completed" and e["action"] == "boundary_escape_water"]
    assert water["result"]["mode"] == "lava_escape"
    assert water["ok"] is lava_escape_succeeds
    assert status["in_lava"] is not lava_escape_succeeds


@pytest.mark.parametrize("health", [9.8, 10.0, 15.5])
def test_trident_hit_directs_low_health_swimmer_to_shore_after_shield(monkeypatch, health):
    status = {
        "pos": [9264.9, 62.3, -11339.2], "health": health, "food": 20, "oxygen": 285,
        "in_water": True, "on_ground": False, "ready": True, "alive": True, "blocking": False,
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": 7.0, "pos": [9257.9, 62.3, -11339.2]},
    }

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "trident"}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "posture":
                status["blocking"] = body["shield"]
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def reach_shore(pos, *, keep_shield, shield_target_id):
        assert pos == [9257.9, 62.3, -11339.2]
        assert keep_shield and shield_target_id == 44 and status["blocking"]
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard, "_escape_water", lambda *_args, **_kwargs: {"ok": False, "in_water": True})
    incident = guard.poll()

    actions = ["face_attacker", "raise_shield"]
    if health >= 10:
        actions.append("escape_water")
    assert [action["action"] for action in incident["actions"]] == [*actions, "escape_to_shore"]


@pytest.mark.parametrize("second_source", ["mob_attack", "arrow"])
def test_water_combat_retries_shore_after_unconfirmed_landing(monkeypatch, second_source):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {
        "pos": [0.5, 62.5, 0.5], "health": 17.8, "food": 20, "oxygen": 285,
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "drowned", "pos": [3.3, 62.5, 0.5], "dist": 2.8},
    }
    hurts = [{"seq": 1, "type": "hurt", "source": "mob_attack", "health": 17.8}]

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [hurt for hurt in hurts if hurt["seq"] > since], "last_seq": hurts[-1]["seq"]}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    shore_attempts = []

    def reach_shore(attacker_pos, **kwargs):
        shore_attempts.append(attacker_pos)
        if len(shore_attempts) == 1:
            return {"ok": False, "in_water": False, "reason": "shore_landing_unconfirmed"}
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def escape_water(*args, **kwargs):
        pytest.fail("a failed shore landing should be followed by another directed escape")

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard, "_escape_water", escape_water)
    first = guard.poll()
    assert [action["action"] for action in first["actions"]] == ["escape_to_shore"]
    assert first["actions"][0]["result"]["reason"] == "shore_landing_unconfirmed"

    clock[0] += 4.0
    status.update(health=11.0, nearest_hostile={})
    hurts.append({"seq": 2, "type": "hurt", "source": second_source, "health": 11.0})
    second = guard.poll()
    assert [action["action"] for action in second["actions"]] == ["escape_to_shore"]
    assert second["actions"][0]["ok"] is True
    assert shore_attempts == [[3.3, 62.5, 0.5], None]


@pytest.mark.parametrize(("entry", "pressure"), [
    ("poll", "low_hp"), ("poll", "fresh_health"), ("poll", "burst"),
    ("boundary", "low_hp"), ("boundary", "burst"), ("tool", "low_hp"),
])
def test_swimming_melee_pressure_prefers_shore_without_a_live_attacker_snapshot(monkeypatch, entry, pressure):
    health = 8.166667 if pressure == "low_hp" else 14.0
    status = {
        "pos": [19023.7, 62.4, 41009.2], "health": health, "food": 20, "oxygen": 200,
        "in_water": True, "on_ground": False, "ready": True, "alive": True,
    }
    events = [{"seq": 1, "type": "hurt", "source": "mob_attack", "health": 8.0 if pressure == "fresh_health" else health}]
    if pressure == "burst":
        events.append({"seq": 2, "type": "hurt", "source": "mob_attack", "health": health})
    calls = []

    class PoolBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [event for event in events if event["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": []}
            pytest.fail(f"Unexpected action without an identified attacker: {action}")

    def reach_shore(threat_pos, *, keep_shield, shield_target_id, under_attack):
        assert threat_pos is None and shield_target_id is None and not keep_shield and under_attack
        calls.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(PoolBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    monkeypatch.setattr(guard, "_escape_water", lambda *_a, **_kw: pytest.fail("Ordinary escape delayed shore movement"))

    if entry == "poll":
        assert guard.poll()["actions"][-1]["action"] == "escape_to_shore"
    elif entry == "boundary":
        assert "登岸" in guard.stabilize_after_tools()
    else:
        result = guard.handle_preemption({"source": "tool_interrupted_hurt", "hurt_source": "mob_attack"},
                                         interrupted_name="mc_skill_unstick")
        assert result["reflex_mode"] == "water_surface"
    assert calls == ["shore"] and status["on_ground"]


@pytest.mark.parametrize("landing", ["grounded", "grounded_on_second_sample", "airborne", "wet_again"])
def test_water_macro_checks_that_a_dry_jump_reaches_ground(monkeypatch, landing):
    monkeypatch.setattr("mc_agent.harness.guardian.time.sleep", lambda _: None)
    status = {
        "pos": [9148.7, 63.4, -11301.5], "health": 20, "food": 20, "oxygen": 140,
        "in_water": True, "on_ground": False, "ready": True, "alive": True,
    }
    rechecks = []
    shore_calls = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if kwargs.get("timeout_s") == 0.25:
                    rechecks.append(action)
                    grounded = landing == "grounded" or (landing == "grounded_on_second_sample" and len(rechecks) == 2)
                    status.update(on_ground=grounded, in_water=landing == "wet_again")
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            return {"ok": True}

    class EscapeMacro:
        def escape_water(self):
            status.update(in_water=False, pos=[9149.7, 64.2, -11301.6])
            return {"ok": True, "in_water": False, "pos_after": status["pos"]}

    def reach_shore(*args, **kwargs):
        shore_calls.append(dict(status))
        return {"ok": False, "in_water": status["in_water"]}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=EscapeMacro())
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    incident = guard.poll()

    water = incident["actions"][0]
    assert water["action"] == "escape_water"
    assert water["ok"] is landing.startswith("grounded")
    assert rechecks == ["status"] * (2 if landing in {"grounded_on_second_sample", "airborne"} else 1)
    if landing.startswith("grounded"):
        assert shore_calls == []
    else:
        assert water["result"]["reason"] == "shore_landing_unconfirmed"
        assert water["result"]["in_water"] is (landing == "wet_again")
        assert len(shore_calls) == 1


@pytest.mark.parametrize("landing", ["dry", "wet", "airborne", "busy", "respawned"])
def test_water_macro_read_timeout_requires_confirmed_same_life_shore(landing):
    status = {
        "pos": [5011.2, 63, 18008.8], "health": 20, "food": 20, "oxygen": 260, "dimension": "overworld",
        "in_water": True, "submerged": True, "on_ground": False, "ready": True, "alive": True, "last_death": {"t": 1},
    }
    stops = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": []}
            assert action == "stop"
            stops.append(action)
            return {"ok": True}

    class EscapeMacro:
        def escape_water(self):
            status.update(
                pos=[5005.1, 63, 18015.6], in_water=landing == "wet", submerged=landing == "wet",
                on_ground=landing not in {"wet", "airborne"}, busy="move_to" if landing == "busy" else None,
            )
            if landing == "respawned":
                status["last_death"] = {"t": 2}
            raise httpx.ReadTimeout("lost the move reply")

    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=EscapeMacro())
    escape, = guard.poll()["actions"]

    assert escape["ok"] is (landing == "dry")
    assert guard._last_water_escape_failed is (landing != "dry")
    assert stops == ([] if landing == "dry" else ["stop"])
    if landing == "dry":
        assert escape["result"]["confirmed_after_read_timeout"] is True
        assert escape["result"]["pos_after"] == [5005.1, 63, 18015.6]
    else:
        assert escape["error"]["code"] == "network_uncertain"


@pytest.mark.parametrize("landing", ["threat_close", "threat_clear", "respawned"])
def test_water_macro_restores_raised_shield_on_dry_landing(landing):
    zombie = {"name": "zombie", "id": 4748724, "dist": 1.5}
    status = {
        "pos": [-11071.4, 50, 10869.4], "health": 16, "food": 20, "oxygen": 140,
        "in_water": True, "on_ground": True, "ready": True, "alive": True, "blocking": True,
        "dimension": "overworld", "last_death": {"t": 1}, "offhand": {"name": "shield"},
        "nearest_hostile": zombie,
    }
    commands = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": status["offhand"]}
            commands.append((action, body))
            return {"blocking": True}

    class EscapeMacro:
        def escape_water(self):
            status.update(pos=[-11071.7, 50, 10869.7], in_water=False, blocking=False, health=6)
            if landing == "respawned":
                status.update(last_death={"t": 2}, ready=False)
            if landing == "threat_clear":
                status["nearest_hostile"] = None
            return {"ok": True, "in_water": False, "pos_after": status["pos"]}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None, skills=EscapeMacro())
    water, = guard.poll()["actions"]

    assert water["ok"] is (landing != "respawned")
    if landing == "threat_close":
        assert commands == [
            ("look", {"entity_id": zombie["id"]}),
            ("posture", {"shield": True, "jump": False, "sneak": False}),
        ]
        assert water["result"]["shield_restored"] is True
    else:
        assert commands == []
        assert "shield_restored" not in water["result"]


@pytest.mark.parametrize("rising", [True, False])
def test_drowning_continues_a_verified_ascent_before_trying_to_path_to_shore(rising):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [-11061.7, 60.2, 10936.7], "in_water": True, "submerged": True,
        "oxygen": 0, "on_ground": False, "ready": True, "alive": True,
    }
    bank = [-11075, 62, 10937]
    actions = []

    class WetCaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "control" and not body.get("forward"):
                actions.append("up")
                if rising and actions == ["up"]:
                    status["pos"] = [-11061.7, 61.0, 10936.7]
                elif rising:
                    status.update(pos=[-11061.7, 63, 10936.7], oxygen=300, submerged=False, in_water=False, on_ground=True)
            if action == "find_blocks":
                return {"blocks": [{"name": "stone", "pos": bank}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                actions.append("move_to")
                status.update(pos=[body["x"], body["y"], body["z"]], submerged=False, in_water=False, on_ground=True)
            return {"ok": True}

        def status(self):
            return dict(status)

    result = CombatEnv(WetCaveBot()).escape_deep_water_to_shore(surface_first=True)

    assert result["ok"] is True and result["in_water"] is False
    assert actions == (["up", "up"] if rising else ["up", "move_to"])


@pytest.mark.parametrize("starts_in_lava, surface_first", [(True, True), (False, True), (False, False)])
def test_directed_shore_escape_does_not_report_lava_as_dry_land(starts_in_lava, surface_first):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [-6139.7, 43.2, -6089.7], "in_water": not starts_in_lava, "in_lava": starts_in_lava,
        "on_ground": starts_in_lava, "ready": True, "alive": True, "dimension": "overworld",
        "submerged": not starts_in_lava,
    }
    commands = []

    class FloodedCaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            commands.append(action)
            if action == "control":
                status.update(in_water=False, in_lava=True, on_ground=True)
            return {"ok": True}

    result = CombatEnv(FloodedCaveBot()).escape_deep_water_to_shore(surface_first=surface_first)

    assert result["ok"] is False and result["in_lava"] is True
    assert result["mode"] == "entered_lava"
    assert commands == ([] if starts_in_lava else ["posture", "control"])


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


def test_directed_water_escape_searches_the_bank_at_the_post_surge_water_level():
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [11208.5, 59.2, -11028.5], "in_water": True, "submerged": True,
        "on_ground": False, "alive": True, "ready": True,
    }
    bank = [11206, 62, -11048]
    moves = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "control" and body.get("ms") == 750:
                status.update(pos=[11206.5, 62.2, -11040.5], submerged=False)
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": bank}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                moves.append(goal)
                if goal == [11206.5, 63.0, -11047.5]:
                    status.update(pos=goal, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore()

    assert moves == [[11206.5, 63.0, -11047.5]]
    assert result["ok"] is True and result["in_water"] is False


@pytest.mark.parametrize("bank_flooded", [False, True])
def test_water_retry_rechecks_nearby_bank_before_swimming_past_it(monkeypatch, bank_flooded):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [7938.5, 63.0, 2777.5]
    status = {
        "pos": [7923.2, 62.8, 2778.8], "in_water": True, "submerged": False,
        "on_ground": False, "alive": True, "ready": True, "dimension": "overworld",
    }
    attempts = []
    retry = False
    surges = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (7938, 2777):
                    return {"name": "stone" if y == 62 else "water" if y == 63 and bank_flooded else "air"}
                return {"name": "water"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                attempts.append(goal)
                if not retry:
                    raise ApiError("path_not_found", "shore movement stalled")
                if goal == bank:
                    status.update(pos=goal, in_water=False, on_ground=True)
            if action == "control" and not retry and body.get("ms") == 1150:
                status.update(pos=[7936.0, 62.6, 2777.5])
            if action == "control" and retry and body.get("ms") == 750:
                surges.append(body["forward"])
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: None if retry else bank)

    assert env.escape_deep_water_to_shore()["in_water"] is True
    retry = True
    status.update(submerged=True)
    result = env.escape_deep_water_to_shore()

    assert surges == [bank_flooded]
    assert ("shore_bank_retry" in result["actions"]) is not bank_flooded
    if bank_flooded:
        assert attempts.count(bank) == 1
        assert result["in_water"] is True
    else:
        assert attempts == [bank, bank]
        assert result["ok"] is True and result["in_water"] is False


@pytest.mark.parametrize("has_threat", [False, True])
def test_failed_shallow_bank_hop_retries_with_sustained_climb(monkeypatch, has_threat):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [7853.5, 63, 2766.5]
    lip = [7854.3, 62.4, 2766.6]
    status = {
        "pos": [7864.5, 62.8, 2762.5], "in_water": True, "on_ground": False,
        "submerged": False, "oxygen": 300, "alive": True, "ready": True, "dimension": "overworld",
    }
    jumps = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                return {"name": "stone" if body["pos"] == [7853, 62, 2766] else "air"}
            if action == "move_to":
                assert [body["x"], body["y"], body["z"]] == bank
                status["pos"] = lip
                raise ApiError("path_not_found", "cannot climb bank")
            if action == "control" and body.get("forward"):
                assert body["jump"] is True
                jumps.append((body["ms"], body["sprint"]))
                if jumps[-1] == (1150, False):
                    status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: bank)
    threat = [7866.5, 62.8, 2762.5] if has_threat else None

    assert env.escape_deep_water_to_shore(threat)["in_water"] is True
    retry = env.escape_deep_water_to_shore(threat)

    assert "shore_bank_retry" in retry["actions"]
    assert jumps == [(450, True), (450, True) if has_threat else (1150, False)]
    assert retry["ok"] is not has_threat and retry["in_water"] is has_threat


@pytest.mark.parametrize("bank_state", [
    "clear", "flooded", "new_life", "observed_after_tool", "observed_during_poll", "higher_bank", "too_high",
])
def test_open_water_retreat_rechecks_the_last_confirmed_dry_bank(monkeypatch, bank_state):
    bank_y = {"higher_bank": 64, "too_high": 65}.get(bank_state, 63)
    bank = [7884.5, bank_y, 2779.5]
    wet = [7885.5, 62.7, 2751.5]
    status = {
        "pos": wet, "in_water": True, "on_ground": False, "alive": True, "ready": True,
        "dimension": "overworld", "last_death": {"t": 1},
    }
    moves = []

    class WaterBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "find_blocks":
                return {"blocks": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if (x, z) == (7884, 2779):
                    if y == bank_y - 1:
                        return {"name": "stone"}
                    return {"name": "water" if bank_state == "flooded" else "air"}
                return {"name": "water"}
            if action == "move_to":
                target = [body["x"], body["y"], body["z"]]
                moves.append(target)
                if target == bank:
                    status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    def generic_escape(_skills):
        status.update(pos=bank, in_water=False, on_ground=True)
        return {"ok": True, "in_water": False, "pos_after": bank}

    guard = GenerationGuard(WaterBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(MacroSkills, "escape_water", generic_escape)
    if bank_state in {"observed_after_tool", "observed_during_poll"}:
        status.update(pos=bank, in_water=False, on_ground=True)
        if bank_state == "observed_after_tool":
            guard.stabilize_after_tools()
        else:
            guard.poll()
    else:
        assert guard._escape_water(dict(status))["ok"] is True

    status.update(pos=wet, in_water=True, on_ground=False)
    if bank_state == "new_life":
        status["last_death"] = {"t": 2}
    result = guard.combat_env.escape_deep_water_to_shore()

    safe_bank = bank_state not in {"flooded", "new_life", "too_high"}
    assert ("return_to_dry_bank" in result["actions"]) is safe_bank
    assert (bank in moves) is safe_bank
    assert result["ok"] is safe_bank


@pytest.mark.parametrize("threat", ["none", "known", "unlocated"])
def test_short_shore_crossing_limits_pathfinder_wait_when_threatened(threat):
    from mc_agent.skills.combat_env import CombatEnv

    under_attack = threat != "none"
    status = {"pos": [10989.2, 62.7, -10987.8], "in_water": True, "on_ground": False,
              "alive": True, "ready": True}
    ledge = [10992, 62, -10985]
    motions = []

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": ledge}]}
            if action == "get_block":
                return {"name": "grass_block" if body["pos"] == ledge else "air"}
            if action == "move_to":
                motions.append("move_to")
                assert body["timeout"] == (1800 if under_attack else 4200)
                if under_attack:
                    assert kwargs["timeout_s"] == 2.4 and kwargs["allow_reauth"] is False
                raise httpx.ReadTimeout("shore pathfinder did not settle")
            if action == "stop" or (action == "control" and body == {"wait": False}):
                if under_attack:
                    assert kwargs["timeout_s"] == 0.5 and kwargs["allow_reauth"] is False
            if action == "control" and body.get("forward"):
                motions.append("jump")
                assert body["jump"] is True
                status.update(pos=[10992.5, 63.0, -10984.5], in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore(
        shield_target_id=44 if threat == "known" else None, under_attack=threat == "unlocated",
    )

    assert motions == ["move_to", "jump"]
    assert result["ok"] is True and result["in_water"] is False
    assert result["actions"] == ["move_to_shore_failed", "shore_lip_jump"]


def test_failed_pathfinder_uses_a_short_hop_to_reach_a_nearby_shallow_bank(monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    bank = [7948.5, 63.0, 2771.5]
    status = {
        "pos": [7949.8, 62.0, 2771.5], "in_water": True, "on_ground": False,
        "alive": True, "ready": True, "dimension": "overworld",
    }

    class ShoreBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "look":
                self.yaw = body["yaw"]
            if action == "move_to":
                raise ApiError("path_not_found", "pathfinder cannot climb the lip")
            if action == "control" and body.get("forward"):
                assert body["jump"] is True
                dx, dz = yaw_to_unit_xz(self.yaw)
                x, _, z = status["pos"]
                distance = 3.0 * body["ms"] / 1000
                x += dx * distance
                z += dz * distance
                landed = int(x) == int(bank[0]) and int(z) == int(bank[2])
                status.update(pos=[x, 63.0 if landed else 62.0, z], in_water=not landed, on_ground=landed)
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_a, **_kw: bank)

    result = env.escape_deep_water_to_shore()

    assert result["ok"] is True
    assert result["actions"] == ["move_to_shore_failed", "shore_lip_jump"]


@pytest.mark.parametrize("hop_result", ["landed", "still_wet", "respawned"])
def test_shore_escape_hops_onto_a_verified_adjacent_bank_before_pathing(hop_result):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [19062.5, 63.0, 41037.5]
    floor = [19062, 62, 41037]
    status = {
        "pos": [19062.0, 62.0, 41038.3], "in_water": True, "on_ground": False,
        "alive": True, "ready": True, "dimension": "overworld", "last_death": {"t": 1},
    }
    motions = []

    class BankBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": floor}]}
            if action == "get_block":
                return {"name": "grass_block" if body["pos"] == floor else "air"}
            if action == "control" and body.get("forward"):
                motions.append("jump")
                assert body["jump"] and body["ms"] == 450
                if hop_result == "landed":
                    status.update(pos=bank, in_water=False, on_ground=True)
                elif hop_result == "respawned":
                    status.update(pos=[19105, 76, 41080], in_water=False, on_ground=True, last_death={"t": 2})
            if action == "move_to":
                motions.append("move_to")
                assert [body[axis] for axis in ("x", "y", "z")] == bank
                status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(BankBot()).escape_deep_water_to_shore()

    assert motions == (["jump", "move_to"] if hop_result == "still_wet" else ["jump"])
    if hop_result == "respawned":
        assert result["mode"] == "respawned" and result["ok"] is False
    else:
        assert result["ok"] is True and result["pos_after"] == bank


@pytest.mark.parametrize("low_bank", ["grass_block", "stone", "blocked", "scan_timeout"])
def test_shore_escape_prefers_an_accessible_low_bank_over_a_closer_high_lip(low_bank):
    from mc_agent.skills.combat_env import CombatEnv

    high = [1, 64, 0]
    low = [3, 62, 0]
    status = {"pos": [0.5, 62.4, 0.5], "in_water": True, "on_ground": False, "alive": True, "ready": True}
    moves = []
    retry_options = []

    class PoolBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                if low_bank == "scan_timeout" and "stone" in body["types"]:
                    retry_options.append(kwargs)
                    raise httpx.ReadTimeout("low-bank scan stalled")
                rows = [{"name": "grass_block", "pos": high}]
                material = "stone" if low_bank in {"stone", "scan_timeout"} else "grass_block"
                if material in body["types"]:
                    rows.append({"name": material, "pos": low})
                return {"blocks": rows}
            if action == "get_block":
                return {"name": "stone" if low_bank == "blocked" and body["pos"] == [3, 64, 0] else "air"}
            if action == "move_to":
                target = [body[axis] for axis in ("x", "y", "z")]
                moves.append(target)
                status.update(pos=target, in_water=False, on_ground=True)
            return {"ok": True}

    result = CombatEnv(PoolBot()).escape_deep_water_to_shore()

    assert result["ok"] is True
    assert moves == [[1.5, 65.0, 0.5] if low_bank in {"blocked", "scan_timeout"} else [3.5, 63.0, 0.5]]
    if low_bank == "scan_timeout":
        options, = retry_options
        assert 0 < options["timeout_s"] <= 0.65 and options["allow_reauth"] is False


@pytest.mark.parametrize("reentry", ["same_pool", "new_life", "expired"])
def test_shore_escape_remembers_both_failed_banks_in_the_same_pool(monkeypatch, reentry):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    high_banks = ([1, 63, 0], [0, 64, -2])
    low_bank = [4, 62, 0]
    status = {"pos": [0.5, 62.4, 0.5], "in_water": True, "on_ground": False, "alive": True,
              "ready": True, "dimension": "overworld", "last_death": {"t": 1}}
    moves = []

    class PoolBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": bank} for bank in (*high_banks, low_bank)]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                target = [body[axis] for axis in ("x", "y", "z")]
                moves.append(target)
                if target[1] > 63:
                    raise ApiError("path_not_found", "bank above a steep lip")
                status.update(pos=target, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(PoolBot())
    assert env.escape_deep_water_to_shore()["in_water"] is True
    clock[0] += 8.5
    assert env.escape_deep_water_to_shore()["in_water"] is True
    clock[0] += 31.0 if reentry == "expired" else 8.5
    if reentry == "new_life":
        status["last_death"] = {"t": 2}
    third = env.escape_deep_water_to_shore()

    assert third["ok"] is (reentry == "same_pool")
    assert moves == [[1.5, 64.0, 0.5], [0.5, 65.0, -1.5],
                     [4.5, 63.0, 0.5] if reentry == "same_pool" else [1.5, 64.0, 0.5]]


@pytest.mark.parametrize("same_life", [True, False])
@pytest.mark.parametrize("under_attack", [False, True])
def test_water_escape_retries_a_different_bank_after_stalling_below_a_high_lip(monkeypatch, same_life, under_attack):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    status = {
        "pos": [11298.2, 62.2, 11021.3], "dimension": "overworld", "last_death": {"t": 1},
        "health": 9.0, "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    high_bank = [11300, 64, 11021]
    low_bank = [11296, 62, 11023]
    targets = []

    class RiverBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "find_blocks":
                banks = (high_bank, low_bank) if targets else (high_bank,)
                return {"blocks": [{"name": "grass_block", "pos": point} for point in banks]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                target = [body["x"], body["y"], body["z"]]
                targets.append(target)
                if target[1] == 63:
                    status.update(pos=target, in_water=False, on_ground=True)
                else:
                    status["pos"] = [11300.5, 62.9, 11021.8]
            return {"ok": True}

    env = CombatEnv(RiverBot())
    shield_target = 44 if under_attack else None
    first = env.escape_deep_water_to_shore(shield_target_id=shield_target)
    assert first["in_water"] is True and first["shore_target"] == [11300.5, 65.0, 11021.5]
    clock[0] += 4.0
    if not same_life:
        status["last_death"] = {"t": 2}
    second = env.escape_deep_water_to_shore(shield_target_id=shield_target)

    expected_bank = [11296.5, 63.0, 11023.5] if same_life else first["shore_target"]
    assert second["ok"] is same_life
    assert targets == [first["shore_target"], expected_bank]


def test_water_boundary_avoids_a_shore_path_that_crosses_a_creeper(monkeypatch):
    status = {
        "pos": [0.5, 62.2, 0.5], "health": 20, "food": 20, "oxygen": 320,
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
        "nearest_hostile": {"id": 44, "name": "creeper", "dist": 5.0},
    }
    moves = []

    class RiverBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [{"id": 44, "name": "creeper", "pos": [5.5, 62.2, 0.5]}]}
            if action == "find_blocks":
                return {"blocks": [{"name": "grass_block", "pos": [11, 62, 0]},
                                   {"name": "grass_block", "pos": [-13, 62, 0]}]}
            if action == "get_block":
                return {"name": "air"}
            if action == "move_to":
                goal = [body["x"], body["y"], body["z"]]
                moves.append(goal)
                status.update(pos=goal, in_water=False, on_ground=True)
            return {"ok": True}

    guard = GenerationGuard(RiverBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda _status: pytest.fail("nearby creeper needs a threat-aware shore"))

    note = guard.stabilize_after_tools()

    assert moves == [[-12.5, 63.0, 0.5]]
    assert note is not None and "登岸" in note


@pytest.mark.parametrize(("entry", "pressure"), [
    ("poll", "nearby"), ("poll", "at_surface"), ("poll", "fresh_hit"), ("boundary", "nearby"),
    ("poll", "distant"), ("poll", "occluded"),
])
def test_swimming_near_exposed_melee_uses_checked_shore_without_generic_float(monkeypatch, entry, pressure):
    zombie_pos = [19981.4, 62, 32993.7]
    distance = 7.0 if pressure == "distant" else 3.2
    zombie = {"id": 357966, "name": "zombie", "dist": distance, "blocked_by_wall": pressure == "occluded"}
    status = {
        "pos": [19981.4, 62, 32990.5], "health": 18.5, "food": 20, "oxygen": 220,
        "in_water": True, "submerged": True, "on_ground": False, "ready": True, "alive": True,
        "nearest_hostile": zombie, "dimension": "overworld",
    }
    if pressure == "at_surface":
        status.update(oxygen=300, submerged=False, jumping=True)
    calls = []

    class CreekBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hit = {"seq": 1, "type": "hurt", "source": "mob_attack"}
                return {"events": [hit] if pressure == "fresh_hit" and not kwargs["params"]["since"] else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                return {"entities": [{**zombie, "pos": zombie_pos}]}
            raise AssertionError(action)

    guard = GenerationGuard(CreekBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def float_to_surface(*_args, **_kwargs):
        calls.append("float")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def reach_shore(threat_pos, *, keep_shield, shield_target_id, avoid_close_threat):
        assert threat_pos == zombie_pos and shield_target_id == zombie["id"]
        assert not keep_shield and avoid_close_threat
        calls.append("shore")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard, "_escape_water", float_to_surface)
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    assert (guard.poll() if entry == "poll" else guard.stabilize_after_tools()) is not None
    assert calls == (["float"] if pressure in {"distant", "occluded"} else ["shore"])


@pytest.mark.parametrize("entry", ["poll", "mc_attack", "mc_move_to"])
def test_nearby_drowned_sends_water_recovery_directly_to_shore(monkeypatch, entry):
    drowned = {"id": 44, "name": "drowned", "dist": 4.7}
    drowned_pos = [9266.3, 61, -11355]
    status = {
        "pos": [9265.5, 62, -11350.5], "health": 14.1, "food": 20, "oxygen": 220,
        "in_water": True, "submerged": True, "on_ground": False, "blocking": False,
        "ready": True, "alive": True, "nearest_hostile": drowned,
    }
    actions = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [] if kwargs["params"]["since"] else [
                    {"seq": 1, "type": "hurt", "source": "mob_attack", "source_entity": drowned},
                ]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                assert kwargs["timeout_s"] == 0.6 and kwargs["allow_reauth"] is False
                return {"entities": [{**drowned, "pos": drowned_pos}]}
            if action == "posture":
                assert body["shield"] and body["jump"]
                actions.append("shield")
                status["blocking"] = True
            return {"ok": True}

    def reach_shore(threat_pos, *, keep_shield, shield_target_id):
        assert threat_pos == drowned_pos and keep_shield and shield_target_id == 44
        assert status["blocking"]
        actions.append("shore")
        status.update(in_water=False, submerged=False, on_ground=True)
        return {"ok": True, "in_water": False}

    guard = GenerationGuard(ShoreBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda *args, **kwargs: pytest.fail("generic escape delays shore recovery"))
    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    if entry == "poll":
        incident = guard.poll()
        assert incident["actions"][-1]["action"] == "escape_to_shore"
    else:
        receipt = guard.handle_preemption({
            "source": "tool_interrupted_hurt" if entry == "mc_move_to" else "boundary_threat",
            "primary_entity_id": 44, "primary_type": "drowned",
        }, interrupted_name=entry)
        assert receipt["reflex_mode"] == "water_surface" and receipt["status_after"]["on_ground"]
    assert actions == ["shield", "shore"]


@pytest.mark.parametrize("landing", ["grounded", "airborne", "in_water", "settled", "settled_above", "respawned"])
def test_shore_escape_confirms_landing_after_airborne_edge_receipt(landing):
    from mc_agent.skills.combat_env import CombatEnv

    moves = []
    posture = []
    landing_reads = []
    status = {"pos": [10937.5, 62.5, -11007.3], "in_water": True, "on_ground": False,
              "alive": True, "ready": True}

    class ShoreBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                landing_reads.append(kwargs["timeout_s"])
                assert kwargs["allow_reauth"] is False
                if landing in {"settled", "settled_above"}:
                    status.update(on_ground=True, pos=[10938.5, 64.0, -11007.5])
                elif landing == "respawned":
                    status.update(on_ground=True, last_death={"t": 1})
                return self.status()
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
                    elif landing in {"settled", "respawned"}:
                        status.update(pos=[10938.5, 64.0, -11007.7])
                    elif landing == "settled_above":
                        status.update(pos=[10938.4, 64.9, -11007.5])
                    elif landing == "in_water":
                        status.update(pos=[10937.7, 63.0, -11007.3], in_water=True)
            return {"ok": True}

    result = CombatEnv(ShoreBot()).escape_deep_water_to_shore(keep_shield=True)

    assert len(moves) == 2
    assert moves[1]["range"] < moves[0]["range"]
    assert moves[1]["timeout"] < moves[0]["timeout"]
    assert moves[1]["stop_on"] == ["death"]
    assert tuple(moves[1][axis] for axis in ("x", "y", "z")) == (10938.5, 64.0, -11007.5)
    assert result["ok"] is (landing in {"grounded", "settled", "settled_above"})
    assert result["in_water"] is (landing == "in_water")
    assert posture[-1]["jump"] is (landing == "in_water")
    assert landing_reads == ([0.25] if landing in {"settled", "settled_above", "respawned"} else [])
    if landing == "respawned":
        assert result["mode"] == "respawned"


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


@pytest.mark.parametrize("solid_exit", [True, False])
@pytest.mark.parametrize("after_step", ["unshielded", "blocking", "respawned"])
def test_guardian_checks_support_before_unstick_moves(solid_exit, after_step):
    movements = []
    faces = []
    death = {"t": 10}

    class LedgeBot:
        def status(self):
            return self.call("status", method="GET")

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 44, 0.5], "ready": True, "alive": True, "on_ground": True,
                    "health": 1.8, "blocking": after_step != "unshielded", "last_death": dict(death),
                    "nearest_hostile": {"id": 44, "name": "pillager", "dist": 10.2},
                }
            if action == "get_block":
                x, y, z = body["pos"]
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
    assert result["ok"] is solid_exit
    if solid_exit:
        step, = movements
        assert step["ms"] == 350 and not step["jump"] and not step["sprint"]
    else:
        assert result["error"]["code"] == "unsafe_landing" and result["requires_replan"] is True
        assert movements == []
    assert faces == ([44] if solid_exit and after_step == "blocking" else [])


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
@pytest.mark.parametrize("blocking", [False, True])
def test_bug076_unsafe_actor_exit_checks_another_route_when_a_hostile_is_close(tool, safe_exit, blocking):
    from mc_agent.skills.combat_env import yaw_to_unit_xz

    status = {
        "pos": [0.5, 64, 0.5], "alive": True, "ready": True, "on_ground": True, "health": 5.0, "blocking": blocking,
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
                if "entity_id" in body:
                    assert body["entity_id"] == 7
                    motions.append("face_attacker")
                else:
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
        assert (motions[-1] == "face_attacker") is blocking
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


@pytest.mark.parametrize("terrain", ["stone", "blocked_approach", "deep_drop", "lava", "unavailable"])
def test_dfs_probes_fresh_support_before_moving_to_a_mining_step(terrain):
    target = [23080.5, 34.0, 10014.5]
    events = []
    calls = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {"pos": [23081.5, 35, 10015.5], "on_ground": True, "ready": True, "alive": True, "health": 20}
            if action == "get_block":
                assert body["pos"] == [23080, 33, 10014]
                calls.append(action)
                if terrain == "unavailable":
                    raise httpx.ReadTimeout("support block was not read")
                return {"name": {"deep_drop": "air", "lava": "lava"}.get(terrain, "stone")}
            if action == "move_to":
                calls.append(action)
                if terrain == "blocked_approach":
                    raise ApiError("path_not_found", "terrain blocked the direct approach")
            if action == "control":
                calls.append(action)
            return {"ok": True}

    bot = ShaftBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None)

    def step():
        try:
            return json.dumps(bot.call("move_to", {
                "x": target[0], "y": target[1], "z": target[2], "range": 0.35,
                "timeout": 4500, "stop_on": ["hurt", "death"],
            }))
        except ApiError:
            return json.dumps(bot.call("control", {"forward": True, "ms": 420, "stop_on": ["hurt", "death"]}))

    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_dfs", step))

    if terrain in {"stone", "blocked_approach"}:
        assert result["ok"]
        assert calls == ["get_block", "move_to"] + (["control"] if terrain == "blocked_approach" else [])
    else:
        assert result["requires_replan"] and result["error"]["code"] == "unsafe_landing"
        assert calls == ["get_block"]
        event, = [row for row in events if row["type"] == "guardian.mining_landing_blocked"]
        assert event["target"] == result["error"]["target"] == target


@pytest.mark.parametrize("placement", [
    "complete", "existing_wall", "partial", "receipt_only", "threat_inside",
    "already_sealed", "sealed_with_exposed_mob", "sealed_unknown_threat",
])
def test_flee_only_confirms_a_shaft_seal_after_both_cells_are_solid(monkeypatch, placement):
    from mc_agent.skills.combat_env import CombatEnv

    blocks = {(1, 95, 0): "stone"} if placement == "existing_wall" else {}
    sealed = placement in {"already_sealed", "sealed_with_exposed_mob", "sealed_unknown_threat"}
    if sealed:
        blocks.update({(1, 95, 0): "tuff", (1, 96, 0): "tuff"})
    attempts = []
    status = {"pos": [0.5, 95, 0.5], "alive": True, "ready": True, "on_ground": True}
    threat = [0.9, 95, 0.5] if placement == "threat_inside" else [3.0, 95, 0.5]

    class ShaftBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 8}]}
            if action == "get_block":
                return {"name": blocks.get(tuple(body["pos"]), "air")}
            if action == "get_entities":
                if placement == "sealed_unknown_threat":
                    raise httpx.ReadTimeout("entity scan timed out")
                entities = [{"id": 42, "name": "zombie", "pos": threat}]
                if placement == "sealed_with_exposed_mob":
                    entities.append({"id": 43, "name": "spider", "pos": [0.5, 95, 2.5]})
                return {"entities": entities}
            if action == "place":
                cell = tuple(body["pos"])
                attempts.append(cell)
                if placement == "partial" and cell[1] == 96:
                    raise ApiError("blocked_by_entity", "placement failed")
                if placement != "receipt_only":
                    blocks[cell] = "cobblestone"
                return {"ok": True}
            pytest.fail(f"unexpected action {action}")

    env = CombatEnv(ShaftBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "carved_shaft"})
    monkeypatch.setattr(env, "probe_retreat", lambda *_a: None)
    result = env.flee(threat)

    complete = placement in {"complete", "existing_wall", "already_sealed"}
    assert result["ok"] is complete
    assert result["mode"] == ("sealed" if complete else "flee")
    if sealed or placement == "threat_inside":
        assert attempts == []
        if not complete:
            assert result["reason"] == "no_safe_retreat"
    else:
        assert attempts[-1] == (1, 96, 0)


@pytest.mark.parametrize("terrain", [
    "wall", "fire", "unknown", "drifted_during_probe", "missed_landing", "respawned",
    "live_shooter_covered", "shooter_flanked", "scan_timeout", "wall_removed_during_scan",
])
def test_bug076_natural_cover_requires_solid_shot_blocking_and_confirmed_landing(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64.0, 0.5], "alive": True, "ready": True, "on_ground": True, "health": 5.0, "food": 20,
        "nearest_hostile": {"id": 44, "name": "pillager", "dist": 8.0},
    }
    shooter = [8.5, 64.0, 0.5]
    moves = []
    status_reads = []
    wall_removed = False

    class CoverBot:
        def status(self):
            status_reads.append(True)
            if terrain == "drifted_during_probe" and len(status_reads) >= 2:
                status.update(pos=[1.0, 63.4, 0.5], on_ground=False)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            nonlocal wall_removed
            if action == "status":
                return self.status()
            if action == "events":
                return {"events": [{"seq": 1, "type": "hurt", "source": "arrow"}] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": [], "offhand": None}
            if action == "get_entities":
                if terrain == "scan_timeout":
                    raise httpx.ReadTimeout("shooter scan was unavailable")
                if terrain == "wall_removed_during_scan":
                    assert len(moves) == 1
                    wall_removed = True
                current_shooter = [0.5, 64.0, -5.0] if terrain == "shooter_flanked" else shooter
                return {"entities": [{"id": 44, "name": "pillager", "pos": current_shooter}]}
            if action == "get_block":
                x, y, z = body["pos"]
                if not wall_removed and y in (64, 65) and 3 <= x <= 5 and 1 <= z <= 2:
                    if terrain == "unknown":
                        raise httpx.ReadTimeout("cover voxel was not read")
                    return {"name": "fire" if terrain == "fire" else "stone"}
                return {"name": "stone" if y == 63 else "air"}
            if action == "move_to":
                moves.append(body)
                if terrain == "respawned":
                    status.update(pos=[1.5, 64.0, 0.5], last_death={"t": 1})
                elif terrain != "missed_landing":
                    status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    bot = CoverBot()
    if terrain == "wall":
        guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
        incident = guard.poll()
        result = next(a["result"] for a in incident["actions"] if a["action"] == "combat_or_flee")
    else:
        result = CombatEnv(bot).seek_natural_cover(
            shooter, verify_live_shooters=terrain in {
                "live_shooter_covered", "shooter_flanked", "scan_timeout", "wall_removed_during_scan",
            },
        )

    if terrain in {"fire", "unknown", "drifted_during_probe"}:
        assert result is None
        assert moves == []
    else:
        goal, = moves
        assert (goal["x"], goal["y"], goal["z"]) == pytest.approx((0.5, 64.0, 2.7))
        assert result["ok"] is (terrain in {"wall", "live_shooter_covered"})
        if result["ok"]:
            assert result["mode"] == "natural_cover"
        else:
            failed_rescan = terrain in {"shooter_flanked", "scan_timeout", "wall_removed_during_scan"}
            assert result["reason"] == (
                "shooter_lanes_unconfirmed" if failed_rescan else "cover_unconfirmed"
            )
            assert bool(result.get("respawned")) is (terrain == "respawned")


def test_stationary_natural_cover_rejects_an_exposed_hitbox_edge(monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv

    pos = [0.5, 64, 0.08]
    shooter = [8.5, 64, 0.08]

    class CornerBot:
        def status(self):
            return {"pos": pos, "ready": True, "alive": True, "on_ground": True}

        def call(self, action, body=None, **_kwargs):
            assert action == "get_block"
            x, y, z = body["pos"]
            return {"name": "stone" if x == 2 and z == 0 and y in (64, 65) else "air"}

    env = CombatEnv(CornerBot())
    monkeypatch.setattr(env, "probe_retreat", lambda *_args: None)

    assert all(not env.has_los(shooter, pos, dst_height=height)["clear"] for height in (0.75, 1.6))
    assert env.seek_natural_cover(shooter, verify_live_shooters=True) is None


@pytest.mark.parametrize("cover", ["confirmed", "exposed_edge", "wall_removed", "second_shooter"])
def test_arrow_poll_rechecks_natural_cover_before_remaining_in_place(monkeypatch, cover):
    from mc_agent.skills.combat_env import CombatEnv

    z = 0.08 if cover == "exposed_edge" else 0.5
    pos = [0.5, 71, z]
    shooter = {"id": 41, "name": "skeleton", "dist": 8.0, "pos": [8.5, 71, z]}
    flank = {"id": 42, "name": "skeleton", "dist": 6.0, "pos": [0.5, 71, z - 6.0]}
    status = {
        "pos": pos, "dimension": "overworld", "health": 5.833332, "food": 17,
        "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 41, "name": "skeleton", "dist": 8.0},
    }
    rescans = []
    retreats = []

    class ArrowBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [
                    {"seq": seq, "type": "hurt", "source": "arrow"}
                    for seq in (1, 2, 3) if seq > kwargs["params"]["since"]
                ]}
            if action == "inventory":
                return {"items": [{"name": "dark_oak_log", "count": 12}], "offhand": None}
            if action == "get_entities":
                if kwargs["timeout_s"] == 0.4:
                    rescans.append(action)
                    if cover == "second_shooter":
                        return {"entities": [shooter, flank]}
                return {"entities": [shooter]}
            if action == "get_block":
                x, y, block_z = body["pos"]
                wall_removed = cover == "wall_removed" and bool(rescans)
                if (x, block_z) == (2, 0) and y in (71, 72) and not wall_removed:
                    return {"name": "stone"}
                return {"name": "air"}
            pytest.fail(f"Unexpected action: {action}")

    def flee(_env, threat, **_kwargs):
        retreats.append(threat)
        return {"ok": False, "mode": "flee", "reason": "no_movement", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", lambda *_args: {"ok": False, "reason": "time_budget"})
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(ArrowBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    assert [action["action"] for action in incident["actions"]] == ["dig_cover", "combat_or_flee"]
    assert rescans == ([] if cover == "exposed_edge" else ["get_entities"])
    assert retreats == ([] if cover == "confirmed" else [shooter["pos"]])
    defense = incident["actions"][-1]
    assert defense["result"]["mode"] == ("natural_cover" if cover == "confirmed" else "flee")
    assert defense["ok"] is (cover == "confirmed")
    assert (guard._ranged_cover_active_until > guard.last_reaction) is (cover == "confirmed")


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


@pytest.mark.parametrize("landing", ["respawned", "airborne", "water"])
def test_blocked_flee_preserves_the_checked_retreat_observation(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv

    shooter = {"id": 44, "name": "skeleton", "dist": 6.0, "pos": [0.5, 69, 6.5]}
    origin = [0.5, 69, 0.5]
    spawn = [-79.5, 63.1, 75.5]
    fallback_pos = [2.5, 68.9, 0.7]
    status = {"pos": origin, "dimension": "overworld", "health": 0.8, "blocking": True,
              "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter}
    attempts = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            pytest.fail(f"Unexpected action during blocked flee: {action}")

    def flee(_env, target, **_kwargs):
        assert target == shooter["pos"]
        attempts.append(target)
        if landing == "respawned":
            return {"ok": False, "mode": "respawned", "respawned": True, "pos_after": spawn,
                    "moved_dist": 0.0, "reason": "unsafe_landing"}
        return {
            "ok": False, "mode": "flee", "moved_dist": 2.19, "pos_after": fallback_pos,
            "reason": "unsafe_landing", "on_ground": False, "in_water": landing == "water", "in_lava": False,
        }

    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    outcome = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    assert attempts == [shooter["pos"]]
    assert outcome["ok"] is False and outcome["requires_replan"] is True
    assert outcome["pos_before"] == origin
    if landing == "respawned":
        assert outcome["mode"] == "respawned" and outcome["respawned"] is True
        assert outcome["pos_after"] == spawn
    else:
        assert outcome["error"]["code"] == "unsafe_landing" and outcome["mode"] == "flee"
        assert outcome["pos_after"] == fallback_pos and outcome["moved_dist"] == 2.19
        assert outcome["on_ground"] is False and outcome["in_water"] is (landing == "water")


@pytest.mark.parametrize(
    "cover", ["safe", "exposed", "fresh_hit", "burning_after_hit", "burning_old_hit"],
)
def test_unsafe_nether_flee_rechecks_recent_cover_before_leaving(monkeypatch, cover):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    blaze = {"id": 44, "name": "blaze", "dist": 6.0, "pos": [0.5, 64, 6.5]}
    second = {"id": 45, "name": "blaze", "dist": 8.0, "pos": [-7.5, 64, 0.5]}
    status = {
        "pos": [0.5, 64, 0.5], "dimension": "the_nether", "health": 8.3,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": blaze,
    }
    if cover.startswith("burning_"):
        status.update(health=2.3, on_fire=True)
    checks = []

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                return {"entities": [blaze, second]}
            raise AssertionError(action)

    def seek_cover(_env, pos, *, other_shooters, allow_stationary=True):
        checks.append("cover")
        assert pos == blaze["pos"] and other_shooters == [second["pos"]]
        if cover != "exposed" and allow_stationary:
            return {"ok": True, "mode": "natural_cover", "moved_dist": 0.0, "pos_after": status["pos"]}
        return None

    def flee(_env, pos, **_kwargs):
        checks.append("flee")
        assert pos == blaze["pos"]
        return {"ok": True, "mode": "flee", "moved_dist": 2.2}

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = FortressBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._mark_ranged_engagement(987.0, attacker=blaze, in_cover=True)
    if cover == "fresh_hit":
        guard._last_ranged_hurt_at = 999.0
    if cover == "burning_after_hit":
        guard._last_ranged_hurt_at = 991.0
    if cover == "burning_old_hit":
        guard._last_ranged_hurt_at = 987.0
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    assert result["requires_replan"]
    assert result["mode"] == ("natural_cover" if cover in {"safe", "burning_old_hit"} else "flee")
    assert checks == {
        "safe": ["cover"],
        "exposed": ["cover", "flee"],
        "fresh_hit": ["flee"],
        "burning_after_hit": ["cover", "flee"],
        "burning_old_hit": ["cover"],
    }[cover]


@pytest.mark.parametrize("blocking, landing", [
    (False, "covered"), (False, "exposed"), (False, "new_life"), (False, "shooter_gone"), (False, "healthy"),
    (False, "cover_moved"), (False, "cover_stationary"),
    (True, "covered"), (True, "exposed"), (True, "new_life"), (True, "shooter_gone"), (True, "near_melee"),
])
def test_blocked_flee_checks_cover_after_a_critical_landing(monkeypatch, blocking, landing):
    from mc_agent.skills.combat_env import CombatEnv

    shooter = {"id": 44, "name": "skeleton", "dist": 5.5, "pos": [0.5, 77, 6.0]}
    origin = [0.5, 77, 0.5]
    landing_pos = [0.5, 77, -2.0]
    failed_cover_pos = [-2.1, 76.2, -2.2]
    status = {"pos": origin, "dimension": "overworld", "health": 1.55 if blocking else 8.5,
              "blocking": blocking, "on_ground": True,
              "ready": True, "alive": True, "last_death": {"t": 1}, "nearest_hostile": shooter}
    checks = []

    class RidgeBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                entities = [shooter]
                if status["pos"] == landing_pos and landing == "near_melee":
                    entities.append({"id": 45, "name": "zombie", "dist": 5.4})
                return {"entities": entities}
            if action == "look" and blocking:
                return {"ok": True}
            pytest.fail(f"Unexpected action during retreat: {action}")

    def flee(_env, target, **_kwargs):
        assert target == shooter["pos"]
        checks.append("flee")
        health_after = {"healthy": 8.5}.get(landing, 1.55 if blocking else 4.5)
        shooter_dist = 4.5 if landing == "near_melee" else 9.4 if blocking else 8.0
        status.update(pos=landing_pos, health=health_after,
                      nearest_hostile=None if landing == "shooter_gone" else {**shooter, "dist": shooter_dist},
                      last_death={"t": 2 if landing == "new_life" else 1})
        return {"ok": True, "mode": "flee", "moved_dist": 2.5, "pos_after": landing_pos}

    def cover(_env, target, *, other_shooters, allow_stationary):
        assert target == shooter["pos"] and not other_shooters and not allow_stationary
        if status["pos"] == origin:
            checks.append("initial_cover")
            return None
        checks.append("cover")
        if landing == "covered":
            return {"ok": True, "mode": "natural_cover", "pos_after": [-1.7, 77, -2.0]}
        if landing in {"cover_moved", "cover_stationary"}:
            status["pos"] = failed_cover_pos if landing == "cover_moved" else landing_pos
            return {"ok": False, "mode": "natural_cover", "reason": "cover_unconfirmed", "pos_after": status["pos"]}
        return None

    monkeypatch.setattr(CombatEnv, "flee", flee)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    bot = RidgeBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    if blocking:
        monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
        guard._last_ranged_hurt_at = 999.0
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    expected_checks = ["initial_cover", "flee"] if blocking else ["flee"]
    if landing in {"covered", "exposed", "cover_moved", "cover_stationary"}:
        expected_checks.append("cover")
    assert checks == expected_checks
    assert result["mode"] == {
        "covered": "natural_cover", "cover_moved": "natural_cover", "new_life": "respawned",
    }.get(landing, "flee")
    assert result["ok"] is (landing not in {"new_life", "cover_moved"}) and result["requires_replan"]
    if landing == "cover_moved":
        assert result["pos_after"] == failed_cover_pos and result["reason"] == "cover_unconfirmed"


@pytest.mark.parametrize("terrain", ["cliff", "same_column", "missing_target", "clear", "new_life", "moved", "expired"])
def test_attack_rechecks_footing_near_a_recent_failed_retreat(monkeypatch, terrain):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    origin = [0.5, 9, 0.5]
    hostile = {"id": 42, "name": "zombie", "pos": [5.5, 9, 0.5], "dist": 5.0}
    status = {"pos": origin, "dimension": "overworld", "last_death": {"t": 1}, "nearest_hostile": hostile,
              "health": 8, "food": 20, "ready": True, "alive": True, "on_ground": True}
    attacks = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_entities":
                return {"entities": []}
            if action == "get_block":
                x, y, _ = body["pos"]
                return {"name": "stone" if y == 8 and (terrain == "clear" or x <= 1) else "air"}
            if action == "attack":
                attacks.append(body["entity_id"])
                return {"ok": True, "hits": 1}
            pytest.fail(f"Unexpected action during attack: {action}")

    monkeypatch.setattr(CombatEnv, "probe_retreat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(CombatEnv, "flee", lambda *_args, **_kwargs: {
        "ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0, "pos_after": origin,
    })
    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    flee = guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 5.5, "z": 0.5, "stop_on": ["death"]}),
    ))
    assert json.loads(flee)["error"]["code"] == "unsafe_landing"

    clock[0] += 11.0 if terrain == "expired" else 4.8
    if terrain == "same_column":
        hostile["pos"] = [0.5, -22, 0.5]
    elif terrain == "missing_target":
        hostile.pop("pos")
    elif terrain == "new_life":
        status["last_death"] = {"t": 2}
    elif terrain == "moved":
        status["pos"] = [4.5, 9, 0.5]
    attack = json.loads(guard.run_tool_with_reflex("mc_attack", lambda: json.dumps(
        bot.call("attack", {"entity_id": 42, "max_hits": 8}),
    )))

    safe = terrain in {"clear", "new_life", "moved", "expired"}
    assert attacks == ([42] if safe else [])
    assert attack["ok"] is safe
    if not safe:
        assert attack["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize("pressure", ["arrow", "stale_arrow", "melee_and_arrow", "exposed"])
def test_unsafe_overworld_flee_checks_cover_when_critical_arrows_bypass_shield(monkeypatch, pressure):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    skeleton = {"id": 44, "name": "skeleton", "dist": 2.0, "pos": [0.5, 73, 2.5]}
    pillager = {"id": 45, "name": "pillager", "dist": 7.8, "pos": [-7.3, 73, 0.5]}
    status = {
        "pos": [0.5, 73, 0.5], "dimension": "overworld", "health": 2.6, "blocking": True,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": skeleton,
    }
    checks = []

    class CliffBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": "air"}
            if action == "get_entities":
                return {"entities": [skeleton, pillager]}
            raise AssertionError(action)

    def seek_cover(_env, pos, *, other_shooters, allow_stationary):
        checks.append("cover")
        assert pos == skeleton["pos"] and other_shooters == [pillager["pos"]]
        assert allow_stationary is False
        if pressure == "exposed":
            return None
        return {"ok": True, "mode": "natural_cover", "moved_dist": 2.2, "pos_after": [2.7, 73, 0.5]}

    def flee(_env, pos, **kwargs):
        checks.append("flee")
        assert kwargs.get("other_shooters", []) == ([pillager["pos"]] if pressure == "exposed" else [])
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    bot = CliffBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_ranged_hurt_at = 991.0 if pressure == "stale_arrow" else 999.0
    if pressure == "melee_and_arrow":
        guard._last_melee_hurt_at = 999.0
    result = json.loads(guard.run_tool_with_reflex("mc_skill_flee", lambda: json.dumps(
        bot.call("move_to", {"x": 8.5, "z": 0.5, "stop_on": ["death"]}),
    )))

    assert result["requires_replan"]
    if pressure == "arrow":
        assert result["mode"] == "natural_cover"
        assert checks == ["cover"]
    else:
        assert result["error"]["code"] == "unsafe_landing"
        assert checks == (["cover", "flee"] if pressure == "exposed" else ["flee"])


@pytest.mark.parametrize("entry", ["poll", "boundary"])
@pytest.mark.parametrize("failure", ["timeout", "death", "failed_respawn"])
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
                if failure == "failed_respawn":
                    status.update(pos=[-152.5, 71, -382.5], health=20, last_death={"t": 2})
                    return {"ok": False, "error": {"code": "unsafe_landing"}, "pos_after": status["pos"]}
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


@pytest.mark.parametrize("safe_bank", [True, False])
def test_repeated_magma_damage_after_successful_sneak_seeks_safe_ground(monkeypatch, safe_bank):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    hurt = {"seq": 1, "type": "hurt", "source": "hot_floor"}
    status = {"pos": [0.5, 64.0, 0.5], "health": 19.54, "food": 20, "ready": True,
              "alive": True, "on_ground": True, "blocking": True, "sneaking": False, "dimension": "the_nether"}
    calls = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [dict(hurt)] if kwargs["params"]["since"] < hurt["seq"] else []}
            if action == "inventory":
                return {"items": []}
            if action == "get_block":
                x, y, z = body["pos"]
                if y < 64:
                    return {"name": "netherrack" if safe_bank and (x, z) != (0, 0) else "magma_block"}
                return {"name": "air"}
            calls.append((action, body))
            if action == "posture":
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            if action == "move_to":
                status["pos"] = [body["x"], body["y"], body["z"]]
            return {"ok": True}

    guard = GenerationGuard(MagmaBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    initial = guard.poll()
    assert initial["requires_replan"] is False
    assert calls == [("posture", {"sneak": True, "shield": True, "jump": False})]
    clock[0] += 4.0
    hurt["seq"] += 1
    status["health"] = 19.08

    repeat = guard.poll()

    assert repeat["requires_replan"] is True
    action, = repeat["actions"]
    assert action["ok"] is safe_bank
    if safe_bank:
        assert action["result"]["mode"] == "stepped_off"
        assert calls[-1][0] == "move_to"
        assert status["pos"] != [0.5, 64.0, 0.5]
    else:
        assert action["result"]["reason"] == "no_safe_hot_floor_exit"
        assert all(action == "posture" for action, _ in calls)


@pytest.mark.parametrize("floor", ["magma_block", "netherrack"])
@pytest.mark.parametrize(("dimension", "tool", "shooter"), [
    ("the_nether", "mc_skill_staircase", "blaze"), ("overworld", "pre_ensure", "skeleton"),
])
def test_critical_tool_preemption_checks_floor_before_combat(monkeypatch, floor, dimension, tool, shooter):
    status = {
        "pos": [2014.3, 76, 1890.2], "health": 0.25, "dimension": dimension,
        "ready": True, "alive": True, "on_ground": True, "blocking": True, "sneaking": False,
    }
    postures = []
    combat = []

    class HazardBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                assert body["pos"][1] == 75 and kwargs["timeout_s"] <= 0.6
                return {"name": floor}
            if action == "posture":
                postures.append(body)
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            pytest.fail(f"Unexpected action during critical preemption: {action}")

    guard = GenerationGuard(HazardBot(), EventSink(lambda _: None), checkpoint=lambda: None)

    def respond(env, trigger):
        combat.append(trigger["primary_type"])
        return {"handled": True, "status_after": dict(status), "mode": "cover_or_flee"}

    monkeypatch.setattr(guard.combat_slot, "handle", respond)
    receipt = guard.handle_preemption(
        {"source": "tool_interrupted_hurt", "primary_type": shooter}, interrupted_name=tool,
    )

    if floor == "magma_block":
        assert receipt["reflex_mode"] == "hot_floor"
        assert postures == [{"sneak": True, "shield": True, "jump": False}]
        assert combat == []
    else:
        assert receipt["reflex_mode"] == "cover_or_flee"
        assert postures == [] and combat == [shooter]


@pytest.mark.parametrize("floor", ["magma_block", "stone"])
def test_critical_move_rechecks_recent_hot_floor_before_dispatch(monkeypatch, floor):
    status = {
        "pos": [6174.3, 66, 56.3], "health": 1.3, "dimension": "overworld",
        "ready": True, "alive": True, "on_ground": True, "sneaking": True,
    }
    actions = []

    class PlateauBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                return {"name": floor}
            if action == "move_to":
                actions.append("move")
                return {"ok": True}
            pytest.fail(f"Unexpected action {action}")

    bot = PlateauBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_hot_floor_block = guard._hot_floor_block(status)

    def step_off(_status):
        actions.append("checked_exit")
        return {"ok": False, "reason": "no_safe_hot_floor_exit"}

    monkeypatch.setattr(guard, "_step_off_hot_floor", step_off)
    receipt = json.loads(guard.run_tool_with_reflex(
        "mc_move_to", lambda: json.dumps(bot.call("move_to", {"x": 6170, "y": 66, "z": 50, "stop_on": ["death"]})),
    ))
    if floor == "magma_block":
        assert actions == ["checked_exit"]
        assert receipt["error"]["code"] == "hot_floor" and receipt["requires_replan"]
    else:
        assert actions == ["move"] and receipt["ok"]


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


@pytest.mark.parametrize(("terrain", "elapsed"), [("water", 21.0), ("stone", 21.0), ("water", 31.0)])
def test_critical_ranged_chase_checks_for_water_after_landing(monkeypatch, terrain, elapsed):
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 100.0)
    pillager = {"id": 5475818, "name": "pillager", "dist": 6.9, "pos": [-10924.6, 62.5, -10944.3]}
    status = {
        "pos": [-10921.5, 63, -10950.4], "health": 3.3, "ready": True, "alive": True,
        "on_ground": True, "dimension": "overworld", "nearest_hostile": pillager,
    }
    attacks = []

    class ShoreBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                y = body["pos"][1]
                return {"name": terrain if y == 62 else "air"}
            if action == "attack":
                attacks.append(body["entity_id"])
                raise httpx.ReadTimeout("attack response timed out")
            raise AssertionError(action)

    bot = ShoreBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_water_escape_at = 100.0 - elapsed

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": pillager["id"], "timeout": 6000}))

    if terrain == "water" and elapsed < guard.WATER_ESCAPE_RETRY_WINDOW_S:
        receipt = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert receipt["error"]["code"] == "unsafe_landing" and receipt["requires_replan"]
        assert attacks == []
    else:
        with pytest.raises(httpx.ReadTimeout, match="attack response timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert attacks == [pillager["id"]]


@pytest.mark.parametrize("terrain", ["solid", "drop", "lava", "unknown"])
def test_nether_attack_checks_footing_toward_nearby_blaze(terrain):
    calls = []
    blaze = {"id": 4645315, "name": "blaze", "dist": 1.3, "pos": [1.8, 65, 0.5]}

    class FortressBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 65, 0.5], "health": 17.64, "ready": True, "alive": True,
                    "on_ground": True, "dimension": "the_nether", "nearest_hostile": blaze,
                }
            if action == "get_block":
                x, y, _ = body["pos"]
                if y == 64:
                    if x == 0 or terrain == "solid":
                        return {"name": "netherrack"}
                    if terrain == "unknown":
                        raise httpx.ReadTimeout("unverified footing")
                    return {"name": "lava" if terrain == "lava" else "air"}
                return {"name": "air"}
            if action == "attack":
                calls.append(action)
                raise httpx.ReadTimeout("attack response timed out")
            raise AssertionError(action)

    bot = FortressBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": blaze["id"], "timeout": 5000}))

    if terrain in {"solid", "unknown"}:
        with pytest.raises(httpx.ReadTimeout, match="attack response timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert calls == ["attack"]
    else:
        result = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert calls == []
        assert result["requires_replan"] and result["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize("attack_result", ["returned", "interrupted", "read_timeout"])
@pytest.mark.parametrize("hazard", [
    "water", "ranged_water", "lava", "unarmed_melee", "unarmed_above_critical", "other_melee", "other_ranged",
])
def test_attack_hazard_requests_stop_and_preserves_read_timeout(monkeypatch, attack_result, hazard):
    import threading

    actor_thread = threading.current_thread()
    stopped = threading.Event()
    observed = threading.Event()
    actions = []
    armed = hazard in {"other_melee", "other_ranged"}
    attack_target = 44 if armed else 27
    above_critical = hazard == "unarmed_above_critical"
    in_water = hazard in {"water", "ranged_water"}
    drowned_distance = 8.5 if hazard == "ranged_water" else 2.0
    hostile_pos = [0.5 + drowned_distance, 62, 0.5]
    hostile_name = {"other_ranged": "pillager", "water": "drowned", "ranged_water": "drowned"}.get(hazard, "zombie")
    status = {
        "pos": [0.5, 63, 0.5], "health": 18 if above_critical else 12, "ready": True, "alive": True, "on_ground": True,
        "nearest_hostile": {"id": 27, "name": hostile_name, "dist": drowned_distance, "pos": hostile_pos},
    }

    class AttackBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                current = dict(status)
                if threading.current_thread() is not actor_thread:
                    observed.set()
                return current
            if action == "get_entities":
                return {"entities": []}
            actions.append(action)
            if action == "attack":
                if in_water or hazard == "lava":
                    health = 9.98 if hazard == "ranged_water" else 6.7
                    status.update(in_water=in_water, in_lava=hazard == "lava", on_ground=False, health=health)
                else:
                    assert observed.wait(2.0)
                    weapon = "iron_sword" if armed else "wooden_pickaxe"
                    status.update(health=12 if above_critical else 6.7, held={"name": weapon})
                assert stopped.wait(2.0), "attack remained active after the survival hazard"
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
        assert threat_pos == hostile_pos
        assert actions[:2] == ["attack", "stop"]
        actions.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    def escape_lava():
        assert actions[:2] == ["attack", "stop"]
        actions.append("lava_escape")
        status.update(in_lava=False, on_ground=True)
        return {"ok": True, "in_lava": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", escape_to_shore)
    monkeypatch.setattr(guard.combat_env, "escape_lava", escape_lava)
    monkeypatch.setattr(
        guard, "_escape_water", lambda *_args, **_kwargs: pytest.fail("drowned attack should hand off directly to shore"),
    )

    def retreat(_env, trigger):
        assert trigger["source"] == "tool_interrupted_hurt" and trigger["primary_entity_id"] == 27
        actions.append("retreat")
        return {"handled": True, "mode": "cover_or_flee", "actions": [], "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", retreat)

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": attack_target, "timeout": 6000}))

    if attack_result == "read_timeout":
        with pytest.raises(httpx.ReadTimeout, match="attack timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert actions == ["attack", "stop"]
    else:
        receipt = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert receipt["preempted_by_reflex"] is True
        assert not receipt["status_after"]["in_water"]
        assert actions.count("attack") == 1
        assert {"water": "shore", "ranged_water": "shore", "lava": "lava_escape"}.get(hazard, "retreat") in actions
        assert not status.get("in_lava")


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


@pytest.mark.parametrize("tool, action, body, oxygen", [
    ("mc_skill_mine_dfs", "dig", {"pos": [41013, 22, 11034]}, 110),
    ("mc_skill_mine_blocks", "dig", {"pos": [41012, 21, 11034], "stop_on": ["death"]}, 110),
    ("mc_skill_mine_blocks", "dig", {"pos": [41012, 21, 11034], "stop_on": ["death"]}, 285),
    ("mc_skill_unstick", "control", {"jump": True, "ms": 900, "stop_on": ["death"]}, 110),
])
def test_tool_water_threat_preempts_before_the_first_hit(monkeypatch, tool, action, body, oxygen):
    import threading

    stopped = threading.Event()
    status = {
        "pos": [41012.5, 22, 11034.4], "health": 20, "oxygen": 285,
        "ready": True, "alive": True, "on_ground": True, "in_water": False,
    }
    calls = []
    drowned = {"id": 45, "name": "drowned", "dist": 4.0, "pos": [41016.5, 22, 11034.4]}

    class CaveBot:
        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return dict(status)
            if endpoint == action:
                calls.append(endpoint)
                status.update(in_water=True, submerged=True, on_ground=False, oxygen=oxygen)
                if oxygen == 285:
                    status["nearest_hostile"] = drowned
                assert stopped.wait(1.5)
            if endpoint == "stop":
                calls.append(endpoint)
                stopped.set()
            if endpoint == "inventory":
                return {"items": []}
            if endpoint == "events":
                return {"events": []}
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def shore(pos, *, surface_first, shield_target_id, **_kwargs):
        assert surface_first and status["health"] == 20
        if oxygen == 285:
            assert pos == drowned["pos"] and shield_target_id == drowned["id"]
        calls.append("surface")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", shore)
    result = json.loads(guard.run_tool_with_reflex(tool, lambda: json.dumps(bot.call(action, body))))
    assert result["reflex_mode"] == "water_surface"
    assert calls == [action, "stop", "surface"]
    assert guard._water_hurt_streak == 0


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("contact", ["in_lava", "lava_hurt", "lava_hurt_then_heal"])
def test_lava_watcher_preempts_death_only_mining_and_waits_for_stop(monkeypatch, interrupted, contact):
    import threading
    import time

    stop_started = threading.Event()
    dig_returning = threading.Event()
    stop_finished = threading.Event()
    watcher_sampled = threading.Event()
    status = {
        "pos": [6391.6, 64, -6359.7], "health": 20, "ready": True, "alive": True,
        "dimension": "overworld", "on_ground": True, "in_lava": False,
    }
    escapes = []

    class LavaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                snapshot = dict(status)
                if threading.current_thread().name == "mc-creeper-watch":
                    watcher_sampled.set()
                return snapshot
            if action == "get_block":
                return {"name": "stone"}
            if action == "events":
                if contact != "in_lava" and status.get("on_fire"):
                    return {"events": [{"seq": 1, "type": "hurt", "source": "lava"}]}
                return {"events": []}
            if action == "dig":
                assert body["stop_on"] == ["death"]
                assert watcher_sampled.wait(1.5)
                status.update(
                    pos=[6391.6, 63, -6359.7], health=4.3 if contact == "in_lava" else 16.528, on_ground=contact != "in_lava",
                    in_lava=contact == "in_lava", on_fire=True,
                    nearest_hostile={"id": 255310, "name": "witch", "dist": 12.1},
                )
                assert stop_started.wait(1.5)
                dig_returning.set()
                if interrupted:
                    raise ApiError("interrupted", "stopped")
            if action == "stop" and threading.current_thread().name == "mc-creeper-watch":
                stop_started.set()
                assert dig_returning.wait(1.5)
                time.sleep(0.12)
                stop_finished.set()
            return {"ok": True}

    bot = LavaBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def escape(*, force_rim_escape=False):
        assert stop_finished.is_set()
        assert force_rim_escape is (contact != "in_lava")
        escapes.append(status["pos"])
        status.update(pos=[6390.5, 64, -6359.7], on_ground=True, in_lava=False)
        if contact == "lava_hurt_then_heal":
            status["health"] = 20
        return {"ok": True, "in_lava": False}

    monkeypatch.setattr(guard.combat_env, "escape_lava", escape)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_mine_blocks", lambda: json.dumps(bot.call(
        "dig", {"pos": [6393, 64, -6360], "stop_on": ["death"]},
    ))))

    assert escapes == [[6391.6, 63, -6359.7]]
    assert result["reflex_mode"] == "lava_escape" and result["status_after"]["on_ground"] is True
    assert "并脱困" in result["message"]


@pytest.mark.parametrize("retry, expected_bank", [
    ("same_life", "known"),
    ("bank_flooded", "alternate"),
    ("new_life", "alternate"),
    ("after_window", "alternate"),
])
def test_bug098_repeated_mining_water_preemption_prefers_verified_dry_tunnel(monkeypatch, retry, expected_bank):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    wet = [10.5, 22, 10.5]
    known_bank = [9.5, 22, 10.5]
    alternate_bank = [12.5, 22, 10.5]
    status = {
        "pos": wet, "health": 20, "oxygen": 110, "dimension": "overworld", "last_death": {"t": 1},
        "ready": True, "alive": True, "in_water": True, "submerged": True, "on_ground": False,
    }
    moves = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                y = body["pos"][1]
                return {"name": "stone" if y == 21 else "water" if y == 22 and retry == "bank_flooded" else "air"}
            if action == "move_to":
                goal = [body[key] for key in ("x", "y", "z")]
                moves.append(goal)
                status.update(pos=goal, in_water=False, submerged=False, on_ground=True)
            if action == "events":
                return {"events": []}
            return {"ok": True}

    guard = GenerationGuard(CaveBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "_find_walkable_shore_target", lambda *_a, **_kw: alternate_bank)
    trigger = {"source": "tool_water_threat", "primary_type": "water"}
    first = guard.handle_preemption(trigger, interrupted_name="mc_skill_mine_dfs")
    assert first["reflex_mode"] == "water_surface" and "矿道重复进水" not in first["message"]

    clock[0] += 61.0 if retry == "after_window" else 5.0
    if retry == "new_life":
        status["last_death"] = {"t": 2}
    guard.combat_env.remember_dry_bank({**status, "pos": known_bank})
    status.update(pos=wet, in_water=True, submerged=True, on_ground=False)
    second = guard.handle_preemption(trigger, interrupted_name="mc_skill_unstick")

    assert second["reflex_mode"] == "water_surface" and second["status_after"]["in_water"] is False
    assert moves == [alternate_bank, known_bank if expected_bank == "known" else alternate_bank]
    assert ("避开水格 (10, 22, 10)" in second["message"]) is (retry in {"same_life", "bank_flooded"})


@pytest.mark.parametrize("landing, expected_error", [
    ("shore", None),
    ("settled", None),
    ("respawned", "respawned"),
    ("unready", "shore_landing_unconfirmed"),
    ("airborne", "shore_landing_unconfirmed"),
    ("reentered", "water_escape_failed"),
    ("wet", "water_escape_failed"),
    ("status_timeout", "shore_landing_unconfirmed"),
])
def test_unstick_water_success_requires_confirmed_same_life_landing(landing, expected_error):
    status = {
        "pos": [-7420.7, 61.5, 2631.7], "health": 12, "oxygen": 300, "last_death": {"t": 1},
        "dimension": "overworld", "ready": True, "alive": True, "on_ground": False, "in_water": True,
    }
    finished = False

    class SwimBot:
        def call(self, action, body=None, **kwargs):
            assert action == "status"
            if finished and landing == "status_timeout":
                raise httpx.ReadTimeout("landing status unavailable")
            if finished and kwargs.get("timeout_s") == 0.25:
                if landing == "settled":
                    status.update(pos=[-7422.5, 63.0, 2630.5], on_ground=True)
                elif landing == "reentered":
                    status.update(pos=[-7422.5, 62.4, 2630.5], in_water=True)
            return dict(status)

    bot = SwimBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def unstick():
        nonlocal finished
        start = bot.call("status", method="GET")
        assert start["in_water"]
        status.update(pos=[-7422.5, 64, 2630.5], in_water=False, on_ground=True)
        if landing == "respawned":
            status.update(pos=[-7421.5, 64.1, 2805.5], ready=False, last_death={"t": 2})
        elif landing == "unready":
            status["ready"] = False
        elif landing in {"airborne", "settled", "reentered"}:
            status["on_ground"] = False
        elif landing == "wet":
            status["in_water"] = True
        finished = True
        return json.dumps({"ok": True, "escaped_water": True, "in_water": False, "pos_after": status["pos"]})

    result = json.loads(guard.run_tool_with_reflex("mc_skill_unstick", unstick))

    assert result["ok"] is (expected_error is None)
    assert result["escaped_water"] is result["ok"]
    if landing == "settled":
        assert result["pos_after"] == [-7422.5, 63.0, 2630.5]
    if expected_error:
        assert result["requires_replan"] is True and result["error"]["code"] == expected_error
        assert result.get("respawned", False) is (landing == "respawned")


@pytest.mark.parametrize(("tool_name", "distance", "wet", "should_preempt"), [
    ("mc_skill_scout_surface", 4.0, True, True),
    ("mc_skill_scout_surface", 8.0, True, False),
    ("mc_move_to", 9.0, True, True),
    ("mc_move_to", 11.0, True, False),
    ("mc_move_to", 9.0, False, False),
])
def test_movement_hands_off_water_near_drowned_before_damage(monkeypatch, tool_name, distance, wet, should_preempt):
    import threading

    moving = threading.Event()
    observed = threading.Event()
    stopped = threading.Event()
    status = {
        "pos": [-11052.6, 64, 11027], "health": 20, "alive": True, "ready": True, "on_ground": True,
        "nearest_hostile": {"id": 44, "name": "drowned", "dist": distance, "pos": [-11048.6, 62, 11027]},
    }
    calls = []

    class MovingBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                if moving.is_set():
                    observed.set()
                return dict(status)
            if action == "inventory":
                return {"items": []}
            if action == "move_to":
                calls.append(action)
                status.update(in_water=wet, on_ground=not wet)
                moving.set()
                if should_preempt:
                    assert stopped.wait(2.0), "movement continued near a drowned after entering water"
                else:
                    assert observed.wait(2.0)
            if action == "stop":
                calls.append(action)
                stopped.set()
            return {"ok": True}

    bot = MovingBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard, "_escape_water", lambda *_a, **_kw: pytest.fail("direct shore recovery expected"))

    def reach_shore(pos, *, keep_shield, shield_target_id):
        assert shield_target_id == 44 and pos == [-11048.6, 62, 11027]
        calls.append("shore")
        status.update(in_water=False, on_ground=True)
        return {"ok": True, "in_water": False}

    monkeypatch.setattr(guard.combat_env, "escape_deep_water_to_shore", reach_shore)
    result = json.loads(guard.run_tool_with_reflex(tool_name, lambda: json.dumps(
        bot.call("move_to", {"x": -11042, "z": 11027, "stop_on": ["death"]}),
    )))
    if should_preempt:
        assert calls == ["move_to", "stop", "shore"]
        assert result["reflex_mode"] == "water_surface" and not result["status_after"]["in_water"]
        assert result["status_after"]["health"] == 20
        assert guard._water_hurt_streak == 0
    else:
        assert calls == ["move_to"] and result["ok"] is True


@pytest.mark.parametrize("interrupted_error", [False, True])
def test_close_creeper_preempts_movement_that_ignores_ordinary_hurt(monkeypatch, interrupted_error):
    import threading

    stopped = threading.Event()
    creeper = {"id": 44, "name": "creeper", "dist": 4.0, "pos": [0.5, 64, 4.5]}
    status = {
        "pos": [0.5, 64, 0.5], "health": 20, "alive": True, "ready": True, "on_ground": True,
        "nearest_hostile": creeper,
    }
    calls = []

    class MovingBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "move_to":
                calls.append("move_to")
                assert body["stop_on"] == ["death"]
                assert stopped.wait(2.0)
                if interrupted_error:
                    raise ApiError("interrupted", "movement stopped")
            if action == "stop":
                calls.append("stop")
                stopped.set()
            return {"ok": True}

    bot = MovingBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def handle(_env, trigger):
        assert trigger["source"] == "pre_hurt_creeper" and trigger["primary_entity_id"] == 44
        calls.append("creeper_evade")
        return {"handled": True, "mode": "creeper_evade", "status_after": dict(status)}

    monkeypatch.setattr(guard.combat_slot, "handle", handle)
    result = json.loads(guard.run_tool_with_reflex(
        "mc_move_to", lambda: json.dumps(bot.call("move_to", {"x": 10, "z": 10, "stop_on": ["death"]})),
    ))

    assert result["preempted_by_reflex"] and result["reflex_mode"] == "creeper_evade"
    assert calls == ["move_to", "stop", "creeper_evade"]


@pytest.mark.parametrize("terrain", ["netherrack", "drop", "lava"])
def test_nether_combat_checks_footing_before_issuing_a_chase_pulse(terrain):
    moves = []
    status = {
        "pos": [0.5, 64, 0.5], "yaw": 0.0, "health": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "the_nether",
    }

    class CombatBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "get_block":
                _, y, z = body["pos"]
                if y == 63:
                    return {"name": "netherrack" if terrain == "netherrack" or z >= 0 else "lava" if terrain == "lava" else "air"}
                return {"name": "air"}
            if action in {"control", "stop"}:
                moves.append(action)
                return {"ok": True}
            raise AssertionError(action)

    bot = CombatBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    result = json.loads(guard.run_tool_with_reflex("mc_skill_combat", lambda: json.dumps(
        bot.call("control", {"forward": True, "sprint": True, "ms": 250, "wait": False, "stop_on": []}),
    )))

    if terrain == "netherrack":
        assert moves == ["control"] and result["ok"]
    else:
        assert moves == ["stop"]
        assert result["requires_replan"] and result["error"]["code"] == "unsafe_landing"


@pytest.mark.parametrize(("tool", "endpoint", "interrupted_error"), [
    ("mc_skill_mine_dfs", "dig", False), ("mc_skill_combat", "move_to", False),
    ("mc_move_to", "move_to", False), ("mc_move_to", "move_to", True),
    ("mc_skill_combat", "control", False), ("mc_skill_combat", "control", True),
])
def test_magma_interrupts_active_tool_before_the_boundary(tool, endpoint, interrupted_error):
    import threading

    stopped = threading.Event()
    status = {
        "pos": [0.5, 38.0, 0.5], "health": 19.5, "food": 20, "on_ground": True,
        "ready": True, "alive": True, "dimension": "the_nether", "sneaking": False,
    }
    actions = []

    class MagmaBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": []}
            if action == "get_block":
                assert body["pos"] == [0, 37, 0]
                return {"name": "magma_block"}
            if action == endpoint:
                assert stopped.wait(3.0)
                if interrupted_error:
                    raise ApiError("interrupted", "stopped because of stop")
                return {"interrupted_by": "hurt"}
            if action == "stop":
                actions.append("stop")
                stopped.set()
            if action == "posture":
                actions.append("sneak")
                status["sneaking"] = body["sneak"]
                return {"sneaking": status["sneaking"]}
            return {"ok": True}

    bot = MagmaBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)
    body = {"stop_on": ["death"]} if tool == "mc_move_to" else {}
    if endpoint == "control":
        body = {"forward": True, "stop_on": ["death"]}
    result = json.loads(guard.run_tool_with_reflex(tool, lambda: json.dumps(bot.call(endpoint, body))))
    assert result["reflex_mode"] == "hot_floor"
    assert result["requires_replan"] is True
    assert status["sneaking"] is True
    assert actions == ["stop", "sneak"]


























@pytest.mark.parametrize("pocket, covered", [
    ("no_filler_block", True),
    ("no_filler_block", False),
    ("no_solid_wall", True),
    ("no_solid_wall", False),
    ("cover_not_entered", False),
])
@pytest.mark.parametrize("hurt_source", ["arrow", "trident"])
def test_projectile_poll_without_a_diggable_pocket_checks_cover_before_flee(monkeypatch, pocket, covered, hurt_source):
    from mc_agent.skills.combat_env import CombatEnv

    trident = hurt_source == "trident"
    shooter = {"id": 41, "name": "drowned" if trident else "skeleton", "dist": 8.0 if trident else 2.0}
    live_shooter = {**shooter, "pos": [0.5, 69, 0.5 + shooter["dist"]]}
    status = {
        "pos": [0.5, 69, 0.5], "dimension": "overworld", "health": 7.17 if trident else 16.0, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    checks = []

    class StairBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": hurt_source, "source_entity": shooter}
                return {"events": [] if kwargs["params"]["since"] else [hurt], "last_seq": 1}
            if action == "inventory":
                return {"items": [{"name": "stone_pickaxe", "count": 1}]}
            if action == "get_entities":
                return {"entities": [live_shooter]}
            raise AssertionError(action)

    def dig_cover(_env, _skills):
        checks.append("dig")
        return {"ok": False, "reason": pocket}

    def seek_cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters=False):
        checks.append("natural_cover")
        assert pos == live_shooter["pos"]
        assert other_shooters == []
        assert allow_stationary is (pocket == "cover_not_entered")
        assert verify_live_shooters is (not trident)
        if covered:
            return {"ok": True, "mode": "natural_cover", "pos_after": [2.7, 69, 0.5]}
        return None

    def flee(_env, _pos, **_kwargs):
        checks.append("flee")
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", dig_cover)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(StairBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    incident = guard.poll()

    expected = ["dig"]
    if trident or pocket in {"no_filler_block", "no_solid_wall"}:
        expected.append("natural_cover")
    if not covered:
        expected.append("flee")
    assert checks == expected
    assert incident["actions"][-1]["result"]["mode"] == ("natural_cover" if covered else "flee")


@pytest.mark.parametrize("landing", ["covered", "exposed", "respawned", "no_shooter", "near_melee"])
@pytest.mark.parametrize("has_shield", [False, True])
@pytest.mark.parametrize("hurt_source, shooter_name, with_attacker", [
    ("arrow", "skeleton", True),
    ("trident", "drowned", True),
    ("trident", "drowned", False),
])
def test_critical_projectile_retreat_checks_cover_from_landing(
    monkeypatch, landing, has_shield, hurt_source, shooter_name, with_attacker,
):
    from mc_agent.skills.combat_env import CombatEnv

    shooter = {"id": 41, "name": shooter_name, "dist": 8.0, "pos": [8.5, 102, 0.5]}
    fresh_dig_failure = has_shield and hurt_source == "arrow" and landing == "covered"
    origin = [0.5, 102 if fresh_dig_failure else 102.2, 0.5]
    status = {
        "pos": origin, "dimension": "overworld", "health": 4.0, "food": 20,
        "ready": True, "alive": True, "on_ground": fresh_dig_failure,
        "nearest_hostile": shooter, "blocking": has_shield,
    }
    checks = []
    melee = {"id": 42, "name": "zombie", "dist": 2.0}

    class HillBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": hurt_source}
                if with_attacker:
                    hurt["source_entity"] = shooter
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"} if has_shield else None}
            if action == "get_entities":
                checks.append("scan")
                entities = [] if landing == "no_shooter" else [shooter]
                if landing == "no_shooter" and hurt_source == "trident":
                    entities.extend([{**shooter, "id": 49}, {**shooter, "id": 50, "name": "skeleton"}])
                if landing == "near_melee":
                    entities.append(melee)
                return {"entities": entities}
            if action == "look" and has_shield:
                return {"ok": True}
            pytest.fail(f"Unexpected call: {action}")

    def flee(_env, _pos, **_kwargs):
        checks.append("flee")
        moved = 0.71 if landing == "covered" else 5.86
        status.update(pos=[0.5 - moved, 102, 0.5], on_ground=True, nearest_hostile=None)
        if landing == "respawned":
            status["last_death"] = {"t": 2}
        elif landing == "near_melee":
            status["nearest_hostile"] = melee
        return {"ok": True, "mode": "flee", "moved_dist": moved, "pos_after": status["pos"]}

    def seek_cover(_env, pos, *, other_shooters, allow_stationary, verify_live_shooters=False):
        assert pos == shooter["pos"] and not other_shooters and allow_stationary
        assert verify_live_shooters is (status["pos"] != origin and hurt_source == "arrow" and not has_shield)
        if status["pos"] == origin:
            checks.append("initial_cover")
            return None
        checks.append("cover")
        if landing == "covered":
            return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}
        return None

    def dig_cover(_env, _skills):
        assert fresh_dig_failure
        checks.append("dig_cover")
        return {"ok": False, "reason": "no_solid_wall"}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", dig_cover)
    guard = GenerationGuard(HillBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if has_shield:
        monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
        if not fresh_dig_failure:
            guard._last_dig_cover_fail_at = 999.0
    incident = guard.poll()

    expected_checks = ["flee"] if has_shield else ["initial_cover", "flee"]
    expected_actions = ["face_attacker", "combat_or_flee"] if has_shield else ["combat_or_flee"]
    if fresh_dig_failure:
        expected_checks[:0] = ["dig_cover", "scan", "initial_cover"]
        expected_actions.insert(1, "dig_cover")
    if landing != "respawned":
        expected_checks.append("scan")
        expected_actions.append("natural_cover_after_retreat")
    if landing in {"covered", "exposed"}:
        expected_checks.append("cover")
    if has_shield:
        expected_actions.append("face_attacker_after_retreat")
    assert checks == expected_checks
    assert [action["action"] for action in incident["actions"]] == expected_actions
    assert (guard._ranged_cover_active_until > guard.last_reaction) is (landing == "covered")


@pytest.mark.parametrize(("reason", "grounded"), [
    ("no_safe_retreat", True), ("unsafe_landing", True), ("unsafe_landing", False),
])
def test_critical_unshielded_arrow_retreat_checks_cover_after_a_stalled_landing(monkeypatch, reason, grounded):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    shooter = {"id": 41, "name": "skeleton", "dist": 7.0}
    status = {
        "pos": [22788.3, 63.2, 17812.7], "dimension": "overworld", "health": 6.7, "food": 17,
        "ready": True, "alive": True, "on_ground": False, "nearest_hostile": shooter, "last_death": {"t": 1},
    }

    class QuarryBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": shooter}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 24}], "offhand": None}
            if action == "get_entities":
                return {"entities": [shooter]}
            raise AssertionError(action)

    guard = GenerationGuard(QuarryBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_dig_cover_block = (22788, 63, 17812)
    guard._last_dig_cover_at = clock[0] - 15

    def defend(landing, attacker, counts, *, cover_only=False, **kwargs):
        if cover_only:
            assert landing["on_ground"] and landing["health"] == 3.7 and kwargs["prefer_cover"]
            return {"ok": True, "mode": "natural_cover"}
        status.update(health=3.7, pos=[22788.3, 63 if grounded else 63.2, 17812.7], on_ground=grounded)
        return {"ok": False, "mode": "flee", "reason": reason, "moved_dist": 0.0, "pos_after": status["pos"]}

    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    incident = guard.poll()

    assert [action["action"] for action in incident["actions"]] == (
        ["combat_or_flee", "natural_cover_after_retreat"] if grounded else ["combat_or_flee"]
    )


@pytest.mark.parametrize("situation", [
    "covered", "receipt_only", "knockback", "near_melee", "two_shooters", "shooter_moved",
    "logs_only", "logs_and_one_stone", "above_critical", "no_materials", "wall_removed_in_rescan",
    "placed_cover_exposed_edge",
    "arrow_burst_with_logs", "followup_arrow_with_logs", "single_arrow_with_logs", "stale_arrow_with_logs",
    "arrow_burst_with_one_log",
])
def test_critical_arrow_retreat_builds_verified_cover_when_no_natural_cover_exists(monkeypatch, situation):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    z = 7381.08 if situation == "placed_cover_exposed_edge" else 7381.5
    shooter = {"id": 41, "name": "skeleton", "dist": 8, "pos": [9824.9, 63, z]}
    status = {
        "pos": [9816.9, 63.2, z], "dimension": "overworld", "health": 5.5, "food": 17,
        "ready": True, "alive": True, "on_ground": False, "last_death": {"t": 1},
        "nearest_hostile": {"id": 41, "name": "skeleton", "dist": 8},
    }
    entities = [shooter]
    if situation == "near_melee":
        entities.append({"id": 42, "name": "zombie", "dist": 3, "pos": [9816.9, 63, 7378.1]})
    if situation == "two_shooters":
        entities.append({"id": 43, "name": "skeleton", "dist": 8, "pos": [9816.9, 63, 7373.1]})
    sustained_arrows = situation in {"arrow_burst_with_logs", "arrow_burst_with_one_log"}
    previous_arrow = situation in {"followup_arrow_with_logs", "stale_arrow_with_logs"}
    early_landing = sustained_arrows or previous_arrow or situation == "single_arrow_with_logs"
    material = "oak_log" if situation in {"logs_only", "logs_and_one_stone"} else "cobblestone"
    if early_landing:
        material = "dark_oak_log"
        status["health"] = 9.7
    items = [] if situation == "no_materials" else [{"name": material, "count": 3}]
    if early_landing:
        items[0]["count"] = 1 if situation == "arrow_burst_with_one_log" else 4
    if situation == "logs_and_one_stone":
        items.insert(0, {"name": "cobblestone", "count": 1})
    blocks = {}
    attempts = []
    hits = [{"seq": 1, "type": "hurt", "source": "arrow"}]
    if sustained_arrows or previous_arrow:
        hits.append({"seq": 2, "type": "hurt", "source": "arrow"})
    available_events = 1

    class RavineBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                return {"events": [hit for hit in hits[:available_events] if hit["seq"] > kwargs["params"]["since"]]}
            if action == "inventory":
                return {"items": items, "offhand": None}
            if action == "get_entities":
                if situation == "wall_removed_in_rescan" and len(attempts) == 2:
                    blocks.clear()
                return {"entities": entities}
            if action == "get_block":
                cell = tuple(body["pos"])
                return {"name": blocks.get(cell, "stone" if cell[1] < 63 else "air")}
            if action == "place":
                cell = tuple(body["pos"])
                attempts.append(cell)
                assert body["item"] == material and kwargs["timeout_s"] <= 0.65
                if situation != "receipt_only":
                    blocks[cell] = material
                if situation == "knockback":
                    status["pos"] = [9816.4, 63, 7381.1]
                if situation == "shooter_moved" and len(attempts) == 2:
                    shooter["pos"] = [9816.9, 63, 7373.1]
                return {"ok": True}
            raise AssertionError(action)

    guard = GenerationGuard(RavineBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    if previous_arrow:
        guard._drain_events_pages()
        clock[0] += 8.1 if situation == "stale_arrow_with_logs" else 5.5
    available_events = len(hits)

    def defend(_position, _attacker, _counts, *, cover_only=False, **_kwargs):
        if cover_only:
            return {"ok": False, "mode": "natural_cover", "reason": "no_safe_natural_cover"}
        landing_hp = 6.1 if situation == "above_critical" else 4.833 if material == "oak_log" else 2.48
        if early_landing:
            landing_hp = 6.166667
        status.update(health=landing_hp, pos=[9816.9, 63, z], on_ground=True)
        return {"ok": True, "mode": "flee", "moved_dist": 2.31, "pos_after": status["pos"]}

    monkeypatch.setattr(guard, "_defend_or_flee", defend)
    incident = guard.poll()

    action = incident["actions"][-1]
    no_cover = {"above_critical", "single_arrow_with_logs", "stale_arrow_with_logs", "arrow_burst_with_one_log"}
    if situation in no_cover or situation == "no_materials":
        assert action["action"] == ("combat_or_flee" if situation in no_cover else "natural_cover_after_retreat")
        assert attempts == []
        return
    assert action["action"] == "place_arrow_cover"
    cover_verified = situation in {"covered", "logs_only", "logs_and_one_stone", "arrow_burst_with_logs", "followup_arrow_with_logs"}
    assert action["ok"] is cover_verified
    exposed_after_placement = situation in {"shooter_moved", "wall_removed_in_rescan", "placed_cover_exposed_edge"}
    if exposed_after_placement:
        assert action["result"]["reason"] == "projectile_lane_unconfirmed"
    if cover_verified or exposed_after_placement:
        assert attempts == [(9818, 63, 7381), (9818, 64, 7381)]
    elif situation in {"receipt_only", "knockback"}:
        assert attempts == [(9818, 63, 7381)]
    else:
        assert attempts == []


@pytest.mark.parametrize("scenario", ["cornered", "second_hostile", "out_of_reach", "respawned", "blocked_by_wall"])
def test_cornered_shielded_bot_strikes_a_lone_reachable_zombie_and_keeps_guard(monkeypatch, scenario):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    distance = 1.5 if scenario == "out_of_reach" else 0.6
    zombie = {
        "id": 11, "name": "zombie_villager", "dist": distance, "pos": [0.5 + distance, 29, 0.5],
        "blocked_by_wall": scenario == "blocked_by_wall",
    }
    status = {
        "pos": [0.5, 29, 0.5], "dimension": "overworld", "health": 4.7, "food": 20,
        "alive": True, "ready": True, "on_ground": True, "blocking": False,
        "nearest_hostile": zombie, "last_death": {"t": 1},
    }
    calls = []

    class TunnelBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                sword = {"name": "iron_sword", "count": 1}
                return {"items": [sword], "held": sword, "offhand": {"name": "shield", "count": 1}}
            if action == "get_entities":
                mobs = [zombie]
                if scenario == "second_hostile":
                    mobs.append({"id": 12, "name": "husk", "dist": 2.0, "pos": [0.5, 29, 2.5]})
                return {"entities": mobs}
            if action == "get_block":
                return {"name": "air"}
            if action in {"attack", "posture"}:
                calls.append((action, body))
            if action == "attack":
                assert status["blocking"] is True
                status["blocking"] = False
                return {"hits": 1}
            if action == "posture":
                status["blocking"] = bool(body.get("shield"))
            return {"ok": True}

    def flee(_env, _pos, **_kwargs):
        if scenario == "respawned":
            status.update(health=20, last_death={"t": 2})
        return {"ok": False, "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "flee", flee)
    outcome = CombatControllerSlot().handle(CombatEnv(TunnelBot()), {
        "source": "pre_hurt_melee", "primary_type": "zombie_villager", "primary_entity_id": 11,
    })

    attacks = [body for action, body in calls if action == "attack"]
    if scenario == "cornered":
        attack, = attacks
        assert attack["entity_id"] == 11 and attack["max_hits"] == 1
        assert outcome["actions"][-1]["action"] == "cornered_melee_strike"
        assert outcome["latched_guard"] is True and status["blocking"] is True
    else:
        assert attacks == []


@pytest.mark.parametrize("landing", ["critical", "healthy", "new_life", "airborne", "close_melee", "long_retreat"])
def test_short_ranged_retreat_checks_cover_before_the_next_arrow(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    shooter = {"id": 41, "name": "skeleton", "dist": 2.4, "pos": [2.9, -9, 0.5]}
    status = {
        "pos": [0.5, -9, 0.5], "health": 15.2, "food": 19, "alive": True, "ready": True,
        "dimension": "overworld", "on_ground": True, "nearest_hostile": shooter, "last_death": {"t": 1},
    }
    checks = []

    class CaveBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": [{"name": "stone_sword", "count": 1}]}
            if action == "get_entities":
                return {"entities": [shooter]}
            return {"ok": True}

    def retreat(_pos, **_kwargs):
        moved = 3.0 if landing == "long_retreat" else 1.41
        status.update(pos=[0.5 - moved, -9, 0.5], health=12 if landing == "healthy" else 6.17,
                      on_ground=landing != "airborne", last_death={"t": 2 if landing == "new_life" else 1})
        if landing == "close_melee":
            status["nearest_hostile"] = {"id": 42, "name": "zombie", "dist": 0.9}
        return {"ok": True, "mode": "flee", "moved_dist": moved}

    def cover(pos, *, other_shooters, allow_stationary, verify_live_shooters):
        assert pos == shooter["pos"] and not other_shooters and allow_stationary and verify_live_shooters
        checks.append(status["pos"])
        return {"ok": True, "mode": "natural_cover"}

    env = CombatEnv(CaveBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(env, "flee", retreat)
    monkeypatch.setattr(env, "seek_natural_cover", cover)
    outcome = CombatControllerSlot().handle(env, {
        "source": "pre_hurt_ranged", "primary_type": "skeleton", "primary_entity_id": 41,
    })

    assert checks == ([status["pos"]] if landing == "critical" else [])
    assert (outcome["mode"] == "natural_cover") is (landing == "critical")


@pytest.mark.parametrize("fallback", ["covered", "exposed", "respawned", "near_melee", "little_time"])
def test_interrupted_arrow_tool_checks_natural_cover_after_no_safe_retreat(monkeypatch, fallback):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    shooter = {"id": 41, "name": "skeleton", "dist": 4.3, "pos": [4.8, 71, 0.5]}
    second_shooter = {"id": 42, "name": "skeleton", "dist": 7.0, "pos": [0.5, 71, 7.5]}
    hostiles = [shooter, second_shooter]
    if fallback == "near_melee":
        hostiles.append({"id": 43, "name": "spider", "dist": 2.0, "pos": [2.5, 71, 0.5]})
    status = {"pos": [0.5, 71, 0.5], "health": 3.33, "food": 17, "alive": True, "ready": True,
              "dimension": "overworld", "on_ground": True, "nearest_hostile": shooter}
    checks = []

    class RidgeBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": hostiles}
            return {"ok": True}

    def flee(pos, **kwargs):
        assert pos == shooter["pos"] and kwargs["wait_for_ground"]
        checks.append("flee")
        if fallback == "little_time":
            clock[0] += 6.0
        return {"ok": False, "moved_dist": 0.0, "reason": "no_safe_retreat", "respawned": fallback == "respawned"}

    def seek_cover(pos, *, other_shooters, allow_stationary, verify_live_shooters):
        assert pos == shooter["pos"] and other_shooters == [second_shooter["pos"]] and not allow_stationary
        assert verify_live_shooters
        checks.append("cover")
        if fallback == "covered":
            status.update(pos=[0.5, 71, -1.7])
            return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}
        return None

    env = CombatEnv(RidgeBot())
    monkeypatch.setattr(env, "arena_snapshot", lambda _pos: {"enclosure": "surface"})
    monkeypatch.setattr(env, "flee", flee)
    monkeypatch.setattr(env, "seek_natural_cover", seek_cover)
    outcome = CombatControllerSlot().handle(env, {
        "source": "tool_interrupted_hurt", "hurt_source": "arrow", "primary_entity_id": 41, "primary_type": "skeleton",
    })

    assert checks == (["flee", "cover"] if fallback in {"covered", "exposed"} else ["flee"])
    assert outcome["mode"] == ("natural_cover" if fallback == "covered" else "cover_or_flee")


@pytest.mark.parametrize("event_source", [
    "arrow", "same_shooter_missing_pos", "other_shooter", "old_arrow", "melee", "read_timeout", "new_life",
])
def test_mining_reflex_recovers_missing_arrow_source_before_a_stalled_retreat(monkeypatch, event_source):
    monkeypatch.setattr("mc_agent.harness.guardian.time.time", lambda: 1000.0)
    shooter = {"id": 505830, "name": "skeleton", "pos": [41043.6, -2, 18938.5], "dist": 3.1}
    alternate = {"id": 505831, "name": "skeleton", "pos": [41040.5, -2, 18944.5], "dist": 6.0}
    targeted = alternate if event_source == "other_shooter" else shooter
    status = {
        "pos": [41040.5, -2, 18938.5], "health": 3.0, "food": 17, "dimension": "overworld",
        "alive": True, "ready": True, "on_ground": True, "last_death": {"t": 2 if event_source == "new_life" else 1},
        "nearest_hostile": shooter,
    }
    event = {
        "seq": 1, "t": 995000 if event_source == "old_arrow" else 999500,
        "type": "hurt", "source": "mob_attack" if event_source == "melee" else "arrow",
        "source_entity": targeted,
    }
    if event_source == "same_shooter_missing_pos":
        event["source_entity"] = {"id": shooter["id"], "name": shooter["name"]}
    calls = []

    class ShaftBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                if kwargs.get("timeout_s") == 0.4:
                    calls.append("read_hurt")
                    if event_source == "read_timeout":
                        raise httpx.ReadTimeout("hurt events not returned")
                return {"events": [event] if kwargs["params"]["since"] < 1 else []}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                if event_source == "same_shooter_missing_pos":
                    return {"entities": [event["source_entity"]]}
                return {"entities": [shooter, alternate] if event_source == "other_shooter" else [shooter]}
            return {"ok": True}

    def flee(pos, **_kwargs):
        calls.append("flee")
        assert pos == targeted["pos"]
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    def cover(pos, *, other_shooters, allow_stationary, verify_live_shooters):
        calls.append("cover")
        assert pos == targeted["pos"] and not allow_stationary and verify_live_shooters
        assert other_shooters == ([shooter["pos"]] if event_source == "other_shooter" else [])
        return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}

    guard = GenerationGuard(ShaftBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": "open_cave"})
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    monkeypatch.setattr(guard.combat_env, "seek_natural_cover", cover)
    result = guard.handle_preemption({
        "source": "tool_interrupted_hurt", "primary_type": "skeleton", "primary_entity_id": shooter["id"],
        "primary_pos": shooter["pos"], "distance": 3.1, "observed_life": ("overworld", {"t": 1}),
    }, interrupted_name="mc_skill_mine_dfs")

    if event_source == "new_life":
        assert calls == [] and result["reflex_mode"] == "life_unavailable"
    elif event_source in {"arrow", "same_shooter_missing_pos", "other_shooter"}:
        assert calls == ["read_hurt", "flee", "cover"] and result["reflex_mode"] == "natural_cover"
    else:
        assert calls == ["read_hurt", "flee"] and result["reflex_mode"] == "cover_or_flee"


@pytest.mark.parametrize("unbroken_ledge", [True, False])
def test_short_retreat_checks_diagonal_supported_ledge(unbroken_ledge):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    origin = [0.3, 63.0, 0.3]
    status = {"pos": origin, "on_ground": True, "alive": True, "ready": True, "dimension": "overworld"}
    support = {(-2, -1), (-2, 0), (-1, -1), (-1, 0), (0, -1), (0, 0)}
    if not unbroken_ledge:
        support.remove((-2, -1))
    steps = []
    away = math.radians(15)
    away_dx, away_dz = yaw_to_unit_xz(away)
    threat = [origin[0] - 2.8 * away_dx, origin[1], origin[2] - 2.8 * away_dz]

    class LedgeBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                return {"name": "stone" if y == 62 and (x, z) in support else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            elif action == "control":
                assert body["ms"] == 220 and not body["jump"] and not body["sprint"]
                steps.append(self.yaw)
                dx, dz = yaw_to_unit_xz(self.yaw)
                status["pos"] = [origin[0] + 0.8 * dx, origin[1], origin[2] + 0.8 * dz]
            elif action == "move_to":
                pytest.fail("Short ledge cannot support a second retreat leg")
            return {"ok": True}

    outcome = CombatEnv(LedgeBot()).flee(threat, allow_shaft_seal=False, wait_for_ground=True)

    assert outcome["ok"] is unbroken_ledge
    assert steps == ([pytest.approx(math.radians(85))] if unbroken_ledge else [])
    if not unbroken_ledge:
        assert outcome["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("short_only", [False, True])
def test_creeper_retreat_does_not_take_the_only_route_toward_the_blast(monkeypatch, short_only):
    from mc_agent.skills.combat_env import CombatEnv

    status = {"pos": [0.5, 66, 0.5], "on_ground": True, "alive": True, "ready": True, "dimension": "overworld"}

    class RidgeBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            pytest.fail(f"Retreat dispatched {action} toward the creeper")

    def probe(_pos, yaw, travel, **_kwargs):
        # The ridge opens only toward the creeper at +X.
        return (66, False) if math.sin(yaw) < -0.8 and (not short_only or travel <= 1.65) else None

    env = CombatEnv(RidgeBot())
    monkeypatch.setattr(env, "probe_retreat", probe)
    result = env.flee(
        [5.7, 66, 0.5], distance=11.5, allow_shaft_seal=False, wait_for_ground=True,
        other_melee=[[-1.0, 66, 0.5]], avoid_blast=True,
    )

    assert result["reason"] == "no_safe_retreat" and result["moved_dist"] == 0.0


@pytest.mark.parametrize("distance, short_only", [(3.0, False), (7.0, False), (3.0, True)])
def test_creeper_retreat_avoids_a_second_creeper_on_the_escape_heading(monkeypatch, distance, short_only):
    from mc_agent.skills.combat_env import CombatControllerSlot, CombatEnv, yaw_to_unit_xz

    origin = [0.5, 66, 0.5]
    primary = {"id": 11, "name": "creeper", "dist": 2.6, "pos": [-2.1, 66, 0.5]}
    second = {"id": 12, "name": "creeper", "dist": distance, "pos": [0.5 + distance, 66, 0.5]}
    status = {
        "pos": origin, "health": 3.85, "ready": True, "alive": True, "on_ground": True,
        "dimension": "overworld", "nearest_hostile": primary,
    }
    path = []

    class RespawnBot:
        yaw = 0.0

        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [{**mob, "dist": math.dist(status["pos"], mob["pos"])} for mob in (primary, second)]}
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 66 else "air"}
            if action == "look" and "yaw" in body:
                self.yaw = body["yaw"]
            if action == "control" and body.get("forward"):
                dx, dz = yaw_to_unit_xz(self.yaw)
                travel = 1.65 if short_only else 3.0
                x, y, z = status["pos"]
                status["pos"] = [x + travel * dx, y, z + travel * dz]
                path.append(status["pos"])
            if action == "move_to":
                status["pos"] = [body["x"], body["y"], body["z"]]
                path.append(status["pos"])
            return {"ok": True}

    if short_only:
        def probe(_env, _pos, yaw, travel, *, allow_step_down=False):
            if allow_step_down:
                return None
            dx, dz = yaw_to_unit_xz(yaw)
            if travel <= 1.65 and (dx > 0.95 or (dx < -0.3 and dz > 0.8)):
                return 66, False
            return None

        monkeypatch.setattr(CombatEnv, "probe_retreat", probe)
    outcome = CombatControllerSlot().handle(CombatEnv(RespawnBot()), {
        "source": "pre_hurt_creeper", "primary_type": "creeper", "primary_entity_id": 11, "primary_pos": primary["pos"],
    })

    assert outcome["mode"] == "creeper_evade"
    assert path
    assert all(math.dist(p, second["pos"]) >= min(distance, 6.0) - 1e-6 for p in path), path


@pytest.mark.parametrize("enclosure, endpoint", [("open_cave", "dig"), ("carved_shaft", "place")])
@pytest.mark.parametrize("post_timeout", ["ready", "busy", "respawned", "water"])
def test_combat_fortification_timeout_leaves_time_to_retreat(monkeypatch, enclosure, endpoint, post_timeout):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    shooter = {"id": 42, "name": "skeleton", "dist": 9.0, "pos": [9.5, 24, 0.5]}
    status = {
        "pos": [0.5, 24, 0.5], "dimension": "overworld", "last_death": {"t": 1},
        "health": 17, "ready": True, "alive": True, "on_ground": True, "nearest_hostile": shooter,
    }
    timeline = []
    events = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "inventory":
                return {"items": [{"name": "cobblestone", "count": 4}]}
            if action == "get_entities":
                return {"entities": [shooter]}
            if action == "stop":
                timeline.append(action)
            if action == endpoint:
                timeline.append(action)
                assert 0.0 < kwargs["timeout_s"] <= 3.0
                clock[0] += kwargs["timeout_s"] + 0.001
                if post_timeout == "busy":
                    status["busy"] = endpoint
                if post_timeout == "respawned":
                    status.update(last_death={"t": 2}, health=20)
                if post_timeout == "water":
                    status["in_water"] = True
                raise httpx.ReadTimeout("fortification timed out")
            return {"ok": True}

    bot = CaveBot()
    guard = GenerationGuard(bot, EventSink(events.append), checkpoint=lambda: None, skills=MacroSkills(bot))

    def build(mode, **_kwargs):
        assert mode == ("full_seal" if enclosure == "carved_shaft" else "cover_pocket")
        return guard.bot.call(endpoint)

    def flee(*_args, **_kwargs):
        assert guard.bot.active_deadline - clock[0] >= 3.0
        timeline.append("flee")
        return {"ok": True, "mode": "flee", "moved_dist": 2.3}

    monkeypatch.setattr(guard.combat_env, "arena_snapshot", lambda _pos: {"enclosure": enclosure})
    monkeypatch.setattr(guard.combat_env, "build_tactical", build)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    outcome = guard._run_subcortical_combat({
        "source": "pre_hurt_ranged", "primary_entity_id": 42, "primary_type": "skeleton", "distance": 9.0,
    })

    assert timeline == ["stop", endpoint, "stop"] + (["flee"] if post_timeout == "ready" else [])
    event, = [event for event in events if event["type"] == "guardian.fortification_budget_exceeded"]
    assert event["endpoint"] == endpoint
    if post_timeout == "ready":
        assert outcome["actions"][0]["reason"] == "fortification_budget"
        assert outcome["actions"][1]["action"] == "flee" and outcome["actions"][1]["ok"]
    else:
        assert outcome["mode"] == ("life_unavailable" if post_timeout == "respawned" else "combat_timeout")
        assert outcome["status_after"] == status


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


@pytest.mark.parametrize("sighting", ["nearest", "scan", "unconfirmed"])
def test_melee_attack_blocks_a_confirmed_creeper_before_starting(sighting):
    target_id = 5325659
    creeper = {"id": target_id, "name": "creeper", "dist": 5.4}
    attacks = []

    class ScoutBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return {
                    "pos": [0.5, 67, 0.5], "health": 19, "ready": True,
                    "alive": True, "on_ground": True, "dimension": "overworld",
                    "nearest_hostile": creeper if sighting == "nearest" else {"id": 91, "name": "zombie", "dist": 4.0},
                }
            if action == "get_entities":
                return {"entities": [creeper] if sighting == "scan" else [{**creeper, "id": target_id + 1}]}
            if action == "attack":
                attacks.append(body)
                raise httpx.ReadTimeout("attack reply timed out")
            raise AssertionError(action)

    bot = ScoutBot()
    guard = GenerationGuard(bot, EventSink(lambda _: None), checkpoint=lambda: None)

    def attack():
        return json.dumps(bot.call("attack", {"entity_id": target_id, "max_hits": 3}))

    if sighting == "unconfirmed":
        with pytest.raises(httpx.ReadTimeout, match="attack reply timed out"):
            guard.run_tool_with_reflex("mc_attack", attack)
        assert len(attacks) == 1
    else:
        result = json.loads(guard.run_tool_with_reflex("mc_attack", attack))
        assert result["requires_replan"]
        assert result["error"]["code"] == "unsafe_creeper_attack"
        assert attacks == []


@pytest.mark.parametrize("landing", ["dry", "water", "respawned"])
def test_retreat_waits_for_ground_after_pathfinder_leg(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv, yaw_to_unit_xz

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    status = {
        "pos": [0.5, 66, 0.5], "dimension": "overworld", "on_ground": True,
        "ready": True, "alive": True, "last_death": {"t": 1},
    }
    steps = []

    class KnockbackBot:
        yaw = 0.0

        def status(self):
            if steps == ["pulse", "pathfinder"] and clock[0] >= 1000.15:
                status["on_ground"] = True
                if landing == "water":
                    status["in_water"] = True
                if landing == "respawned":
                    status.update(pos=[2.5, 68, 2.5], last_death={"t": 2})
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "get_block":
                return {"name": "stone" if body["pos"][1] < 66 else "air"}
            if action == "look":
                self.yaw = body["yaw"]
            if action == "control":
                steps.append("pulse")
                dx, dz = yaw_to_unit_xz(self.yaw)
                status["pos"] = [0.5 + 2 * dx, 66, 0.5 + 2 * dz]
            if action == "move_to":
                steps.append("pathfinder")
                status.update(pos=[body["x"], body["y"] + 0.2, body["z"]], on_ground=False)
            return {"ok": True}

    outcome = CombatEnv(KnockbackBot()).flee([-2.5, 66, 0.5], wait_for_ground=True, allow_shaft_seal=False)

    assert steps == ["pulse", "pathfinder"]
    assert 1000.15 <= clock[0] <= 1000.35
    assert outcome["ok"] is (landing == "dry")
    if landing == "water":
        assert outcome["reason"] == "unsafe_landing"
    if landing == "respawned":
        assert outcome["respawned"] is True and outcome["moved_dist"] == 0.0


@pytest.mark.parametrize("shooter_visible", [True, False])
def test_arrow_retreat_uses_the_live_shooter_when_nearest_hostile_is_a_creeper(monkeypatch, shooter_visible):
    from mc_agent.skills.combat_env import CombatEnv

    creeper = {"id": 77, "name": "creeper", "dist": 4.2, "pos": [4.7, 30, 0.5]}
    skeleton = {"id": 88, "name": "skeleton", "dist": 8.0, "pos": [0.5, 30, -7.5]}
    status = {
        "pos": [0.5, 30, 0.5], "dimension": "overworld", "health": 4.7, "food": 20,
        "ready": True, "alive": True, "on_ground": True, "nearest_hostile": creeper,
    }
    retreat_targets = []

    class GullyBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                events = [{"seq": 1, "type": "hurt", "source": "arrow"}]
                return {"events": [] if kwargs["params"]["since"] else events, "last_seq": 1}
            if action == "inventory":
                return {"items": []}
            if action == "get_entities":
                return {"entities": [creeper, skeleton] if shooter_visible else [creeper]}
            return {"ok": True}

    def flee(_env, threat_pos, **kwargs):
        retreat_targets.append((threat_pos, kwargs.get("other_melee", [])))
        return {"ok": False, "mode": "flee", "reason": "no_safe_retreat", "moved_dist": 0.0}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", lambda *_: {"ok": False, "reason": "no_solid_wall"})
    def seek_cover(*_args, **_kwargs):
        pytest.fail("nearby creeper makes stationary cover unsafe")

    monkeypatch.setattr(CombatEnv, "seek_natural_cover", seek_cover)
    monkeypatch.setattr(CombatEnv, "flee", flee)
    guard = GenerationGuard(GullyBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    assert guard.poll() is not None

    expected = (skeleton["pos"], [creeper["pos"]]) if shooter_visible else (creeper["pos"], [])
    assert retreat_targets == [expected]


@pytest.mark.parametrize("followup", ["stalled", "progress", "new_life"])
def test_water_escape_reselects_a_shore_after_a_stalled_path(followup, monkeypatch):
    from mc_agent.skills.combat_env import CombatEnv

    blocked_bank = [0.5, 64, -4.5]
    alternative_bank = [3.5, 63, 0.5]
    status = {
        "pos": [0.5, 62.8, 0.5], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    attempts = []

    class CreekBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "move_to":
                bank = [body[key] for key in ("x", "y", "z")]
                attempts.append(bank)
                if len(attempts) == 1:
                    raise ApiError("path_not_found", "bank is unreachable")
                status.update(pos=bank, in_water=False, on_ground=True)
            if action == "control" and body.get("forward") and len(attempts) == 1:
                if followup == "progress":
                    status["pos"] = [0.5, 63.2, -2.0]
                else:
                    raise ApiError("blocked", "cannot clear the lip")
            return {"ok": True}

    env = CombatEnv(CreekBot())

    def choose_shore(_pos, *, excluded_shores, **_kwargs):
        return alternative_bank if tuple(blocked_bank) in excluded_shores else blocked_bank

    monkeypatch.setattr(env, "_find_walkable_shore_target", choose_shore)
    monkeypatch.setattr(env, "_dry_bank_is_walkable", lambda _pos: True)
    assert env.escape_deep_water_to_shore()["in_water"] is True
    if followup == "new_life":
        status["last_death"] = {"t": 2}
    assert env.escape_deep_water_to_shore()["ok"] is True

    assert attempts == [blocked_bank, alternative_bank if followup == "stalled" else blocked_bank]


@pytest.mark.parametrize("reentry", ["stalled", "confirmed_landing", "new_life"])
def test_water_escape_changes_bank_after_dry_airborne_lip_attempt(monkeypatch, reentry):
    from mc_agent.skills.combat_env import CombatEnv

    bank = [-5211.5, 24, -7783.5]
    alternate = [-5209.5, 23, -7784.5]
    below_bank = [-5211.5, 23.5, -7784.3]
    status = {
        "pos": [-5211.5, 23, -7784.3], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    attempts = []

    class CavePoolBot:
        def status(self):
            return dict(status)

        def call(self, endpoint, body=None, **kwargs):
            if endpoint == "status":
                return self.status()
            if endpoint == "move_to":
                target = [body[key] for key in ("x", "y", "z")]
                attempts.append(target)
                if len(attempts) > 2:
                    status.update(pos=target, in_water=False, on_ground=True)
            if endpoint == "control" and body.get("forward") and len(attempts) == 1:
                status.update(pos=below_bank, in_water=False, on_ground=False)
            return {"ok": True}

    env = CombatEnv(CavePoolBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda _pos, *, excluded_shores, **_: (
        alternate if tuple(bank) in excluded_shores else bank
    ))

    first = env.escape_deep_water_to_shore()
    assert first["reason"] == "shore_landing_unconfirmed" and first["in_water"] is False
    assert attempts == [bank, bank]
    if reentry == "confirmed_landing":
        env.remember_dry_bank({**status, "pos": bank, "on_ground": True})
    if reentry == "new_life":
        status["last_death"] = {"t": 2}
    status.update(pos=[-5211.5, 23, -7784.3], in_water=True)

    second = env.escape_deep_water_to_shore()
    assert second["ok"] is True
    assert attempts[-1] == (alternate if reentry == "stalled" else bank)
    assert ("shore_lip_stalled" in second["actions"]) is (reentry == "stalled")


@pytest.mark.parametrize("terrain", ["step", "deep_drop", "lava", "low_ceiling"])
def test_grounded_retreat_can_step_down_one_verified_block_in_a_tunnel(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.5, 64, 0.5], "dimension": "overworld", "ready": True, "alive": True, "on_ground": True,
    }
    moves = []

    class StairBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                if z != 0 or x < 0 or x > 2:
                    return {"name": "stone"}
                if x == 0:
                    return {"name": "stone" if y <= 63 else "air"}
                floor = 61 if terrain == "deep_drop" else 62
                if floor == y and terrain == "lava":
                    return {"name": "lava"}
                if terrain == "low_ceiling" and y == 65:
                    return {"name": "stone"}
                return {"name": "stone" if y <= floor else "air"}
            if action == "control":
                moves.append(body)
                status["pos"] = [1.3, 63, 0.5]
            return {"ok": True}

    outcome = CombatEnv(StairBot()).flee([-0.4, 64, 0.5], allow_shaft_seal=False, wait_for_ground=True)

    assert outcome["ok"] is (terrain == "step")
    if terrain == "step":
        move, = moves
        assert move["forward"] and not move["jump"] and not move["sprint"]
    else:
        assert moves == []
        assert outcome["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("terrain", ["step", "low_ceiling", "lava", "fell_short"])
def test_cornered_melee_retreat_can_climb_a_short_supported_step(terrain):
    from mc_agent.skills.combat_env import CombatEnv

    status = {
        "pos": [0.65, 61, 0.5], "dimension": "overworld", "ready": True, "alive": True, "on_ground": True,
    }
    moves = []

    class DepressionBot:
        def status(self):
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "get_block":
                x, y, z = body["pos"]
                if z != 0 or x < 0 or x > 2:
                    return {"name": "stone"}
                if terrain == "low_ceiling" and x == 0 and y == 63:
                    return {"name": "stone"}
                if terrain == "lava" and x > 0 and y == 61:
                    return {"name": "lava"}
                return {"name": "stone" if y <= (60 if x == 0 else 61) else "air"}
            if action == "control":
                moves.append(body)
                status["pos"] = [1.4, 62, 0.5] if terrain != "fell_short" else [1.05, 61, 0.5]
            elif action == "move_to":
                pytest.fail("The short climb grants only one checked movement pulse")
            return {"ok": True}

    result = CombatEnv(DepressionBot()).flee(
        [-0.6, 61, 0.5], allow_shaft_seal=False, wait_for_ground=True,
    )

    assert result["ok"] is (terrain == "step")
    if terrain in {"step", "fell_short"}:
        move, = moves
        assert move["forward"] and move["jump"] and not move["sprint"]
    else:
        assert moves == []
        assert result["reason"] == "no_safe_retreat"


@pytest.mark.parametrize("landing", ["grounded", "water", "respawned", "airborne"])
@pytest.mark.parametrize("early_exit", [False, True])
def test_directed_shore_rechecks_an_airborne_bot_over_the_bank(monkeypatch, landing, early_exit):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    bank = [0.5, 63.0, 14.5] if early_exit else [0.5, 63.0, 4.5]
    actual_landing = [0.5, 63.0, 4.5]
    status = {
        "pos": [0.5, 62.2, 0.5], "dimension": "overworld", "last_death": {"t": 1},
        "in_water": True, "on_ground": False, "alive": True, "ready": True,
    }
    moves = []
    jump = []

    class BankBot:
        def status(self):
            if clock[0] >= 1000.15:
                if landing == "grounded":
                    status.update(in_water=False, on_ground=True, pos=actual_landing)
                elif landing == "water":
                    status.update(in_water=True, on_ground=False)
                elif landing == "respawned":
                    status.update(pos=[2.5, 70, 2.5], last_death={"t": 2})
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "move_to":
                moves.append(body)
                status.update(pos=[0.5, 63.5, 4.5], in_water=False, on_ground=False)
                if early_exit:
                    raise ApiError("timeout", "movement ended before reaching the requested bank")
            if action == "posture":
                jump.append(body.get("jump"))
            return {"ok": True}

    env = CombatEnv(BankBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_args, **_kwargs: bank)
    outcome = env.escape_deep_water_to_shore()

    assert len(moves) == (1 if early_exit else 2)
    assert 1000.15 <= clock[0] <= 1000.31
    assert outcome["ok"] is (landing == "grounded")
    if landing == "water":
        assert outcome["in_water"] is True and jump[-1] is True
    elif landing == "respawned":
        assert outcome["mode"] == "respawned"
    elif landing == "airborne":
        assert outcome["reason"] == "shore_landing_unconfirmed"


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


@pytest.mark.parametrize("mode", ["cover", "bunker"])
def test_subcortical_recovery_macros_share_the_combat_transport_deadline(monkeypatch, mode):
    clock = [1000.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    status = {"pos": [0.5, 74, 0.5], "dimension": "overworld", "ready": True, "alive": True, "on_ground": True}
    calls = []
    events = []

    class CaveBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "dig":
                calls.append(kwargs["timeout_s"])
                clock[0] += kwargs["timeout_s"]
                return {"ok": True}
            return {"ok": True}

    class SurvivalRecovery:
        def on_encounter(self, env, _trigger):
            if mode == "cover":
                env.build_tactical("cover_pocket")
            else:
                env.skills.dig_safety_bunker(urgent=True)
            return {"handled": True, "mode": "covered", "actions": [], "status_after": dict(status)}

    def recovery(skills, **_kwargs):
        return skills.bot.call("dig", {"pos": [0, 75, 0]}, timeout_s=30.0)

    monkeypatch.setattr(MacroSkills, "dig_cover_pocket", recovery)
    monkeypatch.setattr(MacroSkills, "dig_safety_bunker", recovery)
    raw_bot = CaveBot()
    skills = MacroSkills(raw_bot)
    guard = GenerationGuard(raw_bot, EventSink(events.append), checkpoint=lambda: None, skills=skills)
    guard.combat_slot.register(SurvivalRecovery())

    outcome = guard._run_subcortical_combat({"source": "pre_hurt_melee"})

    assert outcome["reason"] == "time_budget"
    budget_event, = [event for event in events if event["type"] == "guardian.combat_budget_exceeded"]
    assert budget_event["endpoint"] == "dig"
    assert calls == [7.5]
    assert clock[0] <= 1009.0
    assert guard.combat_env.skills is skills and skills.bot is raw_bot


@pytest.mark.parametrize("melee", ["far", "near", "unknown_distance", "scan_failed"])
def test_critical_arrow_reaction_checks_live_melee_clearance_before_cover(monkeypatch, melee):
    from mc_agent.skills.combat_env import CombatEnv

    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: 1000.0)
    skeleton = {"id": 42, "name": "skeleton", "dist": 4.5, "pos": [5, 106, 0.5]}
    zombie = {"id": 43, "name": "zombie"}
    if melee != "unknown_distance":
        zombie["dist"] = 8.0 if melee == "far" else 4.8
    status = {"pos": [0.5, 106, 0.5], "dimension": "overworld", "health": 4.048, "food": 17,
              "ready": True, "alive": True, "on_ground": True, "blocking": True, "nearest_hostile": skeleton}
    moves = []

    class HillBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                hurt = {"seq": 1, "type": "hurt", "source": "arrow", "source_entity": skeleton}
                return {"events": [] if kwargs["params"]["since"] else [hurt]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                if melee == "scan_failed":
                    raise httpx.ReadTimeout("hostile scan unavailable")
                assert kwargs["timeout_s"] == 0.6
                return {"entities": [skeleton, zombie]}
            return {"ok": True}

    def cover(_env, shooter, *, other_shooters, allow_stationary):
        assert not other_shooters and shooter == skeleton["pos"] and not allow_stationary
        moves.append("natural_cover")
        return {"ok": True, "mode": "natural_cover"}

    def flee(_pos, **_kwargs):
        moves.append("flee")
        return {"ok": True, "mode": "flee", "moved_dist": 0.95}

    guard = GenerationGuard(HillBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    guard._last_melee_hurt_at = 999.0
    guard._last_dig_cover_at = 990.0
    guard._last_dig_cover_block = (0, 106, 0)
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    monkeypatch.setattr(guard.combat_env, "flee", flee)
    assert guard.poll()["requires_replan"]

    assert moves == (["natural_cover"] if melee == "far" else ["flee"])


@pytest.mark.parametrize("hits, cover_found", [(1, False), (2, True), (2, False)])
def test_repeated_arrow_hits_through_a_shield_prompt_cover_before_critical_health(monkeypatch, hits, cover_found):
    import time

    from mc_agent.skills.combat_env import CombatEnv

    near = {"name": "pillager", "id": 41, "dist": 5.8, "pos": [6.3, 63, 0.5]}
    far = {"name": "pillager", "id": 42, "dist": 7.3, "pos": [0.5, 63, -6.8]}
    status = {
        "pos": [0.5, 63, 0.5], "dimension": "overworld", "health": 12.0, "food": 20,
        "alive": True, "ready": True, "on_ground": True, "blocking": True, "nearest_hostile": near,
    }
    cover_checks = []

    class BankBot:
        def call(self, action, body=None, **kwargs):
            if action == "status":
                return dict(status)
            if action == "events":
                since = kwargs["params"]["since"]
                return {"events": [
                    {"seq": seq, "type": "hurt", "source": "arrow", "source_entity": near if seq == 1 else far}
                    for seq in range(1, hits + 1) if seq > since
                ]}
            if action == "inventory":
                return {"items": [], "offhand": {"name": "shield"}}
            if action == "get_entities":
                return {"entities": [near, far]}
            raise AssertionError(action)

    def cover(_env, pos, *, other_shooters, allow_stationary):
        cover_checks.append((pos, other_shooters, allow_stationary))
        if not cover_found:
            return None
        status["pos"] = [-1.7, 63, 0.5]
        return {"ok": True, "mode": "natural_cover", "pos_after": status["pos"]}

    monkeypatch.setattr(CombatEnv, "dig_cover_pocket", lambda *_args: {"ok": False, "reason": "no_solid_wall"})
    monkeypatch.setattr(CombatEnv, "seek_natural_cover", cover)
    guard = GenerationGuard(BankBot(), EventSink(lambda _: None), checkpoint=lambda: None)
    monkeypatch.setattr(guard.combat_env, "flee", lambda *_args, **_kwargs: {"ok": False, "mode": "flee", "moved_dist": 0.0})
    if hits == 2:
        guard.last_reaction = time.monotonic()
    incident = guard.poll()

    assert cover_checks == ([(far["pos"], [near["pos"]], False)] if hits == 2 else [])
    assert incident["requires_replan"] is cover_found


@pytest.mark.parametrize("landing", ["grounded", "water", "respawned", "still_airborne"])
def test_shore_recovery_confirms_landing_when_the_generic_escape_already_left_water(monkeypatch, landing):
    from mc_agent.skills.combat_env import CombatEnv

    clock = [1000.0]
    monkeypatch.setattr("mc_agent.skills.combat_env.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("mc_agent.skills.combat_env.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    bank = [0.5, 63, 0.5]
    status = {
        "pos": [0.5, 63.5, 0.5], "dimension": "overworld", "ready": True, "alive": True,
        "in_water": False, "on_ground": False, "last_death": {"t": 1},
    }
    moves = []

    class ShoreBot:
        def status(self):
            if clock[0] >= 1000.15 and not moves:
                if landing == "grounded":
                    status.update(pos=bank, on_ground=True)
                elif landing == "water":
                    status.update(pos=[0.5, 62, -1.0], in_water=True)
                elif landing == "respawned":
                    status.update(pos=[1.5, 65, 0.5], last_death={"t": 2}, on_ground=True)
            return dict(status)

        def call(self, action, body=None, **kwargs):
            if action == "status":
                return self.status()
            if action == "move_to":
                moves.append([body[key] for key in ("x", "y", "z")])
                status.update(pos=bank, in_water=False, on_ground=True)
            return {"ok": True}

    env = CombatEnv(ShoreBot())
    monkeypatch.setattr(env, "_find_walkable_shore_target", lambda *_args, **_kwargs: bank)

    outcome = env.escape_deep_water_to_shore()

    assert 1000.15 <= clock[0] <= 1000.31
    assert outcome["ok"] is (landing in {"grounded", "water"})
    assert moves == ([bank] if landing == "water" else [])
    if landing == "respawned":
        assert outcome["mode"] == "respawned"
    elif landing == "still_airborne":
        assert outcome["mode"] == "shore_landing_unconfirmed"
