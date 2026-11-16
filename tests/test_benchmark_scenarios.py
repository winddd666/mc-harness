"""Real-world captured scenario regression tests (20261115_water_pocket_stairwell)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import httpx

from mc_agent.bot import BotSession
from mc_agent.harness.hud import build_world_hud
from mc_agent.harness.mc_tools import McToolset


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "scenario_20261115_water_pocket.json"


class _ScenarioWorld:
    """Stateful MockTransport backed by a captured 3D voxel snapshot."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.default_block = str(scenario.get("default_block") or "stone")
        self.voxels: dict[tuple[int, int, int], str] = {}
        for key, name in (scenario.get("non_stone_blocks") or {}).items():
            x, y, z = (int(v) for v in key.split(","))
            self.voxels[(x, y, z)] = str(name)
        self.status = copy.deepcopy(scenario["status"])
        self.inventory = copy.deepcopy(scenario["inventory"])
        self.entities = copy.deepcopy((scenario.get("entities_r24") or {}).get("entities") or [])
        self.jump = False

    def block_at(self, x: int, y: int, z: int) -> str:
        return self.voxels.get((x, y, z), self.default_block)

    def _adjust_item(self, name: str, delta: int) -> None:
        items = self.inventory.setdefault("items", [])
        if delta > 0:
            items.append({"name": name, "count": delta})
            return
        need = -delta
        for row in items:
            if row.get("name") == name and int(row.get("count") or 0) > 0:
                take = min(need, int(row["count"]))
                row["count"] -= take
                need -= take
                if need <= 0:
                    break
        self.inventory["items"] = [r for r in items if int(r.get("count") or 0) > 0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if path == "/status":
            st = copy.deepcopy(self.status)
            st["jumping"] = self.jump
            return httpx.Response(200, json={"ok": True, "result": st})

        if path == "/inventory":
            return httpx.Response(200, json={"ok": True, "result": copy.deepcopy(self.inventory)})

        if path == "/get_block":
            x, y, z = (int(v) for v in body["pos"])
            name = self.block_at(x, y, z)
            return httpx.Response(200, json={"ok": True, "result": {"name": name, "pos": [x, y, z]}})

        if path == "/find_blocks":
            wanted = body.get("types")
            if not wanted:
                return httpx.Response(200, json={"ok": True, "result": copy.deepcopy(self.scenario["exposed_blocks_r16"])})
            radius = min(int(body.get("radius") or 16), 24)
            limit = int(body.get("limit") or 20)
            px, py, pz = (float(v) for v in self.status["pos"])
            bx, by, bz = math.floor(px), math.floor(py), math.floor(pz)
            matches: list[dict[str, Any]] = []
            for x in range(bx - radius, bx + radius + 1):
                for y in range(by - min(radius, 8), by + min(radius, 8) + 1):
                    for z in range(bz - radius, bz + radius + 1):
                        name = self.block_at(x, y, z)
                        if name in wanted:
                            dist = math.sqrt((x + 0.5 - px) ** 2 + (y - py) ** 2 + (z + 0.5 - pz) ** 2)
                            if dist <= radius:
                                matches.append({"name": name, "pos": [x, y, z], "dist": round(dist, 2)})
            matches.sort(key=lambda r: r["dist"])
            return httpx.Response(200, json={"ok": True, "result": {"blocks": matches[:limit]}})

        if path == "/get_entities":
            return httpx.Response(200, json={"ok": True, "result": {"entities": copy.deepcopy(self.entities)}})

        if path == "/equip":
            item = str(body.get("item") or "stone_pickaxe")
            self.status["held"] = {"name": item, "count": 1}
            return httpx.Response(200, json={"ok": True, "result": {"equipped": item}})

        if path == "/dig":
            x, y, z = (int(v) for v in body["pos"])
            old = self.block_at(x, y, z)
            self.voxels[(x, y, z)] = "air"
            if old != "air":
                drop = "cobblestone" if old == "stone" else old
                self._adjust_item(drop, 1)
            return httpx.Response(200, json={"ok": True, "result": {"dug": old, "pos": [x, y, z]}})

        if path == "/move_to":
            tx = float(body.get("x", self.status["pos"][0]))
            tz = float(body.get("z", self.status["pos"][2]))
            ty = float(body.get("y", self.status["pos"][1]))
            self.status["pos"] = [tx, ty, tz]
            return httpx.Response(200, json={"ok": True, "result": {"pos": [tx, ty, tz], "arrived": True}})

        if path == "/posture":
            self.jump = bool(body.get("jump"))
            return httpx.Response(200, json={"ok": True, "result": {"jump": self.jump}})

        if path == "/place":
            x, y, z = (int(v) for v in body["pos"])
            item = str(body.get("item") or "cobblestone")
            cur_y = float(self.status["pos"][1])
            if self.jump and math.floor(cur_y) == y:
                self.status["pos"][1] = float(y) + 1.15
                return httpx.Response(
                    400,
                    json={"ok": False, "error": {"code": "blocked_by_self", "message": "bot is standing in target cell"}},
                )
            self.voxels[(x, y, z)] = item
            self._adjust_item(item, -1)
            self.status["pos"][1] = float(y) + 1.0
            return httpx.Response(200, json={"ok": True, "result": {"placed": item, "pos": [x, y, z]}})

        raise AssertionError(f"Unexpected mock endpoint: {path}")


def _load_scenario() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_water_pocket_scenario_hud_probe_west_escape_and_pillar() -> None:
    scenario = _load_scenario()
    assert scenario["bot_block_pos"] == [-172, 53, -387]

    # 1. HUD 准确反映 [-172,53,-387] 与 stone_pickaxe 剩余 46 耐久
    world_hud = _ScenarioWorld(scenario)
    with BotSession("tok", bot_username="BeastBot_01", transport=httpx.MockTransport(world_hud.handler)) as bot:
        ts = McToolset(bot)
        hud = build_world_hud(ts, task="收集铁矿石并合成 iron_pickaxe")
    assert "[-172,53,-387]" in hud
    assert "stone_pickaxe(剩46)" in hud

    # 2. 向南平挖遇阶梯悬空安全中断，并返回包括 west 在内的可行逃生方向
    world_south = _ScenarioWorld(scenario)
    with BotSession("tok", bot_username="BeastBot_01", transport=httpx.MockTransport(world_south.handler)) as bot:
        ts_south = McToolset(bot)
        south_out = json.loads(ts_south.mc_skill_tunnel(direction="south", steps=8))
    assert south_out["ok"] is False
    assert south_out["stop_reason"] == "unsafe_or_unknown_floor"
    viable_routes = set(south_out.get("viable_tunnel_directions") or []) | set(south_out.get("viable_up_directions") or [])
    assert "west" in viable_routes

    # 3. 向西传入越界 steps=10：静默钳位至 8 格并顺利掘进逃离 [-172, 53, -387]
    world_west = _ScenarioWorld(scenario)
    with BotSession("tok", bot_username="BeastBot_01", transport=httpx.MockTransport(world_west.handler)) as bot:
        ts_west = McToolset(bot)
        west_out = json.loads(ts_west.mc_skill_tunnel(direction="west", steps=10))
    assert west_out.get("error", {}).get("code") != "bad_args"
    assert west_out["ok"] is True
    assert west_out["steps_requested"] == 8
    assert west_out["steps_done"] >= 1
    assert math.floor(float(west_out["pos_after"][0])) < -172

    # 4. 若将当前水平四周全部封水，pillar 垂直垫高可正常挖顶并抬升至 y >= 55
    world_pillar = _ScenarioWorld(scenario)
    for hx, hy, hz in (
        (-173, 54, -387),
        (-171, 54, -387),
        (-172, 54, -388),
        (-172, 54, -386),
    ):
        world_pillar.voxels[(hx, hy, hz)] = "water"
    with BotSession("tok", bot_username="BeastBot_01", transport=httpx.MockTransport(world_pillar.handler)) as bot:
        ts_pil = McToolset(bot)
        pil_out = json.loads(ts_pil.mc_skill_staircase(direction="north", steps=2, vertical="pillar"))
    assert pil_out["ok"] is True
    assert pil_out["steps_done"] == 2
    assert math.floor(float(pil_out["pos_after"][1])) >= 55
    assert world_pillar.jump is False
