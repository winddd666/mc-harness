"""Shared imports and lightweight test helpers split from legacy test_generation_guard.py."""

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


from mc_agent.harness.guardian import GenerationGuard, ReflexPreempted


from mc_agent.skills.library import MacroSkills


from mc_agent.live.state import ReplayIncrementer


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

