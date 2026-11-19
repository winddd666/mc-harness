"""Offline unit tests for path-compressed MiningDFS: tree nodes, breach seal, catch-block, dual-pickaxe, backtrack, radar, canopy escape, offset retry, WorldLedger, and resupply."""

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
    assert res["goal_met"] is True
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
        res = skills.mine_dfs(target="diamond", goal_count=1, target_y=-54, max_duration_s=25.0, strategy="radar")

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


# ---- Dual-Strategy (fishbone vs radar) & Deep Trajectory Audit Regression Tests ----


def test_dual_strategy_fishbone_ignores_distant_cave_while_radar_triages_hostiles_and_extracts_surgically(
    monkeypatch: Any,
) -> None:
    """Compare `fishbone` vs `v5` (bug-fixed v4) vs `radar` (with hostile triage + LocC-v4 surgical parapet)."""
    monkeypatch.delenv("MC_DFS_STRATEGY", raising=False)
    assert normalize_dfs_strategy(None) == "fishbone_scan"
    monkeypatch.setenv("MC_DFS_STRATEGY", "fishbone")
    assert normalize_dfs_strategy(None) == "fishbone"
    monkeypatch.setenv("MC_DFS_STRATEGY", "v5")
    assert normalize_dfs_strategy(None) == "v5"
    monkeypatch.setenv("MC_DFS_STRATEGY", "radar")
    assert normalize_dfs_strategy(None) == "radar"

    # 1) Conservative `fishbone`: mines corridor-adjacent diamond, completely ignores 15-block distant cave diamond
    fb_world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    # Distant exposed cave diamond 15 blocks east (should NOT be chased by fishbone)
    fb_world.blocks[(16, -54, -2)] = "cave_air"
    fb_world.blocks[(16, -53, -2)] = "cave_air"
    fb_world.blocks[(15, -54, -2)] = "deepslate_diamond_ore"
    # Corridor-adjacent diamond right beside the north Spine at (1, -54, -3)
    fb_world.blocks[(1, -54, -3)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="FishboneBot", transport=httpx.MockTransport(fb_world.handler)) as bot_fb:
        skills_fb = MacroSkills(bot_fb)
        res_fb = skills_fb.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="fishbone",
            max_duration_s=20.0,
        )

    assert res_fb["ok"] is True
    assert res_fb["strategy"] == "fishbone"
    assert res_fb["tree"]["strategy"] == "fishbone"
    assert res_fb["tree"]["radar_targets"] == []
    assert fb_world._count("diamond") >= 1
    assert fb_world.block_at((15, -54, -2)) == "deepslate_diamond_ore"

    # 1.5) Moderate `v5` (bug-fixed original v4):
    #      - Uses 64-block single-point radar to steer east toward (15, -54, -2) when reachable at Y=-54!
    #      - Does NOT perform cluster locking (`locked_cluster is None`) or surgical pocket breaching (`surgical_extractions == 0`).
    v5_world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    v5_world.blocks[(14, -54, -2)] = "cave_air"
    v5_world.blocks[(14, -53, -2)] = "cave_air"
    v5_world.blocks[(13, -54, -2)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="V5Bot", transport=httpx.MockTransport(v5_world.handler)) as bot_v5:
        skills_v5 = MacroSkills(bot_v5)
        res_v5 = skills_v5.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="v5",
            max_duration_s=20.0,
        )

    assert res_v5["ok"] is True
    assert res_v5["strategy"] == "v5"
    assert res_v5["tree"]["strategy"] == "v5"
    assert res_v5["radar_summary"]["ever_seen_count"] >= 1
    assert res_v5["radar_summary"]["locked_cluster"] is None
    assert res_v5["tree"]["surgical_extractions"] == 0
    assert v5_world._count("diamond") >= 1
    assert min((pt[0] for pt, _, _ in v5_world.dig_log), default=0) >= -4

    # 2) Aggressive `radar`:
    #    - West cluster (-10, -54, 0) has 2 diamonds exposed to cave_air (-11, -54, 0) WITH a zombie right inside!
    #    - North-East target (3, -56, -3) replicates LocC-v4 ([3484, -56, 427] from [3481, -54, 424]):
    #      occluded behind 2 solid deepslate blocks & 2m below Y=-54, backed by natural `cave_air` on its far side (x=4),
    #      with 0 hostiles nearby.
    rd_world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    # Dangerous monster-guarded west cluster
    rd_world.blocks[(-11, -54, 0)] = "cave_air"
    rd_world.blocks[(-11, -53, 0)] = "cave_air"
    rd_world.blocks[(-10, -54, 0)] = "deepslate_diamond_ore"
    rd_world.blocks[(-10, -54, -1)] = "deepslate_diamond_ore"
    rd_world.entities.append({"id": 501, "name": "zombie", "type": "hostile", "pos": [-11.5, -54.0, 0.5]})

    # Safe mob-free cave-backed occluded diamond replicating LocC-v4 geometry:
    cave_cells = {(4, -56, -3), (4, -55, -3), (4, -54, -3), (5, -56, -3), (5, -55, -3)}
    for cpt in cave_cells:
        rd_world.blocks[cpt] = "cave_air"
    rd_world.blocks[(3, -56, -3)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="RadarBot", transport=httpx.MockTransport(rd_world.handler)) as bot_rd:
        skills_rd = MacroSkills(bot_rd)
        res_rd = skills_rd.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="radar",
            max_duration_s=25.0,
        )

    assert res_rd["ok"] is True
    assert res_rd["strategy"] == "radar"
    assert rd_world._count("diamond") >= 1
    # Must avoid the zombie-guarded west cluster completely
    assert min((pt[0] for pt, _, _ in rd_world.dig_log), default=0) >= -2
    # Must NEVER step into natural `cave_air` cells at x>=4!
    assert all(vpt[0] < 4 for vpt in rd_world.visited_blocks)


def test_radar_stair_up_homing_reaches_overhead_ore_cluster() -> None:
    """Radar strategy climbs upward via `stair_up` when the locked exposed ore cluster is >=3 blocks above Spine."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 32, "slot": 11},
        ],
    )
    # Overhead diamond cluster at Y=-49 (5 blocks above Y=-54), exposed on its north face to a tiny cave pocket
    world.blocks[(0, -49, -7)] = "cave_air"
    world.blocks[(0, -48, -7)] = "cave_air"
    world.blocks[(0, -49, -6)] = "deepslate_diamond_ore"

    with BotSession("tok", bot_username="RadarClimber", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="diamond", goal_count=1, target_y=-54, strategy="radar", max_duration_s=25.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert world._count("diamond") >= 1
    assert any(n.get("edge_kind") == "stair_up" for n in res["tree"]["nodes"])


def test_zero_displacement_stair_down_never_spins_duplicate_spine_nodes() -> None:
    """Regression for realA4 (3295 nodes) & realA (152 nodes): starting at <=target_y with deeper radar ore never spins."""
    world = _MockMineWorld(
        start_pos=(0.5, 15.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "cobblestone", "count": 32, "slot": 10},
        ],
    )
    # Carve starting pocket at (0, 15, 0) where anchor_y=15 is already <= target_y=16
    world.blocks[(0, 15, 0)] = "air"
    world.blocks[(0, 16, 0)] = "air"
    # Deeper radar ore at (0, 6, -10), while all 4 directions around (0, 15, 0) are boxed in by bedrock at 2 blocks out
    # so stair_down / spine quickly hit bedrock and return 0 steps on blocked faces.
    world.blocks[(0, 6, -11)] = "cave_air"
    world.blocks[(0, 6, -10)] = "iron_ore"
    for dx in range(-2, 3):
        for dy in range(12, 18):
            for dz in range(-2, 3):
                if abs(dx) == 2 or abs(dz) == 2:
                    world.blocks[(dx, dy, dz)] = "bedrock"

    with BotSession("tok", bot_username="NoSpinBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=2, target_y=16, strategy="radar", max_duration_s=10.0)

    assert res["stop_reason"] in {"shaft_exhausted", "no_progress"}
    assert res["goal_met"] is False
    assert "action='reset'" not in res["message"]
    assert "reset 或 explore" not in res["message"]
    # Node count must remain tiny (never exploding to hundreds/thousands of duplicate Spine nodes)
    assert res["tree"]["node_count"] <= 16
    anchors = [tuple(n["anchor"]) for n in res["tree"]["nodes"]]
    assert len(anchors) == len(set(anchors))


def test_collar_hitbox_ledge_snap_prevents_false_no_solid_collar_ground() -> None:
    """Regression for realB & realC2: standing on hitbox ledge (floor(x) over air, x-1 solid) snaps onto solid block."""
    world = _MockMineWorld(
        start_pos=(10.05, 26.0, -20.05),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "cobblestone", "count": 16, "slot": 10},
        ],
    )
    # Make (10, 25, -21) and surrounding pit air so floor(pos)=(10, 26, -21) has NO solid block under it,
    # while adjacent (9, 25, -21) is solid stone with 2-block headroom (9, 26..27, -21) = air!
    for x in (10, 11):
        for y in range(20, 28):
            for z in (-22, -21, -20):
                world.blocks[(x, y, z)] = "air"
    world.blocks[(9, 25, -21)] = "stone"
    world.blocks[(9, 26, -21)] = "air"
    world.blocks[(9, 27, -21)] = "air"
    # Put iron ore along the first north descent from snapped Collar (9, 26, -21) -> (9, 23, -24)
    world.blocks[(9, 23, -24)] = "iron_ore"

    with BotSession("tok", bot_username="LedgeBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=23, strategy="fishbone", max_duration_s=15.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert world._count("raw_iron") >= 1


def test_verified_target_drop_rejects_cobble_trash_and_opens_1x1_pocket_base() -> None:
    """Regression for realA4 2252.5s: cobble pickup is NOT counted as ore; stuck upper 1x1 wall drop opens base block."""
    world = _MockMineWorld(
        start_pos=(0.5, 74.0, 0.5),
        surface_y=80,
        simulate_realistic_reach_and_pockets=True,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "cobblestone", "count": 16, "slot": 10},
        ],
    )
    # Pre-open 1 step north at (0, 74..75, -1) and place upper wall ore at (1, 75, -1) over solid base (1, 74, -1)
    world.blocks[(0, 74, -1)] = "air"
    world.blocks[(0, 75, -1)] = "air"
    world.blocks[(1, 75, -1)] = "iron_ore"
    world.blocks[(1, 74, -1)] = "stone"
    world.stuck_pocket_ores.add((1, 75, -1))

    with BotSession("tok", bot_username="DropVerifyBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(target="iron", goal_count=1, target_y=74, strategy="fishbone", max_duration_s=15.0)

    assert res["ok"] is True
    assert res["stop_reason"] == "quota_reached"
    assert res["target_gained"] >= 1
    assert world._count("raw_iron") >= 1
    # Confirm lower pocket block (1, 74, -1) was opened so the bot could actually collect the trapped raw_iron!
    assert any(pt == (1, 74, -1) for pt, _, _ in world.dig_log)


def test_shallow_diamond_y_clamp_hud_mine_dfs_guidance_and_craft_no_result_retry() -> None:
    """Verify LLM shallow diamond Y clamp (-30<y<=16 -> -54), HUD mine_dfs hints, and craft_gear 'no result' retry."""
    # 1) HUD guidance checks
    hint_iron = compute_goal_gap_hint(
        {"stone_pickaxe": 1, "cobblestone": 12},
        task="生存探险",
        target_advancement=None,
    )
    assert "mc_skill_mine_dfs(target='iron')" in hint_iron
    assert "mc_skill_staircase" not in hint_iron

    hint_diamond = compute_goal_gap_hint(
        {"iron_pickaxe": 1, "stone_pickaxe": 1},
        task="从当前状态出发挖到钻石（diamond >= 1）",
        target_advancement=None,
    )
    assert "target_y=-54" in hint_diamond
    assert "mc_skill_mine_dfs" in hint_diamond

    # 2) McToolset shallow diamond target_y=12 clamp + craft_gear "server placed no result" self-heal
    world = _MockMineWorld(
        start_pos=(0.5, -52.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobbled_deepslate", "count": 24, "slot": 11},
            {"name": "crafting_table", "count": 1, "slot": 12},
        ],
    )
    world.blocks[(0, -54, -3)] = "deepslate_diamond_ore"
    world.fail_next_craft_no_result = True

    with BotSession("tok", bot_username="ClampBot", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot)
        # Passing shallow target_y=12 to LLM tool must clamp to -54 and mine the Y=-54 diamond!
        dfs_out = json.loads(ts.mc_skill_mine_dfs(target="diamond", goal_count=1, target_y=12))
        assert dfs_out["ok"] is True
        assert dfs_out["clamped_shallow_diamond_y_from"] == 12
        assert dfs_out["target_y"] == -54
        assert world._count("diamond") >= 1

        # Craft furnace when first `/craft` fails with "craft failed: server placed no result"
        craft_res = ts.skills.craft_gear("furnace", count=1)
        assert craft_res["ok"] is True
        assert world._count("furnace") >= 1



def test_fishbone_scan_strategy_properties() -> None:
    """fishbone_scan = fishbone safety skeleton + read-only widened ore reach in target band."""
    from mc_agent.skills.mining_dfs import normalize_dfs_strategy, SCAN_BAND

    # alias resolution
    assert normalize_dfs_strategy("fishbone_scan") == "fishbone_scan"
    assert normalize_dfs_strategy("fbscan") == "fishbone_scan"
    assert normalize_dfs_strategy("scan") == "fishbone_scan"
    # must not collide with existing strategies
    assert normalize_dfs_strategy("fishbone") == "fishbone"
    assert normalize_dfs_strategy("radar") == "radar"
    assert normalize_dfs_strategy("v5") == "v5"

    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    with BotSession("tok", bot_username="ScanBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        eng._refresh_strategy("fishbone_scan")
        assert eng.strategy == "fishbone_scan"
        # safety skeleton preserved: NOT radar-aggressive, NOT surgical (no 1x1 breach-through)
        assert eng.radar_enabled is False
        assert eng.surgical_enabled is False
        # target-band gate: within +-SCAN_BAND of target_y is "in band"
        eng.target_y = -54
        assert abs((-54) - eng.target_y) <= SCAN_BAND          # anchor on target layer -> in band
        assert abs((-30) - eng.target_y) > SCAN_BAND           # transit layer -> not in band


def test_fishbone_spine_descends_when_target_deepened() -> None:
    """fishbone/fishbone_scan spine must schedule a stair_down when target_y moves deeper mid-shaft.

    Regression: the pure-fishbone skeleton only emitted branch_leaf/spine_forward, so raising
    the target depth (e.g. iron layer -> diamond layer) made the tree tunnel sideways on the
    stale layer forever instead of descending.
    """
    from mc_agent.skills.mining_dfs import SPINE_DESCEND_MIN_DELTA

    anchor = (0, 16, 0)

    for strategy in ("fishbone", "fishbone_scan"):
        world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
        with BotSession("tok", bot_username="SpineBot", transport=httpx.MockTransport(world.handler)) as bot:
            eng = MacroSkills(bot).mining_dfs
            eng._refresh_strategy(strategy)
            assert eng.radar_enabled is False  # skeleton has no native vertical branch

            # on-layer target: unchanged fishbone behaviour, no descent branch
            eng.target_y = 16
            flat = eng._spine_branches("north", anchor, geofence=None)
            assert [b.kind for b in flat] == ["branch_leaf", "branch_leaf", "spine_forward"]

            # deepened target: descent must be scheduled first so pending-order digs down
            eng.target_y = -54
            deep = eng._spine_branches("north", anchor, geofence=None)
            assert deep[0].kind == "stair_down"
            assert deep[0].branch_target_y == -54
            assert 0 < deep[0].steps <= 16
            assert deep[0].steps <= anchor[1] - eng.target_y
            # horizontal branches still present after the descent
            assert "spine_forward" in [b.kind for b in deep]

            # tiny depth differences are ignored to avoid needless vertical churn
            eng.target_y = 16 - SPINE_DESCEND_MIN_DELTA
            assert "stair_down" not in [b.kind for b in eng._spine_branches("north", anchor, geofence=None)]


def test_fishbone_spine_climbs_when_target_raised() -> None:
    """Symmetric gap: a shaft that overshot below target_y must be able to stair_up back to it."""
    world = _MockMineWorld(start_pos=(0.5, 80.0, 0.5), surface_y=80)
    with BotSession("tok", bot_username="ClimbBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng._refresh_strategy("fishbone")

        anchor = (0, -54, 0)
        eng.target_y = -54
        assert not [b for b in eng._spine_branches("north", anchor, geofence=None) if b.kind == "stair_up"]

        # diamond layer -> iron layer: must climb, otherwise the skeleton stays buried forever
        eng.target_y = 16
        deep = eng._spine_branches("north", anchor, geofence=None)
        assert deep[0].kind == "stair_up"
        assert deep[0].branch_target_y == 16
        assert 0 < deep[0].steps <= 16
        assert deep[0].steps <= 16 - anchor[1]


def test_fishbone_transit_keeps_bonus_wall_ores() -> None:
    """While descending to a deep layer, fishbone must keep harvesting wall iron/coal.

    Regression: target=diamond + an iron pickaxe used to switch the vein hook to
    "target ores only", so the descent ignored deepslate_iron_ore on the walls and the
    pickaxe died with too few ingots to craft a replacement.
    """
    from mc_agent.skills.mining_dfs import SCAN_BAND, _should_focus_target_only

    def focus(strategy: str, anchor_y: int, target_y: int, *, max_tier: int = 3, min_tier: int = 3) -> bool:
        return _should_focus_target_only(
            strategy=strategy,
            min_target_tier=min_tier,
            max_tier=max_tier,
            anchor_y=anchor_y,
            target_y=target_y,
        )

    # fishbone_scan: narrow focus ONLY on the target layer itself
    assert focus("fishbone_scan", -54, -54) is True
    assert focus("fishbone_scan", -54 + SCAN_BAND, -54) is True
    # transit layers keep scanning iron/coal so the pickaxe can always be recrafted
    assert focus("fishbone_scan", 16, -54) is False
    assert focus("fishbone_scan", -30, -54) is False
    assert focus("fishbone_scan", -54 - SCAN_BAND - 1, -54) is False

    # plain fishbone never narrows to target-only
    assert focus("fishbone", -54, -54) is False

    # radar family keeps its ore-steered focus everywhere
    assert focus("radar", 16, -54) is True
    assert focus("v5", 16, -54) is True

    # without a matching-tier pickaxe nothing is filtered out
    assert focus("fishbone_scan", -54, -54, max_tier=2) is False
    assert focus("fishbone_scan", -54, -54, min_tier=2) is False


def test_pickaxe_low_hint_tells_model_how_many_stone_picks_it_can_craft() -> None:
    """On pickaxe_low the model must be told the exact recraft headroom, not just 'low'."""
    from mc_agent.skills.mining_dfs import _pickaxe_low_hint, _stone_pickaxe_headroom

    # scan6's real inventory when it stalled at Y=-48: stone is abundant, logs untouched
    inv = {"cobbled_deepslate": 248, "cobblestone": 139, "oak_log": 30, "stick": 2}
    n, stone, sticks = _stone_pickaxe_headroom(inv)
    assert stone == 387
    assert sticks == 2 + 2 * (4 * 30)   # 2 sticks + 30 logs -> 120 planks -> 240 sticks
    assert n == 121                      # stick-limited, stone-limited would be 129

    hint = _pickaxe_low_hint(inv)
    assert "121" in hint
    assert "stone_pickaxe" in hint

    # stone rich but no wood -> tell it to make planks/sticks first
    no_wood_hint = _pickaxe_low_hint({"cobblestone": 9})
    assert "oak_planks" in no_wood_hint and "stick" in no_wood_hint

    # no stone at all -> dig some first
    assert "石料不足" in _pickaxe_low_hint({"oak_log": 8})

    # zero-material edge case must not claim a positive count
    n0, _, _ = _stone_pickaxe_headroom({})
    assert n0 == 0


def test_engine_recrafts_burner_stone_pickaxe_when_durability_low(monkeypatch) -> None:
    """Rock digging must auto-craft a stone burner pick instead of stopping at pickaxe_low."""
    world = _MockMineWorld(
        start_pos=(0.5, 80.0, 0.5),
        surface_y=80,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [40, 131]},
            {"name": "cobblestone", "count": 30, "slot": 10},
            {"name": "oak_log", "count": 4, "slot": 11},
            {"name": "crafting_table", "count": 1, "slot": 12},
        ],
    )
    with BotSession("tok", bot_username="RecraftBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        crafted: list[str] = []

        def fake_craft(item: str, count: int = 1, equip_slot: str | None = None) -> dict[str, Any]:
            crafted.append(item)
            return {"ok": True, "crafted": item, "gained": count}

        monkeypatch.setattr(skills, "craft_gear", fake_craft)
        counts = {"cobblestone": 30, "oak_log": 4, "crafting_table": 1}
        assert eng._try_recraft_burner_pickaxe(counts) is True
        assert crafted == ["stone_pickaxe"]
        assert eng.recrafted_picks == 1

        # no stone -> must not even attempt a craft
        assert eng._try_recraft_burner_pickaxe({"cobblestone": 1, "oak_log": 4}) is False
        assert crafted == ["stone_pickaxe"]

        # craft failure must not be counted as a recraft
        monkeypatch.setattr(skills, "craft_gear", lambda *a, **k: {"ok": False, "error": "missing_ingredients"})
        assert eng._try_recraft_burner_pickaxe(counts) is False
        assert eng.recrafted_picks == 1


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


def test_retreat_into_shaft_walks_back_and_seals_behind(monkeypatch) -> None:
    """Threat response must stay inside our own tunnel: back up one node and plug the choke."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = _fake_shaft_engine(MacroSkills(bot).mining_dfs)
        walked: list[tuple[list, bool]] = []
        sealed: list[list] = []
        monkeypatch.setattr(
            eng, "_walk_waypoints",
            lambda wps, reverse=False, min_hp_ref=None: (walked.append((list(wps), reverse)) or (True, None)),
        )
        monkeypatch.setattr(
            eng, "_seal_breach",
            lambda coords, cache, allow_corridor=False: (sealed.append(list(coords)) or [list(c) for c in coords]),
        )

        res = eng.retreat_into_shaft(max_hops=1)

    assert res["ok"] is True
    assert res["mode"] == "retreat_shaft"
    assert res["hops"] == 1
    assert res["sealed"] == 3
    assert eng.active_node_id == 1                    # handed back to the collar node
    assert walked and walked[0][1] is True            # walked the edge in reverse
    assert sealed and sealed[0] == [(0, 30, 0), (0, 31, 0), (0, 32, 0)]  # plugged the branch cross-section


def test_retreat_into_shaft_refuses_when_not_inside_own_corridor() -> None:
    """Standing in open cave air must NOT be treated as our shaft (caller falls back to flee)."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = _fake_shaft_engine(MacroSkills(bot).mining_dfs)
        eng.known_corridor_positions = {(40, -20, 40)}
        eng.nodes[1].anchor = (40, -20, 40)
        eng.nodes[2].anchor = (45, -20, 40)
        res = eng.retreat_into_shaft()
    assert res["ok"] is False
    assert res["reason"] == "not_in_shaft"


def test_flee_hostiles_prefers_shaft_retreat_over_free_pathfinding(monkeypatch) -> None:
    """flee_hostiles must route into the shaft retreat when the engine has a live tree."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)

        class _StubEngine:
            nodes = {1: object()}

            def retreat_into_shaft(self, **kwargs):
                return {"ok": True, "mode": "retreat_shaft", "hops": 1, "sealed": 2}

        skills._mining_dfs = _StubEngine()
        res = skills.flee_hostiles(distance=14.0)

    assert res["mode"] == "retreat_shaft"
    assert res["hops"] == 1


def test_retreat_walls_off_threat_side_before_walking(monkeypatch) -> None:
    """Being hit by an adjacent mob must seal the threat side first (walking away only trades hits)."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="RetreatBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = _fake_shaft_engine(MacroSkills(bot).mining_dfs)
        sealed: list[list] = []
        monkeypatch.setattr(
            eng, "_walk_waypoints",
            lambda wps, reverse=False, min_hp_ref=None: (True, None),
        )
        monkeypatch.setattr(
            eng, "_seal_breach",
            lambda coords, cache, allow_corridor=False: (sealed.append(list(coords)) or [list(c) for c in coords]),
        )
        res = eng.retreat_into_shaft(max_hops=1, threat_pos=(0.5, 30.0, 12.5))

    assert res["ok"] is True
    wall = sealed[0]                      # first seal call is the threat-facing wall
    assert len(wall) == 9                 # 3 columns x 3 high
    assert all(c[2] == 1 for c in wall)   # +z side, i.e. toward the threat
    assert res["sealed_first"] == 9


def test_flee_reports_respawn_instead_of_fake_success(monkeypatch) -> None:
    """A 7000-block 'flee' is a death respawn; it must not look like a successful escape."""
    world = _MockMineWorld(start_pos=(0.5, 30.0, 0.5), surface_y=30)
    with BotSession("tok", bot_username="FleeBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        # status(): first call = pre-flee, last call = teleported to world spawn after death
        calls = {"n": 0}

        def fake_status():
            calls["n"] += 1
            if calls["n"] <= 2:
                return {"pos": [0.5, 30.0, 0.5], "health": 12.0, "alive": True, "nearest_hostile": {"name": "zombie", "dist": 3.0}}
            return {"pos": [-152.0, 69.0, -379.0], "health": 20.0, "alive": True, "nearest_hostile": None}

        monkeypatch.setattr(skills.bot, "status", fake_status)
        monkeypatch.setattr(skills.bot, "inventory", lambda *a, **k: {"items": [], "offhand": {}})
        monkeypatch.setattr(skills.bot, "call", lambda *a, **k: {"ok": True})
        res = skills.flee_hostiles(distance=14.0)

    assert res["mode"] == "respawned"
    assert res["ok"] is False
    assert res["moved_dist"] > 64
    assert "死亡重生" in res["note"]


def test_fishbone_scan_harvests_wall_pocket_diamond_and_deep_reset_relocates() -> None:
    """fishbone_scan extracts a target-band diamond 2-3 blocks behind the corridor wall, and deep reset records active_anchor."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "stone_pickaxe", "count": 1, "slot": 9, "durability": [131, 131]},
            {"name": "iron_pickaxe", "count": 1, "slot": 10, "durability": [250, 250]},
            {"name": "cobblestone", "count": 64, "slot": 11},
        ],
    )
    # Block east/south/west at start so spine heads north along x=0, z=0..-4;
    # place an exposed diamond 3 blocks east of (0, -54, -2) behind 2 blocks of solid rock at (1..2, -54, -2),
    # exposed on its far side (4, -54, -2) so anti-xray find_blocks sees (3, -54, -2)!
    for dpos in ((1, -54, 0), (-1, -54, 0), (0, -54, 1)):
        world.blocks[dpos] = "bedrock"
    world.blocks[(3, -54, -2)] = "deepslate_diamond_ore"
    world.blocks[(4, -54, -2)] = "air"
    world.blocks[(4, -53, -2)] = "air"

    with BotSession("tok", bot_username="ScanMiner", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        res = skills.mine_dfs(
            target="diamond",
            goal_count=1,
            target_y=-54,
            strategy="fishbone_scan",
            max_duration_s=25.0,
        )
        assert res["ok"] is True
        assert res["target_gained"] >= 1
        assert [3, -54, -2] in res["mined_ores"]

        # Simulate deep shaft where collar was far away at (0, 16, 0) while active_node is at current deep pos
        eng = skills.mining_dfs
        eng.nodes[eng.collar_node_id].anchor = (0, 16, 0)
        cur_deep = (math.floor(world.pos[0]), math.floor(world.pos[1]), math.floor(world.pos[2]))
        eng.nodes[eng.active_node_id].anchor = cur_deep
        # Contract changed on purpose (2026-11-18): a reset 38 blocks below the collar is refused
        # instead of silently clearing the tree (live s01 burned 8 such resets in one run).
        rst = skills.mine_dfs(action="reset", strategy="fishbone_scan")
        assert rst["ok"] is False
        assert rst["stop_reason"] == "reset_needs_surface"
        assert eng.collar_node_id is not None        # tree preserved, no silent wipe



def test_restore_skips_shaft_saved_for_a_different_target_layer() -> None:
    """A ledger shaft saved for -54 must not be reused when the caller asks for iron at 16."""
    from mc_agent.skills.mining_dfs import ShaftNode

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="RestoreBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        node = ShaftNode(node_id=1, kind="collar", anchor=(0, 20, 0), parent_id=None,
                         edge_from_parent=None, branches=[])
        eng.ledger.saved_shafts = [{
            "collar_anchor": [0, 20, 0],
            "collar_node_id": 1,
            "target_y": -54,
            "nodes": [node.to_dict()],
        }]
        eng.target_y = 16
        assert eng._try_restore_saved_shaft((0, 20, 0), min_hp_ref=[20.0]) is False
        assert not eng.nodes


def test_three_fluid_aborts_mark_shaft_as_water_trap() -> None:
    """Aquifers used to cost whole runs (6 drown deaths): 3 hazard aborts must force a new shaft."""
    world = _MockMineWorld(start_pos=(0.5, 40.0, 0.5), surface_y=40)
    with BotSession("tok", bot_username="WaterBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert eng._note_fluid_abort("fluid_or_bedrock") is False
        assert eng._note_fluid_abort("unsafe_floor_or_cave") is False
        assert eng._note_fluid_abort("fluid_or_bedrock") is True        # 3rd hazard abort

        eng.fluid_aborts = 2
        # neutral outcomes keep the streak (still exploring the same wet layer)
        assert eng._note_fluid_abort("completed") is False
        # a real setback like hurt resets the streak
        assert eng._note_fluid_abort("hurt") is False
        assert eng.fluid_aborts == 0


def test_entry_gate_recrafts_before_reporting_pickaxe_low(monkeypatch) -> None:
    """No pickaxe at call entry must trigger an in-place recraft, not an immediate pickaxe_low bail."""
    world = _MockMineWorld(
        start_pos=(0.5, 20.0, 0.5),
        surface_y=70,
        inventory_items=[
            {"name": "cobblestone", "count": 64, "slot": 10},
            {"name": "oak_log", "count": 8, "slot": 11},
        ],
    )
    with BotSession("tok", bot_username="GateBot", transport=httpx.MockTransport(world.handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        calls = []
        monkeypatch.setattr(skills, "craft_gear",
                            lambda item, count=1, equip_slot=None: (calls.append(item) or {"ok": True}))
        res = eng.run(target="iron", goal_count=1, target_y=16, action="explore", max_duration_s=5.0)
    assert calls == ["stone_pickaxe"]          # recraft attempted at the gate
    assert res.get("stop_reason") != "pickaxe_low" or calls


def test_fluid_trap_deep_underground_does_not_try_surface_relocation() -> None:
    """Live s03: 3 cave breeches at y=8 -> relocation found no surface stand -> orphaned tree."""
    import time as _time

    world = _MockMineWorld(start_pos=(0.5, 8.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="DeepBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        node = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = node.node_id
        moved, auto = eng._force_relocate_on_fluid(
            geofence=None, deadline=_time.monotonic() + 100.0,
            min_hp_ref=[20.0], auto_relocations=0,
        )
    assert (moved, auto) == (False, 0)
    assert eng.fluid_abandoned_deep is True
    assert eng.collar_node_id is None                     # archived, must climb out first


def test_reset_is_refused_while_deep_below_the_collar() -> None:
    """Underground reset used to clear the tree with no surface anchor -> no_progress spin."""
    world = _MockMineWorld(start_pos=(0.5, 8.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="DeepResetBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        node = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = node.node_id
        res = eng.reset(geofence=None)
        assert eng.collar_node_id == node.node_id        # tree kept intact
    assert res["ok"] is False
    assert res["stop_reason"] == "reset_needs_surface"
    assert "climb_out" in res["message"]


def test_reset_refused_when_a_deep_second_collar_tries_to_move_again() -> None:
    """Live s01: 8 resets at y=-54 churned corridors and wasted the whole 40 min budget."""
    world = _MockMineWorld(start_pos=(0.5, -54.0, 0.5), surface_y=99)
    with BotSession("tok", bot_username="ThrashBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        surface = eng._create_node(kind="collar", anchor=(0, 99, 0), parent_id=None,
                                   edge_from_parent=None, branches=[])
        eng.collar_ceiling_y = 99                       # we already opened a surface collar
        deep = eng._create_node(kind="collar", anchor=(0, -54, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = deep.node_id
        res = eng.reset(geofence=None)
        assert eng.collar_node_id == deep.node_id       # tree kept
    assert res["stop_reason"] == "reset_needs_surface"
    assert "explore" in res["message"]
    assert surface.node_id in eng.nodes


def test_shaft_exhausted_message_is_built_after_result_dict_exists() -> None:
    """Regression: s04 crashed with UnboundLocalError because out_res was written too early."""
    import pathlib as _pl

    src = _pl.Path("src/mc_agent/skills/mining_dfs.py").read_text(encoding="utf-8")
    created = src.index("out_res: dict[str, Any] = {")
    used = src.index('if stop_reason == "shaft_exhausted" and self.fluid_abandoned_deep:')
    assert used > created


def test_manual_reset_clears_the_hazard_streak() -> None:
    """Live s03: stale fluid_aborts made reset -> explore return shaft_exhausted instantly, x6."""
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=71)
    with BotSession("tok", bot_username="StreakBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.fluid_aborts = 3
        eng.fluid_abandoned_deep = True
        eng.collar_ceiling_y = 71
        node = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                edge_from_parent=None, branches=[])
        eng.collar_node_id = node.node_id
        res = eng.reset(geofence=None)                 # allowed: we are at the collar level
    assert res["ok"] is True
    assert eng.fluid_aborts == 0
    assert eng.fluid_abandoned_deep is False


def test_deep_empty_tree_asks_for_a_climb_instead_of_opening_fake_collar() -> None:
    """Live s01/s03: a deep "collar" at y=-54 made later resets look legitimate forever."""
    world = _MockMineWorld(start_pos=(0.5, 8.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="DeepCollarBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64                     # we did open a surface collar earlier
        eng.idle_calls = 3
        res = eng.run(target="iron", goal_count=4, target_y=16, action="explore", max_duration_s=6.0)
    assert res["stop_reason"] == "needs_surface_climb"
    assert res["requires_replan"] is True
    assert eng.collar_node_id is None                 # no fake underground collar planted


def test_reset_on_empty_tree_is_refused_not_a_free_ok() -> None:
    """Live s07/s08: empty-tree reset returned ok:true and the model pressed it 250+ times."""
    world = _MockMineWorld(start_pos=(0.5, 62.0, 0.5), surface_y=62)
    with BotSession("tok", bot_username="EmptyResetBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert not eng.nodes
        res = eng.reset(geofence=None)
    assert res["ok"] is False
    assert res["stop_reason"] == "nothing_to_reset"
    assert "explore" in res["message"]


def test_drop_reachability_requires_standing_next_to_the_ore() -> None:
    """The dig API ignores reach, so the engine must gate on pickup range by itself."""
    world = _MockMineWorld(start_pos=(0.5, 16.0, 0.5), surface_y=16)
    with BotSession("tok", bot_username="ReachBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        cache = {}

        # face-adjacent ore: always fine
        assert eng._drop_cell_reachable((1, 16, 0), (0, 16, 0), cache) is True

        # 4 blocks away behind solid stone: not collectable
        solid = {(x, 16, 0): "stone" for x in range(-6, 7)}
        assert eng._drop_cell_reachable((4, 16, 0), (0, 16, 0), dict(solid)) is False

        # 2 blocks away but one neighbour is open and one step from us -> we can walk next to it
        cache2 = dict(solid)
        cache2[(3, 16, 0)] = "air"
        assert eng._drop_cell_reachable((4, 16, 0), (2, 16, 0), cache2) is True


def test_drowning_counts_toward_water_trap_abandonment() -> None:
    """Live s12: 3 successful escapes still left fluid_aborts at 0, so the pool was never abandoned."""
    world = _MockMineWorld(start_pos=(0.5, 17.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="WetBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert eng._note_fluid_abort("in_water") is False
        assert eng._note_fluid_abort("fluid_or_bedrock") is False
        assert eng._note_fluid_abort("in_water") is True     # 3rd water-related abort


def test_climb_out_action_reports_progress_and_target(monkeypatch) -> None:
    """One call must own probe+repeat; the model should not spend a turn per blocked direction."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="ClimbBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64                     # target = 48 (ceiling-16)
        calls: list[str] = []

        def fake_status():
            return {"pos": [0.5, float(world.pos[1]), 0.5], "health": 20.0, "food": 20.0, "alive": True}

        def fake_stair(direction=None, steps=0, vertical="down", stop_on_ore=None):
            if direction in {"north", "south", "east", "west"}:
                calls.append(direction)
                world.pos = [world.pos[0], min(64.0, world.pos[1] + 4.0), world.pos[2]]
                return {"ok": True, "steps_done": 4}
            return {"ok": False}

        monkeypatch.setattr(eng.bot, "status", fake_status)
        monkeypatch.setattr(eng.skills, "dig_staircase", fake_stair)
        res = eng.run(action="climb_out", target_y=16, max_duration_s=20.0)

    assert res["action"] == "climb_out"
    assert res["climb"]["target_y"] == 48
    assert res["climb"]["reached"] is True
    assert res["climb"]["climbed"] >= 28
    assert calls


def test_sealing_cave_breaches_does_not_condemn_a_healthy_shaft() -> None:
    """Live s11: cave-heavy terrain hit 3 seals in a row and the shaft was abandoned at y=54."""
    world = _MockMineWorld(start_pos=(0.5, 54.0, 0.5), surface_y=61)
    with BotSession("tok", bot_username="CaveBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        for _ in range(6):
            assert eng._note_fluid_abort("cave_breach_sealed") is False
        assert eng.fluid_aborts == 0


def test_retired_shaft_keeps_its_walkable_route() -> None:
    """Live s12: record_exhausted_collar deleted the shaft, so the 120-block route was re-dug."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="RouteBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        tip = eng._create_node(kind="spine", anchor=(0, 20, 0), parent_id=collar.node_id,
                               edge_from_parent=ShaftEdge("stair_down", "north",
                                                          [(0, y, 0) for y in range(64, 19, -1)]),
                               branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = tip.node_id
        eng._archive_current_shaft_to_ledger(mark_exhausted=True)

        saved = [s for s in eng.ledger.saved_shafts if tuple(s["collar_anchor"]) == (0, 64, 0)]
        assert saved, "exhausted shaft must keep its node graph"
        assert saved[0]["retired"] is True
        assert len(saved[0]["nodes"]) >= 2


def test_climb_out_prefers_walking_the_shaft_over_new_stairs(monkeypatch) -> None:
    """Walking our own corridor must win over re-digging stairs (s12: 27 turns of staircase)."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="WalkHomeBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        tip = eng._create_node(kind="spine", anchor=(0, 20, 0), parent_id=collar.node_id,
                               edge_from_parent=ShaftEdge("stair_down", "north", [(0, 40, 0)]),
                               branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = tip.node_id
        eng.collar_ceiling_y = 64

        def fake_status():
            return {"pos": [0.5, float(world.pos[1]), 0.5], "health": 20.0, "food": 20.0, "alive": True}

        def fake_nav(goal_node_id, min_hp_ref, deadline=None):
            world.pos = [0.5, 64.0, 0.5]          # walk all the way home
            return True, None

        stairs: list[str] = []
        monkeypatch.setattr(eng.bot, "status", fake_status)
        monkeypatch.setattr(eng, "_navigate_to_node", fake_nav)
        monkeypatch.setattr(eng.skills, "dig_staircase",
                            lambda **kw: (stairs.append(kw.get("direction")), {"ok": True})[1])
        res = eng.run(action="climb_out", target_y=16, max_duration_s=20.0)

    assert res["climb"]["method"] == "walk"
    assert res["climb"]["reached"] is True
    assert stairs == []                            # never dug a single stair


def test_corridor_repair_digs_fill_but_refuses_flooded_cells() -> None:
    """A filled-in corridor cell must be re-opened instead of abandoning the route."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    world.blocks[(0, 20, -1)] = "stone"
    world.blocks[(0, 20, -3)] = "water"
    with BotSession("tok", bot_username="RepairBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        assert eng._clear_route_obstruction((0, 20, -1)) >= 1      # solid fill -> dug
        assert eng._clear_route_obstruction((0, 20, -3)) == -1     # flooded -> give up


def test_waypoint_walk_interpolates_long_hops() -> None:
    """A 35-block climb must be walked cell by cell; one move_exact over it always fails."""
    from mc_agent.skills.mining_dfs import _interpolate_cells

    hops = _interpolate_cells((0, 63, 0), (0, 28, 0))
    assert len(hops) == 36
    assert hops[0] == (0, 63, 0) and hops[-1] == (0, 28, 0)
    assert all(abs(hops[i + 1][1] - hops[i][1]) == 1 for i in range(len(hops) - 1))


def test_upward_layer_switch_is_refused_on_an_existing_shaft() -> None:
    """Live s20: switching diamond(-54) back to iron(16) made the engine climb 20 blocks up."""
    world = _MockMineWorld(start_pos=(0.5, -54.0, 0.5), surface_y=70)
    with BotSession("tok", bot_username="LayerBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = collar.node_id
        eng.collar_ceiling_y = 64
        eng.target_y = -54
        res = eng.run(target="iron", goal_count=4, target_y=16, action="explore", max_duration_s=6.0)
    assert res["stop_reason"] == "layer_switch_upward_refused"
    assert res["requires_replan"] is True


def test_climb_target_overshoots_the_explore_threshold() -> None:
    """Live n0332: target==ceiling-20 made climb "reach" exactly where explore still refuses."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="BoundaryBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64
        climb = eng._climb_out(deadline=0.0)          # no time: just check the computed target
    assert climb["target_y"] == 48                    # > 64-20=44, so explore will accept


def test_thirty_dry_calls_abandon_a_dead_shaft() -> None:
    """n0344 burned 45 dry calls for 1 hit; n0424 recovered after ~20, so the bar is 30."""
    world = _MockMineWorld(
        start_pos=(0.5, -54.0, 0.5),
        surface_y=64,
        inventory_items=[
            {"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]},
            {"name": "stone_pickaxe", "count": 1, "slot": 10, "durability": [131, 131]},
            {"name": "cobblestone", "count": 64, "slot": 11},
        ],
    )
    with BotSession("tok", bot_username="DryBot", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng.active_node_id = collar.node_id
        eng.collar_ceiling_y = 64
        eng.target_y = -54
        eng.dry_calls = 30
        res = eng.run(target="diamond", goal_count=4, target_y=-54, action="explore", max_duration_s=6.0)
    assert res["stop_reason"] == "shaft_dry_abandoned"
    assert res["requires_replan"] is True
    assert eng.dry_calls == 0


class _MiningClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_climb_out_honors_the_callers_budget_in_nested_staircase(monkeypatch) -> None:
    """A 5s climb must never turn into the old implicit 90s staircase budget."""
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    requests: list[tuple[float, str, dict]] = []

    def slow_handler(request):
        timeout = request.extensions["timeout"]
        requests.append((clock.now, request.url.path, timeout))
        clock.now += 0.25
        return world.handler(request)

    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    with BotSession("tok", bot_username="TimedClimb", transport=httpx.MockTransport(slow_handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs
        eng.collar_ceiling_y = 64
        original_call = bot.call
        result = eng.run(action="climb_out", max_duration_s=5.0)
        assert eng.bot is bot and eng.skills is skills
        assert skills.bot is bot and bot.call == original_call
    assert clock.now <= 1005.0
    assert result["stop_reason"] == "time_slice"
    assert requests
    for started, _, timeouts in requests:
        assert 0 < timeouts["read"] <= 1005.0 - started
    # No quarantine reconnect, background retry, or new action starts after cutoff.
    assert all(started < 1005.0 for started, _, _ in requests)
    assert not any(path.endswith(("/connect", "/disconnect")) for _, path, _ in requests)


def test_dfs_budget_includes_entry_observations_and_radar(monkeypatch) -> None:
    """Initialization used to run outside the slice, then get a fresh full deadline."""
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    calls: list[str] = []

    def slow_handler(request):
        calls.append(request.url.path)
        clock.now += 0.4
        return world.handler(request)

    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    with BotSession("tok", bot_username="TimedInit", transport=httpx.MockTransport(slow_handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", max_duration_s=5.0)
    assert clock.now <= 1005.0
    assert result["stop_reason"] == "time_slice"
    assert len(calls) < 15


def test_default_dfs_slice_allows_slow_ore_dig_within_budget(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64)
    ore = (1, 64, 0)
    world.blocks[ore] = "iron_ore"
    dig_timeouts = []

    def handler(request):
        if request.url.path.endswith("/dig") and tuple(json.loads(request.content)["pos"]) == ore:
            timeouts = request.extensions["timeout"]
            dig_timeouts.append((clock.now, timeouts))
            if timeouts["read"] < 8.0:
                clock.now += timeouts["read"]
                raise httpx.ReadTimeout("ore dig still running", request=request)
            clock.now += 8.0
        return world.handler(request)

    with BotSession("tok", bot_username="SlowOreDig", transport=httpx.MockTransport(handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", target_y=64, goal_count=1)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_gained"] == 1
    assert clock.now <= 1032.0
    [(started, timeouts)] = dig_timeouts
    assert 2 * sum(timeouts.values()) + 0.15 <= 1031.5 - started + 1e-9


def test_waypoint_interpolation_checks_deadline_between_hops(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    visited: list[tuple[int, int, int]] = []

    def step(cell):
        visited.append(cell)
        clock.now += 1.0
        return True

    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="TimedWalk", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        monkeypatch.setattr(eng, "_step_to", step)
        result = eng._walk_waypoints([(30, 20, 0)], min_hp_ref=[20.0], deadline=1002.0)
    assert result == (False, "walk_time_slice")
    assert visited == [(1, 20, 0), (2, 20, 0)]


def test_climb_can_restore_a_retired_exhausted_route_from_the_bottom(monkeypatch) -> None:
    """A deep bot must match the archived route, even 44 blocks below its collar."""
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="DeepRetired", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 64, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        eng._create_node(kind="spine", anchor=(0, 20, 0), parent_id=collar.node_id,
                         edge_from_parent=ShaftEdge("stair_down", "north", [(0, 64, 0), (0, 20, 0)]),
                         branches=[])
        eng.collar_ceiling_y = 64
        eng._archive_current_shaft_to_ledger(mark_exhausted=True)
        eng._clear_active_tree_only()
        stairs: list[dict] = []

        def nav(goal_node_id, **kwargs):
            if goal_node_id == eng.collar_node_id:
                world.pos[1] = 64.0
            return True, None

        monkeypatch.setattr(eng, "_navigate_to_node", nav)
        monkeypatch.setattr(eng.skills, "dig_staircase", lambda **kw: stairs.append(kw))
        result = eng.run(action="climb_out", max_duration_s=5.0)
    assert result["climb"]["method"] == "walk"
    assert result["climb"]["reached"] is True
    assert not stairs


def test_navigation_rejoins_an_edge_instead_of_moving_through_a_wall(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(5.5, 15.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="MidEdge", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        collar = eng._create_node(kind="collar", anchor=(0, 20, 0), parent_id=None,
                                  edge_from_parent=None, branches=[])
        eng.collar_node_id = collar.node_id
        route = [(i, 20 - i, 0) for i in range(11)]
        eng._create_node(kind="switchback", anchor=(10, 10, 0), parent_id=collar.node_id,
                         edge_from_parent=ShaftEdge("stair_down", "east", route), branches=[])
        walks: list[list] = []

        def walk(points, **kwargs):
            walks.append(points)
            world.pos = [points[-1][0] + 0.5, float(points[-1][1]), points[-1][2] + 0.5]
            return True, None

        monkeypatch.setattr(eng, "_walk_waypoints", walk)
        monkeypatch.setattr(eng, "_move_exact", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct wall move")))
        assert eng._navigate_to_node(collar.node_id, min_hp_ref=[20.0]) == (True, None)
    assert walks[0][:6] == list(reversed(route[:6]))


def test_budget_cutoff_preserves_partial_edge_for_the_next_call(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="PartialEdge", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        branch = mining.BranchSpec("stair_down", "east", 8)
        collar = eng._create_node(kind="collar", anchor=(0, 20, 0), parent_id=None,
                                  edge_from_parent=None, branches=[branch])
        eng.collar_node_id = collar.node_id

        def interrupted(**kwargs):
            branch.status = "active"
            eng.bot.status()
            world.pos = [1.5, 19.0, 0.5]
            eng.bot.status()
            clock.now = kwargs["deadline"]
            eng.bot.call("dig", {"pos": [2, 18, 0]})

        monkeypatch.setattr(eng, "_run", interrupted)
        result = eng.run(max_duration_s=5.0)
        tip = eng.nodes[eng.active_node_id]
        assert tip.parent_id == collar.node_id
        assert tip.anchor == (1, 19, 0)
        assert tip.edge_from_parent.waypoints == [(0, 20, 0), (1, 19, 0)]
        assert tip.branches[0].status == "pending"
        assert branch.status == "completed"
    assert result["stop_reason"] == "time_slice_progress"
    assert result["ok"] is True
    assert result["goal_met"] is False
    assert result["pos_before"] == [0.5, 20.0, 0.5]
    assert result["pos_after"] == [1.5, 19.0, 0.5]
    assert not world.dig_log


def test_climb_remembers_all_blocked_primitives_at_the_same_cell(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    attempts: list[tuple[str, str]] = []
    with BotSession("tok", bot_username="BlockedClimb", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64
        monkeypatch.setattr(eng.skills, "dig_staircase",
                            lambda **kw: (attempts.append(("stair", kw["direction"])) or {"ok": False}))
        monkeypatch.setattr(eng.skills, "dig_level_tunnel",
                            lambda **kw: (attempts.append(("tunnel", kw["direction"])) or {"ok": False}))
        first = eng.run(action="climb_out", max_duration_s=5.0)
        count = len(attempts)
        second = eng.run(action="climb_out", max_duration_s=5.0)
    assert first["stop_reason"] == second["stop_reason"] == "climb_blocked"
    assert count == 9  # four stairs, one pillar, four lateral exits
    assert len(attempts) == count
    assert "可继续 climb_out" not in second["message"]


def test_slow_blocked_climb_tries_a_different_direction_next_slice(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=70)
    directions: list[str] = []

    def blocked_stair(**kwargs):
        directions.append(kwargs["direction"])
        clock.now += 4.4
        return {"ok": False, "steps_done": 0}

    with BotSession("tok", bot_username="SlowBlockedClimb", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64
        monkeypatch.setattr(eng.skills, "dig_staircase", blocked_stair)
        first = eng.run(action="climb_out", max_duration_s=5.0)
        second = eng.run(action="climb_out", max_duration_s=5.0)
    assert first["stop_reason"] == second["stop_reason"] == "time_slice"
    assert directions == ["north", "south"]
    assert clock.now < 1010.0


@pytest.mark.parametrize(("budget", "retry_budget"), [(32.0, 47), (90.0, None)])
def test_nested_http_timeout_stops_the_action_and_restores_shared_sessions(monkeypatch, budget, retry_budget) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/dig"):
            raise httpx.ReadTimeout("offline simulated stalled dig", request=request)
        return world.handler(request)

    with BotSession("tok", bot_username="TimedHTTP", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        eng = skills.mining_dfs

        def stalled(**kwargs):
            eng.bot.status()
            eng.skills.bot.call("dig", {"pos": [1, 20, 0]})

        monkeypatch.setattr(eng, "_run", stalled)
        result = eng.run(max_duration_s=budget)
        assert eng.bot is bot and eng.skills is skills and skills.bot is bot
        assert bot._recovering_quarantine is False
    assert result["stop_reason"] == "time_slice"
    assert result["ok"] is False and result["requires_replan"] is True
    assert "原地用相同参数重试" in result["message"]
    next_steps = result["suggested_next"]
    assert "安全站位" in next_steps["if_blocked"]
    assert next_steps.get("retry_with") == ({"max_duration_s": retry_budget} if retry_budget else None)
    assert [p.rsplit("/", 1)[-1] for p in calls] == ["status", "dig", "stop"]


def test_level_tunnel_deadline_covers_each_probe_and_skips_final_io(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining
    from mc_agent.skills.tunnel import dig_level_tunnel

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=21)
    calls: list[float] = []

    def slow_handler(request):
        calls.append(clock.now)
        clock.now += 0.2
        return world.handler(request)

    with BotSession("tok", bot_username="TimedTunnel", transport=httpx.MockTransport(slow_handler)) as bot:
        result = dig_level_tunnel(bot, "east", 8, deadline=1001.0)
        before_expired_call = len(calls)
        expired = dig_level_tunnel(bot, "east", 8, deadline=999.0)
    assert result["ok"] is False and result["requires_replan"] is True
    assert result["stop_reason"] == expired["stop_reason"] == "time_slice"
    assert clock.now <= 1001.01
    assert all(t < 1001.0 for t in calls)
    assert not world.dig_log
    assert len(calls) == before_expired_call


def test_escape_primitive_passes_shared_deadline_to_new_skill_contract(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    received: list[float] = []
    with BotSession("tok", bot_username="DeadlineContract", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs

        def stairs(*, deadline, **kwargs):
            received.append(deadline)
            assert kwargs["steps"] == 1
            return {"ok": False}

        monkeypatch.setattr(eng.skills, "dig_staircase", stairs)
        eng._escape_primitive("dig_staircase", deadline=1234.0, steps=1, direction="north", vertical="up")
    assert received == [1234.0]


def test_partial_slice_success_reports_goal_unmet_without_claiming_ore(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="PartialGoal", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs

        def partial_edge(**kwargs):
            return {"steps_done": 0, "stop_reason": "time_slice", "breaches_sealed": 0,
                    "catch_placed": 0, "deferred_added": 0, "mined_ores": [],
                    "waypoints": [(0, 20, 0)], "end_anchor": [0, 20, 0]}

        monkeypatch.setattr(eng, "_dig_stair_down_edge", partial_edge)
        result = eng.run(target="iron", goal_count=4, target_y=16, max_duration_s=5.0)
    assert result["ok"] is True
    assert result["stop_reason"] == "time_slice_progress"
    assert result["goal_met"] is False
    assert result["target_gained"] == 0


def test_no_progress_hint_does_not_recommend_a_redundant_reset(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    monkeypatch.setattr(mining, "MAX_IDLE_ITERATIONS", 0)
    with BotSession("tok", bot_username="IdleHint", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        result = eng.run(target="iron", goal_count=4, target_y=16, max_duration_s=5.0)
    assert result["stop_reason"] == "no_progress"
    assert "reset" not in result["message"]
    assert "空间树为空时直接 explore" in result["message"]
    assert result["goal_met"] is False


def test_collar_at_nether_target_layer_can_mine_over_bedrock() -> None:
    world = _MockMineWorld(start_pos=(0.5, 5.0, 0.5), surface_y=5, dimension="the_nether")
    for x in range(-9, 10):
        for z in range(-9, 10):
            world.blocks[(x, 4, z)] = "bedrock"
    world.blocks[(1, 5, 0)] = "nether_quartz_ore"
    with BotSession("tok", bot_username="BedrockFloor", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="quartz", target_y=5, goal_count=1, max_duration_s=5.0)
    assert result["goal_met"] is True
    assert world._count("nether_quartz_ore") == 1
    assert all(name != "bedrock" for _, name, _ in world.dig_log)


def test_collar_reports_when_solid_station_is_outside_geofence() -> None:
    world = _MockMineWorld(start_pos=(0.5, 70.0, 0.5), surface_y=70)
    fence = VoidGeofence(center_x=100.0, center_z=100.0, radius=15.0, margin=5.0)
    with BotSession("tok", bot_username="FenceAtCollar", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="iron", geofence=fence, max_duration_s=5.0)
    assert result["stop_reason"] == "void_geofence_blocked"
    assert result["error"]["code"] == "void_geofence_blocked"
    assert result["tree"]["node_count"] == 0
    assert not world.dig_log


def test_explore_continues_after_successful_automatic_climb(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=64)
    with BotSession("tok", bot_username="AutoClimb", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        eng.collar_ceiling_y = 64

        def climb(*, deadline):
            world.pos[1] = 64.0
            return {"reached": True}

        monkeypatch.setattr(eng, "_climb_out", climb)
        result = eng.run(target="iron", target_y=60, max_duration_s=10.0)
    assert result["stop_reason"] != "needs_surface_climb"
    assert result["tree"]["collar_node_id"] is not None
    assert world.dig_log


def test_explore_does_not_open_a_fake_collar_after_a_disconnected_move(monkeypatch) -> None:
    from mc_agent.skills.mining_dfs import BranchSpec

    world = _MockMineWorld(start_pos=(60.5, 20.0, 0.5), surface_y=20)
    for dx, dz in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        world.blocks[(60 + dx, 19, dz)] = "bedrock"
    with BotSession("tok", bot_username="BlockedCollar", transport=httpx.MockTransport(world.handler)) as bot:
        eng = MacroSkills(bot).mining_dfs
        old_collar = eng._create_node(
            kind="collar", anchor=(0, 20, 0), parent_id=None, edge_from_parent=None,
            branches=[BranchSpec(kind="stair_down", direction="east", steps=4)],
        )
        eng.collar_node_id = old_collar.node_id
        monkeypatch.setattr(eng, "_find_solid_collar_candidates", lambda *args, **kwargs: [])
        result = eng.run(target="iron", target_y=16, max_duration_s=5.0)
    assert result["stop_reason"] == "no_viable_collar_direction"
    assert "另一处实心站位" in result["message"]
    assert result["tree"]["node_count"] == 0
    assert not world.dig_log


def test_timed_slice_with_confirmed_dig_remains_retryable_at_same_cell(monkeypatch) -> None:
    from mc_agent.harness.agent import _RepeatFailureGuard
    from mc_agent.harness.tool_registry import Tool
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="RetryableSlice", transport=httpx.MockTransport(world.handler)) as bot:
        toolset = McToolset(bot)
        toolset.update_block_pos([0, 20, 0])
        engine = toolset.skills.mining_dfs
        attempts = 0

        def cut_off_after_dig(*, deadline, **kwargs):
            nonlocal attempts
            attempts += 1
            engine.bot.status()
            engine.bot.call("dig", {"pos": [attempts, 19, 0], "collect": False})
            clock.now = deadline
            engine.bot.status()

        monkeypatch.setattr(engine, "_run", cut_off_after_dig)
        tool = _RepeatFailureGuard(toolset).wrap_tool(Tool(
            name="mc_skill_mine_dfs", description="Mine", parameters={},
            handler=toolset.mc_skill_mine_dfs,
        ))
        first = json.loads(tool.handler(target="iron", max_duration_s=20))
        second = json.loads(tool.handler(target="iron", max_duration_s=20))

    assert attempts == 2
    for result in (first, second):
        assert result["stop_reason"] == "time_slice_progress"
        assert result["dug_count"] == 1
        assert result["pos_before"] == result["pos_after"]
        assert result["gained"] == {}
        assert result["goal_met"] is False


def test_timed_slice_credits_only_observed_target_items(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    with BotSession("tok", bot_username="VerifiedSliceItems", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def cut_off_after_inventory(*, deadline, **kwargs):
            engine.bot.inventory()
            world._add_item("raw_iron")
            engine.bot.inventory()
            world._add_item("raw_iron")
            clock.now = deadline
            engine.bot.inventory()

        monkeypatch.setattr(engine, "_run", cut_off_after_inventory)
        result = engine.run(target="iron", goal_count=2, max_duration_s=5.0)

    assert world._count("raw_iron") == 2
    assert result["target_gained"] == 1
    assert result["gained"] == {"raw_iron": 1}
    assert result["goal_met"] is False


@pytest.mark.parametrize(("dimension", "expected_dimension", "available"), [
    ("minecraft:the_nether", "the_nether", {"ancient_debris": 15, "quartz": 48, "gold": 64}),
    ("world_the_end", "the_end", {}),
])
def test_dfs_rejects_diamond_outside_overworld_before_scanning_or_digging(
    dimension, expected_dimension, available,
) -> None:
    world = _MockMineWorld(start_pos=(0.5, 52.0, 0.5), surface_y=64, dimension=dimension)
    endpoints = []

    def handler(request):
        endpoints.append(request.url.path.rsplit("/", 1)[-1])
        return world.handler(request)

    with BotSession("tok", bot_username="BadDimensionOre", transport=httpx.MockTransport(handler)) as bot:
        skills = MacroSkills(bot)
        result = skills.mine_dfs(target="diamond", target_y=15, max_duration_s=60)
        assert skills.mining_dfs.target_y == 16

    assert result["stop_reason"] == "bad_target_for_dimension"
    assert result["ok"] is False and result["requires_replan"] is True
    assert result["dimension"] == expected_dimension
    assert result["allowed_dimensions"] == ["overworld"]
    assert result["available_targets"] == available
    assert result["tree"]["node_count"] == 0
    assert endpoints == ["status"]


def test_generic_gold_in_nether_counts_nuggets_and_uses_the_nether_layer(monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 64.0, 0.5), surface_y=64, dimension="the_nether")
    world.blocks[(1, 64, 0)] = "nether_gold_ore"
    monkeypatch.setattr(world, "_drop_name_for", lambda name: "gold_nugget" if name == "nether_gold_ore" else name)
    with BotSession("tok", bot_username="NetherGold", transport=httpx.MockTransport(world.handler)) as bot:
        result = MacroSkills(bot).mine_dfs(target="gold", goal_count=1, max_duration_s=10.0)

    assert result["stop_reason"] == "quota_reached"
    assert result["target_y"] == 64
    assert result["goal_met"] is True
    assert result["gained"]["gold_nugget"] == 1
    assert any(name == "nether_gold_ore" for _, name, _ in world.dig_log)


def test_pathfinder_expiry_leaves_time_for_local_movement(monkeypatch) -> None:
    import mc_agent.skills.mining_dfs as mining

    clock = _MiningClock()
    monkeypatch.setattr(mining.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mining.time, "sleep", clock.sleep)
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)
    sent = []

    def handler(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "move_to":
            server_timeout = json.loads(request.content)["timeout"] / 1000
            read_timeout = request.extensions["timeout"]["read"]
            sent.append((server_timeout, read_timeout))
            if server_timeout + 0.1 > read_timeout:
                clock.now += read_timeout
                raise httpx.ReadTimeout("pathfinder still running", request=request)
            clock.now += server_timeout + 0.1
            return httpx.Response(200, json={"ok": False, "error": {
                "code": "timeout", "message": "pathfinder reached its own timeout",
            }})
        if endpoint == "control":
            world.pos = [1.5, 20.0, 0.5]
        return world.handler(request)

    with BotSession("tok", bot_username="TimedMovementFallback", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs

        def take_step(**kwargs):
            return {"ok": engine._move_exact((1, 20, 0))}

        monkeypatch.setattr(engine, "_run", take_step)
        result = engine.run(max_duration_s=20.0)

    assert len(sent) == 1
    server_timeout, read_timeout = sent[0]
    assert 0 < server_timeout < read_timeout
    assert result["ok"] is True
    assert tuple(world.pos) == (1.5, 20.0, 0.5)
    assert clock.now < 1020.0


@pytest.mark.parametrize(("landing_y", "on_ground"), [(20.0, True), (21.0, False)])
def test_step_requires_settled_arrival_on_requested_layer(landing_y, on_ground, monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(0.5, 20.0, 0.5), surface_y=20)

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "control":
            world.pos = [1.5, landing_y, 0.5]
        response = world.handler(request)
        if path == "status" and world.pos[0] == 1.5:
            body = response.json()
            body["result"]["on_ground"] = on_ground
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr("mc_agent.skills.mining_dfs.time.sleep", lambda _: None)
    with BotSession("tok", bot_username="WrongLevelStep", transport=httpx.MockTransport(handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        monkeypatch.setattr(engine, "_move_exact", lambda cell: False)
        monkeypatch.setattr(engine, "_hop_up_to", lambda cell: False)
        assert engine._step_to((1, 21, 0)) is False

    assert world.pos[0] == 1.5


@pytest.mark.parametrize(("goal_x", "expected_xs"), [(3, [5, 4, 3]), (8, [5, 6, 7, 8])])
def test_navigation_resumes_mid_edge_toward_the_requested_node(goal_x, expected_xs, monkeypatch) -> None:
    world = _MockMineWorld(start_pos=(5.5, 15.0, 0.5), surface_y=20)
    with BotSession("tok", bot_username="ResumeTowardGoal", transport=httpx.MockTransport(world.handler)) as bot:
        engine = MacroSkills(bot).mining_dfs
        collar = engine._create_node(
            kind="collar", anchor=(0, 20, 0), parent_id=None, edge_from_parent=None, branches=[],
        )
        engine.collar_node_id = collar.node_id
        upper = engine._create_node(
            kind="switchback", anchor=(3, 17, 0), parent_id=collar.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [(i, 20 - i, 0) for i in range(4)]), branches=[],
        )
        lower = engine._create_node(
            kind="switchback", anchor=(8, 12, 0), parent_id=upper.node_id,
            edge_from_parent=ShaftEdge("stair_down", "east", [(i, 20 - i, 0) for i in range(3, 9)]), branches=[],
        )
        traversed = []

        def walk(waypoints, **kwargs):
            traversed.extend(pt[0] for pt in waypoints)
            x, y, z = waypoints[-1]
            world.pos = [x + 0.5, float(y), z + 0.5]
            return True, None

        def reject_shortcut(*args, **kwargs):
            pytest.fail("the bot must resume its recorded edge")

        monkeypatch.setattr(engine, "_walk_waypoints", walk)
        monkeypatch.setattr(engine, "_move_exact", reject_shortcut)
        goal = upper if goal_x == 3 else lower
        assert engine._navigate_to_node(goal.node_id, min_hp_ref=[20.0]) == (True, None)
        assert engine.active_node_id == goal.node_id

    assert traversed == expected_xs
