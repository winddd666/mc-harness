"""Unit tests for Stage 2 full-lifecycle spinal/subcortical reflexes & 429 guarded wait."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import pytest

from mc_agent.bot import BotSession
from mc_agent.harness.agent import run_mc_agent
from mc_agent.harness.control import RequestCancelled
from mc_agent.harness.events import EventSink
from mc_agent.harness.guardian import GenerationGuard
from mc_agent.harness.model import RpmLimiter
from mc_agent.skills.library import MacroSkills


def test_rpm_limiter_polls_control_during_wait_and_aborts_without_burning_quota() -> None:
    limiter = RpmLimiter(rpm_budget=1, window_s=2.0)
    assert limiter.acquire() >= 0.0

    polls = {"n": 0}

    def _ctrl() -> bool:
        polls["n"] += 1
        return polls["n"] >= 2

    with pytest.raises(RequestCancelled):
        limiter.acquire(control=_ctrl)

    assert polls["n"] == 2
    assert len(limiter._times) == 1  # Cancelled request did NOT consume an extra RPM slot!


def test_pre_hurt_creeper_triggers_before_any_damage_even_within_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [200.0]
    monkeypatch.setattr("mc_agent.harness.guardian.time.monotonic", lambda: clock[0])
    calls: list[tuple[str, Any]] = []

    class _CreeperBot:
        _last_event_seq = 0

        def __init__(self, *, stuck: bool = False) -> None:
            self.stuck = stuck
            self.pos = [10.5, 64.0, 10.5]

        def call(self, action: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            payload = body or {}
            calls.append((action, payload))
            if action == "status":
                d = round(abs(13.5 - self.pos[2]), 2)
                return {
                    "pos": list(self.pos),
                    "health": 20.0,
                    "food": 20,
                    "alive": True,
                    "ready": True,
                    "on_ground": True,
                    "in_water": False,
                    "yaw": 0.0,
                    "nearest_hostile": {"id": 55, "name": "creeper", "pos": [10.5, 64.0, 13.5], "dist": d},
                }
            if action == "events":
                return {"events": [], "last_seq": 0}  # ZERO hurt events!
            if action == "inventory":
                return {"items": [{"name": "shield", "count": 1}], "offhand": {}}
            if action == "get_entities":
                d = round(abs(13.5 - self.pos[2]), 2)
                return {"entities": [{"id": 55, "name": "creeper", "pos": [10.5, 64.0, 13.5], "dist": d}]}
            if action == "get_block":
                p = payload["pos"]
                return {"name": "stone" if p[1] < 64 else "air"}
            if action == "move_to" and not self.stuck and "z" in payload:
                self.pos[2] = float(payload["z"])
            return {"ok": True, "pos": list(self.pos)}

    # Case A: Open retreat succeeds -> raises shield during retreat and resets posture to {} on exit!
    guard = GenerationGuard(_CreeperBot(stuck=False), EventSink(lambda _: None), checkpoint=lambda: None)
    guard.last_reaction = 199.5  # Creeper MUST bypass the 3.0s cooldown!
    incident = guard.poll()

    assert incident is not None
    assert "pre_hurt_creeper" in incident["reasons"]
    assert any(a["action"] == "subcortical_combat" and a["ok"] for a in incident["actions"])
    postures = [b for act, b in calls if act == "posture"]
    assert any(p.get("shield") is True for p in postures)
    assert postures[-1] == {}

    # Case B: Cornered/stuck right in front of creeper -> keeps shield latched (`latched_guard=True`)!
    calls.clear()
    guard_stuck = GenerationGuard(_CreeperBot(stuck=True), EventSink(lambda _: None), checkpoint=lambda: None)
    inc_stuck = guard_stuck.poll()
    assert inc_stuck is not None
    postures_stuck = [b for act, b in calls if act == "posture"]
    assert postures_stuck[-1].get("shield") is True


def test_active_macro_tool_preempted_mid_chop_counter_kills_zombie_without_replaying_tool() -> None:
    """When hit mid-way through `mc_skill_harvest_logs`, stop chopping immediately, kill the zombie, and DO NOT replay chop."""
    dig_count = {"n": 0}
    zombie_state = {"approached": False, "alive": True}
    posture_after: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        if action == "status":
            if not zombie_state["alive"]:
                nh = None
            elif zombie_state["approached"]:
                nh = {"id": 91, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.6}
            else:
                nh = {"id": 91, "name": "zombie", "pos": [12.5, 64.0, 0.5], "dist": 12.0}
            res: dict[str, Any] = {
                "pos": [0.5, 64.0, 0.5],
                "health": 15.0 if zombie_state["approached"] else 20.0,
                "food": 14,
                "alive": True,
                "ready": True,
                "on_ground": True,
                "in_water": False,
                "is_day": False,
                "nearest_hostile": nh,
            }
        elif action == "events":
            res = {"events": [], "last_seq": 0}
        elif action == "inventory":
            res = {
                "items": [
                    {"name": "iron_sword", "count": 1},
                    {"name": "bread", "count": 3},
                ],
                "offhand": {"name": "shield", "count": 1},
                "held": {"name": "stone_axe", "count": 1},
            }
        elif action == "find_blocks":
            # 3 logs in the tree; only log #1 should ever be attempted before zombie preemption!
            res = {
                "blocks": [
                    {"name": "oak_log", "pos": [2, 64, 0], "dist": 1.5},
                    {"name": "oak_log", "pos": [2, 65, 0], "dist": 2.0},
                    {"name": "oak_log", "pos": [2, 66, 0], "dist": 2.8},
                ]
            }
        elif action == "move_to":
            res = {"pos": [1.2, 64.0, 0.5]}
        elif action == "dig":
            dig_count["n"] += 1
            # Zombie sneaks up and hits us during log #1 dig!
            zombie_state["approached"] = True
            res = {"dug": "oak_log", "interrupted_by": "hurt"}
        elif action == "get_entities":
            ents = [{"id": 91, "name": "zombie", "pos": [1.5, 64.0, 0.5], "dist": 1.6}] if zombie_state["alive"] else []
            res = {"entities": ents}
        elif action == "attack":
            zombie_state["alive"] = False
            res = {"hits": 1, "killed": True}
        elif action == "posture":
            posture_after.clear()
            posture_after.update(body)
            res = dict(body)
        else:
            res = {"ok": True}
        return httpx.Response(200, json={"ok": True, "result": res})

    turns = {"n": 0}
    seen_tool_msgs: list[str] = []

    def fake_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        turns["n"] += 1
        if turns["n"] == 1:
            return {
                "role": "assistant",
                "content": "砍树。",
                "tool_calls": [
                    {
                        "id": "call_chop",
                        "type": "function",
                        "function": {"name": "mc_skill_harvest_logs", "arguments": json.dumps({"target_logs": 3})},
                    }
                ],
            }
        for m in messages:
            if m.get("role") == "tool":
                seen_tool_msgs.append(str(m.get("content") or ""))
        return {"role": "assistant", "content": "已反杀僵尸，夜间转入地下。"}

    with BotSession("tok", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent(
            "收集木头",
            bot,
            chat_fn=fake_chat,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
        )

    assert report["status"] == "answered"
    assert dig_count["n"] == 1  # Unwound immediately on log #1; NEVER continued to log #2/#3 and NEVER auto-replayed!
    assert zombie_state["alive"] is False  # Killed 0-LLM!
    assert report["guardian"]["tool_preemptions"] == 1
    assert any("preempted_by_reflex" in msg and "counter_killed" in msg for msg in seen_tool_msgs)
    assert posture_after == {}


def test_post_tool_boundary_escapes_water_before_entering_next_model_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces s12 req #23: `unstick` ends with `in_water: true` — must escape water BEFORE `rate_wait`/next LLM call!"""
    in_water = {"v": False}
    escaped_before_turn2 = {"v": False}

    monkeypatch.setattr(
        MacroSkills,
        "escape_water",
        lambda self: in_water.__setitem__("v", False) or {"ok": True, "in_water": False},
    )
    monkeypatch.setattr(
        MacroSkills,
        "unstick",
        lambda self: in_water.__setitem__("v", True) or {"ok": True, "control": {"in_water": True}},
    )

    def handle(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": [0.5, 19.0, 0.5],
                        "health": 18.0,
                        "food": 20,
                        "alive": True,
                        "ready": True,
                        "in_water": in_water["v"],
                        "on_ground": not in_water["v"],
                    },
                },
            )
        if action == "events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        if action == "inventory":
            return httpx.Response(200, json={"ok": True, "result": {"items": []}})
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    turns = {"n": 0}

    def fake_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        turns["n"] += 1
        if turns["n"] == 1:
            return {
                "role": "assistant",
                "content": "脱困。",
                "tool_calls": [
                    {
                        "id": "call_u",
                        "type": "function",
                        "function": {"name": "mc_skill_unstick", "arguments": "{}"},
                    }
                ],
            }
        # By the time turn #2 starts (even before rate_wait!), the bot MUST already be dry!
        escaped_before_turn2["v"] = (in_water["v"] is False)
        assert any("[脊髓边界自稳] 自动浮水登岸" in str(m.get("content") or "") for m in messages)
        return {"role": "assistant", "content": "已安全上岸。"}

    with BotSession("tok", transport=httpx.MockTransport(handle)) as bot:
        report = run_mc_agent(
            "地下脱困",
            bot,
            chat_fn=fake_chat,
            max_calls=4,
            survival_guard=True,
            include_initial_snapshot=False,
        )

    assert report["status"] == "answered"
    assert escaped_before_turn2["v"] is True
    assert report["guardian"]["boundary_stabilizations"] == 1
