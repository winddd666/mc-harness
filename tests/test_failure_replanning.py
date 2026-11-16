"""Regression cases taken from the iron-pickaxe trajectory, independent of live services."""
import json

import pytest

from mc_agent.errors import ApiError
from mc_agent.harness.events import EventSink
from mc_agent.harness.scheduler import ToolScheduler
from mc_agent.skills.library import MacroSkills


class MiningBot:
    def __init__(self, failure="dig_unconfirmed", fail_after=0):
        self.failure = failure
        self.fail_after = fail_after
        self.dug = 0
        self.attempts = 0

    def inventory(self):
        return {"items": [{"name": "wooden_pickaxe", "count": 1}, {"name": "cobblestone", "count": self.dug}]}

    def status(self):
        return {"pos": [0, 64, 0], "health": 8, "in_water": False}

    def call(self, action, body):
        if action == "find_blocks":
            return {"blocks": [{"name": "stone", "pos": [1, 64, 0], "dist": 1}]}
        if action == "get_block":
            return {"name": "air"}
        if action == "equip":
            return {}
        if action == "dig":
            self.attempts += 1
            if self.failure and self.dug >= self.fail_after:
                raise ApiError(self.failure, "test failure", context={"health": 8, "cause": "arrow"})
            self.dug += 1
            return {"dug": "stone", "gained": {"cobblestone": 1}}
        raise AssertionError(action)


@pytest.mark.parametrize("failure", ["dig_unconfirmed", "no_path", "timeout", "out_of_reach"])
def test_transient_failure_is_retryable_after_expiry(monkeypatch, failure):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    bot = MiningBot(failure)
    skills = MacroSkills(bot)
    first = skills.mine_blocks(["stone"], count=1)
    assert first["ok"] is False
    assert first["error"] == "no_targets_available"
    assert first["temporary_reasons"] == [failure]
    assert not skills.protected_positions
    attempts = bot.attempts
    bot.failure = None
    assert skills.mine_blocks(["stone"], count=1)["ok"] is False
    assert bot.attempts == attempts
    clock[0] += 16
    retried = skills.mine_blocks(["stone"], count=1)
    assert retried["ok"] is True
    assert retried["gained"] == {"cobblestone": 1}
    assert not skills.temporary_avoidance


def test_only_explicit_protection_is_permanent(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("mc_agent.skills.library.time.monotonic", lambda: clock[0])
    bot = MiningBot("protected_region")
    skills = MacroSkills(bot)
    skills.mine_blocks(["stone"], count=1)
    assert (1, 64, 0) in skills.protected_positions
    clock[0] += 3600
    bot.failure = None
    assert skills.mine_blocks(["stone"], count=1)["ok"] is False
    assert bot.attempts == 1


def test_hurt_after_partial_progress_requires_replan_and_keeps_context():
    bot = MiningBot("interrupted", fail_after=1)
    out = MacroSkills(bot).mine_blocks(["stone"], count=3)
    assert out["ok"] is False
    assert out["requires_replan"] is True
    assert out["dug_count"] == 1
    assert out["gained"] == {"cobblestone": 1}
    assert out["partial_error"]["cause"] == "arrow"
    assert out["protected_blacklisted"] == 0


def test_mixed_mining_request_explains_allowed_stone_separately():
    bot = MiningBot()
    out = MacroSkills(bot).mine_blocks(["stone", "iron_ore"])
    assert out["ok"] is False
    assert out["allowed_types"] == ["stone"]
    assert out["blocked_types"] == {"iron_ore": 2}
    assert bot.attempts == 0


@pytest.mark.parametrize("result", [
    {"ok": True, "dug_count": 1, "partial_error": {"code": "interrupted"}},
    {"ok": True, "steps_done": 1, "last_error": {"code": "interrupted"}},
    {"ok": True, "result": {"dug": "stone", "interrupted_by": "hurt"}},
    {"ok": True, "requires_replan": True},
])
def test_partial_or_hurt_success_stops_following_actions_but_allows_observation(result):
    from mc_agent.harness.tool_registry import Tool, ToolRegistry
    invoked = []
    registry = ToolRegistry([
        Tool(name="partial", description="partial", parameters={}, handler=lambda: json.dumps(result), concurrency="exclusive"),
        Tool(name="craft", description="craft", parameters={}, handler=lambda: invoked.append("craft") or "{}", concurrency="exclusive"),
        Tool(name="observe", description="observe", parameters={}, handler=lambda: invoked.append("observe") or "{}", concurrency="shared"),
    ])
    calls = [{"id": str(i), "function": {"name": name, "arguments": "{}"}} for i, name in enumerate(["partial", "craft", "observe"])]
    results = ToolScheduler(registry, EventSink(lambda _: None)).run(calls)
    assert invoked == ["observe"]
    assert "previous_action_failed" in results[1]
