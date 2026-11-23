#!/usr/bin/env python3
"""Live WorldMemory regression on nju-beast using isolated bot MapTest_01.

Covers anchors, visited chunks, POI recall/stale pop, >64m segmented travel,
home base + container snapshot + shortage hints, habitats, HUD budget,
disk persistence, dimension separation, portal anchor and last_death.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BOT = "MapTest_01"
STATE_DIR = ROOT / ".state-maptest"
MEM_PATH = STATE_DIR / f"world_memory_{BOT}.json"
BASE = (1995, 150, 1995)  # bot standing point
CORRIDOR = {"x0": 1990, "x1": 2130, "z0": 1990, "z1": 1998, "floor_y": 149}
POS = {
    "near_gold": (2019, 150, 1994),
    "far_gold": (2105, 150, 1994),
    "table": (1996, 150, 1992),
    "furnace": (1997, 150, 1992),
    "chest": (1998, 150, 1992),
    "bed": (1999, 150, 1992),
    "sheep": (2005, 150, 1990),
    "villager": (2010, 150, 1998),
    "portal": (2001, 151, 1996),
}


def rcon_batch(commands: list[str], *, timeout: int = 60) -> list[dict[str, str]]:
    payload = json.dumps(commands, ensure_ascii=False)
    script = (
        "import json, pathlib, sys\n"
        "sys.path.insert(0, '/home/xia/mc-beast/mc/live_viewer')\n"
        "from beast_ctl import _run_rcon_commands\n"
        f"cmds = json.loads({payload!r})\n"
        "res = _run_rcon_commands(pathlib.Path('/home/xia/mc-beast/mc'), pathlib.Path('/home/xia/mc-beast/.env.beast'), '127.0.0.1', 25575, cmds)\n"
        "print(json.dumps(res, ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [
            "ssh", "-T", "-F", "/home/xzq/.ssh/config", "nju-beast",
            "wsl", "-d", "Ubuntu-22.04", "-u", "xia", "--", "python3", "-",
        ],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"RCON SSH failed: {proc.stderr.strip()[-600:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def setup_world() -> list[dict[str, str]]:
    x0, x1, z0, z1, fy = CORRIDOR["x0"], CORRIDOR["x1"], CORRIDOR["z0"], CORRIDOR["z1"], CORRIDOR["floor_y"]
    cmds = [
        f"forceload add {x0 - 2} {z0 - 2} {x1 + 2} {z1 + 4}",
        f"fill {x0} {fy} {z0} {x1} {fy} {z1} minecraft:stone",
        f"fill {x0} {fy + 1} {z0} {x1} {fy + 5} {z1} minecraft:air",
        "time set day",
        "weather clear",
        f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]}",
        f"gamemode survival {BOT}",
        f"clear {BOT}",
        f"effect clear {BOT}",
        f"xp set {BOT} 30 levels",
        f"effect give {BOT} minecraft:resistance 1800 4 true",
        f"effect give {BOT} minecraft:regeneration 1800 2 true",
        f"effect give {BOT} minecraft:saturation 30 20 true",
        f"give {BOT} minecraft:iron_pickaxe 1",
        f"give {BOT} minecraft:white_bed 1",
    ]
    # POIs
    for name, pos in (("near_gold", POS["near_gold"]), ("far_gold", POS["far_gold"])):
        cmds.append(f"setblock {pos[0]} {pos[1]} {pos[2]} minecraft:gold_block replace")
    # Home base cluster
    for name, block in (("table", "crafting_table"), ("furnace", "furnace"), ("chest", "chest")):
        pos = POS[name]
        cmds.append(f"setblock {pos[0]} {pos[1]} {pos[2]} minecraft:{block} replace")
    # Chest contents (empty before item replace)
    chest = POS["chest"]
    cmds.append(f"item replace block {chest[0]} {chest[1]} {chest[2]} container.0 with minecraft:iron_ingot 8")
    cmds.append(f"item replace block {chest[0]} {chest[1]} {chest[2]} container.1 with minecraft:bread 5")
    # Bed (foot + head)
    bed = POS["bed"]
    cmds.append(f"setblock {bed[0]} {bed[1]} {bed[2]} minecraft:white_bed[part=foot,facing=north] replace")
    cmds.append(f"setblock {bed[0]} {bed[1]} {bed[2] - 1} minecraft:white_bed[part=head,facing=north] replace")
    # Habitats
    cmds.append(f"summon minecraft:sheep {POS['sheep'][0]} {POS['sheep'][1]} {POS['sheep'][2]} {{NoAI:1b,PersistenceRequired:1b}}")
    cmds.append(f"summon minecraft:villager {POS['villager'][0]} {POS['villager'][1]} {POS['villager'][2]} {{NoAI:1b,PersistenceRequired:1b}}")
    # Portal frame at z=1996, x=2000..2003, y=150..154 (interior 2x3)
    px, py, pz = POS["portal"]
    frame_x = range(px - 1, px + 3)
    for x in frame_x:
        cmds.append(f"setblock {x} 150 {pz} minecraft:obsidian replace")
        cmds.append(f"setblock {x} 154 {pz} minecraft:obsidian replace")
    for y in range(150, 155):
        cmds.append(f"setblock {px - 1} {y} {pz} minecraft:obsidian replace")
        cmds.append(f"setblock {px + 2} {y} {pz} minecraft:obsidian replace")
    for x in range(px, px + 2):
        for y in (151, 152, 153):
            cmds.append(f"setblock {x} {y} {pz} minecraft:nether_portal replace")
    return rcon_batch(cmds)


def wait_ready(bot: Any, *, timeout_s: float = 45.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    st: dict[str, Any] = {}
    while time.monotonic() < deadline:
        st = bot.status()
        if st.get("ready") and st.get("alive") is not False:
            return st
        time.sleep(1.0)
    return st


def hud_memory_line(toolset: Any, task: str) -> tuple[str, list[str]]:
    from mc_agent.harness.hud import build_world_hud

    text = build_world_hud(toolset, task=task)
    return text, [ln for ln in text.splitlines() if ln.startswith("记忆库:")]


def main() -> int:
    os.environ["MC_STATE_DIR"] = str(STATE_DIR)
    os.environ["MC_WORLD_MEMORY_PATH"] = str(MEM_PATH)
    MEM_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEM_PATH.unlink(missing_ok=True)

    from mc_agent.auth import TokenStore
    from mc_agent.bot import BotSession
    from mc_agent.config import Settings
    from mc_agent.errors import ApiError
    from mc_agent.harness.mc_tools import McToolset
    from mc_agent.harness.world_memory import WorldMemory

    settings = Settings.load(dotenv_path=ROOT / ".env.beast")
    record = TokenStore(STATE_DIR / "tokens.json").get(BOT)
    if record is None:
        raise SystemExit(f"no cached token for {BOT}")
    bot = BotSession(record.token, bot_username=record.bot_username, base_url=settings.mc_api_base_url)
    toolset = McToolset(bot)
    report: dict[str, Any] = {"bot": BOT, "ok": True, "checks": {}}

    def check(key: str, value: Any, ok: bool) -> None:
        report["checks"][key] = {"ok": bool(ok), "value": value}
        if not ok:
            report["ok"] = False

    try:
        report["setup"] = setup_world()
        try:
            st = bot.status()
        except ApiError:
            st = {}
        if not st.get("ready"):
            deadline = time.monotonic() + 420
            last = ""
            while time.monotonic() < deadline:
                try:
                    bot.connect(respect_reconnect_interval=False)
                    st = bot.wait_until_ready(timeout_s=30.0)
                    if st.get("ready"):
                        break
                except ApiError as exc:
                    last = f"{exc.code}: {exc.message}"
                time.sleep(8)
            else:
                raise RuntimeError(f"connect failed: {last}")
        st = wait_ready(bot)
        report["ready"] = {k: st.get(k) for k in ("ready", "pos", "health", "food", "alive", "dimension", "biome")}

        # M1 anchors + chunks
        s0 = bot.call("status", method="GET")
        toolset.world_memory.observe_status(s0)
        check(
            "M1_spawn_biome",
            {"spawn": toolset.world_memory.spawn_points, "biomes": list(toolset.world_memory.biomes), "status_biome": s0.get("biome")},
            "overworld" in toolset.world_memory.spawn_points,
        )
        biome_supported = bool(s0.get("biome"))
        check(
            "M1_biome_layer",
            {
                "status_biome": s0.get("biome"),
                "supported": biome_supported,
                "biomes": list(toolset.world_memory.biomes),
                "note": "" if biome_supported else "Leaves/API status.biome 为空，群系层仅由离线单测覆盖",
            },
            True,
        )
        for x in (2025, 2055, 2085, 2105):
            json.loads(toolset.mc_move_to(x=x, z=1994))
            toolset.world_memory.observe_status(bot.status())
        vc = [k for k in toolset.world_memory.visited_chunks if k[0] == "overworld"]
        check("M1_visited_chunks", {"count": len(vc), "chunks": vc}, len(vc) >= 4)

        # M2 POI record + recall + stale pop + relay mine
        json.loads(toolset.mc_move_to(x=BASE[0], z=BASE[2]))
        toolset.world_memory.observe_status(bot.status())
        near_scan = json.loads(toolset.mc_find_blocks(radius=32, types=["gold_block"]))
        json.loads(toolset.mc_move_to(x=2100, z=1994))
        toolset.world_memory.observe_status(bot.status())
        far_scan = json.loads(toolset.mc_find_blocks(radius=16, types=["gold_block"]))
        json.loads(toolset.mc_move_to(x=BASE[0], z=BASE[2]))
        toolset.world_memory.observe_status(bot.status())
        poi_before = [(e.name, list(e.pos)) for e in toolset.world_memory.pois.values()]
        check(
            "M2_poi_recorded",
            {"near": near_scan.get("result", {}).get("blocks"), "far": far_scan.get("result", {}).get("blocks"), "pois": poi_before},
            any(e[0] == "gold_block" and e[1] == list(POS["near_gold"]) for e in poi_before)
            and any(e[0] == "gold_block" and e[1] == list(POS["far_gold"]) for e in poi_before),
        )
        rcon_batch([f"setblock {POS['near_gold'][0]} {POS['near_gold'][1]} {POS['near_gold'][2]} minecraft:air"])
        time.sleep(1.0)  # let the client receive the block update before scanning
        mem_scan = json.loads(toolset.mc_find_blocks(radius=32, types=["gold_block"]))
        mem_blocks = mem_scan.get("result", {}).get("memory_blocks") or []
        check(
            "M2_stale_pop_and_recall",
            {
                "local": mem_scan.get("result", {}).get("blocks"),
                "memory_blocks": mem_blocks,
                "pois": [(e.name, list(e.pos)) for e in toolset.world_memory.pois.values()],
            },
            mem_scan.get("result", {}).get("blocks") == []
            and any(m.get("pos") == list(POS["far_gold"]) for m in mem_blocks)
            and not any(e.pos == POS["near_gold"] for e in toolset.world_memory.pois.values()),
        )
        mine = json.loads(toolset.mc_skill_mine_blocks(["gold_block"], count=1, radius=32))
        check(
            "M2_segmented_recall_mine",
            mine,
            bool(mine.get("ok"))
            and (mine.get("recalled_from_memory") or {}).get("pos") == list(POS["far_gold"])
            and (mine.get("target_gained") or {}).get("gold_block", 0) >= 1,
        )
        rcon_batch([f"clear {BOT} minecraft:iron_pickaxe"])
        back = json.loads(toolset.mc_move_to(x=BASE[0], z=BASE[2]))
        toolset.world_memory.observe_status(bot.status())
        check(
            "M2_segmented_return",
            {"move": back, "pos": bot.status().get("pos")},
            bool(back.get("ok")) and abs(float(bot.status().get("pos", [0])[0]) - BASE[0]) <= 6,
        )

        # M3 home base + chest snapshot + shortage hint
        scan_home = json.loads(
            toolset.mc_find_blocks(radius=16, types=["crafting_table", "furnace", "chest"])
        )
        check(
            "M3_home_promotion",
            {"scan": scan_home, "home": toolset.world_memory.home_bases},
            "overworld" in toolset.world_memory.home_bases,
        )
        chest = POS["chest"]
        wv = json.loads(toolset.mc_window_view(block="chest", x=chest[0], y=chest[1], z=chest[2]))
        stored = toolset.world_memory.containers.get(("overworld", *chest), {}).get("items", {})
        check(
            "M3_container_snapshot",
            {"view": wv, "stored": stored},
            stored.get("iron_ingot") == 8 and stored.get("bread") == 5,
        )
        short = json.loads(toolset.mc_skill_craft_gear("iron_pickaxe", count=1))
        check(
            "M3_shortage_memory_hint",
            short,
            short.get("ok") is False and "iron_ingot×8" in str(short.get("memory_hint", "")),
        )
        bed = POS["bed"]
        bind = json.loads(toolset.mc_skill_bind_bed(bed[0], bed[1], bed[2]))
        check(
            "M3_bed_anchor",
            {"bind": bind, "bed": toolset.world_memory.bed_spawn},
            bool(bind.get("ok")) and (toolset.world_memory.bed_spawn or {}).get("pos") == list(bed),
        )

        # M4 habitats + HUD
        near_ent = json.loads(toolset.mc_get_entities(radius=32, types=["sheep", "villager"]))
        toolset.mc_move_to(x=2105, z=1994)
        toolset.world_memory.observe_status(bot.status())
        far_ent = json.loads(toolset.mc_get_entities(radius=32, types=["villager"]))
        check(
            "M4_habitats",
            {"near": near_ent.get("result", {}).get("entities"), "far": far_ent.get("result", {}).get("memory_habitats")},
            bool(far_ent.get("result", {}).get("memory_habitats")),
        )
        hud_text, mem_lines = hud_memory_line(toolset, "寻找村民与回家整备")
        check(
            "M4_hud_budget",
            {"hud_len": len(hud_text), "memory_lines": mem_lines},
            len(mem_lines) == 1 and len(mem_lines[0]) <= 84 and len(hud_text) < 650,
        )

        # M5 persistence
        toolset.world_memory.save(force=True)
        reloaded = WorldMemory(MEM_PATH)
        mode = stat.S_IMODE(MEM_PATH.stat().st_mode)
        check(
            "M5_persistence",
            {
                "mode": oct(mode),
                "spawn": reloaded.spawn_points,
                "pois": [(e.name, list(e.pos)) for e in reloaded.pois.values()],
                "containers": list(reloaded.containers),
                "biomes": list(reloaded.biomes),
                "chunks": len(reloaded.visited_chunks),
            },
            mode == 0o600
            and "overworld" in reloaded.spawn_points
            and bool(reloaded.pois)
            and ("overworld", *POS["chest"]) in reloaded.containers
            and (not biome_supported or bool(reloaded.biomes))
            and len(reloaded.visited_chunks) >= 4,
        )

        # M6 portal anchor + dimension separation
        toolset.mc_move_to(x=BASE[0], z=BASE[2])
        toolset.world_memory.observe_status(bot.status())
        portal_scan = json.loads(toolset.mc_find_blocks(radius=16, types=["nether_portal"]))
        check(
            "M6_portal_anchor",
            {"scan": portal_scan, "portals": toolset.world_memory.portals},
            bool(toolset.world_memory.portals.get("overworld")),
        )
        rcon_batch([f"gamemode spectator {BOT}", f"execute in minecraft:the_nether run tp {BOT} 0 100 0"])
        time.sleep(2.0)
        toolset.world_memory.observe_status(bot.status())
        nether_ok = "the_nether" in toolset.world_memory.spawn_points and any(
            k[0] == "the_nether" for k in toolset.world_memory.visited_chunks
        )
        rcon_batch([f"execute in minecraft:overworld run tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]}", f"gamemode survival {BOT}"])
        time.sleep(1.5)
        toolset.world_memory.observe_status(bot.status())
        check(
            "M6_dimension_separation",
            {
                "spawns": toolset.world_memory.spawn_points,
                "nether_chunks": [k for k in toolset.world_memory.visited_chunks if k[0] == "the_nether"][:5],
            },
            nether_ok and "overworld" in toolset.world_memory.spawn_points,
        )

        # M6 last_death (one controlled death at the end)
        rcon_batch([f"kill {BOT}"])
        time.sleep(6.0)
        try:
            st_dead = bot.status()
        except Exception:
            st_dead = {}
        if st_dead:
            toolset.world_memory.observe_status(st_dead)
        check(
            "M6_last_death",
            {"status": {k: st_dead.get(k) for k in ("pos", "last_death", "alive", "ready")}, "memory": toolset.world_memory.last_death},
            bool(toolset.world_memory.last_death),
        )

        report["final_status"] = {k: bot.status().get(k) for k in ("ready", "pos", "health", "food", "alive", "dimension")}
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            report["cleanup"] = rcon_batch(
                [
                    f"gamemode survival {BOT}",
                    f"tp {BOT} {BASE[0]} {BASE[1]} {BASE[2]}",
                    f"effect give {BOT} minecraft:resistance 1800 4 true",
                    "forceload remove 1988 1988 2132 2002",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            report["cleanup_error"] = str(exc)
        bot.close()

    out = ROOT / "runs" / f"world-memory-live-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "checks": {k: v["ok"] for k, v in report["checks"].items()}, "report": str(out)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
