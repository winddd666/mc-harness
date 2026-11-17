"""Offline unit tests for path-compressed MiningDFS: tree nodes, breach seal, catch-block, dual-pickaxe, backtrack, radar, canopy escape, offset retry, WorldLedger, and resupply."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx

from mc_agent.bot import BotSession
from mc_agent.harness.geofence import VoidGeofence
from mc_agent.harness.mc_tools import McToolset
from mc_agent.skills.library import PICKAXE_TIERS, MacroSkills
from mc_agent.skills.mining_dfs import MiningDFSEngine


class _MockMineWorld:
    """3D voxel world simulator enforcing anti-xray find_blocks, solid ground movement, drops, and tool equip logs."""

    def __init__(
        self,
        *,
        start_pos: tuple[float, float, float] = (0.5, 80.0, 0.5),
        inventory_items: list[dict[str, Any]] | None = None,
        surface_y: int = 80,
        simulate_best_harvest_tool: bool = False,
        track_durability: bool = False,
    ) -> None:
        self.pos = list(start_pos)
        self.surface_y = surface_y
        self.simulate_best_harvest_tool = simulate_best_harvest_tool
        self.track_durability = track_durability
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.items: list[dict[str, Any]] = [
            dict(r)
            for r in (
                inventory_items
                or [
                    {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [120, 131]},
                    {"name": "cobblestone", "count": 32, "slot": 10},
                ]
            )
        ]
        self.held_slot = 0
        self.held: dict[str, Any] | None = dict(self.items[0]) if self.items else None
        self.offhand: dict[str, Any] | None = None
        self.entities: list[dict[str, Any]] = []
        self.dig_log: list[tuple[tuple[int, int, int], str, str | None]] = []
        self.place_log: list[tuple[tuple[int, int, int], str]] = []
        self.equip_log: list[str] = []
        self.craft_log: list[tuple[str, int]] = []

    def block_at(self, pt: tuple[int, int, int]) -> str:
        if pt in self.blocks:
            return self.blocks[pt]
        if pt[1] >= self.surface_y:
            return "air"
        return "deepslate" if pt[1] < 0 else "stone"

    def _exposed(self, pt: tuple[int, int, int]) -> bool:
        if self.block_at(pt) in {"air", "cave_air", "water", "lava", "tall_grass", "short_grass"}:
            return False
        for ax, ay, az in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            nb = self.block_at((pt[0] + ax, pt[1] + ay, pt[2] + az))
            if nb in {"air", "cave_air", "torch", "water", "tall_grass", "short_grass"}:
                return True
        return False

    def _settle_gravity_at_current_xz(self) -> None:
        gx, gy, gz = math.floor(self.pos[0]), math.floor(self.pos[1]), math.floor(self.pos[2])
        passable = {"air", "cave_air", "tall_grass", "short_grass", "fern", "torch", "wall_torch", "snow"}
        for _ in range(16):
            if self.block_at((gx, gy - 1, gz)) in passable:
                gy -= 1
            else:
                break
        self.pos[1] = float(gy)

    def _add_row(self, row: dict[str, Any]) -> None:
        if "durability" in row or int(row.get("count", 1)) == 1 and row.get("name", "") in PICKAXE_TIERS:
            used_slots = {int(r["slot"]) for r in self.items if "slot" in r}
            next_slot = next((s for s in range(9, 45) if s not in used_slots), 44)
            new_row = dict(row)
            new_row["slot"] = next_slot
            self.items.append(new_row)
            return
        self._add_item(str(row["name"]), int(row.get("count", 1)))

    def _add_item(self, name: str, count: int = 1) -> None:
        for row in self.items:
            if row["name"] == name and "durability" not in row and row["count"] < 64:
                add = min(count, 64 - row["count"])
                row["count"] += add
                count -= add
                if count <= 0:
                    return
        while count > 0:
            used_slots = {int(r["slot"]) for r in self.items if "slot" in r}
            next_slot = next((s for s in range(9, 45) if s not in used_slots), 44)
            add = min(count, 64)
            self.items.append({"name": name, "count": add, "slot": next_slot})
            count -= add

    def _drop_name_for(self, block_name: str) -> str:
        mapping = {
            "iron_ore": "raw_iron",
            "deepslate_iron_ore": "raw_iron",
            "diamond_ore": "diamond",
            "deepslate_diamond_ore": "diamond",
            "coal_ore": "coal",
            "deepslate_coal_ore": "coal",
            "stone": "cobblestone",
            "deepslate": "cobbled_deepslate",
        }
        return mapping.get(block_name, block_name)

    def _count(self, name: str) -> int:
        return sum(int(r["count"]) for r in self.items if r.get("name") == name)

    def _consume_item(self, name: str, count: int = 1) -> bool:
        if self._count(name) < count:
            return False
        rem = count
        kept: list[dict[str, Any]] = []
        for r in self.items:
            if r["name"] == name and rem > 0:
                take = min(rem, int(r["count"]))
                r["count"] = int(r["count"]) - take
                rem -= take
                if r["count"] > 0:
                    kept.append(r)
            else:
                kept.append(r)
        self.items = kept
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if path == "/status":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "pos": list(self.pos),
                        "health": 20.0,
                        "food": 20,
                        "on_ground": True,
                        "in_water": False,
                        "in_lava": False,
                        "alive": True,
                        "ready": True,
                        "held": dict(self.held) if self.held else None,
                        "offhand": dict(self.offhand) if self.offhand else None,
                    },
                },
            )
        if path == "/inventory":
            used = len(self.items)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "items": [dict(r) for r in self.items],
                        "armor": {},
                        "offhand": dict(self.offhand) if self.offhand else None,
                        "held": dict(self.held) if self.held else None,
                        "empty_slots": max(0, 36 - used),
                    },
                },
            )
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            return httpx.Response(200, json={"ok": True, "result": {"name": self.block_at(pt), "pos": list(pt)}})
        if path == "/find_blocks":
            rad = int(body.get("radius", 16))
            types = set(body.get("types") or [])
            cx, cy, cz = (math.floor(self.pos[i]) for i in range(3))
            found: list[dict[str, Any]] = []
            # Fast sparse path when searching only non-default blocks (ores, crafting_table, etc.)
            if types and types.isdisjoint({"air", "stone", "deepslate"}):
                for pt, bn in self.blocks.items():
                    if bn not in types:
                        continue
                    d = math.dist(self.pos, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5))
                    if d <= rad + 0.5 and self._exposed(pt):
                        found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
            elif types and types.issuperset({"stone"}) and rad > 12:
                # Surface/ground scan optimization: inspect surface_y - 1 plane + explicit overrides
                seen_pts: set[tuple[int, int, int]] = set()
                for pt, bn in self.blocks.items():
                    seen_pts.add(pt)
                    if bn in types:
                        d = math.dist(self.pos, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5))
                        if d <= rad + 0.5 and self._exposed(pt):
                            found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
                surf_blk_y = self.surface_y - 1
                if abs(surf_blk_y - cy) <= rad:
                    for dx in range(-rad, rad + 1):
                        for dz in range(-rad, rad + 1):
                            pt = (cx + dx, surf_blk_y, cz + dz)
                            if pt in seen_pts:
                                continue
                            bn = self.block_at(pt)
                            if bn in types and self._exposed(pt):
                                d = math.dist(self.pos, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5))
                                if d <= rad + 0.5:
                                    found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
            else:
                eff_rad = min(rad, 16)
                for dx in range(-eff_rad, eff_rad + 1):
                    for dy in range(-eff_rad, eff_rad + 1):
                        for dz in range(-eff_rad, eff_rad + 1):
                            pt = (cx + dx, cy + dy, cz + dz)
                            bn = self.block_at(pt)
                            if types and bn not in types:
                                continue
                            if not types and bn in {"air", "cave_air"}:
                                continue
                            if self._exposed(pt):
                                d = math.dist(self.pos, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5))
                                if d <= rad + 0.5:
                                    found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
            found.sort(key=lambda r: r["dist"])
            return httpx.Response(200, json={"ok": True, "result": {"blocks": found[: int(body.get("limit", 20))]}})
        if path == "/get_entities":
            ents = []
            for e in self.entities:
                d = math.dist(self.pos, e["pos"])
                ents.append({**e, "dist": round(d, 2)})
            return httpx.Response(200, json={"ok": True, "result": {"entities": ents}})
        if path == "/equip":
            it = body["item"]
            slot_dest = body.get("slot", "hand")
            found = next((r for r in self.items if r["name"] == it), None)
            if not found:
                return httpx.Response(200, json={"ok": False, "error": {"code": "missing_item", "message": f"no {it}"}})
            if slot_dest == "off-hand":
                prev_off = dict(self.offhand) if self.offhand else None
                if "durability" not in found and int(found.get("count", 1)) > 1:
                    self._consume_item(it, 1)
                    self.offhand = {"name": it, "count": 1, "slot": 45}
                else:
                    self.items = [r for r in self.items if r is not found]
                    self.offhand = {**found, "slot": 45}
                    if self.held and self.held.get("slot") == found.get("slot"):
                        self.held = None
                if prev_off:
                    self._add_row(prev_off)
                self.equip_log.append(f"{it}:off-hand")
                return httpx.Response(200, json={"ok": True, "result": {"held": dict(self.held) if self.held else None}})
            self.held = dict(found)
            self.equip_log.append(it)
            return httpx.Response(200, json={"ok": True, "result": {"held": dict(self.held)}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            bn = self.block_at(pt)
            if bn in {"air", "cave_air"}:
                return httpx.Response(200, json={"ok": False, "error": {"code": "nothing_to_dig", "message": "air"}})
            used_row = None
            is_leaf = bn.endswith("_leaves")
            if not is_leaf:
                if self.simulate_best_harvest_tool:
                    pick_rows = [r for r in self.items if r.get("name") in PICKAXE_TIERS]
                    if pick_rows:
                        used_row = max(pick_rows, key=lambda r: PICKAXE_TIERS.get(r["name"], 0))
                        self.held = dict(used_row)
                elif self.held:
                    used_row = next(
                        (r for r in self.items if r.get("slot") == self.held.get("slot") and r.get("name") == self.held.get("name")),
                        None,
                    )
            held_name = None if is_leaf else (self.held or {}).get("name")
            self.dig_log.append((pt, bn, held_name))
            self.blocks[pt] = "air"
            if not is_leaf and self.track_durability and used_row and "durability" in used_row:
                dur = used_row["durability"]
                dur[0] = max(0, int(dur[0]) - 1)
                if dur[0] <= 0:
                    self.items = [r for r in self.items if r is not used_row]
                    if self.held and self.held.get("slot") == used_row.get("slot"):
                        self.held = None
                elif self.held and self.held.get("slot") == used_row.get("slot"):
                    self.held["durability"] = list(dur)
            if body.get("collect", True) and not is_leaf:
                below = self.block_at((pt[0], pt[1] - 1, pt[2]))
                if below in {"air", "cave_air"} and bn.endswith("_ore"):
                    self.entities.append(
                        {
                            "id": 900 + len(self.entities),
                            "name": "item",
                            "item": f"{self._drop_name_for(bn)}x1",
                            "pos": [pt[0] + 0.5, pt[1] - 1.5, pt[2] + 0.5],
                        }
                    )
                else:
                    self._add_item(self._drop_name_for(bn), 1)
            self._settle_gravity_at_current_xz()
            return httpx.Response(200, json={"ok": True, "result": {"dug": bn}})
        if path == "/place":
            if "pos" not in body:
                bx, by, bz = math.floor(self.pos[0]), math.floor(self.pos[1]), math.floor(self.pos[2])
                pt = (bx + 1, by, bz)
            else:
                pt = tuple(int(v) for v in body["pos"])
            item = body["item"]
            replaceable = {
                "air",
                "cave_air",
                "void_air",
                "water",
                "flowing_water",
                "lava",
                "flowing_lava",
                "tall_grass",
                "short_grass",
                "fern",
                "snow",
            }
            if self.block_at(pt) not in replaceable:
                return httpx.Response(200, json={"ok": False, "error": {"code": "occupied", "message": "occupied"}})
            if not self._consume_item(item, 1):
                return httpx.Response(200, json={"ok": False, "error": {"code": "missing_item", "message": f"no {item}"}})
            self.blocks[pt] = item
            self.place_log.append((pt, item))
            return httpx.Response(200, json={"ok": True, "result": {"placed": item, "pos": list(pt)}})
        if path == "/move_to":
            passable = {"air", "cave_air", "tall_grass", "short_grass", "fern", "torch", "wall_torch", "snow"}
            if "pos" in body:
                tx, ty, tz = (float(v) for v in body["pos"][:3])
                candidate_ys = [math.floor(ty)]
            elif "y" in body:
                tx, ty, tz = float(body["x"]), float(body["y"]), float(body["z"])
                candidate_ys = [math.floor(ty)]
            else:
                tx, tz = float(body["x"]), float(body["z"])
                base_y = math.floor(self.pos[1])
                candidate_ys = [base_y + dy for dy in (0, -1, 1, -2, 2, -3, -4, -5, -6, 3, 4)]
                if self.surface_y not in candidate_ys:
                    candidate_ys.append(self.surface_y)
            gx, gz = math.floor(tx), math.floor(tz)
            for gy in candidate_ys:
                if (
                    self.block_at((gx, gy, gz)) in passable
                    and self.block_at((gx, gy + 1, gz)) in passable
                    and self.block_at((gx, gy - 1, gz)) not in passable | {"lava", "water", "flowing_lava", "flowing_water"}
                ):
                    self.pos = [gx + 0.5, float(gy), gz + 0.5]
                    kept = []
                    for e in self.entities:
                        if e.get("name") == "item" and math.dist(self.pos, e["pos"]) <= 2.2:
                            it_name = e["item"].split("x")[0]
                            self._add_item(it_name, 1)
                        else:
                            kept.append(e)
                    self.entities = kept
                    return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos)}})
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
        if path == "/craft":
            item = str(body["item"])
            cnt = int(body.get("count", 1))
            tables = [pt for pt, bn in self.blocks.items() if bn == "crafting_table"]
            if item in {"stone_pickaxe", "iron_pickaxe", "wooden_pickaxe", "furnace"}:
                if not tables:
                    return httpx.Response(
                        200,
                        json={"ok": False, "error": {"code": "need_crafting_table", "message": "need table"}},
                    )
                nearest_t = min(tables, key=lambda t: math.dist(self.pos, (t[0] + 0.5, t[1] + 0.5, t[2] + 0.5)))
                dist_t = math.dist(self.pos, (nearest_t[0] + 0.5, nearest_t[1] + 0.5, nearest_t[2] + 0.5))
                if dist_t > 4.6:
                    return httpx.Response(
                        200,
                        json={
                            "ok": False,
                            "error": {
                                "code": "no_window",
                                "message": f"crafting table at {nearest_t} did not open",
                                "table": list(nearest_t),
                                "pos": list(self.pos),
                                "eye_dist": round(dist_t, 1),
                            },
                        },
                    )
                if item == "stone_pickaxe":
                    cob_ok = self._consume_item("cobblestone", 3 * cnt) or self._consume_item("cobbled_deepslate", 3 * cnt)
                    stk_ok = self._consume_item("stick", 2 * cnt)
                    if not (cob_ok and stk_ok):
                        return httpx.Response(200, json={"ok": False, "error": {"code": "missing_ingredients", "message": "missing"}})
                    for _ in range(cnt):
                        self._add_row({"name": "stone_pickaxe", "count": 1, "durability": [131, 131]})
                    self.craft_log.append((item, cnt))
                    return httpx.Response(200, json={"ok": True, "result": {"crafted": item, "gained": cnt, "used_table": True}})
            return httpx.Response(200, json={"ok": False, "error": {"code": "no_recipe", "message": item}})
        if path == "/look":
            return httpx.Response(200, json={"ok": True, "result": {"yaw": 0.0, "pitch": 0.0}})
        if path == "/control":
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos)}})
        if path == "/drop":
            it = body["item"]
            cnt = int(body.get("count", 1))
            self._consume_item(it, cnt)
            return httpx.Response(200, json={"ok": True, "result": {"dropped": it}})
        raise AssertionError(f"Unhandled API path: {path}")


def test_mining_dfs_collar_switchback_seal_and_spine() -> None:
    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    world.blocks[(0, 74, -4)] = "lava"
    world.blocks[(0, 75, -4)] = "air"
    world.blocks[(0, 76, -4)] = "air"
    for ox in (-3, -4, -5):
        world.blocks[(ox, 74, -3)] = "iron_ore"
    for ox in (3, 4, 5):
        world.blocks[(ox, 74, -3)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=3, target_y=74, max_duration_s=25.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 3
    assert res["breaches_sealed"] >= 1
    assert res["tree"]["sealed_count"] >= 1
    node_kinds = {n["kind"] for n in res["tree"]["nodes"]}
    assert "collar" in node_kinds
    assert "switchback" in node_kinds or "spine" in node_kinds


def test_mining_dfs_catch_block_prevents_hanging_ore_drop_loss() -> None:
    world = _MockMineWorld(start_pos=(0.5, 74.0, 0.5), surface_y=80)
    world.blocks[(1, 74, -1)] = "air"
    world.blocks[(1, 75, -1)] = "air"
    world.blocks[(2, 75, -1)] = "iron_ore"
    world.blocks[(2, 74, -1)] = "air"
    world.blocks[(2, 73, -1)] = "stone"
    world.blocks[(0, 74, -1)] = "air"
    world.blocks[(0, 75, -1)] = "air"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=74, max_duration_s=15.0)

    assert res["ok"] is True
    assert res["target_gained"] >= 1
    assert any(pt == (2, 74, -1) and item == "cobblestone" for pt, item in world.place_log)
    assert world._count("raw_iron") >= 1


def test_mining_dfs_dual_pickaxe_discipline_and_deferred_diamond_reharvest() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 74.0, 0.5),
        surface_y=80,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [100, 131]},
            {"name": "cobblestone", "count": 32, "slot": 10},
        ],
    )
    world.blocks[(1, 74, 0)] = "air"
    world.blocks[(1, 75, 0)] = "air"
    world.blocks[(2, 75, 0)] = "diamond_ore"
    world.blocks[(2, 74, 0)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res1 = skills.mine_dfs(target="iron", goal_count=1, target_y=74, max_duration_s=15.0)
        assert res1["ok"] is True
        assert world.block_at((2, 75, 0)) == "diamond_ore"
        assert any(d["block"] == "diamond_ore" for d in res1["tree"]["deferred_ores"])

        world.items.append({"name": "iron_pickaxe", "count": 1, "slot": 11, "durability": [250, 250]})
        res2 = skills.mine_dfs(target="diamond", goal_count=1, target_y=74, max_duration_s=15.0)

    assert res2["ok"] is True
    assert res2["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    diamond_digs = [h for pt, bn, h in world.dig_log if bn == "diamond_ore"]
    stone_digs = [h for pt, bn, h in world.dig_log if bn == "stone"]
    assert diamond_digs == ["iron_pickaxe"]
    assert all(h == "stone_pickaxe" for h in stone_digs)


def test_mining_dfs_insufficient_tier_blocks_before_wasting_shaft() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 74.0, 0.5),
        surface_y=80,
        inventory_items=[{"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [100, 131]}],
    )
    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=1)
    assert res["ok"] is False
    assert res["stop_reason"] == "insufficient_pickaxe_tier"
    assert len(world.dig_log) == 0


def test_mining_dfs_zero_dig_return_collar_and_backtrack_after_branch_exhausted() -> None:
    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    world.blocks[(0, 76, -5)] = "iron_ore"
    world.blocks[(0, 77, -5)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res1 = skills.mine_dfs(target="iron", goal_count=2, target_y=76, max_duration_s=20.0)
        assert res1["ok"] is True
        digs_before_return = len(world.dig_log)

        res_ret = skills.mine_dfs(action="return_collar")
        digs_after_return = len(world.dig_log)

    assert res_ret["ok"] is True
    assert res_ret["stop_reason"] == "returned_to_collar"
    assert res_ret["dug_during_return"] == 0
    assert digs_after_return == digs_before_return
    assert tuple(int(v // 1) for v in world.pos) == (0, 80, 0)


def test_mining_dfs_respects_void_geofence() -> None:
    world = _MockMineWorld(start_pos=(0.5, 80.0, -7.5), surface_y=80)
    gf = VoidGeofence(center_x=0.0, center_z=0.0, radius=15.0, margin=5.0)
    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=3, target_y=74, geofence=gf, max_duration_s=10.0)
    for pt, _, _ in world.dig_log:
        assert math.hypot(pt[0] + 0.5, pt[2] + 0.5) <= gf.safe_radius + 1.5
    assert "tree" in res


def test_mining_dfs_deep_descent_two_stone_two_iron_zero_cobble_finds_diamond() -> None:
    world = _MockMineWorld(
        start_pos=(0.5, 70.0, 0.5),
        surface_y=70,
        simulate_best_harvest_tool=True,
        track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "stone_pickaxe", "count": 1, "slot": 10, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 11, "durability": [250, 250]},
            {"name": "iron_pickaxe", "count": 1, "slot": 12, "durability": [250, 250]},
        ],
    )
    world.blocks[(0, 70, -1)] = "tall_grass"
    world.blocks[(0, 71, -1)] = "tall_grass"
    world.blocks[(1, 30, -40)] = "gravel"
    world.blocks[(-1, 30, -40)] = "gravel"
    world.blocks[(0, 29, -40)] = "gravel"
    world.blocks[(0, -54, -125)] = "air"
    world.blocks[(0, -54, -126)] = "lava"
    world.blocks[(-9, -54, -125)] = "deepslate_diamond_ore"
    world.blocks[(-9, -54, -126)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=2, target_y=-54, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2
    assert world._count("diamond") >= 2
    assert world.offhand is not None or any(entry.endswith(":off-hand") for entry in world.equip_log)
    assert res["breaches_sealed"] >= 1
    diamond_digs = [h for _, bn, h in world.dig_log if bn == "deepslate_diamond_ore"]
    assert len(diamond_digs) >= 2
    assert all(h == "iron_pickaxe" for h in diamond_digs)


# ---- New Regression Tests for 2026-11-17 Multi-Location Findings ----


def test_mining_dfs_canopy_spawn_seeks_solid_ground_anchor() -> None:
    """LocA / LocB fix: spawning on oak_leaves canopy with 0 cobblestone descends to solid ground before Collar."""
    world = _MockMineWorld(
        start_pos=(0.5, 75.0, 0.5),
        surface_y=70,
        track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
        ],
    )
    # Build an oak canopy at Y=73..74 above hollow air Y=70..72 over solid ground at surface_y=70
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            world.blocks[(dx, 74, dz)] = "oak_leaves"
            world.blocks[(dx, 73, dz)] = "oak_leaves"
    # Place target iron ore underground at Y=64 along north descent from ground Y=70
    world.blocks[(0, 64, -7)] = "iron_ore"
    world.blocks[(1, 64, -7)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=64, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2
    collar_node = next(n for n in res["tree"]["nodes"] if n["kind"] == "collar")
    assert collar_node["anchor"][1] == 70  # Collar anchored on real ground Y=70, NOT canopy Y=75!
    # Digging oak_leaves must not consume pickaxe durability
    leaf_dig_tools = [tool for _, bn, tool in world.dig_log if bn == "oak_leaves"]
    assert all(t is None for t in leaf_dig_tools)


def test_mining_dfs_side_fluid_seals_and_continues_without_false_prune() -> None:
    """P0-1 fix: lateral water adjacent to descent/tunnel is sealed with cobblestone instead of pruning entire direction."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    # Block east/south/west with in-path bedrock so ONLY north can reach target_y=70
    for dpos in ((1, 75, 0), (-1, 75, 0), (0, 75, 1)):
        world.blocks[dpos] = "bedrock"
    # Put 1-block lateral water pocket right beside the north descent at (1, 74, -2)
    world.blocks[(1, 74, -2)] = "water"
    # Target iron ore further down the north staircase at (0, 70, -7)
    world.blocks[(0, 70, -7)] = "iron_ore"
    world.blocks[(0, 71, -7)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=70, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2
    # Lateral water pocket at (1, 74, -2) was plugged with cobblestone!
    assert world.block_at((1, 74, -2)) == "cobblestone"
    assert res["breaches_sealed"] >= 1


def test_mining_dfs_crosses_self_excavated_cavity_with_bridge_block() -> None:
    """P0-2 fix: crossing a previously dug tunnel (even after reset) places a bridge block and does not prune."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    # Block east/south/west so descent must go north
    for dpos in ((1, 75, 0), (-1, 75, 0), (0, 75, 1)):
        world.blocks[dpos] = "bedrock"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        # Pre-carve a cross-tunnel at z=-3, y=71..72 (which corresponds to support_pt=(0, 72, -3) at step 3)
        for x in (-2, -1, 0, 1, 2):
            for y in (71, 72):
                world.blocks[(x, y, -3)] = "air"
                skills.mining_dfs._record_excavated((x, y, -3))
        # Simulate tree reset (excavated_positions must survive reset!)
        skills.mining_dfs._clear_active_tree_only()

        world.blocks[(0, 70, -7)] = "iron_ore"
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=70, max_duration_s=20.0)
        digs_before_ret = len(world.dig_log)
        ret = skills.mine_dfs(action="return_collar")
        digs_after_ret = len(world.dig_log)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["tree"]["bridge_blocks_placed"] >= 1
    assert world.block_at((0, 72, -3)) == "cobblestone"
    assert ret["ok"] is True
    assert ret["dug_during_return"] == 0
    assert digs_after_ret == digs_before_ret


def test_mining_dfs_offset_sidestep_retry_bypasses_local_obstruction() -> None:
    """P1-6 fix: narrow 1-block bedrock bumpers on all 4 Collar faces trigger 3-block lateral offset retry."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    # Block immediate 1st stair step in all 4 cardinal directions with narrow 1-block bedrock
    for dpos in ((0, 75, -1), (0, 75, 1), (1, 75, 0), (-1, 75, 0)):
        world.blocks[dpos] = "bedrock"
    # Parallel lane 3 blocks west (-3, y, z) is completely open stone with iron ore at (-3, 72, -5)!
    world.blocks[(-3, 72, -5)] = "iron_ore"
    world.blocks[(-3, 73, -5)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=72, max_duration_s=20.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 2


def test_mining_dfs_64_block_radar_steers_spine_toward_distant_diamond() -> None:
    """P1-5 fix: 64-block radar detects exposed diamond ore 18 blocks east in cave wall and steers Spine east."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobblestone", "count": 32, "slot": 11},
        ],
    )
    # Small exposed cave pocket 16 blocks east at (16, -54, -4) with exposed deepslate_diamond_ore at (15, -54, -4)
    world.blocks[(16, -54, -4)] = "air"
    world.blocks[(16, -53, -4)] = "air"
    world.blocks[(15, -54, -4)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=1, target_y=-54, max_duration_s=25.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    # Verify Spine steered east (+X) rather than wasting durability going far west (-X)
    min_dug_x = min((pt[0] for pt, _, _ in world.dig_log), default=0)
    assert min_dug_x >= -4


def test_mining_dfs_reset_relocates_away_from_old_collar() -> None:
    """P1-4 fix: calling reset() when a Collar exists records exhausted collar and relocates >=14 blocks away."""
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    world.blocks[(0, 74, -3)] = "iron_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res1 = skills.mine_dfs(target="iron", goal_count=1, target_y=74, max_duration_s=15.0)
        assert res1["ok"] is True

        reset_res = skills.mine_dfs(action="reset")
        assert reset_res["ok"] is True
        assert "relocated_to" in reset_res
        rx, ry, rz = reset_res["relocated_to"]
        assert math.hypot(rx - 0, rz - 0) >= 12.0


def test_world_ledger_persists_shaft_and_ores_across_respawn_and_disk(tmp_path: Path) -> None:
    """P2-7 fix: respawning 20 blocks from Collar reuses existing shaft 0-dig; disk ledger verifies live world."""
    ledger_file = tmp_path / "world_ledger.json"
    world = _MockMineWorld(start_pos=(0.5, 76.0, 0.5), surface_y=76)
    world.blocks[(0, 72, -5)] = "iron_ore"
    world.blocks[(1, 72, -4)] = "diamond_ore"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        skills._mining_dfs = MiningDFSEngine(skills, ledger_path=ledger_file)
        res1 = skills.mine_dfs(target="iron", goal_count=1, target_y=72, max_duration_s=15.0)
        assert res1["ok"] is True
        assert ledger_file.exists()

    # New session + simulated respawn 18 blocks east at (18.5, 76.0, 0.5), now holding iron_pickaxe!
    world.pos = [18.5, 76.0, 0.5]
    world.items.append({"name": "iron_pickaxe", "count": 1, "slot": 12, "durability": [250, 250]})
    digs_before = len(world.dig_log)

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot2:
        skills2 = MacroSkills(bot2)
        skills2._mining_dfs = MiningDFSEngine(skills2, ledger_path=ledger_file)
        res2 = skills2.mine_dfs(target="diamond", goal_count=1, target_y=72, max_duration_s=15.0)

    assert res2["ok"] is True
    assert res2["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    # Reused Collar at (0, 76, 0) and walked down existing shaft instead of digging a brand-new staircase from Y=76!
    new_digs = world.dig_log[digs_before:]
    assert len(new_digs) <= 4


def test_underground_resupply_collar_return_craft_gear_table_reuse_and_mc_craft_auto_place() -> None:
    """P2-8, P2-9, P2-10 fix: pickaxe_low returns to Collar; craft_gear reuses distant table; mc_craft auto-places backpack table."""
    world = _MockMineWorld(
        start_pos=(0.5, 20.0, 0.5),
        surface_y=70,
        track_durability=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [28, 131]},
            {"name": "cobblestone", "count": 32, "slot": 10},
            {"name": "stick", "count": 4, "slot": 11},
        ],
    )
    # Carve initial Collar room at (0, 20, 0) with an existing placed crafting_table at (1, 20, 0)
    for x in (-1, 0, 1):
        for y in (20, 21):
            world.blocks[(x, y, 0)] = "air"
    world.blocks[(1, 20, 0)] = "crafting_table"

    with BotSession("tok", bot_username="Miner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        # Descend until stone_pickaxe hits redline (<=12) -> should auto-return 0-dig to Collar (0, 20, 0)!
        res_low = skills.mine_dfs(target="iron", goal_count=5, target_y=0, max_duration_s=20.0)
        assert res_low["stop_reason"] == "pickaxe_low"
        assert res_low.get("returned_to_collar_for_resupply") is True
        assert math.dist(world.pos, (0.5, 20.0, 0.5)) <= 1.5

        # Even if bot walked back down to bottom of dug shaft (~5 blocks away -> >4.6m from table),
        # craft_gear("stone_pickaxe") must navigate back to (1, 20, 0) and craft without oak_log ecology error!
        deep_nodes = [n for n in skills.mining_dfs.nodes.values() if n["anchor"][1] < 18] if False else [
            n for n in skills.mining_dfs.nodes.values() if n.anchor[1] < 18
        ]
        assert deep_nodes
        bottom = min(deep_nodes, key=lambda n: n.anchor[1])
        skills.mining_dfs._navigate_to_node(bottom.node_id, min_hp_ref=[20.0])
        assert math.dist(world.pos, (1.5, 20.5, 0.5)) > 4.6

        cg = skills.craft_gear("stone_pickaxe", count=1)
        assert cg["ok"] is True
        assert "ecology_check" not in cg

        # Also verify mc_craft auto-places a crafting_table from backpack when no table is placed nearby!
        world.blocks[(1, 20, 0)] = "stone"  # remove old table
        world._add_item("crafting_table", 1)
        world._add_item("stick", 2)
        world.blocks[(1, 20, 0)] = "air"
        world.blocks[(1, 21, 0)] = "air"
        ts = McToolset(bot)
        raw_mc_craft = json.loads(ts.mc_craft("stone_pickaxe", 1))
        assert raw_mc_craft["ok"] is True
        assert raw_mc_craft["result"]["crafted"] == "stone_pickaxe"
