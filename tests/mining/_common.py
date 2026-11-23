"""Shared imports and fixtures split from legacy tests/test_mining_dfs.py."""

from __future__ import annotations


import json


import math


from pathlib import Path


from typing import Any


import httpx


import pytest


from mc_agent.bot import BotSession


from mc_agent.harness.geofence import VoidGeofence


from mc_agent.harness.hud import compute_goal_gap_hint


from mc_agent.harness.mc_tools import McToolset


from mc_agent.skills.library import PICKAXE_TIERS, MacroSkills


from mc_agent.skills.mining_dfs import ShaftEdge


from mc_agent.skills.mining_dfs import MiningDFSEngine, normalize_dfs_strategy


class _MockMineWorld:
    """3D voxel world simulator enforcing anti-xray find_blocks, solid ground movement, drops, and tool equip logs."""

    def __init__(
        self,
        *,
        start_pos: tuple[float, float, float] = (0.5, 80.0, 0.5),
        inventory_items: list[dict[str, Any]] | None = None,
        surface_y: int = 80,
        dimension: str | None = None,
        simulate_best_harvest_tool: bool = False,
        track_durability: bool = False,
        simulate_realistic_reach_and_pockets: bool = False,
    ) -> None:
        self.pos = list(start_pos)
        self.surface_y = surface_y
        self.dimension = dimension
        self.simulate_best_harvest_tool = simulate_best_harvest_tool
        self.track_durability = track_durability
        self.simulate_realistic_reach_and_pockets = simulate_realistic_reach_and_pockets
        self.stuck_pocket_ores: set[tuple[int, int, int]] = set()
        self.fail_next_craft_no_result: bool = False
        self.visited_blocks: list[tuple[int, int, int]] = []
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
                        "dimension": self.dimension,
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
            req_types = set(body.get("types") or [])
            hostile_names = {"zombie", "skeleton", "creeper", "spider", "witch", "enderman"}
            ents = []
            for e in self.entities:
                ename = str(e.get("name") or "")
                etype = str(e.get("type") or "")
                if req_types:
                    matched = False
                    if "item" in req_types and (ename == "item" or etype == "item"):
                        matched = True
                    if "hostile" in req_types and (ename in hostile_names or etype == "hostile"):
                        matched = True
                    if ename in req_types or etype in req_types:
                        matched = True
                    if not matched:
                        continue
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
            if bn == "bedrock":
                return httpx.Response(200, json={"ok": False, "error": {"code": "dig_failed", "message": "unbreakable bedrock"}})
            if self.simulate_realistic_reach_and_pockets:
                eye = (self.pos[0], self.pos[1] + 1.62, self.pos[2])
                dist_eye = math.dist(eye, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5))
                if dist_eye > 4.75:
                    return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "too far"}})
                bx_f, by_f, bz_f = math.floor(self.pos[0]), math.floor(self.pos[1]), math.floor(self.pos[2])
                if max(abs(pt[0] - bx_f), abs(pt[2] - bz_f)) >= 2 or abs(pt[1] - by_f) >= 3:
                    passable_los = {"air", "cave_air", "tall_grass", "short_grass", "torch", "wall_torch"}
                    has_los_face = False
                    for sx in ({0} if pt[0] == bx_f else ({-1} if pt[0] > bx_f else {1})):
                        if sx != 0 and self.block_at((pt[0] + sx, pt[1], pt[2])) in passable_los:
                            has_los_face = True
                    for sz in ({0} if pt[2] == bz_f else ({-1} if pt[2] > bz_f else {1})):
                        if sz != 0 and self.block_at((pt[0], pt[1], pt[2] + sz)) in passable_los:
                            has_los_face = True
                    for sy in ({0} if pt[1] == by_f else ({-1} if pt[1] > by_f else {1})):
                        if sy != 0 and self.block_at((pt[0], pt[1] + sy, pt[2])) in passable_los:
                            has_los_face = True
                    if not has_los_face:
                        return httpx.Response(
                            200,
                            json={"ok": False, "error": {"code": "out_of_reach", "message": "occluded by solid rock"}},
                        )
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
            if pt in self.stuck_pocket_ores and bn.endswith("_ore"):
                if body.get("collect", True):
                    self._add_item("cobbled_deepslate", 1)
                self.entities.append(
                    {
                        "id": 900 + len(self.entities),
                        "name": "item",
                        "item": f"{self._drop_name_for(bn)}x1",
                        "pos": [pt[0] + 0.5, pt[1] + 0.25, pt[2] + 0.5],
                    }
                )
            elif body.get("collect", True) and not is_leaf:
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
            elif not body.get("collect", True) and not is_leaf and bn.endswith("_ore"):
                self.entities.append(
                    {
                        "id": 900 + len(self.entities),
                        "name": "item",
                        "item": f"{self._drop_name_for(bn)}x1",
                        "pos": [pt[0] + 0.5, pt[1] + 0.15, pt[2] + 0.5],
                    }
                )
            passable_drop = {"air", "cave_air"}
            for ent in self.entities:
                if ent.get("name") == "item":
                    ex, ey, ez = math.floor(ent["pos"][0]), math.floor(ent["pos"][1]), math.floor(ent["pos"][2])
                    while self.block_at((ex, ey - 1, ez)) in passable_drop and ey > -64:
                        ey -= 1
                        ent["pos"][1] = ey + 0.15
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
            if "entity_id" in body:
                ent = next((e for e in self.entities if e.get("id") == body["entity_id"]), None)
                if ent is None:
                    return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "no entity"}})
                ex, ey, ez = float(ent["pos"][0]), float(ent["pos"][1]), float(ent["pos"][2])
                eb_x, eb_y, eb_z = math.floor(ex), math.floor(ey), math.floor(ez)
                cands_g: list[tuple[float, int, int, int]] = []
                for dx_e in (-1, 0, 1):
                    for dz_e in (-1, 0, 1):
                        for dy_e in (-1, 0, 1):
                            gx_c, gy_c, gz_c = eb_x + dx_e, eb_y + dy_e, eb_z + dz_e
                            if (
                                self.block_at((gx_c, gy_c, gz_c)) in passable
                                and self.block_at((gx_c, gy_c + 1, gz_c)) in passable
                                and self.block_at((gx_c, gy_c - 1, gz_c))
                                not in passable | {"lava", "water", "flowing_lava", "flowing_water"}
                            ):
                                d_e = math.dist((gx_c + 0.5, float(gy_c), gz_c + 0.5), (ex, ey, ez))
                                cands_g.append((d_e, gx_c, gy_c, gz_c))
                if not cands_g:
                    return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
                cands_g.sort(key=lambda r: r[0])
                _, gx, gy_sel, gz = cands_g[0]
                candidate_ys = [gy_sel]
            elif "pos" in body:
                tx, ty, tz = (float(v) for v in body["pos"][:3])
                gx, gz = math.floor(tx), math.floor(tz)
                candidate_ys = [math.floor(ty)]
            elif "y" in body:
                tx, ty, tz = float(body["x"]), float(body["y"]), float(body["z"])
                gx, gz = math.floor(tx), math.floor(tz)
                candidate_ys = [math.floor(ty)]
            else:
                tx, tz = float(body["x"]), float(body["z"])
                gx, gz = math.floor(tx), math.floor(tz)
                base_y = math.floor(self.pos[1])
                candidate_ys = [base_y + dy for dy in (0, -1, 1, -2, 2, -3, -4, -5, -6, 3, 4)]
                if self.surface_y not in candidate_ys:
                    candidate_ys.append(self.surface_y)
            for gy in candidate_ys:
                if (
                    self.block_at((gx, gy, gz)) in passable
                    and self.block_at((gx, gy + 1, gz)) in passable
                    and self.block_at((gx, gy - 1, gz)) not in passable | {"lava", "water", "flowing_lava", "flowing_water"}
                ):
                    self.pos = [gx + 0.5, float(gy), gz + 0.5]
                    self.visited_blocks.append((gx, gy, gz))
                    pickup_r = 1.38 if self.simulate_realistic_reach_and_pockets else 2.2
                    kept = []
                    for e in self.entities:
                        if e.get("name") == "item":
                            ix, iy, iz = math.floor(e["pos"][0]), math.floor(e["pos"][1]), math.floor(e["pos"][2])
                            blocked_pocket = (
                                self.simulate_realistic_reach_and_pockets
                                and iy > gy
                                and self.block_at((ix, gy, iz)) not in passable
                            )
                            if not blocked_pocket and math.dist(self.pos, e["pos"]) <= pickup_r:
                                it_name = e["item"].split("x")[0]
                                self._add_item(it_name, 1)
                            else:
                                kept.append(e)
                        else:
                            kept.append(e)
                    self.entities = kept
                    return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos)}})
            return httpx.Response(200, json={"ok": False, "error": {"code": "path_not_found", "message": "blocked"}})
        if path == "/craft":
            if self.fail_next_craft_no_result:
                self.fail_next_craft_no_result = False
                return httpx.Response(
                    200,
                    json={"ok": False, "error": {"code": "craft_failed", "message": "craft failed: server placed no result"}},
                )
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
                if item == "furnace":
                    cob_ok = self._consume_item("cobblestone", 8 * cnt) or self._consume_item("cobbled_deepslate", 8 * cnt)
                    if not cob_ok:
                        return httpx.Response(200, json={"ok": False, "error": {"code": "missing_ingredients", "message": "missing"}})
                    self._add_item("furnace", cnt)
                    self.craft_log.append((item, cnt))
                    return httpx.Response(200, json={"ok": True, "result": {"crafted": item, "gained": cnt, "used_table": True}})
            return httpx.Response(200, json={"ok": False, "error": {"code": "no_recipe", "message": item}})
        if path == "/stop":
            return httpx.Response(200, json={"ok": True, "result": {"stopped": True}})
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


def _fake_shaft_engine(eng):
    """Attach a tiny 2-node shaft (collar -> spine) to a live engine for retreat tests."""
    from mc_agent.skills.mining_dfs import ShaftEdge, ShaftNode

    collar = ShaftNode(node_id=1, kind="collar", anchor=(0, 20, 0), parent_id=None,
                       edge_from_parent=None, branches=[])
    child = ShaftNode(node_id=2, kind="spine", anchor=(0, 30, 0), parent_id=1,
                      edge_from_parent=ShaftEdge("stair_down", "south",
                                                 [(0, 20, 0), (0, 25, 0), (0, 30, 0)]),
                      branches=[])
    eng.nodes = {1: collar, 2: child}
    eng.collar_node_id = 1
    eng.active_node_id = 2
    eng.known_corridor_positions = {(0, 30, 0), (0, 25, 0), (0, 20, 0)}
    return eng


class _MiningClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

