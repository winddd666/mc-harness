"""Shared imports and fixtures split from legacy tests/test_skills_and_providers.py."""

from __future__ import annotations


import json


import math


import time


import httpx


import pytest


from mc_agent.bot import BotSession


from mc_agent.config import should_trust_env


from mc_agent.harness.agent import run_mc_agent


from mc_agent.harness.context import RequestContext


from mc_agent.harness.model import (
    DEEPSEEK_DEFAULT_MODEL,
    QwenSpec,
    _stream_message,
    resolve_qwen_endpoint,
)


from mc_agent.skills import MacroSkills, SafetyEmergency, check_and_stabilize, compute_advancement_frontier


class _SurfaceScoutBackend:
    def __init__(self) -> None:
        self.pos = [0.5, 64, 0.5]
        self.biome = "plains"
        self.health = 20
        self.wet = False
        self.submerged = False
        self.hostile = None
        self.moves = []
        self.calls = []
        self.target = None
        self.move = self.arrive
        self.released = True

    def arrive(self, request, body):
        self.pos = [body["x"], body.get("y", 64), body["z"]]
        return self.ok({"pos": self.pos})

    @staticmethod
    def ok(result):
        return httpx.Response(200, json={"ok": True, "result": result})

    def handle(self, request):
        route = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((route, self.pos[:], body))
        if route == "status":
            return self.ok({
                "pos": self.pos,
                "dimension": "overworld",
                "biome": self.biome,
                "health": self.health,
                "in_water": self.wet,
                "submerged": self.submerged,
                "nearest_hostile": self.hostile,
            })
        if route == "get_block":
            return self.ok({"name": "grass_block"})
        if route in {"find_blocks", "get_entities"}:
            assert body["radius"] == 64
            rows = []
            if self.pos[0] > 50 and self.target in body["types"]:
                row = {"name": self.target, "pos": [62, 64, 1], "biome": "meadow"}
                if route == "get_entities":
                    row["id"] = 17
                rows.append(row)
            if self.pos[0] > 20 and "obsidian" in body["types"]:
                rows.append({"name": "obsidian", "pos": [41, 63, 0]})
            key = "blocks" if route == "find_blocks" else "entities"
            return self.ok({key: rows})
        if route == "move_to":
            assert ("y" in body) is (body["range"] == 0.5)
            assert body["stop_on"] == ["hurt", "death"]
            self.moves.append(body)
            return self.move(request, body)
        if route == "stop":
            return self.ok({"stopped": True, "action": "move_to", "released": self.released})
        raise AssertionError(route)

