"""Offline tests for single-bot WorldMemory: 5 layers, 0 MiningDFS ore coupling, 110m segmented recall, lazy pop/cooldown, home/chest tracking, biomes/habitats, and HUD budget."""

from __future__ import annotations

import json
import math
from pathlib import Path
import stat
from typing import Any

import httpx

from mc_agent.bot import BotSession
from mc_agent.harness.hud import build_world_hud
from mc_agent.harness.mc_tools import McToolset
from mc_agent.harness.world_memory import WorldMemory


class _MockAtlasWorld:
    """3D world simulator with 40m single-hop move_to limit (testing 24m relay), anti-xray blocks, and chest windows."""

    def __init__(
        self,
        *,
        start_pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        biome: str = "plains",
        max_single_move_dist: float = 40.0,
        inventory_items: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pos = list(start_pos)
        self.dimension = "overworld"
        self.biome = biome
        self.max_single_move_dist = max_single_move_dist
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.blocked_xz_zones: list[tuple[float, float, float]] = []  # (cx, cz, radius) -> path_not_found
        self.entities: list[dict[str, Any]] = []
        self.chest_slots: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
        self.items: list[dict[str, Any]] = [dict(r) for r in (inventory_items or [])]
        self.held: dict[str, Any] | None = dict(self.items[0]) if self.items else None
        self.move_calls: list[dict[str, Any]] = []

    def block_at(self, pt: tuple[int, int, int]) -> str:
        if pt in self.blocks:
            return self.blocks[pt]
        return "air" if pt[1] >= 64 else "stone"

    def _add_item(self, name: str, count: int = 1) -> None:
        for row in self.items:
            if row["name"] == name and "durability" not in row:
                row["count"] = int(row["count"]) + count
                return
        self.items.append({"name": name, "count": count, "slot": 9 + len(self.items)})

    def _consume_item(self, name: str, count: int = 1) -> bool:
        total = sum(int(r.get("count", 0)) for r in self.items if r.get("name") == name)
        if total < count:
            return False
        rem = count
        kept = []
        for r in self.items:
            if r.get("name") == name and rem > 0:
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
                        "biome": self.biome,
                        "is_day": True,
                        "health": 20.0,
                        "food": 20,
                        "on_ground": True,
                        "in_water": False,
                        "in_lava": False,
                        "alive": True,
                        "ready": True,
                        "held": dict(self.held) if self.held else None,
                    },
                },
            )
        if path == "/inventory":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "items": [dict(r) for r in self.items],
                        "armor": {},
                        "offhand": None,
                        "held": dict(self.held) if self.held else None,
                        "empty_slots": max(0, 36 - len(self.items)),
                    },
                },
            )
        if path == "/get_block":
            pt = tuple(int(v) for v in body["pos"])
            bn = self.block_at(pt)
            return httpx.Response(200, json={"ok": True, "result": {"name": bn, "pos": list(pt), "diggable": bn != "bedrock"}})
        if path == "/find_blocks":
            rad = int(body.get("radius", 16))
            types = set(body.get("types") or [])
            found = []
            for pt, bn in self.blocks.items():
                if bn == "air":
                    continue
                if types and bn not in types:
                    continue
                d = math.dist(self.pos, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5))
                if d <= rad + 0.5:
                    found.append({"name": bn, "pos": list(pt), "dist": round(d, 2)})
            found.sort(key=lambda r: r["dist"])
            return httpx.Response(200, json={"ok": True, "result": {"blocks": found[: int(body.get("limit", 20))]}})
        if path == "/get_entities":
            rad = float(body.get("radius", 32))
            req_t = set(body.get("types") or [])
            ents = []
            for e in self.entities:
                d = math.dist(self.pos, e["pos"])
                if d > rad:
                    continue
                if req_t and e.get("name") not in req_t and e.get("type") not in req_t:
                    continue
                ents.append({**e, "dist": round(d, 2)})
            return httpx.Response(200, json={"ok": True, "result": {"entities": ents}})
        if path == "/equip":
            it = body["item"]
            found_r = next((r for r in self.items if r["name"] == it), None)
            if not found_r:
                return httpx.Response(200, json={"ok": False, "error": {"code": "missing_item", "message": it}})
            self.held = dict(found_r)
            return httpx.Response(200, json={"ok": True, "result": {"held": dict(self.held)}})
        if path == "/move_to":
            self.move_calls.append(dict(body))
            tx = float(body["x"])
            tz = float(body["z"])
            ty = float(body.get("y", 64.0))
            hdist = math.hypot(tx - self.pos[0], tz - self.pos[2])
            if hdist > self.max_single_move_dist:
                return httpx.Response(
                    200,
                    json={"ok": False, "error": {"code": "path_not_found", "message": "chunk beyond single-hop limit"}},
                )
            for zx, zz, zr in self.blocked_xz_zones:
                if math.hypot(tx - zx, tz - zz) <= zr:
                    return httpx.Response(
                        200,
                        json={"ok": False, "error": {"code": "path_not_found", "message": "walled off trap"}},
                    )
            self.pos = [tx, ty, tz]
            return httpx.Response(200, json={"ok": True, "result": {"pos": list(self.pos)}})
        if path == "/dig":
            pt = tuple(int(v) for v in body["pos"])
            bn = self.block_at(pt)
            if bn == "air":
                return httpx.Response(200, json={"ok": False, "error": {"code": "nothing_to_dig", "message": "air"}})
            for zx, zz, zr in self.blocked_xz_zones:
                if math.hypot(pt[0] + 0.5 - zx, pt[2] + 0.5 - zz) <= zr:
                    return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "sealed"}})
            if math.dist(self.pos, (pt[0] + 0.5, pt[1] + 0.5, pt[2] + 0.5)) > 6.0:
                return httpx.Response(200, json={"ok": False, "error": {"code": "out_of_reach", "message": "too far"}})
            self.blocks[pt] = "air"
            if body.get("collect", True):
                self._add_item(bn, 1)
            return httpx.Response(200, json={"ok": True, "result": {"dug": bn, "gained": {bn: 1}}})
        if path == "/place":
            item = str(body["item"])
            if "pos" in body:
                pt = tuple(int(v) for v in body["pos"])
            else:
                pt = (math.floor(self.pos[0]) + 1, math.floor(self.pos[1]), math.floor(self.pos[2]))
            if not self._consume_item(item, 1):
                return httpx.Response(200, json={"ok": False, "error": {"code": "missing_item", "message": item}})
            self.blocks[pt] = item
            return httpx.Response(200, json={"ok": True, "result": {"placed": item, "pos": list(pt)}})
        if path == "/interact":
            return httpx.Response(200, json={"ok": True, "result": {"interacted": True}})
        if path in ("/window/view", "/window/act"):
            t = body.get("target") or {}
            tpos = t.get("pos") or [0, 64, 0]
            pt = (int(tpos[0]), int(tpos[1]), int(tpos[2]))
            slots = self.chest_slots.get(pt, [])
            container_only = [
                {"name": s["name"], "count": s["count"]}
                for s in slots
                if int(s.get("slot", 0)) < 27
            ]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "window": "generic_9x3",
                        "target": {"kind": "block", "name": "chest", "pos": list(pt)},
                        "sections": {"container": container_only},
                    },
                },
            )
        if path == "/events":
            return httpx.Response(200, json={"ok": True, "result": {"events": [], "last_seq": 0}})
        raise AssertionError(f"Unhandled mock endpoint: {path}")


def test_zero_mining_dfs_coupling_and_hud_budget_cap() -> None:
    """Underground `*_ore` blocks NEVER pollute WorldMemory; 500 POIs stay within <=84 char HUD line & <650 total HUD."""
    wm = WorldMemory()
    # Feed 60 underground ores -> 0 entries recorded!
    ore_blocks = [
        {"name": "iron_ore", "pos": [i, 16, 0]}
        for i in range(20)
    ] + [
        {"name": "deepslate_diamond_ore", "pos": [i, -54, 0]}
        for i in range(20)
    ] + [
        {"name": "ancient_debris", "pos": [i, 15, 0]}
        for i in range(20)
    ]
    assert wm.observe_blocks(ore_blocks, dim="overworld") == 0
    assert len(wm.pois) == 0

    # Contiguous 10x10 lava lake deduplicates spatially into just a handful of cluster representatives
    lava_lake = [{"name": "lava", "pos": [50 + dx, 63, 50 + dz]} for dx in range(10) for dz in range(10)]
    wm.observe_blocks(lava_lake, dim="overworld")
    assert 1 <= len(wm.pois) <= 4

    # Flood 500 diverse POIs across the map and verify HUD line stays strictly <= 84 chars and total HUD < 650 chars
    for idx in range(500):
        kind = ("gold_block", "obsidian", "sugar_cane", "hay_block", "lectern", "furnace", "pumpkin")[idx % 7]
        wm.record_poi(kind, (100 + idx * 5, 64, -80 - idx * 3), dim="overworld")

    world = _MockAtlasWorld(
        start_pos=(0.5, 64.0, 0.5),
        inventory_items=[{"name": "iron_pickaxe", "count": 1, "slot": 9, "durability": [250, 250]}],
    )
    # Put water within 16m so local_visible_names deduplication skips water in `记忆库:`
    world.blocks[(4, 63, 4)] = "water"

    with BotSession("tok", bot_username="HudBot", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot, world_memory=wm)
        hud_text = build_world_hud(ts, task="收集金块与黑曜石")

    mem_lines = [ln for ln in hud_text.splitlines() if ln.startswith("记忆库:")]
    assert len(mem_lines) == 1
    assert len(mem_lines[0]) <= 84
    assert "gold_block(" in mem_lines[0]
    assert "water(" not in mem_lines[0]  # deduplicated because water is already in `16m暴露:`
    assert len(hud_text) < 650


def test_110m_gold_block_recall_stale_pop_blocked_cooldown_and_segmented_relay() -> None:
    """Killer Benchmark #1: spot distant gold_blocks barehanded -> upgrade iron_pickaxe -> pop stale A, cool trap B, relay 110m to C!"""
    world = _MockAtlasWorld(
        start_pos=(0.5, 64.0, 0.5),
        max_single_move_dist=38.0,  # Force >38m journeys to use 24m segmented relay!
        inventory_items=[],  # Barehanded initially
    )
    # Point A (24m east): will be removed before recall (stale/dug out)
    world.blocks[(24, 64, 0)] = "gold_block"
    # Point B (52m south): sealed inside an unreachable trap zone
    world.blocks[(0, 64, 52)] = "gold_block"
    world.blocked_xz_zones.append((0.5, 52.5, 6.0))
    # Point C (110m east, beyond 64m find_blocks max radius!): real ruined portal gold_block
    world.blocks[(110, 64, 0)] = "gold_block"

    with BotSession("tok", bot_username="GoldHunter", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot)

        # Phase 1: Barehanded scouting pass observes A, B, and C
        world.pos = [20.5, 64.0, 0.5]
        ts.mc_find_blocks(radius=16)  # sees A
        world.pos = [0.5, 64.0, 40.5]
        ts.mc_find_blocks(radius=20)  # sees B
        world.pos = [98.5, 64.0, 0.5]
        ts.mc_find_blocks(radius=20)  # sees C

        # Return to main shaft at (0, 64, 0), acquire iron_pickaxe, while Point A disappears!
        world.pos = [0.5, 64.0, 0.5]
        world.blocks[(24, 64, 0)] = "air"
        world._add_item("iron_pickaxe", 1)
        world.items[-1]["durability"] = [250, 250]

        # 1) Calling mc_find_blocks(["gold_block"], radius=32) at origin:
        #    Local scan is empty; Point A (24m <= 32m) is lazily verified via get_block -> popped!
        #    Returns memory_blocks pointing to B (52m) and C (110m)!
        fb_res = json.loads(ts.mc_find_blocks(radius=32, types=["gold_block"]))
        assert fb_res["ok"] is True
        assert fb_res["result"]["blocks"] == []
        mem_blks = fb_res["result"]["memory_blocks"]
        assert [m["pos"] for m in mem_blks] == [[0, 64, 52], [110, 64, 0]]
        assert ("overworld", 24, 64, 0) not in ts.world_memory.pois

        # 2) Calling mc_skill_mine_blocks(["gold_block"], count=1):
        #    Tries nearest B (0, 64, 52) -> hits blocked_xz_zones -> cools B for 300s ->
        #    Automatically falls back to C (110, 64, 0), relays in ~24m hops across 110m, mines gold_block!
        mine_res = json.loads(ts.mc_skill_mine_blocks(["gold_block"], count=1, radius=32))
        assert mine_res["ok"] is True
        assert mine_res["target_gained"].get("gold_block", 0) >= 1
        assert mine_res["recalled_from_memory"]["pos"] == [110, 64, 0]
        assert any(att["status"] == "cooled_unreachable" and att["pos"] == [0, 64, 52] for att in mine_res["memory_attempts"])
        assert sum(int(r["count"]) for r in world.items if r["name"] == "gold_block") == 1
        # Point C was removed after mining; Point B is on cooldown so active query is now empty
        assert ts.world_memory.query_pois(["gold_block"], origin=world.pos, dim="overworld") == []

        # 3) Also verify mc_move_to(0.5, z=0.5) from x=110.5 relays back 110m seamlessly despite 38m single-hop cap!
        mv_back = json.loads(ts.mc_move_to(x=0.5, z=0.5))
        assert mv_back["ok"] is True
        assert math.hypot(world.pos[0] - 0.5, world.pos[2] - 0.5) <= 4.0


def test_home_auto_promotion_storage_chest_snapshot_and_shortage_hints() -> None:
    """Placing table + furnace + chest promotes Home Base; window/view snapshots chest items & feeds shortage hints."""
    world = _MockAtlasWorld(
        start_pos=(10.5, 64.0, -5.5),
        inventory_items=[
            {"name": "crafting_table", "count": 1, "slot": 9},
            {"name": "furnace", "count": 1, "slot": 10},
            {"name": "chest", "count": 1, "slot": 11},
            {"name": "stick", "count": 4, "slot": 12},
        ],
    )
    world.chest_slots[(12, 64, -5)] = [
        {"slot": 0, "name": "iron_ingot", "count": 8},
        {"slot": 1, "name": "bread", "count": 5},
        {"slot": 30, "name": "cobblestone", "count": 64},  # player inventory slot >= 27, must NOT be counted as chest!
    ]

    with BotSession("tok", bot_username="Homesteader", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot)
        ts.mc_status()
        assert ts.world_memory.spawn_points["overworld"] == [10, 64, -6]

        assert json.loads(ts.mc_place("crafting_table", 10, 64, -5))["ok"] is True
        assert json.loads(ts.mc_place("furnace", 11, 64, -5))["ok"] is True
        assert "overworld" not in ts.world_memory.home_bases  # only 2 of 3 pieces placed so far
        assert json.loads(ts.mc_place("chest", 12, 64, -5))["ok"] is True
        # Home Base auto-promoted!
        assert ts.world_memory.home_bases["overworld"] == [10, 64, -5]

        # Open chest via mc_window_view -> snapshots chest slots [0..26]
        wv = json.loads(ts.mc_window_view(block="chest", x=12, y=64, z=-5))
        assert wv["ok"] is True
        stored = ts.world_memory.containers[("overworld", 12, 64, -5)]["items"]
        assert stored == {"iron_ingot": 8, "bread": 5}

        # Also record a distant gold_block at (90, 64, -5)
        ts.world_memory.record_poi("gold_block", (90, 64, -5), dim="overworld")

        # Walk 30m away -> HUD shows Home + chest inventory summary; craft_gear("iron_pickaxe") hints stored iron_ingot!
        world.pos = [40.5, 64.0, -5.5]
        hud_away = build_world_hud(ts, task="整备装备")
        assert "家@[10, 64, -5]" in hud_away
        assert "iron_ingot:8" in hud_away

        short_iron = json.loads(ts.mc_skill_craft_gear("iron_pickaxe", count=1))
        assert short_iron["ok"] is False
        assert "iron_ingot×8" in short_iron.get("memory_hint", "")

        # Upstream POI hint: needing gold_ingot points to remembered gold_block!
        gold_tip = ts.world_memory.shortage_memory_hint(["gold_ingot"], origin=world.pos, dim="overworld")
        assert "gold_block@[90, 64, -5]" in gold_tip


def test_biomes_habitats_unexplored_compass_and_disk_persistence(tmp_path: Path) -> None:
    """Biomes, villager/sheep habitats, unexplored chunk headings, and 0600 disk reload all work cleanly."""
    mem_file = tmp_path / "world_memory_Scout.json"
    world = _MockAtlasWorld(start_pos=(0.5, 64.0, 0.5), biome="plains")
    world.entities = [
        {"id": 11, "type": "other", "name": "villager", "pos": [4.5, 64.0, 2.5]},
        {"id": 12, "type": "animal", "name": "sheep", "pos": [6.5, 64.0, -2.5]},
    ]

    with BotSession("tok", bot_username="Scout", transport=httpx.MockTransport(world.handler)) as bot:
        wm = WorldMemory(mem_file)
        ts = McToolset(bot, world_memory=wm)
        ts.mc_status()
        ts.mc_get_entities(radius=16)

        # Travel north across 3 chunks (-32, -64, -96) into a desert biome where entities are empty
        world.entities = []
        for z_step, b_name in ((-32.5, "plains"), (-64.5, "desert"), (-96.5, "desert")):
            world.pos = [0.5, 64.0, z_step]
            world.biome = b_name
            ts.mc_status()
        wm.save(force=True)

        # North chunks (-32, -64, -96 from z=0, or from z=-96 looking south vs east/west/north):
        # Move back to origin (0.5, 64.0, 0.5) where north (-Z) has already been visited while east/south/west are unvisited!
        world.pos = [0.5, 64.0, 0.5]
        world.biome = "plains"
        unexp = wm.unexplored_directions(world.pos, dim="overworld")
        assert set(unexp[:3]) == {"east", "south", "west"}
        assert unexp[-1] == "north"  # north already has 3 visited chunks, so it ranks last behind fully unvisited E/S/W!

        # Querying villagers at (0, 64, -96) when none are within 32m returns `memory_habitats`!
        world.pos = [0.5, 64.0, -96.5]
        world.biome = "desert"
        ts.mc_status()
        ent_res = json.loads(ts.mc_get_entities(radius=32, types=["villager"]))
        assert ent_res["result"]["entities"] == []
        assert ent_res["result"]["memory_habitats"][0]["name"] == "villager"

        # HUD in desert displays off-screen villager/sheep habitat and previously discovered plains biome
        hud_desert = build_world_hud(ts, task="寻找村民与羊")
        assert "villager(" in hud_desert
        assert "群系:plains(" in hud_desert

    # Verify disk file permissions (0600) and full reload in a brand-new WorldMemory instance
    assert mem_file.is_file()
    assert stat.S_IMODE(mem_file.stat().st_mode) == 0o600
    reloaded = WorldMemory(mem_file)
    assert reloaded.spawn_points["overworld"] == [0, 64, 0]
    assert ("overworld", "desert") in reloaded.biomes
    assert ("overworld", "plains") in reloaded.biomes
    assert len(reloaded.visited_chunks) >= 4
    assert ("overworld", "villager") in reloaded.habitats
    assert ("overworld", "sheep") in reloaded.habitats


def test_stale_cross_session_last_death_ignored_and_explore_hud_has_no_mine_nag() -> None:
    """Fresh WorldMemory ignores pre-existing status.last_death on first alive poll, and surface roam HUD suppresses mining nags."""
    wm = WorldMemory()
    # First alive status carries a stale death from hours ago -> must NOT pollute fresh WorldMemory
    wm.observe_status(
        {
            "pos": [-160.2, 69.0, -390.4],
            "dimension": "overworld",
            "alive": True,
            "last_death": {"pos": [1995.0, 150.0, 1995.0], "seq": 42},
        }
    )
    assert wm.last_death is None

    # Subsequent genuine new death in this session (new seq / pos) IS recorded!
    wm.observe_status(
        {
            "pos": [-160.0, 69.0, -390.0],
            "dimension": "overworld",
            "alive": True,
            "last_death": {"pos": [120.4, 62.0, -88.2], "seq": 43},
        }
    )
    assert wm.last_death is not None
    assert wm.last_death["pos"] == [120, 62, -89]

    # Surface explore task without pickaxe should NOT nag about crafting pickaxes or staircase down
    world = _MockAtlasWorld(
        start_pos=(0.5, 68.0, 0.5),
        biome="plains",
        inventory_items=[
            {"name": "oak_planks", "count": 16, "slot": 9},
            {"name": "cobblestone", "count": 16, "slot": 10},
        ],
    )
    with BotSession("tok", bot_username="Roamer", transport=httpx.MockTransport(world.handler)) as bot:
        ts = McToolset(bot, world_memory=wm)
        # Exact wording from live run 20261118-124221 ("不要长时间原地挖矿...观察裸露矿脉") must STILL be recognized as surface explore!
        hud = build_world_hud(
            ts,
            task="在接下来90分钟自由探索当前世界。优先沿地表向远处走，不要长时间原地挖矿或下潜。沿途观察裸露矿脉与村庄。",
        )
        assert "阶梯下探" not in hud
        assert "严禁调mc_skill_mine_dfs下井" in hud

        # Segmented relay triggered from mc_move_to skips redundant 2nd direct move_to and passes bounded timeout!
        world.blocked_xz_zones.append((50.0, 0.0, 200.0))  # everything east is path_not_found
        world.move_calls.clear()
        fail_mv = json.loads(ts.mc_move_to(x=80.0, z=0.5))
        assert fail_mv["ok"] is False
        # 1 initial direct call + 3 angled 24m hop probes = 4 calls total (no duplicate 2nd full direct call!), hops carry timeout=12000
        assert len(world.move_calls) == 4
        assert all(c.get("timeout") == 12000 for c in world.move_calls[1:])

